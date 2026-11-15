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

## Milestone 5 — one-model source acquisition

Tavily discovery now feeds controlled HTTP reads, Trafilatura/pypdf extraction,
immutable source artifacts and the existing single-model graph. PostgreSQL keeps
search order, dispositions, read reservations, snapshots and frozen run inputs.
Search snippets never enter analytical evidence. Interrupted requests retain their
receipts/reservations; completed reports replay without new provider requests.

The user prioritised one visible live card before catalogue, permanent worker and
complete owner/public UI work. The initial scheduling-software scope is a test of
the working path, not a preselected passing investment recommendation. Failed or
unknown criteria remain visible and do not receive a final ranking.

See docs/source-research.md for the command and boundaries. Provider prices used by
the initial run are saved as dated configuration; no further tariff research is
required by the user. Astra requests select Standard processing and explicit caching
without breakpoints, so automatic cache writes are outside this first execution.
The manual research-result workflow only downloads an already encrypted HTML report;
it does not run analysis or expose source files or credentials.

### First real research result

PR #2 merged as `aba30f4825cf3618fa705906d0367996c1b14de4`; manual release
[43757515178](https://github.com/bcmle/test_ai_builder/actions/runs/43757515178)
deployed image `sha256:8e970eb855c3061aca60e6f1a731f5dbb83b1050364cafb38d972add52ba89f2`.
Linux verification passed 226 tests. Application schema is now version 5.

A real one-off invocation ran on Aeza with server-only Astra/Tavily credentials.
Run `b6bd4e20ca9349e1922da575c68d1944` retrieved five source documents without
acquisition gaps and produced an English private HTML assessment of appointment
scheduling software for small US consulting teams. Six mandatory criteria passed;
F01, F03, F05 and F07 remain unknown (jurisdiction/legal basis, licensing, payment
obligations and custody). Eligibility is `needs_review`, with no final score.
Critic and Verifier correctly did not run because the mandatory gate did not pass.

Calculated API usage: **$1.1248**; reserved/unknown: **$0**; remaining configured
budget: **$23.8752**. These are tariff calculations, not a reconciled invoice.
The HTML was encrypted on Aeza, downloaded via
[43758889049](https://github.com/bcmle/test_ai_builder/actions/runs/43758889049),
decrypted on the owner's Mac and verified against the server SHA-256:
`c5de52993ce0d166aae9e1a09bfd74ddf06e1025463c9bfa06af6b67ea1ebfeb`.

This demonstrates real search, document acquisition, analytical criteria and a
visible saved result. It does not establish full evidence acceptance, a live
Critic/Verifier assessment, catalogue completion or public-site readiness.

## Milestone 6 — v1.3 prototype implementation (2026-11-16)

Specifications were committed before code: 19812ff, then b3dec07 for the batched
independent applicability check. The new collection runner discovers and freezes
20 source-backed base-model hypotheses, uses one shared $100 study ledger, processes
models sequentially and writes sanitized snapshots after each result. PostgreSQL
migration 006 stores collection context, membership and child identities.

The graph independently checks criterion applicability in one batch, then Critic,
neutral handoff and Verifier. Factor/test-plan premises receive a separate isolated
verification before final publication. Unknown/failed mandatory criteria still
block rating. Confidence/method fields are explicit and remain absent for older
records that did not supply them; exporting a legacy run does not rescore it.

The English read-only UI has a collection overview with outcome counts, detailed
cards, source/claim disclosures, comparison of up to three and methodology.
Local visual checks used the real saved Calendly report: desktop/mobile layout,
criterion expansion and source links work, without horizontal mobile overflow.
Empty analytical sections are hidden when the corresponding stages never ran.

Local Python/PostgreSQL verification: **244 passed, 2 skipped**. Linux CI passed
all **246 tests**, including parser isolation and runtime privileges, plus container
restart/restore, public HTTP routing and encrypted export. No paid APIs were called
in tests. The official Caddy binary requires NET_BIND_SERVICE in its capability
bounding set; the proxy otherwise retains its non-root, read-only configuration.

PR #3 merged as `dfafdfc13c7416c47dae5b3d4f93f1f49885007d` after successful
[CI](https://github.com/bcmle/test_ai_builder/actions/runs/43763558733).
The explicit [release](https://github.com/bcmle/test_ai_builder/actions/runs/43763872706)
successfully deployed that commit as
`sha256:75aedfdcd6fb0f9fc1dc238d820b59ef21bc4d34d74b82aa7e142e02ac7707cf`.
Database restore verification and encrypted off-server export passed. The existing
Calendly assessment was published without new API calls. The public HTTP home
returns 200 on Aeza; a Mac connection timed out, consistent with the earlier
unresolved route issue. The manually dispatched `public-result.yml` fetches only
public HTML/JSON from a GitHub runner to verify external access and retrieve the
saved result; it cannot launch research or read credentials/private files.

### First 20-model collection

Collection `1d76fe99e56240989cd67794b7143dce` completed its first processing pass
on Aeza with 20 frozen, distinct base-model hypotheses and no backfilled replacements.
Twelve assessments produced `needs_review` cards; eight stopped at technical
validation. All twenty attempts are visible, with the eight unfinished assessments
counted separately. The collection remains `partial`, not business-ready.

No model reached final eligibility: F01 (lawful activity) and F03 (own licensing)
remain unresolved for all twelve completed assessments; other evidence gaps vary.
Critic, Verifier and final scoring therefore did not run for this live collection.
Their full path and isolation passed synthetic tests only. No synthetic score is
substituted into the live result.

For the interrupted Customer Support Helpdesk assessment, the saved research
response contains a reference outside its frozen document set. It is not an
alternative snapshot ID of the same source. The validator correctly rejects it;
the original response and paid receipt remain preserved. No fuzzy source matching,
automatic pass or paid restart was used to hide this failure.

The PostgreSQL ledger records approximately **$6.68 of $100** after the first pass:
92 Tavily requests and 23 OpenAI requests, all settled, with no reserved/unknown
usage. Detailed tokens and credits remain in PostgreSQL. These are stored-tariff
calculations, not reconciled invoice charges. Six assessments stopped at semantic
validation and two at structured-output validation.

The [external HTTP check](https://github.com/bcmle/test_ai_builder/actions/runs/43765904555)
downloaded the actual public HTML and JSON successfully. A Mac browser still gets
`ERR_CONNECTION_TIMED_OUT`; this is distinct from the verified external HTTP
response. A local read-only UI mirror uses the downloaded public snapshot to allow
visual review without rerunning research on the Mac. Three-column comparison and
source disclosures were checked using real saved data. Desktop/mobile checks and
all 246 Linux tests passed; this is a prototype delivery with explicit research
limitations, not acceptance of twenty investment recommendations.

## Milestone 7 — domain HTTPS (v1.3.1)

The prototype is available at **https://ai-builder-lab.com/**. Aeza DNS points the
apex A record to 77.232.142.154 with TTL 300. Caddy obtains and renews certificates;
private persistent TLS directories survive container replacement. The original
read-only IP HTTP fallback remains available. No research or model data changed.

Specifications were committed first as `824dc3c`; implementation `8376f2b` merged
in [PR #4](https://github.com/bcmle/test_ai_builder/pull/4) as
`3b7662f0569ae6063727b127e73dc15323845fc2`. The reviewed host bundle was installed
before [release 43769174250](https://github.com/bcmle/test_ai_builder/actions/runs/43769174250),
which deployed image `sha256:a712097ec25f7fabc3ab5f877898a6cd28af476a350b68fbe77622cdc1344f05`.

Validation on 16 November 2026: 246 tests passed; trusted domain TLS, HTTP 308,
private-route and POST denial, and the same certificate after Caddy recreation
passed. A request to `/methodology?source=ceo` redirected to the identical path/query
on HTTPS. The release completed its existing backup/restore and encrypted export.
[External check 43769756619](https://github.com/bcmle/test_ai_builder/actions/runs/43769756619)
fetched the homepage and saved collection over HTTPS. The live homepage also opened
successfully in the Mac browser. P08–P10 are satisfied; the earlier research limitations
and empty final ranking remain unchanged.


## UI redesign — 16.11.2026, отдельная ветка codex/ui-redesign

После утверждения и push [визуальной спецификации](../specs/14-ui-redesign.md) реализован центральный hero, локальный Onest (OFL), чередование секций, единая панель шагов, адаптивная навигация и оформление публичных аналитических страниц. Функциональные контракты и focused analysis сохранены; бизнес-логика и deploy не изменены. [Протокол локальной проверки и скриншоты](ui-redesign/verification.md): 13 tests passed, 2 PostgreSQL tests skipped; основные браузерные сценарии проверены. Это результат в UI-ветке, не утверждение о выпуске на production или полной приёмке всех accessibility-сценариев.
