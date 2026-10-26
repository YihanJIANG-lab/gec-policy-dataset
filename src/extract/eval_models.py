# -*- coding: utf-8 -*-
"""四方模型事件抽取正式评测（论文 Table 1）。

对 gold_v1.jsonl 60 篇，用统一 prompt（common.ANNOTATION_SYSTEM_PROMPT）调用：
  - deepseek-flash / deepseek-v4-pro（官方端点 config/api.deepseek.yaml）
  - gpt-5.6-sol / gemini-3.8-flash（中转站 config/api.local.yaml）
  注：qwen3.7-max 因中转站网关强制 x-opencode-session 不可达（400 MissingSessionID），
  按预订规则以同站可用的 gemini-3.8-flash 替代，替代事实写入结果 meta。

评测对齐（与 build_review_excel.py v2 同口径）：
  事件配对 = subtype 精确 + evidence_span 相似度>=0.5 贪心；
  实质字段 strength/effective_date/region/price_direction_prior 用相同归一化。
指标：Event P/R/F1（配对=TP）；Attr-F1（配对事件上 4 个实质字段的逐项一致率，
宏平均）；Evidence-F1（配对事件 evidence 相似度均值）；
Avg tokens（completion_tokens 均值）；成本按显式假设价折算。

输出：data/annotation/eval_v1.json（原始+指标）、03_论文投稿/paper/tab_extraction.tex（booktabs）。
"""
import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ANNOTATION_SYSTEM_PROMPT, call_llm, load_api_config, validate  # noqa: E402
from build_review_excel import COMPARE_FIELDS, norm_substantive, pair_events  # noqa: E402

logger = logging.getLogger("eval_models")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)

SEED = ROOT / "data" / "annotation" / "seed_v1.jsonl"
GOLD = ROOT / "data" / "annotation" / "gold_v1.jsonl"
EVAL_OUT = ROOT / "data" / "annotation" / "eval_v1.json"
TEX_OUT = ROOT / "03_论文投稿" / "paper" / "tab_extraction.tex"

MODELS = [
    {"name": "deepseek-flash", "config": "api.deepseek.yaml"},
    {"name": "deepseek-v4-pro", "config": "api.deepseek.yaml"},
    {"name": "gpt-5.6-sol", "config": "api.local.yaml"},
    {"name": "gemini-3.8-flash", "config": "api.local.yaml",
     "note": "替代 qwen3.7-max（中转站网关 400 MissingSessionID 不可达，2026-10-05 探测）"},
]
QWEN_PROBE = {"model": "qwen3.7-max", "status": "endpoint_unavailable",
              "error": "400 MissingSessionID: 中转站网关强制 x-opencode-session，"
                       "普通 API key 无法满足（2026-10-05 探测）"}

# 成本假设（公开报价/折算，仅供量级参考，随 tex 注释一并披露）
# 口径：CNY/篇（60 篇 gold 单次扫描总成本 ÷ 60）；汇率假设 USD/CNY=7.1（2026-10-05）
COST_ASSUMPTIONS = {
    "deepseek-flash": {"in": 1.0, "out": 4.0, "note": "DeepSeek 官方假设价 flash ¥1/M in、¥4/M out（汇率 7.1，2026-10-05）"},
    "deepseek-v4-pro": {"in": 4.0, "out": 16.0, "note": "DeepSeek 官方假设价 pro ¥4/M in、¥16/M out（汇率 7.1，2026-10-05）"},
    "gpt-5.6-sol": {"in": 2.0, "out": 2.0, "note": "中转站折算假设 ¥2/M（汇率 7.1，2026-10-05）"},
    "gemini-3.8-flash": {"in": 2.0, "out": 2.0, "note": "中转站折算假设 ¥2/M（汇率 7.1，2026-10-05）"},
}

FIELD_LABELS = {"strength": "Strength", "effective_date": "Eff.Date",
                "region": "Region", "price_direction_prior": "PriceDir"}


def eval_one(cfg, model, doc, gold_events):
    user = (f"标题：{doc['title']}\n发布日期：{doc['publish_date']}\n\n"
            f"正文：\n{doc['content_text'][:6000]}")
    rec = {"doc_id": doc["doc_id"], "model": model, "status": "ok",
           "events": None, "error": None, "usage": {}, "elapsed": None}
    try:
        text, elapsed, usage = call_llm(cfg, model, ANNOTATION_SYSTEM_PROMPT, user)
        rec["elapsed"] = round(elapsed, 2)
        rec["usage"] = usage
        events, errs = validate(text)
        if events is None:
            rec["status"] = "failed"
            rec["error"] = f"输出校验失败: {'; '.join(errs)[:200]}"
            rec["raw"] = text[:1500]
        else:
            rec["events"] = events
            rec["validation_errors"] = errs
    except Exception as e:
        rec["status"] = "failed"
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    return rec


def compute_metrics(records, gold_by_doc):
    """按模型汇总 TP/FP/FN、字段一致率、证据相似度、token。"""
    per_model = {}
    for model in {r["model"] for r in records}:
        tp = fp = fn = 0
        field_correct = {f: 0 for f in COMPARE_FIELDS}
        sims = []
        in_tok, out_tok, n_ok = 0, 0, 0
        for r in records:
            if r["model"] != model:
                continue
            gold_events = gold_by_doc[r["doc_id"]]
            pred = r.get("events") or []
            if r["status"] == "failed":
                fn += len(gold_events)  # 调用失败：该篇 gold 全部漏抽
                continue
            n_ok += 1
            u = r.get("usage") or {}
            in_tok += u.get("prompt_tokens", 0)
            out_tok += u.get("completion_tokens", 0)
            pairs, only_pred, only_gold = pair_events(pred, gold_events)
            tp += len(pairs)
            fp += len(only_pred)
            fn += len(only_gold)
            for pe, ge, sim in pairs:
                sims.append(sim)
                for f in COMPARE_FIELDS:
                    if norm_substantive(pe, f) == norm_substantive(ge, f):
                        field_correct[f] += 1
        p = tp / (tp + fp) if tp + fp else 0.0
        r_ = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r_ / (p + r_) if p + r_ else 0.0
        field_acc = {f: (field_correct[f] / tp if tp else 0.0) for f in COMPARE_FIELDS}
        attr_f1 = sum(field_acc.values()) / len(field_acc)
        ev_f1 = sum(sims) / len(sims) if sims else 0.0
        avg_out = out_tok / n_ok if n_ok else 0.0
        ca = COST_ASSUMPTIONS[model]
        cost = (in_tok * ca["in"] + out_tok * ca["out"]) / 1e6
        per_model[model] = {
            "TP": tp, "FP": fp, "FN": fn,
            "P": round(p, 4), "R": round(r_, 4), "F1": round(f1, 4),
            "field_acc": {f: round(a, 4) for f, a in field_acc.items()},
            "AttrF1": round(attr_f1, 4),
            "EvidenceF1": round(ev_f1, 4),
            "calls_ok": n_ok, "calls_failed": 60 - n_ok,
            "prompt_tokens": in_tok, "completion_tokens": out_tok,
            "avg_completion_tokens": round(avg_out, 1),
            "cost_cny": round(cost, 2), "cost_assumption": ca["note"],
        }
    return per_model


def write_tex(per_model, gold_name="gold_v1.jsonl", version="v1"):
    lines = [
        f"% Auto-generated by src/extract/eval_models.py -- Table 1 (four-model extraction benchmark, {version})",
        f"% Data: data/annotation/eval_v1.json (gold {gold_name}, 60 human-adjudicated documents)",
        "% Fld-Agree = field agreement (macro): macro-average of per-field agreement on matched pairs",
        "% over 4 substantive fields; Evid-F1 = mean evidence-span similarity on matched pairs",
        "% (evaluation convention, not a standard metric). Costs are estimates; see table notes.",
        "\\begin{table*}[t]",
        "  \\centering",
        "  \\footnotesize",
        "  \\caption{Four-model policy event extraction benchmark (60-document gold set, temperature 0). "
        "Fld-Agree: field agreement (macro) over strength, effective date, region, price-direction prior "
        "on matched pairs; Evid-F1: mean evidence-span similarity on matched pairs (evaluation convention).}",
        "  \\label{tab:extraction}",
        "  \\begin{tabular}{lccccccc}",
        "    \\toprule",
        "    Model & P & R & F1 & Fld-Agree & Evid-F1 & Avg tok & CNY/doc \\\\",
        "    \\midrule",
    ]
    for m in MODELS:
        name = m["name"]
        s = per_model.get(name)
        if not s:
            continue
        lines.append(
            f"    {name} & {s['P']:.3f} & {s['R']:.3f} & \\textbf{{{s['F1']:.3f}}} & "
            f"{s['AttrF1']:.3f} & {s['EvidenceF1']:.3f} & "
            f"{s['avg_completion_tokens']:.0f} & {s['cost_cny']/60:.4f} \\\\")
    lines += [
        "    \\midrule",
        "    \\multicolumn{8}{l}{\\footnotesize Assumed prices: DeepSeek official flash CNY1/M in, "
        "CNY4/M out; pro CNY4/M in, CNY16/M out; relay flat CNY2/M (USD/CNY 7.1, 2026-10-05).}\\\\",
        "    \\multicolumn{8}{l}{\\footnotesize Qwen3.7-Max unreachable after the relay gateway "
        "changed authentication (MissingSessionID, 2026-10); same-tier Gemini-3.8-Flash substituted.}\\\\",
        "% Per-field agreement (macro components), for the record:",
    ]
    for m in MODELS:
        s = per_model.get(m["name"])
        if s:
            fa = s["field_acc"]
            lines.append(
                f"%   {m['name']}: strength={fa['strength']:.3f} "
                f"eff.date={fa['effective_date']:.3f} region={fa['region']:.3f} "
                f"price-dir={fa['price_direction_prior']:.3f}")
    lines += [
        "    \\bottomrule",
        "  \\end{tabular}",
        "\\end{table*}",
    ]
    TEX_OUT.parent.mkdir(parents=True, exist_ok=True)
    TEX_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("LaTeX 表格已写: %s", TEX_OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--gold", default=str(GOLD), help="gold jsonl 路径（默认 gold_v1.jsonl）")
    ap.add_argument("--docs", type=int, nargs="+", default=None,
                    help="只重跑这些 doc_id（其旧记录先移除），指标仍按全部 60 篇计算")
    ap.add_argument("--version", default="v1", help="写入 meta 的版本标记")
    args = ap.parse_args()

    cfgs = {}
    for m in MODELS:
        if m["config"] not in cfgs:
            cfgs[m["config"]] = load_api_config(ROOT / "config" / m["config"])

    seed = [json.loads(l) for l in SEED.read_text(encoding="utf-8").splitlines() if l.strip()]
    seed_by_id = {d["doc_id"]: d for d in seed}
    gold_by_doc = {json.loads(l)["doc_id"]: json.loads(l)["events"]
                   for l in Path(args.gold).read_text(encoding="utf-8").splitlines() if l.strip()}

    records = []
    if EVAL_OUT.exists():
        prev = json.loads(EVAL_OUT.read_text(encoding="utf-8"))
        records = prev.get("records", [])
        logger.info("续跑：已有 %d 条记录", len(records))
    if args.docs:
        subset = set(args.docs)
        before = len(records)
        records = [r for r in records if r["doc_id"] not in subset]
        logger.info("--docs：移除 %d 篇的 %d 条旧记录，将重跑", len(subset), before - len(records))
    done = {(r["doc_id"], r["model"]) for r in records}
    if args.retry_failed:
        results_keep = [r for r in records if r["status"] != "failed"]
        retry_n = len(records) - len(results_keep)
        records = results_keep
        done = {(r["doc_id"], r["model"]) for r in records}
        logger.info("--retry-failed：重试 %d 项", retry_n)

    doc_ids = args.docs if args.docs else list(gold_by_doc)
    tasks = [(seed_by_id[did], m)
             for did in doc_ids for m in MODELS
             if (did, m["name"]) not in done]
    logger.info("待评测 %d 项（doc×model）", len(tasks))

    def save():
        per_model = compute_metrics(records, gold_by_doc)
        payload = {
            "meta": {
                "version": args.version,
                "gold": Path(args.gold).name, "n_docs": len(gold_by_doc),
                "prompt": "common.ANNOTATION_SYSTEM_PROMPT（含日期精度规则）",
                "alignment": "subtype 精确 + evidence_span SequenceMatcher>=0.5 贪心；"
                             "实质字段归一化同 build_review_excel v2",
                "model_notes": {m["name"]: m.get("note") for m in MODELS if m.get("note")},
                "qwen_probe": QWEN_PROBE,
                "cost_assumptions": COST_ASSUMPTIONS,
            },
            "metrics": per_model,
            "records": records,
        }
        tmp = EVAL_OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(EVAL_OUT)
        return per_model

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(eval_one, cfgs[m["config"]], m["name"], doc,
                            gold_by_doc[doc["doc_id"]]): (doc["doc_id"], m["name"])
                for doc, m in tasks}
        for fut in as_completed(futs):
            doc_id, model = futs[fut]
            rec = fut.result()
            records.append(rec)
            save()  # 增量落盘
            n = len(rec["events"]) if rec["events"] is not None else "-"
            if rec["status"] == "failed":
                fail += 1
                logger.warning("doc=%s model=%s failed: %s", doc_id, model, rec["error"])
            else:
                ok += 1
            logger.info("doc=%s model=%s %ss 事件数=%s 状态=%s",
                        doc_id, model, rec["elapsed"], n, rec["status"])

    per_model = save()
    write_tex(per_model, Path(args.gold).name, args.version)

    print("\n===== 评测指标（60 篇 gold）=====")
    print(f"{'model':<20} {'P':>7} {'R':>7} {'F1':>7} {'AttrF1':>7} {'EvidF1':>7} "
          f"{'avgTok':>8} {'cost¥':>7} {'ok/60':>6}")
    for m in MODELS:
        s = per_model[m["name"]]
        print(f"{m['name']:<20} {s['P']:>7.3f} {s['R']:>7.3f} {s['F1']:>7.3f} "
              f"{s['AttrF1']:>7.3f} {s['EvidenceF1']:>7.3f} "
              f"{s['avg_completion_tokens']:>8.0f} {s['cost_cny']:>7.2f} {s['calls_ok']:>6}")
    print(f"\n本轮调用：成功 {ok}，失败 {fail}")


if __name__ == "__main__":
    main()
