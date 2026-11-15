# AI Builder

Evidence-based business model research, built from the approved [specification v1.2](specs/README.md).

## Implementation status

Implemented: strict analytical contracts, versioned filters and interval scoring,
PostgreSQL budget reservations and usage ledger, fenced LangGraph checkpoints,
isolated Critic/Verifier inputs, metered provider adapters, immutable evidence files,
and a single-model graph that produces a private draft report from prepared documents.

The private technical deployment includes PostgreSQL, explicit migrations, web readiness,
and a synthetic smoke run through the real graph. `/preview` labels the result as synthetic.
GitHub Actions tests the Linux image; a separate manual release delivers its immutable
GHCR digest to Aeza. Ordinary pushes do not deploy. See [deployment instructions](docs/deployment.md).

**The research product is still incomplete.** Automated discovery/fetch, the persistent worker,
complete evidence/manual-review validation, owner authentication and the full English
interface remain in development. No fixture result counts as a live market assessment.
[Implementation progress](docs/implementation.md) records verification and limitations.

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

API consumption is limited to **$25/run**, with a separate explicit study cap. Every physical
request must reserve its maximum expected cost before transmission. Unknown billing continues
consuming its reservation until reconciled. Repeated transport attempts receive new IDs;
replaying a reservation does **not** authorize another external request.

Costs persist as integer microdollars. Calculated cost, cash billed, reserved cost and unknown
cost remain distinct, with tariff versions and corrections. Cached/reasoning tokens are
subcategories, not additional billable totals. Technical smoke reports synthetic tariff
accounting and zero external API calls; these are not actual charges.

## Delivery

Source: [bcmle/test_ai_builder](https://github.com/bcmle/test_ai_builder).
Aeza runs Ubuntu 24.04 on x86_64. OpenAI Astra and Tavily will be called as external APIs.
The technical web binds only to server `127.0.0.1:8080`, viewed through an SSH tunnel.
Domain/HTTPS, owner access control and scheduled external backups precede public release.

Credentials, runtime databases, private evidence and local `full_tt.md` are excluded from
Git and the Docker context. Provider keys are not passed to technical smoke or CI.
