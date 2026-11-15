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
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from psycopg.types.json import Jsonb
from pydantic import ConfigDict, Field, field_validator

from .domain import Contract
from .evidence import ArtifactStore
from .fetch_transport import FetchError, FetchLimits
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


class SourceResearchPlan(FrozenContract):
    schema_version: Literal['1'] = '1'
    seed: ScopeSeed
    queries: tuple[SearchQueryPlan, ...] = Field(min_length=1, max_length=4)
    max_new_reads: int = Field(default=6, ge=1, le=6, strict=True)
    acquisition_limits: FetchLimits = Field(default_factory=FetchLimits)
    parser_limits: ParserLimits = Field(default_factory=ParserLimits)

    def fetch_limits(self):
        return self.acquisition_limits

    def parse_limits(self):
        return self.parser_limits


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
            for index, query in enumerate(plan.queries):
                operation_key = 'source-search:' + digest({'scope': plan.seed.scope_id, 'index': index})
                await conn.execute('''INSERT INTO app.source_queries(id,run_id,scope_id,query_index,operation_key,query)
                                      VALUES (%s,%s,%s,%s,%s,%s)''',
                                   (uid(), ledger.run_id, plan.seed.scope_id, index, operation_key,
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
        query = SearchQueryPlan.model_validate(row['query'])
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
        return await (await conn.execute('''SELECT r.*,q.id AS query_identity,q.query_index,s.document_handle,s.text_ref,s.manifest_ref,s.metadata,
            s.state AS snapshot_state,s.source_id
            FROM app.source_queries q LEFT JOIN app.source_results r ON r.query_id=q.id
            LEFT JOIN app.source_snapshots s ON s.id=r.snapshot_id
            WHERE q.run_id=%s AND q.scope_id=%s ORDER BY q.query_index,r.result_rank''', (ledger.run_id, scope_id))).fetchall()


def _materialize(plan, rows, artifacts):
    """Verify every retained artifact, including unsuccessful source reads."""
    documents, gaps, seen = [], [], set()
    for result in rows:
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
    scope = ScopePacket(**plan.seed.model_dump(), documents=documents)
    return PreparedScope(scope, tuple(gaps))


async def prepare_scope(ledger, plan: SourceResearchPlan, search_factory, fetch_document,
                        artifacts: ArtifactStore, *, parser=parse_document) -> PreparedScope:
    """Acquire one model's finite source packet before any business graph node.

    The caller owns the run lease/heartbeat and overall active-time timeout.
    search_factory(key) returns a TavilyAdapter using this ledger and key.
    fetch_document(url) uses plan.fetch_limits(); it performs no implicit retry.
    """
    saved = await _initialize(ledger, plan)
    if saved:
        verified = _materialize(plan, await _results(ledger, plan.seed.scope_id), artifacts)
        if verified != saved:
            raise Conflict('Frozen scope differs from retained source receipts')
        return saved
    await _search(ledger, plan, search_factory)
    for result in await _results(ledger, plan.seed.scope_id):
        if result['id'] is not None and result['disposition'] == 'pending' and result['reason'] is None:
            await _read(ledger, plan, result, fetch_document, artifacts, parser)
    prepared = _materialize(plan, await _results(ledger, plan.seed.scope_id), artifacts)
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
