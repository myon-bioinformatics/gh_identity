"""DOM-derived structural cases; no login, chat submission or network access."""
import json
from pathlib import Path
import subprocess
import sys
import pytest
import gh_identity as ghi

CASES=json.loads((Path(__file__).parent/'fixtures/browser_dom/chat-surfaces.json').read_text(encoding='utf-8'))['cases']

@pytest.mark.parametrize('case',CASES,ids=[c['name'] for c in CASES])
def test_chat_surfaces_cli(case,tmp_path):
    path=tmp_path/'page.html';path.write_text(case['html'],encoding='utf-8')
    args=[sys.executable,ghi.__file__,'html-content',str(path),'--selector',case['selector'],'--source-kind','dom']
    if case.get('include_controls'):args.append('--include-controls')
    run=subprocess.run(args,capture_output=True,text=True)
    assert run.returncode==0,run.stderr
    assert json.loads(run.stdout)['body']==case['expected']


def test_accordion_is_opt_in_and_data_hidden_is_not_hidden():
    case=CASES[2]
    assert ghi.html_content(case['html'],['h3'])['body']==''
    assert ghi.html_content(case['html'],['h3'],include_controls=True)['body']==case['expected']
    assert ghi.html_content('<h3><button hidden>hidden</button>visible</h3>',['h3'],include_controls=True)['body']=='visible'


def test_control_option_requires_boolean():
    with pytest.raises(ValueError):ghi.html_content('<p>x</p>',['p'],include_controls='yes')
