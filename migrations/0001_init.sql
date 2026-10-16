-- Edina council meetings RAG schema
-- Requires Postgres 14+ with pgvector >= 0.5 (HNSW).
-- vector(384) matches BAAI/bge-small-en-v1.5 (see DECISIONS.md). Changing the
-- embedding dimension means a new migration that rebuilds the column and index.

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per meeting. Minutes and packets for the same meeting join here.
CREATE TABLE meetings (
    id            BIGSERIAL PRIMARY KEY,
    body          TEXT NOT NULL DEFAULT 'city_council',
    meeting_date  DATE NOT NULL,
    meeting_type  TEXT NOT NULL
        CHECK (meeting_type IN ('regular', 'work_session', 'special',
                                'special_work_session', 'other')),
    UNIQUE (body, meeting_date, meeting_type)
);

-- One row per source file (a packet PDF, a minutes PDF, ...).
CREATE TABLE documents (
    id                    BIGSERIAL PRIMARY KEY,
    meeting_id            BIGINT REFERENCES meetings(id),  -- NULL until parsed/linked
    doc_kind              TEXT NOT NULL
        CHECK (doc_kind IN ('packet', 'minutes', 'agenda', 'other')),
    source_system         TEXT NOT NULL
        CHECK (source_system IN ('laserfiche_archive', 'agenda_portal')),
    source_id             TEXT NOT NULL,        -- Laserfiche entry id, or portal id
    folder_path           TEXT,                 -- e.g. 'Meeting Packets_Archive/2025'
    filename              TEXT NOT NULL,
    extension             TEXT,                 -- from the listing, e.g. 'pdf'
    file_size_bytes       BIGINT,
    source_modified_at    TIMESTAMPTZ,          -- listing LastModified; drives incremental re-crawls
    page_count_reported   INT,                  -- from the Laserfiche listing
    page_count_extracted  INT,                  -- from our own extraction; should match
    sha256                TEXT,
    raw_path              TEXT,                 -- where the untouched file is stored
    text_quality          TEXT NOT NULL DEFAULT 'unknown'
        CHECK (text_quality IN ('born_digital', 'ocr', 'mixed', 'unknown')),
    needs_review          BOOLEAN NOT NULL DEFAULT FALSE,
    review_reason         TEXT,
    -- Pipeline progress lives in ingestion_jobs only; see the document_status view.
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id)
);
CREATE INDEX documents_meeting_idx ON documents (meeting_id);

-- Per-page extracted text. Keeping this lets you re-chunk without re-extracting.
CREATE TABLE pages (
    document_id        BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number        INT NOT NULL,            -- 1-based
    content            TEXT NOT NULL,
    extraction_method  TEXT NOT NULL,           -- e.g. 'pymupdf', 'ocrmypdf'
    has_text_layer     BOOLEAN NOT NULL,        -- false = scanned/image-only page, OCR candidate
    char_count         INT GENERATED ALWAYS AS (length(content)) STORED,
    PRIMARY KEY (document_id, page_number)
);

-- Retrievable units. Filter columns are denormalized on purpose so that
-- "only 2024, only minutes" filters don't need joins in the hot path.
CREATE TABLE chunks (
    id               BIGSERIAL PRIMARY KEY,
    document_id      BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    meeting_id       BIGINT REFERENCES meetings(id),
    meeting_date     DATE,
    doc_kind         TEXT NOT NULL,
    chunk_index      INT NOT NULL,
    page_start       INT NOT NULL,
    page_end         INT NOT NULL,
    agenda_item      TEXT,                      -- if you chunk by agenda item
    content          TEXT NOT NULL,
    token_count      INT,
    chunker_version  TEXT NOT NULL,             -- bump when chunking strategy changes
    embedding_model  TEXT,
    embedding        vector(384),
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    UNIQUE (document_id, chunker_version, chunk_index)
);
CREATE INDEX chunks_embedding_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX chunks_filter_idx ON chunks (meeting_date, doc_kind);

-- Postgres-backed job queue. The unit of work is (document, stage, page range)
-- so a crash in a 1,151-page packet resumes instead of restarting.
-- page_start = page_end = 0 means "whole document".
CREATE TABLE ingestion_jobs (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    stage         TEXT NOT NULL
        CHECK (stage IN ('download', 'extract', 'ocr', 'chunk', 'embed')),
    page_start    INT NOT NULL DEFAULT 0,
    page_end      INT NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'dead')),
    attempts      INT NOT NULL DEFAULT 0,
    max_attempts  INT NOT NULL DEFAULT 5,
    last_error    TEXT,
    available_at  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- for retry backoff
    locked_at     TIMESTAMPTZ,
    locked_by     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- idempotency: enqueueing the same work twice is a no-op
    UNIQUE (document_id, stage, page_start, page_end)
);
-- Partial index matching the claim query below; stays small as jobs finish.
CREATE INDEX ingestion_jobs_claim_idx ON ingestion_jobs (available_at, id)
    WHERE status = 'queued';
CREATE INDEX ingestion_jobs_document_idx ON ingestion_jobs (document_id);

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER ingestion_jobs_updated_at
    BEFORE UPDATE ON ingestion_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Per-document progress, derived from jobs so there is one source of truth.
-- last_done_stage is the furthest pipeline stage with a 'done' job; ranges
-- are not checked for completeness here (a stage split into page ranges
-- counts as done once any range is done, so check jobs directly for that).
CREATE VIEW document_status AS
SELECT d.id AS document_id,
       (SELECT j.stage FROM ingestion_jobs j
        WHERE j.document_id = d.id AND j.status = 'done'
        ORDER BY array_position(ARRAY['download', 'extract', 'ocr', 'chunk', 'embed'], j.stage) DESC
        LIMIT 1) AS last_done_stage,
       EXISTS (SELECT 1 FROM ingestion_jobs j
               WHERE j.document_id = d.id AND j.status IN ('failed', 'dead')) AS has_failures,
       EXISTS (SELECT 1 FROM ingestion_jobs j
               WHERE j.document_id = d.id AND j.status IN ('queued', 'running')) AS has_pending
FROM documents d;

-- Worker claim query (run inside a transaction):
--
--   UPDATE ingestion_jobs SET status = 'running', locked_at = now(),
--          locked_by = $1, attempts = attempts + 1
--   WHERE id = (
--       SELECT id FROM ingestion_jobs
--       WHERE status = 'queued' AND available_at <= now()
--       ORDER BY available_at, id
--       FOR UPDATE SKIP LOCKED
--       LIMIT 1
--   )
--   RETURNING *;
--
-- On failure: set status back to 'queued' with available_at = now() + backoff,
-- or to 'dead' once attempts >= max_attempts. Swap in Redis/SQS later and
-- note the migration in your decisions log.