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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from langgraph.checkpoint.postgres.base import MIGRATIONS as CHECKPOINT_MIGRATIONS
import psycopg
from psycopg_pool import PoolTimeout

from .config import load_config
from .pg_execution import execution_pool
from .pg_migrate import MIGRATIONS
from .public import CRITERIA, FACTORS, GOALS, STATUS_LABELS, load_snapshot


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
    assets = Path(__file__).with_name('static')
    if assets.is_dir():
        app.mount('/assets', StaticFiles(directory=assets), name='assets')
    templates = Environment(loader=FileSystemLoader(Path(__file__).with_name('templates')),
                            autoescape=select_autoescape(['html']))
    templates.globals.update(status_labels=STATUS_LABELS, goal_labels=GOALS, criterion_names=CRITERIA,
                             factor_names=FACTORS)
    templates.filters['date'] = lambda value: str(value)[:10] if value else 'Not supplied'
    templates.filters['number'] = lambda value: f'{float(value):,.1f}'.removesuffix('.0') if value is not None else '—'
    templates.filters['dollars'] = lambda value: f'${float(value):,.0f}' if value is not None else 'Not available'

    def page(name, *, status_code=200, **context):
        return HTMLResponse(templates.get_template(name).render(**context), status_code=status_code, headers={
            'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Cache-Control': 'no-cache',
            'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
        })

    def require_snapshot(identifier=None):
        snapshot = load_snapshot(data, identifier)
        if snapshot is None:
            raise HTTPException(404, 'Published research not found')
        return snapshot

    @app.api_route('/', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    async def home():
        snapshot = load_snapshot(data)
        return page('public_home.html', snapshot=snapshot,
                    featured=snapshot['models'][:3] if snapshot else [])

    @app.api_route('/runs/{run_id}', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    async def collection(run_id: str, request: Request):
        snapshot = require_snapshot(run_id)
        if request.query_params.get('format') == 'json':
            return JSONResponse(snapshot, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        query = request.query_params.get('q', '')[:120].strip()
        status = request.query_params.get('status', '')
        category = request.query_params.get('category', '')
        models = [m for m in snapshot['models'] if
                  (not query or query.casefold() in ' '.join(str(m.get(key) or '') for key in ('title', 'buyer', 'summary')).casefold())
                  and (status not in STATUS_LABELS or m['status'] == status)
                  and (not category or m['category'] == category)]
        ranking = {goal: sorted((m for m in snapshot['models'] if m['rank'] is not None and
                                 m['score']['goal_feasibility'] == goal), key=lambda m: (m['rank'], m['title'], m['id']))
                   for goal in GOALS}
        counts = {status: sum(m['status'] == status for m in snapshot['models']) for status in ('pass', 'fail', 'needs_review')}
        counts['unfinished'] = sum(m['status'] in {'queued', 'running', 'incomplete', 'technical_failed'} for m in snapshot['models'])
        return page('public_run.html', snapshot=snapshot, models=models, ranking=ranking,
                    ranked_count=sum(len(group) for group in ranking.values()), query=query, selected_status=status,
                    selected_category=category, categories=sorted({m['category'] for m in snapshot['models']}), counts=counts)

    @app.api_route('/models/{model_id}', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    @app.api_route('/runs/{run_id}/models/{model_id}', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    async def model_detail(model_id: str, request: Request, run_id: str | None = None):
        snapshot = require_snapshot(run_id or request.query_params.get('run'))
        model = next((m for m in snapshot['models'] if m['id'] == model_id), None)
        if model is None:
            raise HTTPException(404, 'Model not found in this published collection')
        return page('public_model.html', snapshot=snapshot, model=model,
                    sources={s['id']: s for s in model['sources']})

    @app.api_route('/compare', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    @app.api_route('/runs/{run_id}/compare', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    async def compare(request: Request, run_id: str | None = None):
        snapshot = require_snapshot(run_id or request.query_params.get('run'))
        identifiers = [value for value in request.query_params.get('models', '').split(',') if value]
        if (not 1 <= len(identifiers) <= 3 or len(set(identifiers)) != len(identifiers)
                or any(identifier not in {m['id'] for m in snapshot['models']} for identifier in identifiers)):
            return page('public_error.html', status_code=400, snapshot=snapshot,
                        message='Choose one to three models from this collection to compare.')
        revision = request.query_params.get('revision')
        if revision and revision != snapshot['revision_id']:
            return page('public_error.html', status_code=409, snapshot=snapshot,
                        message='This research has been updated. Select models again to compare the current revision.')
        selected = [next(m for m in snapshot['models'] if m['id'] == identifier) for identifier in identifiers]
        return page('public_compare.html', snapshot=snapshot, models=selected)

    @app.api_route('/methodology', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    async def methodology():
        return page('public_methodology.html', snapshot=load_snapshot(data))

    @app.get('/health/live')
    @app.get('/api/v1/health/live')
    async def live():
        return {'status':'alive'}

    @app.get('/health/ready')
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
