from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from ai_builder.config import Configuration,load_config
from ai_builder.domain import Interval,rounded_days,timing_factor,total_payment_days
from ai_builder.pipeline import prompt_bundle,UpstreamInheritance

ROOT=Path(__file__).parents[1]


@pytest.mark.parametrize('low,high,rounded,score',[
    ('0','14',('0','14'),('4','4')),
    ('13.2','14.1',('14','15'),('3','4')),
    ('30.1','60',('31','60'),('2','2')),
    ('60.1','90',('61','90'),('1','1')),
    ('90','90.01',('90','91'),('0','1')),
    ('91','120',('91','120'),('0','0')),
])
def test_day_anchors_use_separate_conservative_ceil_intervals(low,high,rounded,score):
    raw=Interval(low=low,high=high)
    assert rounded_days(raw)==Interval(low=rounded[0],high=rounded[1])
    assert timing_factor(raw).model_dump(mode='json')==dict(low=score[0],high=score[1])
    assert raw==Interval(low=low,high=high)


def test_total_sums_rounded_components_without_double_counting_or_filling_unknowns():
    first=Interval(low='13.2',high='14.1');second=Interval(low='15.1',high='29.1')
    assert total_payment_days(first,second)==Interval(low=30,high=45)
    assert total_payment_days(None,second) is None
    assert total_payment_days(first,None) is None
    assert rounded_days(None) is None and timing_factor(None) is None


def test_new_factor_definitions_preserve_original_upstream_prompts_and_legacy_configs():
    old=load_config(ROOT/'config/conditional-config.json');new=load_config(ROOT/'config/milestone-config.json')
    assert new.scoring_policy_version=='1.5.3' and new.schema_version=='1.3'
    assert new.scenario==old.scenario and new.criteria==old.criteria and new.limits==old.limits
    assert [(f.id,f.weight,f.anchors) for f in new.factors]==[(f.id,f.weight,f.anchors) for f in old.factors]
    assert all(f.name and f.definition and f.unit for f in new.factors)
    assert all(set(f.model_dump(mode='json'))=={'id','weight','anchors'} for f in old.factors)
    previous_prefix,previous=prompt_bundle('1.2');prefix,current=prompt_bundle('1.3')
    assert prefix==previous_prefix
    for role in ('research','applicability','critic','neutrality','repair','verifier'):
        assert current[role]==previous[role]
    assert current['factors']!=previous['factors']
    assert set(current)-set(previous)=={'factor_neutrality'}
    assert 'operating stop rules are permitted' in current['factor_neutrality']
    assert 'instructions addressed to the verifier/agent' in current['factor_neutrality']


@pytest.mark.parametrize('defect',['wrong_boundary','wrong_unit','missing_definition','changed_anchors','changed_weights','legacy_metadata'])
def test_milestone_configuration_cannot_hide_definition_or_formula_changes(defect):
    raw=load_config(ROOT/'config/milestone-config.json').model_dump(mode='json')
    if defect=='wrong_boundary':raw['factors'][3]['start_event']='assessment_start'
    elif defect=='wrong_unit':raw['factors'][2]['unit']='weeks'
    elif defect=='missing_definition':raw['factors'][2]['definition']=''
    elif defect=='changed_anchors':raw['factors'][2]['anchors'][4]='≤30 days'
    elif defect=='changed_weights':raw['factors'][2]['weight']='25';raw['factors'][3]['weight']='15'
    else:raw['schema_version']='1.2';raw['scoring_policy_version']='1.5.2'
    with pytest.raises(ValueError):Configuration.model_validate(raw)


def test_inheritance_is_only_an_exact_upstream_prefix_without_factor_or_card_results():
    assert UpstreamInheritance(manifest_hash='hash',parent_run_id='parent',completed_through=None,state={}).state=={}
    for state in ({'factors':{}},{'card':{}},{'factor_neutral':True},{'research':{}}):
        with pytest.raises(ValueError):
            UpstreamInheritance(manifest_hash='hash',parent_run_id='parent',completed_through=None,state=state)
    with pytest.raises(ValueError,match='missing'):
        UpstreamInheritance(manifest_hash='hash',parent_run_id='parent',completed_through='reconcile',state={})
