\set ON_ERROR_STOP on
-- Works for both complete installations and partially initialized schemas.
-- All identifiers come from the catalog and are quoted by PostgreSQL format().
SELECT format('SELECT %L AS relation, count(*)::text AS row_count FROM %I.%I',
              schemaname||'.'||tablename, schemaname, tablename)
FROM pg_tables WHERE schemaname IN ('app','checkpoints')
ORDER BY schemaname,tablename \gexec
SELECT to_regclass('app.usage') IS NOT NULL AS has_usage \gset
\if :has_usage
SELECT 'usage_totals', json_build_object(
 'calculated_micro',coalesce(sum(calculated_micro),0),
 'reserved_micro',coalesce(sum(upper_micro) FILTER (WHERE state IN ('reserved','unknown')),0)
)::text FROM app.usage;
\endif
