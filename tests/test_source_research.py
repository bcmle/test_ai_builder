import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
import json
import os

import httpx
import pytest
from pydantic import ValidationError

from ai_builder.evidence import ArtifactStore
from ai_builder.fetch_transport import FetchError, SourceResponse
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_migrate import migrate
from ai_builder.providers import MeteredHTTP, TavilyAdapter
from ai_builder.source_documents import ParsedDocument
from ai_builder.source_research import (ScopeSeed, SearchQueryPlan, SourceResearchPlan,
                                        canonical_url, load_plan, prepare_scope)
from ai_builder.store import BudgetExceeded, Conflict, uid
from test_pg_ledger import setup
from test_providers import pricing

DSN = os.getenv('TEST_DATABASE_URL')
database = pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')
BODY = b'A primary source describes the customer and recurring invoice product.'


def plan(*queries, max_new_reads=6, max_results=5):
    return SourceResearchPlan(
        seed=ScopeSeed(scope_id='model-scope', base_model_id='model', variant_id='variant',
                       neutral_model_description='Software for recurring invoices'),
        queries=tuple(SearchQueryPlan(query=q, max_results=max_results) for q in queries or ('invoice product',)),
        max_new_reads=max_new_reads)


def response(url):
    return SourceResponse(url, url, datetime.now(timezone.utc), 'text/html', BODY,
                          peer_ips=('93.184.216.34',))


def parse_fixture(source, limits):
    return ParsedDocument(status='succeeded', body_text=source.body.decode(), title='Product details',
                          extraction_method='fixture', parser_version='1',
                          original_hash=hashlib.sha256(source.body).hexdigest())


class Harness:
    def __init__(self, ledger, artifacts, search_results):
        self.ledger, self.artifacts, self.search_results = ledger, artifacts, search_results
        self.search_calls, self.fetch_calls = [], []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.search))

    async def search(self, request):
        body = json.loads(request.content)
        self.search_calls.append(body)
        # These callbacks take the same row lock. A network call made under the
        # orchestration transaction would deadlock and fail the test timeout.
        async with self.ledger.locked(self.ledger.run_id, fenced=True):
            pass
        return httpx.Response(200, json={'results': self.search_results.get(body['query'], []),
                                        'usage': {'credits': 2 if body['search_depth'] == 'advanced' else 1}})

    def factory(self, operation_key):
        return TavilyAdapter(MeteredHTTP(self.ledger, self.ledger.run_id, 'source_research',
                             pricing('tavily'), purpose='eval', client=self.client, operation_key=operation_key), 'fixture')

    async def fetch(self, url):
        self.fetch_calls.append(url)
        async with self.ledger.locked(self.ledger.run_id, fenced=True):
            pass
        return response(url)

    async def prepare(self, value, **kwargs):
        async with asyncio.timeout(10):
            return await prepare_scope(self.ledger, value, self.factory, kwargs.pop('fetch', self.fetch),
                                       self.artifacts, parser=kwargs.pop('parser', parse_fixture))

    async def close(self):
        await self.client.aclose()


def test_source_plan_is_separate_bounded_immutable_contract(config):
    baseline = config.digest()
    original = plan('one', 'two')
    assert original == SourceResearchPlan.model_validate_json(original.model_dump_json())
    assert config.digest() == baseline
    with pytest.raises(ValidationError):
        original.max_new_reads = 1
    with pytest.raises(FrozenInstanceError):
        original.parser_limits.max_pages = 500
    for values in ({'max_new_reads': 7}, {'queries': []}, {'queries': [SearchQueryPlan(query='q')] * 5}):
        with pytest.raises(ValidationError):
            SourceResearchPlan.model_validate({**original.model_dump(), **values})
    for limit in (0, 11, True):
        with pytest.raises(ValidationError):
            SearchQueryPlan(query='query', max_results=limit)
    assert canonical_url('https://EXAMPLE.com:443/path?q=1#heading') == 'https://example.com/path?q=1'
    assert canonical_url('https://example.com/path?q=2') != canonical_url('https://example.com/path?q=1')


def test_v2_plan_bounds_primary_urls_and_legacy_serialization():
    legacy = plan()
    expected = {name: getattr(legacy, name) for name in (
        'schema_version', 'seed', 'queries', 'max_new_reads', 'acquisition_limits', 'parser_limits')}
    assert 'priority_primary_urls' not in legacy.model_dump(mode='json')
    assert set(legacy.model_dump()) == set(expected)
    assert SourceResearchPlan.model_validate_json(legacy.model_dump_json()).model_dump_json() == legacy.model_dump_json()
    v2 = SourceResearchPlan.model_validate({**legacy.model_dump(), 'schema_version': '2',
        'queries': [SearchQueryPlan(query=f'q-{i}') for i in range(12)], 'max_new_reads': 24,
        'priority_primary_urls': ['https://example.com/primary']})
    assert len(v2.queries) == 12 and v2.max_new_reads == 24
    for invalid in ({'queries': list(v2.queries) + [v2.queries[0]]}, {'max_new_reads': 25},
                    {'priority_primary_urls': ['https://example.com/a', 'https://EXAMPLE.com/a#same']},
                    {'priority_primary_urls': ['file:///private/secret']}, {'schema_version': '1'}):
        with pytest.raises(ValidationError):
            SourceResearchPlan.model_validate({**v2.model_dump(), **invalid})


@database
def test_primary_leads_precede_search_reads_use_same_cap_and_replay(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            value = SourceResearchPlan.model_validate({**plan().model_dump(), 'schema_version': '2',
                'queries': [SearchQueryPlan(query=f'q-{i}', max_results=2) for i in range(12)],
                'priority_primary_urls': ['https://example.com/a', 'https://example.com/b'],
                'max_new_reads': 2})
            harness = Harness(ledger, ArtifactStore(tmp_path), {
                'q-0': [{'url': 'https://example.com/c'}, {'url': 'https://example.com/a#same'}]})
            prepared = await harness.prepare(value)
            assert harness.fetch_calls == ['https://example.com/a', 'https://example.com/b']
            assert len(harness.search_calls) == 12  # Direct primary leads are not Tavily calls.
            assert len(prepared.scope.documents) == 2
            assert 'plan_read_limit' in {gap.code for gap in prepared.gaps}
            assert (await harness.prepare(value)) == prepared
            assert len(harness.fetch_calls) == 2 and len(harness.search_calls) == 12
            async with pool.connection() as conn:
                queries = await (await conn.execute('SELECT query_index,query FROM app.source_queries WHERE run_id=%s ORDER BY query_index',
                                                    (ledger.run_id,))).fetchall()
                event = await (await conn.execute("SELECT payload FROM app.events WHERE run_id=%s AND kind='source_primary_urls_saved'",
                                                  (ledger.run_id,))).fetchone()
            assert len(queries) == 13 and queries[-1]['query_index'] == 12
            assert queries[0]['query']['kind'] == 'primary_urls'
            assert event['payload']['url_count'] == 2
            await harness.close()
    asyncio.run(check())


@database
def test_search_to_snapshots_scope_replay_and_distinct_identities(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            artifacts = ArtifactStore(tmp_path)
            value = plan('first', 'second', max_results=10)
            harness = Harness(ledger, artifacts, {
                'first': [{'url': 'https://example.com/a#one', 'content': 'Untrusted snippet is never evidence'},
                          {'url': 'https://example.com/b'}],
                'second': [{'url': 'https://EXAMPLE.com/a#two'}]})
            prepared = await harness.prepare(value)
            replay = await harness.prepare(value)
            assert replay == prepared
            assert await load_plan(ledger) == value
            assert len(harness.search_calls) == 2 and len(harness.fetch_calls) == 2
            assert all(call['max_results'] == 10 for call in harness.search_calls)
            assert all(not call['include_raw_content'] and not call['include_answer'] for call in harness.search_calls)
            assert len(prepared.scope.documents) == 2 and not prepared.gaps
            assert all(doc.text == BODY.decode() for doc in prepared.scope.documents)
            assert (await ledger.summary(ledger.run_id))['calculated_micro'] == 16_000
            async with pool.connection() as conn:
                rows = await (await conn.execute('SELECT * FROM app.source_snapshots WHERE run_id=%s', (ledger.run_id,))).fetchall()
                dispositions = await (await conn.execute('''SELECT disposition FROM app.source_results r JOIN app.source_queries q ON q.id=r.query_id
                    WHERE q.run_id=%s ORDER BY query_index,result_rank''', (ledger.run_id,))).fetchall()
            assert [row['disposition'] for row in dispositions] == ['read', 'read', 'duplicate']
            for row in rows:
                assert len({row['source_id'], row['id'], row['document_handle']}) == 3
                assert row['metadata']['source_kind'] == 'unknown' and row['metadata']['applicability'] == 'unverified'
                assert artifacts.verify_manifest(row['manifest_ref'])['metadata'] == row['metadata']
            changed = SourceResearchPlan.model_validate({**value.model_dump(), 'max_new_reads': 1})
            with pytest.raises(Conflict, match='Changed source research plan'):
                await harness.prepare(changed)
            # A new run reads again: blob deduplication never reuses an old snapshot identity.
            second, _, _ = await setup(pool, config)
            second_harness = Harness(second, artifacts, harness.search_results)
            newer = await second_harness.prepare(value)
            assert len(second_harness.fetch_calls) == 2
            assert {d.snapshot_id for d in newer.scope.documents}.isdisjoint(d.snapshot_id for d in prepared.scope.documents)
            assert {d.content_hash for d in newer.scope.documents} == {d.content_hash for d in prepared.scope.documents}
            await harness.close()
            await second_harness.close()
    asyncio.run(check())


@database
def test_bounded_reads_and_failed_sources_are_gaps_not_evidence(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            harness = Harness(ledger, ArtifactStore(tmp_path), {'leads': [
                {'url': 'https://example.com/a'}, {'url': 'https://example.com/b'},
                {'url': 'https://example.com/a#same'}, {'url': 'file:///private/secret'}]})
            prepared = await harness.prepare(plan('leads', 'empty', max_new_reads=1))
            assert len(harness.fetch_calls) == 1 and len(prepared.scope.documents) == 1
            assert {gap.code for gap in prepared.gaps} == {'plan_read_limit', 'invalid_source_url', 'empty_search'}
            await harness.close()
            failed, _, _ = await setup(pool, config)
            h = Harness(failed, ArtifactStore(tmp_path), {'lead': [{'url': 'https://example.com/blocked'}]})

            async def blocked(url):
                raise FetchError('robots_disallowed', url)

            result = await h.prepare(plan('lead'), fetch=blocked)
            assert not result.scope.documents and result.gaps[0].code == 'robots_disallowed'
            assert result.gaps[0].snapshot_id
            async with pool.connection() as conn:
                saved = await (await conn.execute('SELECT * FROM app.source_snapshots WHERE run_id=%s', (failed.run_id,))).fetchone()
            assert saved['state'] == 'failed' and saved['document_handle'] is None
            assert saved['metadata']['final_url'] is None
            await h.close()
    asyncio.run(check())


@database
def test_parser_failure_retains_original_but_does_not_enter_scope(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            artifacts = ArtifactStore(tmp_path)
            harness = Harness(ledger, artifacts, {'lead': [{'url': 'https://example.com/a'}]})

            def unsupported(source, limits):
                return ParsedDocument('unsupported', error='pdf_without_text_ocr_unsupported',
                                      original_hash=hashlib.sha256(source.body).hexdigest())

            prepared = await harness.prepare(plan('lead'), parser=unsupported)
            assert not prepared.scope.documents and prepared.gaps[0].code == 'pdf_without_text_ocr_unsupported'
            async with pool.connection() as conn:
                saved = await (await conn.execute('SELECT * FROM app.source_snapshots WHERE run_id=%s', (ledger.run_id,))).fetchone()
            assert saved['text_ref'] is None and artifacts.get(saved['metadata']['original_ref']) == BODY
            await harness.close()
    asyncio.run(check())


@database
@pytest.mark.parametrize('failure_event', ['source_search_saved', 'source_scope_frozen'])
def test_crash_replays_paid_search_and_completed_fetch_receipts(config, tmp_path, failure_event):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            harness = Harness(ledger, ArtifactStore(tmp_path), {'lead': [{'url': 'https://example.com/a'}]})
            original_event = ledger.event

            async def fail_event(conn, run_id, kind, payload):
                if kind == failure_event:
                    raise RuntimeError('simulated process interruption')
                await original_event(conn, run_id, kind, payload)

            ledger.event = fail_event
            with pytest.raises(RuntimeError):
                await harness.prepare(plan('lead'))
            ledger.event = original_event
            prepared = await harness.prepare(plan('lead'))
            assert len(prepared.scope.documents) == 1
            assert len(harness.search_calls) == len(harness.fetch_calls) == 1
            assert (await ledger.summary(ledger.run_id))['calculated_micro'] == 8_000
            await harness.close()
    asyncio.run(check())


@database
def test_interrupted_fetch_never_repeats_implicitly(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            harness = Harness(ledger, ArtifactStore(tmp_path), {'lead': [{'url': 'https://example.com/a'}]})
            calls = []

            async def interrupted(url):
                calls.append(url)
                raise asyncio.CancelledError()

            with pytest.raises(asyncio.CancelledError):
                await harness.prepare(plan('lead'), fetch=interrupted)
            async with pool.connection() as conn:
                run = await (await conn.execute('SELECT status FROM app.runs WHERE id=%s', (ledger.run_id,))).fetchone()
                attempt = await (await conn.execute('SELECT state,snapshot_id FROM app.source_fetch_attempts WHERE run_id=%s', (ledger.run_id,))).fetchone()
                assert run['status'] == 'paused' and attempt == {'state': 'unknown', 'snapshot_id': None}
                # Even an explicit owner resume cannot silently repeat an uncertain read.
                await conn.execute("UPDATE app.runs SET status='running' WHERE id=%s", (ledger.run_id,))
            with pytest.raises(Conflict, match='explicit review'):
                await harness.prepare(plan('lead'), fetch=interrupted)
            assert len(calls) == 1
            await harness.close()
    asyncio.run(check())


@database
@pytest.mark.parametrize('extract_first', [True, False])
def test_controlled_reads_and_tavily_extract_share_run_document_cap(config, tmp_path, extract_first):
    async def check():
        await migrate(DSN)
        cfg = config.model_copy(update={'limits': config.limits.model_copy(update={'document_fetches': 1})})
        async with execution_pool(DSN) as pool:
            ledger, _, price = await setup(pool, cfg)
            harness = Harness(ledger, ArtifactStore(tmp_path), {'lead': [{'url': 'https://example.com/a'}]})

            async def reserve_extract():
                return await ledger.reserve(run_id=ledger.run_id, request_id=uid(), logical_request_id=uid(),
                                            provider='tavily', model='tavily', endpoint='extract', agent='research',
                                            purpose='eval', pricing_id=price, upper='0.008')

            if extract_first:
                await reserve_extract()
                with pytest.raises(BudgetExceeded):
                    await harness.prepare(plan('lead'))
                assert not harness.fetch_calls
            else:
                await harness.prepare(plan('lead'))
                with pytest.raises(BudgetExceeded):
                    await reserve_extract()
            await harness.close()
    asyncio.run(check())


@database
def test_expired_clock_stops_before_paid_search(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            async with pool.connection() as conn:
                await conn.execute('UPDATE app.runs SET active_elapsed_ms=%s WHERE id=%s',
                                   (config.limits.active_seconds * 1000, ledger.run_id))
            harness = Harness(ledger, ArtifactStore(tmp_path), {'lead': [{'url': 'https://example.com/a'}]})
            with pytest.raises(BudgetExceeded):
                await harness.prepare(plan('lead'))
            assert not harness.search_calls and not harness.fetch_calls
            await harness.close()
    asyncio.run(check())


@database
@pytest.mark.parametrize('damage', ['delete_original', 'corrupt_text', 'corrupt_failed_manifest'])
def test_frozen_resume_revalidates_all_source_artifacts(config, tmp_path, damage):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            artifacts = ArtifactStore(tmp_path)
            harness = Harness(ledger, artifacts, {'lead': [{'url': 'https://example.com/a'}]})

            async def failed(url):
                raise FetchError('robots_disallowed', url)

            await harness.prepare(plan('lead'), **({'fetch': failed} if damage == 'corrupt_failed_manifest' else {}))
            async with pool.connection() as conn:
                row = await (await conn.execute('SELECT * FROM app.source_snapshots WHERE run_id=%s', (ledger.run_id,))).fetchone()
            if damage == 'delete_original':
                (tmp_path / row['metadata']['original_ref']).unlink()
            elif damage == 'corrupt_text':
                (tmp_path / row['text_ref']).write_bytes(b'tampered')
            else:
                (tmp_path / row['manifest_ref']).write_bytes(b'tampered')
            before = len(harness.search_calls), len(harness.fetch_calls)
            with pytest.raises((ValueError, FileNotFoundError)):
                await harness.prepare(plan('lead'))
            assert (len(harness.search_calls), len(harness.fetch_calls)) == before
            await harness.close()
    asyncio.run(check())
