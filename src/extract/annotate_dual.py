# -*- coding: utf-8 -*-
"""双 LLM 独立标注：对种子集每篇文档分别调用标注者 A/B，增量落盘、断点续跑。

用法:
    python src/extract/annotate_dual.py                  # 跑全部 60 篇 x 2 模型
    python src/extract/annotate_dual.py --retry-failed   # 只补跑此前失败的 doc×model
    python src/extract/annotate_dual.py --workers 4 --input data/annotation/seed_v1.jsonl
"""
import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ANNOTATION_SYSTEM_PROMPT, call_llm, load_api_config, validate  # noqa: E402


def validate_lenient(text):
    """validate() 的宽松版：用 raw_decode 解析首个 JSON 对象，忽略尾随多余内容。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").removeprefix("json").strip()
    start = t.find("{")
    if start < 0:
        return None, ["lenient: 未找到 JSON 起始"]
    try:
        obj, _ = json.JSONDecoder().raw_decode(t[start:])
    except Exception as e:
        return None, [f"lenient JSON 解析失败: {e}"]
    return validate(json.dumps(obj, ensure_ascii=False))

MODELS = {"A": "kimi-k3", "B": "gpt-6-astra"}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("annotate_dual")

_write_lock = threading.Lock()


def load_seed(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def latest_raw(out_dir):
    files = sorted(Path(out_dir).glob("dual_raw_*.json"))
    return files[-1] if files else None


def save_results(path, results):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def annotate_one(cfg, doc, label, model):
    user = (f"标题：{doc['title']}\n发布日期：{doc['publish_date']}\n\n"
            f"正文：\n{doc['content_text'][:6000]}")
    rec = {"doc_id": doc["doc_id"], "annotator": label, "model": model,
           "events": None, "error": None, "validation_errors": [],
           "usage": {}, "elapsed": None, "status": "ok"}
    try:
        text, elapsed, usage = call_llm(cfg, model, ANNOTATION_SYSTEM_PROMPT, user)
        rec["elapsed"] = round(elapsed, 2)
        rec["usage"] = usage
        events, errs = validate(text)
        if events is None:
            # 兜底：模型在合法 JSON 后追加了多余文字时，截取首个完整 JSON 对象重试
            events, errs = validate_lenient(text)
            if events is not None:
                errs = errs + ["lenient: 截取了首个 JSON 对象后的多余输出"]
        if events is None:
            rec["status"] = "failed"
            rec["error"] = f"输出校验失败: {'; '.join(errs)}"
            rec["validation_errors"] = errs
            rec["raw"] = text[:2000]
        else:
            rec["events"] = events
            rec["validation_errors"] = errs
            if errs:
                rec["status"] = "ok_with_validation_errors"
    except Exception as e:
        rec["status"] = "failed"
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(ROOT / "data" / "annotation" / "seed_v1.jsonl"))
    ap.add_argument("--config", default=str(ROOT / "config" / "api.local.yaml"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retry-failed", action="store_true",
                    help="重新尝试此前失败的 doc×model（默认跳过所有已记录项）")
    args = ap.parse_args()

    cfg = load_api_config(args.config)
    docs = load_seed(args.input)
    out_dir = ROOT / "data" / "annotation"

    # 断点续跑：复用最新的 raw 文件
    existing = latest_raw(out_dir)
    if existing:
        out_path = existing
        results = json.loads(out_path.read_text(encoding="utf-8"))
        logger.info("续跑：加载已有结果 %s（%d 条记录）", out_path.name, len(results))
    else:
        out_path = out_dir / f"dual_raw_{time.strftime('%Y%m%d_%H%M%S')}.json"
        results = []

    done = {(r["doc_id"], r["model"]) for r in results}
    if args.retry_failed:
        failed = {(r["doc_id"], r["model"]) for r in results if r["status"] == "failed"}
        done -= failed
        results = [r for r in results if r["status"] != "failed"]
        logger.info("--retry-failed：将重试 %d 个失败项", len(failed))

    tasks = []
    for doc in docs:
        for label, model in MODELS.items():
            if (doc["doc_id"], model) not in done:
                tasks.append((doc, label, model))
    logger.info("待标注 %d 项（doc×model），已完成跳过 %d 项", len(tasks), len(done))
    if not tasks:
        logger.info("无待办，结果文件: %s", out_path)
        return

    t_start = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(annotate_one, cfg, doc, label, model): (doc, model)
                   for doc, label, model in tasks}
        for fut in as_completed(futures):
            doc, model = futures[fut]
            rec = fut.result()
            with _write_lock:
                results.append(rec)
                save_results(out_path, results)  # 每完成一条即落盘
            n = len(rec["events"]) if rec["events"] is not None else "-"
            if rec["status"] == "failed":
                fail += 1
            else:
                ok += 1
            logger.info("doc=%s model=%s 耗时=%ss 事件数=%s 状态=%s",
                        rec["doc_id"], model, rec["elapsed"], n, rec["status"])
            if rec["status"] == "failed":
                logger.warning("  ! %s", rec["error"])

    dt = time.time() - t_start
    logger.info("完成：本轮成功 %d、失败 %d，总记录 %d，耗时 %.0fs，结果文件 %s",
                ok, fail, len(results), dt, out_path)
    if fail:
        failed_list = [(r["doc_id"], r["model"]) for r in results if r["status"] == "failed"]
        logger.warning("失败清单: %s", failed_list)


if __name__ == "__main__":
    main()
