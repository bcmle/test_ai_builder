import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from ai_builder.collection import run_collection
from ai_builder.evidence import ArtifactStore
from ai_builder.focused_research import run_focused
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_migrate import migrate
from ai_builder.public import load_featured_followup, load_snapshot
from test_collection import Providers, context

DSN = os.getenv('TEST_DATABASE_URL')


@pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')
def test_focused_run_shares_parent_budget_and_replays_without_replacing_twenty(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            artifacts = ArtifactStore(tmp_path / 'evidence')
            parent_provider = Providers(pool)
            async with httpx.AsyncClient(transport=httpx.MockTransport(parent_provider)) as client:
                parent = await run_collection(pool, artifacts, context=context(config).model_copy(update={'purpose': 'live'}), openai_key='fixture',
                    tavily_key='fixture', provider_client=client, data_dir=tmp_path)
            before = load_snapshot(tmp_path)
            model_id = before['models'][0]['id']
            async with pool.connection() as conn:
                member = await (await conn.execute('SELECT run_id FROM app.collection_items WHERE collection_id=%s AND model_id=%s',
                                                   (parent.collection_id, model_id))).fetchone()
            configs = tmp_path / 'config'
            configs.mkdir()
            source = Path(__file__).parents[1] / 'config'
            for name in ('config', 'plan', 'roles'):
                value = json.loads((source / f'helpdesk-{name}.json').read_text())
                if name == 'plan':
                    value['seed']['base_model_id'] = model_id
                    value['priority_primary_urls'] = []
                    value['queries'] = [{'query': 'focused-empty-query', 'max_results': 1}]
                if name == 'config':
                    value['limits']['search_requests'] = 1
                (configs / f'helpdesk-{name}.json').write_text(json.dumps(value))
            paid = []

            def provider(request):
                assert request.url.host == 'api.tavily.com'
                paid.append(json.loads(request.content)['query'])
                return httpx.Response(200, json={'usage': {'credits': 1}, 'results': []})

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                focused = await run_focused(pool, artifacts, parent_collection_id=parent.collection_id, model_id=model_id,
                    openai_key='fixture', tavily_key='fixture', data_dir=tmp_path, config_dir=configs, provider_client=client)
            assert paid == ['focused-empty-query']
            snapshot = load_featured_followup(tmp_path)
            assert snapshot['run_id'] == focused.run_id
            assert snapshot['parent_run_id'] == member['run_id']
            assert snapshot['expected_count'] == 1 and snapshot['models'][0]['status'] == 'needs_review'
            assert load_snapshot(tmp_path) == before
            async with pool.connection() as conn:
                row = await (await conn.execute('SELECT study_id,limit_micro FROM app.runs WHERE id=%s', (focused.run_id,))).fetchone()
                study = await (await conn.execute('SELECT limit_micro FROM app.studies WHERE id=%s', (parent.study_id,))).fetchone()
            assert row == {'study_id': parent.study_id, 'limit_micro': 25_000_000}
            assert study['limit_micro'] == 100_000_000

            def forbidden(request):
                raise AssertionError('Focused replay must not call any provider')

            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                replay = await run_focused(pool, artifacts, parent_collection_id=parent.collection_id, model_id=model_id,
                    data_dir=tmp_path, config_dir=tmp_path / 'not-needed-on-replay', provider_client=client)
            assert replay.run_id == focused.run_id and replay.replay
            assert replay.usage == focused.usage and load_snapshot(tmp_path) == before
            # A fully used search quota must not prevent resuming later stages
            # once the immutable source packet has already been saved.
            async with pool.connection() as conn:
                await conn.execute("UPDATE app.runs SET status='paused' WHERE id=%s", (focused.run_id,))
            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                resumed = await run_focused(pool, artifacts, parent_collection_id=parent.collection_id, model_id=model_id,
                    openai_key='fixture', tavily_key='fixture', data_dir=tmp_path, retry_paused=True, provider_client=client)
            assert resumed.run_id == focused.run_id and resumed.usage == focused.usage
    asyncio.run(check())
