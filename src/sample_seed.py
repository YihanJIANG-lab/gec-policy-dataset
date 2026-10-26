# -*- coding: utf-8 -*-
"""从 documents 表分层抽样标注种子集，导出 JSONL 待标注文件。

用法:
    python src/sample_seed.py --target 60                # 抽样 60 篇（默认）
    python src/sample_seed.py --target 200 --seed 42     # 指定规模与随机种子

分层维度（与 01_研究文档/annotation_guideline.md 对齐）:
    信源 source x 年份桶(<=2023/2024/2025/2026) x 主关键词(keywords_matched 首个)

配额: 各层按 sqrt(层大小) 加权分配（比纯比例更照顾小层），每层保底 2 篇，
超出库容时自动截断。确定性抽样（固定 seed 可复现）。
"""
import argparse
import json
import random
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "gec.db"
OUT_DIR = ROOT / "data" / "annotation"
ONTOLOGY_VERSION = "0.1"


def year_bucket(d):
    y = (d or "")[:4]
    if not y.isdigit():
        return "unknown"
    y = int(y)
    if y <= 2023:
        return "<=2023"
    return str(y)


def primary_keyword(kw):
    return (kw or "未命中").split(",")[0].strip() or "未命中"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=60, help="目标样本量（默认 60；库容不足时自动取满）")
    ap.add_argument("--seed", type=int, default=20260913, help="随机种子，保证可复现")
    ap.add_argument("--out", default=None, help="输出文件路径（默认 data/annotation/seed_v1.jsonl）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, source, title, publish_date, keywords_matched, content_text FROM documents"
    ).fetchall()
    conn.close()

    strata = defaultdict(list)
    for rid, source, title, pub, kw, content in rows:
        key = (source, year_bucket(pub), primary_keyword(kw))
        strata[key].append({
            "doc_id": rid,
            "source": source,
            "title": title,
            "publish_date": pub,
            "keywords_matched": kw,
            "content_text": content,
        })

    rng = random.Random(args.seed)
    # sqrt 加权配额：小层也能分到样本，大层不被等比挤占过多
    weights = {k: len(v) ** 0.5 for k, v in strata.items()}
    total_w = sum(weights.values())
    target = min(args.target, len(rows))
    quota = {k: max(2, round(target * w / total_w)) for k, w in weights.items()}
    # 超出层容量的截断；总量超出 target 时按配额从大到小回收
    for k in quota:
        quota[k] = min(quota[k], len(strata[k]))
    overflow = sum(quota.values()) - target
    for k in sorted(quota, key=lambda k: -quota[k]):
        if overflow <= 0:
            break
        cut = min(overflow, max(0, quota[k] - 2))
        quota[k] -= cut
        overflow -= cut

    selected = []
    for key in sorted(quota):
        pool = strata[key]
        rng.shuffle(pool)
        selected.extend(pool[: quota[key]])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT_DIR / "seed_v1.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for item in selected:
            f.write(json.dumps({
                "doc_id": item["doc_id"],
                "source": item["source"],
                "title": item["title"],
                "publish_date": item["publish_date"],
                "keywords_matched": item["keywords_matched"],
                "content_text": item["content_text"],
                "annotator": "",
                "ontology_version": ONTOLOGY_VERSION,
                "events": [],
            }, ensure_ascii=False) + "\n")

    print(f"语料总量: {len(rows)}  目标: {args.target}  实际抽出: {len(selected)}")
    print(f"输出: {out}")
    print("\n分层配额明细:")
    hdr = f"{'信源':<8}{'年份':<8}{'主关键词':<14}{'层大小':>6} {'配额':>4}"
    print(hdr)
    for key in sorted(strata):
        print(f"{key[0]:<8}{key[1]:<8}{key[2]:<14}{len(strata[key]):>6} {quota.get(key, 0):>4}")


if __name__ == "__main__":
    main()
