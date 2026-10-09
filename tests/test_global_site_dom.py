"""Recorded small DOM regions; site-independent extraction, no live requests."""
import json
from pathlib import Path
import subprocess
import sys
import pytest
import gh_identity as ghi

CASES=json.loads((Path(__file__).parent/'fixtures/browser_dom/global-sites.json').read_text(encoding='utf-8'))['cases']

@pytest.mark.parametrize('case',CASES,ids=[c['name'] for c in CASES])
def test_global_dom_via_cli(case,tmp_path):
    path=tmp_path/'capture.html';path.write_text(case['html'],encoding='utf-8')
    run=subprocess.run([sys.executable,ghi.__file__,'html-content',str(path),'--selector',case['selector'],'--source-kind','dom'],capture_output=True,text=True)
    assert run.returncode==0,run.stderr
    body=json.loads(run.stdout)['body']
    if case['comparison']=='exact':
        assert body==case['visible']
        assert '\u200b' in body
    else:
        assert body.split()==case['visible'].split()
    assert '#ea4335' not in body
    assert 'open-in-new' not in body
    assert 'a0b - 100vh' not in body
