# AI Builder

Evidence-based business model research, built from the approved [specification v1.3](specs/README.md).

The prototype presents a fixed collection of **20 business models** for a US launch:
outcomes and reasons, comparable cards, evidence passports, comparison of up to three,
and an ascending ranking of validated assessments. Twenty is the input size, not a
guarantee of twenty passing recommendations. The interface is in English.

## Implementation status

Implemented: strict analytical contracts, versioned filters and interval scoring,
PostgreSQL budget reservations and usage ledger, fenced LangGraph checkpoints,
isolated Critic/Verifier inputs, metered provider adapters, immutable evidence files,
and a single-model graph that produces a private draft report. A bounded research CLI
now connects Tavily Search, controlled HTML/PDF acquisition and that graph; inputs,
source snapshots and provider receipts persist in PostgreSQL/private storage.

The private technical deployment includes PostgreSQL, explicit migrations, web readiness,
and a synthetic smoke run through the real graph. `/preview` labels the result as synthetic.
GitHub Actions tests the Linux image; a separate manual release delivers its immutable
GHCR digest to Aeza. Ordinary pushes do not deploy. See [deployment instructions](docs/deployment.md).

The public site reads a separate, validated snapshot. A manual sequential collection
runner discovers candidates through Tavily, freezes membership before filtering and
persists each result. The English website includes overview, details, comparison and
methodology. Final scores require independent checks of their factual premises.

**This is a prototype.** Market-wide cataloguing, a permanent worker, manual-review
editor and owner dashboard are deferred. No fixture result counts as a live assessment.
[Implementation progress](docs/implementation.md) records verification and limitations.

For the explicit one-model research command, frozen plans, replay and acquisition
limits, see [controlled source research](docs/source-research.md). Production document
parsing requires Linux resource limits; local parser fixtures on macOS are not a
substitute for the Linux CI checks. The technical deployment still has no provider keys.

## Local development

Python 3.13 and PostgreSQL 17 are the tested versions:

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m pytest -q
```

Set `TEST_DATABASE_URL` to a dedicated disposable PostgreSQL database to include the
integration tests. They rebuild their test schemas. Tests use synthetic data and no provider keys.

For web, configure `DATABASE_URL` with a runtime PostgreSQL role and run migrations
explicitly with a separate migrator role; web startup performs no DDL. See the technical
Compose profile for provisioning. Then run:

```sh
.venv/bin/uvicorn ai_builder.api:app --host 127.0.0.1 --port 8000
```

`/api/v1/health/live` checks the process. `/api/v1/health/ready` checks PostgreSQL,
migration checksums/versions and writable artifact storage. Without a configured database
readiness returns 503. `AI_BUILDER_DATA` selects private file storage, and
`AI_BUILDER_CONFIG` selects the versioned configuration (default `config/baseline.json`).
`AI_BUILDER_TECHNICAL_PREVIEW=1` enables the single validated synthetic preview only.

`requirements.lock` pins runtime and test dependencies. Legacy SQLite modules remain for
existing core tests and migration work; deployed web, accounting and checkpoints use PostgreSQL.

## Budgets and data

The 20-model prototype has **one $100 collection cap**, including discovery and every
child analysis. The older single-model command defaults to $25/run. Every physical
request must reserve its maximum expected cost before transmission. Unknown billing continues
consuming its reservation until reconciled. Repeated transport attempts receive new IDs;
replaying a reservation does **not** authorize another external request.

Costs persist as integer microdollars. Calculated cost, cash billed, reserved cost and unknown
cost remain distinct, with tariff versions and corrections. Cached/reasoning tokens are
subcategories, not additional billable totals. Technical smoke reports synthetic tariff
accounting and zero external API calls; these are not actual charges.

## Delivery

Source: [bcmle/test_ai_builder](https://github.com/bcmle/test_ai_builder).
Aeza runs Ubuntu 24.04 on x86_64. OpenAI Astra and Tavily are external APIs; research
runs on Aeza, not the owner's Mac. The prototype exposes read-only HTTP on the server
IP through Caddy. Private health/synthetic preview remain on `127.0.0.1:8080`.
Domain/HTTPS and owner access control are deferred; no paid action is available online.

Credentials, runtime databases, private evidence and local `full_tt.md` are excluded from
Git and the Docker context. Provider keys are not passed to technical smoke or CI.

The earlier SSH-tunnel viewing check was deferred. The read-only prototype has its
own public-IP acceptance in specs/11-acceptance.md; it does not claim completion of
the extended full-product acceptance matrix. See [five decisions](DECISIONS.md) and
[honest limitations](LIMITATIONS.md).

## Run a collection on Aeza

Run inside the deployed application container with the existing server-only provider
environment and runtime `DATABASE_URL`. No keys belong in commands, Git or Actions.

```sh
python -m ai_builder.collection \
  --config /app/config/examples/prototype-config.json \
  --pricing /app/config/examples/openai-pricing-2026-11-15.json \
  --tavily-pricing /app/config/examples/tavily-pricing-2026-11-15.json \
  --settings /app/config/examples/role-settings.json \
  --study-budget 100 --purpose live
```

The command prints the collection ID. Resume the saved collection with
`python -m ai_builder.collection --resume-collection COLLECTION_ID`; completed
results are reused. It never silently increases the shared budget or changes the
analytical scenario. Ambiguous in-flight requests require operator review.

The latest saved public result appears at `/`; specific collections at `/runs/{id}`.
Cards and comparison stay within that collection and scenario. A failed mandatory
criterion or unresolved evidence does not produce a final score. Public pages cannot
start a run. Deployment remains an explicit full-SHA GitHub Actions release.
