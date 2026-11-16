"""Strict analytical contracts; provider output cannot decide ranking or eligibility."""

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Interval(Contract):
    low: Decimal = Field(ge=0)
    high: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.low > self.high:
            raise ValueError("Interval lower bound exceeds upper bound")
        return self


class Factor(Interval):
    low: Decimal = Field(ge=0, le=4)
    high: Decimal = Field(ge=0, le=4)


class Criterion(Contract):
    criterion_id: str
    state: Literal["pending", "running", "completed", "technical_failed"]
    result: Literal["pass", "fail", "unknown"] | None
    evidence_ids: list[str]
    reason: str = Field(min_length=1)
    confidence: Literal['low', 'medium', 'high'] | None = None
    confidence_reason: str | None = None

    @model_validator(mode="after")
    def coherent(self):
        if (self.state == "completed") != (self.result is not None):
            raise ValueError("Only completed evaluation has a business result")
        if self.result in {"pass", "fail"} and not self.evidence_ids:
            raise ValueError("Pass and fail both require validated evidence")
        return self


class DesignBasis(Contract):
    """A quoted operating assumption, never a document/evidence identity."""
    kind: Literal['proposed_design']
    assessment_scope_id: str
    excerpt: str = Field(min_length=1, max_length=4000)


class CriterionV14(Criterion):
    design_basis: list[DesignBasis] = Field(default_factory=list, max_length=8)

    @model_validator(mode='after')
    def coherent(self):
        if (self.state == 'completed') != (self.result is not None):
            raise ValueError('Only completed evaluation has a business result')
        if self.result in {'pass', 'fail'} and not (self.evidence_ids or self.design_basis):
            raise ValueError('Pass and fail require external evidence or permitted design basis')
        return self


class Eligibility(StrEnum):
    FAIL = "fail"
    INCOMPLETE = "incomplete"
    NEEDS_REVIEW = "needs_review"
    PASS = "pass"


class ConditionalEligibility(StrEnum):
    CONFIRMED = 'confirmed'
    CONDITIONAL = 'conditional'
    BLOCKED = 'blocked'
    UNRESOLVED = 'unresolved'


CONDITIONAL_CRITERIA = frozenset({'F01', 'F03', 'F05', 'F06', 'F07'})


class EligibilityCondition(Contract):
    """An unresolved assumption for analysis, never a factual approval."""
    condition_id: str
    criterion_id: Literal['F01', 'F03', 'F05', 'F06', 'F07']
    assumption: str = Field(min_length=1, max_length=3000)
    required_check: str = Field(min_length=1, max_length=3000)
    impact_on_factors: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str]

    @model_validator(mode='after')
    def meaningful(self):
        if not all(value.strip() for value in
                   (self.condition_id, self.assumption, self.required_check, self.impact_on_factors)):
            raise ValueError('Eligibility conditions require nonblank assumptions, checks and effects')
        return self


def conditional_eligibility(criteria: list[Criterion], required: list[str], *,
                            unknown_kinds: dict[str, str | None],
                            conditions: list[EligibilityCondition],
                            legal_reviews_complete: bool) -> ConditionalEligibility:
    """Only independently classified evidence gaps may use the v1.5.2 exception."""
    legacy = eligibility(criteria, required)
    if legacy == Eligibility.FAIL:
        return ConditionalEligibility.BLOCKED
    if legacy == Eligibility.INCOMPLETE or not legal_reviews_complete:
        return ConditionalEligibility.UNRESOLVED
    unknown = {c.criterion_id for c in criteria if c.result == 'unknown'}
    by_criterion = {c.criterion_id: c for c in conditions}
    if len(by_criterion) != len(conditions):
        raise ValueError('Duplicate eligibility condition')
    if set(by_criterion) - unknown:
        raise ValueError('Eligibility condition must describe an unknown criterion')
    if (unknown - CONDITIONAL_CRITERIA or set(by_criterion) != unknown
            or any(unknown_kinds.get(k) != 'evidence_gap' for k in unknown)):
        return ConditionalEligibility.UNRESOLVED
    return ConditionalEligibility.CONDITIONAL if unknown else ConditionalEligibility.CONFIRMED


def eligibility(criteria: list[Criterion], required: list[str]) -> Eligibility:
    ids = [c.criterion_id for c in criteria]
    if len(ids) != len(set(ids)) or set(ids) - set(required):
        raise ValueError("Duplicate or unknown criterion IDs")
    if any(c.result == "fail" for c in criteria):
        return Eligibility.FAIL
    if set(ids) != set(required) or any(c.state != "completed" for c in criteria):
        return Eligibility.INCOMPLETE
    if any(c.result == "unknown" for c in criteria):
        return Eligibility.NEEDS_REVIEW
    return Eligibility.PASS


class Score(Contract):
    status: Literal["final", "conditional", "provisional", "unavailable"]
    value: Decimal | None
    bounds: Interval | None
    goal_feasibility: Literal["feasible", "uncertain", "infeasible"]


def feasibility(days: Interval | None, cost: Interval | None, budget: Decimal,
                resources: Literal["available", "conditional", "unavailable", "unknown"]):
    if resources == "unavailable" or (days and days.low > 90) or (cost and cost.low > budget):
        return "infeasible"
    if days and cost and days.high <= 90 and cost.high <= budget and resources == "available":
        return "feasible"
    return "uncertain"


def score(factors: dict[str, Factor | None], weights: dict[str, Decimal], *,
          days: Interval | None, cost: Interval | None, budget: Decimal,
          resources: Literal["available", "conditional", "unavailable", "unknown"],
          checks_complete: bool, conditional: bool = False) -> Score:
    if not weights or any(w < 0 for w in weights.values()) or sum(weights.values()) != 100:
        raise ValueError("Nonnegative weights must sum to 100")
    if set(factors) != set(weights):
        raise ValueError("Factors must match configured weights")
    goal = feasibility(days, cost, budget, resources)
    if not checks_complete or any(f is None for f in factors.values()):
        return Score(status="unavailable", value=None, bounds=None, goal_feasibility=goal)
    low = sum(weights[k] * f.low / 4 for k, f in factors.items())
    high = sum(weights[k] * f.high / 4 for k, f in factors.items())
    return Score(status="conditional" if conditional else "final", value=low,
                 bounds=Interval(low=low, high=high), goal_feasibility=goal)


def rank_key(item: tuple[str, str, Score]):
    """Ascending groups, then unrounded conservative score. IDs only break display ties."""
    identifier, name, result = item
    if result.status not in {"final", "conditional"}:
        raise ValueError("Unfinished assessments cannot be ranked")
    return ({"infeasible": 0, "uncertain": 1, "feasible": 2}[result.goal_feasibility],
            result.value, name.casefold(), identifier)


class EvidenceRef(Contract):
    evidence_id: str
    source_snapshot_id: str
    excerpt: str = Field(min_length=1)
    locator: str


class Claim(Contract):
    claim_id: str
    neutral_text: str = Field(min_length=1)
    kind: Literal["fact", "calculation", "judgment"]
    entity: str
    geography: str
    period: str | None
    metric: str | None
    source_urls: list[str]
    evidence_refs: list[EvidenceRef]
    formula: str | None
    input_claim_ids: list[str]
    quantity_estimate: Interval | None
    affected_criterion_ids: list[str]
    method: Literal['primary_source', 'secondary_source', 'calculation', 'model_judgment', 'human_required'] | None = None
    confidence: Literal['low', 'medium', 'high'] | None = None
    confidence_reason: str | None = None
    assumptions: list[str] = Field(default_factory=list)


class Risk(Contract):
    risk_id: str
    impact: int = Field(ge=1, le=3)
    critic_order: int = Field(ge=0)
    premise_claim_ids: list[str]
    essential_premise_ids: list[str]

    @model_validator(mode="after")
    def valid_premises(self):
        if not set(self.essential_premise_ids) <= set(self.premise_claim_ids):
            raise ValueError("Essential premises must belong to the risk")
        return self


class HandoffClaim(Contract):
    claim_id: str
    neutral_text: str
    kind: Literal["fact", "calculation", "judgment"]
    entity: str
    geography: str
    period: str | None
    metric: str | None
    source_urls: list[str]
    evidence_refs: list[EvidenceRef]
    formula: str | None
    input_claim_ids: list[str]
    quantity_estimate: Interval | None


def handoff(claims: list[Claim], risks: list[Risk], allowed_snapshots: set[str],
            criterion_ids: set[str]) -> list[HandoffClaim]:
    """Whitelist projection. Semantic neutrality is a separate, required service gate."""
    by_id = {c.claim_id: c for c in claims}
    if len(by_id) != len(claims) or len({r.risk_id for r in risks}) != len(risks):
        raise ValueError("Duplicate claim or risk ID")
    selected = set()
    for c in claims:
        if not set(c.affected_criterion_ids) <= criterion_ids:
            raise ValueError("Unknown criterion")
        if c.affected_criterion_ids:
            selected.add(c.claim_id)
    for r in sorted(risks, key=lambda r: (-r.impact, r.critic_order, r.risk_id))[:3]:
        selected.update(r.premise_claim_ids)
    visited, visiting = set(), set()

    def visit(identifier):
        if identifier in visiting:
            raise ValueError("Cyclic claim calculation")
        if identifier in visited:
            return
        if identifier not in by_id:
            raise ValueError("Missing claim dependency")
        visiting.add(identifier)
        c = by_id[identifier]
        if any(e.source_snapshot_id not in allowed_snapshots for e in c.evidence_refs):
            raise ValueError("Snapshot is outside this assessment scope")
        if c.kind == "calculation" and (not c.formula or not c.input_claim_ids):
            raise ValueError("Calculation requires formula and inputs")
        for dependency in c.input_claim_ids:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in sorted(selected):
        visit(identifier)
    # No impact order, criterion flags, or original risk opinion crosses the boundary.
    return [HandoffClaim.model_validate(by_id[i].model_dump(include=set(HandoffClaim.model_fields)))
            for i in sorted(visited)]


def reconcile(risk: Risk, verification: dict[str, str]) -> str:
    if any(verification.get(i) == "refuted" for i in risk.essential_premise_ids):
        return "refuted_basis"
    if not risk.essential_premise_ids or any(verification.get(i) != "supported"
                                           for i in risk.premise_claim_ids):
        return "unresolved"
    return "supported_basis"
