# -*- coding: utf-8 -*-
"""doc 90 裁决合并 → gold_v1_2.jsonl（冻结版）。

- doc 90 的原有 C1（1 条）替换为裁决的 2 条 C1（2023 约束性 + 2024 预期性），
  其余事件不动；每条过 validate；
- 清空 pending_review.json（存疑全部结清，文件内注明）。
"""
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import validate  # noqa: E402

logger = logging.getLogger("merge_gold_v1_2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)

GOLD_IN = ROOT / "data" / "annotation" / "gold_v1_1.jsonl"
RESOLVED = ROOT / "data" / "annotation" / "pending_doc90_resolved.json"
PENDING = ROOT / "data" / "annotation" / "pending_review.json"
OUT = ROOT / "data" / "annotation" / "gold_v1_2.jsonl"


def main():
    docs = [json.loads(l) for l in GOLD_IN.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {d["doc_id"]: d for d in docs}
    resolved = json.loads(RESOLVED.read_text(encoding="utf-8"))["resolved"]

    problems = []
    for item in resolved:
        did = item["doc_id"]
        doc = by_id[did]
        removed = [e for e in doc["events"] if e.get("subtype") == "C1"]
        if len(removed) != 1:
            problems.append(f"doc {did}: 预期替换 1 条 C1，实际找到 {len(removed)} 条，请人工核查")
        doc["events"] = [e for e in doc["events"] if e.get("subtype") != "C1"]
        for ev in item["events"]:
            _, errs = validate(json.dumps({"events": [ev]}, ensure_ascii=False))
            if errs:
                problems.append(f"doc {did} {ev['subtype']}({ev['effective_date']}): {'; '.join(errs)}")
            doc["events"].append(dict(ev, merge_note=f"doc90 一致行裁决拆分（{item['decision'][:30]}…）"))
        logger.info("doc %d: 移除 C1 %d 条，并入 %d 条", did, len(removed), len(item["events"]))

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

    n_with = sum(1 for d in docs if d["events"])
    print(f"gold_v1_2 总事件数: {n_events}（预期 72 = 71 - 1 + 2）")
    print(f"大类分布: {dict(sorted(type_dist.items()))}")
    print(f"有事件文档 {n_with}/60")
    print("校验问题清单:", problems or "（空）")

    PENDING.write_text(json.dumps({
        "status": "cleared",
        "note": "全部存疑已结清：5 条于 gold_v1_1 并入（pending_review_resolved.json），"
                "doc 90 一致表修改行于 gold_v1_2 裁决拆分（pending_doc90_resolved.json）。"
                "原始存疑内容见上述两个 resolved 文件。",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("pending_review.json 已清零（含结清说明）")
    logger.info("写出: %s", OUT)


if __name__ == "__main__":
    main()
