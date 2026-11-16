"""PostgreSQL health and explicitly enabled synthetic technical preview.

No database/schema is created by web startup. The private deployment binds this
service to server loopback; public product routes remain a separate milestone.
"""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import hashlib
import json
import os
import re
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
from .public import (CONDITIONAL_CRITERIA, CRITERIA, FACTORS, GOALS, STAGES, STATUS_LABELS, load_featured_followup,
                     load_snapshot, snapshot_ancestors)


def _literal_excerpt(value, words, sentences=None):
    """A bounded source excerpt, never a generated paraphrase."""
    original = str(value or '').strip()
    end = len(original)
    if sentences:
        stops = list(re.finditer(r'[.!?](?=\s|$)', original))
        if len(stops) >= sentences:
            end = stops[sentences - 1].end()
    tokens = list(re.finditer(r'\S+', original[:end]))
    if len(tokens) > words:
        end = tokens[words - 1].end()
    text = original[:end].rstrip()
    return text + (' …' if original[end:].strip() else '')


def _ceo_summary(model):
    """Presentation only; the published assessment and its hashes stay intact."""
    claims = {claim['id']: claim for claim in model['claims']}
    risk_uses = Counter(cid for risk in model['risks'] for cid in set(risk['claim_ids']))
    risks = []
    for index, risk in enumerate(model['risks'][:3], 1):
        judgments = [claims[cid] for cid in risk['claim_ids']
                     if cid in claims and claims[cid]['kind'] == 'judgment']
        judgment = min(judgments, key=lambda claim: risk_uses[claim['id']], default=None)
        risks.append(dict(text=_literal_excerpt(judgment['text'] if judgment else risk['text'], 48, 1),
                          href=f"#claim-{judgment['id']}" if judgment else f'#risk-{index}'))
    next_step = None
    plans = [claims[cid] for cid in model['test_plan_claim_ids'] if cid in claims]
    action_patterns = (r'\bfirst action(?: on [^:.\n]+)?:\s*|\bfirst action (?:is|would be)(?: to)?\s+',
                       r'\bexperiment begins by\s+')
    selected_action = next(((claim, match.end()) for pattern in action_patterns for claim in plans
                           if (match := re.search(pattern, claim['text'], re.IGNORECASE))), None)
    if selected_action or plans:
        claim, start = selected_action if selected_action else (plans[0], 0)
        text = claim['text']
        next_step = dict(text=_literal_excerpt(text[start:], 48, 1), href=f"#claim-{claim['id']}")
    factors = {factor['id']: factor for factor in model['factors']}
    factor_names = dict(FACTORS)
    if model.get('scoring_policy_version') == '1.5.3':
        factor_names.update(S03='Time to prepare the minimum test',
                            S04='Cycle from ready offer to first payment',
                            S05='Fit with resources and dependencies')
    missing = sum(not factors.get(fid, {}).get('value') for fid in FACTORS)
    required = [criterion for criterion in model['criteria']
                if not model.get('scoring_policy_version') or criterion['id'] not in CONDITIONAL_CRITERIA]
    if model['status'] in {'queued', 'running', 'technical_failed', 'incomplete'}:
        reason = {'queued': 'Update queued.', 'running': 'Research in progress.',
                  'technical_failed': 'Update interrupted.', 'incomplete': 'Assessment incomplete.'}[model['status']]
    elif any(criterion['result'] == 'fail' and (not model.get('scoring_policy_version')
             or criterion['id'] not in {'F05', 'F06', 'F07'}) for criterion in model['criteria']):
        reason = 'An exclusion criterion failed.'
    elif any(criterion['result'] == 'unknown' for criterion in required):
        reason = 'Required eligibility checks remain unknown.'
    elif missing:
        reason = f'{missing} scoring factor' + ('s remain' if missing != 1 else ' remains') + ' unknown.'
    else:
        reason = 'Score validation is incomplete.'
    return dict(summary=_literal_excerpt(model['summary'], 28, 3),
                buyer=_literal_excerpt(model.get('buyer'), 8),
                revenue=_literal_excerpt(model.get('revenue'), 8),
                comparable=_literal_excerpt(model.get('comparable'), 6),
                risks=risks, next_step=next_step, score_reason=reason,
                factors=[dict(id=fid, name=factors.get(fid, {}).get('name') or name,
                             **{key: factors.get(fid, {}).get(key)
                             for key in ('value', 'claim_ids')}) for fid, name in factor_names.items()])


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
                             factor_names=FACTORS, stage_labels=STAGES)
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

    def update_context(snapshot, model):
        selected = snapshot.get('selected_model_ids')
        return dict(retained_assessment=(snapshot.get('retained_assessments') or {}).get(model['id']),
                    not_selected=bool(selected and model['id'] not in selected),
                    update_state=(snapshot.get('update_states') or {}).get(model['id']))

    def current_reassessment(snapshot):
        latest = load_snapshot(data)
        if latest and snapshot['run_id'] in {ancestor['run_id'] for ancestor in snapshot_ancestors(data, latest)}:
            return latest
        return None

    @app.api_route('/', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    async def home():
        snapshot = load_snapshot(data)
        return page('public_home.html', snapshot=snapshot,
                    followup=load_featured_followup(data),
                    featured=snapshot['models'][:3] if snapshot else [])

    @app.api_route('/runs/{run_id}', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    async def collection(run_id: str, request: Request):
        snapshot = require_snapshot(run_id)
        if request.query_params.get('format') == 'json':
            return JSONResponse(snapshot, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        query = request.query_params.get('q', '')[:120].strip()
        status = request.query_params.get('status', '')
        category = request.query_params.get('category', '')
        def visible_status(model):
            update = update_context(snapshot, model)
            if update['not_selected']:
                return 'not_selected'
            return update['update_state'] if update['retained_assessment'] else model['status']
        models = [m for m in snapshot['models'] if
                  (not query or query.casefold() in ' '.join(str(m.get(key) or '') for key in ('title', 'buyer', 'summary')).casefold())
                  and (status not in {*STATUS_LABELS, 'not_selected'} or visible_status(m) == status)
                  and (not category or m['category'] == category)]
        current_models = [m for m in snapshot['models'] if m['id'] not in snapshot.get('retained_assessments', {})]
        ranking = {goal: sorted((m for m in current_models if m['rank'] is not None and m['score']['status'] == 'final' and
                                 m['score']['goal_feasibility'] == goal), key=lambda m: (m['rank'], m['title'], m['id']))
                   for goal in GOALS}
        conditional_ranking = {goal: sorted((m for m in current_models if m['rank'] is not None and
                                 m['score']['status'] == 'conditional' and m['score']['goal_feasibility'] == goal),
                                 key=lambda m: (m['rank'], m['title'], m['id'])) for goal in GOALS}
        counts = {status: sum(m['status'] == status for m in current_models)
                  for status in ('pass', 'fail', 'needs_review', 'confirmed', 'conditional', 'blocked', 'unresolved')}
        counts['unfinished'] = (sum(state != 'completed' for state in snapshot['update_states'].values())
                                if snapshot.get('selected_model_ids') else
                                sum(m['status'] in {'queued', 'running', 'incomplete', 'technical_failed'} for m in snapshot['models']))
        reassessment = current_reassessment(snapshot)
        return page('public_run.html', snapshot=snapshot, models=models, ranking=ranking,
                    conditional_ranking=conditional_ranking,
                    conditional_count=sum(len(group) for group in conditional_ranking.values()), reassessment=reassessment,
                    ranked_count=sum(len(group) for group in ranking.values()), query=query, selected_status=status,
                    selected_category=category, categories=sorted({m['category'] for m in snapshot['models']}), counts=counts)

    @app.api_route('/models/{model_id}', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    @app.api_route('/runs/{run_id}/models/{model_id}', methods=['GET', 'HEAD'], response_class=HTMLResponse)
    async def model_detail(model_id: str, request: Request, run_id: str | None = None):
        snapshot = require_snapshot(run_id or request.query_params.get('run'))
        model = next((m for m in snapshot['models'] if m['id'] == model_id), None)
        if model is None:
            raise HTTPException(404, 'Model not found in this published collection')
        followup = load_featured_followup(data)
        ancestors = snapshot_ancestors(data, snapshot)
        if followup and (followup['parent_collection_id'] not in {snapshot['run_id'], *(parent['run_id'] for parent in ancestors)}
                         or followup['parent_model_id'] != model['id']
                         or followup['models'][0]['base_model_id'] != model['base_model_id']):
            followup = None
        reassessment = current_reassessment(snapshot)
        previous_snapshot = previous_model = None
        update = update_context(snapshot, model)
        retained = update['retained_assessment']
        if retained:
            parent = load_snapshot(data, retained['run_id'], revision_id=retained['revision_id'])
            if parent:
                previous_snapshot = parent
        elif model['status'] in {'queued', 'running', 'technical_failed'}:
            for parent in ancestors:
                prior = next((item for item in parent['models'] if item['id'] == model['id']
                              and item['base_model_id'] == model['base_model_id']), None)
                if prior and prior['status'] in {'pass', 'fail', 'needs_review', 'confirmed', 'conditional', 'blocked', 'unresolved'}:
                    previous_snapshot, previous_model = parent, prior
                    break
        return page('public_model.html', snapshot=snapshot, model=model, reassessment=reassessment,
                    ceo=_ceo_summary(model),
                    followup=followup, previous_snapshot=previous_snapshot, previous_model=previous_model,
                    previous_sources={s['id']: s for s in previous_model['sources']} if previous_model else {},
                    sources={s['id']: s for s in model['sources']}, **update)

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
        if (revision and revision != snapshot['revision_id']) or (snapshot.get('scoring_policy_version') and not revision):
            return page('public_error.html', status_code=409, snapshot=snapshot,
                        message='This research has been updated. Select models again to compare the current revision.')
        selected = [next(m for m in snapshot['models'] if m['id'] == identifier) for identifier in identifiers]
        if snapshot.get('selected_model_ids') and any(identifier not in snapshot['selected_model_ids']
                or identifier in snapshot['retained_assessments'] for identifier in identifiers):
            return page('public_error.html', status_code=409, snapshot=snapshot,
                        message='Compare completed updates from this revision. Retained assessments keep their original policy and are outside this comparison.')
        return page('public_compare.html', snapshot=snapshot, models=selected,
                    briefs={model['id']: _ceo_summary(model) for model in selected})

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
