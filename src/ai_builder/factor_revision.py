"""Create/resume the authorized five-card factor revision in the existing study."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from psycopg.types.json import Jsonb

from .collection import CollectionContext, _safe_failure, run_collection
from .config import load_config
from .evidence import ArtifactStore
from .pg_execution import execution_pool
from .pg_ledger import PostgresLedger, digest
from .pipeline import prompt_bundle
from .store import BudgetExceeded, Conflict, micro, uid
from .upstream_revision import freeze_upstream_manifest


async def _ancestors(conn, parent_id):
    chain, seen = [], set()
    while parent_id:
        if parent_id in seen or len(seen) >= 20:
            raise Conflict('Ambiguous collection ancestry')
        seen.add(parent_id)
        row = await (await conn.execute('SELECT * FROM app.collections WHERE id=%s', (parent_id,))).fetchone()
        if not row or digest(row['context']) != row['context_hash']:
            raise Conflict('Missing or changed ancestor context')
        items = await (await conn.execute('SELECT * FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                         (parent_id,))).fetchall()
        members = [{key:i[key] for key in ('model_id','position','candidate')} for i in items]
        if len(items) != 20 or row['selected_count'] != 20 or digest(members) != row['membership_hash']:
            raise Conflict('Missing or changed ancestor membership')
        if chain and (row['study_id'] != chain[0][0]['study_id']
                      or {i['model_id'] for i in items} != {i['model_id'] for i in chain[0][1]}):
            raise Conflict('Ancestor study or catalogue identity mismatch')
        chain.append((row, items))
        parent_id = row['context'].get('reassessment_of_collection_id')
    return chain


def _retained_models(data_dir, chain):
    """Freeze exact published payloads; retain useful results through empty attempts."""
    from .public import PublicSnapshot
    result = {}
    for row, items in chain:
        path = Path(data_dir) / 'public' / (row['id']+'.json')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 10_000_000:
            raise Conflict('A published ancestor snapshot is unavailable')
        raw = json.loads(path.read_text())
        PublicSnapshot.model_validate(raw)
        if raw['run_id'] != row['id'] or {m['id'] for m in raw['models']} != {i['model_id'] for i in items}:
            raise Conflict('Published ancestor membership differs from its private manifest')
        for model in raw['models']:
            existing = result.get(model['id'])
            useful = bool(model.get('criteria') or model.get('sources') or model.get('completed_stages'))
            if existing and (existing[2] or not useful):
                continue
            result[model['id']] = (model, dict(run_id=raw['run_id'], revision_id=raw['revision_id'],
                scoring_policy_version=model.get('scoring_policy_version'), model_hash=digest(model)), useful)
    return result


async def preflight_factor_revision(pool, artifacts, *, parent_collection_id, config, role_settings, data_dir):
    """Read-only production compatibility report, without keys or external I/O."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute('SET TRANSACTION READ ONLY')
        chain = await _ancestors(conn,parent_collection_id)
        parent,items = chain[0]
        prior = CollectionContext.model_validate(parent['context'])
        selected = tuple(i['model_id'] for i in items[:5])
        prefix,prompts = prompt_bundle('1.3')
        context = CollectionContext(plan=prior.plan,config=config,openai_pricing=prior.openai_pricing,
            tavily_pricing=prior.tavily_pricing,role_settings=role_settings,study_budget_usd=prior.study_budget_usd,
            purpose=prior.purpose,execution_version='collection-3',pipeline_prefix=prefix,pipeline_prompts=prompts,
            reassessment_of_collection_id=parent_collection_id,processing_order=selected,selected_model_ids=selected)
        retained = _retained_models(data_dir,chain)
        report = []
        for item in items[:5]:
            envelope = await freeze_upstream_manifest(conn,parent_run_id=item['run_id'],config=config,
                                                     context=context,artifacts=artifacts)
            manifest = envelope['manifest']
            ids = await PostgresLedger._known_model_runs(conn,parent['study_id'],item['model_id'])
            proofs = [await PostgresLedger._model_run_proof(conn,parent['study_id'],item['model_id'],identifier)
                      for identifier in sorted(ids)]
            cost = await (await conn.execute('''SELECT COALESCE(sum(CASE WHEN state='settled' THEN calculated_micro
                WHEN state IN ('reserved','unknown') THEN upper_micro ELSE 0 END),0) AS used
                FROM app.usage WHERE run_id=ANY(%s::text[])''',(sorted(ids),))).fetchone()
            report.append(dict(model_id=item['model_id'],title=item['candidate']['metadata']['title'],
                completed_through=manifest['completed_through'],manifest_hash=envelope['manifest_hash'],
                eligibility=manifest['state'].get('eligibility'),
                unknown_criteria=[c['criterion_id'] for c in manifest['state'].get('criteria',[]) if c.get('result')=='unknown'],
                ancestral_runs=len(ids),accounted_micro=int(cost['used']),
                family_limit_micro=min(micro(config.limits.run_cost_usd),micro('25'),*(p['run_limit_micro'] for p in proofs))))
        return dict(parent_collection_id=parent_collection_id,selected_count=5,catalog_count=len(retained),models=report,
                    mode='read_only_no_provider_calls')


async def create_factor_revision(pool, artifacts, *, parent_collection_id, config, role_settings, data_dir):
    if config.schema_version != '1.3' or config.scoring_policy_version != '1.5.3':
        raise ValueError('Factor revision requires milestone configuration 1.3 / policy 1.5.3')
    identifier, coordinator = uid(), uid()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(418724211)')
        chain = await _ancestors(conn, parent_collection_id)
        parent, items = chain[0]
        study = await (await conn.execute('SELECT * FROM app.studies WHERE id=%s FOR UPDATE', (parent['study_id'],))).fetchone()
        if await (await conn.execute("SELECT 1 FROM app.collections WHERE state IN ('discovering','running')")).fetchone():
            raise Conflict('Another collection is active')
        if await (await conn.execute('''SELECT 1 FROM app.run_leases l JOIN app.runs r ON r.id=l.run_id
            WHERE r.study_id=%s AND l.expires_at>clock_timestamp()''', (study['id'],))).fetchone():
            raise Conflict('The study still has an active owner')
        if await (await conn.execute('''SELECT 1 FROM app.usage u JOIN app.runs r ON r.id=u.run_id
            WHERE r.study_id=%s AND u.state IN ('reserved','unknown')''', (study['id'],))).fetchone():
            raise Conflict('The study has unsettled operations')
        prior = CollectionContext.model_validate(parent['context'])
        if prior.execution_version != 'collection-2' or tuple(i['model_id'] for i in items) != prior.processing_order:
            raise Conflict('The parent must be the intact original twenty-card priority reassessment')
        if (config.scenario != prior.config.scenario or config.market != prior.config.market
                or config.criteria != prior.config.criteria or config.model != prior.config.model
                or micro(prior.study_budget_usd) != study['limit_micro']
                or config.limits.run_cost_usd > min(prior.config.limits.run_cost_usd, 25)):
            raise Conflict('Factor revision cannot change the scenario, admission rules or monetary limits')
        usage = await (await conn.execute('''SELECT COALESCE(sum(CASE WHEN u.state='settled' THEN u.calculated_micro
            WHEN u.state IN ('reserved','unknown') THEN u.upper_micro ELSE 0 END),0) AS used
            FROM app.usage u JOIN app.runs r ON r.id=u.run_id WHERE r.study_id=%s''', (study['id'],))).fetchone()
        if usage['used'] >= study['limit_micro']:
            raise BudgetExceeded('Original study budget exhausted')
        selected = tuple(i['model_id'] for i in items[:5])
        prefix, prompts = prompt_bundle('1.3')
        context = CollectionContext(plan=prior.plan, config=config, openai_pricing=prior.openai_pricing,
            tavily_pricing=prior.tavily_pricing, role_settings=role_settings, study_budget_usd=prior.study_budget_usd,
            purpose=prior.purpose, execution_version='collection-3', pipeline_prefix=prefix, pipeline_prompts=prompts,
            reassessment_of_collection_id=parent_collection_id, processing_order=selected, selected_model_ids=selected)
        retained = _retained_models(data_dir, chain)
        packet = []
        for item in items:
            candidate = dict(item['candidate'])
            candidate['retained_assessment'] = retained[item['model_id']][1]
            if item['model_id'] in selected:
                if not item['run_id']:
                    raise Conflict('Selected model has no prior source run')
                manifest = await freeze_upstream_manifest(conn, parent_run_id=item['run_id'],
                    config=config, context=context, artifacts=artifacts)
                candidate.update(parent_run_id=item['run_id'], upstream_manifest=manifest,
                    metadata={**candidate['metadata'], 'scoring_policy_version':'1.5.3'})
                family = await (await conn.execute('''SELECT i.run_id FROM app.collection_items i
                    JOIN app.collections c ON c.id=i.collection_id WHERE c.study_id=%s AND i.model_id=%s
                    AND i.run_id IS NOT NULL ORDER BY c.created_at,i.run_id''', (study['id'],item['model_id']))).fetchall()
                await PostgresLedger.register_model_budget_family(conn, study_id=study['id'], model_id=item['model_id'],
                    ancestor_run_ids=tuple(r['run_id'] for r in family), limit=config.limits.run_cost_usd)
            packet.append(dict(model_id=item['model_id'], position=item['position'], candidate=candidate))
        value = context.model_dump(mode='json')
        await conn.execute('''INSERT INTO app.runs(id,study_id,config_json,config_hash,limit_micro,status,
            graph_version,state_schema_version,coordinator_thread_id)
            VALUES (%s,%s,%s,%s,%s,'selecting','scope-1','1',%s)''',
            (coordinator, study['id'], Jsonb(config.model_dump(mode='json')),config.digest(),study['limit_micro'],uid()))
        await conn.execute('''INSERT INTO app.collections(id,study_id,coordinator_run_id,context_hash,context,state,
            membership_hash,selected_count,frozen_at) VALUES (%s,%s,%s,%s,%s,'running',%s,20,clock_timestamp())''',
            (identifier,study['id'],coordinator,digest(value),Jsonb(value),digest(packet)))
        for item in packet:
            await conn.execute('''INSERT INTO app.collection_items(collection_id,model_id,position,candidate,state,public_model)
                VALUES (%s,%s,%s,%s,%s,%s)''', (identifier,item['model_id'],item['position'],Jsonb(item['candidate']),
                'queued' if item['model_id'] in selected else 'incomplete',Jsonb(retained[item['model_id']][0])))
        await PostgresLedger.event(conn,coordinator,'factor_revision_authorized',dict(
            collection_id=identifier,parent_collection_id=parent_collection_id,selected_model_ids=list(selected),
            selected_count=5,catalog_count=20,policy='1.5.3',membership_hash=digest(packet),
            budget_unchanged_micro=study['limit_micro'],already_accounted_micro=int(usage['used'])))
    return identifier


async def run(args):
    if not os.environ.get('DATABASE_URL'):
        raise ValueError('DATABASE_URL is required')
    if args.resume_collection and any((args.parent_collection,args.config,args.settings,args.create_only,args.preflight_only)):
        raise ValueError('Resume cannot alter the frozen revision')
    if not args.resume_collection and not all((args.parent_collection,args.config,args.settings)):
        raise ValueError('New revision needs parent, milestone configuration and saved role settings')
    directory = Path(os.environ.get('AI_BUILDER_DATA','outputs'))
    artifacts = ArtifactStore(directory/'evidence')
    async with execution_pool(os.environ['DATABASE_URL']) as pool:
        if args.preflight_only:
            result = await preflight_factor_revision(pool,artifacts,parent_collection_id=args.parent_collection,
                config=load_config(args.config),role_settings=json.loads(Path(args.settings).read_text()),data_dir=directory)
            print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
            return result
        identifier = args.resume_collection or await create_factor_revision(pool, artifacts,
            parent_collection_id=args.parent_collection,config=load_config(args.config),
            role_settings=json.loads(Path(args.settings).read_text()),data_dir=directory)
        print('Factor revision: '+identifier,flush=True)
        if args.create_only:
            return identifier
        result = await run_collection(pool,artifacts,resume_collection=identifier,data_dir=directory,
            openai_key=os.environ.get('OPENAI_API_KEY',''),tavily_key=os.environ.get('TAVILY_API_KEY',''),
            retry_failed=args.retry_failed)
        print(f'Updated {result.completed_count}/5; state={result.state}; '
              f'cumulative accounted=${result.usage["accounted_micro"]/1_000_000:.4f}',flush=True)
        return result


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for flag in ('parent-collection','config','settings','resume-collection'):
        cli.add_argument('--'+flag)
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument('--create-only',action='store_true')
    mode.add_argument('--preflight-only',action='store_true')
    cli.add_argument('--retry-failed',action='store_true')
    try:
        asyncio.run(run(cli.parse_args()))
    except Exception as error:
        cli.exit(1,f'Factor revision stopped: {_safe_failure(error)}. Inspect the private audit.\n')


if __name__ == '__main__':
    main()
