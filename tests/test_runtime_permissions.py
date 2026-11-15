"""Optional deployment-role integration test in a disposable PostgreSQL database.

Requires TEST_DEPLOY_ADMIN_DSN and psql on PATH; never targets an existing database.
"""
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from ai_builder.api import create_app
from ai_builder.pg_migrate import migrate
from ai_builder.smoke import run_smoke

ADMIN = os.getenv('TEST_DEPLOY_ADMIN_DSN')
ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(not ADMIN or not shutil.which('psql'), reason='TEST_DEPLOY_ADMIN_DSN and psql required')
def test_runtime_has_no_ddl_but_runs_health_and_complete_smoke(tmp_path,monkeypatch):
    for key in ('OPENAI_API_KEY','TAVILY_API_KEY','ASTRA_API_KEY','LANGSMITH_API_KEY','LANGCHAIN_API_KEY',
                'LANGSMITH_TRACING','LANGCHAIN_TRACING_V2'):
        monkeypatch.delenv(key,raising=False)
    suffix = uuid4().hex[:12]
    database, migrator, runtime = [prefix+suffix for prefix in ('ai_rolecheck_','ai_migrator_','ai_runtime_')]
    admin = psycopg.connect(ADMIN, autocommit=True)
    created = False
    test_migrator_password='synthetic-'+uuid4().hex
    test_runtime_password='synthetic-'+uuid4().hex
    def apply_script(name):
        script=(ROOT/'deploy'/name).read_text().replace('ai_builder',database).replace('ai_migrator',migrator).replace('ai_runtime',runtime)
        env={**os.environ,'MIGRATOR_PASSWORD':test_migrator_password,'RUNTIME_PASSWORD':test_runtime_password}
        result=subprocess.run(['psql','-X','-q','--no-password','--dbname',make_conninfo(ADMIN,dbname=database)],
                              input=script,text=True,capture_output=True,env=env,timeout=30)
        assert result.returncode==0,result.stderr
    try:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
        created=True
        apply_script('roles.sql')
        migrator_dsn=make_conninfo(ADMIN,dbname=database,user=migrator,password=test_migrator_password)
        runtime_dsn=make_conninfo(ADMIN,dbname=database,user=runtime,password=test_runtime_password)
        asyncio.run(migrate(migrator_dsn))
        apply_script('grants.sql')
        with psycopg.connect(runtime_dsn,autocommit=True) as conn:
            for statement in ('CREATE TABLE app.runtime_denied(id int)',
                              'CREATE TABLE checkpoints.runtime_denied(id int)',
                              'CREATE TABLE public.runtime_denied(id int)',
                              'CREATE SCHEMA runtime_denied',
                              'CREATE TEMP TABLE runtime_denied(id int)',
                              "UPDATE app.schema_migrations SET checksum='tampered'",
                              'DELETE FROM checkpoints.checkpoint_migrations'):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
        data=tmp_path/'data'
        first=asyncio.run(run_smoke(runtime_dsn,data,ROOT/'config'/'baseline.json'))
        second=asyncio.run(run_smoke(runtime_dsn,data,ROOT/'config'/'baseline.json'))
        assert first['run_id']==second['run_id']
        assert first['fixture_http_calls_this_invocation']==7
        assert second['fixture_http_calls_this_invocation']==0
        assert first['physical_attempts']==second['physical_attempts']==15
        with TestClient(create_app(data,ROOT/'config'/'baseline.json',database_url=runtime_dsn,technical_preview=True)) as client:
            assert client.get('/api/v1/health/ready').status_code==200
            assert client.get('/preview').status_code==200
    finally:
        # Only the unique database and roles created by THIS test are cleanup targets.
        if created:
            admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(database)))
        for role in (migrator,runtime):
            admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
        admin.close()
