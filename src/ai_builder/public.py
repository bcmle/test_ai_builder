"""Allowlisted public projections. Private graph states never reach a route."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENTIFIER = r'^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$'
CRITERIA = {
    'F01': 'Lawful activity', 'F02': 'Delivered entirely online', 'F03': 'No own special licence',
    'F04': 'Recurring revenue', 'F05': 'No credit risk', 'F06': 'No insurance risk',
    'F07': 'No custody of client funds', 'F08': 'Not a trading business',
    'F09': 'Independent of government services', 'F10': 'A comparable valued at $300M+',
}
FACTORS = {'S01': 'Access to customers', 'S02': 'Evidence of willingness to pay',
           'S03': 'Time to launch a test', 'S04': 'Time to first payment',
           'S05': 'Fit with available resources'}
STATUS_LABELS = {'queued': 'Queued', 'running': 'Researching', 'pass': 'Passed filters',
                 'fail': 'Filtered out', 'needs_review': 'Needs review',
                 'technical_failed': 'Research interrupted', 'incomplete': 'Incomplete'}
GOALS = {'infeasible': 'Outside the launch scenario', 'uncertain': 'Launch feasibility uncertain',
         'feasible': 'Fits the launch scenario'}
TERMINAL = {'pass', 'fail', 'needs_review', 'technical_failed', 'incomplete'}


def safe_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value) or '\\' in value:
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.hostname.lower() == 'localhost'
                or parts.hostname.lower().endswith(('.localhost', '.internal', '.local'))):
            return None
        return value
    except ValueError:
        return None


class PublicContract(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Source(PublicContract):
    id: str
    url: str | None = None
    title: str | None = None
    publisher: str | None = None
    published_at: str | None = None
    fetched_at: str | None = None

    @field_validator('url')
    @classmethod
    def checked_url(cls, value):
        return safe_url(value)


class DiscoveryLead(PublicContract):
    url: str
    title: str | None = None
    query: str | None = None

    @field_validator('url')
    @classmethod
    def checked_url(cls, value):
        if not safe_url(value):
            raise ValueError('Invalid public lead URL')
        return value


class SourceRef(PublicContract):
    source_id: str
    excerpt: str = Field(max_length=320)
    locator: str | None = None


class ClaimPassport(PublicContract):
    id: str
    text: str
    kind: Literal['fact', 'calculation', 'judgment']
    method: str | None = None
    confidence: Literal['low', 'medium', 'high'] | None = None
    confidence_reason: str | None = None
    entity: str | None = None
    geography: str | None = None
    period: str | None = None
    metric: str | None = None
    formula: str | None = None
    input_claim_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    verification: str | None = None
    checked_at: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class Criterion(PublicContract):
    id: str
    name: str
    result: str
    reason: str
    source_ids: list[str] = Field(default_factory=list)
    confidence: Literal['low', 'medium', 'high'] | None = None
    confidence_reason: str | None = None
    method: Literal['model_judgment'] = 'model_judgment'


class Bounds(PublicContract):
    low: float
    high: float

    @model_validator(mode='after')
    def ordered(self):
        if self.low > self.high:
            raise ValueError('Inverted interval')
        return self


class Factor(PublicContract):
    id: str
    name: str
    value: Bounds | None = None
    claim_ids: list[str] = Field(default_factory=list)


class Score(PublicContract):
    status: Literal['final', 'provisional']
    value: float = Field(ge=0, le=100)
    low: float = Field(ge=0, le=100)
    high: float = Field(ge=0, le=100)
    goal_feasibility: Literal['infeasible', 'uncertain', 'feasible']

    @model_validator(mode='after')
    def score_interval(self):
        if not self.low <= self.value <= self.high:
            raise ValueError('Score must be within its interval')
        return self


class Risk(PublicContract):
    id: str
    text: str
    verification: str | None = None
    claim_ids: list[str] = Field(default_factory=list)


class Model(PublicContract):
    id: str = Field(pattern=IDENTIFIER)
    base_model_id: str
    title: str
    category: str
    summary: str
    buyer: str | None = None
    revenue: str | None = None
    comparable: str | None = None
    description_basis: Literal['proposed_launch'] = 'proposed_launch'
    status: Literal['queued', 'running', 'pass', 'fail', 'needs_review', 'technical_failed', 'incomplete']
    eligible_for_ranking: bool = False
    rank: int | None = None
    score: Score | None = None
    criteria: list[Criterion] = Field(default_factory=list)
    factors: list[Factor] = Field(default_factory=list)
    days: Bounds | None = None
    test_budget: Bounds | None = None
    test_plan_claim_ids: list[str] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    claims: list[ClaimPassport] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    discovery_sources: list[DiscoveryLead] = Field(default_factory=list)

    @model_validator(mode='after')
    def rank_gate(self):
        if self.eligible_for_ranking and (self.status != 'pass' or not self.score or self.score.status != 'final'):
            raise ValueError('Only a final eligible assessment may be ranked')
        if self.eligible_for_ranking and (len(self.criteria) != 10 or {c.id for c in self.criteria} != set(CRITERIA)
                                         or any(c.result != 'pass' for c in self.criteria)):
            raise ValueError('Every mandatory criterion must pass before ranking')
        if self.rank is not None and not self.eligible_for_ranking:
            raise ValueError('Unfinished assessment cannot have a rank')
        return self


class Scenario(PublicContract):
    team_size: int
    business_budget_usd: str | float | int
    days: int
    jurisdiction: str | None = None


class PublicSnapshot(PublicContract):
    schema_version: Literal['prototype-1'] = 'prototype-1'
    run_id: str = Field(pattern=IDENTIFIER)
    revision_id: str = Field(pattern=IDENTIFIER)
    title: str
    market: str
    created_at: str
    published_at: str
    status: Literal['running', 'partial', 'completed']
    stage: str | None = None
    expected_count: int = Field(default=20, ge=1, le=20)
    selected_count: int = Field(default=0, ge=0, le=20)
    completed_count: int = Field(default=0, ge=0, le=20)
    scenario: Scenario
    models: list[Model] = Field(max_length=20)

    @model_validator(mode='after')
    def counts(self):
        if len({m.id for m in self.models}) != len(self.models):
            raise ValueError('Duplicate public model identity')
        if len(self.models) > self.expected_count:
            raise ValueError('Collection exceeds its declared size')
        if len(self.models) < self.expected_count and self.status != 'partial':
            raise ValueError('A smaller collection must remain explicitly partial')
        self.selected_count = len(self.models)
        self.completed_count = sum(m.status in TERMINAL for m in self.models)
        return self


def _clip(value, limit=320):
    value = str(value or '')
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def public_model(result, metadata: dict, config) -> dict:
    """Project a ResearchRunResult (or pending candidate) without private payloads."""
    state = result.state if result is not None else {}
    scope = result.prepared.scope if result is not None else None
    card = state.get('card') or {}
    source_metadata = state.get('source_metadata') or {}
    sources, evidence_map = [], {}
    for document in scope.documents if scope is not None else []:
        details = source_metadata.get(document.snapshot_id) or {}
        sources.append(dict(id=document.snapshot_id, url=safe_url(document.url),
                            **{key: details.get(key) for key in ('title', 'publisher', 'published_at', 'fetched_at')}))
        evidence_map[document.evidence_id] = document.snapshot_id
    known_sources = {source['id'] for source in sources}
    critique, factors = state.get('critique') or {}, state.get('factors') or {}
    verification = {item['claim_id']: item.get('status') for field in ('verification', 'factor_verification')
                    for item in (state.get(field) or {}).get('items', [])}
    claims = {}
    for claim in [*critique.get('claims', []), *factors.get('claims', [])]:
        refs = []
        for ref in claim.get('evidence_refs', []):
            if ref.get('source_snapshot_id') in known_sources:
                refs.append(dict(source_id=ref['source_snapshot_id'], excerpt=_clip(ref.get('excerpt')),
                                 locator=ref.get('locator')))
        identifier = claim['claim_id']
        claims[identifier] = dict(id=identifier, text=claim['neutral_text'], kind=claim['kind'],
            **{key: claim.get(key) for key in ('method', 'confidence', 'confidence_reason', 'entity', 'geography', 'period', 'metric', 'formula')},
            input_claim_ids=claim.get('input_claim_ids') or [], assumptions=claim.get('assumptions') or [],
            verification=verification.get(identifier),
            checked_at=card.get('validated_at') if identifier in verification else None, source_refs=refs)
    status = card.get('eligibility') or metadata.get('status', 'queued')
    if status not in STATUS_LABELS:
        status = 'incomplete'
    raw_score = card.get('score') or {}
    eligible = (status == 'pass' and card.get('publication') == 'ready'
                and not card.get('publication_blockers') and raw_score.get('status') == 'final')
    score = None
    if raw_score.get('value') is not None and raw_score.get('bounds'):
        score = dict(status='final' if eligible else 'provisional', value=raw_score['value'],
                     low=raw_score['bounds']['low'], high=raw_score['bounds']['high'],
                     goal_feasibility=raw_score['goal_feasibility'])
    criteria = [dict(id=criterion['criterion_id'], name=CRITERIA.get(criterion['criterion_id'], criterion['criterion_id']),
                     result=criterion.get('result') or criterion.get('state', 'pending'), reason=criterion['reason'],
                     source_ids=[evidence_map[eid] for eid in criterion.get('evidence_ids', []) if eid in evidence_map],
                     confidence=criterion.get('confidence'), confidence_reason=criterion.get('confidence_reason'))
                for criterion in state.get('criteria', [])]
    limitations = [*critique.get('limitations', []), *factors.get('limitations', [])]
    if state.get('source_gaps'):
        limitations.append(f"{len(state['source_gaps'])} source leads could not be fully assessed. Missing evidence remains unknown.")
    if status == 'pass' and not eligible:
        limitations.append('Publication checks are incomplete. This model is excluded from the final ranking.')
    if metadata.get('error_note'):
        limitations.append(str(metadata['error_note']))
    discovery = [{key: lead.get(key) for key in ('url', 'title', 'query')}
                 for lead in metadata.get('discovery_sources', []) if safe_url(lead.get('url'))]
    value = dict(
        id=metadata['id'], base_model_id=metadata.get('base_model_id') or (scope.base_model_id if scope else metadata['id']),
        title=metadata['title'], category=metadata.get('category') or 'Business software',
        summary=metadata.get('summary') or metadata.get('description') or '',
        buyer=metadata.get('buyer'), revenue=metadata.get('revenue'), comparable=metadata.get('comparable'),
        status=status, eligible_for_ranking=eligible, rank=None, score=score, criteria=criteria,
        factors=[dict(id=factor['factor_id'], name=FACTORS.get(factor['factor_id'], factor['factor_id']),
                      value=factor.get('value'), claim_ids=factor.get('claim_ids') or []) for factor in factors.get('factors', [])],
        days=factors.get('days'), test_budget=factors.get('cost'), test_plan_claim_ids=factors.get('test_plan_claim_ids') or [],
        risks=[dict(id=risk['risk_id'],
                    text=risk.get('description') or risk.get('text') or ' '.join(claims[c]['text'] for c in risk.get('premise_claim_ids', []) if c in claims),
                    verification=(state.get('reconciliation') or {}).get(risk['risk_id']),
                    claim_ids=risk.get('premise_claim_ids') or []) for risk in critique.get('risks', [])],
        limitations=list(dict.fromkeys(limitations)), claims=list(claims.values()), sources=sources,
        discovery_sources=discovery,
    )
    return Model.model_validate(value).model_dump(mode='json')


def _atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.public-', mode='w', encoding='utf-8', delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o644)
            json.dump(value, output, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_snapshot(data_dir, snapshot: dict) -> Path:
    public = PublicSnapshot.model_validate(snapshot)
    for model in public.models:
        model.rank = None
    for goal in GOALS:
        ranked = sorted((m for m in public.models if m.eligible_for_ranking and m.score.goal_feasibility == goal),
                        key=lambda m: (m.score.value, m.title.casefold(), m.id))
        previous, place = None, 0
        for index, model in enumerate(ranked, 1):
            if model.score.value != previous:
                place = index
            model.rank = place
            previous = model.score.value
    path = Path(data_dir) / 'public' / f'{public.run_id}.json'
    _atomic_json(path, public.model_dump(mode='json'))
    _atomic_json(path.parent / 'latest.json', {'run_id': public.run_id})
    return path


def load_snapshot(data_dir, run_id=None) -> dict | None:
    directory = Path(data_dir) / 'public'
    try:
        if run_id is None:
            run_id = json.loads((directory / 'latest.json').read_text())['run_id']
        if not isinstance(run_id, str) or not re.fullmatch(IDENTIFIER, run_id) or run_id == 'latest':
            return None
        path = directory / f'{run_id}.json'
        if path.is_symlink() or path.stat().st_size > 10_000_000:
            return None
        snapshot = PublicSnapshot.model_validate_json(path.read_text())
        if snapshot.run_id != run_id:
            return None
        return snapshot.model_dump(mode='json')
    except (OSError, ValueError, KeyError, TypeError):
        return None
