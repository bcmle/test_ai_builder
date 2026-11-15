"""Run one prepared assessment through PostgreSQL/LangGraph and export a private draft.

Explicit live entry point. No migrations, discovery, deployment or publication.
Use fixtures/tests for offline validation; this command spends the configured API budget.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from psycopg.types.json import Jsonb

from .config import load_config, Configuration
from .pipeline import ScopePacket, ModelRoles, execute_scope
from .pg_execution import execution_pool, acquire_lease, renew_lease
from .pg_ledger import PostgresLedger
from .pricing import Pricing
from .report import render_report
from .store import Conflict


async def run(args):
    dsn=os.environ.get('DATABASE_URL')
    key=os.environ.get('OPENAI_API_KEY')
    if not dsn or not key:
        raise ValueError('DATABASE_URL and OPENAI_API_KEY are required')
    async with execution_pool(dsn) as pool:
        admin=PostgresLedger(pool)
        if args.resume_run:
            run_id=args.resume_run
            async with pool.connection() as conn:
                saved=await (await conn.execute('''SELECT s.*,r.config_json FROM app.scope_inputs s
                    JOIN app.runs r ON r.id=s.run_id WHERE run_id=%s''',(run_id,))).fetchone()
            if not saved:
                raise ValueError('No saved scope input for this run')
            config=Configuration.model_validate(saved['config_json'])
            scope=ScopePacket.model_validate(saved['scope'])
            pricing=Pricing.model_validate(saved['pricing'])
            settings=saved['role_settings']
        else:
            if not all([args.scope,args.pricing,args.settings,args.study_budget]):
                raise ValueError('New run requires scope, pricing, settings and an explicit study budget')
            config=load_config(args.config)
            scope=ScopePacket.model_validate_json(Path(args.scope).read_text())
            pricing=Pricing.model_validate_json(Path(args.pricing).read_text())
            settings=json.loads(Path(args.settings).read_text())
            # Reject settings/model incompatibility before creating any paid work.
            ModelRoles(admin,pricing,key,settings)
            if pricing.provider!='openai' or pricing.model!=config.model:
                raise ValueError('Pricing model must match run model')
            study=await admin.create_study(config.market,args.study_budget)
            run_id=await admin.create_run(study,config)
            async with pool.connection() as conn:
                await conn.execute('INSERT INTO app.scope_inputs VALUES (%s,%s,%s,%s,%s)',
                    (run_id,Jsonb(scope.model_dump(mode='json')),Jsonb(settings),Jsonb(pricing.model_dump(mode='json')),'live'))
        token=await acquire_lease(pool,run_id,'scope-cli')
        ledger=PostgresLedger(pool,run_id,token)
        async def heartbeat():
            while True:
                await asyncio.sleep(15)
                await renew_lease(pool,run_id,token)
        heart=asyncio.create_task(heartbeat())
        status='paused'
        print(f'Run: {run_id}',flush=True)
        try:
            remaining=await ledger.start_clock()
            roles=ModelRoles(ledger,pricing,key,settings,deadline=time.monotonic()+remaining)
            async with asyncio.timeout(remaining):
                state=await execute_scope(ledger,config,scope,roles,resume=bool(args.resume_run))
            status='partial'  # card applicability/publication and discovery are not complete
            usage=await ledger.summary(run_id)
            output=Path(args.output or f'outputs/{run_id}.html')
            output.parent.mkdir(parents=True,exist_ok=True)
            # Private file: no route makes this accessible to public visitors.
            descriptor=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(descriptor,'w') as f:
                f.write(render_report(scope,state,usage,mode='live'))
            print(f'Private draft: {output.resolve()}',flush=True)
        finally:
            heart.cancel()
            await asyncio.gather(heart,return_exceptions=True)
            try:
                await ledger.stop_clock(status)
            except Conflict:
                pass  # superseded worker must not overwrite its successor
            async with pool.connection() as conn:
                await conn.execute('UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s AND token=%s',(run_id,token))
        return run_id


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config/baseline.json')
    parser.add_argument('--scope')
    parser.add_argument('--pricing')
    parser.add_argument('--settings')
    parser.add_argument('--study-budget')
    parser.add_argument('--resume-run')
    parser.add_argument('--output')
    args=parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        # Do not echo credentials/DSNs/provider payloads from library exceptions.
        parser.exit(1,f'Run stopped: {type(exc).__name__}. Inspect the private run audit.\n')


if __name__=='__main__':
    main()
