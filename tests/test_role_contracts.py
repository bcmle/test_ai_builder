import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_builder.config import Configuration, load_config
from ai_builder.domain import Criterion, CriterionV14
from ai_builder.pipeline import ModelRoles, PREFIX, PROMPTS, prompt_bundle
from ai_builder.pricing import Pricing
from ai_builder.providers import validation_diagnostics

ROOT = Path(__file__).parents[1]


def test_legacy_config_and_prompt_hashes_survive_new_contracts():
    config = load_config(ROOT/'config/examples/prototype-config.json')
    pricing = Pricing.model_validate_json((ROOT/'config/examples/openai-pricing-2026-11-15.json').read_text())
    settings = json.loads((ROOT/'config/examples/role-settings.json').read_text())
    assert config.digest() == '98cfa932131ffd8a9241d5d0e5464cdda3b8db8863400d689e050316f3ed0a4c'
    assert all('allow_design_basis' not in item for item in config.model_dump()['criteria'])
    prefix, prompts = prompt_bundle('1.0')
    assert prefix == PREFIX and prompts == PROMPTS
    assert ModelRoles(None,pricing,'',settings,prompt_prefix=prefix,prompts=prompts).version() == (
        'cff2ed5c3dd9a6fb087e7af275ba815f7a790c8e370e0f0a0f84e474174af360')
    assert ModelRoles(None,pricing,'',settings,prompt_prefix=prompt_bundle('1.1')[0],
                      prompts=prompt_bundle('1.1')[1]).version() != ModelRoles(None,pricing,'',settings).version()


def test_new_design_basis_is_typed_and_legacy_schema_stays_strict():
    row = dict(criterion_id='F05',state='completed',result='pass',evidence_ids=[],reason='Conditional on prepaid operation',
               design_basis=[dict(kind='proposed_design',assessment_scope_id='scope',excerpt='No lending or guarantees.')])
    assert CriterionV14.model_validate(row).design_basis[0].kind == 'proposed_design'
    with pytest.raises(ValidationError):
        Criterion.model_validate(row)
    row['design_basis'] = []
    with pytest.raises(ValidationError,match='require external evidence'):
        CriterionV14.model_validate(row)


def test_design_basis_allowlist_cannot_enable_legal_or_legacy_assumptions(config):
    value = config.model_dump(mode='json')
    value['criteria'][1]['allow_design_basis'] = True
    with pytest.raises(ValidationError,match='limited'):
        Configuration.model_validate(value)
    value['schema_version'] = '1.1'
    assert Configuration.model_validate(value).criteria[1].allow_design_basis
    for index in (0, 2, 3, 9):
        illegal = json.loads(json.dumps(value))
        illegal['criteria'][index]['allow_design_basis'] = True
        with pytest.raises(ValidationError,match='limited'):
            Configuration.model_validate(illegal)


def test_validation_diagnostics_do_not_embed_provider_input():
    try:
        Criterion.model_validate(dict(criterion_id='F05',state='completed',result='pass',evidence_ids=[],
                                      reason='private source content sentinel'))
    except ValidationError as error:
        detail = validation_diagnostics(error)
        assert detail and set(detail[0]) == {'code','path','message'}
        assert 'private source content sentinel' not in json.dumps(detail)
