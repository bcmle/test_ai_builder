-- A prototype collection shares one study limit across discovery and all children.
CREATE TABLE app.collections (
 id TEXT PRIMARY KEY,
 study_id TEXT NOT NULL UNIQUE REFERENCES app.studies(id),
 coordinator_run_id TEXT NOT NULL UNIQUE REFERENCES app.runs(id),
 context_hash TEXT NOT NULL, context JSONB NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('discovering','running','partial','completed')),
 membership_hash TEXT, selected_count INTEGER NOT NULL DEFAULT 0 CHECK(selected_count BETWEEN 0 AND 20),
 uncertainty JSONB NOT NULL DEFAULT '[]', stop_reason TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 frozen_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 CHECK((membership_hash IS NOT NULL) = (frozen_at IS NOT NULL))
);
CREATE UNIQUE INDEX one_active_collection ON app.collections ((true)) WHERE state IN ('discovering','running');
CREATE TABLE app.collection_items (
 collection_id TEXT NOT NULL REFERENCES app.collections(id),
 model_id TEXT NOT NULL UNIQUE, position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 20),
 candidate JSONB NOT NULL,
 run_id TEXT UNIQUE REFERENCES app.runs(id),
 state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed','technical_failed','incomplete')),
 public_model JSONB, error_code TEXT,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(collection_id,position)
);
CREATE FUNCTION app.protect_collection_context() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.context_hash IS DISTINCT FROM OLD.context_hash OR NEW.context IS DISTINCT FROM OLD.context
    OR NEW.study_id IS DISTINCT FROM OLD.study_id OR NEW.coordinator_run_id IS DISTINCT FROM OLD.coordinator_run_id
    OR (OLD.membership_hash IS NOT NULL AND
       (NEW.membership_hash IS DISTINCT FROM OLD.membership_hash OR NEW.selected_count IS DISTINCT FROM OLD.selected_count
        OR NEW.frozen_at IS DISTINCT FROM OLD.frozen_at)) THEN
   RAISE EXCEPTION 'Collection context and frozen membership are immutable';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER collection_context_immutable BEFORE UPDATE ON app.collections
FOR EACH ROW EXECUTE FUNCTION app.protect_collection_context();
CREATE FUNCTION app.protect_collection_item() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Frozen collection members cannot be deleted'; END IF;
 IF NEW.collection_id IS DISTINCT FROM OLD.collection_id OR NEW.model_id IS DISTINCT FROM OLD.model_id
    OR NEW.position IS DISTINCT FROM OLD.position OR NEW.candidate IS DISTINCT FROM OLD.candidate
    OR (OLD.run_id IS NOT NULL AND NEW.run_id IS DISTINCT FROM OLD.run_id) THEN
   RAISE EXCEPTION 'Frozen collection membership and child identity are immutable';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER collection_item_immutable BEFORE UPDATE OR DELETE ON app.collection_items
FOR EACH ROW EXECUTE FUNCTION app.protect_collection_item();
