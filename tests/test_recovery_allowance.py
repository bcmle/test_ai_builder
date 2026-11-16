"""Shared additional spending is distinct from frozen historical unknown charges."""
import asyncio
from decimal import Decimal
import json
import os

import httpx
import pytest
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

from ai_builder.pg_execution import acquire_lease,execution_pool
from ai_builder.pg_ledger import PostgresLedger,digest
from ai_builder.pg_migrate import migrate
from ai_builder.providers import MeteredHTTP,OpenAIAdapter,ProviderFailure
from ai_builder.store import BudgetExceeded,Conflict,uid
from test_pg_ledger import reserve
from test_providers import pricing,Output,model_result,no_sleep

DSN=os.getenv('TEST_DATABASE_URL')
pytestmark=pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')


async def recovery_fixture(pool,config,*,study_limit='100'):
    admin=PostgresLedger(pool)
    study=await admin.create_study('US',study_limit)
    models=[uid(),uid()];parents=[await admin.create_run(study,config) for _ in models]
    coordinator=await admin.create_run(study,config);collection=uid()
    context=dict(execution_version='collection-3',selected_model_ids=models)
    packets=[dict(model_id=model,position=index+1,candidate={'plan':{'seed':{'base_model_id':model}}}) for index,model in enumerate(models)]
    async with pool.connection() as conn,conn.transaction():
        await conn.execute('''INSERT INTO app.collections(id,study_id,coordinator_run_id,context_hash,context,state,membership_hash,selected_count,frozen_at)
            VALUES (%s,%s,%s,%s,%s,'partial',%s,2,clock_timestamp())''',
            (collection,study,coordinator,digest(context),Jsonb(context),digest(packets)))
        for packet,parent in zip(packets,parents):
            await conn.execute('INSERT INTO app.collection_items(collection_id,model_id,position,candidate,run_id) VALUES (%s,%s,%s,%s,%s)',
                (collection,packet['model_id'],packet['position'],Jsonb(packet['candidate']),parent))
            research=packet['candidate']
            await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',
                               (parent,digest(research),Jsonb(research)))
    old=PostgresLedger(pool,parents[0],await acquire_lease(pool,parents[0],'old-fixture'))
    price=await old.register_pricing({'currency':'USD','version':'recovery-fixture'})
    actual=uid();unknown=uid()
    await reserve(old,price,'17.707590',actual);await old.settle(actual,'17.707590',{})
    await reserve(old,price,'7.078400',unknown);await old.mark_unknown(unknown)
    async with pool.connection() as conn,conn.transaction():
        await conn.execute("UPDATE app.run_leases SET expires_at=clock_timestamp()-interval '1second' WHERE run_id=%s",(parents[0],))
        for model,parent in zip(models,parents):
            await PostgresLedger.register_model_budget_family(conn,study_id=study,model_id=model,ancestor_run_ids=[parent])
        allowance=await PostgresLedger.create_recovery_allowance(conn,allowance_id=uid(),collection_id=collection,model_ids=models,
            authorization={'owner_decision':'Spend no more than $10 additionally on the two models'},extend_model_id=models[0])
    new=[]
    for model,parent in zip(models,parents):
        run=await admin.create_run(study,config)
        # Analytical ancestry remains independent of the operational recovery parent.
        research=dict(plan={'seed':{'base_model_id':model}},parent_run_id='older-source-parent',parent_collection_id='older-source-collection')
        async with pool.connection() as conn,conn.transaction():
            await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',(run,digest(research),Jsonb(research)))
            await PostgresLedger.bind_recovery_run(conn,allowance_id=allowance['id'],run_id=run,model_id=model,parent_run_id=parent)
            await PostgresLedger.bind_recovery_run(conn,allowance_id=allowance['id'],run_id=run,model_id=model,parent_run_id=parent)
        ledger=PostgresLedger(pool,run,await acquire_lease(pool,run,'recovery-fixture'))
        new.append(ledger)
    return dict(study=study,models=models,parents=parents,collection=collection,allowance=allowance,
                old=old,new=new,price=price,unknown=unknown)


def test_recovery_shared_ten_preserves_old_unknowns_family_and_resume(config):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            f=await recovery_fixture(pool,config)
            first,second=f['new']
            assert (await first.summary(first.run_id))['model_budget_family']['limit_micro']==35_000_000
            assert (await second.summary(second.run_id))['model_budget_family']['limit_micro']==25_000_000
            assert (await f['old'].summary(f['old'].run_id))['model_budget_family']['limit_micro']==25_000_000
            a=uid();b=uid()
            await reserve(first,f['price'],'6',a);await first.mark_unknown(a)
            await reserve(second,f['price'],'4',b)
            assert (await first.recovery_allowance())['accounted_micro']==10_000_000
            with pytest.raises(BudgetExceeded): await reserve(second,f['price'],'0.000001')
            assert (await PostgresLedger(pool,first.run_id,first.token).recovery_allowance())['remaining_micro']==0
            before=await f['old'].summary(f['old'].run_id)
            assert before['unknown_micro']==7_078_400 and before['calculated_micro']==17_707_590
            async with pool.connection() as conn,conn.transaction():
                same=await PostgresLedger.create_recovery_allowance(conn,allowance_id=f['allowance']['id'],collection_id=f['collection'],
                    model_ids=f['models'],authorization=f['allowance']['authorization'],extend_model_id=f['models'][0])
                assert same['baseline_accounted_micro']==24_785_990
                with pytest.raises(Conflict):
                    await PostgresLedger.create_recovery_allowance(conn,allowance_id=uid(),collection_id=f['collection'],
                        model_ids=f['models'],authorization=f['allowance']['authorization'],extend_model_id=f['models'][0])
            async with pool.connection() as conn:
                with pytest.raises(RaiseException):
                    await conn.execute('UPDATE app.recovery_allowances SET limit_micro=1 WHERE id=%s',(f['allowance']['id'],))
            assert (await first.recovery_allowance())['accounted_micro']==10_000_000
    asyncio.run(scenario())


def test_recovery_concurrent_attempts_cannot_each_take_ten(config):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            f=await recovery_fixture(pool,config)
            results=await asyncio.gather(*(reserve(ledger,f['price'],'6') for ledger in f['new']),return_exceptions=True)
            assert sum(result is True for result in results)==1
            assert sum(isinstance(result,BudgetExceeded) for result in results)==1
            assert (await f['new'][0].recovery_allowance())['accounted_micro']==6_000_000
    asyncio.run(scenario())


def test_recovery_cannot_expand_original_study_cap(config):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            f=await recovery_fixture(pool,config,study_limit='30')
            with pytest.raises(BudgetExceeded): await reserve(f['new'][0],f['price'],'6')
            assert (await f['new'][0].recovery_allowance())['accounted_micro']==0
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',[False,True])
def test_recovery_exact_count_reserve_replay_and_no_uncertain_automatic_retry(config,failure):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            f=await recovery_fixture(pool,config)
            ledger=f['new'][0];calls=[]
            async def handler(request):
                calls.append(request.url.path)
                if request.url.path.endswith('/input_tokens'):
                    return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':1000})
                if failure: raise httpx.ReadTimeout('lost response',request=request)
                return httpx.Response(200,json=model_result())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                adapter=OpenAIAdapter(MeteredHTTP(ledger,ledger.run_id,'factors',pricing(),client=client,sleep=no_sleep,
                                                operation_key='recovery-fixture-output'),'fixture',reasoning_effort='medium')
                if failure:
                    with pytest.raises(ProviderFailure,match='transport_unknown'):
                        await adapter.generate('English',{'small':'input'},Output)
                else:
                    assert (await adapter.generate('English',{'small':'input'},Output)).answer=='ok'
                    assert (await adapter.generate('English',{'small':'input'},Output)).answer=='ok'
            assert calls==['/v1/responses/input_tokens','/v1/responses']
            summary=await ledger.summary(ledger.run_id)
            assert len(summary['requests'])==1
            assert summary['requests'][0]['upper_micro']==255_760  # (1000+4096)*10/M +4096*50/M
            assert summary['requests'][0]['state']==('unknown' if failure else 'settled')
            assert (await f['old'].summary(f['old'].run_id))['unknown_micro']==7_078_400
    asyncio.run(scenario())
