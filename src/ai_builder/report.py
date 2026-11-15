"""Private draft report. Public publication must use a separately validated projection."""
from pathlib import Path
from urllib.parse import urlsplit
from jinja2 import Environment, FileSystemLoader, select_autoescape


def render_report(scope, state, usage, *, mode):
    if mode not in {'live','eval','replay','development'}:
        raise ValueError('Unknown report mode')
    def source_url(value):
        p=urlsplit(value)
        return value if p.scheme in {'https','http'} and p.hostname and not p.username and not p.password else '#'
    env=Environment(loader=FileSystemLoader(Path(__file__).with_name('templates')),
                    autoescape=select_autoescape(['html']))
    env.filters['source_url']=source_url
    env.filters['money']=lambda micro:f'${micro/1000000:,.4f}'
    state={'criteria':[],'critique':None,'verification':{'items':[]},'reconciliation':{},**state}
    return env.get_template('report.html').render(scope=scope.model_dump(mode='json'),state=state,usage=usage,mode=mode)
