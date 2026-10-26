# GEC-Policy: Review Snapshot (Anonymous)

This repository accompanies the WWW 2027 submission
"GEC-Policy: A Policy-Event-Aligned Dataset and Benchmark for China's
Green Electricity Certificate Market".

## Contents
- `src/` - crawling, LLM extraction, evaluation and forecasting code
- `data/` - frozen artifacts: gold-standard event annotations (gold_v1_2.jsonl),
  extraction evaluation records (eval_extraction.json, scoring v1.4), forecasting results
  (forecast_results.json / forecast_dataset.json, corrected run v4.2), price panel export
  (price_panel.csv), and a URL + SHA-256 manifest of all policy documents
- `paper/` - LaTeX source of the submission
- `docs/` - event ontology, annotation guideline, model selection log, data dictionary
- `REPRODUCE.md` - offline verification instructions (no API keys needed)

## Anonymity
No raw web pages are redistributed (see NOTE-RAW-PAGES.txt). All policy
documents are public government/market disclosures; the manifest provides
URLs and hashes for independent retrieval.
