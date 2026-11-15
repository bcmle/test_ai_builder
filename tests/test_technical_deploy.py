import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from ai_builder.api import create_app
from ai_builder.pg_migrate import migrate
from ai_builder.smoke import run_smoke
from ai_builder.evidence import ArtifactStore

CONFIG = Path(__file__).parents[1]/'config'/'baseline.json'
DSN = os.getenv('TEST_DATABASE_URL')


def test_web_does_not_create_sqlite_or_migrate_without_database(tmp_path):
    with TestClient(create_app(tmp_path,CONFIG,database_url='')) as client:
        assert client.get('/api/v1/health/live').json()=={'status':'alive'}
        assert client.get('/api/v1/health/ready').status_code==503
        assert client.get('/preview').status_code==404
    assert list(tmp_path.iterdir())==[]


def test_preview_is_opt_in_fixed_synthetic_file_with_integrity(tmp_path):
    report=b'<!doctype html><p>Not a live market assessment.</p>'
    manifest={'kind':'synthetic-technical-preview-v1','external_api_calls':0,
              'report_sha256':hashlib.sha256(report).hexdigest()}
    (tmp_path/'technical-preview.html').write_bytes(report)
    (tmp_path/'technical-preview.json').write_text(json.dumps(manifest))
    (tmp_path/'secret.env').write_text('do not expose')
    with TestClient(create_app(tmp_path,CONFIG,database_url='',technical_preview=False)) as client:
        assert client.get('/preview').status_code==404
    with TestClient(create_app(tmp_path,CONFIG,database_url='',technical_preview=True)) as client:
        response=client.get('/preview')
        assert response.status_code==200 and response.content==report
        assert response.headers['cache-control']=='no-store'
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']
        assert client.get('/secret.env').status_code==404
        (tmp_path/'technical-preview.html').write_bytes(b'corrupted')
        assert client.get('/preview').status_code==404


def test_smoke_refuses_provider_credentials_before_database(monkeypatch,tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY','not-a-real-key')
    with pytest.raises(RuntimeError,match='credentials'):
        asyncio.run(run_smoke('not a database',tmp_path,CONFIG))


@pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')
def test_technical_smoke_restart_preserves_graph_and_ledger(tmp_path,monkeypatch):
    for key in ('OPENAI_API_KEY','TAVILY_API_KEY','ASTRA_API_KEY'):
        monkeypatch.delenv(key,raising=False)
    async def forbid_external_http(*args,**kwargs):
        raise AssertionError('External HTTP forbidden during technical smoke')
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbid_external_http)
    config=json.loads(CONFIG.read_text())
    config['model']='synthetic-model-'+uuid4().hex  # separate fixture identity for this test
    config_path=tmp_path/'config.json'
    config_path.write_text(json.dumps(config))
    data=tmp_path/'data'
    async def scenario():
        await migrate(DSN)
        first=await run_smoke(DSN,data,config_path)
        second=await run_smoke(DSN,data,config_path)
        assert first['run_id']==second['run_id']
        assert first['fixture_http_calls_this_invocation']==15
        assert second['fixture_http_calls_this_invocation']==0
        assert second['external_api_calls']==0
        assert first['physical_attempts']==second['physical_attempts']==15
        assert first['synthetic_calculated_micro']==second['synthetic_calculated_micro']==30000
        assert first['report_sha256']==second['report_sha256']
        assert first['evidence_manifest']==second['evidence_manifest']
        ArtifactStore(data/'evidence').verify_manifest(second['evidence_manifest'])
    asyncio.run(scenario())
    with TestClient(create_app(data,config_path,database_url=DSN,technical_preview=True)) as client:
        ready=client.get('/api/v1/health/ready')
        assert ready.status_code==200,ready.text
        assert ready.json()['storage']=='postgresql'
        assert client.get('/preview').status_code==200
    missing=tmp_path/'not-created'
    with TestClient(create_app(missing,config_path,database_url=DSN)) as client:
        assert client.get('/api/v1/health/ready').status_code==503
    assert not missing.exists()
