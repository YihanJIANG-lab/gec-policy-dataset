# -*- coding: utf-8 -*-
"""打包 Zenodo 数据发布包到 04_发布包/release/（不发布，只备料）。

内容：gold set、价格面板 CSV、事件本体、数据字典、复现指针、
URL+SHA256 清单（原始政府/平台页面不整体再分发）、三个 LICENSE 文件。
"""
import csv
import hashlib
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REL = ROOT / "04_发布包" / "release"
PKG = REL / "gec-policy-dataset-v1.0"

MIT = """MIT License

Copyright (c) 2026 GEC-Policy authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

CCBY = """Creative Commons Attribution 4.0 International (CC-BY-4.0)

Applies to: gold_v1_2.jsonl, price_panel.csv, event_ontology.json, DATA_DICT.md,
and all derived tables in this package.

You are free to share and adapt the material for any purpose, even commercially,
provided you give appropriate credit, provide a link to the license, and indicate
if changes were made. Full text: https://creativecommons.org/licenses/by/4.0/legalcode
"""

RAW_NOTE = """Raw government/platform pages are NOT redistributed in this package.
They are public documents; we provide manifest_url_sha256.csv mapping each
document URL to the SHA-256 of the exact page snapshot we archived, plus the
crawling scripts in the code repository to re-fetch them from the original
government/operator portals.
"""

DATA_DICT = """# GEC-Policy Dataset v1.0 — Data Dictionary

## gold_v1_2.jsonl (72 events, 60 documents, ontology v0.2)
Per line (JSON):
- doc_id (int): document id in the corpus (documents table).
- source (str): source code (ndrc / nea / gzpec).
- title, publish_date (str): document metadata.
- ontology_version (str): "0.2".
- annotator (str): "human_adjudicated".
- events (list): each with event_type (A-F), subtype (e.g. C1), actor, object,
  strength (binding/expected/advocatory; Chinese labels), effective_date
  (precision never exceeds the source text), region, industry (list),
  price_direction_prior (up/down/neutral/unclear), evidence_span (verbatim
  source quote; 2 events intentionally blank where adjudicator's candidate
  citations conflicted - disclosed, not repaired), merge_note (optional).

## price_panel.csv
- source: nea_bulletin / gzpec_weekly / nea_index / nea_index_derived /
  nea_report_2024 / news_citation / greenenergy / greenenergy_snapshot
- platform: trading platform or coverage
- cert_type: vintage+type label (e.g. "2024 vintage", "PV|2025-11"); empty if n/a
- price_type: e.g. monthly/weekly average deal price, listing price variants,
  China GEC Price Index (points)
- price: CNY per certificate (index rows: points, base 2025-01=100)
- volume: certificates (source "wan-ge" values converted x10^4)
- period_granularity: day / week / month / year
- period_start, period_end: ISO dates
- url: provenance link (mandatory)
- quality: A official bulletin / B media-cited official data / C inferred
- crawl_time: last crawl timestamp

## manifest_url_sha256.csv
- url, source, title, publish_date of each corpus document
- sha256_of_archived_page: SHA-256 of the exact archived page bytes
  (raw pages themselves are not redistributed; re-fetch via the crawlers).

## event_ontology.json
The 6-category / 20-subtype event ontology with field definitions.
"""


def main():
    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True)

    shutil.copy(ROOT / "data" / "annotation" / "gold_v1_2.jsonl", PKG / "gold_v1_2.jsonl")
    shutil.copy(ROOT / "config" / "event_schema.json", PKG / "event_ontology.json")
    shutil.copy(ROOT / "data" / "price_coverage.json", PKG / "price_coverage.json")

    conn = sqlite3.connect(str(ROOT / "data" / "gec.db"))
    conn.row_factory = sqlite3.Row
    with open(PKG / "price_panel.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["source", "platform", "cert_type", "price_type", "price", "volume",
                    "period_granularity", "period_start", "period_end", "url",
                    "quality", "crawl_time"])
        for r in conn.execute(
                "SELECT source, platform, cert_type, price_type, price, volume,"
                " period_granularity, period_start, period_end, url, quality, crawl_time"
                " FROM prices ORDER BY period_start"):
            w.writerow([r[k] for k in r.keys()])
    docs = list(conn.execute(
        "SELECT url, raw_path, source, title, publish_date FROM documents ORDER BY id"))
    conn.close()

    with open(PKG / "manifest_url_sha256.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["url", "source", "title", "publish_date", "sha256_of_archived_page"])
        for d in docs:
            raw = ROOT / d["raw_path"] if d["raw_path"] else None
            sha = ""
            if raw and raw.exists():
                sha = hashlib.sha256(raw.read_bytes()).hexdigest()
            w.writerow([d["url"], d["source"], d["title"], d["publish_date"], sha])

    (PKG / "LICENSE-CODE.txt").write_text(MIT, encoding="utf-8")
    (PKG / "LICENSE-DATA.txt").write_text(CCBY, encoding="utf-8")
    (PKG / "NOTE-RAW-PAGES.txt").write_text(RAW_NOTE, encoding="utf-8")
    (PKG / "DATA_DICT.md").write_text(DATA_DICT, encoding="utf-8")
    (PKG / "REPRODUCE.md").write_text(
        "# Reproduction pointers\n\n"
        "Code repository: gec-radar (see README.md).\n"
        "- Gold set: rebuild from the adjudication workbook via "
        "`src/extract/build_gold_v1.py`, `merge_gold_v1_1.py`, `merge_gold_v1_2.py`.\n"
        "- Table 1 metrics: `data/annotation/eval_v1.json` (raw model outputs included); "
        "recompute via `src/extract/eval_models.py` (resumable; existing records are reused).\n"
        "- Table 2 metrics: `data/forecast_results.json` (per-fold predictions included); "
        "recompute via `src/forecast/run_experiments.py` and `run_diagnostics.py`.\n"
        "- Price panel: rebuild via `src/backfill_prices.py` against the archived pages "
        "(see manifest) or the live sources listed in config/sources.yaml.\n",
        encoding="utf-8")

    zip_path = REL / "gec-policy-dataset-v1.0.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(PKG.rglob("*")):
            z.write(p, p.relative_to(REL))
    print("打包完成:", zip_path, f"{zip_path.stat().st_size/1024:.0f} KB")
    for p in sorted(PKG.rglob("*")):
        print(f"  {p.name:<28} {p.stat().st_size/1024:>8.1f} KB")


if __name__ == "__main__":
    main()
