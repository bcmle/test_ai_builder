"""Publish an existing completed live scope without executing a graph or an API.

This is an explicit server-side operator command. Legacy research retains its
original scenario and publication blockers; it is never upgraded into a final
ranking by exporting it with newer code.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from .config import Configuration
from .pg_execution import execution_pool
from .pipeline import ScopePacket
from .research_scope import ResearchRunResult
from .source_research import PreparedScope


async def publish_existing(pool, data_dir, run_id, metadata):
    from .public import public_model, publish_snapshot

    async with pool.connection() as conn, conn.transaction():
        await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        saved = await (await conn.execute('''
            SELECT r.config_json,r.created_at,r.status,s.scope,s.purpose,g.thread_id
            FROM app.runs r JOIN app.scope_inputs s ON s.run_id=r.id
            JOIN app.graph_threads g ON g.run_id=r.id AND g.scope_id=s.scope->>'scope_id'
            WHERE r.id=%s
        ''', (run_id,))).fetchone()
        if not saved or saved['purpose'] != 'live' or saved['status'] not in {'completed','partial'}:
            raise ValueError('A completed live assessment is required')
        active = await (await conn.execute('SELECT 1 FROM app.run_leases WHERE run_id=%s AND expires_at>clock_timestamp()', (run_id,))).fetchone()
        if active:
            raise ValueError('The assessment is still being written')
        checkpoint = await AsyncPostgresSaver(conn).aget_tuple({'configurable':{'thread_id':saved['thread_id']}})
        if not checkpoint or not checkpoint.checkpoint.get('channel_values',{}).get('card'):
            raise ValueError('Completed assessment card unavailable')
        state = dict(checkpoint.checkpoint['channel_values'])
        scope = ScopePacket.model_validate(saved['scope'])
        rows = await (await conn.execute('SELECT id,metadata FROM app.source_snapshots WHERE run_id=%s AND id=ANY(%s::text[])',
                    (run_id, [doc.snapshot_id for doc in scope.documents]))).fetchall()
        state['source_metadata'] = {row['id']:row['metadata'] for row in rows}
    config = Configuration.model_validate(saved['config_json'])
    result = ResearchRunResult(run_id, PreparedScope(scope, ()), state, {}, Path(data_dir), True)
    model = public_model(result, metadata, config)
    model['limitations'].append('This earlier assessment retains its original scenario; the new 20-model collection is a separate study.')
    snapshot = {
        'schema_version':'prototype-1', 'run_id':'legacy-'+run_id,
        'revision_id':'legacy-'+run_id, 'title':'First live assessment', 'market':config.market,
        'created_at':saved['created_at'].isoformat(),
        'published_at':datetime.now(timezone.utc).isoformat(),
        'status':'partial', 'expected_count':20, 'completed_count':1,
        'scenario':config.scenario.model_dump(mode='json',include={'team_size','business_budget_usd','days','jurisdiction'}), 'models':[model],
    }
    return publish_snapshot(data_dir, snapshot)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--run', required=True)
    cli.add_argument('--title', required=True)
    cli.add_argument('--summary', required=True)
    cli.add_argument('--buyer', default='Not specified')
    cli.add_argument('--revenue', default='Not specified')
    cli.add_argument('--comparable', default='')
    args = cli.parse_args()

    async def run():
        dsn = os.environ.get('DATABASE_URL')
        if not dsn:
            raise ValueError('DATABASE_URL is required')
        async with execution_pool(dsn) as pool:
            return await publish_existing(pool, Path(os.environ.get('AI_BUILDER_DATA','data')), args.run,
                {'id':'appointment-scheduling-software','title':args.title,'summary':args.summary,
                 'buyer':args.buyer,'revenue':args.revenue,'comparable':args.comparable,
                 'category':'Operations','status':'needs_review'})
    try:
        asyncio.run(run())
    except Exception as exc:
        cli.exit(1, f'Publication stopped: {type(exc).__name__}. No provider requests were made.\n')
    print('Existing live assessment published; no provider requests made.')


if __name__ == '__main__':
    main()
