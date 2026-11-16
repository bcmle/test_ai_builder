"""Five-card selection, frozen source reuse and immutable public history."""
import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from ai_builder.collection import CollectionContext, _ensure_child, run_collection
from ai_builder.config import Configuration, load_config
from ai_builder.evidence import ArtifactStore
from ai_builder.factor_revision import create_factor_revision
from ai_builder.fetch_transport import FetchError
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_migrate import migrate
from ai_builder.public import load_snapshot, publish_snapshot
from ai_builder.reassess_collection import create_reassessment
from ai_builder.store import Conflict
from ai_builder.upstream_revision import upstream_prefix
from test_collection import Providers, cleanup, context

DSN = os.getenv('TEST_DATABASE_URL')


def test_upstream_prefix_never_imports_factors_or_uncompleted_nodes():
    state = dict(research={'criteria':[]}, legal_reviews=[], criteria=[], eligibility='conditional',
        eligibility_conditions=[], applicability_reviews=[], critique={'claims':[]},
        neutral=True, handoff=[], verification={'items':[]}, reconciliation={},
        factors={'untrusted':'old score'}, card={'score':99}, factor_neutral=True)
    through, selected = upstream_prefix(state)
    assert through == 'reconcile'
    assert not {'factors','card','factor_neutral'} & selected.keys()
    through, selected = upstream_prefix({k:v for k,v in state.items() if k not in {'neutral','handoff','verification','reconciliation'}})
    assert through == 'critic' and 'verification' not in selected
    assert upstream_prefix({}) == (None,{})
    state['eligibility'] = 'unresolved'
    assert upstream_prefix(state)[0] == 'reconcile'
    early = {k:v for k,v in state.items() if k in {'research','legal_reviews','criteria','eligibility','eligibility_conditions','applicability_reviews'}}
    assert upstream_prefix(early)[0] == 'applicability'


def milestone_config(prior):
    template = load_config(Path(__file__).parents[1]/'config/milestone-config.json')
    return Configuration.model_validate({**prior.model_dump(mode='json'), 'schema_version':'1.3',
        'scoring_policy_version':'1.5.3', 'factors':[f.model_dump(mode='json') for f in template.factors]})


@pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')
def test_five_selected_source_replay_one_by_one_preserves_other_fifteen(config,tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            artifacts = ArtifactStore(tmp_path/'evidence')
            old_context = context(config)
            async with httpx.AsyncClient(transport=httpx.MockTransport(Providers(pool))) as client:
                original = await run_collection(pool,artifacts,context=old_context,openai_key='fixture',
                    tavily_key='fixture',provider_client=client,data_dir=tmp_path)
            async with pool.connection() as conn:
                old = await (await conn.execute('SELECT * FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                               (original.collection_id,))).fetchall()
            conditional = Configuration.model_validate({**old_context.config.model_dump(mode='json'),
                'schema_version':'1.2','scoring_policy_version':'1.5.2'})
            analogues = {row['model_id']:[dict(analogue_id='fixture',company_name='Fixture',product_name='Fixture',
                official_urls=['https://example.com'])] for row in old}
            parent = await create_reassessment(pool,parent_collection_id=original.collection_id,config=conditional,
                role_settings=old_context.role_settings,analogues_by_model=analogues)
            new_id = None
            try:
                async def empty_sources(request):
                    assert request.url.host == 'api.tavily.com'
                    return httpx.Response(200,json={'usage':{'credits':1},'results':[]})
                async def inaccessible(url):
                    raise FetchError('http_404',url)
                async with httpx.AsyncClient(transport=httpx.MockTransport(empty_sources)) as client:
                    await run_collection(pool,artifacts,resume_collection=parent,openai_key='fixture',tavily_key='fixture',
                                         provider_client=client,fetch_document=inaccessible,data_dir=tmp_path)
                original_bytes = original.snapshot.read_bytes()
                parent_path = tmp_path/'public'/f'{parent}.json'
                parent_bytes = parent_path.read_bytes()
                parent_public = json.loads(parent_bytes)
                roles = {**old_context.role_settings,'factor_neutrality':old_context.role_settings['neutrality']}
                new_id = await create_factor_revision(pool,artifacts,parent_collection_id=parent,
                    config=milestone_config(conditional),role_settings=roles,data_dir=tmp_path)
                async with pool.connection() as conn:
                    row = await (await conn.execute('SELECT context FROM app.collections WHERE id=%s',(new_id,))).fetchone()
                    items = await (await conn.execute('SELECT * FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                                     (new_id,))).fetchall()
                frozen = CollectionContext.model_validate(row['context'])
                assert frozen.selected_model_ids == tuple(i['model_id'] for i in items[:5])
                with pytest.raises(Conflict,match='not selected'):
                    await _ensure_child(None,new_id,items[5],frozen)
                seen = []
                def publish(directory,snapshot):
                    path = publish_snapshot(directory,snapshot)
                    seen.append(load_snapshot(directory,new_id))
                    return path
                async def no_calls(request):
                    raise AssertionError('Frozen source-gap revision must not call any external provider')
                async with httpx.AsyncClient(transport=httpx.MockTransport(no_calls)) as client:
                    result = await run_collection(pool,artifacts,resume_collection=new_id,data_dir=tmp_path,
                        openai_key='fixture',tavily_key='fixture',provider_client=client,snapshot_publisher=publish)
                    replay = await run_collection(pool,artifacts,resume_collection=new_id,data_dir=tmp_path,
                        provider_client=client)
                assert result.selected_count == result.completed_count == replay.completed_count == 5
                assert {s['completed_count'] for s in seen} >= set(range(6))
                latest = load_snapshot(tmp_path,new_id)
                assert latest['catalog_count'] == len(latest['models']) == 20
                assert latest['expected_count'] == latest['selected_count'] == 5
                assert len(latest['selected_model_ids']) == 5
                assert all(m['score'] is None for m in latest['models'])
                prior_models = {m['id']:m for m in parent_public['models']}
                for model in latest['models'][5:]:
                    assert model == prior_models[model['id']]
                    assert model['id'] in latest['retained_assessments']
                async with pool.connection() as conn:
                    items = await (await conn.execute('SELECT * FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                                     (new_id,))).fetchall()
                    usage = await (await conn.execute('SELECT count(*) AS n FROM app.usage WHERE run_id=ANY(%s::text[])',
                        ([i['run_id'] for i in items if i['run_id']],))).fetchone()
                assert all(i['run_id'] for i in items[:5]) and all(i['run_id'] is None for i in items[5:])
                assert usage['n'] == 0
                assert original.snapshot.read_bytes() == original_bytes and parent_path.read_bytes() == parent_bytes
            finally:
                await cleanup(pool,new_id)
                await cleanup(pool,parent)
    asyncio.run(check())
