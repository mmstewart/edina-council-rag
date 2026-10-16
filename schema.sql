-- Edina council meetings RAG schema
-- Requires Postgres 14+ with the pgvector extension.
-- Change vector(1536) to match your embedding model's dimension.

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
    page_count_reported   INT,                  -- from the Laserfiche listing
    page_count_extracted  INT,                  -- from our own extraction; should match
    sha256                TEXT,
    raw_path              TEXT,                 -- where the untouched file is stored
    text_quality          TEXT NOT NULL DEFAULT 'unknown'
        CHECK (text_quality IN ('born_digital', 'ocr', 'mixed', 'unknown')),
    needs_review          BOOLEAN NOT NULL DEFAULT FALSE,
    review_reason         TEXT,
    status                TEXT NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered', 'downloaded', 'extracted',
                          'chunked', 'embedded', 'failed')),
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
    embedding        vector(1536),
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
CREATE INDEX ingestion_jobs_claim_idx ON ingestion_jobs (status, available_at);

-- Worker claim query (run inside a transaction):
--
--   UPDATE ingestion_jobs SET status = 'running', locked_at = now(),
--          locked_by = $1, attempts = attempts + 1, updated_at = now()
--   WHERE id = (
--       SELECT id FROM ingestion_jobs
--       WHERE status = 'queued' AND available_at <= now()
--       ORDER BY id
--       FOR UPDATE SKIP LOCKED
--       LIMIT 1
--   )
--   RETURNING *;
--
-- On failure: set status back to 'queued' with available_at = now() + backoff,
-- or to 'dead' once attempts >= max_attempts. Swap in Redis/SQS later and
-- note the migration in your decisions log.