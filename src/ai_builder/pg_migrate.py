"""Explicit PostgreSQL migration entry point; never called by web/worker startup."""
import argparse
import asyncio
import hashlib
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MIGRATIONS = Path(__file__).with_name('migrations')
# Stable application-specific lock. Never Python's process-randomized hash().
MIGRATION_LOCK = 418724210


async def migrate(dsn: str, directory: Path = MIGRATIONS) -> None:
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK,))
            await conn.execute('CREATE SCHEMA IF NOT EXISTS app')
            await conn.execute('CREATE SCHEMA IF NOT EXISTS checkpoints')
            await conn.execute('''CREATE TABLE IF NOT EXISTS app.schema_migrations (
                name TEXT PRIMARY KEY, checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())''')
            cur = await conn.execute('SELECT name, checksum FROM app.schema_migrations')
            applied = dict(await cur.fetchall())
            scripts = sorted(directory.glob('[0-9][0-9][0-9]_*.sql'))
            if set(applied) - {p.name for p in scripts}:
                raise RuntimeError('Database has migrations missing from this release')
            for path in scripts:
                body = path.read_bytes()
                digest = hashlib.sha256(body).hexdigest()
                if path.name in applied:
                    if applied[path.name] != digest:
                        raise RuntimeError(f'Migration checksum mismatch: {path.name}')
                    continue
                await conn.execute(body.decode('utf-8'), prepare=False)
                await conn.execute('INSERT INTO app.schema_migrations(name,checksum) VALUES (%s,%s)',
                                   (path.name, digest))
    # Saver setup uses its own migrations and autocommit (including concurrent indexes).
    # A session lock serializes that phase across explicit migration processes.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    async with await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, row_factory=dict_row,
        options='-c search_path=checkpoints') as conn:
        await conn.execute('SELECT pg_advisory_lock(%s)', (MIGRATION_LOCK,))
        try:
            await AsyncPostgresSaver(conn).setup()
        finally:
            await conn.execute('SELECT pg_advisory_unlock(%s)', (MIGRATION_LOCK,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        parser.error('DATABASE_URL is required (use migrator credentials)')
    asyncio.run(migrate(dsn))


if __name__ == '__main__':
    main()
