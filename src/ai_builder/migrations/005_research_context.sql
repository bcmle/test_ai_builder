-- Freeze all acquisition/model inputs before the first external request.
-- Existing app.runs configuration and scope_inputs contracts remain unchanged.
CREATE TABLE app.research_contexts (
 run_id TEXT PRIMARY KEY REFERENCES app.runs(id),
 context_hash TEXT NOT NULL,
 context JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE FUNCTION app.reject_research_context_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Research context is immutable; create a new run';
END;
$$;
CREATE TRIGGER research_context_immutable
BEFORE UPDATE OR DELETE ON app.research_contexts
FOR EACH ROW EXECUTE FUNCTION app.reject_research_context_change();
