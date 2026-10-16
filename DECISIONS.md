# Decisions

One entry per non-trivial design choice: what, alternatives, why. Newest last.

## 2026-10-16: Dependency management with uv
- **What:** `pyproject.toml` + committed `uv.lock`, Python pinned to 3.12 (`.python-version`). Model libraries are
  added in the milestone that first uses them, not up front.
- **Alternatives:** pip + requirements.txt; Poetry.
- **Why:** one tool for venv, locking, and running (`uv run …`); fast and reproducible. Already installed.

## 2026-10-16: Package layout
- **What:** code in `edina_rag/`, tests in `tests/`, SQL in `migrations/`, eval data in `eval/`.
- **Alternatives:** keep flat modules at the repo root.
- **Why:** cheap to do now, avoids import-path hacks later as modules multiply.

## 2026-10-16: Numbered SQL migrations with a tiny runner
- **What:** `migrations/NNNN_*.sql` applied by `python -m edina_rag.migrate`, each in one transaction, recorded with a
  sha256 in `schema_migrations`. An applied file that changes is an error; fix forward with a new file. `--reset`
  drops the public schema for dev.
- **Alternatives:** `docker-entrypoint-initdb.d` (runs only on an empty volume); Alembic (needs SQLAlchemy models we
  don't have); `CREATE … IF NOT EXISTS` everywhere (silently skips changed definitions).
- **Why:** idempotent re-runs (CLAUDE.md rule), visible history, ~100 lines with no new dependencies.

## 2026-10-16: Postgres image and storage
- **What:** `pgvector/pgvector:0.8.0-pg17`, named Docker volume, host port 5433.
- **Alternatives:** unpinned `latest`; a `./pgdata` bind mount.
- **Why:** pinning keeps HNSW behavior reproducible. Bind mounts under `/home` are slow on WSL2 and hit permission
  problems. 5433 avoids clashing with a local Postgres.
- **Verified** on the pinned image (Postgres 17, pgvector 0.8.0, Docker Engine 29 inside WSL) and earlier on
  Postgres 16.2 + pgvector 0.6.2.

## 2026-10-16: Pipeline progress lives only in ingestion_jobs
- **What:** removed `documents.status`; added a `document_status` view derived from `ingestion_jobs`. Phase 1 still
  writes job rows even though it runs in a single process.
- **Alternatives:** keep both columns in sync.
- **Why:** two sources of truth drift. Jobs already carry attempts, errors, and page ranges, and Phase 2 workers will
  need them anyway.

## 2026-10-16: No paid services; local models
- **What:** embeddings from `BAAI/bge-small-en-v1.5` via `fastembed` (384 dimensions, CPU, ONNX). Answers from a small
  (~3–4B, 4-bit) instruct model via Ollama. Both sit behind small interfaces (`Embedder`, `Generator`).
- **Alternatives:** hosted embeddings (OpenAI `text-embedding-3-small`, Voyage) and a hosted LLM for answers. Larger
  local embedders (`bge-base-en-v1.5`, `nomic-embed-text-v1.5`, 768 dimensions).
- **Why:** project constraint of zero extra spend. bge-small is a strong small retriever that runs on this machine
  (12 cores, about 7 GB RAM, no GPU) without PyTorch.
- **Trade-off:** answers will be slower and weaker than from a frontier model, so the M8 citation checker and
  abstention tests matter more. Comparing embedders is a Phase 2 eval experiment.
- **Consequence:** `chunks.embedding` is `vector(384)`. Chunks must stay under the model's 512-token input limit,
  measured with its own tokenizer.
