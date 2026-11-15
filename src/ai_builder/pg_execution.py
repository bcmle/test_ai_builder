"""Async PostgreSQL execution primitives (R18/A56).

A saver is bound to one run lease. Every saver cursor, including pending writes,
uses the same transaction as the lease row lock. No network calls belong here.
"""
from contextlib import asynccontextmanager
from uuid import uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .store import Conflict


def execution_pool(dsn: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False,
        kwargs={'autocommit': True, 'row_factory': dict_row,
                'options': '-c search_path=checkpoints,app -c statement_timeout=15000'})


async def acquire_lease(pool, run_id: str, owner_id: str, seconds: int = 150) -> str:
    if seconds <= 0:
        raise ValueError('Lease duration must be positive')
    token = uuid4().hex
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute('''INSERT INTO app.run_leases(run_id,token,owner_id,expires_at)
            VALUES (%s,%s,%s,clock_timestamp() + %s * interval '1 second')
            ON CONFLICT(run_id) DO UPDATE SET token=EXCLUDED.token,
              owner_id=EXCLUDED.owner_id, expires_at=EXCLUDED.expires_at
            WHERE app.run_leases.expires_at <= clock_timestamp()
            RETURNING token''', (run_id, token, owner_id, seconds))
        if await cur.fetchone() is None:
            raise Conflict('Run already has an active owner')
    return token


async def renew_lease(pool, run_id: str, token: str, seconds: int = 150):
    if seconds <= 0:
        raise ValueError('Lease duration must be positive')
    async with pool.connection() as conn, conn.transaction():
        await assert_lease(conn, run_id, token)
        await conn.execute('''UPDATE app.run_leases
            SET expires_at=clock_timestamp() + %s * interval '1 second'
            WHERE run_id=%s''', (seconds, run_id))


async def assert_lease(conn, run_id: str, token: str):
    # Lock first, then inspect expiry: time may pass while waiting for the row lock.
    cur = await conn.execute('SELECT token FROM app.run_leases WHERE run_id=%s FOR UPDATE', (run_id,))
    row = await cur.fetchone()
    if row is None or row['token'] != token:
        raise Conflict('Superseded or missing run lease')
    cur = await conn.execute('SELECT expires_at > clock_timestamp() AS active '
                             'FROM app.run_leases WHERE run_id=%s', (run_id,))
    if not (await cur.fetchone())['active']:
        raise Conflict('Expired run lease')


class FencedPostgresSaver(AsyncPostgresSaver):
    """Pinned saver integration: _cursor is covered by real DB contract tests.

    Do not use for setup/pruning. Those are explicit maintenance operations after
    workers are stopped. One instance belongs to one run, never shared across runs.
    """
    def __init__(self, pool: AsyncConnectionPool, run_id: str, token: str, thread_id: str | None = None):
        super().__init__(pool)
        self.run_id, self.token = run_id, token
        self.thread_id = thread_id

    @asynccontextmanager
    async def _cursor(self, *, pipeline=False):
        # Deliberately use a transaction rather than a pipeline: saver blob,
        # checkpoint and pending-write batches must commit under the row lock.
        async with self.conn.connection() as conn, conn.transaction():
            await assert_lease(conn, self.run_id, self.token)
            async with conn.cursor(binary=True, row_factory=dict_row) as cur:
                yield cur
            await assert_lease(conn, self.run_id, self.token)

    async def setup(self):
        raise RuntimeError('Run python -m ai_builder.pg_migrate with migrator credentials')

    async def adelete_thread(self, thread_id):
        raise RuntimeError('Checkpoint deletion is not a runtime operation')

    async def aprune(self, *args, **kwargs):
        raise RuntimeError('Checkpoint pruning is not a runtime operation')

    def _check_thread(self, config):
        if self.thread_id is not None and (config or {}).get('configurable',{}).get('thread_id') != self.thread_id:
            raise Conflict('Checkpoint thread is outside the bound scope')

    async def aget_tuple(self, config):
        self._check_thread(config)
        return await super().aget_tuple(config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        self._check_thread(config)
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=''):
        self._check_thread(config)
        return await super().aput_writes(config, writes, task_id, task_path)

    async def alist(self, config, **kwargs):
        self._check_thread(config)
        async for item in super().alist(config, **kwargs):
            yield item

    async def aget_delta_channel_writes(self, config, channels):
        self._check_thread(config)
        return await super().aget_delta_channel_writes(config, channels)
