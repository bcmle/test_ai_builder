"""Explicit, zero-provider correction of the reserved v1.5.3 timing sum.

Dry-run saves an immutable private plan; --apply requires that exact plan hash.
Original analytical records and usage are never changed.
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

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.types.json import Jsonb

from .collection import CollectionContext
from .config import Configuration
from .domain import score, MilestoneHandoffClaim
from .evidence import ArtifactStore
from .pg_execution import execution_pool
from .pg_ledger import digest
from .pipeline import (CORRECTION_PROMPT, CORRECTION_VERSION, FactorsV153, FactorsStateV153,
                       ModelRoles, ScopePacket, Verification, materialize_milestones,
                       validate_factor_verification)
from .public import load_snapshot, public_model, publish_snapshot
from .research_scope import ResearchContext, ResearchRunResult
from .source_research import PreparedScope
from .store import Conflict, uid

POLICY = 'milestone-arithmetic-revalidation-1'
TOTAL = 'factor-time-to-first-payment'
BLOCKER = 'factor_claim_unresolved:'+TOTAL


def response_candidate(response):
    if response.get('status')!='completed':
        raise ValueError('Saved response is not completed')
    chunks=[]
    for item in response.get('output',[]):
        for content in item.get('content',[]):
            if content.get('type')=='refusal':
                raise ValueError('Saved response is a refusal')
            if content.get('type')=='output_text':
                chunks.append(content['text'])
    text=''.join(chunks)
    if len(text)>131072:
        raise ValueError('Saved response exceeds its original parsing bound')
    return Verification.model_validate_json(text)


def provider_payload(context, packet):
    """Reconstruct the original request byte-for-byte in meaning, without a transport."""
    schema=deepcopy(Verification.model_json_schema())
    def strict(node):
        if isinstance(node,dict):
            if node.get('type')=='object':
                node['additionalProperties']=False
                node['required']=list(node.get('properties',{}))
            node.pop('default',None)
            for value in node.values(): strict(value)
        elif isinstance(node,list):
            for value in node: strict(value)
    strict(schema)
    system=context.pipeline_prefix+context.pipeline_prompts['verifier']
    if 'output_correction' in packet: system+=CORRECTION_PROMPT
    settings=context.role_settings['verifier']
    payload=dict(model=context.openai_pricing.model,store=False,instructions=system,
        input=[dict(role='user',content=json.dumps(packet,ensure_ascii=False))],
        reasoning=dict(effort=settings['reasoning_effort']),max_output_tokens=settings['max_output_tokens'],
        text=dict(format=dict(type='json_schema',name='Verification',strict=True,schema=schema)))
    if context.openai_pricing.model=='gpt-6-astra':
        payload.update(prompt_cache_options={'mode':'explicit'},service_tier='default')
    return payload


def recover_state(state, scope, config, candidate, validated_at):
    """Only a whole independently produced result may replace the derived projection."""
    if (state.get('card',{}).get('publication_blockers')!=[BLOCKER]
            or state.get('factor_neutral') is not True
            or state.get('eligibility') not in {'confirmed','conditional'}):
        raise Conflict('The reserved arithmetic must be the only publication blocker')
    factors=FactorsStateV153.model_validate(state['factors'])
    validate_factor_verification(candidate,factors,market=config.market,milestones=True)
    statuses={item.claim_id:item.status for item in candidate.items}
    if any(statuses[claim.claim_id] not in ({'supported','not_fact_checkable'}
           if claim.kind=='judgment' else {'supported'}) for claim in factors.claims):
        raise Conflict('Saved verification retains a substantive publication blocker')
    if (any(factor.value is None for factor in factors.factors) or factors.days is None
            or factors.cost is None or not factors.test_plan_claim_ids):
        raise Conflict('A complete unchanged factor assessment is required')
    result=score({factor.factor_id:factor.value for factor in factors.factors},
        {factor.id:factor.weight for factor in config.factors},days=factors.days,cost=factors.cost,
        budget=config.scenario.business_budget_usd,resources=factors.resources,checks_complete=True,
        conditional=state['eligibility']=='conditional')
    updated=deepcopy(state)
    updated['factor_verification']=candidate.model_dump(mode='json')
    card=updated['card']
    card.update(score=result.model_dump(mode='json'),publication='ready',publication_blockers=[],
                analysis_status='complete',validated_at=validated_at)
    if 'card_validation' not in card['completed_stages']:
        card['completed_stages'].append('card_validation')
    return updated


async def quiet(conn, collection, run_ids):
    active=await (await conn.execute('''SELECT 1 FROM app.runs r
        LEFT JOIN app.run_leases l ON l.run_id=r.id
        WHERE r.study_id=%s AND (l.expires_at>clock_timestamp() OR r.active_started_at IS NOT NULL) LIMIT 1''',
        (collection['study_id'],))).fetchone()
    if active: raise Conflict('Study has an active worker or open execution clock')
    pending=await (await conn.execute('''SELECT 1 FROM app.usage WHERE run_id=ANY(%s::text[])
        AND state IN ('reserved','unknown') UNION ALL SELECT 1 FROM app.provider_operations
        WHERE run_id=ANY(%s::text[]) AND state<>'completed' LIMIT 1''',(run_ids,run_ids))).fetchone()
    if pending: raise Conflict('A selected model has unresolved external operations or usage')


async def prepare(conn, data_dir, collection_id, model_ids, *, validated_at, revision_id):
    collection=await (await conn.execute('SELECT * FROM app.collections WHERE id=%s',(collection_id,))).fetchone()
    if not collection or digest(collection['context'])!=collection['context_hash']:
        raise Conflict('Frozen collection context unavailable or changed')
    context=CollectionContext.model_validate(collection['context'])
    if context.execution_version!='collection-3' or context.config.scoring_policy_version!='1.5.3':
        raise Conflict('Only the selected milestone revision is supported')
    if not model_ids or len(model_ids)!=len(set(model_ids)) or not set(model_ids)<=set(context.selected_model_ids):
        raise Conflict('Explicit distinct selected model IDs are required')
    items=await (await conn.execute('SELECT * FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                   (collection_id,))).fetchall()
    membership=[{key:item[key] for key in ('model_id','position','candidate')} for item in items]
    if digest(membership)!=collection['membership_hash']:
        raise Conflict('Frozen collection membership changed')
    selected=[item for item in items if item['model_id'] in model_ids]
    if len(selected)!=len(model_ids) or any(item['state']!='completed' for item in selected):
        raise Conflict('Only completed selected models can be corrected')
    await quiet(conn,collection,[item['run_id'] for item in selected])
    previous=load_snapshot(data_dir,collection_id)
    latest=json.loads((Path(data_dir)/'public'/'latest.json').read_text())
    if not previous or latest.get('run_id')!=collection_id:
        raise Conflict('Correction requires the current published collection')
    corrections=[]
    for item in selected:
        run_id=item['run_id']
        saved=await (await conn.execute('''SELECT r.*,s.scope,s.role_settings,s.pricing,s.purpose,
            c.context,c.context_hash,g.thread_id,g.input_hash AS graph_input_hash
            FROM app.runs r JOIN app.scope_inputs s ON s.run_id=r.id
            JOIN app.research_contexts c ON c.run_id=r.id JOIN app.graph_threads g ON g.run_id=r.id
            AND g.scope_id=s.scope->>'scope_id' WHERE r.id=%s''',(run_id,))).fetchone()
        if not saved or saved['status'] not in {'partial','completed'} or saved['study_id']!=collection['study_id']:
            raise Conflict('Completed model run is unavailable')
        config=Configuration.model_validate(saved['config_json'])
        research=ResearchContext.model_validate(saved['context'])
        scope=ScopePacket.model_validate(saved['scope'])
        roles=ModelRoles(None,research.openai_pricing,'',research.role_settings,purpose=research.purpose,
                         prompt_prefix=research.pipeline_prefix,prompts=research.pipeline_prompts)
        if (config.digest()!=saved['config_hash'] or config!=context.config
                or digest(saved['context'])!=saved['context_hash'] or scope.base_model_id!=item['model_id']
                or saved['role_settings']!=research.role_settings
                or saved['pricing']!=research.openai_pricing.model_dump(mode='json') or saved['purpose']!=research.purpose):
            raise Conflict('Frozen model inputs differ')
        for doc in scope.documents:
            if hashlib.sha256(doc.text.encode()).hexdigest()!=doc.content_hash:
                raise Conflict('Frozen document content changed')
        inheritance=research.upstream_manifest
        if not inheritance or digest(inheritance['manifest'])!=inheritance['manifest_hash']:
            raise Conflict('Frozen inherited prefix is unavailable')
        parent=inheritance['manifest']
        inputs=dict(scope=scope.model_dump(mode='json'),config=config.digest(),roles=roles.version(),
            inheritance=dict(manifest_hash=inheritance['manifest_hash'],parent_run_id=parent['parent_run_id'],
                             completed_through=parent['completed_through'],state=parent['state']))
        if digest(inputs)!=saved['graph_input_hash']:
            raise Conflict('Frozen graph input hash differs')
        checkpoint=await AsyncPostgresSaver(conn).aget_tuple({'configurable':{'thread_id':saved['thread_id']}})
        if not checkpoint: raise Conflict('Completed checkpoint unavailable')
        state=deepcopy(checkpoint.checkpoint['channel_values'])
        receipts=await (await conn.execute('SELECT * FROM app.node_receipts WHERE run_id=%s',(run_id,))).fetchall()
        if not any(r['node_name']=='card' and r['result']==state.get('card') for r in receipts):
            raise Conflict('Checkpoint card lacks its immutable accepted receipt')
        factors=FactorsStateV153.model_validate(state['factors'])
        if not any(r['node_name']=='factors' and materialize_milestones(FactorsV153.model_validate(r['result']),config.market)==factors
                   for r in receipts):
            raise Conflict('Checkpoint factors differ from their accepted receipt')
        if not any(r['node_name']=='verifier' and r['result']==state.get('factor_verification') for r in receipts):
            raise Conflict('Checkpoint verification lacks its accepted receipt')
        gate=state.get('factor_projection_audit',{}).get('neutrality')
        if not gate or gate.get('status')!='accept' or not any(r['node_name']=='factor_neutrality' and r['result']==gate for r in receipts):
            raise Conflict('Accepted factor-neutrality receipt unavailable')
        projected=[MilestoneHandoffClaim.model_validate(c.model_dump(include=set(MilestoneHandoffClaim.model_fields))).model_dump(mode='json')
                   for c in factors.claims]
        if projected!=state.get('factor_handoff'):
            raise Conflict('Factor handoff differs from unchanged author premises')
        docs={doc.evidence_id:doc for doc in scope.documents}
        allowed={ref.evidence_id for c in factors.claims for ref in c.evidence_refs}
        packet=dict(assessment_scope_id=scope.scope_id,base_model_id=scope.base_model_id,variant_id=scope.variant_id,
            neutral_model_description=scope.neutral_model_description,launch_context=config.scenario.model_dump(mode='json'),
            claims=projected,documents=[docs[eid].model_dump(mode='json') for eid in sorted(allowed)])
        events=await (await conn.execute('''SELECT e.id,e.payload,o.input_hash,o.state,o.response
            FROM app.events e JOIN app.provider_operations o ON o.run_id=e.run_id
            AND o.operation_key=e.payload->>'operation_key'
            WHERE e.run_id=%s AND e.kind='role_input' AND e.payload->>'role'='verifier' ORDER BY e.id''',(run_id,))).fetchall()
        chosen=None; failures=[]
        for event in events:
            raw=event['payload']; sent=raw['input']
            if any(c.get('claim_id')==TOTAL for c in sent.get('claims',[])) is False: continue
            attempt=raw.get('correction_attempt')
            if type(attempt) is not int or attempt not in (0,1,2):
                raise Conflict('Invalid saved correction attempt')
            without_correction={key:value for key,value in sent.items() if key!='output_correction'}
            suffix='factor-premises'+(f':{CORRECTION_VERSION}:{attempt}' if attempt else '')
            role_hash=digest(dict(input=sent,scope=saved['graph_input_hash'],role='verifier',suffix=suffix))
            # JSONB does not retain insertion order, but the provider payload embeds
            # the packet as a JSON string. Rebuild its original schema-defined order.
            wire_packet=deepcopy(packet)
            if attempt:
                correction=sent.get('output_correction',{})
                invalid=correction.get('invalid_output')
                if isinstance(invalid,dict):
                    invalid=Verification.model_validate(invalid).model_dump(mode='json')
                diagnostics=[{key:entry[key] for key in ('code','path','message')} for entry in correction.get('validation_errors',[])]
                wire_packet['output_correction']=dict(policy_version=correction.get('policy_version'),attempt=attempt,
                    original_input_hash=correction.get('original_input_hash'),invalid_output=invalid,validation_errors=diagnostics)
                if (wire_packet['output_correction']!=correction or correction.get('policy_version')!=CORRECTION_VERSION
                        or correction.get('original_input_hash')!=digest(packet)):
                    raise Conflict('Saved correction packet differs from the frozen original')
            payload=provider_payload(research,wire_packet)
            request_hash=digest(dict(endpoint='responses',payload=payload,agent='verifier',purpose=research.purpose,
                                     pricing=research.openai_pricing.model_dump(mode='json')))
            if (without_correction!=packet or raw.get('prompt_version')!=roles.version()
                    or raw.get('operation_key')!=scope.scope_id+':verifier:'+role_hash
                    or event['input_hash']!=request_hash or event['state']!='completed'):
                raise Conflict('Saved verifier operation differs from its exact frozen payload')
            try:
                candidate=response_candidate(event['response'])
                validate_factor_verification(candidate,factors,market=config.market,milestones=True)
            except (ValueError,TypeError,KeyError) as error:
                failures.append(dict(event_id=event['id'],response_hash=digest(event['response']),error=type(error).__name__))
                continue
            # First structurally valid whole candidate is final, even if its substantive status blocks publication.
            corrected=recover_state(state,scope,config,candidate,validated_at)
            chosen=dict(event_id=event['id'],operation_key=raw['operation_key'],request_hash=request_hash,
                        response_hash=digest(event['response']),verification=candidate.model_dump(mode='json'))
            break
        if chosen is None: raise Conflict('No previously paid complete verification passes the corrected contract')
        metadata_rows=await (await conn.execute('SELECT id,metadata FROM app.source_snapshots WHERE id=ANY(%s::text[])',
                                              ([doc.snapshot_id for doc in scope.documents],))).fetchall()
        corrected['source_metadata']={row['id']:row['metadata'] for row in metadata_rows}
        # Acquisition gaps are public context, not an input to arithmetic. Preserve existing source-gap limitations below.
        result=ResearchRunResult(run_id,PreparedScope(scope,()),corrected,{},Path(data_dir),True)
        new_model=public_model(result,item['candidate']['metadata'],config)
        original=item['public_model']
        old_public=next((m for m in previous['models'] if m['id']==item['model_id']),None)
        if old_public is None or {**old_public,'rank':None}!={**original,'rank':None}:
            raise Conflict('Published model differs from its saved projection')
        new_model['limitations']=[value for value in original['limitations']
            if value!='Publication checks are incomplete. This model is excluded from the final ranking.']
        for key in ('discovery_sources','queue_position'):
            if key in original: new_model[key]=original[key]
        corrections.append(dict(model_id=item['model_id'],run_id=run_id,checkpoint_id=checkpoint.checkpoint['id'],
            checkpoint_hash=digest(state),scope_hash=digest(saved['scope']),config_hash=config.digest(),
            context_hash=saved['context_hash'],graph_input_hash=saved['graph_input_hash'],factors_hash=digest(state['factors']),
            prior_card_hash=digest(state['card']),prior_projection_hash=digest(original),prior_invalid_candidates=failures,
            selected=chosen,card=corrected['card'],public_model=new_model))
    snapshot=deepcopy(previous)
    snapshot.update(revision_id=revision_id,published_at=validated_at)
    by_model={correction['model_id']:correction['public_model'] for correction in corrections}
    snapshot['models']=[by_model.get(model['id'],model) for model in snapshot['models']]
    return dict(policy=POLICY,collection_id=collection_id,coordinator_run_id=collection['coordinator_run_id'],
        study_id=collection['study_id'],context_hash=collection['context_hash'],membership_hash=collection['membership_hash'],
        model_ids=list(model_ids),validated_at=validated_at,revision_id=revision_id,
        previous_snapshot_hash=digest(previous),corrections=corrections,snapshot=snapshot)


async def revalidate(pool,data_dir,collection_id,model_ids=(),apply_hash=None):
    artifacts=ArtifactStore(Path(data_dir)/'arithmetic-corrections')
    if not apply_hash:
        async with pool.connection() as conn,conn.transaction():
            await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            plan=await prepare(conn,data_dir,collection_id,model_ids,
                validated_at=datetime.now(timezone.utc).isoformat(),revision_id='arithmetic-'+uid())
        ref=artifacts.put(json.dumps(plan,sort_keys=True,separators=(',',':')).encode())
        return dict(status='dry_run',plan_hash=ref,models=[dict(id=c['model_id'],score=c['card']['score']) for c in plan['corrections']])
    plan=json.loads(artifacts.get(apply_hash))
    if plan.get('policy')!=POLICY or plan.get('collection_id')!=collection_id:
        raise Conflict('Apply requires a matching arithmetic dry-run artifact')
    async with pool.connection() as conn,conn.transaction():
        # Blocks acquisition/renewal while checking quiescence and committing the public projections.
        await conn.execute('LOCK TABLE app.run_leases IN SHARE ROW EXCLUSIVE MODE')
        await conn.execute('SELECT id FROM app.studies WHERE id=%s FOR UPDATE',(plan['study_id'],))
        await conn.execute('SELECT id FROM app.collections WHERE id=%s FOR UPDATE',(collection_id,))
        recorded=await (await conn.execute('''SELECT payload FROM app.events WHERE run_id=%s
            AND kind='milestone_arithmetic_revalidated' AND payload->>'plan_hash'=%s''',
            (plan['coordinator_run_id'],apply_hash))).fetchone()
        if not recorded:
            fresh=await prepare(conn,data_dir,collection_id,plan['model_ids'],
                                validated_at=plan['validated_at'],revision_id=plan['revision_id'])
            if fresh!=plan: raise Conflict('Dry-run inputs changed; create a new dry-run')
            for correction in plan['corrections']:
                await conn.execute('UPDATE app.collection_items SET public_model=%s,updated_at=clock_timestamp() WHERE collection_id=%s AND model_id=%s',
                    (Jsonb(correction['public_model']),collection_id,correction['model_id']))
            await conn.execute('INSERT INTO app.events(run_id,kind,payload) VALUES (%s,%s,%s)',
                (plan['coordinator_run_id'],'milestone_arithmetic_revalidated',Jsonb(dict(policy=POLICY,plan_hash=apply_hash,
                    revision_id=plan['revision_id'],model_ids=plan['model_ids'],provider_calls=0))))
        else:
            collection=await (await conn.execute('SELECT * FROM app.collections WHERE id=%s',(collection_id,))).fetchone()
            await quiet(conn,collection,[c['run_id'] for c in plan['corrections']])
            for correction in plan['corrections']:
                row=await (await conn.execute('SELECT public_model FROM app.collection_items WHERE collection_id=%s AND model_id=%s',
                                             (collection_id,correction['model_id']))).fetchone()
                if not row or row['public_model']!=correction['public_model']:
                    raise Conflict('Previously corrected projection changed')
    # DB projection/audit is committed first; retrying this exact artifact repairs an interrupted file publication.
    async with pool.connection() as conn,conn.transaction():
        await conn.execute('LOCK TABLE app.run_leases IN SHARE ROW EXCLUSIVE MODE')
        await conn.execute('SELECT id FROM app.studies WHERE id=%s FOR UPDATE',(plan['study_id'],))
        collection=await (await conn.execute('SELECT * FROM app.collections WHERE id=%s FOR UPDATE',(collection_id,))).fetchone()
        await quiet(conn,collection,[c['run_id'] for c in plan['corrections']])
        latest=json.loads((Path(data_dir)/'public'/'latest.json').read_text())
        previous=load_snapshot(data_dir,collection_id)
        if latest.get('run_id')!=collection_id or previous is None:
            raise Conflict('Current published collection changed')
        if previous.get('revision_id')!=plan['revision_id']:
            if digest(previous)!=plan['previous_snapshot_hash']:
                raise Conflict('Public snapshot changed before correction publication')
            publish_snapshot(data_dir,plan['snapshot'])
        else:
            def unranked(snapshot):
                copy=deepcopy(snapshot)
                for model in copy['models']: model['rank']=None
                return copy
            if unranked(previous)!=unranked(plan['snapshot']):
                raise Conflict('Previously published correction differs from its saved plan')
    return dict(status='applied',plan_hash=apply_hash,revision_id=plan['revision_id'],provider_calls=0)


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--collection',required=True)
    cli.add_argument('--model-id',action='append',default=[])
    cli.add_argument('--apply',dest='apply_hash')
    args=cli.parse_args()
    if args.apply_hash and args.model_id: cli.error('--apply uses only the IDs in its saved dry-run')
    async def run():
        async with execution_pool(os.environ['DATABASE_URL']) as pool:
            return await revalidate(pool,Path(os.environ.get('AI_BUILDER_DATA','data')),args.collection,args.model_id,args.apply_hash)
    try: print(json.dumps(asyncio.run(run())))
    except Exception as error: cli.exit(1,f'Arithmetic correction stopped: {type(error).__name__}: {error}\n')


if __name__=='__main__': main()
