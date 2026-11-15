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
