"""The brief is bounded presentation; evidence and published payloads are intact."""
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from ai_builder.api import _ceo_summary, create_app
from ai_builder.public import load_snapshot, public_model, publish_snapshot
from test_conditional_public import conditional_result
from test_public import metadata, snapshot


class CardHTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.brief = []
        self.ids = set()
        self.brief_links = []
        self.full_analysis = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        if attrs.get('id') == 'full-analysis':
            self.full_analysis = attrs
        if any(item[1] == 'decision-brief' for item in self.stack) and tag == 'a':
            self.brief_links.append(attrs.get('href', ''))
        if tag not in {'meta', 'link', 'input', 'br', 'hr', 'img'}:
            self.stack.append((tag, attrs.get('id')))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, data):
        if any(item[1] == 'decision-brief' for item in self.stack):
            self.brief.append(data)


def test_brief_is_bounded_judgment_first_and_preserves_entire_published_assessment(config, tmp_path):
    model = public_model(conditional_result(), metadata(), config)
    model['summary'] = 'This is a deliberately long proposed model description. ' * 12
    for field in ('buyer', 'revenue', 'comparable'):
        model[field] = f'The supplied {field} description is deliberately very long and remains fully accessible. ' * 4
    judgment = deepcopy(model['claims'][1])
    judgment.update(id='risk-judgment', text='Limited differentiation could prevent the first payment. '
                    'COMPLETE JUDGMENT RETAINED with its assumptions and caveats. ' * 8)
    model['claims'].append(judgment)
    model['risks'] = [dict(id=f'risk-{i}', text='LONG FACT INTRODUCTION ' * 30 + judgment['text'],
                          verification=None, claim_ids=['fact-a', 'risk-judgment']) for i in range(5)]
    model['claims'][1]['text'] = ('Proposed demand test. Buyer: an operations manager. '
        'First action on day 1: prepare a scope sheet identifying candidate states, document categories, '
        'template functions, data flows and settlement boundaries; request priced legal review. '
        'FULL PLAN REMAINS ACCESSIBLE. Stop if the required permission is refused.')
    publish_snapshot(tmp_path, {**snapshot([model]), 'scoring_policy_version': '1.5.2'})
    before = load_snapshot(tmp_path)
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        html = client.get('/models/model-a').text
        parsed = CardHTML(html)
        brief = ' '.join(' '.join(parsed.brief).split())
        assert len(brief.split()) <= 250, brief
        assert parsed.full_analysis is not None and 'open' not in parsed.full_analysis
        assert brief.count('Limited differentiation could prevent the first payment.') == 3
        assert 'LONG FACT INTRODUCTION' not in brief and 'COMPLETE JUDGMENT RETAINED' not in brief
        assert 'prepare a scope sheet identifying candidate states' in brief
        assert 'Proposed next step' in brief and 'FULL PLAN' not in brief
        assert '1 open checks: Lawful activity' in brief and 'Conditional assessment' in brief
        assert '…' in brief
        for original in (model['summary'], model['buyer'], model['revenue'], judgment['text'],
                         model['risks'][0]['text'], model['claims'][1]['text']):
            assert original in html
        assert all(link[1:] in parsed.ids for link in parsed.brief_links if link.startswith('#'))
        assert client.get('/runs/collection-a?format=json').json() == before


def test_unscored_brief_keeps_unknowns_and_legal_conditions_visible(config, tmp_path):
    result = conditional_result()
    result.state['card'].update(score=None, publication='draft')
    model = public_model(result, metadata(), config)
    publish_snapshot(tmp_path, {**snapshot([model]), 'scoring_policy_version': '1.5.2'})
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        html = client.get('/models/model-a').text
        parsed = CardHTML(html)
        brief = ' '.join(parsed.brief)
        assert 'No score yet' in brief and '4 scoring factors remain unknown.' in brief
        assert 'Lawful activity' in brief and 'open checks' in brief
        assert 'The vendor supplies subscription software.' not in brief
        assert 'The vendor supplies subscription software.' in html
        assert 'Reviewing documents' not in brief


def test_risk_excerpt_prefers_specific_judgments_over_repeated_scenario(config):
    model = public_model(conditional_result(), metadata(), config)
    template = next(claim for claim in model['claims'] if claim['kind'] == 'judgment')
    shared = {**template, 'id': 'shared-scenario', 'text': 'The assessment assumes a two-person team and no existing customers.'}
    sentences = [
        'First payment may miss day 90 because the team must find and convert a new buyer willing to prepay for a single-application permission review.',
        'Connector development and recommendation validation may delay first payment beyond day 90.',
        'Buyer authorization and vendor acceptance may prevent payment within 90 days even if the prototype works.',
    ]
    model['claims'].append(shared)
    model['risks'] = []
    for index, sentence in enumerate(sentences):
        claim = {**template, 'id': f'specific-{index}', 'text': sentence + ' Full reasoning remains in the passport.'}
        model['claims'].append(claim)
        model['risks'].append(dict(id=f'risk-{index}', text=shared['text'] + claim['text'],
                                  claim_ids=[shared['id'], claim['id']]))
    before = deepcopy(model)
    brief = _ceo_summary(model)
    assert [risk['text'] for risk in brief['risks']] == [sentence + ' …' for sentence in sentences]
    assert [risk['href'] for risk in brief['risks']] == [f'#claim-specific-{i}' for i in range(3)]
    assert model == before


def test_conditional_risk_excerpt_preserves_complete_condition_and_consequence(config):
    model = public_model(conditional_result(), metadata(), config)
    sentence = ('If the first buyer requires a working demonstration of its subscription rules and usage inputs '
                'before prepaying, payment is conditional on completion of the necessary data mapping, processor '
                'integration and invoice testing.')
    claim = next(claim for claim in model['claims'] if claim['kind'] == 'judgment')
    claim['text'] = sentence + ' Whether that work fits within the 90-day launch period is unknown.'
    model['risks'] = [dict(id='risk', text=claim['text'], claim_ids=[claim['id']])]
    assert _ceo_summary(model)['risks'][0]['text'] == sentence + ' …'


@pytest.mark.parametrize('prefix,action', [
    ('The first action is ', 'preparation of a one-page workflow specification, followed by requests for documented legal review and supplier confirmation.'),
    ('At assessment start, the first action would be to ', 'prepare the proposed state, contract, data-flow and payment-flow description, request scoped review, and begin compiling prospective business contacts.'),
    ("The conditional experiment's first action is ", 'a written scope sheet identifying candidate states, one application, intended data fields, access method and payment flow.'),
    ('The conditional experiment begins by ', 'specifying candidate states, inventorying intended components and telemetry fields, documenting money flow, and seeking scoped review and onboarding requirements.'),
    ('First action on day 1: ', 'prepare a scope sheet and request priced review.'),
])
def test_next_step_finds_action_across_all_plan_claims(config, prefix, action):
    model = public_model(conditional_result(), metadata(), config)
    template = next(claim for claim in model['claims'] if claim['kind'] == 'judgment')
    template['text'] = 'This is a proposed conditional experiment within the supplied 90-day window, not a forecast.'
    readiness = {**template, 'id': 'readiness', 'text': prefix + action + ' Full conditions remain applicable.'}
    model['claims'].append(readiness)
    model['test_plan_claim_ids'] = [template['id'], readiness['id']]
    assert _ceo_summary(model)['next_step'] == dict(text=action + ' …', href='#claim-readiness')


@pytest.mark.parametrize('policy', ['1.5.2', '1.5.3'])
def test_three_model_comparison_reuses_brief_and_keeps_full_analysis_linked(config, tmp_path, policy):
    from test_factor_revision_public import replace_selected, revision_catalog

    if policy == '1.5.3':
        _, value = revision_catalog(config, tmp_path)
        models = [replace_selected(value, config, index) for index in range(3)]
        models[0]['factors'][0]['name'] = 'Saved customer access label'
    else:
        models = [public_model(conditional_result(), metadata(f'model-{i}'), config) for i in range(3)]
        value = {**snapshot(models), 'scoring_policy_version': policy}
    for model in models:
        model['summary'] = 'A supplied description with many operational details. ' * 25
        model['limitations'] = ['FULL LIMITATION REMAINS IN THE CARD. ' * 30]
        model['eligibility_conditions'][0]['assumption'] = 'FULL LEGAL ASSUMPTION. ' * 30
        model['eligibility_conditions'][0]['required_check'] = 'FULL REQUIRED LEGAL CHECK. ' * 30
        judgment = deepcopy(next(claim for claim in model['claims'] if claim['kind'] == 'judgment'))
        judgment.update(id='risk-judgment', text='Differentiation may delay payment. FULL RISK REASONING. ' * 20)
        model['claims'].append(judgment)
        model['risks'] = [dict(id=f'risk-{i}', text='LONG FACT INTRODUCTION ' * 30 + judgment['text'],
                              verification=None, claim_ids=['risk-judgment']) for i in range(5)]
    publish_snapshot(tmp_path, value)
    before = load_snapshot(tmp_path)
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        response = client.get(f"/compare?run={value['run_id']}&revision={value['revision_id']}&models=model-0,model-1,model-2")
        assert response.status_code == 200
        html = response.text
        for full_text in ('FULL LIMITATION', 'FULL LEGAL ASSUMPTION', 'FULL REQUIRED LEGAL CHECK',
                          'LONG FACT INTRODUCTION', 'FULL RISK REASONING'):
            assert full_text not in html
        assert html.count('Differentiation may delay payment.') == 9
        assert html.count('1 open checks: Lawful activity') == 3
        assert html.count('Conditional assessment') == 3
        assert '#full-analysis' in html and '#conditions' in html and '#legal-reviews' in html
        assert '#claim-risk-judgment' in html and '#sources' in html
        if policy == '1.5.3':
            assert 'Cycle from ready offer to first payment' in html
            assert 'Saved customer access label' in html
            assert 'Time to launch a test' not in html
        class Columns(HTMLParser):
            def __init__(self):
                super().__init__()
                self.columns = ['', '', '']
                self.column = 0
                self.inside = False
            def handle_starttag(self, tag, attrs):
                if tag == 'tr': self.column = 0
                if tag == 'td': self.inside = True
            def handle_endtag(self, tag):
                if tag == 'td':
                    self.inside = False
                    self.column += 1
            def handle_data(self, text):
                if self.inside: self.columns[self.column] += ' ' + text
        parser = Columns()
        parser.feed(html)
        assert all(len(column.split()) <= 250 for column in parser.columns)
        linked = client.get(f"/models/model-0?run={value['run_id']}").text
        assert 'FULL LIMITATION' in linked and 'FULL REQUIRED LEGAL CHECK' in linked
        assert 'id="claim-risk-judgment"' in linked
        assert client.get(f"/runs/{value['run_id']}?format=json").json() == before


@pytest.mark.skipif(shutil.which('node') is None, reason='JavaScript runtime is not installed')
def test_hash_navigation_reveals_nested_disclosures_on_load_click_and_history():
    script = Path('src/ai_builder/static/public.js').read_text()
    harness = r"""
const assert = require('node:assert/strict');
const outer = {tagName:'DETAILS', open:false, parentElement:null};
const nested = {tagName:'DETAILS', open:false, parentElement:outer};
const target = {tagName:'DETAILS', open:false, parentElement:nested, scrollIntoView(){this.scrolled=true;}};
const listeners = {};
const link = {getAttribute(){return '#claim-example';}, addEventListener(type, fn){listeners.click=fn;}};
global.document = {
  querySelector(){return null;},
  querySelectorAll(selector){return selector === 'a[href^="#"]' ? [link] : [];},
  getElementById(id){return ['claim-example','source-example'].includes(id) ? target : null;}
};
global.window = {addEventListener(type, fn){listeners[type]=fn;}};
global.location = {hash:'#source-example'};
global.requestAnimationFrame = fn => fn();
"""
    checks = r"""
const check = () => [outer,nested,target].forEach(element => assert.equal(element.open,true));
check(); assert.equal(target.scrolled,true);
[outer,nested,target].forEach(element => element.open=false); listeners.click(); check();
[outer,nested,target].forEach(element => element.open=false); listeners.hashchange(); check();
location.hash='#%broken'; listeners.hashchange();
"""
    result = subprocess.run([shutil.which('node'), '-'], input=harness + '\n' + script + '\n' + checks,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
