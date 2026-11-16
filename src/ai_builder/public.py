"""Allowlisted public projections. Private graph states never reach a route."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

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
                 'confirmed': 'Confirmed', 'conditional': 'Conditional',
                 'blocked': 'Blocked', 'unresolved': 'Unresolved',
                 'technical_failed': 'Research interrupted', 'incomplete': 'Incomplete'}
GOALS = {'infeasible': 'Outside the launch scenario', 'uncertain': 'Launch feasibility uncertain',
         'feasible': 'Fits the launch scenario'}
TERMINAL = {'pass', 'fail', 'needs_review', 'technical_failed', 'incomplete',
            'confirmed', 'conditional', 'blocked', 'unresolved'}
CONDITIONAL_CRITERIA = {'F01', 'F03', 'F05', 'F06', 'F07'}
STAGES = {'research': 'Criteria research', 'applicability': 'Independent applicability check',
          'legal_review': 'Legal document review', 'critic': 'Critic', 'neutrality': 'Neutral handoff',
          'verifier': 'Independent verifier', 'reconciliation': 'Risk reconciliation',
          'factors': 'Factor assessment', 'factor_validation': 'Factor premise verification',
          'card_validation': 'Publication checks', 'scoring': 'Scoring'}
LEGAL_METADATA_FIELDS = ('version', 'effective_date', 'supplier_legal_entity')


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
    verification_text_source: Literal['factor_handoff'] | None = None
    author_text: str | None = None

    @model_serializer(mode='wrap')
    def preserve_legacy_passport(self, handler):
        value = handler(self)
        for field in ('verification_text_source', 'author_text'):
            if getattr(self, field) is None:
                value.pop(field, None)
        return value


class DesignBasis(PublicContract):
    kind: Literal['proposed_design']
    excerpt: str = Field(max_length=320)


class Criterion(PublicContract):
    id: str
    name: str
    result: str
    reason: str
    source_ids: list[str] = Field(default_factory=list)
    confidence: Literal['low', 'medium', 'high'] | None = None
    confidence_reason: str | None = None
    method: Literal['model_judgment'] = 'model_judgment'
    design_basis: list[DesignBasis] = Field(default_factory=list)


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
    status: Literal['final', 'conditional', 'provisional']
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


class EligibilityCondition(PublicContract):
    condition_id: str
    criterion_id: str
    assumption: str
    required_check: str
    impact_on_factors: str
    source_ids: list[str] = Field(default_factory=list)


class LegalMetadataRef(SourceRef):
    # Metadata may name a contracting entity longer than a normal source chip;
    # preserve the complete validated quote instead of cutting its literal value.
    excerpt: str = Field(max_length=600)


class LegalDocument(PublicContract):
    url: str | None = None
    title: str | None = None
    document_type: str
    disposition: str
    source_id: str | None = None
    effective_date: str | None = None
    version: str | None = None
    supplier_legal_entity: str | None = None
    metadata_evidence: dict[Literal['version', 'effective_date', 'supplier_legal_entity'], LegalMetadataRef] = Field(default_factory=dict)
    metadata_unknown_reasons: dict[Literal['version', 'effective_date', 'supplier_legal_entity'], str] = Field(default_factory=dict)
    applicability_finding: str | None = None
    applicability_evidence: SourceRef | None = None
    applicability_unknown_reason: str | None = None
    reason: str | None = None

    @field_validator('url')
    @classmethod
    def checked_url(cls, value):
        return safe_url(value)

    @model_validator(mode='after')
    def metadata_sources(self):
        for field in LEGAL_METADATA_FIELDS:
            value, ref = getattr(self, field), self.metadata_evidence.get(field)
            if value is not None:
                if not ref or not self.source_id or ref.source_id != self.source_id or not ref.excerpt or not ref.locator:
                    raise ValueError('Known legal document metadata requires evidence from that document')
                if value not in ref.excerpt:
                    raise ValueError('Known metadata value must appear in its published quotation')
                if field in self.metadata_unknown_reasons:
                    raise ValueError('Known metadata cannot also have an unknown reason')
            elif ref:
                raise ValueError('Unknown legal metadata cannot claim supporting evidence')
        if self.applicability_finding is not None:
            ref = self.applicability_evidence
            if not ref or not self.source_id or ref.source_id != self.source_id or not ref.excerpt or not ref.locator:
                raise ValueError('Document applicability requires evidence from that document')
            if self.applicability_unknown_reason:
                raise ValueError('Assessed applicability cannot also be unknown')
        elif self.applicability_evidence:
            raise ValueError('Unknown applicability cannot claim supporting evidence')
        return self


class LegalClause(SourceRef):
    finding: str


class LegalReview(PublicContract):
    analogue_id: str
    analogue_name: str | None = None
    execution_state: Literal['pending', 'running', 'completed', 'technical_failed']
    coverage: Literal['reviewed', 'insufficient', 'not_found', 'unavailable'] | None = None
    supplier_role: str | None = None
    our_role_applicability: str | None = None
    documents: list[LegalDocument] = Field(default_factory=list)
    clauses: list[LegalClause] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    search_actions: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def completed_coverage(self):
        if (self.execution_state == 'completed') != (self.coverage is not None):
            raise ValueError('Legal coverage belongs only to a completed review')
        return self


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
    status: Literal['queued', 'running', 'pass', 'fail', 'needs_review', 'technical_failed', 'incomplete',
                    'confirmed', 'conditional', 'blocked', 'unresolved']
    scoring_policy_version: Literal['1.5.2'] | None = None
    eligibility_conditions: list[EligibilityCondition] = Field(default_factory=list)
    legal_reviews: list[LegalReview] = Field(default_factory=list)
    completed_stages: list[str] = Field(default_factory=list)
    queue_position: int | None = Field(default=None, ge=1, le=20)
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

    @model_serializer(mode='wrap')
    def policy_fields(self, handler):
        value = handler(self)
        if self.scoring_policy_version is None:
            for key in ('scoring_policy_version', 'eligibility_conditions', 'legal_reviews', 'completed_stages', 'queue_position'):
                value.pop(key, None)
        return value

    @model_validator(mode='after')
    def rank_gate(self):
        if self.status in {'confirmed', 'conditional', 'blocked', 'unresolved'} and not self.scoring_policy_version:
            raise ValueError('Versioned eligibility requires its scoring policy')
        if not self.scoring_policy_version:
            if self.eligible_for_ranking and (self.status != 'pass' or not self.score or self.score.status != 'final'):
                raise ValueError('Only a final eligible assessment may be ranked')
            if self.eligible_for_ranking and (len(self.criteria) != 10 or {c.id for c in self.criteria} != set(CRITERIA)
                                             or any(c.result != 'pass' for c in self.criteria)):
                raise ValueError('Every mandatory criterion must pass before ranking')
            if self.score and self.score.status == 'conditional':
                raise ValueError('Conditional scores require a versioned scoring policy')
        elif self.eligible_for_ranking:
            expected = {'confirmed': 'final', 'conditional': 'conditional'}.get(self.status)
            if not self.score or self.score.status != expected:
                raise ValueError('Ranked score must match its eligibility category')
            if len(self.criteria) != 10 or {c.id for c in self.criteria} != set(CRITERIA):
                raise ValueError('All criteria must be assessed before ranking')
            unknowns = {c.id for c in self.criteria if c.result == 'unknown'}
            if any(c.result not in {'pass', 'unknown'} for c in self.criteria) or not unknowns <= CONDITIONAL_CRITERIA:
                raise ValueError('Required criteria must pass before ranking')
            if (self.status == 'confirmed' and unknowns) or (self.status == 'conditional' and not unknowns):
                raise ValueError('Eligibility must honestly preserve criterion unknowns')
            if not unknowns <= {c.criterion_id for c in self.eligibility_conditions if c.assumption and c.required_check}:
                raise ValueError('Conditional criteria require explicit launch conditions')
            if not self.legal_reviews or any(r.execution_state != 'completed' for r in self.legal_reviews):
                raise ValueError('Ranking requires a completed legal document review for each analogue')
        if self.rank is not None and not self.eligible_for_ranking:
            raise ValueError('Unfinished assessment cannot have a rank')
        if self.scoring_policy_version and self.status in {'queued', 'running', 'technical_failed', 'blocked', 'unresolved'} and self.score:
            raise ValueError('Unfinished or excluded reassessments cannot reuse an old score')
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
    parent_collection_id: str | None = Field(default=None, pattern=IDENTIFIER)
    parent_run_id: str | None = Field(default=None, pattern=IDENTIFIER)
    parent_model_id: str | None = Field(default=None, pattern=IDENTIFIER)
    launch_conditions: list[str] = Field(default_factory=list, max_length=30)
    scoring_policy_version: Literal['1.5.2'] | None = None
    reassessment_of_collection_id: str | None = Field(default=None, pattern=IDENTIFIER)
    processing_order: list[str] = Field(default_factory=list, max_length=20)

    @model_serializer(mode='wrap')
    def policy_fields(self, handler):
        value = handler(self)
        if self.scoring_policy_version is None:
            for key in ('scoring_policy_version', 'reassessment_of_collection_id', 'processing_order'):
                value.pop(key, None)
        return value

    @model_validator(mode='after')
    def counts(self):
        if self.run_id in {'latest', 'featured'}:
            raise ValueError('Reserved public snapshot identity')
        parents = (self.parent_collection_id, self.parent_run_id, self.parent_model_id)
        if any(value is not None for value in parents):
            if not all(parents) or self.expected_count != 1 or len(self.models) != 1:
                raise ValueError('A focused analysis requires one model and complete parent references')
            if self.parent_collection_id == self.run_id:
                raise ValueError('A focused analysis cannot be its own parent')
        if len({m.id for m in self.models}) != len(self.models):
            raise ValueError('Duplicate public model identity')
        if any(m.scoring_policy_version != self.scoring_policy_version for m in self.models):
            raise ValueError('A collection cannot mix scoring policies')
        if self.reassessment_of_collection_id and (not self.scoring_policy_version or self.reassessment_of_collection_id == self.run_id):
            raise ValueError('Reassessment requires a new collection and scoring policy')
        if self.processing_order and (len(set(self.processing_order)) != len(self.models)
                                      or set(self.processing_order) != {m.id for m in self.models}):
            raise ValueError('Processing order must cover every selected model once')
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


def _legal_reviews(reviews, known_sources, source_metadata):
    """Only public source manifests and short reviewed fragments leave the graph."""
    output = []
    for review in reviews:
        documents = []
        for document in review.get('documents', []):
            source_id = document.get('snapshot_id') or document.get('source_snapshot_id')
            details = source_metadata.get(source_id) or {}
            metadata_refs = {}
            for field in LEGAL_METADATA_FIELDS:
                ref = (document.get('metadata_evidence') or {}).get(field) or {}
                if ref.get('source_snapshot_id') in known_sources:
                    metadata_refs[field] = dict(source_id=ref['source_snapshot_id'], excerpt=ref.get('excerpt') or '',
                                                locator=ref.get('locator'))
            applicability_ref = document.get('applicability_evidence') or {}
            public_applicability_ref = (dict(source_id=applicability_ref['source_snapshot_id'],
                excerpt=_clip(applicability_ref.get('excerpt')), locator=applicability_ref.get('locator'))
                if applicability_ref.get('source_snapshot_id') in known_sources else None)
            documents.append(dict(url=safe_url(document.get('url')),
                title=document.get('title') or details.get('title'),
                document_type=document.get('document_type') or 'other_legal',
                disposition=document.get('disposition') or ('read' if source_id in known_sources else 'inaccessible'),
                source_id=source_id if source_id in known_sources else None,
                effective_date=document.get('effective_date'), version=document.get('version'),
                supplier_legal_entity=document.get('supplier_legal_entity'), metadata_evidence=metadata_refs,
                applicability_finding=document.get('applicability_finding'),
                applicability_evidence=public_applicability_ref,
                applicability_unknown_reason=document.get('applicability_unknown_reason'),
                metadata_unknown_reasons={key: str(value) for key, value in (document.get('metadata_unknown_reasons') or {}).items()
                                          if key in LEGAL_METADATA_FIELDS and value}, reason=document.get('reason')))
        actions = []
        for action in review.get('search_actions', []):
            if isinstance(action, str):
                actions.append(_clip(action))
            elif isinstance(action, dict):
                parts = [str(action[key]) for key in ('action', 'query', 'url', 'outcome') if action.get(key)]
                if parts:
                    actions.append(_clip(' · '.join(parts)))
        execution = review.get('execution_state') or 'pending'
        gaps = []
        for gap in [*review.get('gaps', []), *review.get('analysis_gaps', [])]:
            if isinstance(gap, str):
                gaps.append(gap)
            elif isinstance(gap, dict):
                # Acquisition gaps carry structured reason codes, not free-form
                # public payloads; private artifact/provider details stay private.
                parts = [str(gap[key]) for key in ('code', 'reason', 'message', 'url') if gap.get(key)]
                if type(gap.get('document_count')) is int and gap['document_count'] > 0:
                    parts.append(f"{gap['document_count']} documents outside this bounded review")
                if safe_url(gap.get('final_url')):
                    parts.append('Redirect destination: ' + gap['final_url'])
                if parts:
                    gaps.append(' · '.join(parts))
        output.append(dict(analogue_id=review['analogue_id'],
            analogue_name=review.get('company_name') or review.get('product_name'),
            execution_state=execution, coverage=review.get('coverage') if execution == 'completed' else None,
            supplier_role=review.get('supplier_role'), our_role_applicability=review.get('our_role_applicability'),
            documents=documents,
            clauses=[dict(source_id=clause['source_snapshot_id'], excerpt=_clip(clause.get('excerpt')),
                          locator=clause.get('locator'), finding=clause.get('finding') or '')
                     for clause in review.get('clauses', []) if clause.get('source_snapshot_id') in known_sources],
            gaps=list(dict.fromkeys(gaps)), search_actions=actions))
    return output


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
    factor_ids = {claim['claim_id'] for claim in factors.get('claims', [])}
    factor_verified_ids = {item['claim_id'] for item in (state.get('factor_verification') or {}).get('items', [])
                           if item.get('status')}
    factor_handoff = {claim['claim_id']: claim for claim in state.get('factor_handoff', [])
                      if claim['claim_id'] in factor_ids and claim['claim_id'] in factor_verified_ids}
    claims = {}
    for claim in [*critique.get('claims', []), *factors.get('claims', [])]:
        identifier = claim['claim_id']
        verified_input = factor_handoff.get(identifier)
        text = verified_input['neutral_text'] if verified_input else claim['neutral_text']
        refs = []
        for ref in claim.get('evidence_refs', []):
            if ref.get('source_snapshot_id') in known_sources:
                refs.append(dict(source_id=ref['source_snapshot_id'], excerpt=_clip(ref.get('excerpt')),
                                 locator=ref.get('locator')))
        claims[identifier] = dict(id=identifier, text=text, kind=claim['kind'],
            **{key: claim.get(key) for key in ('method', 'confidence', 'confidence_reason', 'entity', 'geography', 'period', 'metric', 'formula')},
            input_claim_ids=claim.get('input_claim_ids') or [], assumptions=claim.get('assumptions') or [],
            verification=verification.get(identifier),
            checked_at=card.get('validated_at') if identifier in verification else None, source_refs=refs,
            verification_text_source='factor_handoff' if verified_input else None,
            author_text=claim['neutral_text'] if verified_input and text != claim['neutral_text'] else None)
    status = card.get('eligibility') or metadata.get('status', 'queued')
    if status not in STATUS_LABELS:
        status = 'incomplete'
    raw_score = card.get('score') or {}
    policy = card.get('scoring_policy_version') or metadata.get('scoring_policy_version') or getattr(config, 'scoring_policy_version', None)
    expected_score = ({'confirmed': 'final', 'conditional': 'conditional'}.get(status) if policy else
                      ('final' if status == 'pass' else None))
    eligible = (expected_score is not None and card.get('publication') == 'ready'
                and not card.get('publication_blockers') and raw_score.get('status') == expected_score)
    score = None
    if raw_score.get('value') is not None and raw_score.get('bounds'):
        score = dict(status=expected_score if eligible else 'provisional', value=raw_score['value'],
                     low=raw_score['bounds']['low'], high=raw_score['bounds']['high'],
                     goal_feasibility=raw_score['goal_feasibility'])
    criteria = [dict(id=criterion['criterion_id'], name=CRITERIA.get(criterion['criterion_id'], criterion['criterion_id']),
                     result=criterion.get('result') or criterion.get('state', 'pending'), reason=criterion['reason'],
                     source_ids=[evidence_map[eid] for eid in criterion.get('evidence_ids', []) if eid in evidence_map],
                     design_basis=[dict(kind=basis['kind'], excerpt=_clip(basis['excerpt']))
                                   for basis in criterion.get('design_basis', [])],
                     confidence=criterion.get('confidence'), confidence_reason=criterion.get('confidence_reason'))
                for criterion in state.get('criteria', [])]
    if policy:
        for criterion in criteria:
            if criterion['id'] == 'F09':
                criterion['name'] = 'Not a government-operated service'
    limitations = [*critique.get('limitations', []), *factors.get('limitations', [])]
    if state.get('source_gaps'):
        limitations.append(f"{len(state['source_gaps'])} source leads could not be fully assessed. Missing evidence remains unknown.")
    if status in {'pass', 'confirmed', 'conditional'} and not eligible:
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
    if policy:
        value.update(scoring_policy_version=policy, queue_position=metadata.get('queue_position'),
            eligibility_conditions=[dict(condition_id=condition['condition_id'], criterion_id=condition['criterion_id'],
                assumption=condition['assumption'], required_check=condition['required_check'],
                impact_on_factors=condition['impact_on_factors'],
                source_ids=[evidence_map[eid] for eid in condition.get('evidence_ids', []) if eid in evidence_map])
                for condition in card.get('eligibility_conditions', [])],
            legal_reviews=_legal_reviews(card.get('legal_reviews', []), known_sources, source_metadata),
            completed_stages=[stage for stage in card.get('completed_stages', []) if stage in STAGES])
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


def publish_snapshot(data_dir, snapshot: dict, *, set_latest: bool = True) -> Path:
    public = PublicSnapshot.model_validate(snapshot)
    if public.parent_collection_id and set_latest:
        raise ValueError('A focused analysis cannot replace the main collection')
    for model in public.models:
        model.rank = None
    for category, goal in ((category, goal) for category in ('final', 'conditional') for goal in GOALS):
        ranked = sorted((m for m in public.models if m.eligible_for_ranking and m.score.status == category
                         and m.score.goal_feasibility == goal),
                        key=lambda m: (m.score.value, m.title.casefold(), m.id))
        previous, place = None, 0
        for index, model in enumerate(ranked, 1):
            if model.score.value != previous:
                place = index
            model.rank = place
            previous = model.score.value
    path = Path(data_dir) / 'public' / f'{public.run_id}.json'
    if path.exists():
        previous = load_snapshot(data_dir, public.run_id)
        if previous is None:
            raise ValueError('Cannot overwrite an invalid public snapshot')
        history = path.parent / 'history' / public.run_id / f"{previous['revision_id']}.json"
        if history.exists():
            if history.is_symlink() or json.loads(history.read_text()) != previous:
                raise ValueError('Public revision history cannot be replaced')
        else:
            _atomic_json(history, previous)
    _atomic_json(path, public.model_dump(mode='json'))
    if set_latest:
        _atomic_json(path.parent / 'latest.json', {'run_id': public.run_id})
    return path


def load_snapshot(data_dir, run_id=None) -> dict | None:
    directory = Path(data_dir) / 'public'
    try:
        if run_id is None:
            run_id = json.loads((directory / 'latest.json').read_text())['run_id']
        if not isinstance(run_id, str) or not re.fullmatch(IDENTIFIER, run_id) or run_id in {'latest', 'featured'}:
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


def _valid_followup(data_dir, run_id) -> dict | None:
    snapshot = load_snapshot(data_dir, run_id)
    if not snapshot or not snapshot['parent_collection_id']:
        return None
    parent = load_snapshot(data_dir, snapshot['parent_collection_id'])
    if not parent or parent['parent_collection_id']:
        return None
    parent_model = next((model for model in parent['models'] if model['id'] == snapshot['parent_model_id']), None)
    if not parent_model or parent_model['base_model_id'] != snapshot['models'][0]['base_model_id']:
        return None
    return snapshot


def publish_featured_followup(data_dir, run_id: str) -> Path:
    """Point to a saved, scoped analysis without replacing the main collection."""
    if _valid_followup(data_dir, run_id) is None:
        raise ValueError('Focused analysis must reference a saved parent model with the same base identity')
    path = Path(data_dir) / 'public' / 'featured.json'
    _atomic_json(path, {'run_id': run_id})
    return path


def load_featured_followup(data_dir) -> dict | None:
    path = Path(data_dir) / 'public' / 'featured.json'
    try:
        if path.is_symlink() or path.stat().st_size > 1024:
            return None
        pointer = json.loads(path.read_text())
        if set(pointer) != {'run_id'}:
            return None
        return _valid_followup(data_dir, pointer['run_id'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
