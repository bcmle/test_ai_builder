-- V3 adds durable per-analogue searches/link-discovery queues. This only widens
-- queue identities; paid searches and source reads retain their frozen caps.
ALTER TABLE app.source_queries DROP CONSTRAINT source_queries_query_index_check;
ALTER TABLE app.source_queries ADD CONSTRAINT source_queries_query_index_check
 CHECK(query_index BETWEEN 0 AND 63);
