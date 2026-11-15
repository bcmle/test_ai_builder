import asyncio
import hashlib
from decimal import Decimal
import os

import pytest

from ai_builder.pipeline import ScopePacket, Document, build_scope_graph, execute_scope
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_migrate import migrate
from ai_builder.store import Conflict
from test_pg_ledger import setup

DSN=os.getenv('TEST_DATABASE_URL')
pytestmark=pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')


def packet():
    text='Fixture subscription product and pricing. A synthetic company has a valuation of $500 million.'
    return ScopePacket(scope_id='scope',base_model_id='base',variant_id='variant',neutral_model_description='Subscription workflow software',
        documents=[Document(snapshot_id='snapshot',evidence_id='evidence',url='https://example.com/fixture',text=text,
                            content_hash=hashlib.sha256(text.encode()).hexdigest(),policy_status='validated')])


def claim(identifier='claim',kind='fact',quantity=None):
    return dict(claim_id=identifier,neutral_text='Fixture subscription product and pricing.',kind=kind,entity='Fixture',geography='US',
        period=None,metric=None,source_urls=['https://example.com/fixture'],
        evidence_refs=[dict(evidence_id='evidence',source_snapshot_id='snapshot',excerpt='Fixture subscription product and pricing.',locator='paragraph 1')],
        formula=None,input_claim_ids=[],quantity_estimate=quantity,affected_criterion_ids=[])


class FixtureRoles:
    def __init__(self,criterion_status='pass',neutral=True,bad_verifier=False):
        self.calls=[]; self.criterion_status=criterion_status; self.neutral=neutral; self.bad_verifier=bad_verifier
    def version(self):
        return 'fixture-roles-1'
    async def generate(self,role,packet,schema,key):
        self.calls.append((role,packet,key))
        if role=='research':
            result={'criteria':[dict(criterion_id=f'F{i:02}',state='completed',result=self.criterion_status,
                evidence_ids=['evidence'],reason='Synthetic fixture') for i in range(1,11)]}
        elif role=='applicability':
            result=dict(status='supports_'+self.criterion_status if self.criterion_status!='unknown' else 'insufficient',reason='Synthetic fixture')
        elif role in {'critic','repair'}:
            result=dict(risks=[dict(risk_id='risk',impact=3,critic_order=0,premise_claim_ids=['claim'],essential_premise_ids=['claim'])],
                        claims=[claim()],limitations=['Fixture, not market research'])
        elif role=='neutrality':
            result=dict(status='accept' if self.neutral else 'uncertain',reason='Fixture')
        elif role=='verifier':
            assert set(packet)=={'assessment_scope_id','base_model_id','variant_id','neutral_model_description','launch_context','claims','documents'}
            assert 'impact' not in str(packet) and 'critic_order' not in str(packet)
            assert 'affected_criterion_ids' not in str(packet)
            result=dict(items=[dict(claim_id='made-up' if self.bad_verifier else 'claim',status='supported',evidence_ids=['evidence'],reason='Fixture')])
        elif role=='factors':
            result=dict(factors=[dict(factor_id=f'S{i:02}',value=dict(low=2,high=3),claim_ids=['estimate']) for i in range(1,6)],
                claims=[claim('estimate','judgment'),claim('days','judgment',dict(low=30,high=60)),claim('cost','judgment',dict(low=100,high=500))],
                days=dict(low=30,high=60),days_claim_id='days',cost=dict(low=100,high=500),cost_claim_id='cost',
                resources='available',resources_claim_id='estimate',test_plan_claim_ids=['estimate'],limitations=['Synthetic fixture'])
        else:
            raise AssertionError(role)
        return schema.model_validate(result)


@pytest.mark.parametrize('status,neutral,expected',[('pass',True,'pass'),('fail',True,'fail'),('unknown',True,'needs_review'),('pass',False,'needs_review')])
def test_routes_and_payload_isolation(config,status,neutral,expected,tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_=await setup(pool,config)
            roles=FixtureRoles(status,neutral)
            state=await execute_scope(ledger,config,packet(),roles)
            assert state['card']['eligibility']==expected
            from ai_builder.report import render_report
            preview=render_report(packet(),state,await ledger.summary(ledger.run_id),mode='replay')
            (tmp_path/'private-draft.html').write_text(preview)
            assert 'Private draft' in preview
            names=[r[0] for r in roles.calls]
            assert ('verifier' in names)==(expected=='pass')
            if expected=='pass':
                assert Decimal(state['card']['score']['value'])==50
                assert state['card']['publication']=='draft'
                assert state['card']['publication_blockers']
                # Re-running from the same starting input reuses role receipts.
                count=len(roles.calls)
                await execute_scope(ledger,config,packet(),roles)
                assert len(roles.calls)==count
            else:
                assert state['card']['score'] is None
            if not neutral:
                assert names.count('repair')==1
    asyncio.run(check())


def test_invalid_verifier_stops_and_changed_scope_rejected(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_=await setup(pool,config)
            with pytest.raises(ValueError,match='every handed-off'):
                await execute_scope(ledger,config,packet(),FixtureRoles(bad_verifier=True))
            with pytest.raises(Conflict,match='Owner action'):
                await execute_scope(ledger,config,packet(),FixtureRoles())
            changed=packet().model_copy(update={'neutral_model_description':'Changed business'})
            with pytest.raises(Conflict,match='new run'):
                await build_scope_graph(ledger,config,changed,FixtureRoles())
    asyncio.run(check())


def test_crash_after_role_receipt_before_checkpoint_does_not_repeat_role(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_=await setup(pool,config)
            roles=FixtureRoles()
            original=ledger.receipt
            interrupted=False
            async def failing_receipt(scope,node,key,input_hash,result=None):
                nonlocal interrupted
                value=await original(scope,node,key,input_hash,result)
                if node=='research' and result is not None and not interrupted:
                    interrupted=True
                    raise RuntimeError('crash after domain commit')
                return value
            ledger.receipt=failing_receipt
            graph,cfg=await build_scope_graph(ledger,config,packet(),roles)
            with pytest.raises(RuntimeError,match='domain commit'):
                await graph.ainvoke({},cfg,durability='sync')
            ledger.receipt=original
            graph,cfg=await build_scope_graph(ledger,config,packet(),roles)
            result=await graph.ainvoke(None,cfg,durability='sync')
            assert result['card']['eligibility']=='pass'
            assert sum(r[0]=='research' for r in roles.calls)==1
    asyncio.run(check())


def test_business_graph_uses_postgres_metered_model_transport(config):
    import json
    import httpx
    from ai_builder.pipeline import ModelRoles, PROMPTS, PREFIX, Research, Applicability, Critique, Neutrality, Verification, Factors
    from test_providers import pricing, model_result
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_=await setup(pool,config)
            fixture=FixtureRoles()
            schemas={'research':Research,'applicability':Applicability,'critic':Critique,'repair':Critique,
                     'neutrality':Neutrality,'verifier':Verification,'factors':Factors}
            async def handler(request):
                body=json.loads(request.content)
                role=next(role for role,prompt in PROMPTS.items() if PREFIX+prompt==body['instructions'])
                assert 'previous_response_id' not in body and len(body['input'])==1
                result=await fixture.generate(role,json.loads(body['input'][0]['content']),schemas[role],'fixture-http')
                return httpx.Response(200,json=model_result(text=result.model_dump_json()))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                settings={role:dict(reasoning_effort='low',max_output_tokens=4096) for role in PROMPTS}
                roles=ModelRoles(ledger,pricing(),'fixture',settings,purpose='eval',client=client)
                result=await execute_scope(ledger,config,packet(),roles)
                assert result['card']['eligibility']=='pass'
                before=await ledger.summary(ledger.run_id)
                assert len(before['requests'])==15
                assert before['calculated_micro']==30000
                assert before['reserved_micro']==before['unknown_micro']==0
                await execute_scope(ledger,config,packet(),roles)
                after=await ledger.summary(ledger.run_id)
                assert before==after
    asyncio.run(check())


def test_scope_checkpointer_rejects_another_thread(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_=await setup(pool,config)
            graph,cfg=await build_scope_graph(ledger,config,packet(),FixtureRoles())
            with pytest.raises(Conflict,match='outside'):
                await graph.checkpointer.aget_tuple({'configurable':{'thread_id':'another-scope'}})
    asyncio.run(check())
