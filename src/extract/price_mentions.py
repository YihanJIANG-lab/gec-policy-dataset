# -*- coding: utf-8 -*-
"""从自有语料（documents 表）中抽取绿证量价表述，写 data/annotation/price_mentions.json。

用量大价低的模型（gemini-3.8-flash），输出"时间+指标+数值+口径+原文句"三元组，
供 backfill_prices.py 筛选入 prices 表。幂等：覆盖写输出文件。
"""
import json
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import call_llm, load_api_config  # noqa: E402

logger = logging.getLogger("price_mentions")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)

PROMPT = """你是数据抽取助手。从给定中文政策/新闻文档中抽取所有关于**绿证（绿色电力证书）或绿色电力交易的价格与成交量**的具体数据表述。

每条输出：
- metric: price（价格）/ volume（成交量）/ issuance（核发量）
- period: 数据所属时间段，按原文精度（如 "2024" / "2024-05" / "2024上半年" / "2026-07"）
- value: 数值（纯数字）
- unit: 原文单位（如 元/个、元/张、万个、亿个、亿千瓦时）
- caliber: 口径说明（如 挂牌均价/成交均价/累计交易量/月度交易量/参考价；原文没写就填空字符串）
- evidence: 原文原句（不超过100字）

规则：
1. 只抽绿证/绿色电力交易的量价数据；绿电电量交易价格（元/千瓦时）不要
2. 无相关数据输出空数组；严禁编造或推测
3. 只输出 JSON 对象 {"mentions": [...]}"""


def extract_doc(cfg, model, doc):
    user = f"标题：{doc['title']}\n发布日期：{doc['publish_date']}\n\n正文：\n{doc['content_text'][:6000]}"
    try:
        text, elapsed, usage = call_llm(cfg, model, PROMPT, user)
        t = text.strip()
        if t.startswith("```"):
            t = t.strip("`").removeprefix("json").strip()
        try:
            mentions = json.loads(t)["mentions"]
        except json.JSONDecodeError:
            obj, _ = json.JSONDecoder().raw_decode(t[t.find("{"):])
            mentions = obj["mentions"]
        return {"doc_id": doc["id"], "status": "ok", "mentions": mentions,
                "elapsed": round(elapsed, 1)}
    except Exception as e:
        return {"doc_id": doc["id"], "status": "failed", "error": str(e)[:200],
                "mentions": []}


def main():
    cfg = load_api_config(ROOT / "config" / "api.local.yaml")
    model = "gemini-3.8-flash"
    conn = sqlite3.connect(str(ROOT / "data" / "gec.db"))
    conn.row_factory = sqlite3.Row
    docs = [dict(r) for r in conn.execute("""
        SELECT id, title, publish_date, content_text FROM documents
        WHERE (content_text LIKE '%绿证%' OR content_text LIKE '%绿色电力证书%')
          AND (content_text LIKE '%元/%' OR content_text LIKE '%均价%' OR content_text LIKE '%亿个%'
               OR content_text LIKE '%万个%' OR content_text LIKE '%交易量%' OR content_text LIKE '%价格%')
        ORDER BY id""").fetchall()]
    conn.close()
    logger.info("待抽取文档 %d 篇，模型 %s", len(docs), model)

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(extract_doc, cfg, model, d): d for d in docs}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            n = len(rec["mentions"])
            if n or rec["status"] != "ok":
                logger.info("doc=%s 状态=%s mentions=%d", rec["doc_id"], rec["status"], n)

    out = ROOT / "data" / "annotation" / "price_mentions.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(r["mentions"]) for r in results)
    failed = [r["doc_id"] for r in results if r["status"] != "ok"]
    logger.info("完成：%d 篇，共 %d 条 mention，失败 %d %s，耗时 %.0fs -> %s",
                len(results), total, len(failed), failed, time.time() - t0, out)


if __name__ == "__main__":
    main()
