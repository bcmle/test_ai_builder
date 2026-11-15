from ai_builder.report import render_report
from test_pipeline import packet


def test_draft_report_escapes_untrusted_text_and_handles_incomplete_roles():
    scope=packet().model_copy(update={'neutral_model_description':'<script>alert(1)</script>'})
    state={'card':{'eligibility':'needs_review','score':None},'criteria':[]}
    usage=dict(calculated_micro=0,reserved_micro=0,unknown_micro=0,remaining_micro=25000000)
    html=render_report(scope,state,usage,mode='replay')
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'No score' in html and 'Not a live market assessment' in html
    assert 'Critic and Verifier were not run' in html


def test_runner_imports_without_starting_network_or_migrations():
    from ai_builder.run_scope import main
    assert callable(main)


def test_complete_card_renders_claims_and_verified_risks():
    state={'card':{'eligibility':'pass','score':{'status':'final','bounds':{'low':'50','high':'75'},'goal_feasibility':'feasible'}},
           'criteria':[], 'critique':{'risks':[{'risk_id':'r','impact':3,'premise_claim_ids':['c']}],
                                    'claims':[{'claim_id':'c','neutral_text':'A checked premise','kind':'fact','evidence_refs':[]}]},
           'verification':{'items':[{'claim_id':'c','status':'supported','reason':'Fixture source'}]},
           'reconciliation':{'r':'supported_basis'},
           'factors':{'factors':[],'days':{'low':30,'high':60},'cost':None,'claims':[]}}
    usage=dict(calculated_micro=30000,reserved_micro=0,unknown_micro=0,remaining_micro=24970000)
    html=render_report(packet(),state,usage,mode='replay')
    assert 'Verifier: supported' in html and '50–75/100' in html and '$0.0300' in html
