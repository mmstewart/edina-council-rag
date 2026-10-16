# Plan: Edina Council RAG, Phase 1

Status: draft for review. No code has been written yet. Findings come from reading the starter files and
`local/folder_browse.har`. The HAR is a capture of the 2025 packets folder (id 851714) plus opening the 2025-10-07
packet. I made no requests to the live site.

---

## 1. Repo setup

**Dependencies.** Use `uv` (already installed at `~/.local/bin/uv`) with a `pyproject.toml` and a committed `uv.lock`,
pinned to Python 3.12.
- Runtime: `requests`, `psycopg[binary]` (v3), `pgvector`, `pymupdf`, `python-dotenv`, `fastembed` (local ONNX embeddings, no
  PyTorch), and `ollama` (client for a local LLM). No paid APIs; see Decisions.
- Dev: `pytest`, `ruff`.

pytest is not installed right now, so the existing tests have not been run. That is the first thing M0 checks.

**Layout.** Move the flat files into a package now, while it is still cheap:
```
edina_rag/   crawler.py  filename_parser.py  migrate.py  db.py  extract.py  chunk.py  embed.py  retrieve.py  answer.py  cli.py
migrations/  0001_init.sql            (was schema.sql; fixes 1–6 below applied)
tests/       test_filename_parser.py  ...
eval/        questions.jsonl  runs/   (eval results are committed so before/after numbers are on record)
docker-compose.yml  .env.example  DECISIONS.md
```

**docker-compose.**
- One service, image `pgvector/pgvector:0.8.0-pg17`. Pin the version so HNSW behavior is reproducible.
- Store data in a named volume, not a `./pgdata` bind mount. On WSL2, bind mounts under `/home` are slow and run into
  permission problems.
- Expose the database on host port 5433 to avoid clashing with a local Postgres.
- Add a healthcheck using `pg_isready`.
- Read credentials from `.env`, with `DATABASE_URL` documented in `.env.example`.
- `docker` isn't on PATH in this WSL shell, so turn on Docker Desktop's WSL integration first.

**Applying the schema.** Use `python -m edina_rag.migrate`. It applies `migrations/*.sql` in order, each file inside one
transaction, and records what it applied in a `schema_migrations` table. That makes it safe to re-run, which satisfies
the CLAUDE.md idempotency rule. I don't recommend the `docker-entrypoint-initdb.d` approach because it only runs on an
empty volume. Also add a `db-reset` command that drops and recreates the database for development.

**.gitignore.** It already covers the important things: PDFs, HARs, `raw_cache/`, `local/`, `.env`. Add:
- `*.part` (partial downloads)
- `cookies*.txt`
- `.coverage`, `htmlcov/`
- `*.egg-info/`, `dist/`, `build/`
- `.ipynb_checkpoints/`

Because of `*.pdf`, tests must generate their fixture PDFs at runtime with PyMuPDF instead of committing them. I think
that's the better approach anyway.

### Does schema.sql look correct?

Yes, it should apply cleanly on Postgres 14+ with pgvector 0.5 or newer:
- The generated columns use immutable expressions: `length()`, and two-argument `to_tsvector` with a constant config.
- HNSW skips NULL embeddings.
- The constraints are consistent.

It hasn't actually been run yet, though; that is M0. Changes I'd make before the first migration:

| # | Change | Why |
|---|--------|-----|
| 1 | Change `vector(1536)` to `vector(384)` for `bge-small-en-v1.5` (see Decisions) | 1536 is a placeholder. Changing it later means rebuilding the column and the index. HNSW caps at 2000 dimensions. |
| 2 | Add `documents.file_size_bytes`, `source_modified_at`, `extension` | The listing returns these. `LastModified` is what makes incremental re-crawls possible in Phase 3. |
| 3 | Add `pages.has_text_layer BOOL` (or `pages.text_quality`) | Packets are born-digital but contain scanned attachments, so text quality varies page by page, not just per document. |
| 4 | Pick one source of truth: `documents.status` or `ingestion_jobs` | Today both track progress and will drift apart. I'd keep `ingestion_jobs` and derive status with a view. |
| 5 | Make the queue index partial: `ON ingestion_jobs (available_at, id) WHERE status = 'queued'` | Matches the claim query and stays small. |
| 6 | Set `updated_at` in a trigger, or always set it in the claim and finish SQL | Nothing updates it today. |
| 7 | Defer: a `chunk_embeddings(chunk_id, model, embedding)` table | One `embedding` column on `chunks` means comparing two models requires re-embedding in place. That is fine for Phase 1, so I'd add the table in Phase 2 when it's needed. |

### Other things I'd change in the starter files

- **crawler.py**
  - `_get` only does GET, but the listing API is a POST. Generalize it to `_request(method, ...)`.
  - Only PDFs are cached. CLAUDE.md says to cache every raw response, so also save listing JSON under
    `raw_cache/listings/`.
  - `main()` walks the whole City Council tree and rewrites `manifest.jsonl` with mode `"w"`. Add a `--folder` argument
    (Phase 1 only needs 851714), and upsert into `documents` instead. The DB becomes the resumable manifest.
  - `fetch_document_bytes` holds the whole file in memory, and the 2025-10-07 packet is 82 MB. Stream to the `.part`
    file and hash while writing.
  - Validate each download: it starts with `%PDF`, the size matches `Content-Length`, and PyMuPDF's page count matches
    the page count from the listing.
  - `USER_AGENT` still contains `you@example.com`.
- **filename_parser.py** is fine for every 2025 name in the HAR. Minor: "Joint …" or "Closed session" names would be
  classified as `regular`. Add test cases for them when they show up in older years.

---

## 2. Crawler: what the HAR shows

**Session.** `GET Browse.aspx?id=…&dbid=0&repo=Laserfiche` redirects (302) to `CookieCheck.aspx`, which redirects back
with `&cr=1`. `requests.Session` follows both and keeps the cookie, so `warm_up()` already works as written. The HAR was
sanitized (it has no cookies). No CSRF token was observed.

**`list_folder(folder_id)`** is a JSON POST:
```
POST /WebLink/FolderListingService.aspx/GetFolderListing2
Content-Type: application/json
X-Lf-Suppress-Login-Redirect: 1        # session problems come back as JSON errors, not a 302 to a login page
{"repoName":"Laserfiche","folderId":851714,"getNewListing":true,"start":0,"end":38,"sortColumn":"","sortAscending":true}
```

The response looks like this:
```
{"data": {...}, "__breach": "<random padding>"}
```
The `__breach` field is padding against the BREACH attack and can be ignored. Inside `data`:
- `failed` and `errMsg`: check these on every call.
- `totalEntries`: 46 for 2025.
- `colTypes[]`: names the columns of each row's `data` array.
- `results[]`: one item per entry, with `name`, `entryId`, `type`, `isEdoc`, `extension`, and the `data` array.

Implementation notes:
- **Paginate.** `start:0, end:38` returned 38 of 46 entries, so the last 8 (probably Nov–Dec 2025) are missing from
  the HAR. Loop `start += len(results)` until you reach `totalEntries`.
- **Map columns by name, not position.** Look up `PageCount`, `LastModified`, `Extension`, and `EntryType` by their
  index in `colTypes`.
- **Entry types.** Documents have `type == -2`. I haven't seen a folder row yet; my guess is `0`. Also handle shortcuts,
  which have a non-zero `shortcutTargetId`, and skip anything where `isEdoc` is false.
- Other endpoints in the HAR that are useful for sanity checks:
  - `GetBreadCrumbs` confirms 1 → City Council 6369 → Meeting Packets_Archive 6371 → 2025 851714.
  - `GetMetaData` returns the folder path and its created/modified dates.

**`fetch_document_bytes(entry)`.** The browser did **not** download from the city's server. WebLink loads a third-party
accessibility viewer (`docaccess.com/docbox.js`), and that viewer streamed the PDF from `docaccess.com/docs/<hash>/document.pdf`
using range requests. The viewer's metadata gives the canonical source:
```
https://edinadocs.edinamn.gov/WebLink/ElectronicFile.aspx?docid=856626&dbid=0&repo=Laserfiche
```
That is the standard Laserfiche WebLink 11 download endpoint. Plan: a streaming `GET` of that URL with the warmed
session. Do **not** scrape docaccess.com: it's a third party, and its copy is derived from the city's file.

**To capture before implementing** (one more short HAR, saved to `./local/`). In the Network tab, turn on **Preserve
log** and **Disable cache**, then:
1. Open **City Council** (6369) or **Meeting Packets_Archive** (6371). This gives a `GetFolderListing2` response that
   contains *folder* rows, so we can confirm the folder `type` code.
2. In the 2025 folder, scroll to the bottom so the second `GetFolderListing2` page (`start:38`) loads. This confirms
   whether `end` is exclusive.
3. Download one small packet using WebLink's own **Download** action, not by clicking to open it. Pick
   `2025-06-17 Work Session Meeting Packet` (3 pages, id 855459). Alternatively, paste the `ElectronicFile.aspx` URL
   above with `docid=855459` into a new tab. Record:
   - the final URL and any redirects
   - status, `Content-Type`, `Content-Disposition`, `Content-Length`
   - whether it returns the PDF itself or an HTML wrapper or "generating" page

---

## 3. Phase 1 milestones

Scope: 2025 packets only. That's 38 of 46 files seen so far, about 12,150 pages, and roughly 1.2–1.5 GB at about
100 KB/page. One process, no queue workers yet.

| # | Milestone | Done when |
|---|-----------|-----------|
| M0 | **Scaffold.** *(Done 2026-10-16 except the Docker check; see DECISIONS.md.)* pyproject/uv, package layout, compose, migration runner, schema fixes 1–6, `.env.example`, DECISIONS.md entries (uv, migrations, image pin). | `docker compose up -d` is healthy. `python -m edina_rag.migrate` runs twice with no errors and the second run is a no-op. `\dt` shows 5 tables plus `schema_migrations`. The existing parser tests pass under `uv run pytest`. |
| M1 | **Listing.** `list_folder` with pagination, column mapping, and raw JSON caching. The crawler upserts into `meetings` and `documents`. Unit tests run against JSON fixtures trimmed from the HAR (sanitized, committed). | Tests pass, offline. A live run on 851714 gives `documents` = `totalEntries` (46) with `status='discovered'`. A second run changes 0 rows and makes 0 listing requests, because the cache is used. Every row has `meeting_id`, or `needs_review` with a reason. |
| M2 | **Download.** Streaming `ElectronicFile.aspx` with validation, sha256, `raw_path`, and resume-after-kill. Try it on 1 small file first, then run the full year in the background (about 46 requests at ≥2 s apart; this is bandwidth-bound). | Every packet is downloaded. `page_count_extracted == page_count_reported` for 46/46, with mismatches flagged. Killing the run partway and restarting downloads only the missing files. No `.part` files are left behind. |
| M3 | **Extract.** PyMuPDF per page goes into `pages`, with `has_text_layer` set by a chars-per-page heuristic. A text-quality report gives the share of empty or low-text pages per document. | `count(pages) == sum(page_count_extracted)`. Re-running inserts 0 rows. Tests use a generated PDF with one text page and one image-only page. The report is written to DECISIONS.md and decides whether OCR moves into Phase 1. |
| M4 | **Chunk (v1, simple).** Page-aware fixed-size windows (about 500 tokens with about 15% overlap) that never cross document boundaries and record `page_start`/`page_end`. Agenda-item detection is out of scope for this milestone. Skip or tag boilerplate pages such as check registers and claims lists, based on M3's findings. | Unit tests cover page spans, overlap, empty pages, and determinism. Re-chunking with the same `chunker_version` is a no-op. The chunk count and token distribution are logged. |
| M5 | **Embed + index.** Batched embedding with retry, only for rows where `embedding IS NULL`, so it's resumable. Then build HNSW. | `count(*) WHERE embedding IS NULL` = 0. Re-running embeds 0 chunks. Throughput (chunks/s on this machine) is logged. A smoke query returns the right packet for an obvious question. |
| M6 | **Eval set (before any QA tuning).** 25 hand-written questions in `eval/questions.jsonl`, each with `{question, expected meeting_date, expected pages, answer notes}`. Mix: factual lookups, "when did council approve X", numeric (dollar amounts), and 3–4 unanswerable questions. Write them by reading packets, not by querying the system. | 25 questions, each with ground-truth page citations checked against the PDFs. |
| M7 | **Retrieval eval harness.** Vector-only top-k. Metrics: recall@5/10 and MRR at the page level (a hit is any overlap with the expected pages). | `uv run python -m edina_rag.eval retrieval` prints a table and writes `eval/runs/<date>-<config>.json`. The baseline number is recorded in DECISIONS.md. |
| M8 | **QA with citations.** Retrieve top-k, then an answer from a local LLM (Ollama) that must cite `[document, date, p. N]` drawn only from the supplied chunks, or say it doesn't know. Add a CLI `ask "…"`. | Every citation in the output maps to a real retrieved chunk, checked automatically. Unanswerable questions get abstentions. Answer-level scoring on the 25 questions (correct / partial / wrong / abstained) and seconds per answer are recorded. |
| M9 | **Wrap-up.** README with architecture, how to run, the eval table, and known limits. Tag `v0.1`. | Someone can clone, follow the README, ingest a single small packet, and ask a question. |

If ingestion turns out to be slow, M2–M5 can run on a subset of about 5 packets first. Don't block on the full year.

---

## 4. Risks and open questions

1. **PDF text quality.** 2025 files are almost certainly born-digital, but packets bundle scanned attachments:
   signed contracts, plats, letters. Until M3 measures the share of near-empty pages, I can't say whether OCR is a
   Phase 1 concern. My guess is under 10% of pages, so Phase 1 skips them and flags them. A related trap: some
   "born-digital" pages have garbage text layers (bad font encodings). The quality heuristic should look at the ratio
   of dictionary words, not just character count.
2. **Chunking by agenda item.** This is the real retrieval win, and it shapes citations, but packets have no machine
   structure. Options: the PDF outline/bookmarks, if present (check in M3); parsing the agenda pages at the front
   (item numbers like "VII.A") and matching item cover sheets; or page-header heuristics. I'd treat this as chunker v2
   in Phase 2, measured against the M7 baseline as CLAUDE.md requires. M3 should record whether outlines exist.
3. **Noise pages.** Check registers, claims lists, and plan sheets are large and will dominate nearest-neighbor
   results for any money question. Decide whether to drop or down-weight them (M4).
4. **Page-count drift.** The listing says the 2025-04-16 packet has 1,201 pages, above CLAUDE.md's "300–1,150". It's
   harmless but suggests the upper bound isn't really known. Page counts also vary by an order of magnitude, so
   per-document budgets (time, embedding cost) will be lumpy.
5. **Local models on modest hardware.** This WSL VM has 12 cores, about 7 GB RAM, and no GPU visible.
   - Embedding 30–40k chunks with a small model on CPU should take tens of minutes, not hours. M5 measures the real
     number.
   - A local answer LLM has to be small (about 3–4B parameters, 4-bit) to fit alongside Postgres. Expect slow answers
     (tens of seconds) and weaker citation discipline than a frontier model. The citation checker in M8 is what keeps
     this honest.
   - To give WSL more memory, raise `memory=` in `%UserProfile%\.wslconfig`. That's free, but it takes RAM from Windows.
   - Model files are free but large (hundreds of MB to a few GB). Keep them out of the repo: fastembed and Ollama
     cache them outside it.
6. **Download behavior is unverified.** `ElectronicFile.aspx` is inferred, not observed (section 2). If it serves a
   "generating PDF" interstitial for large files, M2 needs polling logic.
7. **Missing meetings.** There's no 2025-07-01 council packet in the listing, and Nov–Dec aren't in the HAR yet. Make
   sure the eval set doesn't assume meetings that aren't there.
8. **Terms of use.** Before M2, read edinadocs' terms and `robots.txt` once in your browser, then note the result in
   DECISIONS.md.
9. **Minutes vs packets.** Phase 1 has packets only, so questions about *what was decided* will often only be
   answerable from the next meeting's packet, which includes the prior minutes. Write eval questions about what was
   proposed or reported, or accept lower answer scores.

---

## Decisions

**No paid services (decided).** Everything runs locally for free. Each model sits behind a small interface (`Embedder`,
`Generator`), so a hosted model can be A/B tested later if that's ever wanted.

- **Embeddings:** `BAAI/bge-small-en-v1.5` via `fastembed`. That's 384 dimensions, CPU-only ONNX, and about 130 MB.
  - It's a strong small retriever and needs no PyTorch install, which matters with 7 GB RAM.
  - Bigger alternatives are `bge-base-en-v1.5` or `nomic-embed-text-v1.5` (768 dimensions). Comparing models later is a
    good Phase 2 eval experiment.
  - Migration 0001 uses `vector(384)`.
  - Mind the 512-token input limit: M4's ~500-token chunks fit, but token counts need measuring with the model's own
    tokenizer, not a word-based estimate.
- **Answer generation:** a small instruct model via Ollama, for example a 3–4B model at 4-bit.
  - Install Ollama on Windows or inside WSL before M8.
  - M8's citation checker and abstention tests matter even more with a small model.
- M0 writes a DECISIONS.md entry covering both choices: no paid APIs, the alternatives, and the trade-off of quality and
  latency against cost.
