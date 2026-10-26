# -*- coding: utf-8 -*-
"""v4.2 frozen forecast input derivation. Preserve v4.1 observed prices, deduplicate curated markers already represented by gold, and aggregate event features on the complete fixed grid. No active database reads in main()."""
import json
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "gec.db"
GOLD = ROOT / "data" / "annotation" / "gold_v1_2.jsonl"
OUT = ROOT / "data" / "forecast_dataset_v4_2.json"

logger = logging.getLogger("forecast_data")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)

STRENGTH_W = {"约束性": 3, "预期性": 2, "倡导性": 1}

# 人工从 documents 表按标题确认日期的关键政策事件（doc_id 可溯），
# 权重 = 对绿证价格的事前重要性主观分级 1-3（显式主观，已在汇报中声明）
CURATED_EVENTS = [
    {"date": "2025-02-09", "doc_id": 52, "weight": 3, "dir": "下行",
     "name": "136号文：新能源上网电价市场化改革"},
    {"date": "2025-03-17", "doc_id": 50, "weight": 3, "dir": "上行",
     "name": "关于促进可再生能源绿色电力证书市场高质量发展的意见"},
    {"date": "2025-07-11", "doc_id": 41, "weight": 2, "dir": "上行",
     "name": "2025年可再生能源电力消纳责任权重通知"},
    {"date": "2025-10-13", "doc_id": 80, "weight": 2, "dir": "上行",
     "name": "消纳责任权重制度实施办法（征求意见稿）"},
    {"date": "2026-06-05", "doc_id": 9, "weight": 3, "dir": "上行",
     "name": "消纳责任权重制度实施办法正式出台（42号令，2026-08-01施行）"},
    {"date": "2026-07-24", "doc_id": None, "weight": 1, "dir": "中性",
     "name": "国家能源局正式发布中国绿证价格指数"},
]

# v4.2 protocol: these two document IDs already contribute adjudicated gold
# events. Preserve all gold events; exclude only the duplicate curated rows.
CURATED_WINDOW_DATES = [e["date"] for e in CURATED_EVENTS]
DEDUPLICATED_CURATED_DOC_IDS = {9, 41}
CURATED_EVENTS = [e for e in CURATED_EVENTS if e["doc_id"] not in DEDUPLICATED_CURATED_DOC_IDS]


def week_start(date_str):
    """任意日期 -> 所在周周一（gzpec 周报周期为周一起）。"""
    import datetime as dt
    d = dt.date.fromisoformat(date_str)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def load_series(conn):
    import datetime as dt
    series = {}
    rows = conn.execute(
        """SELECT period_start d, period_end e, price p FROM prices
           WHERE source='gzpec_weekly' AND cert_type='' AND price IS NOT NULL
           ORDER BY period_start""").fetchall()
    # 按报告期中点所在 ISO 周对齐到周一：gzpec 有两期周报起始日非周一
    # （2026 年第1周 1/4~1/12、第8周 2/24~3/1），直接用 period_start 字符串
    # 匹配周一网格会把这两期真实观测漏掉并误插值。
    pts = {}
    for r in rows:
        d0 = dt.date.fromisoformat(r["d"])
        d1 = dt.date.fromisoformat(r["e"]) if r["e"] else d0
        mid = d0 + (d1 - d0) / 2
        mon = (mid - dt.timedelta(days=mid.weekday())).isoformat()
        if mon in pts:
            logger.warning("周对齐冲突：%s（%s~%s）与已有记录同周，保留先到者",
                           r["d"], r["d"], r["e"])
            continue
        pts[mon] = r["p"]
    series["weekly_overall"] = {"unit": "元/个", "freq": "W",
                                "points": [[k, pts[k]] for k in sorted(pts)]}
    for ct in ("2024年证", "2025年证"):
        rows = conn.execute(
            """SELECT period_start d, price p FROM prices
               WHERE source='nea_bulletin' AND price_type='单独交易月均价'
                 AND cert_type=? AND price IS NOT NULL ORDER BY period_start""",
            (ct,)).fetchall()
        series[f"monthly_{ct}"] = {"unit": "元/个", "freq": "M",
                                   "points": [[r["d"][:7], r["p"]] for r in rows]}
    # 推算整体月均价：分 vintage 按成交量加权（缺量月份退化为简单平均，逐月标注）
    rows = conn.execute(
        """SELECT period_start d, price p, volume v FROM prices
           WHERE source='nea_bulletin' AND price_type='单独交易月均价'
             AND cert_type!='' AND price IS NOT NULL ORDER BY period_start""").fetchall()
    by_m = {}
    for r in rows:
        by_m.setdefault(r["d"][:7], []).append((r["p"], r["v"]))
    derived = []
    for mo, items in sorted(by_m.items()):
        w = [(p, v) for p, v in items if v]
        if w:
            p = sum(p * v for p, v in w) / sum(v for _, v in w)
            method = "vol_weighted"
        else:
            p = sum(p for p, _ in items) / len(items)
            method = "simple_avg(该月vintage无成交量)"
        derived.append([mo, round(p, 4), method])
    series["monthly_overall_derived"] = {
        "unit": "元/个", "freq": "M", "derived": True,
        "points": [[mo, p] for mo, p, _ in derived],
        "methods": {mo: m for mo, _, m in derived}}
    return series


def load_events():
    """gold 事件（取文档发布日）+ curated 关键事件，去重合并。"""
    events = []
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        for e in d["events"]:
            events.append({
                "date": d["publish_date"], "source": f"gold_doc{d['doc_id']}",
                "subtype": e.get("subtype"),
                "strength_w": STRENGTH_W.get(e.get("strength"), 1),
                "dir": e.get("price_direction_prior", "不明"),
            })
    for c in CURATED_EVENTS:
        events.append({"date": c["date"], "source": f"curated_doc{c['doc_id']}",
                       "subtype": None, "strength_w": c["weight"], "dir": c["dir"],
                       "name": c["name"]})
    return events


def build_exog(events, periods, freq):
    """把事件聚合到各期：event_count, strength_sum, up_count, down_count。"""
    agg = {p: [0, 0, 0, 0] for p in periods}
    for e in events:
        if freq == "W":
            key = week_start(e["date"])
        else:
            key = e["date"][:7]
        if key in agg:
            a = agg[key]
            a[0] += 1
            a[1] += e["strength_w"]
            if e["dir"] == "上行":
                a[2] += 1
            elif e["dir"] == "下行":
                a[3] += 1
    return {p: v for p, v in agg.items()}, ["event_count", "strength_sum", "up_count", "down_count"]


def main():
    # Freeze the exact historical observed prices; do not silently pull new
    # rows from the actively collected SQLite database during a correction run.
    historical = json.loads((ROOT / "data/forecast_dataset.json").read_text(encoding="utf-8"))
    series = {name: {k: v for k, v in values.items() if k not in {"exog", "exog_cols"}}
              for name, values in historical["series"].items()}
    events = load_events()

    dataset = {"meta": {"version": "v4.2", "gold_events": 72, "curated_events": 4,
                        "curated_dedup_doc_ids": sorted(DEDUPLICATED_CURATED_DOC_IDS),
                        "weekly_alignment": "report-period midpoint ISO week",
                        "event_date": "document publish_date"},
               "series": {}, "events": events}
    for name, s in series.items():
        periods = [p for p, _ in s["points"]]
        if s["freq"] == "W":
            import datetime as dt
            first, last = dt.date.fromisoformat(periods[0]), dt.date.fromisoformat(periods[-1])
            periods = [(first + dt.timedelta(days=7*i)).isoformat()
                       for i in range((last-first).days//7 + 1)]
        exog, cols = build_exog(events, periods, s["freq"])
        dataset["series"][name] = {**s, "exog": exog, "exog_cols": cols}
        n_ev = sum(1 for v in exog.values() if v[0] > 0)
        logger.info("%s: %d 点（%s ~ %s），含事件期数 %d",
                    name, len(periods), periods[0], periods[-1], n_ev)

    # 事件窗口清单：curated 事件 ±1 期（专项误差用）
    dataset["key_event_dates"] = list(CURATED_WINDOW_DATES)
    OUT.write_text(json.dumps(dataset, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    logger.info("数据集已写: %s", OUT)


if __name__ == "__main__":
    main()
