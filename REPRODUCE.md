# Reproduction pointers

Code repository: gec-radar (see README.md).
- Gold set: rebuild from the adjudication workbook via `src/extract/build_gold_v1.py`, `merge_gold_v1_1.py`, `merge_gold_v1_2.py`.
- Table 1 metrics: `data/annotation/eval_v1.json` (raw model outputs included); recompute via `src/extract/eval_models.py` (resumable; existing records are reused).
- Table 2 metrics: `data/forecast_results.json` (per-fold predictions included); recompute via `src/forecast/run_experiments.py` and `run_diagnostics.py`.
- Price panel: rebuild via `src/backfill_prices.py` against the archived pages (see manifest) or the live sources listed in config/sources.yaml.
