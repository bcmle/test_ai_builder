import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from ai_builder.evidence import ArtifactStore
from ai_builder.config import load_config
from ai_builder.fetch_transport import FetchError
from ai_builder.legal_sources import (LegalAnalogue, analogue_id, discover_legal_links, document_type,
                                     legal_search_plan, materialize_legal_reviews, select_legal_links)
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_ledger import digest
from ai_builder.pg_migrate import migrate
from ai_builder.source_documents import ParsedDocument
from ai_builder.source_research import SourceResearchPlan, PreparedScope, make_reassessment_plan, prepare_scope
from ai_builder.pipeline import ScopePacket
from ai_builder.research_scope import _gap_state
from ai_builder.store import Conflict, uid
from test_pg_ledger import setup
from test_source_research import Harness, plan, response, parse_fixture

DSN = os.getenv('TEST_DATABASE_URL')
database = pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')


def analogue(product='Invoice'):
    return LegalAnalogue(analogue_id=analogue_id('Example', product), company_name='Example', product_name=product,
                         official_urls=('https://example.com/',))


def legal_plan(*analogues, reads=24, **kwargs):
    return SourceResearchPlan(schema_version='3', seed=plan().seed, queries=(),
                              legal_analogues=analogues or (analogue(),), max_new_reads=reads, **kwargs)


def test_plan_v3_is_explicit_frozen_and_legacy_bytes_stay_unchanged():
    legacy = plan()
    assert set(legacy.model_dump()) == {'schema_version', 'seed', 'queries', 'max_new_reads',
                                      'acquisition_limits', 'parser_limits'}
    version2 = SourceResearchPlan.model_validate({**legacy.model_dump(), 'schema_version': '2'})
    assert set(version2.model_dump()) == set(legacy.model_dump()) | {'priority_primary_urls'}
    new = make_reassessment_plan(seed=legacy.seed, parent_run_id='parent', analogues=[analogue()])
    assert new.schema_version == '3' and new.queries == () and new.legal_link_rounds == 2
    assert new == SourceResearchPlan.model_validate_json(new.model_dump_json())
    for invalid in ({'schema_version': '2'}, {'legal_analogues': []},
                    {'legal_analogues': [analogue(), analogue()]}, {'legal_links_per_round': 5},
                    {'reassessed_from_run_id': ''}, {'supplemented_from_run_id': 'old'}):
        with pytest.raises(ValidationError):
            SourceResearchPlan.model_validate({**new.model_dump(), **invalid})
    assert analogue_id(' EXAMPLE ', 'invoice') == analogue().analogue_id
    assert analogue('Other product').analogue_id != analogue().analogue_id


def test_legal_link_discovery_is_official_deduplicated_and_bounded_by_type():
    html = b'''<a href="/legal/terms#heading">Terms</a><a href="/legal/terms">Terms again</a>
        <a href="https://evil.example/legal/terms">Terms</a><a href="javascript:secret()">Privacy</a>
        <a href="/privacy">Privacy</a><a href="/dpa">Data processing agreement</a>
        <a href="/acceptable-use">Acceptable use</a><a href="/legal/license">License</a>'''
    links = discover_legal_links(html, 'https://example.com/', analogue())
    assert len(links) == 5
    selected, excluded = select_legal_links(links, 4)
    assert {link['document_type'] for link in selected} == {'terms', 'privacy', 'dpa', 'aup'}
    assert excluded[0]['reason'] == 'outside_bounded_review'
    assert document_type('https://example.com/legal/software-license') == 'software_license'
    assert document_type('https://example.com/legal/regulatory-license') == 'regulatory_license'
    assert document_type('https://example.com/product') is None


def test_hundreds_of_leads_do_not_become_hundreds_of_mandatory_reads():
    links = discover_legal_links(''.join(f'<a href="/legal/terms-{i}">Terms</a>' for i in range(500)).encode(),
                                 'https://example.com/', analogue())
    selected, excluded = select_legal_links(links, 4)
    assert len(selected) == 4 and len(excluded) == 496
    assert select_legal_links(reversed(links), 4) == (selected, excluded)


def test_no_source_modern_card_does_not_claim_unperformed_analysis(config):
    scope = ScopePacket(**plan().seed.model_dump(), documents=[])
    prepared = PreparedScope(scope, ())
    modern = load_config(Path(__file__).parents[1] / 'config/conditional-config.json')
    state = _gap_state(modern, prepared)
    assert all(item['state'] == 'pending' and item['result'] is None for item in state['criteria'])
    assert state['card']['eligibility'] == 'unresolved' and state['card']['completed_stages'] == []
    assert state['card']['score'] is None
    legacy = _gap_state(config, prepared)
    assert all(item['state'] == 'completed' and item['result'] == 'unknown' for item in legacy['criteria'])


@database
def test_migration_supports_legal_queue_slots_without_more_document_reads(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            harness = Harness(ledger, ArtifactStore(tmp_path), {})
            prepared = await harness.prepare(legal_plan(*(analogue(str(i)) for i in range(8))))
            assert len(prepared.scope.legal_source_reviews) == 8
            assert len(harness.fetch_calls) == 1 and len(harness.search_calls) == 8
            async with pool.connection() as conn:
                slots = await (await conn.execute('SELECT max(query_index) AS n FROM app.source_queries WHERE run_id=%s',
                                                  (ledger.run_id,))).fetchone()
                assert slots['n'] == 31
                migrations = await (await conn.execute('SELECT name FROM app.schema_migrations WHERE name=%s',
                    ('008_legal_source_queues.sql',))).fetchone()
                assert migrations
            await harness.close()
    asyncio.run(check())


@database
def test_legal_sources_each_analogue_read_dedup_replay_and_snapshot_binding(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            value = legal_plan(analogue('One'), analogue('Two'))
            search = {legal_search_plan(item)['query']: [{'url': 'https://example.com/legal/terms'}]
                      for item in value.legal_analogues}
            harness = Harness(ledger, ArtifactStore(tmp_path), search)
            calls = []

            async def fetch(url):
                calls.append(url)
                html = b'<p>Full source text.</p>'
                if url.endswith('/'):
                    html += b'<a href="/privacy">Privacy</a><a href="/dpa">Data processing agreement</a>'
                return replace(response(url), body=html)

            prepared = await harness.prepare(value, fetch=fetch)
            assert len(calls) == len(set(calls)) == 4
            assert len(prepared.scope.documents) == 4
            reviews = prepared.scope.legal_source_reviews
            assert len(reviews) == 2 and all(item['execution_state'] == 'completed' for item in reviews)
            assert all(len(item['documents']) == 3 for item in reviews)
            assert {item['snapshot_id'] for item in reviews[0]['documents']} == {
                item['snapshot_id'] for item in reviews[1]['documents']}
            assert all(doc['version'] is None and doc['effective_date'] is None for doc in reviews[0]['documents'])
            usage = await ledger.summary(ledger.run_id)
            assert usage['controlled_document_attempts'] == 4 and len(harness.search_calls) == 2

            def forbidden(*args, **kwargs):
                raise AssertionError('Frozen replay must not perform external work')

            assert await prepare_scope(ledger, value, forbidden, forbidden, harness.artifacts, parser=forbidden) == prepared
            assert await ledger.summary(ledger.run_id) == usage
            await harness.close()
    asyncio.run(check())


@database
@pytest.mark.parametrize('code', ['robots_disallowed', 'http_status', 'fetch_timeout'])
def test_performed_inaccessible_reads_are_completed_documentary_gaps(config, tmp_path, code):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            value = legal_plan()
            harness = Harness(ledger, ArtifactStore(tmp_path), {
                legal_search_plan(analogue())['query']: [{'url': 'https://example.com/terms'}]})

            async def denied(url):
                raise FetchError(code, url)

            result = await harness.prepare(value, fetch=denied)
            review = result.scope.legal_source_reviews[0]
            assert review['execution_state'] == 'completed' and review['coverage'] == 'unavailable'
            assert not result.scope.documents
            assert review['documents'][0]['disposition'] == 'inaccessible'
            assert review['documents'][0]['snapshot_id'] and review['documents'][0]['evidence_id'] is None
            assert any(action['action'] == 'search' and action['outcome'] == 'succeeded'
                       for action in review['search_actions'])
            await harness.close()
    asyncio.run(check())


@database
def test_empty_completed_search_is_not_found_but_no_search_is_not_completed(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            harness = Harness(ledger, ArtifactStore(tmp_path), {})
            result = await harness.prepare(legal_plan())
            assert result.scope.legal_source_reviews[0]['coverage'] == 'not_found'
            assert result.scope.legal_source_reviews[0]['execution_state'] == 'completed'
            assert materialize_legal_reviews((analogue(),), [])[0]['execution_state'] == 'pending'
            await harness.close()
    asyncio.run(check())


@database
def test_official_redirect_preserves_full_external_document_with_attribution_gap(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            harness = Harness(ledger, ArtifactStore(tmp_path), {
                legal_search_plan(analogue())['query']: [{'url': 'https://example.com/terms'}]})

            async def fetch(url):
                if url.endswith('/terms'):
                    return replace(response(url), final_url='https://legal-host.example/terms',
                                   redirect_chain=('https://legal-host.example/terms',))
                return response(url)

            prepared = await harness.prepare(legal_plan(), fetch=fetch)
            review = prepared.scope.legal_source_reviews[0]
            assert review['execution_state'] == 'completed' and review['coverage'] == 'insufficient'
            terms = review['documents'][0]
            assert terms['disposition'] == 'read' and terms['final_host_attribution'] == 'unverified_external_host'
            stored = next(doc for doc in prepared.scope.documents if doc.evidence_id == terms['evidence_id'])
            assert stored.text == response('https://example.com/terms').body.decode()
            assert stored.url == terms['final_url'] == 'https://legal-host.example/terms'
            gap = next(item for item in review['gaps'] if item['code'] == 'external_redirect_attribution_unverified')
            assert gap['url'] == 'https://example.com/terms' and gap['snapshot_id'] == stored.snapshot_id
            assert 'not independently established' in gap['reason']
            await harness.close()
    asyncio.run(check())


@database
@pytest.mark.parametrize('parser_failure', [False, True])
def test_selected_pending_read_and_internal_parser_failure_cannot_become_gap(config, tmp_path, parser_failure):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            harness = Harness(ledger, ArtifactStore(tmp_path), {
                legal_search_plan(analogue())['query']: [{'url': 'https://example.com/terms'}]})
            parser = (lambda response, limits: ParsedDocument(status='failed', error='parser_invalid_output',
                original_hash=hashlib.sha256(response.body).hexdigest())) if parser_failure else parse_fixture
            with pytest.raises(Conflict, match='pending or failed work'):
                await harness.prepare(legal_plan(reads=24 if parser_failure else 1), parser=parser)
            async with pool.connection() as conn:
                row = await (await conn.execute('SELECT state FROM app.source_research WHERE run_id=%s', (ledger.run_id,))).fetchone()
                assert row['state'] == 'preparing'
                run = await (await conn.execute('SELECT status FROM app.runs WHERE id=%s', (ledger.run_id,))).fetchone()
                assert run['status'] == 'paused'
            await harness.close()
    asyncio.run(check())


@database
def test_bounded_link_exclusions_are_audited_and_do_not_fake_read(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            harness = Harness(ledger, ArtifactStore(tmp_path), {})
            calls = []

            async def fetch(url):
                calls.append(url)
                body = ''.join(f'<a href="/legal/terms-{i}">Terms</a>' for i in range(300)).encode() if url.endswith('/') else b'Full terms.'
                return replace(response(url), body=body)

            result = await harness.prepare(legal_plan(), fetch=fetch)
            assert len(calls) == 5
            review = result.scope.legal_source_reviews[0]
            assert review['execution_state'] == 'completed' and review['coverage'] == 'insufficient'
            gap = next(gap for gap in review['gaps'] if gap['code'] == 'outside_bounded_review')
            assert gap['document_count'] == 296
            manifest = harness.artifacts.verify_manifest(gap['exclusion_manifest_ref'])
            omissions = json.loads(harness.artifacts.get(manifest['metadata']['data_ref']))
            assert len(omissions['documents']) == 296
            assert all(item['disposition'] == 'excluded' for item in omissions['documents'])
            assert len(review['documents']) == 4
            await harness.close()
    asyncio.run(check())


@database
def test_reassessment_retains_verified_full_parent_documents_without_refetch(config, tmp_path):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            parent, study, _ = await setup(pool, config)
            artifacts = ArtifactStore(tmp_path)
            original = plan()
            harness = Harness(parent, artifacts, {'invoice product': [{'url': 'https://example.com/terms'}]})
            old = await harness.prepare(original)
            context = {'plan': original.model_dump(mode='json')}
            async with pool.connection() as conn:
                await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',
                                   (parent.run_id, digest(context), Jsonb(context)))
                await conn.execute("UPDATE app.runs SET status='partial' WHERE id=%s", (parent.run_id,))
                await conn.execute('UPDATE app.run_leases SET expires_at=clock_timestamp() WHERE run_id=%s', (parent.run_id,))
            child, _, _ = await setup(pool, config, study=study)
            seed = original.seed.model_copy(update={'scope_id': uid(), 'variant_id': uid()})
            value = make_reassessment_plan(seed=seed, parent_run_id=parent.run_id, analogues=[analogue()])
            new = Harness(child, artifacts, {legal_search_plan(analogue())['query']: [{'url': 'https://example.com/terms'}]})
            prepared = await new.prepare(value)
            assert new.fetch_calls == ['https://example.com/']
            assert prepared.scope.documents[0] == old.scope.documents[0]
            document = prepared.scope.legal_source_reviews[0]['documents'][0]
            assert document['snapshot_id'] == old.scope.documents[0].snapshot_id
            assert document['disposition'] == 'duplicate'
            assert (await child.summary(child.run_id))['controlled_document_attempts'] == 1
            async with pool.connection() as conn:
                old_packet = await (await conn.execute('SELECT packet FROM app.source_research WHERE run_id=%s', (parent.run_id,))).fetchone()
            assert old_packet['packet'] == old.scope.model_dump(mode='json')
            await new.close()
            await harness.close()
    asyncio.run(check())
