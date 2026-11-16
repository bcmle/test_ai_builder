-- One owner-authorized additional allowance, shared by two standalone recovery runs.
-- Historical limits, external outcomes and paid receipts remain immutable.
CREATE TABLE app.recovery_allowances (
 id TEXT PRIMARY KEY,
 study_id TEXT NOT NULL UNIQUE REFERENCES app.studies(id),
 collection_id TEXT NOT NULL UNIQUE REFERENCES app.collections(id),
 limit_micro BIGINT NOT NULL CHECK(limit_micro > 0 AND limit_micro <= 10000000),
 model_ids JSONB NOT NULL CHECK(jsonb_typeof(model_ids)='array' AND jsonb_array_length(model_ids)=2),
 parent_runs JSONB NOT NULL CHECK(jsonb_typeof(parent_runs)='object'),
 extend_model_id TEXT,
 baseline_usage JSONB NOT NULL CHECK(jsonb_typeof(baseline_usage)='array'),
 baseline_accounted_micro BIGINT NOT NULL CHECK(baseline_accounted_micro>=0),
 "authorization" JSONB NOT NULL,
 input_hash TEXT NOT NULL,
 reservation_policy TEXT NOT NULL CHECK(reservation_policy='exact-input-plus4096-1'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(id,study_id)
);
CREATE TABLE app.recovery_runs (
 run_id TEXT PRIMARY KEY REFERENCES app.runs(id),
 allowance_id TEXT NOT NULL,
 study_id TEXT NOT NULL,
 model_id TEXT NOT NULL,
 parent_run_id TEXT NOT NULL REFERENCES app.runs(id),
 context_hash TEXT NOT NULL,
 config_hash TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(allowance_id,model_id),
 FOREIGN KEY(allowance_id,study_id) REFERENCES app.recovery_allowances(id,study_id),
 FOREIGN KEY(study_id,model_id) REFERENCES app.model_budget_families(study_id,model_id),
 CHECK(run_id<>parent_run_id)
);
CREATE TRIGGER recovery_allowance_immutable BEFORE UPDATE OR DELETE ON app.recovery_allowances
 FOR EACH ROW EXECUTE FUNCTION app.protect_model_budget_identity();
CREATE TRIGGER recovery_run_immutable BEFORE UPDATE OR DELETE ON app.recovery_runs
 FOR EACH ROW EXECUTE FUNCTION app.protect_model_budget_identity();
