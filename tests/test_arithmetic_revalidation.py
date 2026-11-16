"""Real checkpoint/operation replay; no live provider is involved."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from ai_builder.collection import CollectionContext, CollectionPlan
from ai_builder.config import load_config
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_ledger import digest
from ai_builder.pg_migrate import migrate
from ai_builder.pipeline import (ModelRoles, UpstreamInheritance, execute_scope, prompt_bundle)
from ai_builder.public import public_model, publish_snapshot, load_snapshot, model_digest
from ai_builder.research_scope import ResearchContext, ResearchRunResult
from ai_builder.revalidate_milestone_arithmetic import revalidate,provider_payload,TOTAL
from ai_builder.source_research import PreparedScope, SourceResearchPlan, ScopeSeed
from ai_builder.store import uid, Conflict
from test_conditional_pipeline import conditional_packet
from test_milestone_pipeline import MilestoneRoles
from test_pg_ledger import setup
from test_providers import pricing,model_result

DSN=os.getenv('TEST_DATABASE_URL')
pytestmark=pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')


@pytest.mark.parametrize('with_budget_share',[False,True])
def test_paid_candidate_revalidation_dry_run_apply_and_idempotent_history(monkeypatch,tmp_path,with_budget_share):
    import ai_builder.pipeline as pipeline
    current=pipeline.validate_factor_verification
    def old_validation(result,factors,*,market,milestones=False,business_budget_usd=None):
        current(result,factors,market=market,milestones=False)
    monkeypatch.setattr(pipeline,'validate_factor_verification',old_validation)
    async def scenario():
        await migrate(DSN)
        config=load_config(Path(__file__).parents[1]/'config/milestone-config.json')
        prefix,prompts=prompt_bundle('1.3')
        settings={role:dict(reasoning_effort='medium',max_output_tokens=16384) for role in prompts}
        scope=conditional_packet()
        async with execution_pool(DSN) as pool:
            ledger,study,_=await setup(pool,config)
            collection_id=uid()
            parent=dict(parent_run_id='parent-fixture',parent_collection_id='parent-collection',completed_through=None,state={})
            envelope=dict(manifest_hash=digest(parent),manifest=parent)
            inheritance=UpstreamInheritance(manifest_hash=envelope['manifest_hash'],parent_run_id=parent['parent_run_id'],completed_through=None,state={})
            plan=SourceResearchPlan(schema_version='3',seed=ScopeSeed(**scope.model_dump(include=set(ScopeSeed.model_fields))),queries=(),
                legal_analogues=[dict(analogue_id='fixture',company_name='Fixture',product_name='Fixture',official_urls=['https://example.com'])])
            research=ResearchContext(plan=plan,openai_pricing=pricing(),tavily_pricing=pricing('tavily'),
                role_settings=settings,purpose='eval',pipeline_prefix=prefix,pipeline_prompts=prompts,
                parent_collection_id=parent['parent_collection_id'],parent_run_id=parent['parent_run_id'],upstream_manifest=envelope)
            real_roles=ModelRoles(None,pricing(),'',settings,purpose='eval',prompt_prefix=prefix,prompts=prompts)
            class PaidFixture(MilestoneRoles):
                def version(self): return real_roles.version()
                async def generate(self,role,request,schema,key):
                    value=await super().generate(role,request,schema,key)
                    if role=='factors' and with_budget_share:
                        value['cost']=dict(low=3000,high=9000)
                        cost=next(c for c in value['claims'] if c['claim_id']=='factor-cost')
                        cost.update(quantity_estimate=value['cost'],metric='Non-salary cost in USD',
                            neutral_text='The conditional cost estimate is 3000 to 9000 USD.',
                            assumptions=['The conditional cost estimate is 3000 to 9000 USD.'])
                        scenario={**deepcopy(cost),'claim_id':'factor-scenario','quantity_estimate':None,
                            'metric':'Scenario boundaries','neutral_text':'The scenario budget is 15000 USD.',
                            'assumptions':['The scenario budget is 15000 USD.']}
                        share={**deepcopy(scenario),'claim_id':'factor-budget-share','kind':'calculation','method':'calculation',
                            'quantity_estimate':dict(low=20,high=60),'metric':'Non-salary budget share, percent',
                            'formula':'100×[3000,9000]/15000=[20,60].','input_claim_ids':['factor-cost','factor-scenario'],
                            'neutral_text':'The conditional cost represents 20 to 60 percent of the budget.',
                            'assumptions':[]}
                        value['claims'].extend([scenario,share])
                    if role=='verifier' and any(c['claim_id']==TOTAL for c in request['claims']):
                        value=value.model_dump(mode='json') if hasattr(value,'model_dump') else value
                        for item in value['items']:
                            if item['claim_id']==TOTAL:
                                item['status']='not_fact_checkable' if 'output_correction' in request else 'supported'
                                item['evidence_ids']=[]
                            elif item['claim_id']=='factor-budget-share':
                                item['status']='insufficient' if 'output_correction' in request else 'supported'
                                item['evidence_ids']=[]
                        payload=provider_payload(research,request)
                        request_hash=digest(dict(endpoint='responses',payload=payload,agent='verifier',purpose='eval',pricing=pricing().model_dump(mode='json')))
                        async with pool.connection() as conn:
                            await conn.execute('''INSERT INTO app.provider_operations(run_id,operation_key,input_hash,state,response)
                                VALUES (%s,%s,%s,'completed',%s)''',
                                (ledger.run_id,key,request_hash,Jsonb(model_result(text=json.dumps(value)))))
                    return value
            roles=PaidFixture()
            state=await execute_scope(ledger,config,scope,roles,inheritance=inheritance)
            assert set(state['card']['publication_blockers'])=={'factor_claim_unresolved:'+TOTAL}|({'factor_claim_unresolved:factor-budget-share'} if with_budget_share else set())
            metadata=dict(id=scope.base_model_id,title='Fixture model',category='Fixture',summary='Fixture scope',buyer='Teams',revenue='Subscription',comparable='Fixture')
            original=public_model(ResearchRunResult(ledger.run_id,PreparedScope(scope,()),state,{},tmp_path,False),metadata,config)
            models=[original];members=[]
            selected=[scope.base_model_id]+[uid() for _ in range(4)]
            ids=selected+[uid() for _ in range(15)]
            for position,identifier in enumerate(ids,1):
                meta={**metadata,'id':identifier}
                members.append(dict(model_id=identifier,position=position,candidate=dict(metadata=meta)))
                if position>1: models.append(public_model(None,meta,config))
            collection_context=CollectionContext(plan=CollectionPlan(),config=config,openai_pricing=pricing(),tavily_pricing=pricing('tavily'),
                role_settings=settings,study_budget_usd=100,purpose='eval',execution_version='collection-3',pipeline_prefix=prefix,pipeline_prompts=prompts,
                reassessment_of_collection_id='parent-collection',processing_order=tuple(selected),selected_model_ids=tuple(selected))
            coordinator=await ledger.create_run(study,config)
            async with pool.connection() as conn:
                await conn.execute('INSERT INTO app.scope_inputs(run_id,scope,role_settings,pricing,purpose) VALUES (%s,%s,%s,%s,%s)',
                    (ledger.run_id,Jsonb(scope.model_dump(mode='json')),Jsonb(settings),Jsonb(pricing().model_dump(mode='json')),'eval'))
                await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',
                    (ledger.run_id,digest(research.model_dump(mode='json')),Jsonb(research.model_dump(mode='json'))))
                await conn.execute("UPDATE app.runs SET status='completed' WHERE id=%s",(ledger.run_id,))
                await conn.execute("UPDATE app.run_leases SET expires_at=clock_timestamp()-interval '1second' WHERE run_id=%s",(ledger.run_id,))
                await conn.execute('''INSERT INTO app.collections(id,study_id,coordinator_run_id,context_hash,context,state,membership_hash,selected_count,frozen_at)
                    VALUES (%s,%s,%s,%s,%s,'completed',%s,20,clock_timestamp())''',
                    (collection_id,study,coordinator,digest(collection_context.model_dump(mode='json')),Jsonb(collection_context.model_dump(mode='json')),digest(members)))
                for member in members:
                    first=member['position']==1
                    await conn.execute('''INSERT INTO app.collection_items(collection_id,model_id,position,candidate,run_id,state,public_model)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)''',(collection_id,member['model_id'],member['position'],Jsonb(member['candidate']),
                        ledger.run_id if first else None,'completed' if first else 'queued',Jsonb(original) if first else None))
                before=await (await conn.execute('SELECT invocation_key,result FROM app.node_receipts WHERE run_id=%s ORDER BY invocation_key',(ledger.run_id,))).fetchall()
                usage_before=await (await conn.execute('SELECT * FROM app.usage WHERE run_id=%s',(ledger.run_id,))).fetchall()
            snapshot=dict(schema_version='prototype-1',run_id=collection_id,revision_id=uid(),title='Fixture catalog',market='US',
                created_at=datetime.now(timezone.utc).isoformat(),published_at=datetime.now(timezone.utc).isoformat(),status='partial',
                expected_count=5,selected_count=5,completed_count=1,scenario=config.scenario.model_dump(mode='json',include={'team_size','business_budget_usd','days','jurisdiction'}),
                models=models,catalog_count=20,selected_model_ids=selected,processing_order=selected,scoring_policy_version='1.5.3',
                reassessment_of_collection_id='parent-collection',update_states={identifier:'completed' if identifier==scope.base_model_id else 'queued' for identifier in selected})
            historical={key:value for key,value in snapshot.items() if key not in {'catalog_count','selected_model_ids','processing_order',
                'scoring_policy_version','reassessment_of_collection_id','update_states'}}
            legacy=load_config(Path(__file__).parents[1]/'config/conditional-config.json')
            historical_models=[public_model(None,member['candidate']['metadata'],legacy) for member in members]
            snapshot['models'][1:]=historical_models[1:]
            historical.update(run_id='parent-collection',revision_id='fixture-parent-public',expected_count=20,selected_count=20,
                              scoring_policy_version='1.5.2',models=historical_models)
            publish_snapshot(tmp_path,historical)
            prior=load_snapshot(tmp_path,'parent-collection')
            snapshot['retained_assessments']={model['id']:dict(run_id='parent-collection',revision_id='fixture-parent-public',scoring_policy_version='1.5.2',model_hash=model_digest(model))
                                             for model in prior['models'][1:]}
            publish_snapshot(tmp_path,snapshot)
            old=load_snapshot(tmp_path,collection_id)
            monkeypatch.setattr(pipeline,'validate_factor_verification',current)
            dry=await revalidate(pool,tmp_path,collection_id,[scope.base_model_id])
            assert dry['status']=='dry_run' and load_snapshot(tmp_path,collection_id)==old
            applied=await revalidate(pool,tmp_path,collection_id,apply_hash=dry['plan_hash'])
            assert applied['provider_calls']==0
            new=load_snapshot(tmp_path,collection_id)
            assert new['models'][0]['score']['status']=='conditional'
            assert new['models'][0]['score']['value']==66.25
            assert json.loads((tmp_path/'public'/'history'/collection_id/(old['revision_id']+'.json')).read_text())==old
            assert await revalidate(pool,tmp_path,collection_id,apply_hash=dry['plan_hash'])==applied
            async with pool.connection() as conn:
                after=await (await conn.execute('SELECT invocation_key,result FROM app.node_receipts WHERE run_id=%s ORDER BY invocation_key',(ledger.run_id,))).fetchall()
                assert before==after
                assert usage_before==await (await conn.execute('SELECT * FROM app.usage WHERE run_id=%s',(ledger.run_id,))).fetchall()
                audit=await (await conn.execute("SELECT payload FROM app.events WHERE run_id=%s AND kind='milestone_arithmetic_revalidated'",(coordinator,))).fetchall()
                assert len(audit)==1
    asyncio.run(scenario())
