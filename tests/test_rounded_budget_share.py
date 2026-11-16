"""A displayed rounded ratio remains arithmetic on conditional, unchanged inputs."""
from copy import deepcopy
from decimal import Decimal

import pytest

from ai_builder.pipeline import FactorsStateV153, validate_factor_verification
from test_milestone_arithmetic_verification import budget_share_fixture


def rounded_fixture():
    factors,checked=budget_share_fixture()
    raw=factors.model_dump(mode='json')
    raw['cost']={'low':'4600','high':'9700'}
    cost=next(c for c in raw['claims'] if c['claim_id']=='factor-cost')
    cost.update(quantity_estimate=dict(raw['cost']),metric='USD, non-salary expenditure')
    scenario=next(c for c in raw['claims'] if c['claim_id']=='factor-scenario')
    scenario.update(claim_id='factor-operating-branch',neutral_text=(
        'The conditional model uses one full-time developer and one full-time sales/product person, '
        'each working five days per week, with a $15,000 non-salary budget and a 90-day experiment horizon. '
        'There are no existing customers. Access remains conditional.'))
    next(i for i in checked.items if i.claim_id=='factor-scenario').claim_id='factor-operating-branch'
    share=next(c for c in raw['claims'] if c['claim_id']=='factor-budget-share')
    share.update(metric='Percent of non-salary budget, rounded',quantity_estimate={'low':'31','high':'65'},
        formula='4600/15000×100 ≈ 31%; 9700/15000×100 ≈ 65%.',
        input_claim_ids=['factor-cost','factor-operating-branch'])
    return FactorsStateV153.model_validate(raw),checked


def test_explicit_whole_percent_rounding_preserves_factors_and_verifier_statuses():
    factors,checked=rounded_fixture(); before=deepcopy((factors.model_dump(),checked.model_dump()))
    validate_factor_verification(checked,factors,market='US',milestones=True,business_budget_usd=Decimal('15000'))
    assert before==(factors.model_dump(),checked.model_dump())
    assert all(i.status=='not_fact_checkable' for i in checked.items if i.claim_id in {'factor-cost','factor-operating-branch'})


@pytest.mark.parametrize('defect',['low','high','typed_percent','cost','lower_budget','upper_budget','both_budgets',
    'scenario_budget','scenario_negation','scenario_missing','unreviewed_scenario','unreviewed_cost',
    'wrong_unit','wrong_operator','exact_label','wrong_dependency'])
def test_rounded_display_cannot_hide_changed_input_or_arithmetic(defect):
    factors,checked=rounded_fixture();raw=factors.model_dump(mode='json')
    share=next(c for c in raw['claims'] if c['claim_id']=='factor-budget-share')
    cost=next(c for c in raw['claims'] if c['claim_id']=='factor-cost')
    scenario=next(c for c in raw['claims'] if c['claim_id']=='factor-operating-branch')
    if defect=='low':share['formula']=share['formula'].replace('31%','30%')
    elif defect=='high':share['formula']=share['formula'].replace('65%','64%')
    elif defect=='typed_percent':share['quantity_estimate']['high']='64'
    elif defect=='cost':cost['quantity_estimate']['low']='4500'
    elif defect=='lower_budget':share['formula']=share['formula'].replace('4600/15000','4600/15001')
    elif defect=='upper_budget':share['formula']=share['formula'].replace('9700/15000','9700/15001')
    elif defect=='both_budgets':share['formula']=share['formula'].replace('/15000','/15001')
    elif defect=='scenario_budget':scenario['neutral_text']=scenario['neutral_text'].replace('$15,000','$16,000')
    elif defect=='scenario_negation':scenario['neutral_text']='It is false that '+scenario['neutral_text']
    elif defect=='scenario_missing':scenario['neutral_text']='The budget has not been specified.'
    elif defect=='unreviewed_scenario':next(i for i in checked.items if i.claim_id=='factor-operating-branch').status='insufficient'
    elif defect=='unreviewed_cost':next(i for i in checked.items if i.claim_id=='factor-cost').status='insufficient'
    elif defect=='wrong_unit':cost['metric']='EUR, not USD, non-salary expenditure'
    elif defect=='wrong_operator':share['formula']=share['formula'].replace('×100','+100')
    elif defect=='exact_label':share['metric']='Non-salary budget share, percent'
    else:share['input_claim_ids']=['factor-cost','factor-resources']
    with pytest.raises(ValueError):
        validate_factor_verification(checked,FactorsStateV153.model_validate(raw),market='US',milestones=True,
                                     business_budget_usd=Decimal('15000'))


def test_rounding_uses_exact_half_up_without_intermediate_decimal_precision():
    factors,checked=rounded_fixture();raw=factors.model_dump(mode='json')
    raw['cost']={'low':'4575','high':'9675'}
    next(c for c in raw['claims'] if c['claim_id']=='factor-cost')['quantity_estimate']=dict(raw['cost'])
    share=next(c for c in raw['claims'] if c['claim_id']=='factor-budget-share')
    share['formula']='4575/15000×100 ≈ 31%; 9675/15000×100 ≈ 65%.'
    validate_factor_verification(checked,FactorsStateV153.model_validate(raw),market='US',milestones=True,
                                 business_budget_usd=Decimal('15000'))
