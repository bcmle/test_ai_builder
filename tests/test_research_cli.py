import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from ai_builder.evidence import ArtifactStore
from ai_builder.fetch_transport import ControlledFetcher
from ai_builder.config import load_config
from ai_builder.pg_execution import acquire_lease, execution_pool
from ai_builder.pg_ledger import PostgresLedger, digest
from ai_builder.pg_migrate import migrate
from ai_builder.pipeline import PROMPTS, PREFIX, prompt_bundle
from ai_builder.research_scope import ResearchContext, _load_context, create_research_run, freeze_context, run_research
from ai_builder.smoke import FixtureHTTP
from ai_builder.source_documents import _html, _pdf, parse_document
from ai_builder.source_research import ScopeSeed, SearchQueryPlan, SourceResearchPlan
from ai_builder.store import BudgetExceeded, Conflict, uid
from test_fetch_transport import fixture_fetcher, robots, Reply
from test_pipeline import FixtureRoles
from test_providers import model_result, pricing
from test_source_documents import ARTICLE, pdf_document

DSN = os.getenv('TEST_DATABASE_URL')
needs_postgres = pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')


def context():
    return ResearchContext(
        plan=SourceResearchPlan(seed=ScopeSeed(scope_id='scope-' + uid(), base_model_id='fixture-model',
            variant_id='fixture-US', neutral_model_description='Synthetic invoice subscription model'),
            queries=(SearchQueryPlan(query='synthetic product subscription pricing', max_results=2),)),
        openai_pricing=pricing(), tavily_pricing=pricing('tavily'),
        role_settings={role: dict(reasoning_effort='low', max_output_tokens=4096) for role in PROMPTS}, purpose='eval')


def fixture_parser(response, limits):
    if sys.platform == 'linux':
        # CI executes the production child process with its CPU/RLIMIT_AS bounds.
        return parse_document(response, limits)
    # macOS rejects the small RLIMIT_AS in production (which fails closed).
    # Only these bounded, generated test documents use real extractors directly.
    document = _pdf(response.body, limits) if response.content_type == 'application/pdf' else _html(response.body, limits)
    return replace(document, original_hash=hashlib.sha256(response.body).hexdigest(), parser_limits=asdict(limits))


class ResearchProviders:
    def __init__(self, pool, frozen_context, urls=('https://example.com/pricing', 'https://example.com/report.pdf')):
        self.pool, self.context, self.urls = pool, frozen_context, urls
        self.calls = []
        self.roles = FixtureRoles()
        self.document = None

    async def __call__(self, request):
        self.calls.append(str(request.url))
        async with self.pool.connection() as conn:
            row = await (await conn.execute('''SELECT c.*, s.scope FROM app.research_contexts c
                LEFT JOIN app.scope_inputs s ON s.run_id=c.run_id
                WHERE c.context->'plan'->'seed'->>'scope_id'=%s''', (self.context.plan.seed.scope_id,))).fetchone()
        assert row is not None
        expected = {**self.context.model_dump(mode='json'), 'pipeline_prefix': PREFIX, 'pipeline_prompts': PROMPTS}
        assert row['context_hash'] == digest(expected)
        assert row['context'] == expected
        body = json.loads(request.content)
        if request.url.host == 'api.tavily.com':
            assert request.url.path == '/search'
            assert row['scope'] is None  # Source packet is frozen after reading.
            assert body['include_answer'] is False and body['include_raw_content'] is False
            return httpx.Response(200, json={'results': [{'url': url, 'title': 'Fixture source',
                'content': 'SEARCH SNIPPET MUST NEVER BECOME EVIDENCE'} for url in self.urls], 'usage': {'credits': 1}})
        assert request.url.host == 'api.openai.com'
        assert row['scope'] is not None  # Immutable graph packet precedes calls.
        role = next(role for role, prompt in PROMPTS.items() if PREFIX + prompt == body['instructions'])
        packet = json.loads(body['input'][0]['content'])
        assert 'SEARCH SNIPPET MUST NEVER' not in json.dumps(packet)
        if packet.get('documents'):
            self.document = packet['documents'][0]
        result = await self.roles.generate(role, packet, FixtureHTTP.schemas[role], 'fixture-role')
        document = self.document
        substitutions = {
            'evidence': document['evidence_id'], 'snapshot': document['snapshot_id'],
            'https://example.com/fixture': document['url'],
            'Fixture subscription product and pricing.': document['text'].splitlines()[0],
        }

        def rewrite(value):
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            return substitutions.get(value, value) if isinstance(value, str) else value

        return httpx.Response(200, json=model_result(text=json.dumps(rewrite(result.model_dump(mode='json')))))


def sources():
    return fixture_fetcher(robots(), Reply('/pricing', body=ARTICLE), robots(),
                           Reply('/report.pdf', body=pdf_document(['Synthetic company valuation: USD 500 million.']), mime='application/pdf'))


def test_resume_rejects_overrides_before_opening_database(tmp_path):
    with pytest.raises(ValueError, match='saved inputs'):
        asyncio.run(run_research(None, ArtifactStore(tmp_path), resume_run='existing', context=context()))


def test_context_requires_both_tariffs_and_complete_role_settings():
    value = context().model_dump(mode='json')
    value['tavily_pricing'] = value['openai_pricing']
    with pytest.raises(ValueError, match='tariffs'):
        ResearchContext.model_validate(value)
    value = context().model_dump(mode='json')
    value['role_settings'].pop('verifier')
    with pytest.raises(ValueError, match='Every role'):
        ResearchContext.model_validate(value)


def test_context_legacy_bytes_and_explicit_frozen_bundle(config):
    legacy = context()
    assert set(legacy.model_dump()) == {'schema_version', 'plan', 'openai_pricing', 'tavily_pricing',
                                      'role_settings', 'purpose'}
    assert ResearchContext.model_validate_json(legacy.model_dump_json()).model_dump_json() == legacy.model_dump_json()
    frozen = freeze_context(config, legacy)
    assert frozen.pipeline_prefix == PREFIX and frozen.pipeline_prompts == PROMPTS
    assert legacy.pipeline_prefix is None
    explicit = frozen.model_copy(update={'pipeline_prefix': 'Historical frozen prefix. '})
    assert freeze_context(config, explicit).pipeline_prefix == explicit.pipeline_prefix
    for invalid in ({'pipeline_prefix': 'prefix'}, {'parent_collection_id': 'parent'}, {'parent_run_id': 'run'}):
        with pytest.raises(ValueError):
            ResearchContext.model_validate({**legacy.model_dump(), **invalid})


def test_followup_requires_existing_study_before_database(config):
    followup = context().model_copy(update={'parent_collection_id': 'collection', 'parent_run_id': 'parent'})
    with pytest.raises(ValueError, match='existing study_id'):
        asyncio.run(create_research_run(None, config, followup, '25'))


@needs_postgres
def test_legacy_collection_prompt_recovery_and_followup_parent_integrity(config):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            admin = PostgresLedger(pool)
            study = await admin.create_study('US', '100')
            parent_run = await admin.create_run(study, config)
            coordinator = await admin.create_run(study, config)
            collection = uid()
            legacy = context()
            legacy = legacy.model_copy(update={'plan': legacy.plan.model_copy(update={
                'seed': legacy.plan.seed.model_copy(update={'base_model_id': uid()})})})
            old_prefix = 'An immutable historical role prefix. '
            frozen_collection = {**legacy.model_dump(mode='json'), 'config': config.model_dump(mode='json'),
                                 'pipeline_prefix': old_prefix, 'pipeline_prompts': PROMPTS}
            members = [{'model_id': legacy.plan.seed.base_model_id if index == 1 else uid(),
                        'position': index, 'candidate': {'plan': legacy.plan.model_dump(mode='json')}}
                       for index in range(1, 21)]
            async with pool.connection() as conn, conn.transaction():
                await conn.execute('''INSERT INTO app.collections
                    (id,study_id,coordinator_run_id,context_hash,context,state,membership_hash,selected_count,frozen_at)
                    VALUES (%s,%s,%s,%s,%s,'partial',%s,20,clock_timestamp())''',
                    (collection, study, coordinator, digest(frozen_collection), Jsonb(frozen_collection), digest(members)))
                for member in members:
                    await conn.execute('''INSERT INTO app.collection_items
                        (collection_id,model_id,position,candidate,run_id,state) VALUES (%s,%s,%s,%s,%s,'queued')''',
                        (collection, member['model_id'], member['position'], Jsonb(member['candidate']),
                         parent_run if member['position'] == 1 else None))
                # Model the pre-v1.4 saved record directly; immutable rows are
                # neither altered nor repaired during compatibility loading.
                await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',
                                   (parent_run, digest(legacy.model_dump(mode='json')), Jsonb(legacy.model_dump(mode='json'))))
            ledger = PostgresLedger(pool, parent_run, await acquire_lease(pool, parent_run, 'legacy-test'))
            _, recovered, _ = await _load_context(ledger)
            assert recovered.pipeline_prefix == old_prefix and recovered.pipeline_prompts == PROMPTS
            async with pool.connection() as conn:
                unchanged = await (await conn.execute('SELECT context,context_hash FROM app.research_contexts WHERE run_id=%s',
                                                      (parent_run,))).fetchone()
            assert unchanged == {'context': legacy.model_dump(mode='json'),
                                 'context_hash': digest(legacy.model_dump(mode='json'))}
            new_config = load_config(Path(__file__).parents[1] / 'config' / 'helpdesk-config.json')
            seed = legacy.plan.seed.model_copy(update={'scope_id': uid(), 'variant_id': uid()})
            followup = legacy.model_copy(update={'plan': legacy.plan.model_copy(update={'seed': seed}),
                                                 'parent_collection_id': collection, 'parent_run_id': parent_run})
            child = await create_research_run(pool, new_config, followup, study_id=study)
            async with pool.connection() as conn:
                row = await (await conn.execute('''SELECT r.study_id,c.context FROM app.runs r
                    JOIN app.research_contexts c ON c.run_id=r.id WHERE r.id=%s''', (child,))).fetchone()
            assert row['study_id'] == study
            assert row['context']['pipeline_prefix'] == prompt_bundle('1.1')[0]
            assert row['context']['parent_run_id'] == parent_run
            other_study = await admin.create_study('US', '100')
            with pytest.raises(ValueError, match='parent study and base model'):
                await create_research_run(pool, new_config, followup, study_id=other_study)
            wrong_seed = seed.model_copy(update={'base_model_id': 'not-a-member'})
            wrong_model = followup.model_copy(update={'plan': followup.plan.model_copy(update={'seed': wrong_seed})})
            with pytest.raises(ValueError, match='parent study and base model'):
                await create_research_run(pool, new_config, wrong_model, study_id=study)
            wrong_parent = followup.model_copy(update={'parent_collection_id': 'not-the-parent'})
            with pytest.raises(Conflict, match='unique frozen parent'):
                await create_research_run(pool, new_config, wrong_parent, study_id=study)
    asyncio.run(check())


@needs_postgres
def test_research_end_to_end_http_search_pinned_fetch_parsers_pg_graph_and_replay(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            inputs = context()
            fixture = ResearchProviders(pool, inputs)
            fetcher, network, _ = sources()
            artifacts = ArtifactStore(tmp_path / 'evidence')
            output = tmp_path / 'private.html'
            output.write_text('old report')
            output.chmod(0o644)
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                result = await run_research(pool, artifacts, config=config, context=inputs, study_budget='25',
                    openai_key='fixture-openai', tavily_key='fixture-tavily', provider_client=client,
                    fetch_document=fetcher.fetch, parser=fixture_parser, output=output)
            assert result.state['card']['eligibility'] == 'pass'
            assert result.state['card']['publication'] == 'ready'
            assert len(result.prepared.scope.documents) == 2
            assert len(network.connections) == 4
            assert len(fixture.calls) == 8
            assert 'critic' in [r[0] for r in fixture.roles.calls]
            assert 'verifier' in [r[0] for r in fixture.roles.calls]
            assert result.usage['calculated_micro'] == 22_000
            assert result.usage['reserved_micro'] == result.usage['unknown_micro'] == 0
            assert result.usage['controlled_document_attempts'] == 2
            assert all(request['purpose'] == 'eval' for request in result.usage['requests'])
            assert stat.S_IMODE(output.stat().st_mode) == 0o600
            assert len(result.state['source_metadata']) == 2
            assert {row['extraction_method'] for row in result.state['source_metadata'].values()} == {'pypdf', 'trafilatura'}

            def forbidden(*args, **kwargs):
                raise AssertionError('Completed run must not contact any provider')

            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                replay = await run_research(pool, artifacts, resume_run=result.run_id,
                    provider_client=client, fetch_document=forbidden, parser=forbidden, output=tmp_path / 'replay.html')
            assert replay.replay
            assert replay.state == result.state
            assert replay.usage == result.usage
            async with pool.connection() as conn:
                run = await (await conn.execute('SELECT status,active_started_at FROM app.runs WHERE id=%s', (result.run_id,))).fetchone()
                count = await (await conn.execute('SELECT count(*) AS n FROM app.scope_inputs WHERE run_id=%s', (result.run_id,))).fetchone()
            assert run == {'status': 'partial', 'active_started_at': None}
            assert count['n'] == 1
    asyncio.run(check())


@needs_postgres
def test_all_sources_inaccessible_becomes_unknown_without_any_llm_call(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            inputs = context()
            fixture = ResearchProviders(pool, inputs, urls=('https://example.com/pricing',))
            fetcher, network, _ = fixture_fetcher(robots(b'User-agent: *\nDisallow: /pricing'))
            artifacts = ArtifactStore(tmp_path / 'evidence')
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                result = await run_research(pool, artifacts, config=config, context=inputs, study_budget='25',
                    openai_key='fixture', tavily_key='fixture', provider_client=client,
                    fetch_document=fetcher.fetch, parser=fixture_parser)
            assert result.state['card']['eligibility'] == 'needs_review'
            assert result.state['card']['score'] is None
            assert {c['result'] for c in result.state['criteria']} == {'unknown'}
            assert result.state['source_gaps'][0]['code'] == 'robots_denied'
            assert len(fixture.calls) == len(network.connections) == 1
            assert not fixture.roles.calls
            assert result.usage['calculated_micro'] == 8000
            async with pool.connection() as conn:
                assert await (await conn.execute('SELECT 1 FROM app.graph_threads WHERE run_id=%s', (result.run_id,))).fetchone() is None
            replay = await run_research(pool, artifacts, resume_run=result.run_id)
            assert replay.state == result.state
            assert replay.usage == result.usage
    asyncio.run(check())


@needs_postgres
def test_context_is_database_immutable_and_paused_resume_cannot_change_limits(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            inputs = context()
            run_id = await create_research_run(pool, config, inputs, '25')
            async with pool.connection() as conn:
                with pytest.raises(psycopg.errors.RaiseException, match='immutable'):
                    await conn.execute("UPDATE app.research_contexts SET context='{}' WHERE run_id=%s", (run_id,))
                with pytest.raises(psycopg.errors.RaiseException, match='immutable'):
                    await conn.execute('DELETE FROM app.research_contexts WHERE run_id=%s', (run_id,))
                await conn.execute("UPDATE app.runs SET status='paused' WHERE id=%s", (run_id,))
            with pytest.raises(Conflict, match='owner action'):
                await run_research(pool, ArtifactStore(tmp_path), resume_run=run_id, openai_key='fixture', tavily_key='fixture')
            async with pool.connection() as conn:
                run = await (await conn.execute('SELECT status,limit_micro,active_started_at FROM app.runs WHERE id=%s', (run_id,))).fetchone()
                saved = await (await conn.execute('SELECT context FROM app.research_contexts WHERE run_id=%s', (run_id,))).fetchone()
            assert run == {'status': 'paused', 'limit_micro': 25_000_000, 'active_started_at': None}
            assert saved['context'] == freeze_context(config, inputs).model_dump(mode='json')
            assert not (await PostgresLedger(pool).summary(run_id))['requests']
    asyncio.run(check())


@needs_postgres
def test_saved_context_can_resume_before_first_search_without_new_defaults(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            inputs = context()
            run_id = await create_research_run(pool, config, inputs, '25')
            fixture = ResearchProviders(pool, inputs, urls=())
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                result = await run_research(pool, ArtifactStore(tmp_path), resume_run=run_id,
                    openai_key='fixture', tavily_key='fixture', provider_client=client)
            assert result.run_id == run_id
            assert not result.replay
            assert result.prepared.scope.scope_id == inputs.plan.seed.scope_id
            assert result.state['source_gaps'][0]['code'] == 'empty_search'
            assert len(fixture.calls) == 1
    asyncio.run(check())


@needs_postgres
def test_same_budget_rejects_search_before_network_and_persists_pause(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            inputs = context()
            calls = []
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
                with pytest.raises(BudgetExceeded):
                    await run_research(pool, ArtifactStore(tmp_path), config=config, context=inputs,
                        study_budget='0.001', openai_key='fixture', tavily_key='fixture', provider_client=client)
            async with pool.connection() as conn:
                saved = await (await conn.execute('''SELECT r.* FROM app.runs r JOIN app.research_contexts c ON c.run_id=r.id
                    WHERE c.context->'plan'->'seed'->>'scope_id'=%s''', (inputs.plan.seed.scope_id,))).fetchone()
            assert saved['status'] == 'paused'
            assert saved['active_started_at'] is None
            assert not calls
            assert not (await PostgresLedger(pool).summary(saved['id']))['requests']
    asyncio.run(check())
