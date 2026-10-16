"""Migration runner and schema checks against a real Postgres.

Each test gets its own throwaway database on the server at DATABASE_URL, so
the dev database is never touched. Skipped when no server is reachable.
"""
import os
import shutil
import uuid

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from edina_rag.migrate import MIGRATIONS_DIR, MigrationError, migrate

pytestmark = pytest.mark.db


@pytest.fixture
def conn():
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    try:
        admin = psycopg.connect(url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"no Postgres at DATABASE_URL: {e}")
    name = f"edina_test_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(url)
    params["dbname"] = name
    c = psycopg.connect(make_conninfo(**params))
    try:
        yield c
    finally:
        c.close()
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.close()


def test_migrate_is_idempotent(conn):
    assert migrate(conn) == ["0001_init.sql"]
    assert migrate(conn) == []
    tables = {r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert tables == {"meetings", "documents", "pages", "chunks", "ingestion_jobs", "schema_migrations"}


def test_embedding_dimension_is_384(conn):
    migrate(conn)
    typ = conn.execute("""
        SELECT format_type(atttypid, atttypmod) FROM pg_attribute
        WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'""").fetchone()[0]
    assert typ == "vector(384)"


def test_edited_migration_is_rejected(conn, tmp_path):
    for f in MIGRATIONS_DIR.glob("*.sql"):
        shutil.copy(f, tmp_path / f.name)
    migrate(conn, tmp_path)
    p = tmp_path / "0001_init.sql"
    p.write_text(p.read_text() + "\n-- edited\n")
    with pytest.raises(MigrationError, match="edited"):
        migrate(conn, tmp_path)


def _doc(conn):
    return conn.execute("""
        INSERT INTO documents (doc_kind, source_system, source_id, filename)
        VALUES ('packet', 'laserfiche_archive', '851717', '2025-01-07 City Council Meeting Packet')
        RETURNING id""").fetchone()[0]


def test_document_status_view_and_updated_at_trigger(conn):
    migrate(conn)
    doc = _doc(conn)
    assert conn.execute("SELECT last_done_stage, has_pending FROM document_status WHERE document_id = %s",
                        (doc,)).fetchone() == (None, False)

    for stage in ("download", "extract", "chunk"):
        conn.execute("INSERT INTO ingestion_jobs (document_id, stage, status) VALUES (%s, %s, 'done')",
                     (doc, stage))
    job = conn.execute("""INSERT INTO ingestion_jobs (document_id, stage) VALUES (%s, 'embed')
                          RETURNING id, updated_at""", (doc,)).fetchone()
    assert conn.execute("SELECT last_done_stage, has_failures, has_pending FROM document_status "
                        "WHERE document_id = %s", (doc,)).fetchone() == ("chunk", False, True)

    # Separate transactions so now() differs between insert and update.
    conn.commit()
    conn.execute("UPDATE ingestion_jobs SET status = 'failed' WHERE id = %s", (job[0],))
    conn.commit()
    updated = conn.execute("SELECT updated_at FROM ingestion_jobs WHERE id = %s", (job[0],)).fetchone()[0]
    assert updated > job[1]
    assert conn.execute("SELECT has_failures FROM document_status WHERE document_id = %s",
                        (doc,)).fetchone()[0] is True


def test_duplicate_job_enqueue_is_rejected(conn):
    migrate(conn)
    doc = _doc(conn)
    conn.execute("INSERT INTO ingestion_jobs (document_id, stage) VALUES (%s, 'download')", (doc,))
    n = conn.execute("""INSERT INTO ingestion_jobs (document_id, stage) VALUES (%s, 'download')
                        ON CONFLICT DO NOTHING""", (doc,)).rowcount
    assert n == 0
