# Chart Analyzer

Backend test assignment (Zimran / Finelo): upload a trading chart image, get an AI analysis
enriched with real market data, keep history, enforce a daily token quota.
Design and rationale: [docs/DESIGN.md](docs/DESIGN.md). Read it before changing behaviour.

## Commands

```bash
uv sync                                   # install
docker compose up -d postgres redis rabbitmq   # infra for local runs and integration tests
uv run pytest tests/unit                  # fast, no services needed
uv run pytest                             # all tests (needs the three services above)
uv run ruff check . && uv run ruff format . && uv run mypy
uv run alembic upgrade head               # apply migrations to DATABASE_URL
docker compose up --build                 # whole system: api, worker, migrate, infra
```

Host ports of the dockerised infra: Postgres `55432`, Redis `56379`, RabbitMQ `55672`
(management UI `15673`, user/password `chart`/`chart`). Integration tests default to them
and use the separate database `chart_analyzer_test` and Redis db `1`.

## Rules of the codebase

- Dependencies point inwards: `api` / `worker` / `infrastructure` → `application` → `domain`.
  `domain` and `application` never import FastAPI, SQLAlchemy, aio-pika, redis, httpx or openai.
- Use cases depend on ports in `application/ports`; adapters live in `infrastructure`.
  Only `bootstrap.py` constructs adapters.
- A new external dependency gets a port first, then an adapter, then a fake in `tests/fakes.py`.
- State changes of an analysis are single atomic statements in `AnalysisRepository`
  (`claim`, `complete`, `fail`, …). Do not read-modify-write an analysis.
- A worker finishes, fails or releases an analysis only with the claim number it got from
  `claim` (`attempt=analysis.attempts`): that is the fencing against a second worker.
- `ANALYSIS_DEADLINE_SECONDS` must stay below `ANALYSIS_LEASE_SECONDS`, and
  `ANALYSIS_STALE_AFTER_SECONDS` above the whole retry schedule.
- Only the successful AI calls are added to `tokens_used`; that is what keeps an analysis
  within `QUOTA_RESERVATION_TOKENS`.
- The quota check and the insert of an analysis must stay inside one transaction that
  starts with `uow.lock_user(user_id)`.
- Enum values (`domain/enums.py`) are part of the API contract. Do not rename them.
- Every `ErrorCode` needs a message in all nine languages (`application/localization.py`);
  a unit test enforces this.
- Log with `structlog.get_logger(__name__)`, event names like `analysis.completed`,
  data as key-value pairs. Never log image bytes, API keys or full AI prompts.
- Tests: unit tests use fakes only; anything touching a real service goes to
  `tests/integration` and is marked `@pytest.mark.integration`.

## Secrets

`.env` is git-ignored and holds `FINNHUB_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`.
`.env.example` documents every variable. Never print or commit real keys.

## State of external access (2026-10-13)

- Finnhub free key works: quote (stocks and `BINANCE:<BASE>USDT`), profile2, recommendation,
  company-news, news?category=crypto. `/stock/price-target` is premium (403).
  Unknown symbols return HTTP 200 with price 0, not an error.
- The LiteLLM key (`https://litellm.zimran.net/v1`) allows `gpt-5-mini` (chat, images,
  strict structured outputs). The AI adapter and the whole flow were run against it:
  recognition about 1,700 tokens and 2-3 s, analysis about 5,500 tokens and 10-17 s.
  `gpt-5-mini` is a reasoning model: `max_completion_tokens` must leave room for reasoning.
