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
from ai_builder.store import Conflict
from test_collection import Providers, context
from test_providers import model_result
from test_source_research import parse_fixture, response

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
            with pytest.raises(Conflict, match='existing paused'):
                await run_focused(pool, artifacts, parent_collection_id=parent.collection_id, model_id=model_id,
                                  data_dir=tmp_path, refine_sources=True)
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
            with pytest.raises(Conflict, match='paused oversized'):
                await run_focused(pool, artifacts, parent_collection_id=parent.collection_id, model_id=model_id,
                                  data_dir=tmp_path, refine_sources=True)
            # A fully used search quota must not prevent resuming later stages
            # once the immutable source packet has already been saved.
            async with pool.connection() as conn:
                await conn.execute("UPDATE app.runs SET status='paused' WHERE id=%s", (focused.run_id,))
            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                resumed = await run_focused(pool, artifacts, parent_collection_id=parent.collection_id, model_id=model_id,
                    openai_key='fixture', tavily_key='fixture', data_dir=tmp_path, retry_paused=True, provider_client=client)
            assert resumed.run_id == focused.run_id and resumed.usage == focused.usage
    asyncio.run(check())


@pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')
def test_focused_supplement_is_new_frozen_revision_and_latest_replays_without_calls(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            artifacts = ArtifactStore(tmp_path / 'evidence')
            async with httpx.AsyncClient(transport=httpx.MockTransport(Providers(pool))) as client:
                parent = await run_collection(pool, artifacts,
                    context=context(config).model_copy(update={'purpose': 'live'}),
                    openai_key='fixture', tavily_key='fixture', provider_client=client, data_dir=tmp_path)
            collection_snapshot = load_snapshot(tmp_path)
            model_id = collection_snapshot['models'][0]['id']
            original_urls = [f'https://example.com/original-primary-{index}' for index in range(10)]
            configs = tmp_path / 'config'
            configs.mkdir()
            source = Path(__file__).parents[1] / 'config'
            for name in ('config', 'plan', 'roles'):
                value = json.loads((source / f'helpdesk-{name}.json').read_text())
                if name == 'plan':
                    value['seed']['base_model_id'] = model_id
                    value['priority_primary_urls'] = original_urls
                    value['queries'] = [{'query': 'original-focused-search', 'max_results': 1}]
                (configs / f'helpdesk-{name}.json').write_text(json.dumps(value))
            (configs / 'helpdesk-supplement.json').write_text(json.dumps({
                'schema_version': '1', 'base_model_id': model_id,
                'reason': 'Check an unresolved external prerequisite.',
                'priority_primary_urls': ['https://example.com/additional-primary']}))
            searches, research_packets, reads = [], [], []

            def provider(request):
                payload = json.loads(request.content)
                if request.url.host == 'api.tavily.com':
                    searches.append(payload['query'])
                    return httpx.Response(200, json={'usage': {'credits': 1}, 'results': []})
                assert payload['text']['format']['name'] == 'ResearchV14'
                research_packets.append(json.loads(payload['input'][0]['content']))
                value = {'criteria': [dict(criterion_id=f'F{i:02}', state='completed', result='unknown',
                    evidence_ids=[], design_basis=[], reason='The external prerequisite remains unresolved.')
                    for i in range(1, 11)]}
                return httpx.Response(200, json=model_result(text=json.dumps(value)))

            async def fetch(url):
                reads.append(url)
                return response(url)

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                original = await run_focused(pool, artifacts,
                    parent_collection_id=parent.collection_id, model_id=model_id,
                    openai_key='fixture', tavily_key='fixture', provider_client=client,
                    data_dir=tmp_path, config_dir=configs, fetch_document=fetch, parser=parse_fixture)
                assert original.state['card']['eligibility'] == 'needs_review'
                async with pool.connection() as conn:
                    before = await (await conn.execute('SELECT packet,packet_hash FROM app.source_research WHERE run_id=%s',
                                                       (original.run_id,))).fetchone()
                revised = await run_focused(pool, artifacts,
                    parent_collection_id=parent.collection_id, model_id=model_id, supplement_sources=True,
                    openai_key='fixture', tavily_key='fixture', provider_client=client,
                    data_dir=tmp_path, config_dir=configs, fetch_document=fetch, parser=parse_fixture)
            assert revised.run_id != original.run_id
            assert revised.prepared.scope.scope_id != original.prepared.scope.scope_id
            assert revised.prepared.scope.variant_id != original.prepared.scope.variant_id
            assert revised.prepared.scope.neutral_model_description == original.prepared.scope.neutral_model_description
            assert revised.prepared.scope.documents[:10] == original.prepared.scope.documents
            assert len(revised.prepared.scope.documents) == 11
            assert searches == ['original-focused-search']
            assert reads == original_urls + ['https://example.com/additional-primary']
            assert len(research_packets) == 2
            assert [len(packet['documents']) for packet in research_packets] == [10, 11]
            async with pool.connection() as conn:
                after = await (await conn.execute('SELECT packet,packet_hash FROM app.source_research WHERE run_id=%s',
                                                  (original.run_id,))).fetchone()
                rows = await (await conn.execute('''SELECT r.id,r.study_id,r.config_json,r.limit_micro,rc.context
                    FROM app.runs r JOIN app.research_contexts rc ON rc.run_id=r.id
                    WHERE r.id=ANY(%s::text[])''', ([original.run_id, revised.run_id],))).fetchall()
            assert after == before
            old, new = ({row['id']:row for row in rows}[identifier] for identifier in (original.run_id, revised.run_id))
            assert old['config_json'] == new['config_json']
            assert old['study_id'] == new['study_id'] == parent.study_id
            assert old['limit_micro'] == new['limit_micro'] == 25_000_000
            assert new['context']['plan']['supplemented_from_run_id'] == original.run_id
            assert new['context']['parent_run_id'] == old['context']['parent_run_id']
            assert new['context']['pipeline_prompts'] == old['context']['pipeline_prompts']

            def forbidden(*args, **kwargs):
                raise AssertionError('Latest supplemental replay must not call providers, fetch or parse')

            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                replay = await run_focused(pool, artifacts,
                    parent_collection_id=parent.collection_id, model_id=model_id, supplement_sources=True,
                    provider_client=client, fetch_document=forbidden, parser=forbidden,
                    data_dir=tmp_path, config_dir=tmp_path / 'manifest-not-needed-on-replay')
            assert replay.replay and replay.run_id == revised.run_id and replay.usage == revised.usage
            assert load_snapshot(tmp_path) == collection_snapshot
            assert load_featured_followup(tmp_path)['run_id'] == revised.run_id
            async with pool.connection() as conn:
                count = await (await conn.execute('''SELECT count(*) AS n FROM app.research_contexts
                    WHERE context->>'parent_collection_id'=%s''', (parent.collection_id,))).fetchone()
            assert count['n'] == 2
    asyncio.run(check())
