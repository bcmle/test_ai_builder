import asyncio
import json
import os

import httpx
import pytest

from ai_builder.pg_execution import execution_pool
from ai_builder.pg_ledger import digest
from ai_builder.pg_migrate import migrate
from ai_builder.providers import MeteredHTTP, OpenAIAdapter, ProviderFailure
from ai_builder.store import Conflict
from test_pg_ledger import setup
from test_providers import Output, model_result, pricing

DSN = os.getenv('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')
PACKET = {'document':'Unchanged full document. '*14000}


def adapter(ledger,client):
    return OpenAIAdapter(MeteredHTTP(ledger,ledger.run_id,'repair',pricing(),purpose='eval',
        client=client,operation_key='original-generation-operation'),'fixture')


def test_exact_count_reserves_frozen_limit_and_both_operations_replay(config):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,config)
            requests = []
            async def handler(request):
                body = json.loads(request.content)
                requests.append((request.url.path,body))
                if request.url.path.endswith('/input_tokens'):
                    assert set(body) == {'model','instructions','input','reasoning','text','truncation'}
                    assert body['truncation'] == 'disabled'
                    return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':70000},
                                          headers={'x-request-id':'count-fixture'})
                summary = await ledger.summary(ledger.run_id)
                # Full saved input limit, original output ceiling; not the smaller count.
                assert summary['reserved_micro'] == 2720000 + 4096*50
                assert len(summary['requests']) == 1
                async with pool.connection() as conn:
                    count = await (await conn.execute("""SELECT response FROM app.provider_operations
                        WHERE run_id=%s AND operation_key LIKE 'input-token-count:%%' AND state='completed'""",
                        (ledger.run_id,))).fetchone()
                assert count['response']['input_tokens'] == 70000  # Committed before paid generation.
                return httpx.Response(200,json=model_result())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                assert (await adapter(ledger,client).generate('Original instructions',PACKET,Output)).answer == 'ok'
                usage = await ledger.summary(ledger.run_id)
                assert usage['calculated_micro'] == 2000 and len(usage['requests']) == 1
                assert (await adapter(ledger,client).generate('Original instructions',PACKET,Output)).answer == 'ok'
                assert await ledger.summary(ledger.run_id) == usage
            assert [path for path,_ in requests] == ['/v1/responses/input_tokens','/v1/responses']
            counted, generated = requests[0][1],requests[1][1]
            assert {key:counted[key] for key in counted if key!='truncation'} == {
                key:generated[key] for key in ('model','instructions','input','reasoning','text')}
            assert 'truncation' not in generated and json.loads(generated['input'][0]['content']) == PACKET
            async with pool.connection() as conn:
                original = await (await conn.execute('''SELECT input_hash FROM app.provider_operations
                    WHERE run_id=%s AND operation_key='original-generation-operation' ''',(ledger.run_id,))).fetchone()
                event = await (await conn.execute("SELECT payload FROM app.events WHERE run_id=%s AND kind='input_token_count_completed'",
                                                   (ledger.run_id,))).fetchone()
            assert original['input_hash'] == digest({'endpoint':'responses','payload':generated,'agent':'repair',
                'purpose':'eval','pricing':pricing().model_dump(mode='json')})
            assert event['payload']['cost'] is None and event['payload']['billing'] == 'not_reported'
            assert event['payload']['provider_request_id'] == 'count-fixture'
    asyncio.run(scenario())


@pytest.mark.parametrize('defect',['timeout','http_error','invalid_count','over_limit'])
def test_count_failure_or_oversize_never_sends_generation_or_repeats_preflight(config,defect):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,config)
            requests = []
            def handler(request):
                requests.append(request.url.path)
                assert request.url.path.endswith('/input_tokens')
                if defect == 'timeout':
                    raise httpx.ReadTimeout('Unreported preflight outcome')
                if defect == 'http_error':
                    return httpx.Response(429,json={'error':{'message':'Never expose this provider text'}})
                return httpx.Response(200,json={'object':'response.input_tokens',
                    'input_tokens':True if defect=='invalid_count' else 272000-4096+1})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(ProviderFailure) as error:
                    await adapter(ledger,client).generate('Original instructions',PACKET,Output)
                assert 'Never expose' not in str(error.value)
                if defect == 'over_limit':
                    assert error.value.code == 'input_exceeds_pricing_scope'
                    with pytest.raises(ProviderFailure,match='input_exceeds_pricing_scope'):
                        await adapter(ledger,client).generate('Original instructions',PACKET,Output)
                else:
                    with pytest.raises(Conflict,match='explicit review'):
                        await adapter(ledger,client).generate('Original instructions',PACKET,Output)
            assert requests == ['/v1/responses/input_tokens']
            assert not (await ledger.summary(ledger.run_id))['requests']
            async with pool.connection() as conn:
                rows = await (await conn.execute('SELECT state FROM app.provider_operations WHERE run_id=%s',
                                                 (ledger.run_id,))).fetchall()
                events = await (await conn.execute("SELECT payload FROM app.events WHERE run_id=%s AND kind LIKE 'input_token_count_%%'",
                                                   (ledger.run_id,))).fetchall()
            assert rows == [{'state':'completed' if defect=='over_limit' else 'running'}]
            assert all(row['payload']['cost'] is None and row['payload']['billing']=='not_reported' for row in events)
    asyncio.run(scenario())
