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

## Milestone 3b — PostgreSQL accounting and the single-scope business graph

Implemented against v1.2:

- Async PostgreSQL ledger: study-then-run locking, exact microdollars, request caps,
  durable reservations, unknown usage, corrections, late settlement and snapshot-consistent summaries.
- Physical provider attempts use this ledger. Stable operation keys reuse saved
  responses. An interrupted operation without a response pauses instead of silently
  resending; node/domain receipts survive the domain-commit/checkpoint gap.
- Single-scope LangGraph: research → independent criterion applicability → critic →
  neutral handoff gate (one repair) → verifier → reconciliation → factors → draft card.
  Fail/unknown bypass critic and scoring. Any critic claim affecting a mandatory
  criterion conservatively requests reassessment; it is not silently kept as pass.
- Every role uses fresh explicit input, configurable reasoning/output settings, a
  version hash and private input audit. Scope/thread/input hashes reject cross-scope
  checkpoint access and changed input on resume. Verifier sees no critic impact/order.
- A private English HTML report shows criteria, risk verification, claim quotations,
  source links, provisional estimates and accounting. It is not a public publication.
- Explicit CLI `python -m ai_builder.run_scope` accepts a prepared, hash-checked source
  packet; `DATABASE_URL` and `OPENAI_API_KEY` are environment-only. Migrations remain
  explicit via `python -m ai_builder.pg_migrate`. The CLI keeps a lease heartbeat,
  persists active time, applies an overall timeout and prints the run ID for diagnosis.

Example (spends real API budget; not executed during development):

```sh
python -m ai_builder.run_scope --scope private/scope.json \
  --pricing private/verified-openai-pricing.json \
  --settings config/examples/role-settings.json --study-budget 25 \
  --output outputs/private-draft.html
```

`role-settings.json` is an initial example, not an evaluated quality recommendation.
A tariff file must be independently verified; no fabricated production price is shipped.
A scope packet has scope/base/variant IDs, neutral description and documents with
snapshot/evidence ID, URL, text, SHA-256 and validated policy status. This is a trusted
internal input, not a public upload API. This slice does not claim it fetched sources.
`--resume-run ID` reloads frozen inputs and continues an interrupted active run. Paused
runs require owner resolution, which is not implemented by this CLI. Unknown reservations
are not erased. Interrupted clock intervals are charged conservatively until recovery;
this may pause earlier after downtime rather than reset limits.

Remaining before live/public acceptance: controlled source acquisition/discovery,
role search/tool loops and independent counter-source retrieval, full semantic claim/card
validation, durable owner manual-review/resume flow, coordination of the complete batch,
public projection/auth/UI and deployment. Cards have explicit publication blockers.
The existing SQLite Store remains only in the legacy health application/tests; the new
scope CLI does not use its stage scheduler. SQLite import (A57) and retirement of that
legacy API path still remain. R18/R19/full A56–A62 are not all declared complete.

Validation: 81 tests including real PostgreSQL concurrency, state restoration and a
15-call mocked HTTP path through the actual model adapter/ledger; no external API calls.
Fixture tariff totals in these tests are synthetic and are not actual provider bills.

## Milestone 4 — private technical deployment preparation

Web now uses PostgreSQL exclusively. Startup runs no migrations. Readiness checks
application migration checksums, pinned checkpointer schema versions and storage.
The opt-in `/preview` serves only the fixed synthetic smoke report verified by its
manifest/hash; it does not expose arbitrary files or draft business results.
`python -m ai_builder.smoke` runs the actual graph/adapter/ledger using synthetic
HTTP responses and no provider credentials. Re-running reads the completed graph
without new physical fixture requests. It writes private evidence artifacts too.

Compose isolates the network and binds web to server loopback only. Separate
migration/runtime DB roles, explicit bootstrap, CI image smoke/restore checks,
and manual digest-based deployment are described in docs/deployment.md. This
profile has no persistent live worker and does not permit live API traffic.

GitHub CI passed 86 tests, the installed Linux image, restricted runtime permissions,
PostgreSQL/web restart with no repeated fixture calls, and isolated restore. Health
also accepts the unversioned `/health/live` and `/health/ready` routes from the deploy plan.
The encrypted export uses the owner's age public recipient; Actions transports only
ciphertext, with the private decryption key retained on the Mac. Remote release and
Mac restore verification are tracked separately from these CI results.

### First Aeza technical release — 15 November 2026

PR #1 was merged after successful CI. Manual [release 43753431745](https://github.com/bcmle/test_ai_builder/actions/runs/43753431745)
deployed commit `cf5c3261a59dfcff5c9d9cf6c5fb23896f33a40a` with image digest
`sha256:5d8792d3abcc40c32e7e623da558051401ce2d01b9beeb9b9fd7dfed347ffae0`.
The exact image passed 86 tests and Linux Compose checks. Aeza then passed explicit
migrations, synthetic graph, PostgreSQL/web restart, zero-call replay and isolated restore.
Release manifest records all three application migration checksums.

Readiness reports PostgreSQL/schema version 3. Server inspection confirms only
`127.0.0.1:8080` is published; PostgreSQL has no host port. `/preview` matches its
stored SHA-256. No external API calls occurred; synthetic accounting is not billing.
Provider secret file permissions remain root:root 0600; their values were not read.

A verified backup was encrypted for the owner's age public recipient and transported
through an Actions artifact. On the Mac the archive was downloaded, decrypted and all
snapshot/evidence checksums passed. Private decryption material remains on the Mac,
outside the Git repository. The temporary local test PostgreSQL was stopped.

The user explicitly deferred the Mac SSH-tunnel viewing check. It remains outstanding,
not a passed acceptance test. Live discovery/worker, evidence/manual-review completion,
live assessments and public product access remain subsequent stages. This release does
not claim complete A59/A60 or product acceptance.
