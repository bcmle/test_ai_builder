"""Deterministic legal-document discovery metadata, never a legality verdict.

Search leads and contractual text do not prove regulatory compliance. This
module only identifies the analogue, records performed work and binds acquired
snapshots to it. Semantic applicability belongs to the research/review stages.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from pydantic import ConfigDict, Field, field_validator

from .domain import Contract

LEGAL_DOCUMENT_ORDER = ('terms', 'privacy', 'dpa', 'aup', 'regulatory_license', 'software_license', 'other_legal')


class LegalAnalogue(Contract):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, frozen=True)
    analogue_id: str = Field(min_length=1, max_length=200)
    company_name: str = Field(min_length=1, max_length=300)
    product_name: str = Field(min_length=1, max_length=300)
    official_urls: tuple[str, ...] = Field(min_length=1, max_length=4)

    @field_validator('analogue_id', 'company_name', 'product_name')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Analogue identity must be explicit')
        return value

    @field_validator('official_urls')
    @classmethod
    def valid_urls(cls, values):
        # Import at validation time to keep the pure metadata module independent
        # of the source/pipeline initialization order.
        from .source_research import canonical_url
        normalized = [canonical_url(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError('Official URLs must be distinct')
        return values


def analogue_id(company_name: str, product_name: str) -> str:
    """Stable company/product identity; separate from a business-model id."""
    identity = '\0'.join(' '.join(value.casefold().split()) for value in (company_name, product_name))
    return 'analogue-' + hashlib.sha256(identity.encode()).hexdigest()[:24]


def official_domains(analogue: LegalAnalogue) -> tuple[str, ...]:
    return tuple(sorted({(urlsplit(url).hostname or '').lower().removeprefix('www.')
                         for url in analogue.official_urls}))


def is_official_url(url: str, analogue: LegalAnalogue) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        return (parsed.scheme in {'http', 'https'} and parsed.username is None
                and parsed.password is None and parsed.port in {None, 80, 443}
                and any(host == domain or host.endswith('.' + domain) for domain in official_domains(analogue)))
    except ValueError:
        return False


def legal_search_plan(analogue: LegalAnalogue) -> dict:
    return {'query': f'{analogue.company_name} {analogue.product_name} terms conditions privacy data processing agreement acceptable use regulatory license',
            'max_results': 5, 'advanced': False, 'domains': list(official_domains(analogue))}


def document_type(url: str, title: str | None = None) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    value = ' '.join((parsed.path, parsed.query, title or '')).casefold()
    value = re.sub(r'[-_+/%]+', ' ', value)
    for kind, pattern in (
        ('dpa', r'\bdpa\b|data process(?:ing|or)|data protection (?:addendum|agreement)'),
        ('aup', r'\baup\b|acceptable use'),
        ('privacy', r'privacy'),
        ('regulatory_license', r'regulatory|licen[cs](?:e|ing|es).*regulat|regulat.*licen[cs]'),
        ('software_license', r'\beula\b|software licen[cs]|open source licen[cs]'),
        ('terms', r'\bterms\b|\btos\b|\btou\b|service agreement|customer agreement'),
        ('other_legal', r'\blegal\b|\blicen[cs](?:e|es|ing)\b|\bcompliance\b'),
    ):
        if re.search(pattern, value):
            return kind
    return None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.href, self.label = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href, self.label = dict(attrs).get('href'), []

    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href is not None:
            self.links.append((self.href, ' '.join(self.label)))
            self.href, self.label = None, []


def discover_legal_links(body: bytes, base_url: str, analogue: LegalAnalogue) -> list[dict]:
    """Inspect already acquired HTML; no network, executable content or guesses.

    Only official-domain links are acquired automatically. External linked
    regulators require an explicitly planned source, so identity is not inferred
    from arbitrary third-party links. No text excerpts replace full documents.
    """
    from .source_research import canonical_url
    parser = _Links()
    parser.feed(body.decode('utf-8', errors='replace'))
    found = {}
    for href, title in parser.links:
        try:
            url = canonical_url(urljoin(base_url, href))
        except ValueError:
            continue
        kind = document_type(url, title)
        if kind and is_official_url(url, analogue):
            found.setdefault(url, {'url': url, 'title': title, 'document_type': kind})
    order = {kind: index for index, kind in enumerate(LEGAL_DOCUMENT_ORDER)}
    return sorted(found.values(), key=lambda item: (order[item['document_type']], item['url']))


def select_legal_links(links, limit):
    """Prefer type coverage; record every unselected lead as a bounded omission."""
    order = {kind: index for index, kind in enumerate(LEGAL_DOCUMENT_ORDER)}
    ordered = sorted(links, key=lambda item: (order[item['document_type']], item['url']))
    selected, kinds = [], set()
    for item in ordered:
        if item['document_type'] not in kinds and len(selected) < limit:
            selected.append(item)
            kinds.add(item['document_type'])
    selected_urls = {item['url'] for item in selected}
    for item in ordered:
        if len(selected) < limit and item['url'] not in selected_urls:
            selected.append(item)
            selected_urls.add(item['url'])
    excluded = [{**item, 'disposition': 'excluded', 'reason': 'outside_bounded_review'}
                for item in ordered if item['url'] not in selected_urls]
    return selected, excluded


def materialize_legal_reviews(analogues, rows):
    """A missing document is a gap; pending work is never a completed review."""
    reviews = []
    for analogue in analogues:
        actions, documents, gaps, queries, seen_docs = [], [], [], set(), set()
        external_redirects = set()
        pending = failed = searched = False
        for row in rows:
            definition = row['query_definition']
            owned = definition.get('analogue_id') == analogue.analogue_id
            retained = definition.get('kind') == 'retained_reassessment_sources'
            if not owned and not (retained and row.get('url') and is_official_url(row['url'], analogue)):
                continue
            if definition.get('kind') == 'legal_link_urls' and row['query_identity'] not in queries:
                queries.add(row['query_identity'])
                actions.append({'action': 'link_discovery', 'query_id': row['query_identity'],
                    'outcome': 'completed', 'performed_at': _iso(row.get('query_completed_at')),
                    'source_snapshot_ids': definition.get('source_snapshot_ids', []),
                    'selected_count': len(definition.get('urls', [])),
                    'excluded_count': definition.get('excluded_count', 0)})
                if definition.get('excluded_count'):
                    gaps.append({'code': 'outside_bounded_review', 'document_count': definition['excluded_count'],
                                 'exclusion_manifest_ref': definition['exclusion_manifest_ref'],
                                 'reason': 'Discovered leads omitted by the frozen per-round link limit; no reading claimed.'})
            if definition.get('kind') == 'legal_search' and row['query_identity'] not in queries:
                queries.add(row['query_identity'])
                state = row.get('query_state', 'pending')
                searched = state == 'succeeded' or searched
                pending = pending or state == 'pending'
                failed = failed or state == 'failed'
                actions.append({'action': 'search', 'query_id': row['query_identity'],
                    'query': definition['search_plan']['query'], 'outcome': state,
                    'reason': row.get('query_error'), 'performed_at': _iso(row.get('query_completed_at'))})
            if row.get('id') is None:
                if owned and definition.get('kind') == 'legal_search':
                    gaps.append({'code': 'empty_legal_search', 'query_id': row['query_identity']})
                continue
            metadata = row.get('metadata') or {}
            disposition = row['disposition']
            final_url = metadata.get('final_url')
            external_redirect = (disposition in {'read', 'duplicate'} and final_url
                                 and is_official_url(row['url'], analogue)
                                 and not is_official_url(final_url, analogue))
            if external_redirect and (row.get('snapshot_id'), final_url) not in external_redirects:
                external_redirects.add((row.get('snapshot_id'), final_url))
                gaps.append({'code': 'external_redirect_attribution_unverified', 'url': row['url'],
                    'final_url': final_url, 'snapshot_id': row.get('snapshot_id'),
                    'reason': 'The declared official URL redirected to an external host. The redirect is retained as provenance; the final host publisher and applicability are not independently established.'})
            # Every intended read is represented. A cap or unfinished read must
            # not be relabelled "not found" to unlock conditional scoring.
            pending = pending or disposition == 'pending'
            failed = failed or row.get('reason') in {
                'parser_timeout', 'parser_resource_or_process_failure', 'parser_invalid_output'}
            if disposition != 'pending':
                actions.append({'action': 'reuse' if disposition == 'duplicate' else
                                'exclude' if disposition == 'excluded' else 'read',
                    'query_id': row['query_identity'], 'url': row['url'],
                    'final_url': metadata.get('final_url'), 'outcome': disposition,
                    'reason': row.get('reason'), 'snapshot_id': row.get('snapshot_id'),
                    'performed_at': _iso(row.get('query_completed_at')) if disposition in {'duplicate', 'excluded'}
                                    else metadata.get('fetched_at'),
                    'source_fetched_at': metadata.get('fetched_at')})
            kind = document_type(row['url'], row.get('title') or metadata.get('title'))
            if kind is None:
                if disposition == 'inaccessible':
                    gaps.append({'code': row.get('reason') or 'official_page_unavailable', 'url': row['url']})
                continue
            identity = row.get('snapshot_id') or row['url']
            if identity in seen_docs:
                continue
            seen_docs.add(identity)
            documents.append({'evidence_id': row.get('document_handle'), 'snapshot_id': row.get('snapshot_id'),
                'url': row['url'], 'final_url': metadata.get('final_url'), 'document_type': kind,
                'title': row.get('title') or metadata.get('title'), 'disposition': disposition,
                'reason': row.get('reason'), 'source_manifest_ref': row.get('manifest_ref'),
                'final_host_attribution': 'unverified_external_host' if external_redirect else
                    'declared_official_domain' if final_url and is_official_url(final_url, analogue) else 'not_established',
                'version': None, 'effective_date': None, 'unknown_date_reason': 'not_established_by_acquisition'})
            if disposition in {'inaccessible', 'pending', 'excluded'}:
                gaps.append({'code': row.get('reason') or 'document_not_read', 'url': row['url']})
        if not searched:
            pending = True
            gaps.append({'code': 'legal_search_not_completed'})
        read = [item for item in documents if item['disposition'] in {'read', 'duplicate'}]
        if not read:
            gaps.append({'code': 'no_readable_legal_document'})
        state = 'technical_failed' if failed else 'pending' if pending else 'completed'
        # Acquisition alone cannot claim a substantive completed legal analysis.
        coverage = None if state != 'completed' else ('insufficient' if read else
                    'unavailable' if any(action['outcome'] == 'inaccessible' for action in actions) else 'not_found')
        reviews.append({**analogue.model_dump(mode='json'), 'execution_state': state,
                        'coverage': coverage, 'search_actions': actions, 'documents': documents, 'gaps': gaps})
    return reviews


def _iso(value):
    return value.isoformat() if hasattr(value, 'isoformat') else value
