"""Explicit, resumable follow-up of a frozen collection member, within its budget.

The original collection stays intact. This command publishes one separately
scoped assessment, without making it the latest twenty-model collection.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .collection import CollectionContext, _read_collection, _resume_child, _safe_failure
from .config import load_config
from .evidence import ArtifactStore
from .pg_execution import acquire_lease, execution_pool, renew_lease
from .pg_ledger import PostgresLedger
from .public import public_model, publish_featured_followup, publish_snapshot
from .research_scope import ResearchContext, create_research_run, run_research
from .source_research import SourceResearchPlan
from .store import Conflict, uid


async def run_focused(pool, artifacts, *, parent_collection_id, model_id,
                      openai_key='', tavily_key='', data_dir=None,
                      config_dir='config', retry_paused=False,
                      provider_client=None, fetch_document=None, parser=None):
    """Create once, then resume from stored context; no fresh study allocation."""
    async with pool.connection() as conn:
        row = await (await conn.execute('SELECT coordinator_run_id FROM app.collections WHERE id=%s',
                                       (parent_collection_id,))).fetchone()
    if not row:
        raise ValueError('Unknown parent collection')
    coordinator = row['coordinator_run_id']
    token = await acquire_lease(pool, coordinator, 'focused-research-cli')
    parent_ledger = PostgresLedger(pool, coordinator, token)
    data_dir = Path(data_dir) if data_dir is not None else artifacts.root.parent

    async def heartbeat():
        while True:
            await asyncio.sleep(15)
            await renew_lease(pool, coordinator, token)

    async def process():
        collection, items = await _read_collection(parent_ledger, parent_collection_id)
        item = next((item for item in items if item['model_id'] == model_id), None)
        if not item or not item['run_id']:
            raise ValueError('Follow-up requires an existing analysed collection member')
        parent = CollectionContext.model_validate(collection['context'])
        if parent.purpose != 'live':
            raise ValueError('Public focused analysis requires a live parent collection')
        async with parent_ledger.locked(coordinator, fenced=True) as (conn, _, __):
            existing = await (await conn.execute('''SELECT r.id,r.status,rc.context,r.config_json,r.created_at
                FROM app.research_contexts rc JOIN app.runs r ON r.id=rc.run_id
                WHERE r.study_id=%s AND rc.context->>'parent_collection_id'=%s
                AND rc.context->>'parent_run_id'=%s ORDER BY r.created_at''',
                (collection['study_id'], parent_collection_id, item['run_id']))).fetchall()
        if len(existing) > 1:
            raise Conflict('Multiple follow-ups exist; an explicit revision selection is required')
        if existing:
            run_id = existing[0]['id']
            context = ResearchContext.model_validate(existing[0]['context'])
            from .config import Configuration
            config = Configuration.model_validate(existing[0]['config_json'])
            if existing[0]['status'] == 'paused':
                if not retry_paused:
                    raise Conflict('Follow-up is paused; use --retry-paused after inspecting the audit')
                await _resume_child(parent_ledger, parent_collection_id, run_id,
                                    reason='Owner invoked focused --retry-paused; original limits and inputs retained')
        else:
            if not openai_key or not tavily_key:
                raise ValueError('Both provider keys are required for a new follow-up')
            folder = Path(config_dir)
            config = load_config(folder / 'helpdesk-config.json')
            plan_value = json.loads((folder / 'helpdesk-plan.json').read_text())
            if plan_value['seed']['base_model_id'] != model_id:
                raise ValueError('The focused plan belongs to a different base model')
            plan_value['seed'].update(scope_id=uid(), variant_id=uid())
            context = ResearchContext(
                plan=SourceResearchPlan.model_validate(plan_value),
                openai_pricing=parent.openai_pricing, tavily_pricing=parent.tavily_pricing,
                role_settings=json.loads((folder / 'helpdesk-roles.json').read_text()), purpose=parent.purpose,
                parent_collection_id=parent_collection_id, parent_run_id=item['run_id'])
            run_id = await create_research_run(pool, config, context, study_id=collection['study_id'])
        if context.plan.seed.base_model_id != model_id:
            raise Conflict('Follow-up base identity differs from its parent')
        print(f'Focused run: {run_id}; parent={parent_collection_id}; model={model_id}', flush=True)
        options = dict(resume_run=run_id, openai_key=openai_key, tavily_key=tavily_key,
                       provider_client=provider_client, fetch_document=fetch_document)
        if parser is not None:
            options['parser'] = parser
        result = await run_research(pool, artifacts, **options)
        metadata = dict(item['candidate']['metadata'])
        metadata['summary'] = context.plan.seed.neutral_model_description.split('\n', 1)[0]
        if any('zendesk.com/' in doc.url for doc in result.prepared.scope.documents):
            metadata['comparable'] = 'Zendesk (documented source); Intercom (original discovery lead)'
        model = public_model(result, metadata, config)
        async with parent_ledger.locked(coordinator, fenced=True) as (conn, _, __):
            row = await (await conn.execute('SELECT created_at FROM app.runs WHERE id=%s', (run_id,))).fetchone()
            now = datetime.now(timezone.utc).isoformat()
            snapshot = dict(schema_version='prototype-1', run_id=run_id, revision_id=uid(),
                title='Customer Support Helpdesk · Texas / Austin', market=config.market,
                created_at=row['created_at'].isoformat(), published_at=now, status='completed',
                stage='Focused analysis complete', expected_count=1, selected_count=1, completed_count=1,
                parent_collection_id=parent_collection_id, parent_run_id=item['run_id'], parent_model_id=model_id,
                launch_conditions=[context.plan.seed.neutral_model_description],
                scenario=dict(team_size=config.scenario.team_size, business_budget_usd=str(config.scenario.business_budget_usd),
                              days=config.scenario.days, jurisdiction=config.scenario.jurisdiction), models=[model])
            destination = publish_snapshot(data_dir, snapshot, set_latest=False)
            publish_featured_followup(data_dir, run_id)
            await parent_ledger.event(conn, coordinator, 'focused_analysis_published', {
                'followup_run_id': run_id, 'parent_run_id': item['run_id'], 'model_id': model_id,
                'eligibility': model['status'], 'eligible_for_ranking': model['eligible_for_ranking'],
                'revision_id': snapshot['revision_id']})
        print(f'Focused result: {model["status"]}; ranked={model["eligible_for_ranking"]}; snapshot={destination}', flush=True)
        return result

    work, heart = asyncio.create_task(process()), asyncio.create_task(heartbeat())
    try:
        finished, _ = await asyncio.wait({work, heart}, return_when=asyncio.FIRST_COMPLETED)
        if heart in finished:
            heart.result()
            raise Conflict('Follow-up coordinator heartbeat stopped')
        return work.result()
    finally:
        work.cancel()
        heart.cancel()
        await asyncio.gather(work, heart, return_exceptions=True)
        async with pool.connection() as conn:
            await conn.execute('UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s AND token=%s',
                               (coordinator, token))


async def run(args):
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        raise ValueError('DATABASE_URL is required')
    data = Path(os.environ.get('AI_BUILDER_DATA', 'outputs'))
    async with execution_pool(dsn) as pool:
        return await run_focused(pool, ArtifactStore(data / 'evidence'),
            parent_collection_id=args.parent_collection, model_id=args.model_id,
            openai_key=os.environ.get('OPENAI_API_KEY', ''), tavily_key=os.environ.get('TAVILY_API_KEY', ''),
            data_dir=data, retry_paused=args.retry_paused)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--parent-collection', required=True)
    cli.add_argument('--model-id', required=True)
    cli.add_argument('--retry-paused', action='store_true')
    try:
        asyncio.run(run(cli.parse_args()))
    except Exception as error:
        cli.exit(1, f'Focused research stopped: {_safe_failure(error)}. Inspect the saved private audit.\n')


if __name__ == '__main__':
    main()
