"""Replay actual browser DOM fragments without repeat GitHub access."""
import json
from pathlib import Path
import subprocess
import sys
import pytest
import gh_identity as ghi

CAPTURE=json.loads((Path(__file__).parent/'fixtures/browser_dom/file-code.json').read_text())

@pytest.mark.parametrize('case',CAPTURE['cases'])
def test_live_file_preview_dom_matches_browser_text(case,tmp_path):
    path=tmp_path/'captured.html'
    path.write_text(case['html'],encoding='utf-8')
    result=subprocess.run([sys.executable,ghi.__file__,'html-content',str(path),
                           '--selector','pre','--source-kind','dom'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    observation=json.loads(result.stdout)
    assert observation['body']==case['visible']
    assert observation['source_kind']=='dom'
