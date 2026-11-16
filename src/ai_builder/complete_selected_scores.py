"""Owner-authorized recovery of Docusign and KAYAK within one additional $10.

No discovery, replacement of original runs, global reranking, or historical
budget adjustment. Create once; resume always uses the same two saved runs.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from psycopg.types.json import Jsonb

from .collection import CollectionContext, _safe_failure
from .config import Configuration
from .evidence import ArtifactStore
from .pg_execution import acquire_lease, execution_pool, renew_lease
from .pg_ledger import PostgresLedger, digest
from .public import PublicSnapshot, public_model
from .research_scope import ResearchContext, insert_research_run, run_research
from .source_research import (RECOVERY_KAYAK_ID, RECOVERY_KAYAK_URLS, SourceResearchPlan,
                              verify_recovery_sources)
from .store import Conflict, uid
from .upstream_revision import load_upstream_manifest

DOCUSIGN='23c49394cadf4e0ab3b7f78d9d70373a'
TARGETS=(DOCUSIGN,RECOVERY_KAYAK_ID)
COMPLETED=('64a6c084e6e84158aba11efa123bdf4c','1f30cb99b362414bac6f9433b6f0d92b','ae9f63b524c74ecf9a0c661637d6328b')
POLICY='selected-score-recovery-1'
AUTHORIZATION={'policy':POLICY,'instruction':'Сделай, потрать не более 10 долларов дополнительно',
               'additional_limit_usd':'10','study_limit_usd':'100','model_ids':list(TARGETS)}


def sha(raw): return hashlib.sha256(raw).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()


def _json_spans(raw):
    """Locate original JSON value bytes; untouched model objects are never encoded again."""
    text=raw.decode('utf-8'); decoder=json.JSONDecoder(); top={}; models={}
    def space(pos):
        while pos<len(text) and text[pos].isspace(): pos+=1
        return pos
    pos=space(0)
    if text[pos]!='{': raise Conflict('Public snapshot must be an object')
    pos=space(pos+1)
    while text[pos]!='}':
        key,end=decoder.raw_decode(text,pos); pos=space(end)
        if not isinstance(key,str) or key in top or text[pos]!=':': raise Conflict('Ambiguous snapshot key')
        start=space(pos+1); value,end=decoder.raw_decode(text,start); top[key]=(start,end,value)
        if key=='models':
            if text[start]!='[': raise Conflict('Public models must be an array')
            cursor=space(start+1)
            while text[cursor]!=']':
                model,finish=decoder.raw_decode(text,cursor); identifier=model.get('id')
                if not identifier or identifier in models: raise Conflict('Duplicate public model')
                models[identifier]=(cursor,finish,model)
                cursor=space(finish)
                if text[cursor]==',': cursor=space(cursor+1)
                elif text[cursor]!=']': raise Conflict('Invalid model separator')
        pos=space(end)
        if text[pos]==',': pos=space(pos+1)
        elif text[pos]!='}': raise Conflict('Invalid snapshot separator')
    if space(pos+1)!=len(text): raise Conflict('Trailing snapshot content')
    return text,top,models


def model_payload_hashes(raw):
    text,_,models=_json_spans(raw)
    return {identifier:sha(text[start:end].encode()) for identifier,(start,end,_) in models.items()}


def replace_target(raw, model, revision_id, published_at):
    text,top,models=_json_spans(raw)
    identifier=model['id']
    if identifier not in TARGETS or len(models)!=20 or identifier not in models:
        raise Conflict('Only an existing recovery target may be published')
    changes=[(*models[identifier][:2],model)]
    updates={'revision_id':revision_id,'published_at':published_at}
    if 'retained_assessments' in top:
        retained=deepcopy(top['retained_assessments'][2]); retained.pop(identifier,None)
        updates['retained_assessments']=retained
    if 'update_states' in top:
        states=deepcopy(top['update_states'][2]); states[identifier]='completed'; updates['update_states']=states
    for key,value in updates.items():
        if key not in top: raise Conflict('Current snapshot lacks its required revision fields')
        changes.append((*top[key][:2],value))
    for start,end,value in sorted(changes,reverse=True):
        text=text[:start]+json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)+text[end:]
    result=text.encode(); PublicSnapshot.model_validate_json(result)
    before,after=model_payload_hashes(raw),model_payload_hashes(result)
    if any(before[key]!=after[key] for key in before if key!=identifier):
        raise Conflict('Untargeted public payload bytes changed')
    return result


def _write_bytes(path,raw):
    path=Path(path)
    if path.is_symlink(): raise Conflict('Public path is a symbolic link')
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.recovery-',delete=False) as stream:
            temporary=Path(stream.name); os.fchmod(stream.fileno(),0o644)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if temporary: temporary.unlink(missing_ok=True)


def _current_bytes(data_dir,collection_id):
    latest=json.loads((Path(data_dir)/'public/latest.json').read_text())
    path=Path(data_dir)/'public'/f'{collection_id}.json'
    if latest.get('run_id')!=collection_id or path.is_symlink() or path.stat().st_size>10_000_000:
        raise Conflict('Recovery requires the current intact published collection')
    raw=path.read_bytes(); public=PublicSnapshot.model_validate_json(raw)
    if public.run_id!=collection_id: raise Conflict('Published collection identity differs')
    return raw


async def _quiet(conn,study_id):
    if await (await conn.execute('''SELECT 1 FROM app.runs r LEFT JOIN app.run_leases l ON l.run_id=r.id
        WHERE r.study_id=%s AND (l.expires_at>clock_timestamp() OR r.active_started_at IS NOT NULL) LIMIT 1''',
        (study_id,))).fetchone(): raise Conflict('Study has an active worker or open clock')


async def _publication_quiet(conn,ledger,plan):
    await conn.execute('LOCK TABLE app.run_leases IN SHARE ROW EXCLUSIVE MODE')
    if await (await conn.execute('''SELECT 1 FROM app.runs r LEFT JOIN app.run_leases l ON l.run_id=r.id
        WHERE r.study_id=%s AND r.id<>%s AND (l.expires_at>clock_timestamp() OR r.active_started_at IS NOT NULL) LIMIT 1''',
        (plan['study_id'],ledger.run_id))).fetchone():
        raise Conflict('Another study worker or open clock prevents recovery publication')


async def _build_plan(conn,artifacts,data_dir,collection_id,allowance_id):
    collection=await (await conn.execute('SELECT * FROM app.collections WHERE id=%s',(collection_id,))).fetchone()
    if not collection or digest(collection['context'])!=collection['context_hash']:
        raise Conflict('Frozen collection is unavailable or changed')
    context=CollectionContext.model_validate(collection['context'])
    if context.execution_version!='collection-3' or set(context.selected_model_ids)!=set(TARGETS+COMPLETED):
        raise Conflict('Recovery is restricted to the original five selected identities')
    await _quiet(conn,collection['study_id'])
    items=await (await conn.execute('SELECT * FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                    (collection_id,))).fetchall()
    members=[{key:i[key] for key in ('model_id','position','candidate')} for i in items]
    if len(items)!=20 or digest(members)!=collection['membership_hash']:
        raise Conflict('Frozen collection membership differs')
    baseline=_current_bytes(data_dir,collection_id); snapshot=json.loads(baseline)
    public={m['id']:m for m in snapshot['models']}
    if set(public)!={i['model_id'] for i in items}:
        raise Conflict('Public and private catalogue identity differs')
    if any(not public[mid].get('score') or public[mid]['score'].get('value') is None for mid in COMPLETED):
        raise Conflict('The three completed scores must be retained as the publication baseline')
    plans=[]
    for identifier in TARGETS:
        item=next(i for i in items if i['model_id']==identifier)
        saved=await (await conn.execute('''SELECT r.config_json,r.config_hash,c.context,c.context_hash
            FROM app.runs r JOIN app.research_contexts c ON c.run_id=r.id WHERE r.id=%s''',(item['run_id'],))).fetchone()
        if not saved or digest(saved['context'])!=saved['context_hash']:
            raise Conflict('Target current-run context is unavailable or changed')
        config=Configuration.model_validate(saved['config_json'])
        old=ResearchContext.model_validate(saved['context'])
        if config!=context.config or config.digest()!=saved['config_hash'] or not old.upstream_manifest:
            raise Conflict('Target must retain its unchanged milestone config and explicit source ancestry')
        # This checks exact old source artifacts/checkpoint/receipts, without touching the failed current operation.
        _,inheritance=await load_upstream_manifest(conn,envelope=old.upstream_manifest,config=config,context=old,artifacts=artifacts)
        if identifier==DOCUSIGN and inheritance.completed_through!='reconcile':
            raise Conflict('Docusign recovery requires the completed compatible upstream prefix')
        value=old.model_dump(mode='json'); value['recovery_allowance_id']=allowance_id
        if identifier==RECOVERY_KAYAK_ID:
            source_id=old.upstream_manifest['manifest']['parent_run_id']
            seed=old.plan.seed.model_copy(update={'scope_id':uid(),'variant_id':uid()})
            plan=SourceResearchPlan(schema_version='4',seed=seed,queries=(),max_new_reads=3,
                acquisition_limits=old.plan.acquisition_limits,parser_limits=old.plan.parser_limits,
                priority_primary_urls=RECOVERY_KAYAK_URLS,legal_analogues=old.plan.legal_analogues,
                recovery_from_run_id=source_id)
            await verify_recovery_sources(conn,plan,artifacts,collection['study_id'])
            value.update(plan=plan.model_dump(mode='json'),upstream_manifest=None)
        new_context=ResearchContext.model_validate(value)
        plans.append(dict(model_id=identifier,parent_run_id=item['run_id'],
            config=config.model_dump(mode='json'),context=new_context.model_dump(mode='json'),
            metadata=item['candidate']['metadata']))
    return dict(policy=POLICY,allowance_id=allowance_id,collection_id=collection_id,
        study_id=collection['study_id'],coordinator_run_id=collection['coordinator_run_id'],
        context_hash=collection['context_hash'],membership_hash=collection['membership_hash'],
        baseline_snapshot_hash=sha(baseline),baseline_model_hashes=model_payload_hashes(baseline),
        parent_members=[dict(model_id=i['model_id'],run_id=i['run_id'],candidate_hash=digest(i['candidate'])) for i in items],
        baseline_snapshot=baseline.decode(),targets=plans)


async def preflight(pool,artifacts,data_dir,collection_id):
    async with pool.connection() as conn,conn.transaction():
        await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        plan=await _build_plan(conn,artifacts,data_dir,collection_id,uid())
    return dict(status='preflight',model_ids=list(TARGETS),new_document_reads=3,new_search_calls=0,
                additional_limit_usd='10',study_limit_usd='100',baseline_snapshot_hash=plan['baseline_snapshot_hash'])


async def create_recovery(pool,artifacts,data_dir,collection_id):
    audit=ArtifactStore(Path(data_dir)/'selected-recovery')
    async with pool.connection() as conn,conn.transaction():
        row=await (await conn.execute('SELECT study_id FROM app.collections WHERE id=%s',(collection_id,))).fetchone()
        if not row: raise Conflict('Unknown recovery collection')
        await conn.execute('SELECT id FROM app.studies WHERE id=%s FOR UPDATE',(row['study_id'],))
        await conn.execute('LOCK TABLE app.run_leases IN SHARE ROW EXCLUSIVE MODE')
        existing=await (await conn.execute('SELECT id FROM app.recovery_allowances WHERE collection_id=%s',(collection_id,))).fetchone()
        if existing: return existing['id']
        identifier=uid(); plan=await _build_plan(conn,artifacts,data_dir,collection_id,identifier)
        await PostgresLedger.create_recovery_allowance(conn,allowance_id=identifier,collection_id=collection_id,
            model_ids=TARGETS,limit='10',authorization=AUTHORIZATION,extend_model_id=DOCUSIGN)
        for target in plan['targets']:
            config=Configuration.model_validate(target['config']); context=ResearchContext.model_validate(target['context'])
            run_id=await insert_research_run(conn,config,context,plan['study_id'])
            await PostgresLedger.bind_recovery_run(conn,allowance_id=identifier,run_id=run_id,
                model_id=target['model_id'],parent_run_id=target['parent_run_id'])
            target['run_id']=run_id
        ref=audit.put(json.dumps(plan,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
        await PostgresLedger.event(conn,plan['coordinator_run_id'],'selected_recovery_created',
            {'policy':POLICY,'allowance_id':identifier,'manifest_ref':ref,'model_ids':list(TARGETS)})
    return identifier


async def _load_plan(pool,data_dir,allowance_id):
    async with pool.connection() as conn:
        allowance=await (await conn.execute('SELECT * FROM app.recovery_allowances WHERE id=%s',(allowance_id,))).fetchone()
        rows=await (await conn.execute("SELECT payload FROM app.events WHERE kind='selected_recovery_created' AND payload->>'allowance_id'=%s",
                                       (allowance_id,))).fetchall()
    if not allowance or len(rows)!=1: raise Conflict('Recovery manifest is unavailable or ambiguous')
    plan=json.loads(ArtifactStore(Path(data_dir)/'selected-recovery').get(rows[0]['payload']['manifest_ref']))
    if (plan['policy']!=POLICY or plan['allowance_id']!=allowance_id or plan['collection_id']!=allowance['collection_id']
            or [t['model_id'] for t in plan['targets']]!=list(TARGETS)):
        raise Conflict('Recovery manifest differs from its frozen allowance')
    return plan


async def _verify_members(conn,plan):
    row=await (await conn.execute('SELECT * FROM app.collections WHERE id=%s',(plan['collection_id'],))).fetchone()
    items=await (await conn.execute('SELECT model_id,run_id,candidate FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                    (plan['collection_id'],))).fetchall()
    if (not row or row['context_hash']!=plan['context_hash'] or row['membership_hash']!=plan['membership_hash']
            or [dict(model_id=i['model_id'],run_id=i['run_id'],candidate_hash=digest(i['candidate'])) for i in items]!=plan['parent_members']):
        raise Conflict('Original collection runs or candidates changed')


def _untouched(plan,raw):
    hashes=model_payload_hashes(raw)
    if (set(hashes)!=set(plan['baseline_model_hashes']) or any(hashes[mid]!=old for mid,old in plan['baseline_model_hashes'].items()
            if mid not in TARGETS)):
        raise Conflict('A non-target public model changed after recovery authorization')


async def _known_publications(conn,ledger,plan,audit,raw):
    """Only this immutable allowance's publication intents may change either target."""
    rows=await (await conn.execute("SELECT payload FROM app.events WHERE run_id=%s AND kind='selected_recovery_publication' AND payload->>'allowance_id'=%s",
                                  (ledger.run_id,plan['allowance_id']))).fetchall()
    allowed={mid:{plan['baseline_model_hashes'][mid]} for mid in TARGETS}
    snapshot_hashes={plan['baseline_snapshot_hash']}
    for row in rows:
        intent=json.loads(audit.get(row['payload']['intent_ref']))
        published=intent['next_raw'].encode(); _untouched(plan,published)
        identifier=row['payload']['model_id']
        if identifier not in TARGETS or intent['model']['id']!=identifier:
            raise Conflict('Publication intent has a different target identity')
        allowed[identifier].add(model_payload_hashes(published)[identifier])
        snapshot_hashes.add(sha(published))
    hashes=model_payload_hashes(raw)
    if any(hashes[mid] not in values for mid,values in allowed.items()):
        raise Conflict('A target changed outside this saved recovery allowance')
    return snapshot_hashes


async def _publish_result(ledger,plan,target,result,data_dir):
    audit=ArtifactStore(Path(data_dir)/'selected-recovery')
    config=Configuration.model_validate(target['config'])
    model=public_model(result,target['metadata'],config)
    if not model.get('eligible_for_ranking') or not model.get('score') or model['score'].get('value') is None:
        return False
    model['rank']=None  # No global reranking of the three completed scores.
    baseline_model=next(m for m in json.loads(plan['baseline_snapshot'])['models'] if m['id']==target['model_id'])
    for field in ('discovery_sources','queue_position'):
        if field in baseline_model: model[field]=baseline_model[field]
    async with ledger.locked(ledger.run_id,fenced=True) as (conn,_,__):
        await _publication_quiet(conn,ledger,plan)
        await _verify_members(conn,plan)
        saved=await (await conn.execute("SELECT payload FROM app.events WHERE run_id=%s AND kind='selected_recovery_publication' AND payload->>'recovery_run_id'=%s",
                                       (ledger.run_id,target['run_id']))).fetchone()
        if saved:
            intent=json.loads(audit.get(saved['payload']['intent_ref']))
            if intent['model']!=model: raise Conflict('Saved recovered projection differs')
        else:
            raw=_current_bytes(data_dir,plan['collection_id']); _untouched(plan,raw)
            await _known_publications(conn,ledger,plan,audit,raw)
            revision='recovery-'+digest({'allowance':plan['allowance_id'],'run':target['run_id']})[:32]
            updated=replace_target(raw,model,revision,now())
            intent=dict(previous_hash=sha(raw),previous_raw=raw.decode(),next_raw=updated.decode(),model=model)
            ref=audit.put(json.dumps(intent,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
            await conn.execute('UPDATE app.collection_items SET public_model=%s,updated_at=clock_timestamp() WHERE collection_id=%s AND model_id=%s',
                               (Jsonb(model),plan['collection_id'],target['model_id']))
            await ledger.event(conn,ledger.run_id,'selected_recovery_publication',dict(
                allowance_id=plan['allowance_id'],recovery_run_id=target['run_id'],model_id=target['model_id'],intent_ref=ref))
    # The immutable intent commits before local file replacement; resume repairs a crash here without another API call.
    async with ledger.locked(ledger.run_id,fenced=True) as (conn,_,__):
        await _publication_quiet(conn,ledger,plan)
        await _verify_members(conn,plan)
        raw=_current_bytes(data_dir,plan['collection_id']); _untouched(plan,raw)
        known=await _known_publications(conn,ledger,plan,audit,raw)
        updated=intent['next_raw'].encode()
        if raw!=updated:
            current=next(m for m in json.loads(raw)['models'] if m['id']==target['model_id'])
            if current==intent['model'] and sha(raw) in known:
                return True  # A later publication by this allowance already retained this target exactly.
            if sha(raw)!=intent['previous_hash']: raise Conflict('Public revision changed before targeted publication')
            previous=json.loads(raw)
            history=Path(data_dir)/'public/history'/plan['collection_id']/f"{previous['revision_id']}.json"
            if history.exists() and history.read_bytes()!=raw: raise Conflict('Public history already differs')
            if not history.exists(): _write_bytes(history,raw)
            _write_bytes(Path(data_dir)/'public'/f"{plan['collection_id']}.json",updated)
    return True


async def resume_recovery(pool,artifacts,data_dir,allowance_id,*,openai_key='',tavily_key='',provider_client=None,
                          fetch_document=None,parser=None):
    plan=await _load_plan(pool,data_dir,allowance_id)
    token=await acquire_lease(pool,plan['coordinator_run_id'],'selected-recovery')
    ledger=PostgresLedger(pool,plan['coordinator_run_id'],token)
    async def heartbeat():
        while True:
            await asyncio.sleep(15); await renew_lease(pool,ledger.run_id,token)
    async def process():
        statuses=[]
        for target in plan['targets']:
            async with ledger.locked(ledger.run_id,fenced=True) as (conn,_,__):
                await _publication_quiet(conn,ledger,plan)
                await _verify_members(conn,plan); _untouched(plan,_current_bytes(data_dir,plan['collection_id']))
                await _known_publications(conn,ledger,plan,ArtifactStore(Path(data_dir)/'selected-recovery'),
                                          _current_bytes(data_dir,plan['collection_id']))
                run=await (await conn.execute('SELECT status FROM app.runs WHERE id=%s',(target['run_id'],))).fetchone()
            if not run or run['status'] not in {'selecting','queued','running','partial','completed'}:
                statuses.append(dict(model_id=target['model_id'],status='requires_review')); continue
            print(json.dumps({'model_id':target['model_id'],'run_id':target['run_id'],'status':'starting'}),flush=True)
            try:
                options=dict(resume_run=target['run_id'],openai_key=openai_key,tavily_key=tavily_key,
                             provider_client=provider_client,fetch_document=fetch_document)
                if parser is not None: options['parser']=parser
                result=await run_research(pool,artifacts,**options)
                published=await _publish_result(ledger,plan,target,result,data_dir)
                status='published' if published else 'needs_review'
            except Exception as error:
                status=_safe_failure(error)
                async with ledger.locked(ledger.run_id,fenced=True) as (conn,_,__):
                    await ledger.event(conn,ledger.run_id,'selected_recovery_stopped',dict(
                        allowance_id=allowance_id,recovery_run_id=target['run_id'],model_id=target['model_id'],reason=status))
            accounting=await PostgresLedger(pool,target['run_id']).recovery_allowance()
            summary={key:accounting[key] for key in ('allowance_id','limit_micro','accounted_micro','remaining_micro') if key in accounting}
            current=json.loads(_current_bytes(data_dir,plan['collection_id']))
            published_model=next(m for m in current['models'] if m['id']==target['model_id'])
            statuses.append(dict(model_id=target['model_id'],status=status,
                                 score=published_model.get('score') if status=='published' else None,allowance=summary))
            print(json.dumps(statuses[-1]),flush=True)
        accounting=await PostgresLedger(pool,plan['targets'][0]['run_id']).recovery_allowance()
        return dict(allowance_id=allowance_id,models=statuses,
            allowance={key:accounting[key] for key in ('allowance_id','limit_micro','accounted_micro','remaining_micro') if key in accounting})
    work,heart=asyncio.create_task(process()),asyncio.create_task(heartbeat())
    try:
        done,_=await asyncio.wait({work,heart},return_when=asyncio.FIRST_COMPLETED)
        if heart in done: heart.result(); raise Conflict('Recovery coordinator heartbeat ended')
        return work.result()
    finally:
        work.cancel(); heart.cancel(); await asyncio.gather(work,heart,return_exceptions=True)
        async with pool.connection() as conn:
            await conn.execute('UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s AND token=%s',(ledger.run_id,token))


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    source=cli.add_mutually_exclusive_group(required=True)
    source.add_argument('--collection'); source.add_argument('--resume-allowance')
    mode=cli.add_mutually_exclusive_group()
    mode.add_argument('--preflight-only',action='store_true'); mode.add_argument('--create-only',action='store_true')
    args=cli.parse_args()
    if args.resume_allowance and (args.preflight_only or args.create_only): cli.error('Resume accepts only its frozen allowance')
    async def run():
        directory=Path(os.environ.get('AI_BUILDER_DATA','outputs')); artifacts=ArtifactStore(directory/'evidence')
        async with execution_pool(os.environ['DATABASE_URL']) as pool:
            if args.preflight_only: return await preflight(pool,artifacts,directory,args.collection)
            identifier=args.resume_allowance or await create_recovery(pool,artifacts,directory,args.collection)
            if args.create_only: return {'status':'created','allowance_id':identifier}
            return await resume_recovery(pool,artifacts,directory,identifier,openai_key=os.environ.get('OPENAI_API_KEY',''),
                                         tavily_key=os.environ.get('TAVILY_API_KEY',''))
    try: print(json.dumps(asyncio.run(run())),flush=True)
    except Exception as error: cli.exit(1,'Selected recovery stopped: '+_safe_failure(error)+'; inspect saved audit.\n')


if __name__=='__main__': main()
