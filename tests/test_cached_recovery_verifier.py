"""No-provider recovery boundary and real paused-graph/publication replay."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

import ai_builder.replay_recovery_verifier as replay_module
from ai_builder.config import load_config
from ai_builder.domain import MilestoneHandoffClaim
from ai_builder.evidence import ArtifactStore
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_ledger import PostgresLedger,digest
from ai_builder.pg_migrate import migrate
from ai_builder.public import model_digest
from ai_builder.pipeline import (Verification,ScopePacket,build_scope_graph,execute_scope,prompt_bundle)
from ai_builder.research_scope import ResearchContext
from ai_builder.source_research import PreparedScope
from ai_builder.store import uid,Conflict
from test_conditional_pipeline import conditional_packet
from test_milestone_pipeline import MilestoneRoles
from test_pg_ledger import setup
from test_providers import pricing,model_result
from test_public import metadata
from test_selected_score_recovery import recovery_plan,current_bytes
from ai_builder.complete_selected_scores import TARGETS,COMPLETED,model_payload_hashes,sha

DSN=os.getenv('TEST_DATABASE_URL')
ROOT=Path(__file__).parents[1]


def context_fixture():
    prefix,prompts=prompt_bundle('1.3')
    return ResearchContext(plan=recovery_plan(),openai_pricing=pricing(),tavily_pricing=pricing('tavily'),
        role_settings={role:dict(reasoning_effort='medium',max_output_tokens=16384) for role in prompts},
        purpose='eval',pipeline_prefix=prefix,pipeline_prompts=prompts,
        recovery_allowance_id='fixture-allowance',parent_collection_id='fixture-parent',parent_run_id='old-source-run')


@pytest.mark.parametrize('defect',['role','schema','key','packet','response'])
def test_cached_roles_refuse_every_unproved_request_or_changed_response(defect):
    context=context_fixture();packet={'claims':[]}
    response=model_result(text=json.dumps({'items':[]}))
    proof=dict(operation_key='saved',request_hash=replay_module.request_hash(context,packet),
               response_hash=digest(response),response=response)
    roles=replay_module.SavedVerifierRoles(context,proof)
    args=['verifier',packet,Verification,'saved']
    if defect=='role':args[0]='research'
    elif defect=='schema':args[2]=ScopePacket
    elif defect=='key':args[3]='new-operation'
    elif defect=='packet':args[1]={'claims':[],'extra':'changed'}
    else:proof['response']['status']='incomplete'
    with pytest.raises(Conflict,match='unproved'):
        asyncio.run(roles.generate(*args))


@pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')
def test_paused_graph_uses_one_whole_cached_candidate_and_publishes_once_without_money_changes(monkeypatch,tmp_path,config):
    """Source/binding proof is separately covered and live-preflighted; graph, leases and publisher are real."""
    import ai_builder.pipeline as pipeline
    real_validator=pipeline.validate_factor_verification
    def rejecting_validator(*args,**kwargs):
        raise ValueError('Historical overly narrow arithmetic validator')
    monkeypatch.setattr(pipeline,'validate_factor_verification',rejecting_validator)
    async def scenario():
        await migrate(DSN)
        cfg=load_config(ROOT/'config/milestone-config.json');context=context_fixture()
        scope=conditional_packet();scope.base_model_id=TARGETS[1]
        scope.scope_id=uid();scope.variant_id=uid()
        async with execution_pool(DSN) as pool:
            ledger,study,price=await setup(pool,cfg)
            initial={}
            class PaidFixture(MilestoneRoles):
                def version(self):return replay_module.roles_version(context)
                async def generate(self,role,request,schema,key):
                    value=await super().generate(role,request,schema,key)
                    if role=='verifier' and any(c['claim_id']=='factor-time-to-first-payment' for c in request['claims']):
                        value=value.model_dump(mode='json') if hasattr(value,'model_dump') else value
                        total=next(i for i in value['items'] if i['claim_id']=='factor-time-to-first-payment')
                        total.update(status='supported',evidence_ids=[])
                        response=model_result(text=json.dumps(value));request_hash=replay_module.request_hash(context,request)
                        async with pool.connection() as conn:
                            await conn.execute("INSERT INTO app.provider_operations(run_id,operation_key,input_hash,state,response) VALUES (%s,%s,%s,'completed',%s)",
                                (ledger.run_id,key,request_hash,Jsonb(response)))
                        if 'output_correction' not in request:
                            initial.update(operation_key=key,request_hash=request_hash,response_hash=digest(response),response=response)
                    return value
            with pytest.raises(ValueError,match='Historical'):
                await execute_scope(ledger,cfg,scope,PaidFixture())
            graph,graph_config=await build_scope_graph(ledger,cfg,scope,PaidFixture())
            before_state=await graph.aget_state(graph_config)
            assert before_state.next==('factor_validation',) and 'card' not in before_state.values
            admin=PostgresLedger(pool);coordinator=await admin.create_run(study,cfg);collection=uid()
            raw_value=json.loads(current_bytes(config));raw_value['run_id']=collection
            selected=list(TARGETS+COMPLETED)
            raw_value.update(scoring_policy_version='1.5.3',reassessment_of_collection_id='fixture-parent',
                expected_count=5,selected_count=5,catalog_count=20,selected_model_ids=selected,processing_order=selected,
                update_states={mid:'queued' for mid in selected},retained_assessments={m['id']:dict(
                    run_id='fixture-parent',revision_id='fixture-original',scoring_policy_version=None,
                    model_hash=model_digest(m)) for m in raw_value['models']})
            raw=json.dumps(raw_value,indent=2).encode();public=tmp_path/'public';public.mkdir()
            path=public/f'{collection}.json';path.write_bytes(raw)
            (public/'latest.json').write_text(json.dumps({'run_id':collection}))
            members=[dict(model_id=m['id'],position=i+1,candidate={'metadata':metadata(m['id'])}) for i,m in enumerate(raw_value['models'])]
            target=dict(model_id=TARGETS[1],run_id=ledger.run_id,parent_run_id='fixture',metadata=metadata(TARGETS[1]),config=cfg.model_dump(mode='json'))
            plan=dict(allowance_id='fixture-allowance',collection_id=collection,study_id=study,coordinator_run_id=coordinator,
                context_hash=digest({}),membership_hash=digest(members),baseline_snapshot=raw.decode(),baseline_snapshot_hash=sha(raw),
                baseline_model_hashes=model_payload_hashes(raw),targets=[target],
                parent_members=[dict(model_id=m['model_id'],run_id=None,candidate_hash=digest(m['candidate'])) for m in members])
            async with pool.connection() as conn:
                await conn.execute("UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s",(ledger.run_id,))
                await conn.execute("INSERT INTO app.usage(request_id,logical_request_id,run_id,provider,model,endpoint,agent,purpose,pricing_id,upper_micro,state,calculated_micro) VALUES (%s,'fixture',%s,'openai','fixture','responses','verifier','eval',%s,2000,'settled',1000)",(uid(),ledger.run_id,price))
                await conn.execute("INSERT INTO app.collections(id,study_id,coordinator_run_id,context_hash,context,state,membership_hash,selected_count,frozen_at) VALUES (%s,%s,%s,%s,'{}','partial',%s,20,clock_timestamp())",(collection,study,coordinator,digest({}),digest(members)))
                for m in members:
                    await conn.execute('INSERT INTO app.collection_items(collection_id,model_id,position,candidate,public_model) VALUES (%s,%s,%s,%s,%s)',
                        (collection,m['model_id'],m['position'],Jsonb(m['candidate']),Jsonb(next(x for x in raw_value['models'] if x['id']==m['model_id']))))
                original_receipts=await (await conn.execute('SELECT * FROM app.node_receipts WHERE run_id=%s ORDER BY invocation_key',(ledger.run_id,))).fetchall()
                original_usage=await (await conn.execute('SELECT * FROM app.usage WHERE run_id=%s',(ledger.run_id,))).fetchall()
            async def load_plan(*args):return plan
            async def proof(conn,plan,target,artifacts,*,allowed_leases=()):
                await replay_module._quiet(conn,study,allowed_leases)
                usage=await (await conn.execute('SELECT request_id,state,upper_micro,calculated_micro,billed_micro FROM app.usage WHERE run_id=%s ORDER BY request_id',(ledger.run_id,))).fetchall()
                operations=await (await conn.execute('SELECT operation_key,input_hash,state,response FROM app.provider_operations WHERE run_id=%s ORDER BY operation_key',(ledger.run_id,))).fetchall()
                return {**initial,'policy':replay_module.POLICY,'usage':usage,'operations_hash':digest(operations)},cfg,context,PreparedScope(scope,()),{}
            async def accounting(self):return dict(allowance_id='fixture-allowance',limit_micro=10000000,accounted_micro=1000,remaining_micro=9999000)
            monkeypatch.setattr(replay_module,'_load_plan',load_plan);monkeypatch.setattr(replay_module,'_proof',proof)
            monkeypatch.setattr(PostgresLedger,'recovery_allowance',accounting)
            monkeypatch.setattr(pipeline,'validate_factor_verification',real_validator)
            def no_http(*args,**kwargs):raise AssertionError('A network client must never be constructed')
            import httpx
            monkeypatch.setattr(httpx,'AsyncClient',no_http)
            preflight=await replay_module.replay(pool,ArtifactStore(tmp_path/'evidence'),tmp_path,'fixture-allowance',preflight_only=True)
            assert preflight['provider_calls']==0 and path.read_bytes()==raw
            applied=await replay_module.replay(pool,ArtifactStore(tmp_path/'evidence'),tmp_path,'fixture-allowance')
            final=path.read_bytes();assert applied['status']=='published' and applied['score']['value']=='66.25'
            assert all(model_payload_hashes(final)[mid]==h for mid,h in model_payload_hashes(raw).items() if mid!=TARGETS[1])
            assert await replay_module.replay(pool,ArtifactStore(tmp_path/'evidence'),tmp_path,'fixture-allowance')==applied
            assert path.read_bytes()==final
            async with pool.connection() as conn:
                receipts=await (await conn.execute('SELECT * FROM app.node_receipts WHERE run_id=%s ORDER BY invocation_key',(ledger.run_id,))).fetchall()
                assert all(row in receipts for row in original_receipts)
                assert len(receipts)==len(original_receipts)+2  # Accepted verifier + deterministic card only.
                assert await (await conn.execute('SELECT * FROM app.usage WHERE run_id=%s',(ledger.run_id,))).fetchall()==original_usage
                accepted=next(r['result'] for r in receipts if r['invocation_key']==initial['operation_key'])
                assert accepted==replay_module.response_candidate(initial['response']).model_dump(mode='json')
                events=await (await conn.execute("SELECT 1 FROM app.events WHERE run_id=%s AND kind='selected_recovery_publication'",(coordinator,))).fetchall()
                assert len(events)==1
    asyncio.run(scenario())
