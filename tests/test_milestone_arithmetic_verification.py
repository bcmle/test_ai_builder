"""A source-free arithmetic check must not certify its forecast inputs as facts."""
from copy import deepcopy
from decimal import Decimal

import pytest

from ai_builder.pipeline import (FactorsV153, FactorsStateV153, Verification,
                                materialize_milestones, validate_factor_verification)
from test_milestone_pipeline import milestone_output


def fixture():
    raw = milestone_output({'eligibility_conditions':[]})
    factors = materialize_milestones(FactorsV153.model_validate(raw),'US')
    checked = Verification.model_validate({'items':[
        {'claim_id':claim.claim_id,
         'status':'supported' if claim.claim_id=='factor-time-to-first-payment' else 'not_fact_checkable',
         'evidence_ids':[], 'reason':'Arithmetic on explicitly conditional estimates.'}
        for claim in factors.claims]})
    return factors, checked


def test_generated_total_is_arithmetic_while_inputs_remain_conditional_and_immutable():
    factors,checked = fixture()
    before = deepcopy((factors.model_dump(mode='json'),checked.model_dump(mode='json')))
    validate_factor_verification(checked,factors,market='US',milestones=True)
    assert (factors.model_dump(mode='json'),checked.model_dump(mode='json'))==before
    inputs = {factors.preparation_claim_id,factors.payment_cycle_claim_id}
    assert all(item.status=='not_fact_checkable' for item in checked.items if item.claim_id in inputs)
    assert factors.resources=='conditional'


def test_total_rounds_each_conditional_stage_up_separately():
    raw = milestone_output({'eligibility_conditions':[]})
    for field,identifier in [('preparation_days','factor-preparation'),('payment_cycle_days','factor-payment')]:
        raw[field]={'low':'1.1','high':'2.1'}
        claim = next(c for c in raw['claims'] if c['claim_id']==identifier)
        claim['quantity_estimate']=dict(raw[field])
        claim['neutral_text']='The conditional stage is estimated at 1.1 to 2.1 calendar days.'
        claim['assumptions']=[claim['neutral_text']]
    factors=materialize_milestones(FactorsV153.model_validate(raw),'US')
    _,checked=fixture()
    validate_factor_verification(checked,factors,market='US',milestones=True)
    assert factors.days.model_dump(mode='json')=={'low':'4','high':'6'}


@pytest.mark.parametrize('defect',['total_range','formula','dependencies','metric','start_event','end_event',
                                  'input_quantity','input_start','input_end','missing_input'])
def test_arithmetic_exemption_rejects_changed_total_or_inconsistent_stage_passports(defect):
    factors,checked=fixture()
    raw=factors.model_dump(mode='json')
    total=next(c for c in raw['claims'] if c['claim_id']=='factor-time-to-first-payment')
    preparation=next(c for c in raw['claims'] if c['claim_id']==raw['preparation_claim_id'])
    if defect=='total_range':
        total['quantity_estimate']['low']='1'
    elif defect=='formula':
        total['formula']='ceil(preparation.low + payment_cycle.low)'
    elif defect=='dependencies':
        total['input_claim_ids'].reverse()
    elif defect=='metric':
        total['metric']='price_in_usd'
    elif defect=='start_event':
        total['start_event']='offer_ready'
    elif defect=='end_event':
        total['end_event']='offer_ready'
    elif defect=='input_quantity':
        preparation['quantity_estimate']={'low':'1','high':'2'}
    elif defect=='input_start':
        preparation['start_event']='offer_ready'
    elif defect=='input_end':
        preparation['end_event']='first_payment_received'
    else:
        raw['claims']=[c for c in raw['claims'] if c['claim_id']!=raw['preparation_claim_id']]
        checked.items=[i for i in checked.items if i.claim_id!=raw['preparation_claim_id']]
    with pytest.raises(ValueError):
        validate_factor_verification(checked,FactorsStateV153.model_validate(raw),market='US',milestones=True)


@pytest.mark.parametrize('status',['refuted','mixed','insufficient'])
def test_unacceptable_input_review_cannot_unlock_supported_arithmetic(status):
    factors,checked=fixture()
    item=next(i for i in checked.items if i.claim_id==factors.preparation_claim_id)
    item.status=status
    item.evidence_ids=['evidence'] if status in {'refuted','mixed'} else []
    with pytest.raises(ValueError):
        validate_factor_verification(checked,factors,market='US',milestones=True)


@pytest.mark.parametrize('kind',['fact','calculation'])
def test_other_source_free_supported_claim_still_rejects_whole_candidate(kind):
    factors,checked=fixture()
    raw=factors.model_dump(mode='json')
    other=deepcopy(raw['claims'][0])
    other.update(claim_id='arbitrary-claim',kind=kind,method=kind if kind=='calculation' else 'primary_source',
                 formula='x+y' if kind=='calculation' else None,
                 input_claim_ids=[raw['preparation_claim_id']] if kind=='calculation' else [],
                 source_urls=[],evidence_refs=[])
    raw['claims'].insert(-1,other)  # The generated total remains last, as in real materialization.
    candidate=Verification.model_validate({'items':checked.model_dump(mode='json')['items']+
        [{'claim_id':'arbitrary-claim','status':'supported','evidence_ids':[],'reason':'No source supplied.'}]})
    with pytest.raises(ValueError,match='evidence'):
        validate_factor_verification(candidate,FactorsStateV153.model_validate(raw),market='US',milestones=True)


def test_legacy_path_has_no_reserved_arithmetic_exemption():
    factors,checked=fixture()
    with pytest.raises(ValueError,match='evidence'):
        validate_factor_verification(checked,factors,market='US',milestones=False)


def budget_share_fixture():
    raw=milestone_output({'eligibility_conditions':[]})
    raw['cost']={'low':'3000','high':'9000'}
    cost=next(c for c in raw['claims'] if c['claim_id']=='factor-cost')
    cost.update(quantity_estimate=dict(raw['cost']),metric='Non-salary cost in USD',
                neutral_text='The proposed conditional test costs 3000 to 9000 USD.')
    scenario=deepcopy(cost)
    scenario.update(claim_id='factor-scenario',quantity_estimate=None,metric='Scenario boundaries',
                    neutral_text='The unchanged scenario has a 15000 USD non-salary budget.',
                    assumptions=['The scenario budget is a constraint, not a forecast.'])
    share=deepcopy(cost)
    share.update(claim_id='factor-budget-share',kind='calculation',method='calculation',
        neutral_text='The conditional test cost represents 20 to 60 percent of the scenario budget.',
        metric='Non-salary budget share, percent',quantity_estimate={'low':'20','high':'60'},
        formula='100×[3000,9000]/15000=[20,60].',input_claim_ids=['factor-cost','factor-scenario'],assumptions=[])
    raw['claims'].extend([scenario,share])
    factors=materialize_milestones(FactorsV153.model_validate(raw),'US')
    checked=Verification.model_validate({'items':[
        {'claim_id':c.claim_id,'status':'supported' if c.kind=='calculation' else 'not_fact_checkable',
         'evidence_ids':[],'reason':'Arithmetic only; forecast and scenario inputs remain conditional.'}
        for c in factors.claims]})
    return factors,checked


def test_exact_budget_share_arithmetic_preserves_raw_conditional_inputs():
    factors,checked=budget_share_fixture()
    before=deepcopy((factors.model_dump(mode='json'),checked.model_dump(mode='json')))
    validate_factor_verification(checked,factors,market='US',milestones=True,business_budget_usd=Decimal('15000'))
    assert (factors.model_dump(mode='json'),checked.model_dump(mode='json'))==before
    assert all(i.status=='not_fact_checkable' for i in checked.items if i.claim_id in {'factor-cost','factor-scenario'})


@pytest.mark.parametrize('budget',[None,Decimal('0'),Decimal('-15000'),Decimal('15001')])
def test_budget_share_requires_positive_exact_frozen_denominator(budget):
    factors,checked=budget_share_fixture()
    with pytest.raises(ValueError):
        validate_factor_verification(checked,factors,market='US',milestones=True,business_budget_usd=budget)


@pytest.mark.parametrize('defect',['cost_mismatch','cost_missing','cost_link','cost_unit','percent_missing','percent_range',
    'percent_unit','operator','expression','dependencies','arbitrary_id'])
def test_budget_share_exemption_has_exact_structured_inputs_and_allowlisted_operation(defect):
    factors,checked=budget_share_fixture()
    raw=factors.model_dump(mode='json')
    cost=next(c for c in raw['claims'] if c['claim_id']=='factor-cost')
    share=next(c for c in raw['claims'] if c['claim_id']=='factor-budget-share')
    if defect=='cost_mismatch': cost['quantity_estimate']['high']='8000'
    elif defect=='cost_missing': raw['cost']=None
    elif defect=='cost_link': raw['cost_claim_id']='factor-scenario'
    elif defect=='cost_unit': cost['metric']='Non-salary cost in EUR, not USD'
    elif defect=='percent_missing': share['quantity_estimate']=None
    elif defect=='percent_range': share['quantity_estimate']['high']='61'
    elif defect=='percent_unit': share['metric']='Non-salary budget share, USD'
    elif defect=='operator': share['formula']='100+[3000,9000]/15000=[20,60].'
    elif defect=='expression': share['formula']='eval(100×[3000,9000]/15000)=[20,60].'
    elif defect=='dependencies': share['input_claim_ids']=['factor-cost','factor-resources']
    else:
        share['claim_id']='arbitrary-budget-share'
        next(i for i in checked.items if i.claim_id=='factor-budget-share').claim_id=share['claim_id']
    with pytest.raises(ValueError):
        validate_factor_verification(checked,FactorsStateV153.model_validate(raw),market='US',milestones=True,
                                     business_budget_usd=Decimal('15000'))


@pytest.mark.parametrize('identifier',['factor-cost','factor-scenario'])
def test_budget_share_does_not_promote_unresolved_independent_input(identifier):
    factors,checked=budget_share_fixture()
    next(i for i in checked.items if i.claim_id==identifier).status='insufficient'
    with pytest.raises(ValueError):
        validate_factor_verification(checked,factors,market='US',milestones=True,business_budget_usd=Decimal('15000'))


def test_budget_share_does_not_accept_context_rounded_percent_as_exact_arithmetic():
    factors,checked=budget_share_fixture()
    raw=factors.model_dump(mode='json')
    raw['cost']={'low':'1','high':'1'}
    next(c for c in raw['claims'] if c['claim_id']=='factor-cost')['quantity_estimate']=dict(raw['cost'])
    share=next(c for c in raw['claims'] if c['claim_id']=='factor-budget-share')
    rounded=str(Decimal(100)/Decimal(3))
    share.update(quantity_estimate={'low':rounded,'high':rounded},formula=f'100×[1,1]/3=[{rounded},{rounded}].')
    with pytest.raises(ValueError):
        validate_factor_verification(checked,FactorsStateV153.model_validate(raw),market='US',milestones=True,
                                     business_budget_usd=Decimal('3'))
