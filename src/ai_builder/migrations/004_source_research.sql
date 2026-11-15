-- Source acquisition is separate from semantic Claim -> EvidenceLink review.
CREATE TABLE app.source_research (
 run_id TEXT NOT NULL REFERENCES app.runs(id), scope_id TEXT NOT NULL,
 plan_hash TEXT NOT NULL, plan JSONB NOT NULL,
 state TEXT NOT NULL DEFAULT 'preparing' CHECK(state IN ('preparing','frozen')),
 packet JSONB, packet_hash TEXT, gaps JSONB,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), frozen_at TIMESTAMPTZ,
 PRIMARY KEY(run_id,scope_id),
 CHECK((state='frozen') = (packet IS NOT NULL AND packet_hash IS NOT NULL AND gaps IS NOT NULL))
);
CREATE TABLE app.source_queries (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope_id TEXT NOT NULL,
 query_index INTEGER NOT NULL CHECK(query_index BETWEEN 0 AND 3),
 operation_key TEXT NOT NULL, query JSONB NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','succeeded','failed')),
 error TEXT, completed_at TIMESTAMPTZ,
 UNIQUE(run_id,scope_id,query_index), UNIQUE(run_id,operation_key),
 FOREIGN KEY(run_id,scope_id) REFERENCES app.source_research(run_id,scope_id)
);
CREATE TABLE app.sources (
 id TEXT PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE app.source_snapshots (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES app.sources(id),
 run_id TEXT NOT NULL REFERENCES app.runs(id),
 document_handle TEXT UNIQUE, -- Legacy pipeline document reference, NOT an EvidenceLink.
 manifest_ref TEXT NOT NULL, text_ref TEXT,
 state TEXT NOT NULL CHECK(state IN ('succeeded','failed','unsupported')),
 metadata JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 CHECK((state='succeeded') = (document_handle IS NOT NULL AND text_ref IS NOT NULL))
);
CREATE TABLE app.source_fetch_attempts (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope_id TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES app.sources(id), canonical_url TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed','unknown')),
 snapshot_id TEXT REFERENCES app.source_snapshots(id), error TEXT,
 started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), finished_at TIMESTAMPTZ,
 UNIQUE(run_id,canonical_url),
 FOREIGN KEY(run_id,scope_id) REFERENCES app.source_research(run_id,scope_id),
 CHECK((state IN ('succeeded','failed')) = (snapshot_id IS NOT NULL))
);
CREATE TABLE app.source_results (
 id TEXT PRIMARY KEY, query_id TEXT NOT NULL REFERENCES app.source_queries(id),
 result_rank INTEGER NOT NULL CHECK(result_rank BETWEEN 1 AND 10),
 url TEXT NOT NULL, title TEXT, snippet TEXT,
 disposition TEXT NOT NULL DEFAULT 'pending'
   CHECK(disposition IN ('pending','read','duplicate','excluded','inaccessible')),
 reason TEXT, snapshot_id TEXT REFERENCES app.source_snapshots(id),
 UNIQUE(query_id,result_rank)
);
CREATE INDEX source_fetch_run_idx ON app.source_fetch_attempts(run_id);
CREATE INDEX source_snapshots_run_idx ON app.source_snapshots(run_id);
