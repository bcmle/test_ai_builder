"""Current HTML rankings include completed recovery cards without rewriting JSON."""
from copy import deepcopy
import json
import re

from fastapi.testclient import TestClient

from ai_builder.api import create_app
from ai_builder.public import GOALS, PublicSnapshot, public_model
from test_conditional_public import conditional_result, versioned_snapshot
from test_factor_revision_public import revision_catalog, replace_selected
from test_public import metadata, result_fixture, snapshot


def write_saved_snapshot(directory, value):
    """Keep the saved ranks exactly as supplied, like targeted recovery publication."""
    value = PublicSnapshot.model_validate(value).model_dump(mode='json')
    public = directory / 'public'
    public.mkdir(exist_ok=True)
    (public / f"{value['run_id']}.json").write_text(json.dumps(value, indent=2))
    (public / 'latest.json').write_text(json.dumps({'run_id': value['run_id']}))
    return value


def scored_model(config, identifier, value, *, rank=None, goal='uncertain', conditional=True, title=None):
    model = public_model(conditional_result(conditional=conditional), metadata(identifier), config)
    model.update(title=title or identifier, rank=rank)
    model['score'].update(value=value, low=value, high=value + 10, goal_feasibility=goal)
    return model


def ranking_rows(html, section, goal=None):
    content = re.search(r'<section\b[^>]*id="' + section + r'"[^>]*>(.*?)</section>', html, re.S)
    assert content, section
    content = content.group(1)
    if goal is not None:
        content = next((group for group in content.split('<div class="rank-group">')
                        if group.startswith(f'<h3>{GOALS[goal]}</h3>')), '')
    return [(identifier, int(place)) for identifier, place in re.findall(
        r'<a class="rank-row" href="/models/([^?]+)\?run=[^"]+"><span class="rank-number">(\d+)</span>',
        content)]


def public_files(directory):
    return {str(path.relative_to(directory)): path.read_bytes() for path in (directory / 'public').rglob('*.json')}


def test_five_completed_conditional_cards_render_despite_two_unsaved_places(config, tmp_path):
    models = [scored_model(config, f'model-{index}', value, rank=rank)
              for index, (value, rank) in enumerate(zip((46.25, 50, 51.25, 56.25, 62.5), (1, 2, 3, None, None)))]
    saved = write_saved_snapshot(tmp_path, versioned_snapshot(models))
    files_before = public_files(tmp_path)
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        before = client.get('/runs/reassessment-a?format=json').json()
        html = client.get('/runs/reassessment-a').text
        assert '5 conditional assessments' in html
        assert ranking_rows(html, 'conditional-ranking') == [(f'model-{i}', i + 1) for i in range(5)]
        assert 'id="final-ranking"' not in html
        assert client.get('/runs/reassessment-a?format=json').json() == before == saved
        assert [m['rank'] for m in before['models']] == [1, 2, 3, None, None]
    assert public_files(tmp_path) == files_before


def test_versioned_display_keeps_categories_feasibility_and_unrounded_ties_separate(config, tmp_path):
    models = [
        scored_model(config, 'tie-z', 40.04, title='Zulu'),
        scored_model(config, 'tie-a', 40.04, title='Alpha'),
        scored_model(config, 'higher', 40.049, title='Aardvark'),
        scored_model(config, 'feasible', 5, goal='feasible'),
        scored_model(config, 'outside', 30, goal='infeasible'),
        scored_model(config, 'confirmed', 20, conditional=False),
    ]
    write_saved_snapshot(tmp_path, versioned_snapshot(models))
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        html = client.get('/runs/reassessment-a').text
        assert ranking_rows(html, 'conditional-ranking', 'uncertain') == [('tie-a', 1), ('tie-z', 1), ('higher', 3)]
        assert ranking_rows(html, 'conditional-ranking', 'feasible') == [('feasible', 1)]
        assert ranking_rows(html, 'conditional-ranking', 'infeasible') == [('outside', 1)]
        assert ranking_rows(html, 'final-ranking') == [('confirmed', 1)]


def test_current_ranking_excludes_retained_provisional_unscored_and_ineligible(config, tmp_path):
    _, value = revision_catalog(config, tmp_path)
    replace_selected(value, config, 0)
    provisional = replace_selected(value, config, 1)
    provisional.update(eligible_for_ranking=False, rank=None)
    provisional['score']['status'] = 'provisional'
    unscored = replace_selected(value, config, 2)
    unscored.update(status='unresolved', eligible_for_ranking=False, score=None, rank=None)
    ineligible = replace_selected(value, config, 3)
    ineligible.update(eligible_for_ranking=False, rank=None)
    # model-4 remains a queued retained assessment; all 15 unselected cards retain old final scores.
    saved = write_saved_snapshot(tmp_path, value)
    files_before = public_files(tmp_path)
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        html = client.get('/runs/factor-revision').text
        assert ranking_rows(html, 'conditional-ranking') == [('model-0', 1)]
        assert 'id="final-ranking"' not in html
        assert client.get('/runs/factor-revision?format=json').json() == saved
    assert public_files(tmp_path) == files_before


def test_legacy_history_retains_stored_places_and_json(config, tmp_path):
    legacy = [public_model(result_fixture(), metadata(identifier), config)
              for identifier in ('legacy-first', 'legacy-second', 'legacy-unplaced')]
    for model, value, rank in zip(legacy, (10, 80, 20), (7, 2, None)):
        model['score'].update(value=value, low=value, high=value + 10)
        model['rank'] = rank
    saved = write_saved_snapshot(tmp_path, snapshot(legacy))
    write_saved_snapshot(tmp_path, versioned_snapshot([scored_model(config, 'current', 50)]))
    files_before = public_files(tmp_path)
    before = deepcopy(saved)
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        html = client.get('/runs/collection-a').text
        assert ranking_rows(html, 'final-ranking') == [('legacy-second', 2), ('legacy-first', 7)]
        assert client.get('/runs/collection-a?format=json').json() == before
    assert public_files(tmp_path) == files_before
