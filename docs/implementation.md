# Implementation progress

## Milestone 1 — deterministic core and execution storage

Implemented against baseline v1.0 (15 November 2026). Unit/integration tests use synthetic fixtures
and temporary SQLite databases. They make no paid network calls.

| Requirement / acceptance | Implemented evidence | Remaining work |
| --- | --- | --- |
| R02, A03/A36 | Strict criterion states; missing evaluations never become business unknown | Source applicability validator and all live filters |
| R06, A10/A11/A13/A43 | Config validation; deterministic score; conservative intervals; feasibility groups | Factor extraction and source-backed numerical estimates |
| R03, A39 | Handoff whitelist, transitive inputs, snapshot allowlist, reconciliation | Semantic neutrality gate, actual isolated LLM requests and audit UI |
| R07/R11, A16/A40/A45 | Atomic budget and request limits, unknown reservations, restart persistence, stale lease rejection | Active-time deadline, circuit breaker, worker retry loop, pause/resume endpoints |
| R15, A46 | Versioned tariff calculations; explicit unknown/billed distinctions; idempotent settlement | Provider usage adapters, full export/dashboard and A47 |
| R04, A41/A42 | Immutable files, manifest hashes, excerpt presence check | Full entity schemas, snapshot metadata, public revisions and backup/restore workflow |
| R08 | FastAPI health endpoints; Dockerfile | Owner auth, public UI, deployment, TLS and live acceptance |

The implementation is intentionally not marked as passing the full A01–A47 acceptance suite.
The remaining application and live acceptance must be completed before deployment is called ready.

## Clarifications found during implementation

The approved architecture still contained older references to Claude, an unselected VPS, and Russian
prompt output. These were editorial remnants: the approved baseline selects Astra, Aeza and English.
Those passages have been corrected without changing the approved product decisions.

For the first source adapter, one extraction request must cover one document so the existing document
counter cannot be bypassed by an uncounted batch. Batch extraction will require counting every URL.

## Development environment

Python 3.13; fully pinned dependencies in requirements.lock. TestClient currently emits an upstream
Starlette warning that its HTTPX integration is deprecated. It still passes the health test; upgrading
the testing transport is independent of the runtime API. Docker is not installed on the local host,
so image building and Linux/container smoke tests remain unverified.

## Milestone 2 — metered provider transports

Added OpenAI Responses structured-output and Tavily Search/Extract adapters. Each physical attempt
has a durable budget reservation and usage record; retries share only logical_request_id, not billing
identity. Authentication/quota failures pause the run; missing usage/uncertain transport costs remain
reserved. Requests have bounded output, timeouts and at most two transport retries. OpenAI requests
use fresh input with no shared conversation/previous_response_id. Strict JSON output, refusals and
incomplete generations are distinguished from successful analysis. Tests use HTTPX MockTransport.

These adapters have NOT yet been exercised against the user's live keys. The worker must supply the
run's remaining active-time deadline and enforce lease ownership before invoking an adapter. Global
circuit-breaker state, schema repair, role tool loops, prompt audit and all business workflows remain
pending. URL checks reject private initial/returned extraction URLs. Tavily's internal redirect path
is not exposed, so this does not claim end-to-end redirect/robots enforcement or fulfill full A18.

## Milestone 3a — PostgreSQL migrations and fenced LangGraph persistence

Baseline v1.2 (R18, partial A56/A57). Explicit `python -m ai_builder.pg_migrate`
uses `DATABASE_URL` with migrator credentials. Versioned packaged SQL migrations
verify checksums under an advisory lock; LangGraph setup runs separately in its
own schema. Web/worker startup does not run these migrations.

Async psycopg pools, run lease acquire/renew, and a run-bound AsyncPostgresSaver
adapter now keep the lease row lock and saver writes in the same transaction.
The adapter overrides the pinned saver's `_cursor` integration point. Upgrades
must rerun these contract tests. It does not use pipeline mode. LangSmith tracing
is not configured or required. Tests run a real StateGraph interrupt/resume with
new connections, reject stale checkpoint/pending writes, and verify transaction
rollback and serialization with a competing ownership change.

Run the complete suite against a **dedicated test database**:

```sh
TEST_DATABASE_URL='postgresql://localhost/ai_builder_test' python -m pytest -q
```

Without TEST_DATABASE_URL the three PostgreSQL tests are explicitly skipped.
No production database or paid API is used. Runtime credentials must not be
migration credentials; deployment role provisioning is still pending.

This is an execution-storage foundation, NOT a completed PostgreSQL migration:
the existing Store/provider budget adapter still uses SQLite; usage migration,
PostgreSQL budget reservations, receipt operations, thread ownership registry,
RunCommand delivery, the business graph and production wiring remain pending.
The new saver is not exposed through web routes. No two schedulers are deployed.
Linux Docker build, SQLite data import, target-server measurements and full
A56/A57 acceptance remain unverified. The next step is PostgreSQL budget/store
integration before any live workflow is enabled.

Validation on macOS/Python 3.13 with temporary PostgreSQL 17.12: **63 tests passed**
(including all 3 PostgreSQL tests), `pip check` passed. The test database used a
local Unix socket only and was stopped after verification.
