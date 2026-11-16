"""Bounded, durable Tavily discovery -> controlled source reads -> frozen scope.

Search results are leads, never evidence. Acquired text enters the existing
business graph only after an immutable scope is saved. Source identity, read
snapshot identity and the graph's legacy document handle remain distinct.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from psycopg.types.json import Jsonb
from pydantic import ConfigDict, Field, field_validator, model_serializer, model_validator

from .domain import Contract
from .evidence import ArtifactStore
from .fetch_transport import FetchError, FetchLimits
from .legal_sources import (LegalAnalogue, discover_legal_links,
                            is_official_url, legal_search_plan, materialize_legal_reviews, select_legal_links)
from .pg_ledger import digest
from .pipeline import Document, ScopePacket
from .providers import ProviderFailure
from .source_documents import ParserLimits, SourceDocuments, parse_document
from .store import BudgetExceeded, Conflict, uid


class FrozenContract(Contract):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, frozen=True)


class ScopeSeed(FrozenContract):
    scope_id: str = Field(min_length=1, max_length=200)
    base_model_id: str = Field(min_length=1, max_length=200)
    variant_id: str = Field(min_length=1, max_length=200)
    neutral_model_description: str = Field(min_length=1, max_length=20_000)


class SearchQueryPlan(FrozenContract):
    query: str = Field(min_length=1, max_length=2000)
    max_results: int = Field(default=5, ge=1, le=10, strict=True)
    advanced: bool = False
    domains: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator('query')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Search query cannot be blank')
        return value

    @field_validator('domains')
    @classmethod
    def bounded_domains(cls, values):
        for value in values:
            if not value or len(value) > 253 or any(c in value for c in '/:@?# '):
                raise ValueError('Use plain domain names in search restrictions')
        return values


RECOVERY_KAYAK_ID = '03b5d22868b44bbb975d3cfc01d46f93'
RECOVERY_KAYAK_URLS = ('https://affiliates.kayak.com/', 'https://affiliates.kayak.com/deeplinks',
                       'https://www.kayak.com/terms-of-use')


class SourceResearchPlan(FrozenContract):
    schema_version: Literal['1', '2', '3', '4'] = '1'
    seed: ScopeSeed
    queries: tuple[SearchQueryPlan, ...] = Field(max_length=12)
    max_new_reads: int = Field(default=6, ge=1, le=24, strict=True)
    acquisition_limits: FetchLimits = Field(default_factory=FetchLimits)
    parser_limits: ParserLimits = Field(default_factory=ParserLimits)
    priority_primary_urls: tuple[str, ...] = Field(default=(), max_length=16)
    derived_from_run_id: str | None = None
    supplemented_from_run_id: str | None = None
    supplement_reason: str | None = Field(default=None, min_length=1, max_length=2000)
    legal_analogues: tuple[LegalAnalogue, ...] = Field(default=(), max_length=8)
    reassessed_from_run_id: str | None = None
    recovery_from_run_id: str | None = None
    legal_link_rounds: Literal[2] = 2
    legal_links_per_round: Literal[4] = 4

    @model_validator(mode='after')
    def version_bounds(self):
        if self.schema_version not in {'3','4'} and (self.legal_analogues or self.reassessed_from_run_id is not None):
            raise ValueError('Legal acquisition requires source plan version 3')
        if self.schema_version == '4':
            if (self.seed.base_model_id != RECOVERY_KAYAK_ID or not self.recovery_from_run_id
                    or not self.recovery_from_run_id.strip() or len(self.legal_analogues)!=1
                    or self.queries or self.priority_primary_urls != RECOVERY_KAYAK_URLS or self.max_new_reads != 3
                    or self.derived_from_run_id or self.supplemented_from_run_id or self.reassessed_from_run_id
                    or self.supplement_reason):
                raise ValueError('Recovery source plan permits only the three frozen KAYAK documents')
        elif self.recovery_from_run_id is not None:
            raise ValueError('Recovery provenance requires source plan version 4')
        if self.schema_version == '3':
            if (not self.legal_analogues or len(self.queries) + len(self.legal_analogues) > 12
                    or self.derived_from_run_id is not None or self.supplemented_from_run_id is not None
                    or self.priority_primary_urls or self.supplement_reason is not None):
                raise ValueError('Version 3 requires explicit analogues and at most twelve total searches')
            if len({item.analogue_id for item in self.legal_analogues}) != len(self.legal_analogues):
                raise ValueError('Every analogue must have a distinct stable identity')
            if self.reassessed_from_run_id is not None and not self.reassessed_from_run_id.strip():
                raise ValueError('Reassessment requires a nonblank parent identifier')
        if self.schema_version == '1' and (not self.queries or len(self.queries) > 4 or self.max_new_reads > 6
                                            or self.priority_primary_urls or self.derived_from_run_id is not None
                                            or self.supplemented_from_run_id is not None):
            raise ValueError('Version 1 permits four queries, six reads and no direct primary leads')
        if self.derived_from_run_id is not None and self.supplemented_from_run_id is not None:
            raise ValueError('Derived and supplemental source revisions are mutually exclusive')
        if self.supplement_reason is not None and self.supplemented_from_run_id is None:
            raise ValueError('Supplement reason belongs only to a supplemental revision')
        if self.derived_from_run_id is not None:
            if not self.derived_from_run_id.strip() or self.queries or not self.priority_primary_urls:
                raise ValueError('Derived source plan requires a parent, selected primary URLs and no new queries')
        elif self.supplemented_from_run_id is not None:
            if (not self.supplemented_from_run_id.strip() or self.queries
                    or not 1 <= len(self.priority_primary_urls) <= 5
                    or not len(self.priority_primary_urls) <= self.max_new_reads <= 5
                    or not self.supplement_reason or not self.supplement_reason.strip()):
                raise ValueError('Supplemental source plan requires a parent, one to five new URLs and no queries')
        elif not self.queries and self.schema_version not in {'3','4'}:
            raise ValueError('A source acquisition plan requires at least one query')
        urls = [canonical_url(url) for url in self.priority_primary_urls]
        if len(urls) != len(set(urls)):
            raise ValueError('Primary source URLs must be distinct after canonicalization')
        return self

    @model_serializer(mode='wrap')
    def serialize_compatible(self, handler):
        value = handler(self)
        if self.schema_version == '1':
            value.pop('priority_primary_urls', None)
        if self.derived_from_run_id is None:
            value.pop('derived_from_run_id', None)
        if self.supplemented_from_run_id is None:
            value.pop('supplemented_from_run_id', None)
        if self.supplement_reason is None:
            value.pop('supplement_reason', None)
        if self.recovery_from_run_id is None:
            value.pop('recovery_from_run_id', None)
        if self.schema_version not in {'3','4'}:
            value.pop('legal_analogues', None)
            value.pop('reassessed_from_run_id', None)
            value.pop('legal_link_rounds', None)
            value.pop('legal_links_per_round', None)
        elif self.reassessed_from_run_id is None:
            value.pop('reassessed_from_run_id', None)
        return value

    def fetch_limits(self):
        return self.acquisition_limits

    def parse_limits(self):
        return self.parser_limits


def make_reassessment_plan(*, seed: ScopeSeed, parent_run_id: str,
                           analogues, queries=(), max_new_reads: int = 24) -> SourceResearchPlan:
    """Build new applicability work without mutating or rerunning a frozen parent."""
    return SourceResearchPlan(schema_version='3', seed=seed, queries=tuple(queries),
        max_new_reads=max_new_reads, legal_analogues=tuple(analogues), reassessed_from_run_id=parent_run_id)


class ResearchGap(FrozenContract):
    code: str
    query_id: str | None = None
    result_id: str | None = None
    url: str | None = None
    snapshot_id: str | None = None


@dataclass(frozen=True)
class PreparedScope:
    scope: ScopePacket
    gaps: tuple[ResearchGap, ...]


def canonical_url(url: str) -> str:
    """Conservative syntactic identity; fetch transport still validates DNS/hops.

    Query strings and path spelling are significant and are never stripped to
    merge distinct documents. Fragments do not select different HTTP resources.
    """
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 for c in url):
        raise ValueError('Invalid source URL')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('Invalid source URL')
    if parsed.port not in {None, 80, 443}:
        raise ValueError('Unsupported source port')
    host = parsed.hostname.encode('idna').decode('ascii').lower()
    if ':' in host:
        host = '[' + host + ']'
    default_port = 443 if parsed.scheme == 'https' else 80
    netloc = host + (':' + str(parsed.port) if parsed.port and parsed.port != default_port else '')
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or '/', parsed.query, ''))


async def load_plan(ledger, scope_id=None) -> SourceResearchPlan:
    """Read one persisted plan for CLI resume; no reconstruction from defaults."""
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
        rows = await (await conn.execute(
            'SELECT plan FROM app.source_research WHERE run_id=%s AND (%s::text IS NULL OR scope_id=%s)',
            (ledger.run_id, scope_id, scope_id))).fetchall()
    if len(rows) != 1:
        raise Conflict('A unique saved source research plan is required')
    return SourceResearchPlan.model_validate(rows[0]['plan'])


async def _pause(ledger, conn, code, **detail):
    await conn.execute("UPDATE app.runs SET status='paused',control_version=control_version+1 WHERE id=%s",
                       (ledger.run_id,))
    await ledger.event(conn, ledger.run_id, code, detail)


async def _initialize(ledger, plan):
    value = plan.model_dump(mode='json')
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, run):
        row = await (await conn.execute('SELECT * FROM app.source_research WHERE run_id=%s AND scope_id=%s',
                                        (ledger.run_id, plan.seed.scope_id))).fetchone()
        if row:
            if row['plan_hash'] != digest(value):
                raise Conflict('Changed source research plan requires a new run')
            if row['state'] == 'frozen':
                if row['packet_hash'] != digest({'scope': row['packet'], 'gaps': row['gaps']}):
                    raise Conflict('Frozen source scope integrity mismatch')
                return PreparedScope(ScopePacket.model_validate(row['packet']),
                                     tuple(ResearchGap.model_validate(g) for g in row['gaps']))
        if run['status'] not in {'selecting', 'queued', 'running'}:
            raise Conflict('Run is paused; owner action required')
        bound = await (await conn.execute('SELECT 1 FROM app.graph_threads WHERE run_id=%s AND scope_id=%s',
                                          (ledger.run_id, plan.seed.scope_id))).fetchone()
        if bound:
            raise Conflict('Cannot acquire sources after the business graph has started')
        if run['active_started_at'] is None:
            await conn.execute('UPDATE app.runs SET active_started_at=clock_timestamp() WHERE id=%s', (ledger.run_id,))
        if not row:
            await conn.execute('INSERT INTO app.source_research(run_id,scope_id,plan_hash,plan) VALUES (%s,%s,%s,%s)',
                               (ledger.run_id, plan.seed.scope_id, digest(value), Jsonb(value)))
            if plan.priority_primary_urls:
                # Operator-supplied leads are not search results or evidence.
                # This distinct, already-resolved queue uses no provider receipt;
                # every URL still passes through the ordinary controlled reader.
                primary_id = uid()
                await conn.execute('''INSERT INTO app.source_queries
                    (id,run_id,scope_id,query_index,operation_key,query,state,completed_at)
                    VALUES (%s,%s,%s,0,%s,%s,'succeeded',clock_timestamp())''',
                    (primary_id, ledger.run_id, plan.seed.scope_id,
                     'source-primary:' + digest({'scope': plan.seed.scope_id}),
                     Jsonb({'kind': 'primary_urls', 'urls': list(plan.priority_primary_urls)})))
                for rank, url in enumerate(plan.priority_primary_urls, 1):
                    await conn.execute('''INSERT INTO app.source_results(id,query_id,result_rank,url)
                        VALUES (%s,%s,%s,%s)''', (uid(), primary_id, rank, url))
                await ledger.event(conn, ledger.run_id, 'source_primary_urls_saved',
                                   {'scope_id': plan.seed.scope_id, 'query_id': primary_id,
                                    'url_count': len(plan.priority_primary_urls)})
            for index, query in enumerate(plan.queries):
                operation_key = 'source-search:' + digest({'scope': plan.seed.scope_id, 'index': index})
                await conn.execute('''INSERT INTO app.source_queries(id,run_id,scope_id,query_index,operation_key,query)
                                      VALUES (%s,%s,%s,%s,%s,%s)''',
                                   (uid(), ledger.run_id, plan.seed.scope_id,
                                    index + int(bool(plan.priority_primary_urls)), operation_key,
                                    Jsonb(query.model_dump(mode='json'))))
            await ledger.event(conn, ledger.run_id, 'source_plan_saved', {'scope_id': plan.seed.scope_id,
                                                                        'plan_hash': digest(value)})
    return None


async def _search(ledger, plan, search_factory):
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
        queries = await (await conn.execute('SELECT * FROM app.source_queries WHERE run_id=%s AND scope_id=%s ORDER BY query_index',
                                           (ledger.run_id, plan.seed.scope_id))).fetchall()
    for row in queries:
        if row['state'] == 'succeeded':
            continue
        if row['state'] == 'failed':
            raise Conflict('Failed source search requires explicit review or a new run')
        query = SearchQueryPlan.model_validate(row['query'].get('search_plan', row['query']))
        adapter = search_factory(row['operation_key'])
        # A factory cannot silently switch accounting to another run/provider.
        if (adapter.transport.store is not ledger or adapter.transport.run_id != ledger.run_id
                or adapter.transport.operation_key != row['operation_key']):
            raise ValueError('Search transport must use this run ledger and durable operation key')
        try:
            response = await adapter.search(query.query, advanced=query.advanced,
                                            domains=list(query.domains), max_results=query.max_results)
            results = response.get('results')
            if not isinstance(results, list) or len(results) > query.max_results:
                raise ProviderFailure('invalid_search_results')
            for result in results:
                if not isinstance(result, dict) or not isinstance(result.get('url'), str):
                    raise ProviderFailure('invalid_search_result')
                if any(result.get(k) is not None and not isinstance(result[k], str) for k in ('title', 'content')):
                    raise ProviderFailure('invalid_search_result')
        except ProviderFailure as error:
            async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
                await conn.execute("UPDATE app.source_queries SET state='failed',error=%s,completed_at=clock_timestamp() WHERE id=%s",
                                   (error.code, row['id']))
                await _pause(ledger, conn, 'source_search_failed', query_id=row['id'], error=error.code)
            raise
        # A completed MeteredHTTP operation is replayed after a crash between the
        # paid search and this transaction, without a second request or charge.
        async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
            current = await (await conn.execute('SELECT state FROM app.source_queries WHERE id=%s', (row['id'],))).fetchone()
            if current['state'] == 'succeeded':
                continue
            for rank, result in enumerate(results, 1):
                await conn.execute('INSERT INTO app.source_results(id,query_id,result_rank,url,title,snippet) VALUES (%s,%s,%s,%s,%s,%s)',
                                   (uid(), row['id'], rank, result['url'], result.get('title'), result.get('content')))
            await conn.execute("UPDATE app.source_queries SET state='succeeded',completed_at=clock_timestamp() WHERE id=%s", (row['id'],))
            await ledger.event(conn, ledger.run_id, 'source_search_saved', {'query_id': row['id'], 'result_count': len(results)})


async def _reserve_read(ledger, plan, result, url):
    blocked = uncertain = False
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, run):
        old = await (await conn.execute('SELECT * FROM app.source_fetch_attempts WHERE run_id=%s AND canonical_url=%s',
                                        (ledger.run_id, url))).fetchone()
        if old:
            if old['state'] in {'succeeded', 'failed'}:
                disposition = 'duplicate' if old['state'] == 'succeeded' else 'inaccessible'
                await conn.execute('UPDATE app.source_results SET disposition=%s,reason=%s,snapshot_id=%s WHERE id=%s',
                                   (disposition, 'same_run_receipt' if disposition == 'duplicate' else old['error'], old['snapshot_id'], result['id']))
                return None
            uncertain = True
            await conn.execute("UPDATE app.source_fetch_attempts SET state='unknown',error='interrupted_read' WHERE id=%s", (old['id'],))
            await _pause(ledger, conn, 'uncertain_source_read', attempt_id=old['id'])
        else:
            if run['status'] not in {'selecting', 'queued', 'running'}:
                raise Conflict('Run is not accepting source reads')
            own = await (await conn.execute('SELECT count(*) AS n FROM app.source_fetch_attempts WHERE run_id=%s AND scope_id=%s',
                                            (ledger.run_id, plan.seed.scope_id))).fetchone()
            if own['n'] >= plan.max_new_reads:
                await conn.execute("UPDATE app.source_results SET reason='plan_read_limit' WHERE id=%s", (result['id'],))
                return None
            count = await (await conn.execute('''SELECT (SELECT count(*) FROM app.source_fetch_attempts WHERE run_id=%s) +
                (SELECT count(*) FROM app.usage WHERE run_id=%s AND provider='tavily' AND endpoint='extract') AS n''',
                (ledger.run_id, ledger.run_id))).fetchone()
            blocked = (count['n'] >= run['config_json']['limits']['document_fetches'] or
                       await ledger.active_ms(conn, run) >= run['config_json']['limits']['active_seconds'] * 1000)
            if blocked:
                await _pause(ledger, conn, 'source_budget_paused', result_id=result['id'])
            else:
                await conn.execute('INSERT INTO app.sources(id,canonical_url) VALUES (%s,%s) ON CONFLICT(canonical_url) DO NOTHING', (uid(), url))
                source = await (await conn.execute('SELECT id FROM app.sources WHERE canonical_url=%s', (url,))).fetchone()
                attempt = {'id': uid(), 'source_id': source['id'], 'url': url}
                await conn.execute('''INSERT INTO app.source_fetch_attempts(id,run_id,scope_id,source_id,canonical_url,state)
                                      VALUES (%s,%s,%s,%s,%s,'running')''',
                                   (attempt['id'], ledger.run_id, plan.seed.scope_id, source['id'], url))
                await ledger.event(conn, ledger.run_id, 'source_read_reserved', {'attempt_id': attempt['id'], 'source_id': source['id']})
                return attempt
    if uncertain:
        raise Conflict('Unfinished source read: explicit review required before retry; use a new run')
    if blocked:
        raise BudgetExceeded('Source document cap or active time exhausted')


async def _save_read(ledger, result, attempt, snapshot):
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
        old = await (await conn.execute('SELECT state FROM app.source_fetch_attempts WHERE id=%s', (attempt['id'],))).fetchone()
        if old['state'] != 'running':
            raise Conflict('Source read is no longer active')
        await conn.execute('''INSERT INTO app.source_snapshots(id,source_id,run_id,document_handle,manifest_ref,text_ref,state,metadata)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                           (snapshot['id'], attempt['source_id'], ledger.run_id, snapshot['document_handle'], snapshot['manifest_ref'],
                            snapshot['text_ref'], snapshot['state'], Jsonb(snapshot['metadata'])))
        succeeded = snapshot['state'] == 'succeeded'
        await conn.execute('''UPDATE app.source_fetch_attempts SET state=%s,snapshot_id=%s,error=%s,finished_at=clock_timestamp() WHERE id=%s''',
                           ('succeeded' if succeeded else 'failed', snapshot['id'], snapshot['metadata'].get('error'), attempt['id']))
        await conn.execute('UPDATE app.source_results SET disposition=%s,reason=%s,snapshot_id=%s WHERE id=%s',
                           ('read' if succeeded else 'inaccessible', snapshot['metadata'].get('error'), snapshot['id'], result['id']))
        await ledger.event(conn, ledger.run_id, 'source_read_saved', {'attempt_id': attempt['id'], 'snapshot_id': snapshot['id'], 'status': snapshot['state']})


async def _read(ledger, plan, result, fetch_document, artifacts, parser):
    try:
        url = canonical_url(result['url'])
    except ValueError:
        async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
            await conn.execute("UPDATE app.source_results SET disposition='excluded',reason='invalid_source_url' WHERE id=%s", (result['id'],))
        return
    if plan.schema_version in {'3','4'}:
        definition = result['query_definition']
        analogue = next((item for item in plan.legal_analogues
                         if item.analogue_id == definition.get('analogue_id')), None)
        if analogue is not None and not is_official_url(url, analogue):
            async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
                await conn.execute("UPDATE app.source_results SET disposition='excluded',reason='outside_official_analogue_domains' WHERE id=%s", (result['id'],))
            return
        # A reused immutable source is not a new network operation. In
        # particular, parent snapshots have no fetch attempt in this child run.
        async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
            old = await (await conn.execute('''SELECT s.id FROM app.source_results r
                JOIN app.source_queries q ON q.id=r.query_id
                JOIN app.source_snapshots s ON s.id=r.snapshot_id
                JOIN app.sources source ON source.id=s.source_id
                WHERE q.run_id=%s AND (source.canonical_url=%s OR s.metadata->>'final_url'=%s) AND s.state='succeeded'
                ORDER BY q.query_index,r.result_rank LIMIT 1''', (ledger.run_id, url, url))).fetchone()
            if old:
                await conn.execute("UPDATE app.source_results SET disposition='duplicate',reason='reused_immutable_snapshot',snapshot_id=%s WHERE id=%s",
                                   (old['id'], result['id']))
                return
    attempt = await _reserve_read(ledger, plan, result, url)
    if not attempt:
        return
    try:
        try:
            response = await fetch_document(url)
        except FetchError as error:
            metadata = {'kind': 'source_snapshot', 'schema_version': '1.2', 'snapshot_id': uid(),
                        'source_id': attempt['source_id'], 'run_id': ledger.run_id, 'requested_url': url,
                        'final_url': None, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                        'fetch_status': 'failed', 'error': error.code, 'redirect_chain': list(error.redirect_chain),
                        'provenance': 'controlled_http', 'source_kind': 'unknown', 'published_at': None,
                        'event_date': None, 'applicability': 'unverified'}
            manifest = artifacts.manifest([], metadata)
            snapshot = dict(id=metadata['snapshot_id'], document_handle=None, manifest_ref=manifest,
                            text_ref=None, state='failed', metadata=metadata)
        else:
            if canonical_url(response.requested_url) != url:
                raise ValueError('Fetch response belongs to a different requested URL')
            parsed = await asyncio.to_thread(parser, response, plan.parse_limits())
            saved = await asyncio.to_thread(SourceDocuments(artifacts).save, response, parsed,
                                           source_id=attempt['source_id'], run_id=ledger.run_id)
            metadata = artifacts.verify_manifest(saved.manifest_ref)['metadata']
            snapshot = dict(id=saved.snapshot_id, document_handle=uid() if saved.status == 'succeeded' else None,
                            manifest_ref=saved.manifest_ref, text_ref=saved.text_ref, state=saved.status, metadata=metadata)
        await _save_read(ledger, result, attempt, snapshot)
    except BaseException:
        # External completion cannot be inferred after interruption. Keep the
        # counted reservation and fail closed, including after lease expiry.
        try:
            async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
                await conn.execute("UPDATE app.source_fetch_attempts SET state='unknown',error='interrupted_read' WHERE id=%s AND state='running'", (attempt['id'],))
                await _pause(ledger, conn, 'uncertain_source_read', attempt_id=attempt['id'])
        except (Conflict, asyncio.CancelledError):
            pass  # A stale worker must not publish; the next lease sees running.
        raise


async def _results(ledger, scope_id):
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
        return await _result_rows(conn, ledger.run_id, scope_id)


async def _result_rows(conn, run_id, scope_id):
    return await (await conn.execute('''SELECT r.*,q.id AS query_identity,q.query_index,q.query AS query_definition,
            q.state AS query_state,q.error AS query_error,q.completed_at AS query_completed_at,
            s.document_handle,s.text_ref,s.manifest_ref,s.metadata,
            s.state AS snapshot_state,s.source_id
            FROM app.source_queries q LEFT JOIN app.source_results r ON r.query_id=q.id
            LEFT JOIN app.source_snapshots s ON s.id=r.snapshot_id
            WHERE q.run_id=%s AND q.scope_id=%s ORDER BY q.query_index,r.result_rank''', (run_id, scope_id))).fetchall()


async def _queue(ledger, conn, plan, index, definition, *, retained=(), urls=(), search=False):
    query_id = uid()
    await conn.execute('''INSERT INTO app.source_queries
        (id,run_id,scope_id,query_index,operation_key,query,state,completed_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN NULL ELSE clock_timestamp() END)''',
        (query_id, ledger.run_id, plan.seed.scope_id, index,
         'source-v3:' + digest({'scope': plan.seed.scope_id, 'index': index, 'definition': definition}),
         Jsonb(definition), 'pending' if search else 'succeeded', search))
    if retained:
        for rank, row in enumerate(retained, 1):
            await conn.execute('''INSERT INTO app.source_results
                (id,query_id,result_rank,url,title,disposition,reason,snapshot_id)
                VALUES (%s,%s,%s,%s,%s,'duplicate','retained_frozen_source',%s)''',
                (uid(), query_id, rank, row['url'], row.get('title'), row['snapshot_id']))
    else:
        for rank, item in enumerate(urls, 1):
            item = {'url': item} if isinstance(item, str) else item
            await conn.execute('INSERT INTO app.source_results(id,query_id,result_rank,url,title) VALUES (%s,%s,%s,%s,%s)',
                               (uid(), query_id, rank, item['url'], item.get('title')))
    return index + 1


async def _initialize_legal_sources(ledger, plan, artifacts):
    """Create v3 queues atomically, preserving verified full parent snapshots."""
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, run):
        if await (await conn.execute('SELECT 1 FROM app.source_research WHERE run_id=%s AND scope_id=%s',
                                    (ledger.run_id, plan.seed.scope_id))).fetchone():
            return
        if run['status'] not in {'selecting', 'queued', 'running'}:
            raise Conflict('Run is not accepting a new legal source plan')
        retained = []
        if plan.reassessed_from_run_id is not None:
            parent = await (await conn.execute('SELECT * FROM app.runs WHERE id=%s', (plan.reassessed_from_run_id,))).fetchone()
            context = await (await conn.execute('SELECT context,context_hash FROM app.research_contexts WHERE run_id=%s',
                                                (plan.reassessed_from_run_id,))).fetchone()
            packets = await (await conn.execute('SELECT * FROM app.source_research WHERE run_id=%s',
                                                (plan.reassessed_from_run_id,))).fetchall()
            if (not parent or parent['study_id'] != run['study_id'] or parent['status'] not in {'partial', 'completed'}
                    or not context or digest(context['context']) != context['context_hash']
                    or len(packets) != 1 or packets[0]['state'] != 'frozen'):
                raise Conflict('Reassessment requires a completed frozen parent in the same study')
            if await (await conn.execute('''SELECT 1 FROM app.usage WHERE run_id=%s AND state IN ('reserved','unknown')
                UNION ALL SELECT 1 FROM app.provider_operations WHERE run_id=%s AND state<>'completed'
                UNION ALL SELECT 1 FROM app.run_leases WHERE run_id=%s AND expires_at>clock_timestamp() LIMIT 1''',
                (parent['id'], parent['id'], parent['id']))).fetchone():
                raise Conflict('Reassessment requires no active parent owner or uncertain provider operation')
            source = packets[0]
            parent_plan = SourceResearchPlan.model_validate(source['plan'])
            if (digest(source['plan']) != source['plan_hash'] or context['context']['plan'] != source['plan']
                    or digest({'scope': source['packet'], 'gaps': source['gaps']}) != source['packet_hash']
                    or parent_plan.seed.base_model_id != plan.seed.base_model_id
                    or parent_plan.seed.neutral_model_description != plan.seed.neutral_model_description
                    or parent_plan.seed.scope_id == plan.seed.scope_id
                    or parent_plan.seed.variant_id == plan.seed.variant_id):
                raise Conflict('Reassessment differs from its frozen parent model or source integrity')
            rows = await _result_rows(conn, parent['id'], parent_plan.seed.scope_id)
            verified = _materialize(parent_plan, rows, artifacts)
            if (verified.scope.model_dump(mode='json') != source['packet']
                    or [gap.model_dump(mode='json') for gap in verified.gaps] != source['gaps']):
                raise Conflict('Parent frozen sources differ from retained receipts')
            by_snapshot = {row['snapshot_id']: row for row in rows if row['snapshot_state'] == 'succeeded'}
            retained = [by_snapshot[doc.snapshot_id] for doc in verified.scope.documents]
        value = plan.model_dump(mode='json')
        await conn.execute('INSERT INTO app.source_research(run_id,scope_id,plan_hash,plan) VALUES (%s,%s,%s,%s)',
                           (ledger.run_id, plan.seed.scope_id, digest(value), Jsonb(value)))
        index = 0
        for start in range(0, len(retained), 16):
            chunk = retained[start:start + 16]
            index = await _queue(ledger, conn, plan, index,
                {'kind': 'retained_reassessment_sources', 'source_run_id': plan.reassessed_from_run_id,
                 'source_result_refs': [{'result_id': row['id'], 'snapshot_id': row['snapshot_id'],
                                         'manifest_ref': row['manifest_ref']} for row in chunk]}, retained=chunk)
        for analogue in plan.legal_analogues:
            index = await _queue(ledger, conn, plan, index,
                {'kind': 'legal_official_urls', 'analogue_id': analogue.analogue_id,
                 'urls': list(analogue.official_urls)}, urls=analogue.official_urls)
            index = await _queue(ledger, conn, plan, index,
                {'kind': 'legal_search', 'analogue_id': analogue.analogue_id,
                 'search_plan': legal_search_plan(analogue)}, search=True)
        for query in plan.queries:
            index = await _queue(ledger, conn, plan, index, query.model_dump(mode='json'), search=True)
        await ledger.event(conn, ledger.run_id, 'legal_source_plan_saved', {
            'scope_id': plan.seed.scope_id, 'plan_hash': digest(value), 'source_run_id': plan.reassessed_from_run_id,
            'analogue_ids': [item.analogue_id for item in plan.legal_analogues],
            'retained_snapshot_ids': [row['snapshot_id'] for row in retained], 'retained_full_documents': True})


async def _discover_legal_sources(ledger, plan, artifacts, depth):
    """Two explicit link-discovery rounds, based only on stored official HTML."""
    rows = await _results(ledger, plan.seed.scope_id)
    discoveries = []
    for analogue in plan.legal_analogues:
        if any(row['query_definition'].get('kind') == 'legal_link_urls'
               and row['query_definition'].get('analogue_id') == analogue.analogue_id
               and row['query_definition'].get('depth') == depth for row in rows):
            continue
        known, links, snapshots = set(), {}, set()
        for row in rows:
            if row.get('url') and (row['query_definition'].get('analogue_id') == analogue.analogue_id
                                  or row['query_definition'].get('kind') == 'retained_reassessment_sources'):
                try:
                    known.add(canonical_url(row['url']))
                except ValueError:
                    pass
        for row in rows:
            definition, metadata = row['query_definition'], row.get('metadata') or {}
            owned = definition.get('analogue_id') == analogue.analogue_id
            retained = definition.get('kind') == 'retained_reassessment_sources'
            if (not (owned or retained) or definition.get('depth', 0) != depth - 1
                    or row.get('snapshot_state') != 'succeeded'
                    or not metadata.get('original_ref') or not metadata.get('mime', '').startswith(('text/html', 'application/xhtml+xml'))
                    or not is_official_url(metadata.get('final_url', ''), analogue)
                    or row['snapshot_id'] in snapshots):
                continue
            # Integrity must hold before HTML can create new durable work.
            if artifacts.verify_manifest(row['manifest_ref'])['metadata'] != metadata:
                raise Conflict('Legal discovery source manifest integrity mismatch')
            snapshots.add(row['snapshot_id'])
            for link in discover_legal_links(artifacts.get(metadata['original_ref']), metadata['final_url'], analogue):
                if link['url'] not in known:
                    links.setdefault(link['url'], link)
        selected, excluded = select_legal_links(links.values(), plan.legal_links_per_round)
        exclusion_manifest = None
        if excluded:
            exclusion_data = {'kind': 'legal_link_exclusions', 'schema_version': '1',
                'scope_id': plan.seed.scope_id, 'analogue_id': analogue.analogue_id, 'depth': depth,
                'source_snapshot_ids': sorted(snapshots), 'documents': excluded}
            exclusion_ref = artifacts.put(json.dumps(exclusion_data, sort_keys=True, separators=(',', ':')).encode())
            exclusion_manifest = artifacts.manifest([exclusion_ref], {
                'kind': 'legal_link_exclusions', 'analogue_id': analogue.analogue_id,
                'depth': depth, 'document_count': len(excluded), 'data_ref': exclusion_ref})
        discoveries.append((analogue, selected, sorted(snapshots), len(excluded), exclusion_manifest))
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, run):
        if run['status'] not in {'selecting', 'queued', 'running'}:
            raise Conflict('Run paused before legal link discovery')
        last = await (await conn.execute('SELECT max(query_index) AS n FROM app.source_queries WHERE run_id=%s AND scope_id=%s',
                                         (ledger.run_id, plan.seed.scope_id))).fetchone()
        index = last['n'] + 1
        required = len(discoveries)
        if index + required > 64:
            # Do not drop known documents or manufacture a completed gap.
            raise Conflict('Legal discovery queue bound exceeded; explicit narrower scope is required')
        for analogue, links, snapshots, excluded_count, exclusion_manifest in discoveries:
            for start in range(0, max(1, len(links)), 16):
                chunk = links[start:start + 16]
                index = await _queue(ledger, conn, plan, index,
                    {'kind': 'legal_link_urls', 'analogue_id': analogue.analogue_id, 'depth': depth,
                     'source_snapshot_ids': snapshots, 'urls': [item['url'] for item in chunk],
                     'excluded_count': excluded_count, 'exclusion_manifest_ref': exclusion_manifest}, urls=chunk)
            await ledger.event(conn, ledger.run_id, 'legal_links_discovered', {
                'analogue_id': analogue.analogue_id, 'depth': depth, 'source_snapshot_ids': snapshots,
                'url_count': len(links), 'excluded_count': excluded_count, 'exclusion_manifest_ref': exclusion_manifest})


async def _initialize_retained_sources(ledger, plan, artifacts):
    """Create a distinct source-only revision after an unpaid input-size stop.

    Reuse is explicit: rows point to the original immutable snapshots, dates and
    manifests. No fetch attempts, search calls or provider receipts are invented.
    """
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, run):
        existing = await (await conn.execute('SELECT 1 FROM app.source_research WHERE run_id=%s AND scope_id=%s',
                                             (ledger.run_id, plan.seed.scope_id))).fetchone()
        if existing:
            return  # The common initializer checks the exact persisted plan hash.
        parent = await (await conn.execute('SELECT * FROM app.runs WHERE id=%s',
                                           (plan.derived_from_run_id,))).fetchone()
        saved_context = await (await conn.execute('SELECT context,context_hash FROM app.research_contexts WHERE run_id=%s',
                                                  (plan.derived_from_run_id,))).fetchone()
        packets = await (await conn.execute('SELECT * FROM app.source_research WHERE run_id=%s',
                                            (plan.derived_from_run_id,))).fetchall()
        if (run['status'] not in {'selecting', 'queued', 'running'} or not parent
                or parent['status'] != 'paused' or parent['study_id'] != run['study_id']
                or parent['config_hash'] != run['config_hash'] or parent['config_json'] != run['config_json']
                or not saved_context or digest(saved_context['context']) != saved_context['context_hash']
                or len(packets) != 1 or packets[0]['state'] != 'frozen'):
            raise Conflict('Retained sources require a paused, frozen parent in the same study and configuration')
        if await (await conn.execute('''SELECT 1 FROM app.usage WHERE run_id=%s
            AND (provider='openai' OR state IN ('reserved','unknown'))
            UNION ALL SELECT 1 FROM app.provider_operations WHERE run_id=%s AND state<>'completed' LIMIT 1''',
            (parent['id'], parent['id']))).fetchone():
            raise Conflict('Source refinement requires no prior LLM usage or uncertain provider operation')
        source = packets[0]
        parent_plan = SourceResearchPlan.model_validate(source['plan'])
        if (digest(source['plan']) != source['plan_hash']
                or saved_context['context']['plan'] != source['plan']
                or digest({'scope': source['packet'], 'gaps': source['gaps']}) != source['packet_hash']
                or parent_plan.seed.base_model_id != plan.seed.base_model_id
                or parent_plan.seed.neutral_model_description != plan.seed.neutral_model_description
                or parent_plan.seed.scope_id == plan.seed.scope_id
                or parent_plan.seed.variant_id == plan.seed.variant_id):
            raise Conflict('Retained source revision differs from its frozen parent model or scope')
        context_limit = saved_context['context']['openai_pricing']['input_context_limit']
        if len(json.dumps(source['packet']['documents'], ensure_ascii=False).encode()) <= context_limit:
            raise Conflict('Parent source packet does not establish an input-size preflight stop')
        rows = await _result_rows(conn, parent['id'], parent_plan.seed.scope_id)
        verified = _materialize(parent_plan, rows, artifacts)
        if (verified.scope.model_dump(mode='json') != source['packet']
                or [gap.model_dump(mode='json') for gap in verified.gaps] != source['gaps']):
            raise Conflict('Parent frozen sources differ from retained receipts')
        eligible = {canonical_url(row['url']): row for row in rows
                    if row['url'] and row['query_definition'].get('kind') in {'primary_urls', 'retained_primary_urls'}
                    and row['snapshot_state'] == 'succeeded' and row['text_ref']}
        selected = []
        for url in plan.priority_primary_urls:
            row = eligible.get(canonical_url(url))
            if row is None:
                raise Conflict('Selected URL is not a successfully acquired primary source of this parent')
            selected.append(row)
        value, primary_id = plan.model_dump(mode='json'), uid()
        await conn.execute('INSERT INTO app.source_research(run_id,scope_id,plan_hash,plan) VALUES (%s,%s,%s,%s)',
                           (ledger.run_id, plan.seed.scope_id, digest(value), Jsonb(value)))
        await conn.execute('''INSERT INTO app.source_queries
            (id,run_id,scope_id,query_index,operation_key,query,state,completed_at)
            VALUES (%s,%s,%s,0,%s,%s,'succeeded',clock_timestamp())''',
            (primary_id, ledger.run_id, plan.seed.scope_id,
             'source-retained:' + digest({'scope': plan.seed.scope_id, 'parent': parent['id']}),
             Jsonb({'kind': 'retained_primary_urls', 'source_run_id': parent['id'],
                    'urls': list(plan.priority_primary_urls)})))
        for rank, (url, row) in enumerate(zip(plan.priority_primary_urls, selected), 1):
            await conn.execute('''INSERT INTO app.source_results
                (id,query_id,result_rank,url,disposition,reason,snapshot_id)
                VALUES (%s,%s,%s,%s,'duplicate','retained_frozen_source',%s)''',
                (uid(), primary_id, rank, url, row['snapshot_id']))
        selected_ids = {row['snapshot_id'] for row in selected}
        await ledger.event(conn, ledger.run_id, 'source_scope_derived', {
            'source_run_id': parent['id'], 'scope_id': plan.seed.scope_id,
            'selected_snapshot_ids': sorted(selected_ids),
            'excluded_snapshot_ids': sorted({doc.snapshot_id for doc in verified.scope.documents} - selected_ids),
            'retained_full_documents': True, 'new_document_reads': 0, 'new_provider_calls': 0})


async def _initialize_supplemented_sources(ledger, plan, artifacts):
    """One explicitly requested evidence addition to a completed needs-review run."""
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, run):
        existing = await (await conn.execute('SELECT 1 FROM app.source_research WHERE run_id=%s AND scope_id=%s',
                                             (ledger.run_id, plan.seed.scope_id))).fetchone()
        if existing:
            return  # Resume validates the immutable plan in the common initializer.
        parent = await (await conn.execute('SELECT * FROM app.runs WHERE id=%s',
                                           (plan.supplemented_from_run_id,))).fetchone()
        saved_context = await (await conn.execute('SELECT context,context_hash FROM app.research_contexts WHERE run_id=%s',
                                                  (plan.supplemented_from_run_id,))).fetchone()
        packets = await (await conn.execute('SELECT * FROM app.source_research WHERE run_id=%s',
                                            (plan.supplemented_from_run_id,))).fetchall()
        if (run['status'] not in {'selecting', 'queued', 'running'} or not parent
                or parent['status'] not in {'partial', 'completed'} or parent['study_id'] != run['study_id']
                or parent['config_hash'] != run['config_hash'] or parent['config_json'] != run['config_json']
                or not saved_context or digest(saved_context['context']) != saved_context['context_hash']
                or len(packets) != 1 or packets[0]['state'] != 'frozen'):
            raise Conflict('Supplement requires a completed frozen parent in the same study and configuration')
        if await (await conn.execute('''SELECT 1 FROM app.usage WHERE run_id=%s AND state IN ('reserved','unknown')
            UNION ALL SELECT 1 FROM app.provider_operations WHERE run_id=%s AND state<>'completed'
            UNION ALL SELECT 1 FROM app.run_leases WHERE run_id=%s AND expires_at>clock_timestamp() LIMIT 1''',
            (parent['id'], parent['id'], parent['id']))).fetchone():
            raise Conflict('Supplement requires no active parent owner or uncertain provider operation')
        source = packets[0]
        parent_plan = SourceResearchPlan.model_validate(source['plan'])
        cards = await (await conn.execute('''SELECT result FROM app.node_receipts
            WHERE run_id=%s AND scope_id=%s AND node_name='card' ''',
            (parent['id'], parent_plan.seed.scope_id))).fetchall()
        if (len(cards) != 1 or cards[0]['result'].get('eligibility') != 'needs_review'
                or cards[0]['result'].get('assessment_scope_id') != parent_plan.seed.scope_id):
            raise Conflict('Supplement requires an accepted needs-review card for the parent scope')
        if (parent_plan.supplemented_from_run_id is not None
                or await (await conn.execute('''SELECT 1 FROM app.source_research
                    WHERE plan->>'supplemented_from_run_id'=%s LIMIT 1''', (parent['id'],))).fetchone()):
            raise Conflict('Only one supplemental revision is allowed; resume its saved run')
        if (digest(source['plan']) != source['plan_hash']
                or saved_context['context']['plan'] != source['plan']
                or digest({'scope': source['packet'], 'gaps': source['gaps']}) != source['packet_hash']
                or parent_plan.seed.base_model_id != plan.seed.base_model_id
                or parent_plan.seed.neutral_model_description != plan.seed.neutral_model_description
                or parent_plan.seed.scope_id == plan.seed.scope_id
                or parent_plan.seed.variant_id == plan.seed.variant_id):
            raise Conflict('Supplement differs from its frozen parent model or scope')
        rows = await _result_rows(conn, parent['id'], parent_plan.seed.scope_id)
        verified = _materialize(parent_plan, rows, artifacts)
        if (verified.scope.model_dump(mode='json') != source['packet']
                or [gap.model_dump(mode='json') for gap in verified.gaps] != source['gaps']
                or len(verified.scope.documents) != 10):
            raise Conflict('Supplement must preserve the ten verified full parent documents')
        parent_urls = {canonical_url(row['url']) for row in rows if row['url']}
        parent_urls.update(canonical_url(doc.url) for doc in verified.scope.documents)
        if any(canonical_url(url) in parent_urls for url in plan.priority_primary_urls):
            raise Conflict('Supplemental URLs must be new; parent source reads are not repeated')
        by_snapshot = {row['snapshot_id']: row for row in rows if row['snapshot_state'] == 'succeeded'}
        retained = [by_snapshot[doc.snapshot_id] for doc in verified.scope.documents]
        value, retained_id, new_id = plan.model_dump(mode='json'), uid(), uid()
        manifest = {'source_run_id': parent['id'], 'reason': plan.supplement_reason,
                    'retained': [{'result_id': row['id'], 'snapshot_id': row['snapshot_id'],
                                  'manifest_ref': row['manifest_ref']} for row in retained],
                    'new_primary_urls': list(plan.priority_primary_urls)}
        await conn.execute('INSERT INTO app.source_research(run_id,scope_id,plan_hash,plan) VALUES (%s,%s,%s,%s)',
                           (ledger.run_id, plan.seed.scope_id, digest(value), Jsonb(value)))
        for index, query_id, kind, payload in (
                (0, retained_id, 'retained_primary_urls', {'source_run_id': parent['id'],
                    'source_result_refs': manifest['retained'], 'manifest_hash': digest(manifest)}),
                (1, new_id, 'supplemental_primary_urls', {'urls': list(plan.priority_primary_urls),
                    'manifest_hash': digest(manifest)})):
            await conn.execute('''INSERT INTO app.source_queries
                (id,run_id,scope_id,query_index,operation_key,query,state,completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,'succeeded',clock_timestamp())''',
                (query_id, ledger.run_id, plan.seed.scope_id, index,
                 'source-supplement:' + digest({'scope': plan.seed.scope_id, 'kind': kind}),
                 Jsonb({'kind': kind, **payload})))
        for rank, row in enumerate(retained, 1):
            await conn.execute('''INSERT INTO app.source_results
                (id,query_id,result_rank,url,disposition,reason,snapshot_id)
                VALUES (%s,%s,%s,%s,'duplicate','retained_frozen_source',%s)''',
                (uid(), retained_id, rank, row['url'], row['snapshot_id']))
        for rank, url in enumerate(plan.priority_primary_urls, 1):
            await conn.execute('INSERT INTO app.source_results(id,query_id,result_rank,url) VALUES (%s,%s,%s,%s)',
                               (uid(), new_id, rank, url))
        await ledger.event(conn, ledger.run_id, 'source_supplement_manifest_frozen',
                           {**manifest, 'manifest_hash': digest(manifest), 'scope_id': plan.seed.scope_id,
                            'retained_full_documents': True, 'new_search_calls': 0})


async def verify_recovery_sources(conn, plan, artifacts, study_id):
    """Read-only proof of the complete old KAYAK packet, including failed reads."""
    parent = await (await conn.execute('SELECT * FROM app.runs WHERE id=%s', (plan.recovery_from_run_id,))).fetchone()
    context = await (await conn.execute('SELECT * FROM app.research_contexts WHERE run_id=%s',
                                        (plan.recovery_from_run_id,))).fetchone()
    packets = await (await conn.execute('SELECT * FROM app.source_research WHERE run_id=%s',
                                        (plan.recovery_from_run_id,))).fetchall()
    if (not parent or parent['study_id']!=study_id or parent['status'] not in {'partial','completed'}
            or not context or digest(context['context'])!=context['context_hash']
            or len(packets)!=1 or packets[0]['state']!='frozen'):
        raise Conflict('Recovery requires the intact frozen source parent in the same study')
    if await (await conn.execute('''SELECT 1 FROM app.usage WHERE run_id=%s AND state IN ('reserved','unknown')
        UNION ALL SELECT 1 FROM app.provider_operations WHERE run_id=%s AND state<>'completed'
        UNION ALL SELECT 1 FROM app.run_leases WHERE run_id=%s AND expires_at>clock_timestamp() LIMIT 1''',
        (parent['id'],parent['id'],parent['id']))).fetchone():
        raise Conflict('Recovery source parent has active or unresolved operations')
    source=packets[0]
    old=SourceResearchPlan.model_validate(source['plan'])
    if (old.schema_version!='3' or old.legal_analogues!=plan.legal_analogues
            or old.seed.base_model_id!=plan.seed.base_model_id
            or old.seed.neutral_model_description!=plan.seed.neutral_model_description
            or old.seed.scope_id==plan.seed.scope_id or old.seed.variant_id==plan.seed.variant_id
            or source['plan_hash']!=digest(source['plan']) or context['context']['plan']!=source['plan']
            or source['packet_hash']!=digest({'scope':source['packet'],'gaps':source['gaps']})):
        raise Conflict('Recovery source plan differs from its exact parent identity')
    rows=await _result_rows(conn,parent['id'],old.seed.scope_id)
    verified=_materialize(old,rows,artifacts)
    if (verified.scope.model_dump(mode='json')!=source['packet']
            or [g.model_dump(mode='json') for g in verified.gaps]!=source['gaps']
            or any(row['query_state']!='succeeded' or row.get('disposition')=='pending' for row in rows)
            or any(r['execution_state']!='completed' for r in verified.scope.legal_source_reviews)):
        raise Conflict('Recovery parent documents or completed acquisition receipts differ')
    return verified, rows


async def _initialize_recovery_sources(ledger, plan, artifacts):
    async with ledger.locked(ledger.run_id,fenced=True) as (conn,_,run):
        if await (await conn.execute('SELECT 1 FROM app.source_research WHERE run_id=%s', (ledger.run_id,))).fetchone():
            return
        bound=await (await conn.execute('SELECT * FROM app.recovery_runs WHERE run_id=%s', (ledger.run_id,))).fetchone()
        if not bound or bound['model_id']!=plan.seed.base_model_id or bound['study_id']!=run['study_id']:
            raise Conflict('Source recovery must belong to its immutable two-model allowance')
        _,rows=await verify_recovery_sources(conn,plan,artifacts,run['study_id'])
        value=plan.model_dump(mode='json')
        await conn.execute('INSERT INTO app.source_research(run_id,scope_id,plan_hash,plan) VALUES (%s,%s,%s,%s)',
                           (ledger.run_id,plan.seed.scope_id,digest(value),Jsonb(value)))
        groups={}
        for row in rows: groups.setdefault(row['query_identity'],[]).append(row)
        if len(groups)+1>64: raise Conflict('Recovery source queue bound exceeded')
        for index,(old_query,items) in enumerate(groups.items()):
            first=items[0]; query_id=uid()
            definition={**first['query_definition'],'retained_query_id':old_query,
                        'retained_from_run_id':plan.recovery_from_run_id}
            await conn.execute('''INSERT INTO app.source_queries
                (id,run_id,scope_id,query_index,operation_key,query,state,completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,'succeeded',%s)''',
                (query_id,ledger.run_id,plan.seed.scope_id,index,'recovery-retained:'+old_query,
                 Jsonb(definition),first['query_completed_at']))
            for row in items:
                if row['id'] is None: continue
                await conn.execute('''INSERT INTO app.source_results
                    (id,query_id,result_rank,url,title,snippet,disposition,reason,snapshot_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (uid(),query_id,row['result_rank'],row['url'],row['title'],row['snippet'],
                     row['disposition'],row['reason'],row['snapshot_id']))
        await _queue(ledger,conn,plan,len(groups),{'kind':'legal_official_urls',
            'analogue_id':plan.legal_analogues[0].analogue_id,'urls':list(plan.priority_primary_urls)},
            urls=plan.priority_primary_urls)
        await ledger.event(conn,ledger.run_id,'recovery_source_plan_saved',{
            'source_run_id':plan.recovery_from_run_id,'plan_hash':digest(value),
            'retained_query_ids':list(groups),'new_primary_urls':list(plan.priority_primary_urls),
            'new_search_calls':0,'retained_full_documents':True})


def _materialize(plan, rows, artifacts):
    """Verify every retained artifact, including unsuccessful source reads."""
    documents, gaps, seen = [], [], set()
    for result in rows:
        if result['query_definition'].get('exclusion_manifest_ref'):
            artifacts.verify_manifest(result['query_definition']['exclusion_manifest_ref'])
        metadata = None
        if result['snapshot_id']:
            metadata = artifacts.verify_manifest(result['manifest_ref'])['metadata']
            if (metadata != result['metadata'] or metadata['snapshot_id'] != result['snapshot_id']
                    or metadata['source_id'] != result['source_id'] or metadata['fetch_status'] != result['snapshot_state']
                    or metadata.get('body_ref') != result['text_ref']):
                raise Conflict('Stored source snapshot integrity mismatch')
            # Verify referenced originals/parser outputs even if a damaged
            # manifest entry list omitted them. Failed reads may have no body.
            for ref in (metadata.get('original_ref'), metadata.get('parser_ref'), result['text_ref']):
                if ref:
                    artifacts.get(ref)
        if result['id'] is None:
            if result['query_definition'].get('kind') != 'legal_link_urls':
                gaps.append(ResearchGap(code='empty_search', query_id=result['query_identity']))
        elif result['snapshot_id'] and result['text_ref']:
            if result['snapshot_id'] in seen:
                continue
            seen.add(result['snapshot_id'])
            if metadata['policy_status'] != 'validated' or result['snapshot_state'] != 'succeeded':
                raise Conflict('Stored source snapshot integrity mismatch')
            text = artifacts.get(result['text_ref']).decode('utf-8')
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            if metadata['content_hash'] != content_hash:
                raise Conflict('Stored source body integrity mismatch')
            documents.append(Document(snapshot_id=result['snapshot_id'], evidence_id=result['document_handle'],
                                      url=metadata['final_url'], text=text, content_hash=content_hash, policy_status='validated'))
        else:
            gaps.append(ResearchGap(code=result['reason'] or 'source_unavailable', query_id=result['query_identity'],
                                    result_id=result['id'], url=result['url'], snapshot_id=result['snapshot_id']))
    extra = {}
    if plan.schema_version in {'3','4'}:
        legal_rows=[{**row,'query_identity':row['query_definition'].get('retained_query_id',row['query_identity'])}
                    for row in rows] if plan.schema_version=='4' else rows
        extra['legal_source_reviews'] = materialize_legal_reviews(plan.legal_analogues, legal_rows)
        if plan.schema_version=='4':
            for review in extra['legal_source_reviews']:
                review['retained_acquisition_run_id']=plan.recovery_from_run_id
    scope = ScopePacket(**plan.seed.model_dump(), documents=documents, **extra)
    return PreparedScope(scope, tuple(gaps))


def _require_recovery_document(plan, rows):
    if plan.schema_version=='4' and not any(
            not r['query_definition'].get('retained_from_run_id') and r.get('snapshot_state')=='succeeded'
            and r.get('disposition') in {'read','duplicate'} and r.get('text_ref') for r in rows):
        raise Conflict('Recovery sources yielded no readable new official document; no model call is allowed')


async def prepare_scope(ledger, plan: SourceResearchPlan, search_factory, fetch_document,
                        artifacts: ArtifactStore, *, parser=parse_document) -> PreparedScope:
    """Acquire one model's finite source packet before any business graph node.

    The caller owns the run lease/heartbeat and overall active-time timeout.
    search_factory(key) returns a TavilyAdapter using this ledger and key.
    fetch_document(url) uses plan.fetch_limits(); it performs no implicit retry.
    """
    if plan.schema_version == '4':
        await _initialize_recovery_sources(ledger,plan,artifacts)
    elif plan.schema_version == '3':
        await _initialize_legal_sources(ledger, plan, artifacts)
    elif plan.derived_from_run_id is not None:
        await _initialize_retained_sources(ledger, plan, artifacts)
    elif plan.supplemented_from_run_id is not None:
        await _initialize_supplemented_sources(ledger, plan, artifacts)
    saved = await _initialize(ledger, plan)
    if saved:
        rows=await _results(ledger, plan.seed.scope_id)
        verified = _materialize(plan, rows, artifacts)
        if verified != saved:
            raise Conflict('Frozen scope differs from retained source receipts')
        _require_recovery_document(plan,rows)
        return saved
    await _search(ledger, plan, search_factory)
    for result in await _results(ledger, plan.seed.scope_id):
        if plan.schema_version == '3' and not result['query_definition'].get('kind', '').startswith('legal_'):
            continue
        if result['id'] is not None and result['disposition'] == 'pending' and result['reason'] is None:
            await _read(ledger, plan, result, fetch_document, artifacts, parser)
    if plan.schema_version == '3':
        for depth in range(1, plan.legal_link_rounds + 1):
            await _discover_legal_sources(ledger, plan, artifacts, depth)
            for result in await _results(ledger, plan.seed.scope_id):
                if not result['query_definition'].get('kind', '').startswith('legal_'):
                    continue
                if result['id'] is not None and result['disposition'] == 'pending' and result['reason'] is None:
                    await _read(ledger, plan, result, fetch_document, artifacts, parser)
        # Supplementary business-source queries cannot consume the legal review
        # allocation before its explicitly selected work has been attempted.
        for result in await _results(ledger, plan.seed.scope_id):
            if result['id'] is not None and result['disposition'] == 'pending' and result['reason'] is None:
                await _read(ledger, plan, result, fetch_document, artifacts, parser)
    rows=await _results(ledger, plan.seed.scope_id)
    prepared = _materialize(plan, rows, artifacts)
    _require_recovery_document(plan,rows)
    if plan.schema_version in {'3','4'} and any(review['execution_state'] != 'completed'
                                         for review in prepared.scope.legal_source_reviews):
        async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, __):
            await _pause(ledger, conn, 'legal_source_work_incomplete',
                         analogue_ids=[review['analogue_id'] for review in prepared.scope.legal_source_reviews
                                       if review['execution_state'] != 'completed'])
        raise Conflict('Legal acquisition has pending or failed work; it cannot become a completed documentary gap')
    scope, gaps = prepared.scope, prepared.gaps
    packet, gap_values = scope.model_dump(mode='json'), [g.model_dump(mode='json') for g in gaps]
    blocked = False
    async with ledger.locked(ledger.run_id, fenced=True) as (conn, _, run):
        if run['status'] not in {'selecting', 'queued', 'running'}:
            raise Conflict('Run paused before source packet was frozen')
        blocked = await ledger.active_ms(conn, run) >= run['config_json']['limits']['active_seconds'] * 1000
        if blocked:
            await _pause(ledger, conn, 'source_budget_paused', scope_id=plan.seed.scope_id)
        else:
            current = await (await conn.execute('SELECT * FROM app.source_research WHERE run_id=%s AND scope_id=%s',
                                                (ledger.run_id, plan.seed.scope_id))).fetchone()
            packet_hash = digest({'scope': packet, 'gaps': gap_values})
            if current['state'] == 'frozen' and current['packet_hash'] != packet_hash:
                raise Conflict('Frozen source scope cannot change')
            if current['state'] != 'frozen':
                await conn.execute("""UPDATE app.source_research SET state='frozen',packet=%s,packet_hash=%s,gaps=%s,frozen_at=clock_timestamp()
                    WHERE run_id=%s AND scope_id=%s""", (Jsonb(packet), packet_hash, Jsonb(gap_values), ledger.run_id, plan.seed.scope_id))
                await ledger.event(conn, ledger.run_id, 'source_scope_frozen', {'scope_id': plan.seed.scope_id,
                                                                            'document_count': len(scope.documents), 'gap_count': len(gaps), 'packet_hash': packet_hash})
    if blocked:
        raise BudgetExceeded('Active time exhausted before source scope freeze')
    return PreparedScope(scope, tuple(gaps))
