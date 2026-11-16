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


@pytest.mark.parametrize('original,revised', [
    ('Record outreach routes and responses. First action on day 1: contact the buyer.',
     'Record outreach routes and responses. The proposed test begins on day 1: contacts the buyer.'),
    ('Before first customer, identify launch states and document categories and obtain documented legal review.',
     'Before first customer, The proposed plan is to identify launch states and document categories and obtain documented legal review.'),
    ('If checks are not satisfied, defer affected customer launch, narrow supported states and retain the $15k budget.',
     'If checks are not satisfied, The proposed plan is to defer affected customer launch, narrow supported states and retain the $15k budget.'),
])
def test_saved_docusign_presentation_forms_preserve_actions_and_clause_scope(original,revised):
    assert _projection_case(original,revised)[0].neutral_text == revised


@pytest.mark.parametrize('original,revised', [
    ('Before first customer, identify launch states.', 'Before first customer. The proposed plan is to identify launch states.'),
    ('If checks fail, defer launch and obtain review.', 'If checks fail, The proposed plan is to defer launch or obtain review.'),
    ('If checks fail, defer launch and obtain review.', 'If checks fail, The proposed plan is to defer launch obtain review.'),
    ('Before first customer, identify launch states.', 'Before first customer, The proposed scenario assumes identify launch states.'),
    ('The buyer selects First action on day 1.', 'The buyer selects on day 1.'),
    ('And the service must not launch unlicensed.', 'The service must not launch unlicensed.'),
])
def test_presentation_exception_does_not_remove_conditions_or_ordinary_conjunctions(original,revised):
    with pytest.raises(ValueError,match='dependency|modality'):
        _projection_case(original,revised)


def _adjacent_verdict_case(prefix=''):
    from ai_builder.domain import Claim, HandoffClaim, FactorNeutralProjection, factor_projection_sources
    from test_pipeline import claim
    original = Claim.model_validate({**claim('test','judgment'), 'neutral_text':
        prefix + 'F03 would support fail and the service must not launch unlicensed. '
        'Licensing work or a substantive role change could require specialist resources, fees and delays beyond the stated budget and timetable. '
        'Reassess F01 and the launch plan rather than assuming that a partner’s status covers us.'})
    handoff = HandoffClaim.model_validate(original.model_dump(include=set(HandoffClaim.model_fields)))
    sources = factor_projection_sources([original], [])
    assert sources[-2]['omittable'] and sources[-2]['source_end']==sources[-1]['source_start']
    retained_part = sources[-1]['source_excerpt'].removeprefix(' and ').replace('Reassess F01', 'The proposed plan is to reassess F01')
    retained = prefix + retained_part
    projection = FactorNeutralProjection.model_validate(dict(rewrites=[dict(claim_id='test',neutral_text=retained)],
        coverage=([dict(source_id=sources[0]['source_id'],disposition='retained',target_claim_id='test',retained_excerpt=prefix)] if prefix else [])
        + [dict(source_id=sources[-2]['source_id'],disposition='omitted_author_evaluation',target_claim_id=None,retained_excerpt=None),
           dict(source_id=sources[-1]['source_id'],disposition='retained',target_claim_id='test',retained_excerpt=retained_part)]))
    return projection,[handoff],sources


@pytest.mark.parametrize('prefix',['','If an own special licence is required for the described role, '])
def test_leading_and_only_connecting_an_actually_omitted_adjacent_verdict_can_be_removed(prefix):
    from copy import deepcopy
    from ai_builder.domain import validate_factor_projection
    projection,originals,sources = _adjacent_verdict_case(prefix)
    frozen = deepcopy(sources)
    assert validate_factor_projection(projection,originals,sources)[0].neutral_text == projection.rewrites[0].neutral_text
    assert sources == frozen  # No new source-unit fields or frozen-input changes.


@pytest.mark.parametrize('defect',['different_origin','nonadjacent','not_omittable','not_omitted','or','internal_and'])
def test_leading_connector_exception_requires_same_origin_adjacent_omitted_author_unit(defect):
    from ai_builder.domain import validate_factor_projection
    projection,originals,sources = _adjacent_verdict_case()
    if defect=='different_origin':
        sources[0]['origin'] = 'claim:other:text'
    elif defect=='nonadjacent':
        sources[0]['source_end'] -= 1
    elif defect=='not_omittable':
        sources[0]['omittable'] = False
    elif defect=='not_omitted':
        projection.coverage[0].disposition = 'retained'
        projection.coverage[0].target_claim_id = 'test'
        projection.coverage[0].retained_excerpt = 'F03 would support fail'
        projection.rewrites[0].neutral_text = 'F03 would support fail ' + projection.rewrites[0].neutral_text
    elif defect=='or':
        sources[1]['source_excerpt'] = sources[1]['source_excerpt'].replace(' and ',' or ',1)
    else:
        projection.rewrites[0].neutral_text = projection.rewrites[0].neutral_text.replace('fees and delays','fees delays')
        projection.coverage[1].retained_excerpt = projection.rewrites[0].neutral_text
    with pytest.raises(ValueError):
        validate_factor_projection(projection,originals,sources)


ZUORA_TEST_PLAN = (
    'Proposed 90-day demand test, not a forecast. '
    'Seek 2 unrelated paying pilots by day 60 and renewals during days 61-90. '
    'Success: 2 actual pilot payments, accepted invoice outputs without unresolved material errors, '
    'and at least 1 paid renewal by day 90. Interest and unpaid commitments do not count. '
    'Stop the assumed paid-launch role upon an identified prohibition or required own special licence; '
    'never onboard while either eligibility check remains unresolved. '
    'Unresolved checks at day 60 constitute an eligibility/timing failure, not buyer rejection. '
    'Fewer than 8 interviews after 40 contacts triggers channel revision before further product spending.'
)


def test_saved_zuora_success_label_can_be_clarified_with_all_thresholds_and_stop_conditions_intact():
    revised = ZUORA_TEST_PLAN.replace('Success:', 'Proposed success criteria:')
    assert _projection_case(ZUORA_TEST_PLAN,revised)[0].neutral_text == revised


@pytest.mark.parametrize('defect', ['delete_success','reverse_proposal','remove_plan_proposal',
    'no_proposed_test','later_proposed_test','inline_success','nested_label','threshold','deadline','stop_condition','punctuation'])
def test_success_label_clarification_is_source_bound_directional_and_preserves_test_semantics(defect):
    original = ZUORA_TEST_PLAN
    revised = original.replace('Success:','Proposed success criteria:')
    if defect=='delete_success':
        revised = original.replace('Success:','')
    elif defect=='reverse_proposal':
        original,revised = revised,original
    elif defect=='remove_plan_proposal':
        revised = revised.removeprefix('Proposed ')
    elif defect=='no_proposed_test':
        original = original.removeprefix('Proposed ')
        revised = revised.removeprefix('Proposed ')
    elif defect=='later_proposed_test':
        prefix = 'Proposed 90-day demand test, not a forecast. '
        original = original.removeprefix(prefix) + ' ' + prefix
        revised = revised.removeprefix(prefix) + ' ' + prefix
    elif defect=='inline_success':
        original = original.replace('Success:', 'Customer success:')
        revised = revised.replace('Proposed success criteria:', 'Customer proposed success criteria:')
    elif defect=='nested_label':
        original = original.replace('Success:', 'Customer: Success:')
        revised = revised.replace('Proposed success criteria:', 'Customer: Proposed success criteria:')
    elif defect=='threshold':
        revised = revised.replace('2 actual pilot payments','3 actual pilot payments')
    elif defect=='deadline':
        revised = revised.replace('renewal by day 90','renewal by day 91')
    elif defect=='stop_condition':
        revised = revised.replace('never onboard while either eligibility check remains unresolved',
                                  'onboard while either eligibility check remains unresolved')
    elif defect=='punctuation':
        revised = revised.replace('days 61-90. Proposed', 'days 61-90, Proposed')
    with pytest.raises(ValueError,match='dependency|modality|premise'):
        _projection_case(original,revised)


def test_success_label_cannot_borrow_proposed_test_scope_from_another_source_unit():
    from ai_builder.domain import Claim,HandoffClaim,FactorNeutralProjection,factor_projection_sources,validate_factor_projection
    from test_pipeline import claim
    original = Claim.model_validate({**claim('test','judgment'),'neutral_text':'Proposed 90-day demand test, not a forecast.',
                                    'assumptions':['Success: 2 actual pilot payments by day 90.']})
    handoff = HandoffClaim.model_validate(original.model_dump(include=set(HandoffClaim.model_fields)))
    sources = factor_projection_sources([original],[])
    revised = original.neutral_text + ' Proposed success criteria: 2 actual pilot payments by day 90.'
    projection = FactorNeutralProjection.model_validate(dict(rewrites=[dict(claim_id='test',neutral_text=revised)],
        coverage=[dict(source_id=source['source_id'],disposition='retained',target_claim_id='test',
                       retained_excerpt=source['source_excerpt'].replace('Success:','Proposed success criteria:')) for source in sources]))
    with pytest.raises(ValueError,match='dependency|modality'):
        validate_factor_projection(projection,[handoff],sources)
