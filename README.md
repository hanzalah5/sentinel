# Sentinel

**An agentic incident-response and root-cause copilot.** Sentinel ingests a production
alert, investigates it the way a senior SRE would — correlating the signals, forming a
ranked, evidence-backed root-cause hypothesis — and (in later iterations) proposes a
remediation behind a human-approval gate.

The engineering point of this project is **not** "an LLM that talks about incidents."
It is a production-grade agent *system*: typed at every boundary, traced end to end,
evaluated in CI, and scoped deliberately.

> **Status: Iteration 1 — the traced walking skeleton.** One alert in over HTTP → a
> single-node LangGraph agent → Gemini structured output → a typed `RootCauseHypothesis`
> out, fully traced and correlation-logged, with lint/type/test gates in CI.

---

## Architecture (Iteration 1)

```
                         ┌─────────────────────────────────────────────┐
   POST /investigate     │                FastAPI app                   │
  (Alert JSON) ────────► │  CorrelationIdMiddleware → typed endpoint    │
                         │            │                                 │
                         │            ▼                                 │
                         │   InvestigationService                       │
                         │            │  (correlation id + Langfuse cb) │
                         │            ▼                                 │
                         │   LangGraph: [triage_and_hypothesize]        │
                         │            │                                 │
                         │            ▼                                 │
                         │   Gemini .with_structured_output(...)        │
                         └────────────┼─────────────────────────────────┘
                                      ▼
                     RootCauseHypothesis (validated)  ──►  Langfuse trace
```

**Design choices that matter here**

- **Structured output, not JSON parsing.** The model is forced to return a
  `RootCauseHypothesis` — validation happens at the model boundary, so a malformed
  response is a caught error, not a downstream surprise.
- **Observability before features.** Tracing and correlation IDs were wired in the very
  first iteration; every request and its trace share one `correlation_id`.
- **Liveness ≠ readiness.** `/healthz` stays green whenever the process is up;
  `/readyz` only passes when the agent is actually wired (Gemini key present).
- **A provider seam.** The agent depends on a `BaseChatModel` from a `get_chat_model()`
  factory — a later iteration swaps in cost/latency-aware routing without touching the graph.

---

## Tech stack

| Concern | Choice |
|---|---|
| Orchestration | LangGraph (single node today, room to grow) |
| LLM | Google Gemini via `langchain-google-genai` |
| Type-safe I/O | Pydantic v2 + structured output |
| API | FastAPI |
| Observability | Langfuse (v3) + structured `structlog` logs, shared correlation ID |
| Tooling | `uv`, `ruff`, `mypy --strict`, `pytest` |
| Packaging / deploy | Docker + Docker Compose |
| CI | GitHub Actions: lint → format-check → type-check → test |

---

## Quickstart

Requires Python 3.12 and [`uv`](https://docs.astral.sh/uv/).

```bash
# 1. Install dependencies (creates .venv, resolves the lockfile)
uv sync

# 2. Configure (optional for tests; required for a live call)
cp .env.example .env        # then set GOOGLE_API_KEY

# 3. Quality gates — all run offline, no API key needed
uv run ruff check .
uv run mypy src tests
uv run pytest

# 4. Run the API locally
uv run uvicorn sentinel.api.app:create_app --factory --reload --port 8000
```

(If you have `make`: `make check` runs lint + typecheck + test; `make run` starts the API.)

### Try it

```bash
curl -s http://localhost:8000/healthz

curl -s -X POST http://localhost:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{
    "alert_id": "alert-1024",
    "title": "High p99 latency on checkout-api",
    "description": "p99 latency rose to 2.4s; DB connection pool saturation warnings.",
    "severity": "critical",
    "service": "checkout-api",
    "source": "prometheus",
    "fired_at": "2026-07-12T09:31:00Z",
    "labels": {"region": "us-east-1"}
  }'
```

Interactive API docs at `http://localhost:8000/docs`.

### Docker

```bash
docker compose up --build      # builds the image and serves on :8000
```

---

## Configuration

All settings load from the environment or a local `.env` (see `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | — | Gemini key. Without it, `/investigate` returns `503`. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model id. |
| `LLM_TEMPERATURE` | `0.1` | Sampling temperature. |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | — | Enable tracing when both are set. |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse endpoint. |
| `LOG_LEVEL` / `JSON_LOGS` | `INFO` / `true` | Logging. |

---

## Deliberate cuts & roadmap

Scope is a feature. Iteration 1 ships one complete, instrumented slice; everything below
is intentionally deferred to the iteration that first *needs* it (infra included).

- [ ] **Iteration 2 — Ingestion:** Redis Streams alert queue + a replayable synthetic
      incident generator (backpressure, at-least-once).
- [ ] **Iteration 3 — Runbook RAG:** Postgres + pgvector retrieval to ground hypotheses
      in real runbooks.
- [ ] **Iteration 4 — Durable HITL:** LangGraph checkpointer + `interrupt()`; remediation
      pauses for human approval and resumes on decision.
- [ ] **Iteration 5 — Eval-as-CI:** a labelled incident set scoring root-cause accuracy,
      gating merges on regression.
- [ ] **Iteration 6 — Cost routing & hardening:** cheap-model triage + frontier-model
      reasoning, semantic cache, rate limiting, dashboard, deploy.

---

## Project layout

```
src/sentinel/
  config.py         # pydantic-settings
  logging.py        # structlog + correlation-id contextvar
  contracts.py      # Alert, RootCauseHypothesis, InvestigationResult, ...
  llm.py            # get_chat_model() provider seam (Gemini)
  observability.py  # Langfuse v3 client + LangChain callback handler
  service.py        # InvestigationService: run graph, package result
  agent/
    state.py        # typed graph state
    graph.py        # the LangGraph investigation graph
  api/
    middleware.py   # CorrelationIdMiddleware
    app.py          # FastAPI factory, lifespan, endpoints
tests/              # offline suite (fake model), keyless
```
