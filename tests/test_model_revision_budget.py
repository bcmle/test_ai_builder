import asyncio
import os
from copy import deepcopy

import pytest
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

from ai_builder.pg_execution import execution_pool, acquire_lease
from ai_builder.pg_ledger import PostgresLedger, digest
from ai_builder.pg_migrate import migrate
from ai_builder.store import BudgetExceeded, Conflict, uid
from test_pg_ledger import reserve

DSN = os.getenv('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')


async def model_run(pool, config, study, model, *, parent=None, limit='25', modern=False, bad_hash=False):
    from decimal import Decimal
    cfg = config.model_copy(update={'limits':config.limits.model_copy(update={'run_cost_usd':Decimal(limit)}),
                                    **({'schema_version':'1.3'} if modern else {})})
    admin = PostgresLedger(pool)
    run = await admin.create_run(study,cfg)
    coordinator = await admin.create_run(study,config)
    collection = uid()
    context, research = {}, {'plan':{'seed':{'base_model_id':model}}}
    candidate = deepcopy(research)
    if parent:
        context['reassessment_of_collection_id'] = parent['collection']
        research.update(parent_collection_id=parent['collection'],parent_run_id=parent['run'])
        candidate['parent_run_id'] = parent['run']
    packet = [{'model_id':model,'position':1,'candidate':candidate}]
    async with pool.connection() as conn, conn.transaction():
        await conn.execute('''INSERT INTO app.collections
            (id,study_id,coordinator_run_id,context_hash,context,state,membership_hash,selected_count,frozen_at)
            VALUES (%s,%s,%s,%s,%s,'partial',%s,1,clock_timestamp())''',
            (collection,study,coordinator,'invalid' if bad_hash else digest(context),Jsonb(context),digest(packet)))
        await conn.execute('''INSERT INTO app.collection_items(collection_id,model_id,position,candidate,run_id)
            VALUES (%s,%s,1,%s,%s)''', (collection,model,Jsonb(candidate),run))
        await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',
                           (run,digest(research),Jsonb(research)))
    token = await acquire_lease(pool,run,'family-test')
    ledger = PostgresLedger(pool,run,token)
    price = await ledger.register_pricing({'currency':'USD','version':'family-fixture'})
    return {'run':run,'collection':collection,'ledger':ledger,'price':price}


async def register(pool, study, model, runs, limit='25'):
    async with pool.connection() as conn, conn.transaction():
        return await PostgresLedger.register_model_budget_family(conn,study_id=study,model_id=model,
            ancestor_run_ids=[run['run'] for run in runs],limit=limit)


async def bind(pool, study, model, run, parent):
    async with pool.connection() as conn, conn.transaction():
        await PostgresLedger.bind_model_budget_run(conn,study_id=study,model_id=model,
            run_id=run['run'],parent_run_id=parent['run'])


def test_revision_family_accounts_all_ancestor_states_and_never_recharges_receipts(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            study = await PostgresLedger(pool).create_study('US','100')
            model = uid()
            original = await model_run(pool,config,study,model)
            request = uid()
            await reserve(original['ledger'],original['price'],'20',request)
            await original['ledger'].settle(request,'10',{})
            second = await model_run(pool,config,study,model,parent=original)
            unknown = uid()
            await reserve(second['ledger'],second['price'],'5',unknown)
            await second['ledger'].mark_unknown(unknown)
            await reserve(second['ledger'],second['price'],'3')
            released = uid()
            await reserve(second['ledger'],second['price'],'2',released)
            await second['ledger'].release(released,'No request was sent')
            before = await original['ledger'].summary(original['run'])
            await register(pool,study,model,[second,original])  # Caller order is irrelevant.
            third = await model_run(pool,config,study,model,parent=second,modern=True)
            await bind(pool,study,model,third,second)
            summary = await third['ledger'].summary(third['run'])
            assert summary['model_budget_family']['accounted_micro'] == 18_000_000
            assert summary['model_budget_family']['remaining_micro'] == 7_000_000
            await third['ledger'].receipt('scope','inherited-check','key','hash',{'parent':original['run']})
            assert (await third['ledger'].summary(third['run']))['requests'] == []
            await reserve(third['ledger'],third['price'],'7')
            with pytest.raises(BudgetExceeded):
                await reserve(third['ledger'],third['price'],'0.000001')
            after = await original['ledger'].summary(original['run'])
            assert before['requests'] == after['requests']
            assert after['model_budget_family']['accounted_micro'] == 25_000_000
    asyncio.run(check())


def test_parallel_revision_reservations_share_one_model_cap(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            study = await PostgresLedger(pool).create_study('US','100')
            model = uid()
            first = await model_run(pool,config,study,model)
            request = uid()
            await reserve(first['ledger'],first['price'],'20',request)
            await first['ledger'].settle(request,'20',{})
            await register(pool,study,model,[first])
            second = await model_run(pool,config,study,model,parent=first,modern=True)
            await bind(pool,study,model,second,first)
            third = await model_run(pool,config,study,model,parent=second,modern=True)
            await bind(pool,study,model,third,second)
            outcomes = await asyncio.gather(reserve(second['ledger'],second['price'],'4'),
                                           reserve(third['ledger'],third['price'],'4'),return_exceptions=True)
            assert sum(value is True for value in outcomes)==1
            assert sum(isinstance(value,BudgetExceeded) for value in outcomes)==1
            assert (await first['ledger'].summary(first['run']))['model_budget_family']['accounted_micro']==24_000_000
    asyncio.run(check())


def test_lower_historical_limit_is_sticky_and_current_run_limit_remains(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            study = await PostgresLedger(pool).create_study('US','100')
            model = uid()
            first = await model_run(pool,config,study,model,limit='10')
            request = uid()
            await reserve(first['ledger'],first['price'],'8',request)
            await first['ledger'].settle(request,'8',{})
            assert (await register(pool,study,model,[first]))['limit_micro']==10_000_000
            second = await model_run(pool,config,study,model,parent=first,modern=True)
            await bind(pool,study,model,second,first)
            with pytest.raises(BudgetExceeded):
                await reserve(second['ledger'],second['price'],'3')
            assert (await second['ledger'].summary(second['run']))['model_budget_family']['remaining_micro']==2_000_000
            # Even changing the historical live column cannot undo its frozen lower cap.
            async with pool.connection() as conn:
                await conn.execute('UPDATE app.runs SET limit_micro=25000000 WHERE id=%s',(first['run'],))
            assert (await first['ledger'].summary(first['run']))['model_budget_family']['limit_micro']==10_000_000
    asyncio.run(check())


def test_missing_or_ambiguous_ancestry_fails_before_any_operation(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            study = await PostgresLedger(pool).create_study('US','100')
            model = uid()
            first = await model_run(pool,config,study,model)
            second = await model_run(pool,config,study,model,parent=first)
            with pytest.raises(Conflict,match='all known'):
                await register(pool,study,model,[second])
            await register(pool,study,model,[first,second])
            third = await model_run(pool,config,study,model,parent=second,modern=True)
            with pytest.raises(Conflict,match='no verified'):
                await third['ledger'].begin_operation('no-http','input')
            with pytest.raises(Conflict):
                await reserve(third['ledger'],third['price'],'1')
            with pytest.raises(Conflict,match='incomplete'):
                await reserve(first['ledger'],first['price'],'1')
            with pytest.raises(Conflict,match='explicit parent'):
                await bind(pool,study,model,third,first)
            await bind(pool,study,model,third,second)
            await bind(pool,study,model,third,second)  # Idempotent restart.
            assert await reserve(third['ledger'],third['price'],'1')
            async with pool.connection() as conn:
                row = await (await conn.execute('SELECT count(*) AS n FROM app.provider_operations WHERE run_id=%s',
                                                (third['run'],))).fetchone()
                assert row['n']==0
    asyncio.run(check())


def test_shared_study_cap_includes_unselected_and_coordinator_history(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            study = await PostgresLedger(pool).create_study('US','100')
            outsiders = []
            for _ in range(4):
                other = await model_run(pool,config,study,uid())
                request = uid()
                await reserve(other['ledger'],other['price'],'24',request)
                await other['ledger'].settle(request,'24',{})
                outsiders.append(other)
            model = uid()
            first = await model_run(pool,config,study,model)
            await register(pool,study,model,[first])
            second = await model_run(pool,config,study,model,parent=first,modern=True)
            await bind(pool,study,model,second,first)
            with pytest.raises(BudgetExceeded):
                await reserve(second['ledger'],second['price'],'5')
            assert (await second['ledger'].summary(second['run']))['model_budget_family']['accounted_micro']==0
    asyncio.run(check())


def test_budget_family_membership_and_cap_are_database_immutable(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            study = await PostgresLedger(pool).create_study('US','100')
            model = uid()
            first = await model_run(pool,config,study,model)
            await register(pool,study,model,[first])
            await register(pool,study,model,[first])
            for sql,args in [
                ('UPDATE app.model_budget_families SET limit_micro=1 WHERE study_id=%s',(study,)),
                ('DELETE FROM app.model_budget_members WHERE run_id=%s',(first['run'],)),
                ('UPDATE app.model_budget_members SET parent_run_id=run_id WHERE run_id=%s',(first['run'],)),
            ]:
                async with pool.connection() as conn:
                    with pytest.raises(RaiseException,match='immutable'):
                        await conn.execute(sql,args)
    asyncio.run(check())


def test_unknown_ancestry_or_context_corruption_never_silently_excludes_spending(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            admin = PostgresLedger(pool)
            study = await admin.create_study('US','100')
            model = uid()
            first = await model_run(pool,config,study,model)
            orphan = await admin.create_run(study,config)
            context = {'plan':{'seed':{'base_model_id':model}}}
            async with pool.connection() as conn:
                await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',
                                   (orphan,digest(context),Jsonb(context)))
            with pytest.raises(Conflict,match='all known'):
                await register(pool,study,model,[first])
            with pytest.raises(Conflict,match='unambiguous'):
                await register(pool,study,model,[first,{'run':orphan}])
            bad_model = uid()
            bad = await model_run(pool,config,study,bad_model,bad_hash=True)
            with pytest.raises(Conflict,match='hash mismatch'):
                await register(pool,study,bad_model,[bad])
            wrong_model = uid()
            wrong = await model_run(pool,config,study,wrong_model,parent=first)
            with pytest.raises(Conflict,match='declared parent'):
                await register(pool,study,wrong_model,[wrong])
            async with pool.connection() as conn:
                with pytest.raises(Conflict,match='explicit transaction'):
                    await PostgresLedger.register_model_budget_family(conn,study_id=study,model_id=model,
                                                                     ancestor_run_ids=[first['run']])
                row = await (await conn.execute('SELECT count(*) AS n FROM app.model_budget_families WHERE study_id=%s',
                                                (study,))).fetchone()
                assert row['n']==0
    asyncio.run(check())


def test_unknown_parent_upper_bound_blocks_even_unmetered_new_operations(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            study = await PostgresLedger(pool).create_study('US','100')
            model = uid()
            first = await model_run(pool,config,study,model)
            request = uid()
            await reserve(first['ledger'],first['price'],'25',request)
            await first['ledger'].mark_unknown(request)
            await register(pool,study,model,[first])
            child = await model_run(pool,config,study,model,parent=first,modern=True)
            await bind(pool,study,model,child,first)
            with pytest.raises(BudgetExceeded):
                await child['ledger'].begin_operation('input-count-would-call-http','hash')
            async with pool.connection() as conn:
                state = await (await conn.execute('SELECT status FROM app.runs WHERE id=%s',(child['run'],))).fetchone()
                calls = await (await conn.execute('SELECT count(*) AS n FROM app.provider_operations WHERE run_id=%s',
                                                  (child['run'],))).fetchone()
                assert state['status']=='paused' and calls['n']==0
            summary = await first['ledger'].summary(first['run'])
            assert summary['unknown_micro']==25_000_000 and summary['model_budget_family']['remaining_micro']==0
    asyncio.run(check())


def test_new_lower_run_limit_constrains_future_family_revisions(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            study = await PostgresLedger(pool).create_study('US','100')
            model = uid()
            first = await model_run(pool,config,study,model)
            await register(pool,study,model,[first])
            child = await model_run(pool,config,study,model,parent=first,limit='5',modern=True)
            await bind(pool,study,model,child,first)
            later = await model_run(pool,config,study,model,parent=child,modern=True)
            await bind(pool,study,model,later,child)
            await reserve(child['ledger'],child['price'],'4')
            with pytest.raises(BudgetExceeded):
                await reserve(later['ledger'],later['price'],'2')
            assert (await later['ledger'].summary(later['run']))['model_budget_family']['limit_micro']==5_000_000
    asyncio.run(check())
