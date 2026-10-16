# Edina Council RAG

Retrieval-augmented QA over City of Edina, MN city council meeting records. See [PLAN.md](PLAN.md) for the roadmap and
[DECISIONS.md](DECISIONS.md) for design choices.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and Docker (on WSL, turn on Docker Desktop's WSL integration).

```bash
cp .env.example .env
uv sync
docker compose up -d --wait
uv run python -m edina_rag.migrate
uv run pytest
```

Database tests (`-m db`) create and drop their own throwaway database on the server at `DATABASE_URL`, and are skipped
when no server is reachable.
