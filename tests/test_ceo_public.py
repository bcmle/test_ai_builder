"""The brief is bounded presentation; evidence and published payloads are intact."""
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from ai_builder.api import create_app
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
