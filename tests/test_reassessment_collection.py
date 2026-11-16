import asyncio
import json
import os

import httpx
import pytest
from psycopg.types.json import Jsonb

from ai_builder.collection import CollectionContext, run_collection
from ai_builder.config import Configuration
from ai_builder.evidence import ArtifactStore
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_migrate import migrate
from ai_builder.public import load_snapshot, public_model, publish_snapshot
from ai_builder.reassess_collection import create_reassessment, priority_key
from ai_builder.store import BudgetExceeded, Conflict
from test_collection import Providers, cleanup, context

DSN = os.environ.get('TEST_DATABASE_URL')


def item(identifier, position, unknown, f10):
    return dict(model_id=identifier, position=position, public_model={'criteria': [
        {'id': f'F{i:02}', 'result': ('unknown' if i <= unknown else 'pass') if i < 10 else f10}
        for i in range(1, 11)]})


def test_priority_f10_then_unknown_then_requested_tie_without_incomplete_shortcut():
    values = [item('nonpass', 1, 0, 'unknown'), item('three', 2, 3, 'pass'),
              item('first', 3, 2, 'pass'), item('two', 4, 2, 'pass'),
              dict(model_id='unfinished', position=5, public_model=None)]
    assert [x['model_id'] for x in sorted(values, key=lambda x: priority_key(x, 'first'))] == [
        'first', 'two', 'three', 'nonpass', 'unfinished']
    unfinished = item('null-results', 1, 0, None)
    for criterion in unfinished['public_model']['criteria']:
        criterion['result'] = None
    assert priority_key(unfinished)[1] == 10


def test_legacy_collection_context_serialization_unchanged(config):
    old = context(config).model_dump(mode='json')
    assert 'reassessment_of_collection_id' not in old and 'processing_order' not in old
    assert CollectionContext.model_validate(old).model_dump(mode='json') == old


@pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')
def test_same_study_frozen_priority_sequential_publication_and_budget_stop(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            old_context = context(config)
            fixture = Providers(pool)
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                original = await run_collection(pool, ArtifactStore(tmp_path/'evidence'), context=old_context,
                    openai_key='fixture', tavily_key='fixture', provider_client=client, data_dir=tmp_path)
            original_bytes = original.snapshot.read_bytes()
            async with pool.connection() as conn:
                old = await (await conn.execute('SELECT * FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                                (original.collection_id,))).fetchall()
                for i, row in enumerate(old):
                    projection = row['public_model']
                    projection['criteria'] = item(row['model_id'], i+1, 2 if i < 2 else 5,
                                                  'pass' if i < 3 else 'unknown')['public_model']['criteria']
                    await conn.execute('UPDATE app.collection_items SET public_model=%s WHERE collection_id=%s AND model_id=%s',
                                       (Jsonb(projection), original.collection_id, row['model_id']))
            new_config = Configuration.model_validate({**old_context.config.model_dump(mode='json'),
                                                       'schema_version':'1.2', 'scoring_policy_version':'1.5.2'})
            analogues = {r['model_id']: [dict(analogue_id='fixture-product', company_name='Fixture Company',
                product_name='Fixture Product', official_urls=['https://example.com/'])] for r in old}
            new_id = await create_reassessment(pool, parent_collection_id=original.collection_id,
                config=new_config, role_settings=old_context.role_settings, analogues_by_model=analogues,
                preferred_model_id=old[1]['model_id'])
            try:
                async with pool.connection() as conn:
                    saved = await (await conn.execute('SELECT * FROM app.collections WHERE id=%s', (new_id,))).fetchone()
                    members = await (await conn.execute('SELECT * FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                                        (new_id,))).fetchall()
                assert saved['study_id'] == original.study_id
                assert [m['model_id'] for m in members[:3]] == [old[1]['model_id'],old[0]['model_id'],old[2]['model_id']]
                assert all(m['candidate']['plan']['schema_version']=='3' for m in members)
                assert all(m['candidate']['plan']['seed']['scope_id'] != old[0]['candidate']['plan']['seed']['scope_id'] for m in members)
                with pytest.raises(Conflict, match='active'):
                    await create_reassessment(pool, parent_collection_id=original.collection_id, config=new_config,
                                              role_settings=old_context.role_settings, analogues_by_model=analogues)
                seen, calls = [], []

                def publish(directory, snapshot):
                    path = publish_snapshot(directory, snapshot)
                    seen.append(load_snapshot(directory, new_id))
                    return path

                async def run_child(pool, artifacts, **kwargs):
                    calls.append(kwargs['resume_run'])
                    if len(calls) == 2:
                        assert any(s['completed_count']==1 for s in seen), 'First card must be public before second child'
                        raise BudgetExceeded('Synthetic cumulative budget stop')
                    return None

                def project(result, metadata, frozen_config):
                    status = metadata.get('status') or 'unresolved'
                    return public_model(None, {**metadata, 'status':status}, frozen_config)

                result = await run_collection(pool, ArtifactStore(tmp_path/'evidence'), resume_collection=new_id,
                    openai_key='fixture', tavily_key='fixture', data_dir=tmp_path,
                    research_runner=run_child, model_projector=project, snapshot_publisher=publish)
                assert result.state == 'partial' and len(calls)==2
                assert result.usage == original.usage
                assert original.snapshot.read_bytes() == original_bytes
                current = load_snapshot(tmp_path)
                assert current['run_id'] == new_id and current['scoring_policy_version']=='1.5.2'
                assert current['reassessment_of_collection_id'] == original.collection_id
                assert current['processing_order'][:2] == [old[1]['model_id'],old[0]['model_id']]
                assert [m['status'] for m in current['models'][:3]] == ['unresolved','technical_failed','queued']
                assert all(m['score'] is None for m in current['models'])
            finally:
                await cleanup(pool,new_id)
    asyncio.run(check())
