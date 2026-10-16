I'm building a portfolio project to show AI-engineering skills: a RAG system over City of Edina, MN city council meeting
records. Read CLAUDE.md, schema.sql, filename_parser.py, test_filename_parser.py, and crawler.py first.

Do not write or change any code yet. This session is planning only. Produce a written plan I can review and edit,
covering:

1. Repo setup: dependency management, docker-compose for Postgres + pgvector, a script to apply schema.sql, and a
   .gitignore review. Flag anything in the starter files you'd change, and say whether schema.sql looks correct (I
   haven't run it against a real Postgres yet).
2. Crawler: how to implement Crawler.list_folder() and fetch_document_bytes(). If ./local/folder_browse.har exists,
   inspect it to see how Laserfiche WebLink lists a folder and downloads a document. If it doesn't, tell me exactly what
   to capture from my browser's Network tab. Do not make requests to the live site.
3. Phase 1 milestones: ordered, each small enough to finish in one session, each with a concrete "done" check (tests
   pass, row counts, etc.). Scope is one year of packets (2025), single process, simple chunking, basic QA with
   citations, and about 25 eval questions.
4. Risks and open questions you'd want answered before coding (PDF text quality, OCR, chunking by agenda item, embedding
   model choice).

Keep the plan concise. End with the one decision you most need from me.
