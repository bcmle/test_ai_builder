"""Strict analytical contracts; provider output cannot decide ranking or eligibility."""

from decimal import Decimal, ROUND_CEILING
from collections import Counter
from enum import StrEnum
import re
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


def rounded_days(value: Interval | None) -> Interval | None:
    if value is None:
        return None
    return Interval(low=value.low.to_integral_value(rounding=ROUND_CEILING),
                    high=value.high.to_integral_value(rounding=ROUND_CEILING))


def timing_factor(value: Interval | None) -> Factor | None:
    days = rounded_days(value)
    if days is None:
        return None
    def anchor(day):
        return 4 if day<=14 else 3 if day<=30 else 2 if day<=60 else 1 if day<=90 else 0
    return Factor(low=anchor(days.high),high=anchor(days.low))


def total_payment_days(preparation: Interval | None, payment_cycle: Interval | None) -> Interval | None:
    first,second = rounded_days(preparation),rounded_days(payment_cycle)
    if first is None or second is None:
        return None
    return Interval(low=first.low+second.low,high=first.high+second.high)


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


class MilestoneClaim(Claim):
    start_event: Literal['assessment_start','offer_ready','first_payment_received'] | None
    end_event: Literal['assessment_start','offer_ready','first_payment_received'] | None

    @model_validator(mode='after')
    def milestone_pair(self):
        if (self.start_event is None) != (self.end_event is None):
            raise ValueError('A time estimate requires both milestone boundaries')
        return self


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


class MilestoneHandoffClaim(HandoffClaim):
    start_event: Literal['assessment_start','offer_ready','first_payment_received'] | None
    end_event: Literal['assessment_start','offer_ready','first_payment_received'] | None


FACTOR_PROJECTION_POLICY = 'factor-neutral-projection-repair-1'
# Only standalone author labels may be omitted. Splitting records every other
# span separately, so a mixed score-plus-dependency cannot disappear with it.
_AUTHOR_EVALUATION = re.compile(
    r'\bS0[1-5]\s+(?:scores?|is\s+scored|has\s+a\s+score\s+of)\s+[0-4](?:\s*[/–-]\s*[0-4])?(?:\s+out\s+of\s+4)?\b'
    r'|\bF(?:0[1-9]|10)\s+(?:(?:would|could|will|may)\s+)?(?:support(?:s)?\s+|is\s+|be\s+)?(?:pass|fail|unknown)\b'
    r'|\bAnother\s+[0-4]\s+remains\s+the\s+conservative\s+interpretation\b', re.I)
_PROTECTED_ALIASES = {
    'licensed':'license','licence':'license','licensing':'license','licences':'license','licenses':'license',
    'partners':'partner','partnership':'partner','dependencies':'dependency','dependent':'dependency',
    'depends':'dependency','requires':'dependency','required':'dependency','require':'dependency',
    'needed':'dependency','needs':'dependency','need':'dependency','prerequisite':'dependency',
    'customers':'customer','client':'customer','clients':'customer','buyers':'buyer',
    'payments':'payment','paying':'payment','paid':'payment','pay':'payment',
    'reviews':'review','reviewing':'review','regulatory':'regulator','regulators':'regulator',
    'suppliers':'provider','supplier':'provider','providers':'provider','processor':'provider',
    'days':'day','weeks':'week','months':'month','hours':'hour',
    'stops':'stop','stopping':'stop','halt':'stop','cease':'stop',
    'assumption':'assume','assumptions':'assume','assumed':'assume','assumes':'assume','assuming':'assume','hypothesis':'assume',
    'unverified':'uncertain','unproven':'uncertain','uncertainty':'uncertain','unknown':'uncertain',
    'cannot':'negative','not':'negative','no':'negative','without':'negative',
    'might':'may','could':'may','conditional':'if','unless':'unless',
    'proceeds':'proceed','launches':'launch','launching':'launch',
    'guaranteed':'certainty','definitely':'certainty','established':'certainty','proven':'certainty',
    'obtaining':'obtain','obtains':'obtain','obtained':'obtain',
    'contacting':'contact','contacts':'contact','contacted':'contact',
    'checking':'check','checks':'check','checked':'check',
    'proposed':'propose','proposes':'propose','proposing':'propose',
    'begins':'begin','beginning':'begin','includes':'include','including':'include',
    'states':'state','describes':'describe','premises':'premise','rules':'rule',
    'we':'team','us':'team','our':'team',
    'runs':'run','uses':'use','onboards':'onboard','remains':'remain','validates':'validate',
    'collects':'collect','records':'record','assesses':'assess','completes':'complete',
}
_PROTECTED_WORDS = set('license partner dependency customer buyer payment review regulator provider data jurisdiction budget '
    'day week month hour stop launch proceed investment before after until only if unless may must assume uncertain negative certainty'.split())
_MODAL_TERMS = set('before after until only if unless may must assume uncertain negative certainty'.split())


def _protected_premises(text):
    values = re.findall(r'[$€£]|\d+(?:[,.]\d+)*%?|[A-Za-z]+', text)
    return {token.lower() if re.search(r'\d|[$€£]', token) else _PROTECTED_ALIASES.get(token.lower(), token.lower())
            for token in values if re.search(r'\d|[$€£]', token)
            or _PROTECTED_ALIASES.get(token.lower(), token.lower()) in _PROTECTED_WORDS}


def _quantities(text):
    return Counter(re.findall(r'[$€£]|\d+(?:[,.]\d+)*(?:[kKmMbB]\b)?%?', text))


_FUNCTION_WORDS = set('a an the is are be am do does'.split())
# Unlike the supplementary premise buckets above, equality must not conflate
# legal roles, epistemic states, predicates, or different dependency wording.
_GRAMMATICAL_ALIASES = {
    'contacts':'contact', 'stops':'stop', 'runs':'run', 'uses':'use', 'proceeds':'proceed',
    'licence':'license', 'licences':'licenses', 'licenced':'licensed', 'licencing':'licensing',
}
_PRESENTATION = re.compile(
    r'(^|(?<=[.;:\n]))\s*(?:the\s+)?proposed\s+(?:stop\s+rule|test|plan|scenario|procedure|team)'
    r'(?:\s+(?:is\s+to|assumes?|includes?|begins?|states?|describes?))?\s*'
    r'|(^|(?<=[.;:\n]))\s*(?:the\s+)?plan\s+is\s+to\s*'
    r'|(?<=,)\s*The\s+proposed\s+plan\s+is\s+to\s*'
    r'|Unverified scenario premises:\s*'
    r'|(^|(?<=[.;:\n]))\s*First action(?=\s+(?:on\s+day|:)|:)', re.I)


def _operational_tokens(text):
    """Conservative grammar: remove only declared presentation, retain relations."""
    masked = _PRESENTATION.sub(lambda match:' ' * len(match.group()), text)
    result = []
    for match in re.finditer(r'[$€£]|\d+(?:[,.]\d+)*(?:[kKmMbB]\b)?%?|<=|>=|[<>.,;:!?()]|[A-Za-z]+', masked):
        word = match.group().lower()
        if word in _FUNCTION_WORDS:
            continue
        token = _GRAMMATICAL_ALIASES.get(word, word)
        result.append((token, match.start(), match.end()))
    return result


def _content_tokens(text):
    return [token for token,_,__ in _operational_tokens(text)]


def _proposed_success_label_variant(text):
    """Forward clarification of a standalone heading inside this proposed test."""
    heading = re.match(r'^\s*(?:the\s+)?proposed\s+(?:\d+[-– ]day\s+)?(?:demand\s+)?test'
                       r'(?:,\s*not\s+a\s+forecast)?\s*[.:]', text, re.I)
    if not heading:
        return text
    return re.sub(r'(^|(?<=[.\n]))(\s*)Success(?=\s*:)',
                  lambda match:match.group(1)+match.group(2)+'Proposed success criteria'
                  if match.start() >= heading.end() else match.group(), text, flags=re.I)


def _negative_anchors(text):
    words = [_PROTECTED_ALIASES.get(word.lower(), word.lower()) for word in re.findall(r'[A-Za-z]+', text)]
    material = _PROTECTED_WORDS - _MODAL_TERMS - {'day','week','month','hour'}
    anchors = []
    for index, word in enumerate(words):
        if word != 'negative':
            continue
        after = next((token for token in words[index + 1:] if token in material), None)
        if after == 'dependency':
            after = next((token for token in reversed(words[:index]) if token in material - {'dependency'}), after)
        anchors.append(after)
    return Counter(anchors)


class FactorTextRewrite(Contract):
    claim_id: str
    neutral_text: str = Field(min_length=1, max_length=24000)


class FactorProjectionCoverage(Contract):
    source_id: str
    disposition: Literal['retained', 'omitted_author_evaluation']
    target_claim_id: str | None
    retained_excerpt: str | None = Field(max_length=16000)


class FactorNeutralProjection(Contract):
    rewrites: list[FactorTextRewrite]
    coverage: list[FactorProjectionCoverage]


def factor_projection_sources(claims: list[Claim], conditions: list[dict]) -> list[dict]:
    """Private, deterministic provenance units. No source is silently removed."""
    parts = [(f'claim:{claim.claim_id}:text', claim.claim_id, claim.neutral_text) for claim in claims]
    parts += [(f'claim:{claim.claim_id}:assumption:{index}', claim.claim_id, text)
              for claim in claims for index, text in enumerate(claim.assumptions)]
    parts += [(f'condition:{condition["condition_id"]}:{field}', None, condition[field])
              for condition in conditions for field in ('assumption','required_check','impact_on_factors')]
    units = []
    for origin, claim_id, text in parts:
        position, spans = 0, []
        for match in _AUTHOR_EVALUATION.finditer(text):
            if match.start() > position:
                spans.append((position, match.start(), False))
            spans.append((match.start(), match.end(), True))
            position = match.end()
        if position < len(text) or not spans:
            spans.append((position, len(text), False))
        for index, (start, end, omittable) in enumerate(spans):
            excerpt = text[start:end]
            units.append(dict(source_id=f'{origin}:{index}', origin=origin, claim_id=claim_id,
                source_start=start, source_end=end, source_excerpt=excerpt, omittable=omittable,
                author_evaluation_connector=bool(index and spans[index - 1][2]
                                                and re.match(r'\s*because\b', excerpt, re.I)),
                protected_terms=sorted(_protected_premises(excerpt))))
    return units


def validate_factor_projection(projection: FactorNeutralProjection, originals: list[HandoffClaim], sources: list[dict]):
    original_ids = {claim.claim_id for claim in originals}
    rewritten = {claim.claim_id:claim.neutral_text for claim in projection.rewrites}
    if len(projection.rewrites) != len(originals) or set(rewritten) != original_ids:
        raise ValueError('Neutral projection must preserve every original claim ID exactly once')
    source_map = {source['source_id']:source for source in sources}
    if len(projection.coverage) != len(sources) or {c.source_id for c in projection.coverage} != set(source_map):
        raise ValueError('Neutral projection must account for every text, assumption and condition component exactly once')
    coverage_by_source = {item.source_id:item for item in projection.coverage}
    predecessors, previous_by_origin = {}, {}
    for source in sources:
        previous = previous_by_origin.get(source['origin'])
        if previous and previous['source_end'] == source['source_start']:
            predecessors[source['source_id']] = previous
        previous_by_origin[source['origin']] = source
    covered = {identifier:set() for identifier in original_ids}
    for item in projection.coverage:
        source = source_map[item.source_id]
        if item.disposition == 'omitted_author_evaluation':
            if not source['omittable'] or item.target_claim_id is not None or item.retained_excerpt is not None:
                raise ValueError('Only a pure author score or verdict may be omitted; operational premises must remain')
            continue
        text = rewritten.get(item.target_claim_id)
        if (text is None or (source['claim_id'] is not None and item.target_claim_id != source['claim_id'])
                or not item.retained_excerpt or item.retained_excerpt not in text):
            raise ValueError('Coverage must quote a retained fragment in the corresponding neutral claim')
        if source['omittable']:
            raise ValueError('The rejected author score or verdict must not enter the repaired verifier projection')
        target_terms = _protected_premises(item.retained_excerpt)
        required = set(source['protected_terms'])
        source_content = re.sub(r'^\s*because\s+', '', source['source_excerpt'], flags=re.I) if source['author_evaluation_connector'] else source['source_excerpt']
        source_variants = [source_content]
        previous = predecessors.get(item.source_id)
        if previous and previous['omittable']:
            previous_coverage = coverage_by_source[previous['source_id']]
            if (previous_coverage.disposition == 'omitted_author_evaluation'
                    and previous_coverage.target_claim_id is None and previous_coverage.retained_excerpt is None):
                # Infer only from immutable adjacent spans and actual omission;
                # keep frozen source-unit fields and repair packet hashes intact.
                source_variants.append(re.sub(r'^\s*and\s+', '', source_content, flags=re.I))
        source_variants += [_proposed_success_label_variant(content) for content in tuple(source_variants)]
        if (not required <= target_terms or required & _MODAL_TERMS != target_terms & _MODAL_TERMS
                or _quantities(source['source_excerpt']) != _quantities(item.retained_excerpt)
                or _negative_anchors(source['source_excerpt']) != _negative_anchors(item.retained_excerpt)
                or not any(_content_tokens(content) == _content_tokens(item.retained_excerpt) for content in source_variants)):
            raise ValueError('Neutral repair changed an operational dependency, number, time, negation or modality')
        # Bind each map entry to one literal occurrence, preferring uncovered
        # occurrences. Repeating a proposition cannot hide behind one reference.
        positions, offset = [], 0
        while (start := text.find(item.retained_excerpt, offset)) >= 0:
            positions.append(start)
            offset = start + 1
        start = max(positions, key=lambda position:sum(index not in covered[item.target_claim_id]
                    for index in range(position, position + len(item.retained_excerpt))))
        covered[item.target_claim_id].update(range(start, start + len(item.retained_excerpt)))
    for original in originals:
        routed_conditions = {item.source_id for item in projection.coverage
                             if item.disposition == 'retained' and item.target_claim_id == original.claim_id
                             and source_map[item.source_id]['claim_id'] is None}
        retained_sources = [source['source_excerpt'] for source in sources
                            if (source['claim_id'] == original.claim_id or source['source_id'] in routed_conditions)
                            and not source['omittable']]
        original_text = ' '.join(retained_sources)
        new_text = rewritten[original.claim_id]
        if _quantities(new_text) - _quantities(original_text):
            raise ValueError('Neutral repair introduced a new quantitative premise')
        # A declarative judgment may explicitly label its existing hypotheses;
        # it may not add certainty, negate a new premise or strengthen may to must.
        permitted = {'assume','uncertain'} if original.kind == 'judgment' else set()
        if (_protected_premises(new_text) & _MODAL_TERMS) - (_protected_premises(original_text) | permitted):
            raise ValueError('Neutral repair introduced a new condition, negation or strengthened modality')
        if any(not set(range(start,end)) <= covered[original.claim_id]
               for _,start,end in _operational_tokens(new_text)):
            raise ValueError('Neutral repair contains an unmapped operational assertion or duplicate proposition')
    # All nontext fields are copied by code, never reconstructed by the model.
    return [claim.model_copy(update={'neutral_text':rewritten[claim.claim_id]}) for claim in originals]


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
