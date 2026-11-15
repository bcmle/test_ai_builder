# AI Builder

Evidence-based business model research, built from the approved [specification](specs/README.md).

## Implementation status

Baseline v1.0 is approved. The first implementation milestone provides:

- strict analytical contracts and versioned configuration;
- mandatory-filter state aggregation and conservative interval scoring;
- goal-feasibility groups so an impossible 90-day plan cannot win on unrelated factors;
- whitelist handoff with dependency/snapshot validation and risk reconciliation;
- SQLite WAL storage, atomic run/study budget reservations, usage corrections and fencing tokens;
- immutable content-addressed artifacts and integrity-checked manifests;
- FastAPI liveness/readiness endpoints which do not call paid services.

**This is not yet the complete research application.** Live provider adapters, the orchestration worker,
catalogue, publication/review flows, owner authentication, English product UI, usage dashboard and
deployment pipeline are subsequent implementation milestones. No real research has been performed.
See [implementation progress](docs/implementation.md) for acceptance coverage and limitations.

## Local development

Python 3.13 is the tested interpreter. Create an isolated environment from the repository root:

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m pytest -q
.venv/bin/uvicorn ai_builder.api:app --host 127.0.0.1 --port 8000
```

Health endpoints: `/api/v1/health/live` and `/api/v1/health/ready`.
The database is created under `data/`; override with `AI_BUILDER_DATA`.
Configuration is read from `config/baseline.json`, or `AI_BUILDER_CONFIG`.
All mandatory filters and scoring rubrics are versioned in configuration, not in model prompts.
Run creation snapshots the configuration and its hash. Current rubrics are written in Russian as
the original specification; generated product text must be English.

`requirements.lock` pins the tested runtime and test dependency set. No provider key is needed for tests.
Test data is synthetic and does not count as live business evidence or an LLM quality evaluation.

## Budgets and data

API consumption is limited to **$25/run**, with a separate explicit study cap. Every physical request
must reserve its maximum expected cost before transmission. Unknown billing continues consuming its
reservation until reconciled. Repeated transport attempts receive new IDs; replaying the same reservation
does **not** authorize another external request. Free provider credits do not erase usage cost.

Costs are persisted as integer microdollars after conservative Decimal conversion. Calculated cost,
cash billed, reserved cost and unknown cost remain distinct. Tariff versions and corrections are retained.
Cached/reasoning tokens are subcategories of input/output, not additional billable token totals.

## Delivery

Source repository: [bcmle/test_ai_builder](https://github.com/bcmle/test_ai_builder).
The target is Aeza Ubuntu 24.04, with OpenAI Astra and Tavily called as external APIs.
The Dockerfile currently packages the foundation API; the production Compose/Caddy/worker and
GitHub Actions release workflow will be added with the corresponding implementation stages.
Docker execution has not yet been verified on this development machine.

Credentials, runtime databases, private evidence and the local `full_tt.md` discussion are excluded
from Git and the Docker build context. No API keys are embedded in source or build arguments.
