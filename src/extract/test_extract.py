# -*- coding: utf-8 -*-
"""L2 事件抽取：多模型对比测试脚本。

对同一批样本文档调用多个候选模型，输出抽取 JSON 并做基本校验
（JSON 可解析、枚举值合法、evidence_span 非空），用于人工对比抽取质量。

用法:
    python src/extract/test_extract.py                 # 默认 2 篇样本 x 4 个模型
    python src/extract/test_extract.py --doc-ids 1 9   # 指定文档
    python src/extract/test_extract.py --models deepseek-v4-pro qwen3.7-max
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "gec.db"

SYSTEM_PROMPT = """你是绿证市场政策分析专家。从给定政策/新闻文档中抽取"政策事件"，输出严格 JSON。

事件类型（event_type.子类）：
A 政策发布: A1顶层规划 A2实施细则 A3管理办法 A4征求意见
B 考核范围扩容: B1新增考核行业 B2新增考核主体类型 B3考核力度升级
C 消纳责任权重调整: C1年度权重下达 C2权重上调或下调 C3省级分解方案
D 价格信号: D1官方价格通报 D2成交量通报 D3价格异常说明
E 供需冲击: E1核发放量 E2需求放量 E3供给受限
F 市场规则变更: F1交易规则 F2绿电直连 F3核销与有效期 F4国际互认

每条事件输出字段：
- event_type: A/B/C/D/E/F
- subtype: 如 "C1"
- actor: 发布主体（规范机构名）
- object: 受影响对象（无则空字符串）
- strength: 约束性/预期性/倡导性（"约束性指标/考核"→约束性；"预期/力争"→预期性；"鼓励/支持"→倡导性）
- effective_date: YYYY-MM-DD 或 YYYY-MM 或 "未明确"（注意：生效时间≠发布时间）
- region: 全国 或 省份名
- industry: 涉及行业数组
- price_direction_prior: 该事件对绿证价格的预期方向：上行/下行/中性/不明
- evidence_span: 原文证据片段（不超过100字，必须是原文原句）

规则：
1. 一篇文档可含多条事件；同子类同对象只保留一条
2. 无政策事件则输出空数组（严禁强行编造）
3. 新闻评论只抽取其中引用的官方事实，不抽观点
4. 只输出 JSON 对象 {"events": [...]}，不要输出任何其他文字"""


def extract(model, title, pub_date, content):
    user = f"标题：{title}\n发布日期：{pub_date}\n\n正文：\n{content[:6000]}"
    t0 = time.time()
    r = requests.post(
        f"{CFG['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {CFG['api_key']}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        },
        timeout=CFG.get("timeout", 120),
    )
    elapsed = time.time() - t0
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return text, elapsed, usage


VALID_TYPES = set("ABCDEF")
VALID_SUBTYPES = {f"{t}{i}" for t, n in [("A", 4), ("B", 3), ("C", 3), ("D", 3), ("E", 3), ("F", 4)] for i in range(1, n + 1)}
VALID_STRENGTH = {"约束性", "预期性", "倡导性"}
VALID_DIR = {"上行", "下行", "中性", "不明"}


def validate(text):
    """返回 (events 或 None, 错误列表)"""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").removeprefix("json").strip()
    try:
        obj = json.loads(t)
        events = obj["events"]
    except Exception as e:
        return None, [f"JSON 解析失败: {e}"]
    errs = []
    for i, ev in enumerate(events):
        if ev.get("event_type") not in VALID_TYPES:
            errs.append(f"事件{i}: event_type 非法 {ev.get('event_type')}")
        if ev.get("subtype") not in VALID_SUBTYPES:
            errs.append(f"事件{i}: subtype 非法 {ev.get('subtype')}")
        if ev.get("strength") not in VALID_STRENGTH:
            errs.append(f"事件{i}: strength 非法 {ev.get('strength')}")
        if ev.get("price_direction_prior") not in VALID_DIR:
            errs.append(f"事件{i}: price_direction_prior 非法 {ev.get('price_direction_prior')}")
        if not ev.get("evidence_span"):
            errs.append(f"事件{i}: evidence_span 为空")
    return events, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-ids", type=int, nargs="+", default=None)
    ap.add_argument("--models", nargs="+", default=["deepseek-v4-flash", "deepseek-v4-pro", "qwen3.7-max", "gpt-5.6-sol"])
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--config", default="config/api.local.yaml", help="API 配置文件（相对项目根目录）")
    args = ap.parse_args()

    global CFG
    from common import load_api_config
    CFG = load_api_config(ROOT / args.config)

    conn = sqlite3.connect(DB)
    if args.doc_ids:
        marks = ",".join("?" * len(args.doc_ids))
        docs = conn.execute(
            f"SELECT id, title, publish_date, content_text FROM documents WHERE id IN ({marks})",
            args.doc_ids,
        ).fetchall()
    else:
        docs = conn.execute(
            "SELECT id, title, publish_date, content_text FROM documents "
            "WHERE length(content_text) > 500 ORDER BY length(content_text) DESC LIMIT ?",
            (args.n,),
        ).fetchall()
    conn.close()
    if not docs:
        sys.exit("没有找到样本文档")

    out_path = ROOT / "data" / "annotation" / f"extract_test_{time.strftime('%Y%m%d_%H%M%S')}.json"
    results = []
    for doc_id, title, pub, content in docs:
        print(f"\n{'='*70}\n文档 #{doc_id}: {title} ({pub})  正文{len(content)}字")
        for model in args.models:
            try:
                text, elapsed, usage = extract(model, title, pub, content)
            except Exception as e:
                print(f"  [{model}] 调用失败: {e}")
                results.append({"doc_id": doc_id, "model": model, "error": str(e)})
                continue
            events, errs = validate(text)
            n = len(events) if events is not None else "?"
            status = "OK" if not errs else f"{len(errs)} 处校验问题"
            print(f"  [{model}] {elapsed:.1f}s  事件数={n}  in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}  {status}")
            for e in errs:
                print(f"      ! {e}")
            results.append({
                "doc_id": doc_id, "model": model, "elapsed": elapsed, "usage": usage,
                "events": events, "validation_errors": errs, "raw": text,
            })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整结果已存: {out_path}")


if __name__ == "__main__":
    main()
