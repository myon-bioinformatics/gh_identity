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


def test_live_wiki_paragraph_text():
    fixture=Path(__file__).parent/'fixtures/browser_dom/wiki-paragraph.html'
    result=ghi.html_content(fixture.read_text(),['p'],source_kind='dom')
    assert result['body']=='OBS Studio is a free and open source software for video recording and live streaming.'


@pytest.mark.parametrize('body',['x','x\n','x\n\n','\nx\n','\t日本語\n  x\n'])
def test_selected_pre_preserves_all_whitespace(body):
    assert ghi.html_content('<pre>'+body+'</pre>',['pre'])['body']==body


def test_live_wiki_link_list_text_order():
    fixture=Path(__file__).parent/'fixtures/browser_dom/wiki-links.html'
    result=ghi.html_content(fixture.read_text(),['ul'],source_kind='dom')
    expected='Knowledge Base\nOBS Studio Discord\nOBS Community Chat\nOBS Studio Forum'
    assert result['body'].split()==expected.split()
