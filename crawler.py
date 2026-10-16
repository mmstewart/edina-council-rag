"""Polite crawler skeleton for Edina's Laserfiche WebLink archive.

Two methods are deliberately left for you to fill in, because they depend on
requests the portal makes behind the scenes that I couldn't observe:

    Crawler.list_folder(folder_id)       -> list[Entry]
    Crawler.fetch_document_bytes(entry)  -> bytes

Find them by opening a folder in your browser with dev tools open on the
Network tab, then replay those requests here using self._get(). Everything
else (cookies, throttling, retries, raw caching, folder walk, filename
parsing, review flagging) is done.

Usage:  python crawler.py            # writes manifest.jsonl
Before running: put a real contact address in USER_AGENT and check the
city's terms of use and robots.txt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import requests

from filename_parser import kind_from_name, parse_filename

log = logging.getLogger("crawler")

BASE = "https://edinadocs.edinamn.gov/WebLink"
USER_AGENT = "edina-rag-portfolio/0.1 (contact: you@example.com)"  # <- edit me
MIN_DELAY_S = 2.0
RAW_DIR = Path("raw_cache")
CITY_COUNCIL_FOLDER_ID = 6369  # the id in your Browse.aspx URL; verify it's "City Council"
RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Entry:
    entry_id: int
    name: str
    is_folder: bool
    page_count: int | None = None


class Crawler:
    def __init__(self, delay: float = MIN_DELAY_S):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.delay = delay
        self._last = 0.0
        RAW_DIR.mkdir(exist_ok=True)

    # -- HTTP -------------------------------------------------------------
    def _get(self, url: str, **kw) -> requests.Response:
        """GET with a minimum delay between requests and backoff on transient errors."""
        for attempt in range(4):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.s.get(url, timeout=60, **kw)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 3:
                    raise
                time.sleep(5 * 2**attempt)
                continue
            if r.status_code in RETRY_STATUSES and attempt < 3:
                log.warning("HTTP %s from %s, backing off", r.status_code, url)
                time.sleep(5 * 2**attempt)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError("unreachable")

    def warm_up(self) -> None:
        """WebLink needs a cookie session before it will show folder contents."""
        self._get(f"{BASE}/Browse.aspx", params={"id": CITY_COUNCIL_FOLDER_ID,
                                                  "dbid": 0, "repo": "Laserfiche"})

    # -- TODO: fill these in from the browser's Network tab ----------------
    def list_folder(self, folder_id: int) -> list[Entry]:
        raise NotImplementedError("replay the folder-listing request you see in dev tools")

    def fetch_document_bytes(self, entry: Entry) -> bytes:
        raise NotImplementedError("replay the document download request (a direct .../edoc/<id>/<name>.pdf link may work)")

    # -- Done --------------------------------------------------------------
    def download(self, entry: Entry, dest_dir: Path = RAW_DIR) -> tuple[Path, str]:
        """Download once; later runs reuse the cached file. Returns (path, sha256)."""
        dest = dest_dir / f"{entry.entry_id}.pdf"
        if not dest.exists():
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(self.fetch_document_bytes(entry))
            tmp.rename(dest)  # atomic, so a crash never leaves a half-written PDF
        return dest, hashlib.sha256(dest.read_bytes()).hexdigest()

    def walk(self, folder_id: int, path: tuple[str, ...] = (),
             default_kind: str = "other") -> Iterator[dict]:
        """Depth-first walk yielding one manifest record per file."""
        for e in self.list_folder(folder_id):
            if e.is_folder:
                kind = kind_from_name(e.name) or default_kind
                yield from self.walk(e.entry_id, path + (e.name,), kind)
                continue
            p = parse_filename(e.name, default_kind)
            if p.needs_review:
                log.warning("needs review: %r (%s)", e.name, p.reason)
            yield {
                "entry_id": e.entry_id,
                "folder_path": "/".join(path),
                "filename": e.name,
                "page_count_reported": e.page_count,
                "meeting_date": p.meeting_date.isoformat() if p.meeting_date else None,
                "meeting_type": p.meeting_type,
                "doc_kind": p.doc_kind,
                "needs_review": p.needs_review,
                "review_reason": p.reason,
            }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    c = Crawler()
    c.warm_up()
    with open("manifest.jsonl", "w") as out:
        for rec in c.walk(CITY_COUNCIL_FOLDER_ID):
            out.write(json.dumps(rec) + "\n")
    log.info("wrote manifest.jsonl")


if __name__ == "__main__":
    main()