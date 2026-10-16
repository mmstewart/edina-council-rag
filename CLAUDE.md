# Edina Council RAG

Portfolio project: a retrieval-augmented QA system over the City of Edina, MN city council meeting records (agendas,
packets, minutes), built to demonstrate AI-engineering skills: ingestion pipelines, retrieval quality, evaluation, and
scaling/observability.

## Data source

- Laserfiche WebLink archive at https://edinadocs.edinamn.gov/WebLink (City Council folder id 6369): Meeting
  Packets_Archive and Minutes_Archive, year folders 2000-2025 plus range folders (1974-1979, 1981-1985 Agendas,
  1982-1999). Files are named like "2025-10-07 City Council Meeting".
- 2026+ meetings come from the city's newer Agenda & Minutes portal (later phase).
- Packets are very large (300-1,150 pages), bundle many agenda items, and older files are likely scanned (OCR needed).

## Stack

Python 3.12, Postgres + pgvector (docker compose), PyMuPDF for extraction, OCRmyPDF for scans (later), pytest. Embedding
model is configurable.

## Rules

- Be polite to the city's server: descriptive User-Agent, >= 2s between requests, cache every raw response/file, crawls
  must be resumable.
- Never commit raw PDFs, HAR files, cookies, or secrets (keep in .gitignore).
- Every pipeline stage must be idempotent (see ingestion_jobs in schema.sql).
- Write tests for parsing/chunking logic. Run them before declaring done.
- Record each non-trivial design choice in DECISIONS.md (what, alternatives, why).
- Measure, don't guess: retrieval changes need an eval-set number before/after.

## Phases

1. Baseline: one recent year of packets, simple chunking, basic QA with citations (document, date, page), ~25
   hand-written eval questions.
2. 2020-2025 packets + minutes, queue-based ingestion, hybrid search, reranking, eval harness, load tests.
3. 2006-2019, new-portal incremental feed.
4. Pre-2006 scans with OCR + OCR-quality experiment.
