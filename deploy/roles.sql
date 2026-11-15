\set ON_ERROR_STOP on
\getenv migrator_password MIGRATOR_PASSWORD
\getenv runtime_password RUNTIME_PASSWORD
SELECT format('CREATE ROLE ai_migrator LOGIN PASSWORD %L', :'migrator_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='ai_migrator') \gexec
SELECT format('CREATE ROLE ai_runtime LOGIN PASSWORD %L', :'runtime_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='ai_runtime') \gexec
SELECT format('ALTER ROLE ai_migrator PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'migrator_password') \gexec
SELECT format('ALTER ROLE ai_runtime PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'runtime_password') \gexec
REVOKE ALL ON DATABASE ai_builder FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE ai_builder TO ai_migrator;
GRANT CONNECT ON DATABASE ai_builder TO ai_runtime;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION ai_migrator;
CREATE SCHEMA IF NOT EXISTS checkpoints AUTHORIZATION ai_migrator;
GRANT USAGE ON SCHEMA app, checkpoints TO ai_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_migrator IN SCHEMA app, checkpoints
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ai_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_migrator IN SCHEMA app, checkpoints
  GRANT USAGE, SELECT ON SEQUENCES TO ai_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app, checkpoints TO ai_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app, checkpoints TO ai_runtime;
