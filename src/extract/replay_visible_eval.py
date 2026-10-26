"""Replay the saved extraction outputs against input-observable gold events.

This is a retrospective scoring correction, not a new inference experiment.
The full 72-event gold set and all 240 original model outputs remain unchanged.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_review_excel as alignment
from eval_models import COST_ASSUMPTIONS, compute_metrics

INPUT_LIMIT = 6000


def observable_gold(seed, gold, limit=INPUT_LIMIT):
    """Apply a model-independent rule based only on the actual visible input."""
    by_doc = {doc["doc_id"]: doc for doc in seed}
    accepted, excluded = {}, []
    for doc in gold:
        did = doc["doc_id"]
        full_text = by_doc[did]["content_text"]
        visible_text = full_text[:limit]
        accepted[did] = []
        for index, event in enumerate(doc["events"]):
            evidence = event.get("evidence_span") or ""
            if not evidence:
                reason = "empty_evidence"
            elif evidence not in full_text:
                reason = "not_a_contiguous_source_span"
            elif evidence not in visible_text:
                reason = "outside_actual_input_prefix"
            else:
                accepted[did].append(event)
                continue
            excluded.append({"doc_id": did, "event_index": index,
                             "subtype": event.get("subtype"), "reason": reason,
                             "evidence_start": full_text.find(evidence) if evidence else None,
                             "evidence_length": len(evidence),
                             "evidence_span": evidence})
    return accepted, excluded


def recompute(root=ROOT):
    root = Path(root)
    ann = root / "data" / "annotation"
    original = json.loads((ann / "eval_v1.json").read_text(encoding="utf-8"))
    seed = [json.loads(line) for line in (ann / "seed_v1.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    gold = [json.loads(line) for line in (ann / "gold_v1_2.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    full = {doc["doc_id"]: doc["events"] for doc in gold}
    eligible, excluded = observable_gold(seed, gold)
    records = original["records"]
    excluded_by_doc = {did: [] for did in full}
    for item in excluded:
        excluded_by_doc[item["doc_id"]].append(full[item["doc_id"]][item["event_index"]])
    saved_threshold = alignment.SIM_THRESHOLD
    thresholds = {}
    ignored_details = {}
    try:
        for threshold in (0.4, 0.5, 0.6):
            alignment.SIM_THRESHOLD = threshold
            scoring_records = []
            ignored = []
            for record in records:
                predictions = record.get("events") or []
                _, unmatched, _ = alignment.pair_events(predictions, eligible[record["doc_id"]])
                candidates = [event for event in unmatched if event.get("evidence_span")]
                unscored = [event for event in excluded_by_doc[record["doc_id"]] if event.get("evidence_span")]
                pairs, _, _ = alignment.pair_events(candidates, unscored)
                ignored_ids = {id(prediction) for prediction, _, _ in pairs}
                scoring_records.append({**record, "events": [event for event in predictions if id(event) not in ignored_ids]})
                for prediction, reference, similarity in pairs:
                    ignored.append({"model": record["model"], "doc_id": record["doc_id"],
                                    "prediction_index": next(i for i, event in enumerate(predictions) if event is prediction),
                                    "subtype": prediction.get("subtype"), "similarity": similarity,
                                    "reason": "Unmatched prediction matches a nonempty, unscorable gold reference in second-stage matching."})
            values = compute_metrics(scoring_records, eligible)
            for model, value in values.items():
                value["ignored"] = sum(item["model"] == model for item in ignored)
                value["original_predictions"] = sum(len(record.get("events") or []) for record in records if record["model"] == model)
                value["scored_predictions"] = value["TP"] + value["FP"]
                assert value["TP"] + value["FP"] + value["ignored"] == value["original_predictions"]
                assert value["TP"] + value["FN"] == sum(map(len, eligible.values()))
            thresholds[str(threshold)] = values
            ignored_details[str(threshold)] = ignored
    finally:
        alignment.SIM_THRESHOLD = saved_threshold
    payload = {
        "meta": {
            **copy.deepcopy(original["meta"]),
            "version": "v1.4",
            "source_version": original["meta"]["version"],
            "source_records": "data/annotation/eval_v1.json",
            "inference_rerun": False,
            "gold": "gold_v1_2.jsonl",
            "evaluation_scope": {
                "name": "input_observable_gold",
                "status": "retrospective_scoring_correction",
                "input_character_limit": INPUT_LIMIT,
                "rule": "Nonempty evidence must be an exact contiguous substring of the original content_text[:6000]; all predictions and all 60 documents are retained.",
                "full_gold_events": sum(map(len, full.values())),
                "eligible_gold_events": sum(map(len, eligible.values())),
                "excluded_gold_events": excluded,
                "prediction_policy": "First match predictions to the 68 scorable references using the original order, subtype rule, evidence threshold and tie handling. Then match only still-unmatched predictions with nonempty evidence to excluded references with nonempty evidence, consuming each excluded reference at most once. Second-stage matches are ignored, not TP/FP/FN. Empty evidence never matches in stage two. All raw predictions remain archived.",
                "limitations": "This reuses previously observed model outputs. It neither supplies missing full-context answers nor establishes a new confirmatory experiment. Original prompts predate ontology adjudication changes. Model IDs and relay routing are recorded identities, not independently authenticated providers.",
            },
            "cost_assumptions": COST_ASSUMPTIONS,
        },
        "metrics": thresholds["0.5"],
        "full_gold_metrics": compute_metrics(records, full),
        "nonempty_gold_metrics": compute_metrics(records, {did: [event for event in events if event.get('evidence_span')] for did, events in full.items()}),
        "threshold_sensitivity": thresholds,
        "ignored_predictions": ignored_details,
        "records": records,
    }
    return payload


def render_tex(payload):
    scope = payload["meta"]["evaluation_scope"]
    lines = [
        "% Auto-generated by src/extract/replay_visible_eval.py; v1.4 retrospective replay.",
        "% All 240 original model outputs and the complete 72-event dataset are preserved.",
        r"\begin{table*}[t]", r"  \centering", r"  \footnotesize",
        "  \\caption{Policy event extraction from saved model outputs (60 documents; "
        + str(scope["eligible_gold_events"]) + " of 72 gold events have nonempty, verbatim evidence within the actual 6,000-character input). Fld-Agree is mean normalized agreement over four fields on matched pairs; Evid-Sim is mean matched evidence similarity.}",
        r"  \label{tab:extraction}",
        r"  \begin{tabular}{lcccccccc}", r"    \toprule",
        "    Model & P & R & F1 & Fld-Agree & Evid-Sim & Ignored & Avg tok & CNY/doc " + r"\\",
        r"    \midrule",
    ]
    for model in ("deepseek-flash", "deepseek-v4-pro", "gpt-5.6-sol", "gemini-3.8-flash"):
        row = payload["metrics"][model]
        # Field names are inherited from the historical scorer, not renamed metrics.
        lines.append(f"    {model} & {row['P']:.3f} & {row['R']:.3f} & {row['F1']:.3f} & "
                     f"{row['AttrF1']:.3f} & {row['EvidenceF1']:.3f} & {row['ignored']} & {row['avg_completion_tokens']:.0f} & {row['cost_cny'] / payload['meta']['n_docs']:.4f} " + r"\\")
    lines += [r"    \midrule",
              r"    \multicolumn{9}{l}{\footnotesize Ignored: unmatched predictions matching a nonempty excluded reference; these count neither as TP nor FP. No new API calls.}\\",
              r"    \multicolumn{9}{l}{\footnotesize Full-gold legacy scores and exclusions: eval\_v1\_4.json. Costs use historical assumptions; relay model identities are not independently authenticated.}\\",
              r"    \bottomrule", r"  \end{tabular}", r"\end{table*}", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tex-output", type=Path)
    args = parser.parse_args()
    payload = recompute(args.root)
    output = args.output or args.root / "data/annotation/eval_v1_4.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.tex_output:
        args.tex_output.parent.mkdir(parents=True, exist_ok=True)
        args.tex_output.write_text(render_tex(payload), encoding="utf-8")
    print(json.dumps({"version": payload["meta"]["version"], "scope": payload["meta"]["evaluation_scope"],
                      "F1": {model: values["F1"] for model, values in payload["metrics"].items()}}, ensure_ascii=True))


if __name__ == "__main__":
    main()
