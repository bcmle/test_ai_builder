"""Real PostgreSQL checks; use a dedicated test database, never production."""
import asyncio
import os
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

from ai_builder.pg_migrate import migrate, MIGRATIONS
from ai_builder.pg_execution import execution_pool, acquire_lease, renew_lease, FencedPostgresSaver
from ai_builder.store import Conflict

DSN = os.getenv('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='TEST_DATABASE_URL required for real PostgreSQL')


async def seed(pool):
    run = uuid4().hex
    async with pool.connection() as conn:
        await conn.execute('INSERT INTO app.studies VALUES (%s,%s,%s)', (run, 'US', 50000000))
        await conn.execute('''INSERT INTO app.runs(id,study_id,config_json,config_hash,limit_micro,
            status,graph_version,state_schema_version,coordinator_thread_id)
            VALUES (%s,%s,'{}','fixture',25000000,'running','test','1',%s)''', (run,run,run))
    return run


def test_migration_idempotence_and_checksum(tmp_path):
    async def check():
        await migrate(DSN)
        await migrate(DSN)
        for path in MIGRATIONS.glob('*.sql'):
            (tmp_path / path.name).write_bytes(path.read_bytes() + b'\n-- changed\n')
        with pytest.raises(RuntimeError, match='checksum'):
            await migrate(DSN, tmp_path)
    asyncio.run(check())


class State(TypedDict):
    value: int


def test_graph_interrupt_restart_and_stale_writer():
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            run = await seed(pool)
            token = await acquire_lease(pool, run, 'worker-a')
            await renew_lease(pool, run, token)
            with pytest.raises(Conflict):
                await acquire_lease(pool, run, 'worker-b')
            saver = FencedPostgresSaver(pool, run, token)
            builder = StateGraph(State)
            builder.add_node('review', lambda state: {'value': interrupt('Approve value')})
            builder.add_edge(START, 'review')
            builder.add_edge('review', END)
            config = {'configurable': {'thread_id': run}}
            graph = builder.compile(checkpointer=saver)
            first = await graph.ainvoke({'value': 0}, config, durability='sync')
            assert first['__interrupt__']
            # Replace both pool and saver to model reconnection/restart.
        async with execution_pool(DSN) as pool:
            saver = FencedPostgresSaver(pool, run, token)
            graph = builder.compile(checkpointer=saver)
            assert (await graph.ainvoke(Command(resume=42), config, durability='sync'))['value'] == 42
            saved = await saver.aget_tuple(config)
            async with pool.connection() as conn:
                await conn.execute("UPDATE app.run_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (run,))
            new_token = await acquire_lease(pool, run, 'worker-b')
            with pytest.raises(Conflict):
                await saver.aput(saved.config, saved.checkpoint, saved.metadata, {})
            with pytest.raises(Conflict):
                await saver.aput_writes(saved.config, [('value', 99)], 'late-task')
            fresh = FencedPostgresSaver(pool, run, new_token)
            assert (await fresh.aget_tuple(config)).checkpoint['channel_values']['value'] == 42
    asyncio.run(check())


def test_fencing_transaction_rolls_back_and_serializes_takeover():
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            run = await seed(pool)
            token = await acquire_lease(pool, run, 'a')
            saver = FencedPostgresSaver(pool, run, token)
            with pytest.raises(RuntimeError, match='injected'):
                async with saver._cursor() as cur:
                    await cur.execute("UPDATE app.runs SET status='failed' WHERE id=%s", (run,))
                    raise RuntimeError('injected failure before commit')
            async with pool.connection() as conn:
                row = await (await conn.execute('SELECT status FROM app.runs WHERE id=%s', (run,))).fetchone()
                assert row['status'] == 'running'
            started = asyncio.Event()
            async def replace_token():
                async with pool.connection() as conn:
                    started.set()
                    await conn.execute('UPDATE app.run_leases SET token=%s WHERE run_id=%s', ('replacement', run))
            async with saver._cursor() as cur:
                task = asyncio.create_task(replace_token())
                await started.wait()
                await asyncio.sleep(0.05)
                assert not task.done()  # competing ownership change cannot pass row lock
                await cur.execute('SELECT 1')
            await task
            with pytest.raises(Conflict):
                async with saver._cursor():
                    pass
    asyncio.run(check())
