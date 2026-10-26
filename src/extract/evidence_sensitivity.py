# -*- coding: utf-8 -*-
"""空 evidence_span 敏感性：排除 gold_v1_2 中 2 条空证据事件后重算四模型 Event-P/R/F1。
离线重算 eval_v1.json 中的既有预测，零 API。结果写回 eval_v1.json（meta v1.3）。"""
import json
import sys
from pathlib import Path
from difflib import SequenceMatcher

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "data" / "annotation" / "eval_v1.json"
GOLD = ROOT / "data" / "annotation" / "gold_v1_2.jsonl"


def pair_count(pred, gold_ev, thr=0.5):
    used = set()
    by_sub = {}
    for j, g in enumerate(gold_ev):
        by_sub.setdefault(g.get("subtype"), []).append(j)
    tp = 0
    for p in pred:
        best, bs = None, 0.0
        for j in by_sub.get(p.get("subtype"), []):
            if j in used:
                continue
            s = SequenceMatcher(None, p.get("evidence_span", ""),
                                gold_ev[j].get("evidence_span", "")).ratio()
            if s > bs:
                best, bs = j, s
        if best is not None and bs >= thr:
            tp += 1
            used.add(best)
    return tp, len(pred) - tp, len(gold_ev) - tp


def prf(records, gold, exclude_empty):
    out = {}
    for m in ["deepseek-flash", "deepseek-v4-pro", "gpt-5.6-sol", "gemini-3.8-flash"]:
        tp = fp = fn = 0
        for r in records:
            if r["model"] != m:
                continue
            g = [e for e in gold[r["doc_id"]]
                 if not (exclude_empty and not e.get("evidence_span"))]
            p = r.get("events") or []
            a, b, c = pair_count(p, g)
            tp += a
            fp += b
            fn += c
        P = tp / (tp + fp) if tp + fp else 0.0
        R = tp / (tp + fn) if tp + fn else 0.0
        F1 = 2 * P * R / (P + R) if P + R else 0.0
        out[m] = {"P": round(P, 3), "R": round(R, 3), "F1": round(F1, 3),
                  "TP": tp, "FP": fp, "FN": fn}
    return out


def main():
    d = json.loads(EVAL.read_text(encoding="utf-8"))
    gold = {}
    for l in GOLD.read_text(encoding="utf-8").splitlines():
        x = json.loads(l)
        gold[x["doc_id"]] = x["events"]
    empty = [(did, e.get("subtype")) for did, evs in gold.items()
             for e in evs if not e.get("evidence_span")]
    print("空 evidence gold 事件:", empty)

    full = prf(d["records"], gold, False)
    excl = prf(d["records"], gold, True)
    for m in full:
        print(f"{m}: 全量 F1={full[m]['F1']} (TP={full[m]['TP']}) -> "
              f"排除后 F1={excl[m]['F1']} P={excl[m]['P']} R={excl[m]['R']}")

    d["meta"]["version"] = "v1.3"
    d["meta"]["evidence_sensitivity"] = {
        "note": "排除 gold 中 2 条空 evidence_span 事件（doc 14 F2、doc 1 A2）后重算；"
                "匹配口径不变（subtype 精确 + SequenceMatcher>=0.5 贪心）",
        "full_72": full,
        "excl_empty_70": excl,
    }
    EVAL.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print("eval_v1.json 已更新为 v1.3")


if __name__ == "__main__":
    main()
