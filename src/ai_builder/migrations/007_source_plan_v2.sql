-- V2 source plans admit a bounded primary-lead queue and twelve search queries.
-- Existing plans, indices, receipts and frozen scopes are unchanged.
ALTER TABLE app.source_queries DROP CONSTRAINT source_queries_query_index_check;
ALTER TABLE app.source_queries ADD CONSTRAINT source_queries_query_index_check
 CHECK(query_index BETWEEN 0 AND 12);
ALTER TABLE app.source_results DROP CONSTRAINT source_results_result_rank_check;
ALTER TABLE app.source_results ADD CONSTRAINT source_results_result_rank_check
 CHECK(result_rank BETWEEN 1 AND 16);
