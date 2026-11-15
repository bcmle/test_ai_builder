import asyncio
import json
import os
from pathlib import Path

import httpx
import psycopg
import pytest
from pydantic import ValidationError

from ai_builder.collection import (CollectionContext, CollectionPlan, DiscoverySelection,
    DiscoveredModel, NORMALIZATION_PROMPT, _valid_selection, create_collection, run_collection)
from ai_builder.evidence import ArtifactStore
from ai_builder.pg_execution import acquire_lease, execution_pool
from ai_builder.pg_ledger import PostgresLedger
from ai_builder.pg_migrate import migrate
from ai_builder.pipeline import PROMPTS
from ai_builder.public import load_snapshot
from ai_builder.research_scope import create_research_run, run_research
from ai_builder.source_research import SearchQueryPlan
from ai_builder.store import BudgetExceeded, Conflict, uid
from test_providers import model_result, pricing
from test_research_cli import context as research_context

DSN = os.getenv('TEST_DATABASE_URL')
postgres = pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')


def context(config, budget='100'):
    config = config.model_copy(update={'scenario': config.scenario.model_copy(update={
        'jurisdiction': 'United States (federal scope; state-specific requirements unresolved where applicable)'})})
    return CollectionContext(plan=CollectionPlan(queries=(SearchQueryPlan(query='discovery-one', max_results=10),
                                                         SearchQueryPlan(query='discovery-two', max_results=10))),
        config=config, openai_pricing=pricing(), tavily_pricing=pricing('tavily'),
        role_settings={role: {'reasoning_effort': 'low', 'max_output_tokens': 4096} for role in PROMPTS},
        study_budget_usd=budget, purpose='eval')


def candidate(index):
    return DiscoveredModel(canonical_key=f'base-mechanism-{index}', title=f'Business mechanism {index}',
        category='Synthetic test category', core_job=f'Distinct synthetic customer task number {index}',
        buyer=f'Synthetic buyer {index}', revenue='Prepaid subscription', comparable=f'Synthetic product {index}',
        summary=f'A synthetic launch proposal serving customer task number {index}.',
        source_ids=(f'lead-{1 if index <= 10 else 2}-{(index - 1) % 10 + 1}',),
        queries=(SearchQueryPlan(query=f'child-research-{index}', max_results=1),))


class Providers:
    def __init__(self, pool, count=20, failure=None):
        self.pool, self.count, self.failure = pool, count, failure
        self.searches, self.normalizations, self.child_searches = [], 0, []

    async def __call__(self, request):
        payload = json.loads(request.content)
        if request.url.host == 'api.openai.com':
            assert payload['instructions'] == NORMALIZATION_PROMPT
            assert payload['max_output_tokens'] == 16384
            self.normalizations += 1
            output = DiscoverySelection(models=tuple(candidate(i) for i in range(1, self.count + 1)), uncertainties=())
            return httpx.Response(200, json=model_result(text=output.model_dump_json()))
        query = payload['query']
        if query.startswith('discovery-'):
            self.searches.append(query)
            return httpx.Response(200, json={'usage': {'credits': 1}, 'results': [
                {'url': f'https://example.com/{query}/{n}', 'title': f'Real mocked lead {n}',
                 'content': 'A search discovery lead, never a read evidence document.'} for n in range(1, 11)]})
        self.child_searches.append(query)
        # Every child call happens AFTER the immutable 20-member snapshot and
        # has a research context in the very same budget study as discovery.
        async with self.pool.connection() as conn:
            row = await (await conn.execute('''SELECT c.selected_count,c.membership_hash,c.study_id,r.study_id AS child_study
                FROM app.collections c JOIN app.collection_items i ON i.collection_id=c.id
                JOIN app.runs r ON r.id=i.run_id JOIN app.research_contexts rc ON rc.run_id=r.id
                WHERE rc.context->'plan'->'queries'->0->>'query'=%s ORDER BY c.created_at DESC LIMIT 1''', (query,))).fetchone()
        assert row['selected_count'] == 20 and row['membership_hash']
        assert row['study_id'] == row['child_study']
        if self.failure and query == 'child-research-1':
            return httpx.Response(self.failure, json={'error': {'code': 'fixture_auth'}})
        return httpx.Response(200, json={'usage': {'credits': 1}, 'results': []})


async def cleanup(pool, identifier):
    if identifier:
        async with pool.connection() as conn:
            await conn.execute("UPDATE app.collections SET state='partial' WHERE id=%s AND state IN ('discovering','running')", (identifier,))


def test_plan_limits_and_selection_do_not_invent_padding(config):
    value = context(config)
    assert CollectionContext.model_validate_json(value.model_dump_json()) == value
    assert value.plan.normalization_attempts == 2
    assert len(CollectionPlan().queries) == 12
    with pytest.raises(ValidationError):
        CollectionPlan(queries=tuple(SearchQueryPlan(query='query') for _ in range(13)))
    with pytest.raises(ValidationError):
        CollectionContext.model_validate({**value.model_dump(), 'study_budget_usd': '100.01'})
    first = candidate(1)
    duplicate = first.model_copy(update={'title': 'Different branding', 'canonical_key': 'different-brand'})
    selected, errors = _valid_selection(DiscoverySelection(models=(first, duplicate, candidate(2)), uncertainties=()),
                                        [{'id': 'lead-1-1'}])
    assert selected == (first,)
    assert any('duplicate base' in error for error in errors)
    assert any('unknown discovery source' in error for error in errors)


@postgres
def test_real_discovery_twenty_frozen_shared_budget_sequential_children_and_zero_call_replay(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            fixture = Providers(pool)
            artifacts = ArtifactStore(tmp_path / 'evidence')
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                result = await run_collection(pool, artifacts, context=context(config), openai_key='fixture',
                                              tavily_key='fixture', provider_client=client, data_dir=tmp_path)
            assert (result.state, result.selected_count, result.completed_count) == ('completed', 20, 20)
            assert len(fixture.searches) == 2 and fixture.normalizations == 1 and len(fixture.child_searches) == 20
            assert result.usage['accounted_micro'] == 22 * 8000 + 2000
            snapshot = load_snapshot(tmp_path, result.collection_id)
            assert len(snapshot['models']) == 20
            assert {model['status'] for model in snapshot['models']} == {'needs_review'}
            assert all(not model['rank'] for model in snapshot['models'])
            assert 'United States' in snapshot['scenario']['jurisdiction']
            async with pool.connection() as conn:
                studies = await (await conn.execute('SELECT count(DISTINCT study_id) AS n,count(*) AS runs FROM app.runs WHERE study_id=%s', (result.study_id,))).fetchone()
                coordinator = await (await conn.execute('''SELECT r.active_started_at,r.status FROM app.collections c
                    JOIN app.runs r ON r.id=c.coordinator_run_id WHERE c.id=%s''', (result.collection_id,))).fetchone()
            assert studies == {'n': 1, 'runs': 21}
            assert coordinator == {'active_started_at': None, 'status': 'partial'}

            def forbidden(request):
                raise AssertionError('Completed collection must never invoke providers')

            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                replay = await run_collection(pool, artifacts, resume_collection=result.collection_id,
                                              provider_client=client, data_dir=tmp_path)
            assert replay.usage == result.usage and replay.completed_count == 20
            assert load_snapshot(tmp_path, result.collection_id)['models'] == snapshot['models']
    asyncio.run(check())


@postgres
def test_fewer_than_twenty_after_two_attempts_is_partial_and_never_runs_filters(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            fixture = Providers(pool, count=19)
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                result = await run_collection(pool, ArtifactStore(tmp_path / 'evidence'), context=context(config),
                                              openai_key='fixture', tavily_key='fixture', provider_client=client, data_dir=tmp_path)
            assert result.state == 'partial' and result.selected_count == 19 and result.completed_count == 0
            assert fixture.normalizations == 2 and not fixture.child_searches
            snapshot = load_snapshot(tmp_path, result.collection_id)
            assert snapshot['status'] == 'partial' and len(snapshot['models']) == 19
            assert {model['status'] for model in snapshot['models']} == {'incomplete'}
            async with pool.connection() as conn:
                child_count = await (await conn.execute('SELECT count(*) AS n FROM app.collection_items WHERE collection_id=%s AND run_id IS NOT NULL', (result.collection_id,))).fetchone()
            assert child_count['n'] == 0
    asyncio.run(check())


@postgres
def test_local_failure_continues_and_provider_auth_stops_collection(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            fixture = Providers(pool)
            calls = []

            async def locally_failing(pool, artifacts, **kwargs):
                calls.append(kwargs['resume_run'])
                if len(calls) == 1:
                    raise ValueError('synthetic invalid analytical result')
                return await run_research(pool, artifacts, **kwargs)

            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                result = await run_collection(pool, ArtifactStore(tmp_path / 'one'), context=context(config),
                    openai_key='fixture', tavily_key='fixture', provider_client=client,
                    research_runner=locally_failing, data_dir=tmp_path / 'local')
            assert result.state == 'partial' and result.completed_count == 19 and len(calls) == 20
            assert load_snapshot(tmp_path / 'local', result.collection_id)['models'][0]['status'] == 'technical_failed'
            async with pool.connection() as conn:
                failed = await (await conn.execute('SELECT model_id,run_id FROM app.collection_items WHERE collection_id=%s AND position=1',
                                                   (result.collection_id,))).fetchone()
            original_counts = (len(fixture.searches), fixture.normalizations, len(fixture.child_searches))
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                recovered = await run_collection(pool, ArtifactStore(tmp_path / 'one'),
                    resume_collection=result.collection_id, retry_failed=True, retry_model_ids=(failed['model_id'],),
                    openai_key='fixture', tavily_key='fixture', provider_client=client, data_dir=tmp_path / 'local')
            assert recovered.state == 'completed' and recovered.completed_count == 20
            assert (len(fixture.searches), fixture.normalizations, len(fixture.child_searches)) == (
                original_counts[0], original_counts[1], original_counts[2] + 1)
            async with pool.connection() as conn:
                same = await (await conn.execute('SELECT model_id,run_id FROM app.collection_items WHERE collection_id=%s AND position=1',
                                                 (result.collection_id,))).fetchone()
                event = await (await conn.execute("SELECT payload FROM app.events WHERE kind='collection_recovery_selected' AND payload->>'collection_id'=%s",
                                                  (result.collection_id,))).fetchone()
            assert same == failed and event['payload']['frozen_inputs_unchanged']

            def forbidden(request):
                raise AssertionError('Repeated recovery must skip completed children')

            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                replay = await run_collection(pool, ArtifactStore(tmp_path / 'one'), resume_collection=result.collection_id,
                    retry_failed=True, retry_model_ids=(failed['model_id'],), provider_client=client, data_dir=tmp_path / 'local')
            assert replay.usage == recovered.usage
            unauthorized = Providers(pool, failure=401)
            async with httpx.AsyncClient(transport=httpx.MockTransport(unauthorized)) as client:
                stopped = await run_collection(pool, ArtifactStore(tmp_path / 'two'), context=context(config),
                    openai_key='fixture', tavily_key='fixture', provider_client=client, data_dir=tmp_path / 'auth')
            assert stopped.state == 'partial' and stopped.completed_count == 0
            assert unauthorized.child_searches == ['child-research-1']
            assert len(load_snapshot(tmp_path / 'auth', stopped.collection_id)['models']) == 20
    asyncio.run(check())


def test_recovery_requires_explicit_saved_collection(tmp_path):
    async def check():
        with pytest.raises(ValueError, match='requires --resume-collection'):
            await run_collection(None, ArtifactStore(tmp_path), retry_failed=True)
        with pytest.raises(ValueError, match='requires --retry-failed'):
            await run_collection(None, ArtifactStore(tmp_path), resume_collection='existing', retry_model_ids=('member',))
    asyncio.run(check())


@postgres
def test_atomic_child_mapping_resume_and_safe_paused_child(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            fixture = Providers(pool)
            artifacts = ArtifactStore(tmp_path / 'evidence')
            interrupted_id = None

            async def stop_before_first_call(pool, artifacts, **kwargs):
                nonlocal interrupted_id
                interrupted_id = kwargs['resume_run']
                async with pool.connection() as conn:
                    await conn.execute("UPDATE app.runs SET status='paused' WHERE id=%s", (interrupted_id,))
                raise asyncio.CancelledError()

            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                with pytest.raises(asyncio.CancelledError):
                    await run_collection(pool, artifacts, context=context(config), openai_key='fixture', tavily_key='fixture',
                                         provider_client=client, research_runner=stop_before_first_call, data_dir=tmp_path)
            async with pool.connection() as conn:
                row = await (await conn.execute('SELECT collection_id FROM app.collection_items WHERE run_id=%s', (interrupted_id,))).fetchone()
            assert row
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                result = await run_collection(pool, artifacts, resume_collection=row['collection_id'],
                    openai_key='fixture', tavily_key='fixture', provider_client=client, data_dir=tmp_path)
            assert result.completed_count == 20
            assert len(fixture.searches) == 2 and fixture.normalizations == 1
            async with pool.connection() as conn:
                first = await (await conn.execute('SELECT run_id FROM app.collection_items WHERE collection_id=%s AND position=1', (result.collection_id,))).fetchone()
                event = await (await conn.execute("SELECT 1 FROM app.events WHERE run_id=%s AND kind='explicit_collection_resume'", (interrupted_id,))).fetchone()
                runs = await (await conn.execute('SELECT count(*) AS n FROM app.runs WHERE study_id=%s', (result.study_id,))).fetchone()
            assert first['run_id'] == interrupted_id and event and runs['n'] == 21
    asyncio.run(check())


@postgres
def test_collection_context_membership_immutable_and_only_one_active(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            identifier = await create_collection(pool, context(config))
            try:
                with pytest.raises(Conflict, match='Another collection'):
                    await create_collection(pool, context(config))
                async with pool.connection() as conn:
                    with pytest.raises(psycopg.errors.RaiseException, match='immutable'):
                        await conn.execute("UPDATE app.collections SET context='{}' WHERE id=%s", (identifier,))
            finally:
                await cleanup(pool, identifier)
    asyncio.run(check())


@postgres
def test_research_runs_share_existing_study_limit(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            admin = PostgresLedger(pool)
            study = await admin.create_study('US', '0.01')
            first = await create_research_run(pool, config, research_context(), study_id=study)
            second = await create_research_run(pool, config, research_context(), study_id=study)
            price = await admin.register_pricing(pricing('tavily').model_dump(mode='json'))
            for index, run in enumerate((first, second)):
                token = await acquire_lease(pool, run, 'test-collection-budget')
                ledger = PostgresLedger(pool, run, token)
                request = uid()
                async def reserve():
                    await ledger.reserve(run_id=run, request_id=request, logical_request_id=request,
                        provider='tavily', model='tavily', endpoint='search', agent='research', purpose='eval', pricing_id=price, upper='0.008')
                if index == 0:
                    await reserve()
                    await ledger.settle(request, '0.008', {'credits': '1'})
                else:
                    with pytest.raises(BudgetExceeded):
                        await reserve()
            with pytest.raises(ValueError, match='cannot be overridden'):
                await create_research_run(pool, config, research_context(), '100', study_id=study)
    asyncio.run(check())
