\set ON_ERROR_STOP on
REVOKE INSERT, UPDATE, DELETE ON app.schema_migrations FROM ai_runtime;
REVOKE INSERT, UPDATE, DELETE ON checkpoints.checkpoint_migrations FROM ai_runtime;
DO $$ BEGIN
  IF has_database_privilege('ai_runtime','ai_builder','CREATE')
    OR has_schema_privilege('ai_runtime','app','CREATE')
    OR has_schema_privilege('ai_runtime','checkpoints','CREATE') THEN
    RAISE EXCEPTION 'Runtime role must not have DDL privileges';
  END IF;
END $$;
