"""New-policy, one-card-at-a-time reassessment in the original cumulative budget.

Creation only freezes a new queue; run_collection publishes and processes it.
No discovery, budget reset, historical mutation, or automatic deployment.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from psycopg.types.json import Jsonb

from .collection import CollectionContext, _safe_failure, run_collection
from .config import Configuration, load_config
from .evidence import ArtifactStore
from .legal_sources import LegalAnalogue
from .pg_execution import execution_pool
from .pg_ledger import PostgresLedger, digest
from .pipeline import prompt_bundle
from .source_research import ScopeSeed, SourceResearchPlan, make_reassessment_plan
from .store import BudgetExceeded, Conflict, micro, uid


def priority_key(item, preferred_model_id=None):
    """Order only by the frozen, already-published original assessment."""
    criteria = (item.get('public_model') or {}).get('criteria', [])
    f10_pass = any(c['id'] == 'F10' and c.get('result') == 'pass' for c in criteria)
    unknown = sum(c.get('result') not in {'pass', 'fail'} for c in criteria)
    # Missing/technically incomplete criteria cannot outrank an analysed model
    # merely because there was no result to count as unknown.
    unknown += len(set(f'F{i:02}' for i in range(1, 11)) - {c['id'] for c in criteria})
    return (not f10_pass, unknown, item['model_id'] != preferred_model_id, item['position'])


async def create_reassessment(pool, *, parent_collection_id, config: Configuration,
                              role_settings, analogues_by_model, preferred_model_id=None):
    """Atomically freeze same twenty identities and same study, no external I/O."""
    if config.schema_version != '1.2':
        raise ValueError('Reassessment requires the conditional scoring configuration')
    prefix, prompts = prompt_bundle(config.schema_version)
    identifier, coordinator = uid(), uid()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(418724211)')
        parent = await (await conn.execute('SELECT * FROM app.collections WHERE id=%s',
                                           (parent_collection_id,))).fetchone()
        if not parent:
            raise ValueError('Unknown source collection')
        study = await (await conn.execute('SELECT * FROM app.studies WHERE id=%s FOR UPDATE',
                                          (parent['study_id'],))).fetchone()
        if await (await conn.execute("SELECT 1 FROM app.collections WHERE state IN ('discovering','running')")).fetchone():
            raise Conflict('Another collection is active; resume it before creating a revision')
        if await (await conn.execute('''SELECT 1 FROM app.run_leases l JOIN app.runs r ON r.id=l.run_id
            WHERE r.study_id=%s AND l.expires_at>clock_timestamp()''', (study['id'],))).fetchone():
            raise Conflict('An assessment in the original study still has an active owner')
        prior = CollectionContext.model_validate(parent['context'])
        items = await (await conn.execute('SELECT * FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                          (parent_collection_id,))).fetchall()
        members = [{key: item[key] for key in ('model_id', 'position', 'candidate')} for item in items]
        if (len(items) != 20 or parent['selected_count'] != 20 or digest(members) != parent['membership_hash']
                or digest(parent['context']) != parent['context_hash']):
            raise Conflict('The source collection must have twenty intact frozen models')
        if config.scenario != prior.config.scenario or config.market != prior.config.market:
            raise ValueError('This revision must preserve the original launch scenario and market')
        if micro(prior.study_budget_usd) != study['limit_micro']:
            raise Conflict('Original cumulative budget differs from its saved context')
        if (config.limits.run_cost_usd > prior.config.limits.run_cost_usd
                or config.limits.run_cost_usd > 25):
            raise ValueError('Reassessment cannot increase the per-run monetary limit')
        cost = await (await conn.execute('''SELECT COALESCE(sum(CASE WHEN u.state='settled' THEN u.calculated_micro
            WHEN u.state IN ('reserved','unknown') THEN u.upper_micro ELSE 0 END),0) AS used
            FROM app.usage u JOIN app.runs r ON r.id=u.run_id WHERE r.study_id=%s''', (study['id'],))).fetchone()
        if cost['used'] >= study['limit_micro']:
            raise BudgetExceeded('The original cumulative study budget is exhausted')
        ids = {item['model_id'] for item in items}
        if set(analogues_by_model) != ids or (preferred_model_id and preferred_model_id not in ids):
            raise ValueError('Analogue manifest and priority must match the original twenty model IDs')
        ordered = sorted(items, key=lambda item: priority_key(item, preferred_model_id))
        context = CollectionContext(plan=prior.plan, config=config, openai_pricing=prior.openai_pricing,
            tavily_pricing=prior.tavily_pricing, role_settings=role_settings,
            study_budget_usd=prior.study_budget_usd, purpose=prior.purpose, execution_version='collection-2',
            pipeline_prefix=prefix, pipeline_prompts=prompts, reassessment_of_collection_id=parent_collection_id,
            processing_order=tuple(item['model_id'] for item in ordered))
        packet = []
        for position, item in enumerate(ordered, 1):
            if not item['run_id']:
                raise Conflict('Every original member requires its saved research run')
            old = SourceResearchPlan.model_validate(item['candidate']['plan'])
            source = await (await conn.execute('SELECT state FROM app.source_research WHERE run_id=%s',
                                               (item['run_id'],))).fetchone()
            if not source or source['state'] != 'frozen':
                raise Conflict('Original sources must be frozen before a policy reassessment')
            seed = ScopeSeed(**{**old.seed.model_dump(), 'scope_id': uid(), 'variant_id': uid()})
            analogues = tuple(LegalAnalogue.model_validate(a) for a in analogues_by_model[item['model_id']])
            # Retain previously read evidence and repeat bounded targeted source
            # queries to repair nonlegal gaps, in addition to each legal review.
            plan = make_reassessment_plan(seed=seed, parent_run_id=item['run_id'],
                analogues=analogues, queries=old.queries, max_new_reads=min(24, config.limits.document_fetches))
            candidate = {**item['candidate'], 'plan': plan.model_dump(mode='json'), 'parent_run_id': item['run_id'],
                'metadata': {**item['candidate']['metadata'], 'queue_position': position,
                             'scoring_policy_version': '1.5.2'}}
            packet.append({'model_id': item['model_id'], 'position': position, 'candidate': candidate})
        value = context.model_dump(mode='json')
        await conn.execute('''INSERT INTO app.runs(id,study_id,config_json,config_hash,limit_micro,status,
            graph_version,state_schema_version,coordinator_thread_id)
            VALUES (%s,%s,%s,%s,%s,'selecting','scope-1','1',%s)''',
            (coordinator, study['id'], Jsonb(config.model_dump(mode='json')), config.digest(), study['limit_micro'], uid()))
        await conn.execute('''INSERT INTO app.collections(id,study_id,coordinator_run_id,context_hash,context,state,
            membership_hash,selected_count,frozen_at) VALUES (%s,%s,%s,%s,%s,'running',%s,20,clock_timestamp())''',
            (identifier, study['id'], coordinator, digest(value), Jsonb(value), digest(packet)))
        for item in packet:
            await conn.execute('''INSERT INTO app.collection_items(collection_id,model_id,position,candidate,state)
                VALUES (%s,%s,%s,%s,'queued')''', (identifier, item['model_id'], item['position'], Jsonb(item['candidate'])))
        await PostgresLedger.event(conn, coordinator, 'collection_reassessment_authorized', {
            'parent_collection_id': parent_collection_id, 'collection_id': identifier,
            'processing_order': list(context.processing_order), 'policy': '1.5.2',
            'budget_unchanged_micro': study['limit_micro'], 'already_accounted_micro': int(cost['used']),
            'membership_hash': digest(packet)})
    return identifier


async def run(args):
    if not os.environ.get('DATABASE_URL'):
        raise ValueError('DATABASE_URL is required')
    if not args.resume_collection and not all((args.parent_collection, args.config, args.settings, args.analogues)):
        raise ValueError('New revision requires parent, config, settings and analogue manifest')
    if args.resume_collection and any((args.parent_collection, args.config, args.settings, args.analogues, args.first_model)):
        raise ValueError('Resume cannot change frozen queue inputs')
    data_dir = Path(os.environ.get('AI_BUILDER_DATA', 'outputs'))
    async with execution_pool(os.environ['DATABASE_URL']) as pool:
        identifier = args.resume_collection or await create_reassessment(pool,
            parent_collection_id=args.parent_collection, config=load_config(args.config),
            role_settings=json.loads(Path(args.settings).read_text()),
            analogues_by_model=json.loads(Path(args.analogues).read_text()), preferred_model_id=args.first_model)
        print(f'Reassessment collection: {identifier}', flush=True)
        result = await run_collection(pool, ArtifactStore(data_dir / 'evidence'), resume_collection=identifier,
            data_dir=data_dir, openai_key=os.environ.get('OPENAI_API_KEY', ''),
            tavily_key=os.environ.get('TAVILY_API_KEY', ''), retry_failed=args.retry_failed)
    print(f'Updated {result.completed_count}/20; state={result.state}; '
          f'cumulative accounted=${result.usage["accounted_micro"]/1_000_000:.4f}', flush=True)
    return result


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for flag in ('parent-collection', 'config', 'settings', 'analogues', 'first-model', 'resume-collection'):
        cli.add_argument('--' + flag)
    cli.add_argument('--retry-failed', action='store_true')
    try:
        asyncio.run(run(cli.parse_args()))
    except Exception as error:
        cli.exit(1, f'Reassessment stopped: {_safe_failure(error)}. Inspect saved progress and private audit.\n')


if __name__ == '__main__':
    main()
