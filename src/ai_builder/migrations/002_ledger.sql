CREATE TABLE app.pricing (
 id TEXT PRIMARY KEY, body JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE app.usage (
 request_id TEXT PRIMARY KEY, logical_request_id TEXT NOT NULL,
 run_id TEXT NOT NULL REFERENCES app.runs(id), provider TEXT NOT NULL, model TEXT,
 endpoint TEXT NOT NULL, agent TEXT NOT NULL, purpose TEXT NOT NULL,
 pricing_id TEXT NOT NULL REFERENCES app.pricing(id), upper_micro BIGINT NOT NULL CHECK(upper_micro>=0),
 state TEXT NOT NULL CHECK(state IN ('reserved','unknown','settled','released')),
 calculated_micro BIGINT CHECK(calculated_micro>=0), billed_micro BIGINT CHECK(billed_micro>=0),
 metrics JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 CHECK ((state='settled') = (calculated_micro IS NOT NULL))
);
CREATE INDEX usage_run ON app.usage(run_id);
CREATE TABLE app.events (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 run_id TEXT NOT NULL REFERENCES app.runs(id), kind TEXT NOT NULL,
 payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE app.provider_operations (
 run_id TEXT NOT NULL REFERENCES app.runs(id), operation_key TEXT NOT NULL,
 input_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('running','completed')),
 response JSONB, PRIMARY KEY(run_id,operation_key)
);
CREATE TABLE app.graph_threads (
 thread_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES app.runs(id),
 scope_id TEXT NOT NULL, graph_version TEXT NOT NULL, input_hash TEXT NOT NULL,
 UNIQUE(run_id,scope_id)
);
