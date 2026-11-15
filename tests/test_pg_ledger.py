import asyncio
import os

import httpx
import pytest

from ai_builder.pg_migrate import migrate
from ai_builder.pg_execution import execution_pool, acquire_lease
from ai_builder.pg_ledger import PostgresLedger
from ai_builder.store import BudgetExceeded, Conflict, uid
from ai_builder.providers import MeteredHTTP, OpenAIAdapter
from test_providers import pricing, Output, model_result

DSN = os.getenv('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')


async def setup(pool, config, budget='30', study=None):
    admin = PostgresLedger(pool)
    study = study or await admin.create_study('US',budget)
    run = await admin.create_run(study,config)
    token = await acquire_lease(pool,run,'test')
    ledger = PostgresLedger(pool,run,token)
    price = await ledger.register_pricing({'currency':'USD','version':'fixture'})
    return ledger,study,price


async def reserve(ledger, price, upper, request=None):
    request = request or uid()
    return await ledger.reserve(run_id=ledger.run_id,request_id=request,logical_request_id='logical-'+request,
        provider='openai',model='fixture',endpoint='responses',agent='critic',purpose='eval',pricing_id=price,upper=upper)


@pytest.mark.parametrize('shared_study',[False,True])
def test_parallel_budget_cannot_overspend(config, shared_study):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            first,study,price = await setup(pool,config,'25')
            second = (await setup(pool,config,study=study))[0] if shared_study else first
            outcomes = await asyncio.gather(reserve(first,price,'20'),reserve(second,price,'20'),return_exceptions=True)
            assert sum(v is True for v in outcomes)==1
            assert sum(isinstance(v,BudgetExceeded) for v in outcomes)==1
            totals = (await first.summary(first.run_id))['reserved_micro']
            if shared_study:
                totals += (await second.summary(second.run_id))['reserved_micro']
            assert totals == 20000000
    asyncio.run(check())


def test_unknown_settlement_correction_and_idempotence(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,price = await setup(pool,config)
            request = uid()
            assert await reserve(ledger,price,'10',request)
            assert not await reserve(ledger,price,'10',request)
            with pytest.raises(Conflict):
                await reserve(ledger,price,'11',request)
            await ledger.mark_unknown(request)
            assert (await ledger.summary(ledger.run_id))['unknown_micro']==10000000
            await ledger.settle(request,'2',{'input_tokens':100},billed='1')
            await ledger.settle(request,'2',{'input_tokens':100},billed='1')
            await ledger.settle(request,'3',{'input_tokens':200},billed='2')
            report = await ledger.summary(ledger.run_id)
            assert (report['calculated_micro'],report['billed_micro'],report['unknown_micro'],report['remaining_micro'])==(3000000,2000000,0,22000000)
            with pytest.raises(ValueError):
                await ledger.settle(request,1,{'Authorization':'secret'})
            with pytest.raises(Conflict):
                await ledger.release(request,'cannot erase billed usage')
    asyncio.run(check())


def test_request_limit_and_stale_lease(config):
    async def check():
        await migrate(DSN)
        cfg=config.model_copy(update={'limits':config.limits.model_copy(update={'provider_requests':1})})
        async with execution_pool(DSN) as pool:
            ledger,_,price = await setup(pool,cfg)
            request = uid()
            await reserve(ledger,price,'1',request)
            with pytest.raises(BudgetExceeded):
                await reserve(ledger,price,'1')
            async with pool.connection() as conn:
                await conn.execute('UPDATE app.run_leases SET token=%s WHERE run_id=%s',(uid(),ledger.run_id))
            with pytest.raises(Conflict):
                await reserve(ledger,price,'1')
            await ledger.settle(request,'0.5',{})  # late settlement of own request still valid
            assert (await ledger.summary(ledger.run_id))['calculated_micro']==500000
    asyncio.run(check())


def test_transport_replay_uses_receipt_without_second_charge(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,_ = await setup(pool,config)
            calls=[]
            def handler(req):
                calls.append(req)
                return httpx.Response(200,json=model_result())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                for _ in range(2):
                    transport=MeteredHTTP(ledger,ledger.run_id,'critic',pricing(),purpose='eval',client=client,operation_key='stable-node-call')
                    result=await OpenAIAdapter(transport,'fixture').generate('English',{},Output)
                    assert result.answer=='ok'
            assert len(calls)==1
            assert (await ledger.summary(ledger.run_id))['calculated_micro']==2000
            await ledger.begin_operation('crashed','hash')
            with pytest.raises(Conflict,match='explicit review'):
                await ledger.begin_operation('crashed','hash')
            async with pool.connection() as conn:
                run=await (await conn.execute('SELECT status FROM app.runs WHERE id=%s',(ledger.run_id,))).fetchone()
                assert run['status']=='paused'
    asyncio.run(check())


def test_receipt_restart_does_not_change_usage(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,price=await setup(pool,config)
            await reserve(ledger,price,'4')
            await ledger.receipt('scope','critic','node','hash',{'output':42})
            run,token=ledger.run_id,ledger.token
        async with execution_pool(DSN) as pool:
            ledger=PostgresLedger(pool,run,token)
            assert await ledger.receipt('scope','critic','node','hash')=={'output':42}
            assert (await ledger.summary(run))['remaining_micro']==21000000
            with pytest.raises(Conflict):
                await ledger.receipt('scope','critic','node','new-hash')
    asyncio.run(check())


def test_active_time_is_persistent_and_blocks_new_spending(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger,_,price=await setup(pool,config)
            remaining=await ledger.start_clock()
            assert 0 < remaining <= config.limits.active_seconds
            async with pool.connection() as conn:
                await conn.execute('UPDATE app.runs SET active_elapsed_ms=%s WHERE id=%s',(config.limits.active_seconds*1000,ledger.run_id))
            with pytest.raises(BudgetExceeded):
                await reserve(ledger,price,'1')
            await ledger.stop_clock('partial')
            async with pool.connection() as conn:
                row=await (await conn.execute('SELECT * FROM app.runs WHERE id=%s',(ledger.run_id,))).fetchone()
                assert row['status']=='paused' and row['active_started_at'] is None
                assert row['active_elapsed_ms']>=config.limits.active_seconds*1000
    asyncio.run(check())
