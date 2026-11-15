ALTER TABLE app.runs ADD COLUMN active_elapsed_ms BIGINT NOT NULL DEFAULT 0 CHECK(active_elapsed_ms>=0);
ALTER TABLE app.runs ADD COLUMN active_started_at TIMESTAMPTZ;
CREATE TABLE app.scope_inputs (
 run_id TEXT PRIMARY KEY REFERENCES app.runs(id),
 scope JSONB NOT NULL, role_settings JSONB NOT NULL, pricing JSONB NOT NULL,
 purpose TEXT NOT NULL CHECK(purpose IN ('development','eval','live'))
);
