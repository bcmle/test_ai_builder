"""Explicit one-model Tavily -> source snapshots -> LangGraph research CLI.

This command can spend API budget. It does not migrate, publish or deploy.
It is a bounded invocation, not the permanent owner-command worker. A paused
run requires owner control; --resume-run never increases budgets or changes
saved settings. Completed private drafts can be exported without API calls.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import model_validator

from .config import Configuration, load_config
from .evidence import ArtifactStore
from .fetch_transport import ControlledFetcher
from .pg_execution import acquire_lease, execution_pool, renew_lease
from .pg_ledger import PostgresLedger, digest
from .pipeline import ModelRoles, build_scope_graph, execute_scope
from .pricing import Pricing
from .providers import MeteredHTTP, TavilyAdapter
from .report import render_report
from .source_documents import parse_document
from .source_research import FrozenContract, PreparedScope, SourceResearchPlan, prepare_scope
from .store import BudgetExceeded, Conflict, micro


class ResearchContext(FrozenContract):
    schema_version: Literal['1'] = '1'
    plan: SourceResearchPlan
    openai_pricing: Pricing
    tavily_pricing: Pricing
    role_settings: dict
    purpose: Literal['development', 'eval', 'live']

    @model_validator(mode='after')
    def compatible(self):
        if self.openai_pricing.provider != 'openai' or self.tavily_pricing.provider != 'tavily':
            raise ValueError('Explicit OpenAI and Tavily tariffs are required')
        # Validation constructs no network request and writes no ledger state.
        ModelRoles(None, self.openai_pricing, '', self.role_settings, purpose=self.purpose)
        return self


@dataclass(frozen=True)
class ResearchRunResult:
    run_id: str
    prepared: PreparedScope
    state: dict
    usage: dict
    output: Path
    replay: bool


async def create_research_run(pool, config: Configuration, context: ResearchContext, study_budget) -> str:
    """Persist the complete validated context before any search or model call."""
    if context.openai_pricing.model != config.model:
        raise ValueError('Pricing model must match the frozen run configuration')
    if study_budget is None or micro(study_budget) <= 0:
        raise ValueError('An explicit positive study budget is required')
    # Revalidate a serialized copy so later mutation of the caller's dictionaries
    # cannot alter the context executed below.
    value = ResearchContext.model_validate_json(context.model_dump_json()).model_dump(mode='json')
    admin = PostgresLedger(pool)
    study = await admin.create_study(config.market, study_budget)
    run_id = await admin.create_run(study, config)
    async with pool.connection() as conn, conn.transaction():
        await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',
                           (run_id, digest(value), Jsonb(value)))
        await admin.event(conn, run_id, 'research_context_saved', {'context_hash': digest(value)})
    return run_id


async def _load_context(ledger):
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, run):
        row = await (await conn.execute('SELECT * FROM app.research_contexts WHERE run_id=%s', (ledger.run_id,))).fetchone()
        if not row or row['context_hash'] != digest(row['context']):
            raise Conflict('Missing or altered research context')
        config = Configuration.model_validate(run['config_json'])
        context = ResearchContext.model_validate(row['context'])
        if config.digest() != run['config_hash'] or context.openai_pricing.model != config.model:
            raise Conflict('Run configuration integrity mismatch')
        return config, context, run['status']


async def _save_scope(ledger, prepared: PreparedScope, context: ResearchContext):
    scope = prepared.scope.model_dump(mode='json')
    expected = dict(scope=scope, role_settings=context.role_settings,
                    pricing=context.openai_pricing.model_dump(mode='json'), purpose=context.purpose)
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, run):
        old = await (await conn.execute('SELECT * FROM app.scope_inputs WHERE run_id=%s', (ledger.run_id,))).fetchone()
        if old:
            if any(old[name] != value for name, value in expected.items()):
                raise Conflict('Frozen graph input changed')
            return
        if run['status'] not in {'selecting', 'queued', 'running'}:
            raise Conflict('Owner action required before freezing graph input')
        await conn.execute('INSERT INTO app.scope_inputs(run_id,scope,role_settings,pricing,purpose) VALUES (%s,%s,%s,%s,%s)',
                           (ledger.run_id, Jsonb(scope), Jsonb(expected['role_settings']),
                            Jsonb(expected['pricing']), expected['purpose']))
        await ledger.event(conn, ledger.run_id, 'research_graph_input_frozen',
                           {'scope_id': prepared.scope.scope_id, 'scope_hash': digest(scope)})


def _gap_state(config, prepared):
    return {
        'criteria': [dict(criterion_id=c.id, state='completed', result='unknown', evidence_ids=[],
                          reason='No policy-validated source document was available.') for c in config.criteria],
        'eligibility': 'needs_review',
        'card': dict(assessment_scope_id=prepared.scope.scope_id, eligibility='needs_review', score=None,
                     publication='draft', publication_blockers=['source_documents_unavailable']),
    }


def _write_report(path: Path, scope, state, usage, mode):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.research-', mode='w', encoding='utf-8', delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(render_report(scope, state, usage, mode=mode))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def run_research(pool, artifacts: ArtifactStore, *, openai_key: str = '', tavily_key: str = '',
                       config: Configuration | None = None, context: ResearchContext | None = None,
                       study_budget=None, resume_run: str | None = None, output: str | Path | None = None,
                       provider_client=None, fetch_document=None, parser=parse_document) -> ResearchRunResult:
    """Run from frozen inputs; injected HTTP/parser boundaries are for fixtures.

    Every metered provider shares the same ledger and active-time deadline.
    A resume can continue an interrupted running invocation after its lease
    expires, or re-export a finished draft. It cannot unpause a run.
    """
    if resume_run:
        if any(value is not None for value in (config, context, study_budget)):
            raise ValueError('Resume accepts only saved inputs; changed settings require a new run')
        run_id = resume_run
    else:
        if config is None or context is None or study_budget is None:
            raise ValueError('New research requires configuration, context and explicit study budget')
        if not openai_key or not tavily_key:
            raise ValueError('OPENAI_API_KEY and TAVILY_API_KEY are required')
        run_id = await create_research_run(pool, config, context, study_budget)
    token = await acquire_lease(pool, run_id, 'research-cli')
    ledger = PostgresLedger(pool, run_id, token)
    work = heart = None
    clock_started = False
    final_status = 'paused'

    async def heartbeat():
        while True:
            await asyncio.sleep(15)
            await renew_lease(pool, run_id, token)

    try:
        config, context, prior_status = await _load_context(ledger)
        if prior_status not in {'selecting', 'queued', 'running', 'partial', 'completed'}:
            raise Conflict('Run is paused; owner action required')
        replay = prior_status in {'partial', 'completed'}
        if not replay and (not openai_key or not tavily_key):
            raise ValueError('OPENAI_API_KEY and TAVILY_API_KEY are required')
        try:
            remaining = None if replay else await ledger.start_clock()
        except BudgetExceeded:
            await ledger.pause_run(run_id, 'research_active_time_exhausted')
            await ledger.stop_clock('paused')
            raise
        clock_started = not replay
        deadline = None if replay else time.monotonic() + remaining
        roles = ModelRoles(ledger, context.openai_pricing, openai_key, context.role_settings,
                           purpose=context.purpose, client=provider_client, deadline=deadline)
        fetch = fetch_document or ControlledFetcher(limits=context.plan.fetch_limits()).fetch

        def search_factory(operation_key):
            return TavilyAdapter(MeteredHTTP(ledger, run_id, 'discovery', context.tavily_pricing,
                                            context.purpose, client=provider_client, deadline=deadline,
                                            operation_key=operation_key), tavily_key)

        async def process():
            async with asyncio.timeout(remaining):
                if replay:
                    # A read-only replay must never enter prepare_scope's paid
                    # branch even if the database is incomplete/corrupted.
                    async with ledger.locked(run_id, fenced=True) as (conn, _, __):
                        row = await (await conn.execute(
                            'SELECT state FROM app.source_research WHERE run_id=%s AND scope_id=%s',
                            (run_id, context.plan.seed.scope_id))).fetchone()
                    if not row or row['state'] != 'frozen':
                        raise Conflict('Completed run is missing its frozen source packet')
                prepared = await prepare_scope(ledger, context.plan, search_factory, fetch, artifacts, parser=parser)
                await _save_scope(ledger, prepared, context)
                if not prepared.scope.documents:
                    state = _gap_state(config, prepared)
                    receipt_hash = digest({'scope': prepared.scope.model_dump(mode='json'),
                                           'gaps': [g.model_dump(mode='json') for g in prepared.gaps],
                                           'config': config.digest()})
                    saved = await ledger.receipt(prepared.scope.scope_id, 'source_gap_card',
                                                 'source-gap:' + receipt_hash, receipt_hash,
                                                 None if replay else state)
                    if saved != state:
                        raise Conflict('Source-gap result is missing or changed')
                else:
                    graph, graph_config = await build_scope_graph(ledger, config, prepared.scope, roles)
                    checkpoint = await graph.aget_state(graph_config)
                    if replay:
                        if checkpoint.next or not checkpoint.values.get('card'):
                            raise Conflict('Completed run has no completed graph checkpoint')
                        state = dict(checkpoint.values)
                    else:
                        state = await execute_scope(ledger, config, prepared.scope, roles,
                                                    resume=bool(checkpoint.created_at))
                async with ledger.locked(run_id, fenced=True) as (conn, _, __):
                    snapshots = await (await conn.execute(
                        'SELECT id,metadata FROM app.source_snapshots WHERE run_id=%s AND id=ANY(%s::text[])',
                        (run_id, [doc.snapshot_id for doc in prepared.scope.documents]))).fetchall()
                allowed_metadata = {'title', 'publisher', 'published_at', 'fetched_at', 'parser_version', 'extraction_method'}
                state = {**state, 'source_gaps': [g.model_dump(mode='json') for g in prepared.gaps],
                         'source_metadata': {row['id']: {key: value for key, value in row['metadata'].items()
                                                         if key in allowed_metadata} for row in snapshots}}
                usage = await ledger.summary(run_id)
                destination = Path(output) if output else artifacts.root.parent / 'research' / f'{run_id}.html'
                _write_report(destination, prepared.scope, state, usage, 'replay' if replay else context.purpose)
                return ResearchRunResult(run_id, prepared, state, usage, destination.resolve(), replay)

        work, heart = asyncio.create_task(process()), asyncio.create_task(heartbeat())
        finished, _ = await asyncio.wait({work, heart}, return_when=asyncio.FIRST_COMPLETED)
        if heart in finished:
            heart.result()  # Lost lease / failed heartbeat must stop live work.
            raise Conflict('Run heartbeat stopped')
        result = work.result()
        final_status = 'partial'  # Full claim review/publication remains separate.
        return result
    finally:
        for task in (work, heart):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (work, heart) if task is not None), return_exceptions=True)
        if clock_started:
            try:
                await ledger.stop_clock(final_status)
            except Conflict:
                pass  # An expired/superseded lease cannot change run state.
        async with pool.connection() as conn:
            await conn.execute('UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s AND token=%s', (run_id, token))


async def run(args):
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        raise ValueError('DATABASE_URL is required; migrations must already be applied')
    context = config = None
    if args.resume_run:
        if any(getattr(args, field) is not None for field in ('config', 'plan', 'pricing', 'tavily_pricing', 'settings', 'study_budget', 'purpose')):
            raise ValueError('Resume cannot override saved configuration, plan, prices or role settings')
    else:
        if not all((args.plan, args.pricing, args.tavily_pricing, args.settings, args.study_budget)):
            raise ValueError('New research requires plan, both tariffs, role settings and study budget')
        config = load_config(args.config or 'config/baseline.json')
        context = ResearchContext(
            plan=SourceResearchPlan.model_validate_json(Path(args.plan).read_text()),
            openai_pricing=Pricing.model_validate_json(Path(args.pricing).read_text()),
            tavily_pricing=Pricing.model_validate_json(Path(args.tavily_pricing).read_text()),
            role_settings=json.loads(Path(args.settings).read_text()), purpose=args.purpose or 'live')
    data_root = Path(os.environ.get('AI_BUILDER_DATA', 'outputs'))
    async with execution_pool(dsn) as pool:
        result = await run_research(pool, ArtifactStore(data_root / 'evidence'),
                                    openai_key=os.environ.get('OPENAI_API_KEY', ''),
                                    tavily_key=os.environ.get('TAVILY_API_KEY', ''),
                                    config=config, context=context, study_budget=args.study_budget,
                                    resume_run=args.resume_run, output=args.output)
    print(f'Run: {result.run_id}\nPrivate draft: {result.output}\nSource gaps: {len(result.prepared.gaps)}', flush=True)
    return result.run_id


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for flag in ('config', 'plan', 'pricing', 'tavily-pricing', 'settings', 'study-budget', 'resume-run', 'output'):
        cli.add_argument('--' + flag)
    cli.add_argument('--purpose', choices=('development', 'eval', 'live'))
    try:
        asyncio.run(run(cli.parse_args()))
    except Exception as error:
        cli.exit(1, f'Research stopped: {type(error).__name__}. Inspect the private run audit.\n')


if __name__ == '__main__':
    main()
