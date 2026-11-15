"""PostgreSQL health and explicitly enabled synthetic technical preview.

No database/schema is created by web startup. The private deployment binds this
service to server loopback; public product routes remain a separate milestone.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from langgraph.checkpoint.postgres.base import MIGRATIONS as CHECKPOINT_MIGRATIONS
import psycopg
from psycopg_pool import PoolTimeout

from .config import load_config
from .pg_execution import execution_pool
from .pg_migrate import MIGRATIONS


async def check_database(pool):
    """Read-only schema/checksum verification, including the pinned saver schema."""
    if pool is None:
        raise RuntimeError('DATABASE_URL not configured')
    expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(MIGRATIONS.glob('[0-9][0-9][0-9]_*.sql'))}
    if not expected:
        raise RuntimeError('Migration resources missing from image')
    async with pool.connection() as conn, conn.transaction():
        await conn.execute('SET TRANSACTION READ ONLY')
        rows = await (await conn.execute('SELECT name, checksum FROM app.schema_migrations')).fetchall()
        if {r['name']:r['checksum'] for r in rows} != expected:
            raise RuntimeError('Application migration version/checksum mismatch')
        rows = await (await conn.execute('SELECT v FROM checkpoints.checkpoint_migrations')).fetchall()
        if {r['v'] for r in rows} != set(range(len(CHECKPOINT_MIGRATIONS))):
            raise RuntimeError('Checkpoint migration version mismatch')
        await conn.execute('SELECT id FROM app.runs LIMIT 0')
        await conn.execute('SELECT request_id FROM app.usage LIMIT 0')
        await conn.execute('SELECT thread_id FROM checkpoints.checkpoints LIMIT 0')
    return len(expected)


def create_app(data_dir=None, config_path=None, *, database_url=None, technical_preview=None):
    data = Path(data_dir or os.environ.get('AI_BUILDER_DATA', 'data'))
    config = Path(config_path or os.environ.get('AI_BUILDER_CONFIG', 'config/baseline.json'))
    dsn = database_url if database_url is not None else os.environ.get('DATABASE_URL')
    preview_enabled = (os.environ.get('AI_BUILDER_TECHNICAL_PREVIEW') == '1'
                       if technical_preview is None else technical_preview)

    @asynccontextmanager
    async def lifespan(app):
        app.state.pool = execution_pool(dsn) if dsn else None
        if app.state.pool is not None:
            await app.state.pool.open(wait=False)
        try:
            yield
        finally:
            if app.state.pool is not None:
                await app.state.pool.close()

    app = FastAPI(title='AI Builder', version='0.1.0', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/api/v1/health/live')
    async def live():
        return {'status':'alive'}

    @app.get('/api/v1/health/ready')
    async def ready():
        try:
            load_config(config)
            async with asyncio.timeout(3):
                schema = await check_database(app.state.pool)
            # Probe the actual evidence/results volume without publishing a file.
            with tempfile.TemporaryFile(dir=data) as probe:
                probe.write(b'ready')
                probe.flush()
                os.fsync(probe.fileno())
            return {'status':'ready','storage':'postgresql','schema_version':schema}
        except (OSError, ValueError, RuntimeError, psycopg.Error, PoolTimeout, TimeoutError):
            return JSONResponse({'code':'not_ready','message':'Storage or configuration unavailable'}, status_code=503)

    @app.get('/preview', response_class=HTMLResponse)
    async def preview():
        if not preview_enabled:
            raise HTTPException(404)
        try:
            manifest = json.loads((data/'technical-preview.json').read_text())
            if manifest.get('kind') != 'synthetic-technical-preview-v1' or manifest.get('external_api_calls') != 0:
                raise ValueError('Not a synthetic technical preview')
            report = (data/'technical-preview.html').read_bytes()
            if hashlib.sha256(report).hexdigest() != manifest['report_sha256']:
                raise ValueError('Preview integrity mismatch')
            if b'Not a live market assessment.' not in report:
                raise ValueError('Synthetic label missing')
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(404, 'Technical preview not available') from None
        return HTMLResponse(report, headers={
            'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
        })

    return app


app = create_app()
