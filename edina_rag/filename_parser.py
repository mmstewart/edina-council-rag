"""Tolerant parsing of Laserfiche file and folder names.

Never raises on odd input: anything it can't parse comes back with
needs_review=True so the crawler can log it instead of crashing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import unquote

# 2025-10-07, 2017 02 22, 2014-10-07_COUNCIL ..., 20251007
_DATE_RE = re.compile(r"^(?P<y>(?:19|20)\d{2})[-_ .]?(?P<m>\d{1,2})[-_ .]?(?P<d>\d{1,2})(?!\d)")
_YEAR_FOLDER_RE = re.compile(r"^\s*(?P<a>(?:19|20)\d{2})(?:\s*-\s*(?P<b>(?:19|20)\d{2}))?")
_EXT_RE = re.compile(r"\.(pdf|docx?|txt)$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedName:
    raw: str
    meeting_date: date | None
    meeting_type: str          # regular | work_session | special | special_work_session | other
    doc_kind: str              # packet | minutes | agenda | other
    needs_review: bool
    reason: str | None = None


def _normalize(name: str) -> str:
    s = unquote(name)
    s = _EXT_RE.sub("", s.strip())
    s = s.replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def kind_from_name(name: str) -> str | None:
    """Doc kind implied by a file or folder name, or None if it says nothing."""
    n = name.lower()
    if "minutes" in n:
        return "minutes"
    if "packet" in n:
        return "packet"
    if "agenda" in n:
        return "agenda"
    return None


def classify_meeting_type(rest: str) -> str:
    r = rest.lower()
    special = "special" in r
    if "work session" in r or "worksession" in r:
        return "special_work_session" if special else "work_session"
    if special:
        return "special"
    # "meetng" because the city's own filenames contain that typo
    if "council" in r or "meeting" in r or "meetng" in r:
        return "regular"
    return "other"


def parse_filename(name: str, default_kind: str = "other") -> ParsedName:
    s = _normalize(name)
    kind = kind_from_name(s) or default_kind

    m = _DATE_RE.match(s)
    if not m:
        return ParsedName(name, None, "other", kind, True, "no leading date")
    try:
        d = date(int(m["y"]), int(m["m"]), int(m["d"]))
    except ValueError:
        return ParsedName(name, None, "other", kind, True, "invalid calendar date")

    mtype = classify_meeting_type(s[m.end():])
    if mtype == "other":
        return ParsedName(name, d, mtype, kind, True, "unrecognized meeting type")
    return ParsedName(name, d, mtype, kind, False)


def parse_year_folder(name: str) -> tuple[int, int] | None:
    """'2025' -> (2025, 2025); '1974-1979' -> (1974, 1979); '1981-1985 Agendas' -> (1981, 1985)."""
    m = _YEAR_FOLDER_RE.match(name)
    if not m:
        return None
    a = int(m["a"])
    b = int(m["b"]) if m["b"] else a
    return (a, b) if a <= b else None