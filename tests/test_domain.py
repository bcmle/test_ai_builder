from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from ai_builder.domain import Criterion, Eligibility, Factor, Interval, eligibility, feasibility, rank_key, score


def result(state="completed", value="pass", evidence=None):
    return Criterion(criterion_id="F01", state=state, result=value,
                     evidence_ids=["e1"] if evidence is None else evidence, reason="Fixture")


@pytest.mark.parametrize("state", ["pending", "running", "technical_failed"])
def test_technical_state_never_business_unknown(state):
    assert eligibility([result(state, None)], ["F01"]) == Eligibility.INCOMPLETE


def test_required_criterion_cannot_be_silently_omitted():
    assert eligibility([result()], ["F01", "F02"]) == Eligibility.INCOMPLETE


@pytest.mark.parametrize("value", ["pass", "fail"])
def test_pass_and_fail_require_evidence(value):
    with pytest.raises(ValidationError):
        result(value=value, evidence=[])


def test_unknown_is_not_failure_or_zero():
    assert eligibility([result(value="unknown", evidence=[])], ["F01"]) == Eligibility.NEEDS_REVIEW


def test_config_rejects_bad_weights_and_missing_filter(config):
    data = config.model_dump()
    data["factors"][0]["weight"] = D(26)
    with pytest.raises(ValidationError):
        type(config).model_validate(data)
    data = config.model_dump()
    data["criteria"].pop()
    with pytest.raises(ValidationError):
        type(config).model_validate(data)


def assessment(config, numbers, days):
    return score({f.id: Factor(low=v, high=v) for f, v in zip(config.factors, numbers)},
                 {f.id: f.weight for f in config.factors}, days=Interval(low=days, high=days),
                 cost=Interval(low=100, high=100), budget=D(15000), resources="available", checks_complete=True)


def test_92_day_model_cannot_outrank_feasible_model(config):
    a = assessment(config, [4, 4, 1, 2, 4], 92)
    b = assessment(config, [3, 3, 3, 3, 2], 60)
    assert a.value == 75 and b.value == D("72.5")
    assert rank_key(("a", "A", a)) < rank_key(("b", "B", b))
    assert a.goal_feasibility == "infeasible" and b.goal_feasibility == "feasible"


def test_honest_interval_is_final_and_conservative(config):
    factors = {f.id: Factor(low=2, high=3) for f in config.factors}
    s = score(factors, {f.id: f.weight for f in config.factors}, days=Interval(low=14, high=15),
              cost=Interval(low=100, high=200), budget=D(15000), resources="available", checks_complete=True)
    assert s.status == "final" and s.value == 50 and s.bounds.high == 75


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_invalid_numeric_estimates(value):
    with pytest.raises(ValidationError):
        Interval(low=value, high=value)


def test_unknown_resources_are_not_available():
    assert feasibility(Interval(low=1, high=2), Interval(low=1, high=2), D(10), "unknown") == "uncertain"


@pytest.mark.parametrize('criterion', [f'F{i:02}' for i in range(1, 11)])
def test_only_explicitly_permitted_completed_evidence_gaps_can_be_conditional(criterion):
    from ai_builder.domain import CONDITIONAL_CRITERIA, EligibilityCondition, conditional_eligibility
    criteria = [result().model_copy(update={'criterion_id':f'F{i:02}',
                'result':'unknown' if f'F{i:02}' == criterion else 'pass'}) for i in range(1,11)]
    conditions = [EligibilityCondition(condition_id='condition-'+criterion, criterion_id=criterion,
        assumption='The described role can meet ordinary launch requirements.',
        required_check='Obtain local role-specific advice before the first customer.',
        impact_on_factors='Add the bounded paid review cost and lead time.', evidence_ids=['e1'])] if criterion in CONDITIONAL_CRITERIA else []
    evaluate = lambda **overrides: conditional_eligibility(criteria, [c.criterion_id for c in criteria],
        **dict(unknown_kinds={criterion:'evidence_gap'}, conditions=conditions, legal_reviews_complete=True) | overrides)
    assert evaluate() == ('conditional' if conditions else 'unresolved')
    assert evaluate(legal_reviews_complete=False) == 'unresolved'
    assert evaluate(unknown_kinds={criterion:'material_conflict'}) == 'unresolved'
    assert evaluate(conditions=[]) == 'unresolved'
    criteria[0] = result('technical_failed', None)
    assert evaluate() == 'unresolved'


def test_conditional_score_uses_identical_math_but_cannot_skip_checks(config):
    factors = {f.id:Factor(low=2, high=3) for f in config.factors}
    kwargs = dict(days=Interval(low=14, high=15), cost=Interval(low=100, high=200),
                  budget=D(15000), resources='conditional', conditional=True)
    result = score(factors, {f.id:f.weight for f in config.factors}, checks_complete=True, **kwargs)
    assert result.status == 'conditional' and result.value == 50 and result.bounds.high == 75
    result = score(factors, {f.id:f.weight for f in config.factors}, checks_complete=False, **kwargs)
    assert result.status == 'unavailable' and result.value is None


def _projection_case(original, revised):
    from ai_builder.domain import Claim, HandoffClaim, FactorNeutralProjection, factor_projection_sources, validate_factor_projection
    from test_pipeline import claim
    value = Claim.model_validate({**claim('test','judgment'), 'neutral_text':original})
    handoff = HandoffClaim.model_validate(value.model_dump(include=set(HandoffClaim.model_fields)))
    sources = factor_projection_sources([value], [])
    result = FactorNeutralProjection.model_validate(dict(rewrites=[dict(claim_id='test',neutral_text=revised)],
        coverage=[dict(source_id=s['source_id'], disposition='retained', target_claim_id='test', retained_excerpt=revised)
                  for s in sources]))
    return validate_factor_projection(result, [handoff], sources)


def test_declarative_plan_can_keep_meaning_without_every_generic_word():
    result = _projection_case(
        'First action on day 1: contact the buyer; stop further feature investment if no payment by day 30.',
        'The proposed test begins on day 1: contacts the buyer; stops further feature investment if no payment by day 30.')
    assert result[0].kind == 'judgment'


@pytest.mark.parametrize('original,revised', [
    ('A partner may be required.', 'The scenario may operate without a partner.'),
    ('A partner may be required.', 'The scenario may operate and must have a partner.'),
    ('The review costs $500.', 'The review costs $500 plus $900.'),
    ('The review takes 3 days.', 'The review takes 30 days.'),
    ('No license is required before launch.', 'A license is required before no launch.'),
    ('The role may need a license.', 'The role needs a license.'),
    ('Launch occurs after review.', 'Launch occurs before review.'),
    ('Budget must stay below $15k before launch.', 'Budget must stay below $15m before launch.'),
    ('The test stops investment if conversion is below 5% after 30 days.', 'The test stops investment if conversion is above 5% after 30 days.'),
    ('Customer data is deleted after review.', 'Customer data is retained after review.'),
    ('The test stops if no customer payment and no partner approval.', 'The test stops if no customer payment or no partner approval.'),
    ('Funds are paid to the customer after review.', 'Funds are paid by the customer after review.'),
    ('Because a customer pays, the partner approves.', 'A customer pays, the partner approves.'),
    ('The customer was licensed.', 'The customer is licensed.'),
    ('If the legal review is complete, launch is allowed.', 'If the legal review is complete. Launch is allowed.'),
    ('No license is required before launch.', 'No. License is required before launch.'),
    ('The data processor must delete customer data after review.', 'The data supplier must delete customer data after review.'),
    ('The supplier is not the data processor.', 'The processor is not the data supplier.'),
    ('The result is established.', 'The result is guaranteed.'),
    ('Launch depends on review.', 'Launch requires on review.'),
    ('A customer may require support.', 'A client may require support.'),
    ('A license could be required.', 'A license may be required.'),
])
def test_factor_projection_cannot_mutate_or_add_material_premises(original,revised):
    with pytest.raises(ValueError, match='premise|dependency|modality'):
        _projection_case(original,revised)


def test_condition_components_may_add_their_supplied_operational_premises_to_a_claim():
    from ai_builder.domain import Claim, HandoffClaim, FactorNeutralProjection, factor_projection_sources, validate_factor_projection
    from test_pipeline import claim
    original = Claim.model_validate({**claim('test','judgment'), 'neutral_text':'A partner may be required.'})
    handoff = HandoffClaim.model_validate(original.model_dump(include=set(HandoffClaim.model_fields)))
    condition = dict(condition_id='condition-F03', assumption='Legal review may cost $700.',
                     required_check='Complete the review before day 5.', impact_on_factors='Budget must stay below $15k.')
    sources = factor_projection_sources([original], [condition])
    text = ' '.join([original.neutral_text, condition['assumption'], condition['required_check'], condition['impact_on_factors']])
    projection = FactorNeutralProjection.model_validate(dict(rewrites=[dict(claim_id='test',neutral_text=text)],
        coverage=[dict(source_id=source['source_id'], disposition='retained', target_claim_id='test',
                       retained_excerpt=source['source_excerpt']) for source in sources]))
    assert validate_factor_projection(projection,[handoff],sources)[0].neutral_text == text
    projection.rewrites[0].neutral_text += ' An additional payment is $900.'
    with pytest.raises(ValueError,match='new quantitative premise'):
        validate_factor_projection(projection,[handoff],sources)


@pytest.mark.parametrize('extra', ['Customer data is deleted.', 'Customer data is not deleted.'])
def test_projection_cannot_append_an_unmapped_opposite_or_duplicate_proposition(extra):
    from ai_builder.domain import Claim, HandoffClaim, FactorNeutralProjection, factor_projection_sources, validate_factor_projection
    from test_pipeline import claim
    original = Claim.model_validate({**claim('test','judgment'), 'neutral_text':'Customer data is not deleted.'})
    handoff = HandoffClaim.model_validate(original.model_dump(include=set(HandoffClaim.model_fields)))
    sources = factor_projection_sources([original], [])
    result = FactorNeutralProjection.model_validate(dict(rewrites=[dict(claim_id='test',neutral_text=original.neutral_text+' '+extra)],
        coverage=[dict(source_id=sources[0]['source_id'],disposition='retained',target_claim_id='test',retained_excerpt=original.neutral_text)]))
    with pytest.raises(ValueError,match='unmapped operational assertion|duplicate proposition'):
        validate_factor_projection(result,[handoff],sources)


@pytest.mark.parametrize('original,revised', [
    ('Run a conditional 90-day demand test.', 'The proposed plan runs a conditional 90-day demand test.'),
    ('Use synthetic data until customer access is authorized.', 'The proposed plan uses synthetic data until customer access is authorized.'),
    ('Do not launch the unresolved role.', 'The proposed plan does not launch the unresolved role.'),
    ('Do not onboard customers while the requirement remains unresolved.', 'The proposed plan does not onboard customers while the requirement remains unresolved.'),
    ('Run 10 interviews; proceed only if it fits the $15k budget; stop investment if no payment by day 30.',
     'The proposed plan is to run 10 interviews; proceed only if it fits the $15k budget; stop investment if no payment by day 30.'),
])
def test_live_imperative_cases_have_a_close_declarative_repair(original,revised):
    assert _projection_case(original,revised)[0].neutral_text == revised


def test_specific_pure_veza_rating_can_be_omitted_while_partner_premise_remains():
    from ai_builder.domain import Claim, HandoffClaim, FactorNeutralProjection, factor_projection_sources, validate_factor_projection
    from test_pipeline import claim
    original = Claim.model_validate({**claim('test','judgment'), 'neutral_text':
        'Another 2 remains the conservative interpretation because an external partner may be required.'})
    handoff = HandoffClaim.model_validate(original.model_dump(include=set(HandoffClaim.model_fields)))
    sources = factor_projection_sources([original], [])
    assert len(sources)==2 and sources[0]['omittable'] and not sources[1]['omittable']
    neutral = 'The proposed scenario assumes an external partner may be required.'
    projection = FactorNeutralProjection.model_validate(dict(rewrites=[dict(claim_id='test',neutral_text=neutral)],
        coverage=[dict(source_id=sources[0]['source_id'],disposition='omitted_author_evaluation',target_claim_id=None,retained_excerpt=None),
                  dict(source_id=sources[1]['source_id'],disposition='retained',target_claim_id='test',
                       retained_excerpt='an external partner may be required.')]))
    assert validate_factor_projection(projection,[handoff],sources)[0].neutral_text == neutral
    original.neutral_text = 'Another 20 customers may require support.'
    assert not any(s['omittable'] for s in factor_projection_sources([original], []))
