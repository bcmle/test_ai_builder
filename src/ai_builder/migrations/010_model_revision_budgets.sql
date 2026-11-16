-- Additive accounting links only: no frozen inputs, usage or receipts change.
CREATE TABLE app.model_budget_families (
 study_id TEXT NOT NULL REFERENCES app.studies(id),
 model_id TEXT NOT NULL,
 limit_micro BIGINT NOT NULL CHECK(limit_micro > 0 AND limit_micro <= 25000000),
 ancestor_run_ids JSONB NOT NULL CHECK(jsonb_typeof(ancestor_run_ids)='array'),
 ancestry_hash TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(study_id,model_id)
);
CREATE TABLE app.model_budget_members (
 run_id TEXT PRIMARY KEY REFERENCES app.runs(id),
 study_id TEXT NOT NULL,
 model_id TEXT NOT NULL,
 parent_run_id TEXT,
 run_limit_micro BIGINT NOT NULL CHECK(run_limit_micro > 0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(run_id,study_id,model_id),
 FOREIGN KEY(study_id,model_id) REFERENCES app.model_budget_families(study_id,model_id),
 FOREIGN KEY(parent_run_id,study_id,model_id)
   REFERENCES app.model_budget_members(run_id,study_id,model_id) DEFERRABLE INITIALLY DEFERRED,
 CHECK(parent_run_id IS DISTINCT FROM run_id)
);
CREATE INDEX model_budget_member_family ON app.model_budget_members(study_id,model_id);
CREATE UNIQUE INDEX model_budget_one_root ON app.model_budget_members(study_id,model_id)
 WHERE parent_run_id IS NULL;

CREATE FUNCTION app.protect_model_budget_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Model budget ancestry and limits are immutable';
END;
$$;
CREATE TRIGGER model_budget_family_immutable BEFORE UPDATE OR DELETE ON app.model_budget_families
 FOR EACH ROW EXECUTE FUNCTION app.protect_model_budget_identity();
CREATE TRIGGER model_budget_member_immutable BEFORE UPDATE OR DELETE ON app.model_budget_members
 FOR EACH ROW EXECUTE FUNCTION app.protect_model_budget_identity();

CREATE FUNCTION app.check_model_budget_run() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE original app.runs%ROWTYPE;
BEGIN
 SELECT * INTO original FROM app.runs WHERE id=NEW.run_id;
 IF original.study_id IS DISTINCT FROM NEW.study_id OR original.limit_micro IS DISTINCT FROM NEW.run_limit_micro THEN
   RAISE EXCEPTION 'Model budget member must preserve its run study and monetary limit';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER model_budget_run_checked BEFORE INSERT ON app.model_budget_members
 FOR EACH ROW EXECUTE FUNCTION app.check_model_budget_run();
