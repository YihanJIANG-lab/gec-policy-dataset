# GEC-Policy Dataset v1.0 — Data Dictionary

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
