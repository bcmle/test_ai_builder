"""Explicit KAYAK cached-only completion; no HTTP client, credentials or new calls.

Read-only preflight must validate the original initial whole Verifier response.
Apply appends the missing graph result and uses the existing targeted publisher.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from .complete_selected_scores import (_load_plan,_verify_members,_current_bytes,_untouched,
    _known_publications,_publish_result,RECOVERY_KAYAK_ID)
from .collection import _safe_failure
from .config import Configuration
from .domain import MilestoneHandoffClaim
from .evidence import ArtifactStore
from .pg_execution import acquire_lease,execution_pool
from .pg_ledger import PostgresLedger,digest
from .pipeline import (FactorsV153,FactorsStateV153,ModelRoles,Neutrality,Verification,
    ScopePacket,materialize_milestones,validate_factor_verification,build_scope_graph,execute_scope)
from .research_scope import ResearchContext,ResearchRunResult
from .revalidate_milestone_arithmetic import provider_payload,response_candidate
from .source_research import _materialize,_result_rows
from .store import Conflict

POLICY='kayak-cached-verifier-replay-1'


def roles_version(context):
    return ModelRoles(None,context.openai_pricing,'',context.role_settings,purpose=context.purpose,
        prompt_prefix=context.pipeline_prefix,prompts=context.pipeline_prompts).version()


def request_hash(context,packet):
    return digest(dict(endpoint='responses',payload=provider_payload(context,packet),agent='verifier',
                       purpose=context.purpose,pricing=context.openai_pricing.model_dump(mode='json')))


class SavedVerifierRoles:
    """No transport exists. A changed role, input, schema or operation cannot execute."""
    def __init__(self,context,proof):
        self.context,self.proof=context,proof
    def version(self): return roles_version(self.context)
    async def generate(self,role,packet,schema,operation_key):
        if (role!='verifier' or schema is not Verification or operation_key!=self.proof['operation_key']
                or request_hash(self.context,packet)!=self.proof['request_hash']
                or digest(self.proof['response'])!=self.proof['response_hash']):
            raise Conflict('Cached-only replay refuses any unproved operation')
        return response_candidate(deepcopy(self.proof['response']))


async def _quiet(conn,study_id,allowed=()):
    if await (await conn.execute('''SELECT 1 FROM app.runs r LEFT JOIN app.run_leases l ON l.run_id=r.id
        WHERE r.study_id=%s AND (r.active_started_at IS NOT NULL OR
        (l.expires_at>clock_timestamp() AND NOT(r.id=ANY(%s::text[])))) LIMIT 1''',
        (study_id,list(allowed)))).fetchone():
        raise Conflict('Cached replay requires no other worker or open execution clock')


async def _proof(conn,plan,target,artifacts,*,allowed_leases=()):
    await _verify_members(conn,plan)
    await _quiet(conn,plan['study_id'],allowed_leases)
    run=await (await conn.execute('SELECT * FROM app.runs WHERE id=%s',(target['run_id'],))).fetchone()
    context_row=await (await conn.execute('SELECT * FROM app.research_contexts WHERE run_id=%s',(target['run_id'],))).fetchone()
    binding=await (await conn.execute('SELECT * FROM app.recovery_runs WHERE run_id=%s',(target['run_id'],))).fetchone()
    if (not run or run['study_id']!=plan['study_id'] or run['status'] not in {'paused','running','partial','completed'}
            or not context_row or digest(context_row['context'])!=context_row['context_hash']
            or context_row['context']!=target['context'] or run['config_json']!=target['config']
            or not binding or binding['allowance_id']!=plan['allowance_id']
            or binding['model_id']!=RECOVERY_KAYAK_ID or binding['parent_run_id']!=target['parent_run_id']
            or binding['context_hash']!=context_row['context_hash'] or binding['config_hash']!=run['config_hash']):
        raise Conflict('Cached replay requires the exact signed recovery binding and frozen inputs')
    config=Configuration.model_validate(run['config_json']);context=ResearchContext.model_validate(context_row['context'])
    if (config.digest()!=run['config_hash'] or context.plan.schema_version!='4' or context.upstream_manifest is not None
            or context.recovery_allowance_id!=plan['allowance_id']):
        raise Conflict('Cached replay is restricted to the same KAYAK source recovery')
    # Also validate the immutable authorization, both run memberships and cumulative budget provenance.
    await PostgresLedger._recovery_budget(conn,run)
    if await (await conn.execute('''SELECT 1 FROM app.usage WHERE run_id=%s AND state IN ('reserved','unknown')
        UNION ALL SELECT 1 FROM app.provider_operations WHERE run_id=%s AND state<>'completed'
        UNION ALL SELECT 1 FROM app.source_fetch_attempts WHERE run_id=%s AND state IN ('running','unknown') LIMIT 1''',
        (run['id'],run['id'],run['id']))).fetchone():
        raise Conflict('Cached replay cannot resolve unknown external work')
    source=await (await conn.execute('SELECT * FROM app.source_research WHERE run_id=%s AND scope_id=%s',
                                    (run['id'],context.plan.seed.scope_id))).fetchone()
    if (not source or source['state']!='frozen' or source['plan']!=context.plan.model_dump(mode='json')
            or source['plan_hash']!=digest(source['plan'])
            or source['packet_hash']!=digest({'scope':source['packet'],'gaps':source['gaps']})):
        raise Conflict('Frozen KAYAK sources are unavailable or changed')
    prepared=_materialize(context.plan,await _result_rows(conn,run['id'],context.plan.seed.scope_id),artifacts)
    if (prepared.scope.model_dump(mode='json')!=source['packet']
            or [g.model_dump(mode='json') for g in prepared.gaps]!=source['gaps']):
        raise Conflict('Source artifacts differ from their frozen packet')
    scope=prepared.scope
    inputs=await (await conn.execute('SELECT * FROM app.scope_inputs WHERE run_id=%s',(run['id'],))).fetchone()
    thread=await (await conn.execute('SELECT * FROM app.graph_threads WHERE run_id=%s AND scope_id=%s',(run['id'],scope.scope_id))).fetchone()
    graph_hash=digest(dict(scope=scope.model_dump(mode='json'),config=config.digest(),roles=roles_version(context)))
    if (not inputs or inputs['scope']!=source['packet'] or inputs['role_settings']!=context.role_settings
            or inputs['pricing']!=context.openai_pricing.model_dump(mode='json') or inputs['purpose']!=context.purpose
            or not thread or thread['input_hash']!=graph_hash):
        raise Conflict('Frozen graph inputs differ')
    checkpoint=await AsyncPostgresSaver(conn).aget_tuple({'configurable':{'thread_id':thread['thread_id']}})
    if not checkpoint: raise Conflict('Saved graph checkpoint unavailable')
    state=deepcopy(checkpoint.checkpoint['channel_values'])
    factors=FactorsStateV153.model_validate(state['factors'])
    receipts=await (await conn.execute('SELECT * FROM app.node_receipts WHERE run_id=%s',(run['id'],))).fetchall()
    if (state.get('eligibility') not in {'confirmed','conditional'} or not any(r['node_name']=='factors'
            and materialize_milestones(FactorsV153.model_validate(r['result']),config.market)==factors for r in receipts)):
        raise Conflict('Saved Factors or eligibility lacks its accepted upstream result')
    projected=[MilestoneHandoffClaim.model_validate(c.model_dump(include=set(MilestoneHandoffClaim.model_fields))) for c in factors.claims]
    by_id={c.claim_id:c for c in projected}
    gate_packet=dict(assessment_scope_id=scope.scope_id,neutral_texts=[c.neutral_text for c in projected],
        reference_conditions=state['eligibility_conditions'],condition_claims=[dict(condition_id=r.condition_id,
        claim_id=r.claim_id,neutral_text=by_id[r.claim_id].neutral_text) for r in factors.condition_claims])
    gate_hash=digest(dict(input=gate_packet,scope=graph_hash,role='factor_neutrality',suffix='factor-premises'))
    gate_key=scope.scope_id+':factor_neutrality:'+gate_hash
    if not any(r['node_name']=='factor_neutrality' and r['invocation_key']==gate_key and r['input_hash']==gate_hash
               and Neutrality.model_validate(r['result']).status=='accept' for r in receipts):
        raise Conflict('The exact accepted factor-neutrality receipt is missing')
    docs={d.evidence_id:d for d in scope.documents};evidence={r.evidence_id for c in factors.claims for r in c.evidence_refs}
    packet=dict(assessment_scope_id=scope.scope_id,base_model_id=scope.base_model_id,variant_id=scope.variant_id,
        neutral_model_description=scope.neutral_model_description,launch_context=config.scenario.model_dump(mode='json'),
        claims=[c.model_dump(mode='json') for c in projected],documents=[docs[e].model_dump(mode='json') for e in sorted(evidence)])
    role_hash=digest(dict(input=packet,scope=graph_hash,role='verifier',suffix='factor-premises'))
    key=scope.scope_id+':verifier:'+role_hash; wire_hash=request_hash(context,packet)
    operation=await (await conn.execute('SELECT * FROM app.provider_operations WHERE run_id=%s AND operation_key=%s',(run['id'],key))).fetchone()
    original=await (await conn.execute("SELECT payload FROM app.events WHERE run_id=%s AND kind='role_input' AND payload->>'operation_key'=%s ORDER BY id LIMIT 1",(run['id'],key))).fetchone()
    if (not operation or operation['state']!='completed' or operation['input_hash']!=wire_hash or not original
            or original['payload'].get('correction_attempt')!=0 or original['payload'].get('input')!=packet
            or original['payload'].get('prompt_version')!=roles_version(context)):
        raise Conflict('Original whole Verifier response does not match the exact frozen request')
    candidate=response_candidate(operation['response'])
    validate_factor_verification(candidate,factors,market=config.market,milestones=True,business_budget_usd=config.scenario.business_budget_usd)
    statuses={item.claim_id:item.status for item in candidate.items}
    if any(statuses[c.claim_id] not in ({'supported','not_fact_checkable'} if c.kind=='judgment' else {'supported'}) for c in factors.claims):
        raise Conflict('Original whole Verifier response retains a substantive blocker')
    usage=await (await conn.execute('SELECT * FROM app.usage WHERE run_id=%s ORDER BY request_id',(run['id'],))).fetchall()
    # JSON-safe stable snapshots of original money and external outcomes.
    money=[{k:r[k] for k in ('request_id','state','upper_micro','calculated_micro','billed_micro')} for r in usage]
    operations=await (await conn.execute('SELECT operation_key,input_hash,state,response FROM app.provider_operations WHERE run_id=%s ORDER BY operation_key',(run['id'],))).fetchall()
    proof=dict(policy=POLICY,run_id=run['id'],checkpoint_id=checkpoint.checkpoint['id'],state_hash=digest(state),
        source_packet_hash=source['packet_hash'],context_hash=context_row['context_hash'],graph_hash=graph_hash,
        operation_key=key,request_hash=wire_hash,response_hash=digest(operation['response']),response=operation['response'],
        usage=money,operations_hash=digest(operations))
    return proof,config,context,prepared,state


async def replay(pool,artifacts,data_dir,allowance_id,*,preflight_only=False):
    plan=await _load_plan(pool,data_dir,allowance_id)
    target=next(t for t in plan['targets'] if t['model_id']==RECOVERY_KAYAK_ID)
    async with pool.connection() as conn,conn.transaction():
        await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        proof,*_=await _proof(conn,plan,target,artifacts)
        raw=_current_bytes(data_dir,plan['collection_id']); _untouched(plan,raw)
        await _known_publications(conn,PostgresLedger(pool,plan['coordinator_run_id']),plan,
                                  ArtifactStore(Path(data_dir)/'selected-recovery'),raw)
    summary=dict(policy=POLICY,model_id=RECOVERY_KAYAK_ID,run_id=target['run_id'],
        operation_key=proof['operation_key'],response_hash=proof['response_hash'],provider_calls=0)
    if preflight_only: return {**summary,'status':'preflight_valid'}
    owner=await acquire_lease(pool,plan['coordinator_run_id'],'cached-kayak-replay')
    coordinator=PostgresLedger(pool,plan['coordinator_run_id'],owner)
    child_token=None
    try:
        child_token=await acquire_lease(pool,target['run_id'],'cached-verifier-only')
        child=PostgresLedger(pool,target['run_id'],child_token)
        async with child.locked(child.run_id,fenced=True) as (conn,_,__):
            await conn.execute('LOCK TABLE app.run_leases IN SHARE ROW EXCLUSIVE MODE')
            fresh,config,context,prepared,state=await _proof(conn,plan,target,artifacts,
                allowed_leases=(coordinator.run_id,child.run_id))
            if fresh!=proof: raise Conflict('Cached replay inputs changed since preflight')
            await coordinator.event(conn,child.run_id,'cached_verifier_replay_authorized',
                {k:v for k,v in proof.items() if k not in {'response','usage'}})
            await conn.execute("UPDATE app.runs SET status='running',control_version=control_version+1 WHERE id=%s",(child.run_id,))
        roles=SavedVerifierRoles(context,proof)
        graph,graph_config=await build_scope_graph(child,config,prepared.scope,roles)
        checkpoint=await graph.aget_state(graph_config)
        if checkpoint.next==('factor_validation',):
            state=await execute_scope(child,config,prepared.scope,roles,resume=True)
        elif not checkpoint.next and checkpoint.values.get('card'):
            state=dict(checkpoint.values)  # Idempotent completed replay; no role executes.
        else: raise Conflict('Cached replay accepts only the saved factor-validation boundary')
        async with child.locked(child.run_id,fenced=True) as (conn,_,__):
            current=await (await conn.execute('SELECT request_id,state,upper_micro,calculated_micro,billed_micro FROM app.usage WHERE run_id=%s ORDER BY request_id',(child.run_id,))).fetchall()
            operations=await (await conn.execute('SELECT operation_key,input_hash,state,response FROM app.provider_operations WHERE run_id=%s ORDER BY operation_key',(child.run_id,))).fetchall()
            if current!=proof['usage'] or digest(operations)!=proof['operations_hash']:
                raise Conflict('Cached replay changed external operations or usage')
            snapshots=await (await conn.execute('SELECT id,metadata FROM app.source_snapshots WHERE id=ANY(%s::text[])',([d.snapshot_id for d in prepared.scope.documents],))).fetchall()
            state={**state,'source_gaps':[g.model_dump(mode='json') for g in prepared.gaps],
                'source_metadata':{r['id']:r['metadata'] for r in snapshots}}
            await conn.execute("UPDATE app.runs SET status='partial' WHERE id=%s",(child.run_id,))
            await child.event(conn,child.run_id,'cached_verifier_replay_completed',dict(policy=POLICY,
                operation_key=proof['operation_key'],response_hash=proof['response_hash'],provider_calls=0,usage_unchanged=True))
        async with pool.connection() as conn:
            await conn.execute('UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s AND token=%s',(child.run_id,child_token))
        child_token=None
        result=ResearchRunResult(child.run_id,prepared,state,{},Path(data_dir),True)
        published=await _publish_result(coordinator,plan,target,result,data_dir)
        accounting=await PostgresLedger(pool,child.run_id).recovery_allowance()
        return {**summary,'status':'published' if published else 'needs_review','score':state.get('card',{}).get('score'),
                'allowance':{k:accounting[k] for k in ('allowance_id','limit_micro','accounted_micro','remaining_micro')}}
    finally:
        if child_token:
            try:
                async with child.locked(child.run_id,fenced=True) as (conn,_,__):
                    await conn.execute("UPDATE app.runs SET status='paused' WHERE id=%s AND status='running'",(child.run_id,))
            except Conflict:
                pass  # An expired or replaced owner must not change the successor's run.
        async with pool.connection() as conn:
            if child_token:
                await conn.execute('UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s AND token=%s',(target['run_id'],child_token))
            await conn.execute('UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s AND token=%s',(coordinator.run_id,owner))


def main():
    cli=argparse.ArgumentParser(description=__doc__);cli.add_argument('--allowance',required=True)
    mode=cli.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preflight-only',action='store_true');mode.add_argument('--apply',action='store_true')
    args=cli.parse_args()
    async def run():
        directory=Path(os.environ.get('AI_BUILDER_DATA','outputs'))
        async with execution_pool(os.environ['DATABASE_URL']) as pool:
            return await replay(pool,ArtifactStore(directory/'evidence'),directory,args.allowance,preflight_only=args.preflight_only)
    try: print(json.dumps(asyncio.run(run())),flush=True)
    except Exception as error: cli.exit(1,'Cached-only replay stopped: '+_safe_failure(error)+'\n')


if __name__=='__main__': main()
