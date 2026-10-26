# -*- coding: utf-8 -*-
"""合并 5 条存疑裁决 → gold_v1_1.jsonl（ontology_version 0.2）。

- data/annotation/pending_review_resolved.json 的 5 条 event 追加进对应文档
  （已核实与现有 gold 事件无 subtype+evidence 重复，仍做防重检查）；
- 每条过 validate 校验；
- doc 90 一致表"修改"行（自由文本备注）完整摘出打印，留 pending 不机器解析。
"""
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import validate  # noqa: E402

logger = logging.getLogger("merge_gold_v1_1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)

GOLD_V1 = ROOT / "data" / "annotation" / "gold_v1.jsonl"
RESOLVED = ROOT / "data" / "annotation" / "pending_review_resolved.json"
PENDING = ROOT / "data" / "annotation" / "pending_review.json"
OUT = ROOT / "data" / "annotation" / "gold_v1_1.jsonl"


def main():
    docs = [json.loads(l) for l in GOLD_V1.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {d["doc_id"]: d for d in docs}
    resolved = json.loads(RESOLVED.read_text(encoding="utf-8"))["resolved"]

    problems, added = [], []
    for item in resolved:
        did, ev = item["doc_id"], item["event"]
        doc = by_id[did]
        dup = any(e.get("subtype") == ev["subtype"]
                  and (e.get("evidence_span") or "") == (ev.get("evidence_span") or "")
                  for e in doc["events"])
        if dup:
            problems.append(f"doc {did} {ev['subtype']}: 与现有 gold 事件重复，跳过")
            continue
        _, errs = validate(json.dumps({"events": [ev]}, ensure_ascii=False))
        if errs:
            problems.append(f"doc {did} {ev['subtype']}: {'; '.join(errs)}")
        ev = dict(ev, merge_note=f"pending 存疑裁决并入（{item['decision'][:40]}…）")
        doc["events"].append(ev)
        added.append(f"doc {did} {ev['subtype']}")

    n_events = 0
    type_dist = {}
    with OUT.open("w", encoding="utf-8") as f:
        for doc in docs:
            doc["ontology_version"] = "0.2"
            doc["annotator"] = "human_adjudicated"
            n_events += len(doc["events"])
            for e in doc["events"]:
                t = e.get("event_type")
                if t:
                    type_dist[t] = type_dist.get(t, 0) + 1
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    print(f"并入 {len(added)} 条: {added}")
    print(f"gold_v1_1 总事件数: {n_events}（v1 为 66，预期 71）")
    print(f"大类分布: {dict(sorted(type_dist.items()))}（D 类应由 0 变为 3）")
    n_with = sum(1 for d in docs if d["events"])
    print(f"有事件文档 {n_with}/60")
    if problems:
        print("问题清单:")
        for p in problems:
            print(" !", p)
    else:
        print("校验问题清单: （空）")

    # doc 90 一致表"修改"行：完整摘出供转交人工裁决
    pending = json.loads(PENDING.read_text(encoding="utf-8"))
    print("\n===== 待人工裁决（doc 90，一致表修改行，机器不解析）=====")
    print(json.dumps([p for p in pending if p.get("doc_id") == 90],
                     ensure_ascii=False, indent=2))
    logger.info("写出: %s", OUT)


if __name__ == "__main__":
    main()
