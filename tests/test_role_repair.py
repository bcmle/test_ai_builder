import asyncio
import json
import os

import httpx
import pytest

from ai_builder.config import Configuration
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_ledger import digest
from ai_builder.pg_migrate import migrate
from ai_builder.pipeline import (ModelRoles, PROMPTS, Research, ResearchV14, build_scope_graph, execute_scope)
from ai_builder.providers import ProviderFailure
from test_pg_ledger import setup
from test_pipeline import FixtureRoles, packet
from test_providers import model_result, pricing

DSN = os.getenv('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')


def unknown_research():
    return {'criteria':[dict(criterion_id=f'F{i:02}',state='completed',result='unknown',evidence_ids=[],
                             reason='No applicable evidence established') for i in range(1,11)]}


def model_roles(ledger, client):
    return ModelRoles(ledger, pricing(), 'fixture',
                      {role:dict(reasoning_effort='low',max_output_tokens=4096) for role in PROMPTS},
                      purpose='eval',client=client)


@pytest.mark.parametrize('defect',['scope_as_evidence','pass_without_evidence'])
def test_schema_and_semantic_correction_replays_without_repaying(config, defect):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,config)
            requests = []
            def handler(request):
                body = json.loads(request.content)
                data = json.loads(body['input'][0]['content'])
                requests.append(data)
                result = unknown_research()
                if len(requests) == 1:
                    result['criteria'][4].update(result='pass',evidence_ids=['scope'] if defect=='scope_as_evidence' else [])
                else:
                    assert data['output_correction']['attempt'] == 1
                    assert data['output_correction']['validation_errors']
                return httpx.Response(200,json=model_result(text=json.dumps(result)))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                roles = model_roles(ledger,client)
                result = await execute_scope(ledger,config,packet(),roles)
                assert result['card']['eligibility'] == 'needs_review'
                assert len(requests) == 2
                first_usage = await ledger.summary(ledger.run_id)
                assert len(first_usage['requests']) == 2 and first_usage['calculated_micro'] == 4000
                await execute_scope(ledger,config,packet(),roles)
                assert len(requests) == 2 and await ledger.summary(ledger.run_id) == first_usage
            async with pool.connection() as conn:
                accepted = await (await conn.execute("SELECT result FROM app.node_receipts WHERE run_id=%s AND node_name='research'",
                                                      (ledger.run_id,))).fetchall()
            assert len(accepted) == 1
            assert accepted[0]['result']['criteria'][4]['result'] == 'unknown'
    asyncio.run(scenario())


def test_correction_count_survives_crash_after_original_raw_response(config):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,config)
            requests = []
            def handler(request):
                requests.append(json.loads(request.content))
                result = unknown_research()
                if len(requests) == 1:
                    result['criteria'][4]['result'] = 'pass'
                return httpx.Response(200,json=model_result(text=json.dumps(result)))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                roles = model_roles(ledger,client)
                generate = roles.generate
                crashed = False
                async def crash_once(*args):
                    nonlocal crashed
                    try:
                        return await generate(*args)
                    except ProviderFailure:
                        if not crashed:
                            crashed = True
                            raise RuntimeError('Crash after paid provider response')
                        raise
                roles.generate = crash_once
                graph,cfg = await build_scope_graph(ledger,config,packet(),roles)
                with pytest.raises(RuntimeError,match='paid provider'):
                    await graph.ainvoke({},cfg,durability='sync')
                result = await graph.ainvoke(None,cfg,durability='sync')
                assert result['card']['eligibility'] == 'needs_review'
                assert len(requests) == 2
                usage = await ledger.summary(ledger.run_id)
                assert len(usage['requests']) == 2
    asyncio.run(scenario())


def test_exhausted_corrections_pause_and_are_not_business_failure(config):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,config)
            requests = []
            def handler(request):
                body = json.loads(request.content)
                requests.append(json.loads(body['input'][0]['content']))
                invalid = unknown_research()
                invalid['criteria'][4].update(result='pass',evidence_ids=['scope'])
                return httpx.Response(200,json=model_result(text=json.dumps(invalid)))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(ValueError,match='Unknown criterion evidence'):
                    await execute_scope(ledger,config,packet(),model_roles(ledger,client))
            assert len(requests) == 3
            assert [r['output_correction']['attempt'] for r in requests[1:]] == [1,2]
            async with pool.connection() as conn:
                row = await (await conn.execute('SELECT status FROM app.runs WHERE id=%s',(ledger.run_id,))).fetchone()
                count = await (await conn.execute("SELECT count(*) AS n FROM app.node_receipts WHERE run_id=%s AND node_name='research'",
                                                   (ledger.run_id,))).fetchone()
            assert row['status'] == 'paused' and count['n'] == 0
    asyncio.run(scenario())


def test_invalid_legacy_receipt_is_retained_and_corrected_separately(config):
    class CorrectingRoles(FixtureRoles):
        async def generate(self,role,data,schema,key):
            self.calls.append((role,data,key))
            assert role == 'research' and data['output_correction']['attempt'] == 1
            return schema.model_validate(unknown_research())
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,config)
            scope = packet()
            roles = CorrectingRoles()
            old = unknown_research()
            old['criteria'][4].update(result='pass',evidence_ids=['scope'])
            input_hash = digest({'scope':scope.model_dump(mode='json'),'config':config.digest(),'roles':roles.version()})
            original_input = dict(assessment_scope_id=scope.scope_id,base_model_id=scope.base_model_id,
                variant_id=scope.variant_id,neutral_model_description=scope.neutral_model_description,
                launch_context=config.scenario.model_dump(mode='json'),documents=[d.model_dump(mode='json') for d in scope.documents],
                criteria=[c.model_dump() for c in config.criteria])
            h = digest({'input':original_input,'scope':input_hash,'role':'research','suffix':''})
            key = scope.scope_id+':research:'+h
            await ledger.receipt(scope.scope_id,'research',key,h,old)
            assert (await execute_scope(ledger,config,scope,roles))['card']['eligibility'] == 'needs_review'
            assert await ledger.receipt(scope.scope_id,'research',key,h) == old
            await execute_scope(ledger,config,scope,roles)
            assert len(roles.calls) == 1
    asyncio.run(scenario())


def test_v14_rechecks_affected_criterion_without_critic_conclusions(config):
    value = config.model_dump(mode='json')
    value['schema_version'] = '1.1'
    value['criteria'][4]['allow_design_basis'] = True
    revised = Configuration.model_validate(value)
    class NewRoles(FixtureRoles):
        async def generate(self,role,data,schema,key):
            if role=='research':
                result = await super().generate(role,data,Research,key)
                raw = result.model_dump(mode='json')
                raw['criteria'][4]['evidence_ids'] = []
                raw['criteria'][4]['design_basis'] = [dict(kind='proposed_design',assessment_scope_id='scope',
                                                           excerpt='Subscription workflow software')]
                return ResearchV14.model_validate(raw)
            result = await super().generate(role,data,schema,key)
            if role=='critic':
                result.claims[0].affected_criterion_ids = ['F05']
            if role in {'critic','factors'}:
                for item in result.claims:
                    item.method = 'model_judgment' if item.kind=='judgment' else 'primary_source'
                    item.confidence,item.confidence_reason = 'low','Fixture assumptions'
                    if item.kind=='judgment':
                        item.assumptions = ['Fixture team can implement this design']
            if role=='applicability' and 'claims' in data:
                assert 'critique' not in data and 'risks' not in data and 'verification' not in data
                assert 'impact' not in str(data) and 'affected_criterion_ids' not in str(data)
            return result
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,revised)
            roles = NewRoles()
            state = await execute_scope(ledger,revised,packet(),roles)
            assert state['card']['publication'] == 'ready'
            assert sum(role=='applicability' for role,_,_ in roles.calls) == 2
            assert state['criteria'][4]['design_basis'][0]['kind'] == 'proposed_design'
            assert state['criteria'][4]['evidence_ids'] == []
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['refusal', 'unknown_usage'])
def test_non_contract_provider_failure_never_starts_correction(config, failure):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,config)
            requests = []
            def handler(request):
                requests.append(json.loads(request.content))
                result = model_result(text='{}')
                if failure == 'refusal':
                    result['output'][0]['content'] = [{'type':'refusal','refusal':'Fixture refusal'}]
                else:
                    result.pop('usage')
                return httpx.Response(200,json=result)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(ProviderFailure,match='model_refusal' if failure=='refusal' else 'usage_unavailable'):
                    await execute_scope(ledger,config,packet(),model_roles(ledger,client))
            assert len(requests) == 1
            assert 'output_correction' not in json.loads(requests[0]['input'][0]['content'])
            usage = await ledger.summary(ledger.run_id)
            assert len(usage['requests']) == 1
            assert bool(usage['unknown_micro']) == (failure == 'unknown_usage')
    asyncio.run(scenario())


@pytest.mark.parametrize('defect', ['forbidden_criterion', 'different_scope', 'invented_excerpt'])
def test_invalid_design_basis_is_never_accepted(config, defect):
    value = config.model_dump(mode='json')
    value['schema_version'] = '1.1'
    value['criteria'][4]['allow_design_basis'] = True
    revised = Configuration.model_validate(value)
    class InvalidDesignRoles(FixtureRoles):
        async def generate(self,role,data,schema,key):
            self.calls.append((role,data,key))
            assert role == 'research'
            result = unknown_research()
            item = result['criteria'][0 if defect=='forbidden_criterion' else 4]
            item.update(result='pass',design_basis=[dict(kind='proposed_design',
                assessment_scope_id='other' if defect=='different_scope' else 'scope',
                excerpt='A made-up operating promise' if defect=='invented_excerpt' else 'Subscription workflow software')])
            return schema.model_validate(result)
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,revised)
            roles = InvalidDesignRoles()
            with pytest.raises(ValueError,match='Design basis'):
                await execute_scope(ledger,revised,packet(),roles)
            assert len(roles.calls) == 3
            async with pool.connection() as conn:
                row = await (await conn.execute('SELECT count(*) AS n FROM app.node_receipts WHERE run_id=%s',
                                                 (ledger.run_id,))).fetchone()
            assert row['n'] == 0
    asyncio.run(scenario())
