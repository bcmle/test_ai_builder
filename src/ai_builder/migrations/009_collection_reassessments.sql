-- New policy revisions retain the original study budget and base model IDs.
-- Existing immutable rows and snapshots remain unchanged.
ALTER TABLE app.collections DROP CONSTRAINT collections_study_id_key;
CREATE INDEX collections_study ON app.collections(study_id);
ALTER TABLE app.collection_items DROP CONSTRAINT collection_items_model_id_key;
ALTER TABLE app.collection_items ADD CONSTRAINT collection_member_identity UNIQUE(collection_id,model_id);
