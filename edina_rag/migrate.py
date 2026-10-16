"""Apply migrations/*.sql in filename order, each once, each in its own transaction.

Applied files are recorded in schema_migrations with a checksum, so re-running
is a no-op and an edited, already-applied migration is an error rather than a
silent divergence. Fix forward with a new numbered file instead.

Usage:  uv run python -m edina_rag.migrate            # apply pending migrations
        uv run python -m edina_rag.migrate --reset    # drop everything first (dev only)
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

log = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Arbitrary constant; keeps two runners from applying the same file concurrently.
LOCK_KEY = 0x45444E41


class MigrationError(RuntimeError):
    pass


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


def pending(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Files not yet applied. Raises if an applied file has changed or gone missing."""
    applied = dict(conn.execute("SELECT filename, sha256 FROM schema_migrations").fetchall())
    files = sorted(migrations_dir.glob("*.sql"))
    names = {f.name for f in files}
    missing = sorted(set(applied) - names)
    if missing:
        raise MigrationError(f"applied migrations missing from {migrations_dir}: {missing}")
    todo = []
    for f in files:
        if f.name not in applied:
            todo.append(f)
        elif applied[f.name] != _checksum(f.read_text()):
            raise MigrationError(f"{f.name} was edited after being applied; add a new migration instead")
    return todo


def migrate(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations; returns the filenames applied (empty on a no-op run)."""
    conn.autocommit = True
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            sha256      TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        done = []
        for f in pending(conn, migrations_dir):
            sql = f.read_text()
            with conn.transaction():
                conn.execute(sql)  # no params, so multi-statement files are allowed
                conn.execute("INSERT INTO schema_migrations (filename, sha256) VALUES (%s, %s)",
                             (f.name, _checksum(sql)))
            log.info("applied %s", f.name)
            done.append(f.name)
        return done
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def reset(conn: psycopg.Connection) -> None:
    """Drop and recreate the public schema. Destroys all data; for local dev only."""
    conn.autocommit = True
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")


def database_url() -> str:
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set; copy .env.example to .env")
    return url


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reset", action="store_true", help="drop all tables first (destroys data)")
    args = ap.parse_args()
    with psycopg.connect(database_url()) as conn:
        if args.reset:
            reset(conn)
            log.warning("reset: dropped and recreated schema public")
        applied = migrate(conn)
    log.info("applied %d migration(s)%s", len(applied), "" if applied else "; already up to date")


if __name__ == "__main__":
    main()
