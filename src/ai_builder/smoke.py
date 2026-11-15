"""No-cost technical smoke: real graph/ledger/checkpointer, in-process HTTP fixtures.

This command never makes an external API request and refuses provider credentials.
It does not run migrations. Repeated invocations reuse the same synthetic run and
validate the stored checkpoint/ledger without replaying completed model calls.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile

import httpx
from psycopg.types.json import Jsonb

from .api import check_database
from .config import load_config
from .evidence import ArtifactStore
from .pg_execution import acquire_lease, execution_pool
from .pg_ledger import PostgresLedger, digest
from .pipeline import (ScopePacket, Document, ModelRoles, PROMPTS, PREFIX,
                       Research, Applicability, Critique, Neutrality, Verification,
                       Factors, build_scope_graph)
from .pricing import Pricing
from .report import render_report

SMOKE_LOCK = 418724212
FIXTURE_VERSION = 'technical-smoke-v1'


def fixture_scope():
    text = 'SYNTHETIC FIXTURE: subscription workflow software. Fictional valuation: $500 million.'
    return ScopePacket(scope_id=FIXTURE_VERSION, base_model_id='synthetic-base', variant_id='synthetic-variant',
        neutral_model_description='Synthetic workflow software — technical test, not market research',
        documents=[Document(snapshot_id='synthetic-snapshot',evidence_id='synthetic-evidence',
                            url='https://example.com/synthetic-fixture',text=text,
                            content_hash=hashlib.sha256(text.encode()).hexdigest(),policy_status='validated')])


def fixture_claim(identifier='synthetic-claim',kind='fact',quantity=None):
    return dict(claim_id=identifier, neutral_text='SYNTHETIC FIXTURE: subscription workflow software.',
        kind=kind, entity='Fictional fixture company', geography='US', period=None, metric=None,
        source_urls=['https://example.com/synthetic-fixture'],
        evidence_refs=[dict(evidence_id='synthetic-evidence',source_snapshot_id='synthetic-snapshot',
                            excerpt='SYNTHETIC FIXTURE: subscription workflow software.',locator='fixture paragraph')],
        formula=None,input_claim_ids=[],quantity_estimate=quantity,affected_criterion_ids=[])


class FixtureHTTP:
    schemas = {'research':Research,'applicability':Applicability,'critic':Critique,'repair':Critique,
               'neutrality':Neutrality,'verifier':Verification,'factors':Factors}

    def __init__(self):
        self.calls = 0

    def __call__(self, request):
        if str(request.url) != 'https://api.openai.com/v1/responses':
            raise RuntimeError('Unexpected fixture endpoint')
        body=json.loads(request.content)
        role=next((r for r,prompt in PROMPTS.items() if PREFIX+prompt==body['instructions']),None)
        packet=json.loads(body['input'][0]['content'])
        if role=='research':
            result={'criteria':[dict(criterion_id=f'F{i:02}',state='completed',result='pass',
                evidence_ids=['synthetic-evidence'],reason='Synthetic fixture assertion, not real-world evidence') for i in range(1,11)]}
        elif role=='applicability':
            result=dict(status='supports_pass',reason='Synthetic applicability fixture')
        elif role in {'critic','repair'}:
            result=dict(risks=[dict(risk_id='synthetic-risk',impact=3,critic_order=0,
                premise_claim_ids=['synthetic-claim'],essential_premise_ids=['synthetic-claim'])],
                claims=[fixture_claim()],limitations=['All businesses and evidence here are synthetic'])
        elif role=='neutrality':
            result=dict(status='accept',reason='Synthetic neutral wording')
        elif role=='verifier':
            if set(packet)!={'assessment_scope_id','base_model_id','variant_id','neutral_model_description',
                             'launch_context','claims','documents'}:
                raise RuntimeError('Verifier input boundary failed')
            result=dict(items=[dict(claim_id='synthetic-claim',status='supported',evidence_ids=['synthetic-evidence'],
                                   reason='Only supported within the synthetic fixture')])
        elif role=='factors':
            result=dict(factors=[dict(factor_id=f'S{i:02}',value=dict(low=2,high=3),claim_ids=['synthetic-estimate']) for i in range(1,6)],
                claims=[fixture_claim('synthetic-estimate','judgment'),fixture_claim('synthetic-days','judgment',dict(low=30,high=60)),
                        fixture_claim('synthetic-cost','judgment',dict(low=100,high=500))],
                days=dict(low=30,high=60),days_claim_id='synthetic-days',cost=dict(low=100,high=500),cost_claim_id='synthetic-cost',
                resources='available',resources_claim_id='synthetic-estimate',test_plan_claim_ids=['synthetic-estimate'],
                limitations=['Synthetic test estimates, not a business forecast'])
        else:
            raise RuntimeError('Unknown fixture role')
        output=self.schemas[role].model_validate(result)
        self.calls+=1
        return httpx.Response(200,json={'status':'completed',
            'usage':{'input_tokens':100,'output_tokens':20,'input_tokens_details':{'cached_tokens':0},
                     'output_tokens_details':{'reasoning_tokens':5}},
            'output':[{'type':'message','content':[{'type':'output_text','text':output.model_dump_json()}]}]})


def atomic_write(path, content):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        temporary=Path(f.name)
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(temporary,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


async def run_smoke(dsn, data_dir, config_path):
    if any(os.environ.get(k) for k in ('OPENAI_API_KEY','TAVILY_API_KEY','ASTRA_API_KEY','LANGSMITH_API_KEY','LANGCHAIN_API_KEY')):
        raise RuntimeError('Provider credentials must not be present in technical smoke')
    if any(os.environ.get(k,'').lower() in {'1','true','yes'} for k in ('LANGSMITH_TRACING','LANGCHAIN_TRACING_V2')):
        raise RuntimeError('External tracing must be disabled in technical smoke')
    config=load_config(config_path)
    scope=fixture_scope()
    pricing=Pricing(provider='openai',model=config.model,effective_date='2026-11-15',
        source_url='https://example.com/synthetic-fixture-tariff',currency='USD',input_per_million=10,
        cached_input_per_million=1,output_per_million=50,per_credit=0,input_context_limit=272000)
    settings={role:dict(reasoning_effort='low',max_output_tokens=4096) for role in PROMPTS}
    identity=digest({'fixture':FIXTURE_VERSION,'config':config.digest(),'prompts':PROMPTS,'settings':settings})
    scope=scope.model_copy(update={'scope_id':FIXTURE_VERSION+'-'+identity})
    data=Path(data_dir)
    async with execution_pool(dsn) as pool:
        await check_database(pool)
        async with pool.connection() as guard:
            locked=await (await guard.execute('SELECT pg_try_advisory_lock(%s) AS locked',(SMOKE_LOCK,))).fetchone()
            if not locked['locked']:
                raise RuntimeError('Another technical smoke is active')
            try:
                admin=PostgresLedger(pool)
                async with pool.connection() as conn:
                    saved=await (await conn.execute("SELECT s.run_id FROM app.scope_inputs s JOIN app.runs r ON r.id=s.run_id WHERE s.purpose='eval' AND s.scope->>'scope_id'=%s AND r.config_hash=%s ORDER BY r.created_at LIMIT 1",
                                                  (scope.scope_id,config.digest()))).fetchone()
                if saved:
                    run_id=saved['run_id']
                else:
                    study=await admin.create_study(config.market,'25')
                    run_id=await admin.create_run(study,config)
                    async with pool.connection() as conn, conn.transaction():
                        await conn.execute('INSERT INTO app.scope_inputs VALUES (%s,%s,%s,%s,%s)',
                                           (run_id,Jsonb(scope.model_dump(mode='json')),Jsonb(settings),Jsonb(pricing.model_dump(mode='json')),'eval'))
                        await conn.execute("UPDATE app.run_commands SET state='applied' WHERE run_id=%s AND kind='start'",(run_id,))
                token=await acquire_lease(pool,run_id,'technical-smoke')
                ledger=PostgresLedger(pool,run_id,token)
                fixture=FixtureHTTP()
                try:
                    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture),trust_env=False) as client:
                        roles=ModelRoles(ledger,pricing,'synthetic-fixture-key',settings,purpose='eval',client=client)
                        graph,thread_config=await build_scope_graph(ledger,config,scope,roles)
                        snapshot=await graph.aget_state(thread_config)
                        if snapshot.values.get('card') and not snapshot.next:
                            state=snapshot.values
                        else:
                            remaining=await ledger.start_clock()
                            async with asyncio.timeout(min(remaining,90)):
                                state=await graph.ainvoke(None if snapshot.values else {},thread_config,durability='sync')
                            await ledger.stop_clock('completed')
                    usage=await ledger.summary(run_id)
                    if state['card']['eligibility']!='pass' or len(usage['requests'])!=15 or usage['calculated_micro']!=30000:
                        raise RuntimeError('Synthetic expected graph/ledger result mismatch')
                    if usage['reserved_micro'] or usage['unknown_micro']:
                        raise RuntimeError('Synthetic ledger contains unfinished requests')
                    if any(r['purpose']!='eval' for r in usage['requests']):
                        raise RuntimeError('Smoke unexpectedly contains non-eval usage')
                    artifacts=ArtifactStore(data/'evidence')
                    source_refs=[artifacts.put(d.text.encode()) for d in scope.documents]
                    evidence_manifest=artifacts.manifest(source_refs,{'kind':FIXTURE_VERSION,'run_id':run_id})
                    artifacts.verify_manifest(evidence_manifest)
                    report=render_report(scope,state,usage,mode='replay').encode()
                    manifest={'kind':'synthetic-technical-preview-v1','fixture_version':identity,'run_id':run_id,
                              'report_sha256':hashlib.sha256(report).hexdigest(),'evidence_manifest':evidence_manifest,
                              'external_api_calls':0,'fixture_http_calls_this_invocation':fixture.calls,
                              'synthetic_calculated_micro':usage['calculated_micro'],'physical_attempts':len(usage['requests'])}
                    atomic_write(data/'technical-preview.html',report)
                    atomic_write(data/'technical-preview.json',json.dumps(manifest,sort_keys=True).encode())
                    return manifest
                finally:
                    async with pool.connection() as conn:
                        await conn.execute('UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s AND token=%s',(run_id,token))
            finally:
                await guard.execute('SELECT pg_advisory_unlock(%s)',(SMOKE_LOCK,))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    dsn=os.environ.get('DATABASE_URL')
    if not dsn:
        parser.error('DATABASE_URL is required')
    try:
        result=asyncio.run(run_smoke(dsn,os.environ.get('AI_BUILDER_DATA','data'),
                                    os.environ.get('AI_BUILDER_CONFIG','config/baseline.json')))
    except Exception as exc:
        parser.exit(1,f'Technical smoke failed: {type(exc).__name__}.\n')
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':
    main()
