# Sentinel

[![CI](https://github.com/hanzalah5/sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/hanzalah5/sentinel/actions/workflows/ci.yml)
&nbsp;[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](LICENSE)

**An agentic incident-response and root-cause copilot.** Sentinel ingests a production
alert, investigates it the way a senior SRE would — correlating the signals, forming a
ranked, evidence-backed root-cause hypothesis — and (in later iterations) proposes a
remediation behind a human-approval gate.

The engineering point of this project is **not** "an LLM that talks about incidents."
It is a production-grade agent *system*: typed at every boundary, traced end to end,
evaluated in CI, and scoped deliberately.

> **Status: Iteration 4 — durable human-in-the-loop remediation.** An incident now runs
> investigate → **plan a remediation** → **pause for human approval** → execute. The
> pause is a LangGraph `interrupt()` backed by a **checkpointer** (Postgres for true
> cross-restart durability, in-memory for dev), so a paused incident survives a restart
> and resumes on decision. Execution is **simulated** — the agent never touches real
> infrastructure. (Earlier: I1 traced skeleton; I2 Redis Streams ingestion; I3 pgvector
> runbook RAG.)

---

## Architecture

Alerts reach the agent two ways — synchronously via `POST /investigate`, or
asynchronously off a Redis Stream consumed by the ingestion worker. Both drive the same
graph:

```
   POST /investigate ─┐                        ┌─ Redis Stream (alerts)
   (Alert JSON)       │                        │      │  ingestion worker
                      ▼                        ▼      ▼  (at-least-once + DLQ)
                 ┌───────────────────────────────────────────────┐
                 │              InvestigationService              │
                 │        (correlation id + Langfuse trace)       │
                 │                      │                         │
                 │   LangGraph:  retrieve_runbooks ──► triage     │
                 │                  │                     │       │
                 │     Postgres + pgvector          Gemini        │
                 │     (top-k runbooks)     .with_structured_output│
                 └──────────────────────┼────────────────────────┘
                                        ▼
                      RootCauseHypothesis (validated, cites runbooks)
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
- **Grounded, and degradable.** Retrieval is behind a `RunbookRetriever` protocol; the
  real one uses pgvector, but with no DB configured (or on a lookup error) it falls back
  to a no-op and the agent still answers. RAG never becomes a hard dependency.
- **Durable human-in-the-loop.** The incident graph pauses at `interrupt()` for any
  action above the auto-approve risk ceiling (default: *everything* needs approval). A
  checkpointer persists the paused state, so approval can arrive minutes or a restart
  later and resume exactly where it left off. Execution is behind a
  `RemediationExecutor` protocol; the only shipped executor is a safe simulator.
- **Checkpoints revive only known types.** LangGraph's default revives any class named
  in a checkpoint. Sentinel's checkpointer uses a strict serializer instead: LangGraph's
  built-in safe types plus Sentinel's own state types, derived from its contract
  modules. Anything else comes back as plain data. A test walks `IncidentState` so a
  new state type can't be left off the list, and CI resumes a paused incident from a
  fresh Postgres connection to prove it survives a restart.
- **Authenticated, attributable decisions.** Every endpoint except liveness/readiness
  requires a bearer API key, compared in constant time against SHA-256 digests. With no
  keys configured the API fails closed. Each approval or rejection records *who* made it
  and *why*, so the human-in-the-loop step leaves an audit trail.

### Incident graph (Iteration 4)

```
retrieve_runbooks ─► triage ─► plan_remediation ─► request_approval ─approved─► execute ─► END
                                                          │ interrupt()
                                                          └─rejected──────────────────────► END
   (compiled with a checkpointer: PostgresSaver when DATABASE_URL is set, else MemorySaver)
```

---

## Tech stack

| Concern | Choice |
|---|---|
| Orchestration | LangGraph (`retrieve_runbooks` → `triage_and_hypothesize`) |
| LLM | Google Gemini via `langchain-google-genai` |
| Retrieval (RAG) | Runbook grounding via Postgres + **pgvector** (cosine, HNSW), Gemini embeddings |
| Type-safe I/O | Pydantic v2 + structured output |
| API | FastAPI |
| Durable HITL | LangGraph `interrupt()` + checkpointer (PostgresSaver / MemorySaver), risk-tiered approval |
| Ingestion | Redis Streams — consumer groups, at-least-once, dead-letter queue |
| Observability | Langfuse (v3) + structured `structlog` logs, shared correlation ID |
| Tooling | `uv`, `ruff`, `mypy --strict`, `pytest` |
| Packaging / deploy | Multi-stage Docker image (non-root, no build tooling, healthcheck) + Docker Compose |
| CI | GitHub Actions: lint → format-check → type-check → test (Python 3.12 + 3.13); image build + container smoke test |

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

Every endpoint except `/healthz` and `/readyz` needs an API key. Generate one and add it
to `.env` as a `name:key` pair (the name is how the caller appears in logs and on
approval decisions):

```bash
export SENTINEL_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
echo "SENTINEL_API_KEYS=local-dev:$SENTINEL_API_KEY" >> .env   # then restart the API
```

```bash
curl -s http://localhost:8000/healthz

curl -s -X POST http://localhost:8000/investigate \
  -H "Authorization: Bearer $SENTINEL_API_KEY" \
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

Interactive API docs at `http://localhost:8000/docs` (use **Authorize** to set the key).

### Ingestion pipeline (event-driven path)

Instead of calling `/investigate` directly, alerts can flow through Redis Streams:

```bash
# Terminal 1 — start Redis + the app + the consumer worker
docker compose up --build

# Terminal 2 — replay synthetic incidents onto the stream
uv run python -m sentinel.ingestion.generator --count 20 --rate 2 --seed 42
# (or: make seed)
```

The worker consumes each alert, runs an investigation, and ACKs only on success.
Failed alerts are retried; alerts that fail repeatedly (or can't be parsed) land in the
dead-letter stream `sentinel:alerts:dlq`. To run the worker outside Docker (needs a
reachable `REDIS_URL` and `GOOGLE_API_KEY`): `make worker`.

### Runbook retrieval (RAG)

To ground hypotheses in runbooks, point `DATABASE_URL` at Postgres+pgvector and ingest
the `runbooks/` corpus once:

```bash
# docker compose brings up a pgvector-enabled Postgres alongside the app/worker
docker compose up --build

# embed the runbooks into pgvector (needs DATABASE_URL + GOOGLE_API_KEY)
uv run python -m sentinel.rag.ingest --path runbooks
# (or: make ingest)
```

After ingest, `/investigate` responses include `runbook_references` naming the runbooks
the analysis used. Leave `DATABASE_URL` unset to disable retrieval entirely — the agent
still runs, ungrounded.

### Incident lifecycle with human approval (HITL)

The incident flow investigates, proposes a remediation, and **pauses for approval**:

```bash
# 1. Open an incident -> returns a thread_id and status "awaiting_approval"
curl -s -X POST http://localhost:8000/incidents \
  -H "Authorization: Bearer $SENTINEL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"alert_id":"inc-1","title":"5xx spike on payments-api",
       "description":"5xx rose to 15% after deploy v4.1.0","severity":"critical",
       "service":"payments-api","source":"prometheus","fired_at":"2026-07-13T09:31:00Z"}'

# 2. Inspect the durable state any time
curl -s -H "Authorization: Bearer $SENTINEL_API_KEY" http://localhost:8000/incidents/<thread_id>

# 3. Approve (or reject) -> the paused graph resumes and executes (or skips)
curl -s -X POST http://localhost:8000/incidents/<thread_id>/decision \
  -H "Authorization: Bearer $SENTINEL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"decision":"approved","note":"rollback window confirmed"}'
```

The response records `decided_by` (the caller's key name, or `policy:auto-approve` for
actions under the auto-approve ceiling) and `decision_note`. Both are stored in the
checkpointed state, so the audit trail survives restarts along with the incident.

With `DATABASE_URL` set, the pause is persisted in Postgres — restart the process between
steps 1 and 3 and the incident still resumes correctly. Execution is simulated; no real
infrastructure is touched.

### Docker

```bash
docker compose up --build   # postgres (pgvector) + redis + app (:8000) + worker
```

The image is multi-stage: dependencies are installed from `uv.lock` in a build stage, and
the runtime stage carries only the virtualenv and runbooks. It runs as an unprivileged
user, listens on `$PORT` (default 8000, so platforms like Cloud Run can set it), has a
`HEALTHCHECK` on `/healthz`, and runs uvicorn as PID 1 so `SIGTERM` triggers a graceful
shutdown. CI builds the image and checks all of this on every push.

```bash
docker build -t sentinel .
docker run --rm -p 8000:8000 --env-file .env sentinel
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
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse endpoint (use the US host if that's your region). |
| `DATABASE_URL` | — | Postgres+pgvector DSN. Unset = retrieval disabled (agent still runs). |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `gemini-embedding-001` / `768` | Embeddings for runbook retrieval. |
| `RUNBOOK_TOP_K` | `3` | Number of runbooks retrieved per alert. |
| `HITL_AUTO_APPROVE_MAX_RISK` | `none` | Auto-approve actions up to this risk; `none` = always require approval. |
| `SENTINEL_API_KEYS` | — | Comma-separated `name:key` pairs (keys ≥ 24 chars). Callers send `Authorization: Bearer <key>`. Unset = every protected endpoint returns `503` and `/readyz` is not ready (fail closed). Malformed values stop startup. |
| `LOG_LEVEL` / `JSON_LOGS` | `INFO` / `true` | Logging. |

---

## Deliberate cuts & roadmap

Scope is a feature. Iteration 1 ships one complete, instrumented slice; everything below
is intentionally deferred to the iteration that first *needs* it (infra included).

- [x] **Iteration 2 — Ingestion:** Redis Streams alert queue + a replayable synthetic
      incident generator, consumed with at-least-once delivery, bounded retries, and a
      dead-letter stream.
- [x] **Iteration 3 — Runbook RAG:** Postgres + pgvector retrieval grounds hypotheses in
      curated runbooks (cosine + HNSW, Gemini embeddings), behind a retriever protocol
      that degrades gracefully when no DB is configured.
- [x] **Iteration 4 — Durable HITL:** an incident plans a remediation, pauses at a
      LangGraph `interrupt()` for human approval (state persisted by a checkpointer), and
      resumes on decision; execution is behind a protocol with a simulated executor.
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
  contracts.py      # Alert, RootCauseHypothesis, RemediationAction, IncidentView, ...
  llm.py            # get_chat_model() provider seam (Gemini)
  observability.py  # Langfuse v3 client + LangChain callback handler
  checkpointer.py   # build_checkpointer(): PostgresSaver / MemorySaver
  service.py        # InvestigationService: run graph, package result
  incident.py       # IncidentService: durable start / decide / get over a thread_id
  agent/
    state.py        # typed graph state
    graph.py        # the LangGraph investigation graph (retrieve → triage)
    incident_graph.py  # incident graph (… → plan → approve[interrupt] → execute)
  remediation/
    executor.py     # RemediationExecutor protocol + SimulatedExecutor (safe, dry-run)
  rag/
    runbooks.py     # RunbookMatch, RunbookRetriever protocol, NullRetriever
    embeddings.py   # get_embeddings() provider seam (Gemini)
    store.py        # pgvector store + retriever (hand-rolled SQL) + build_retriever
    ingest.py       # embed the runbooks/ corpus into pgvector (CLI)
  ingestion/
    streams.py      # AlertStream: Redis Streams + consumer-group wrapper
    worker.py       # IngestionWorker: at-least-once consume, retry, dead-letter
    generator.py    # synthetic-incident generator CLI
  api/
    middleware.py   # CorrelationIdMiddleware
    app.py          # FastAPI factory, lifespan, endpoints
runbooks/           # curated markdown runbooks (the RAG corpus)
tests/              # offline suite (fake model + fakeredis + fake retriever), keyless
```

---

## Known limitations

- **Simulated execution only.** Remediations are never actually run; a real
  `RemediationExecutor` is deliberately out of scope.
