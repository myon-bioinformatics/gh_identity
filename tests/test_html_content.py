import json
from pathlib import Path
import subprocess
import sys
import pytest
import gh_identity as ghi

HTML='''<html><head><style>#body {color:red}</style></head><body>
<nav>Repository menu</nav><article id="description"><p>日本語 &amp; <strong>本文</strong></p>
<style>.x { color: red }</style><script>evil()</script><p hidden>secret</p>
<div aria-hidden="true">hidden</div><div style="display:none">invisible</div>
<pre>def main():\n    return "#CSS"\n</pre><p><a href="/">リンク</a><br>次行</p>
<img alt="説明"><button>Copy</button></article><article class="comment-body">別コメント</article></body></html>'''


def test_structural_text_and_code():
    out=ghi.html_content(HTML,['#missing','#description'])
    assert out['selector']=='#description'
    assert '日本語 & 本文' in out['body']
    assert 'def main():\n    return "#CSS"\n' in out['body']
    assert 'リンク\n次行' in out['body']
    for unwanted in ['Repository menu','color:red','color: red','evil()','secret','hidden','invisible','Copy','別コメント']:
        assert unwanted not in out['body']
    assert out['identity_verified'] is False
    assert out['visibility']=='structural_only'


def test_ambiguous_candidate_does_not_fall_back():
    with pytest.raises(ghi.Error,match='ambiguous_html_body'):
        ghi.html_content('<div class="body">one</div><div class="body">two</div><p id="other">x</p>', ['.body','#other'])


def test_no_page_fallback_or_hidden_candidate():
    with pytest.raises(ghi.Error,match='html_body_not_found'):
        ghi.html_content('<div hidden><p id="body">hidden</p></div><main>Sign in</main>',['#body'])


@pytest.mark.parametrize('selector',['div p','[id=body]','', '#body > p'])
def test_unsupported_selectors(selector):
    with pytest.raises(ValueError):ghi.html_content(HTML,[selector])


def test_limits():
    with pytest.raises(ValueError):ghi.html_content(HTML,['#description'],max_bytes=3)
    with pytest.raises(ValueError):ghi.html_content('<div>'*260,['div'])


def test_cli_saved_dom(tmp_path):
    path=tmp_path/'page.html';path.write_text(HTML,encoding='utf-8')
    cli=Path(ghi.__file__)
    run=subprocess.run([sys.executable,str(cli),'html-content',str(path),'--selector','#description','--source-kind','dom'],capture_output=True,text=True)
    assert run.returncode==0,run.stderr
    assert json.loads(run.stdout)['body']==ghi.html_content(HTML,['#description'])['body']
    bad=subprocess.run([sys.executable,str(cli),'html-content',str(path),'--selector','#absent'],capture_output=True,text=True)
    assert bad.returncode==2
