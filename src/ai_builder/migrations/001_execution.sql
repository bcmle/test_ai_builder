CREATE TABLE app.studies (
 id TEXT PRIMARY KEY, market TEXT NOT NULL,
 limit_micro BIGINT NOT NULL CHECK (limit_micro > 0)
);
CREATE TABLE app.runs (
 id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES app.studies(id),
 config_json JSONB NOT NULL, config_hash TEXT NOT NULL,
 limit_micro BIGINT NOT NULL CHECK (limit_micro > 0),
 status TEXT NOT NULL, control_version BIGINT NOT NULL DEFAULT 1,
 graph_version TEXT NOT NULL, state_schema_version TEXT NOT NULL,
 coordinator_thread_id TEXT NOT NULL UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE app.run_leases (
 run_id TEXT PRIMARY KEY REFERENCES app.runs(id), token TEXT NOT NULL,
 owner_id TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE app.node_receipts (
 run_id TEXT NOT NULL REFERENCES app.runs(id), invocation_key TEXT NOT NULL,
 scope_id TEXT NOT NULL, node_name TEXT NOT NULL, input_hash TEXT NOT NULL,
 result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (run_id, invocation_key)
);
CREATE TABLE app.run_commands (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES app.runs(id),
 kind TEXT NOT NULL CHECK (kind IN ('start','resume','pause')),
 expected_control_version BIGINT NOT NULL, idempotency_key TEXT NOT NULL,
 payload JSONB NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
 CHECK (state IN ('pending','applied','rejected')),
 UNIQUE (run_id, idempotency_key)
);
