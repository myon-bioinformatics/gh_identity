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


@pytest.mark.parametrize('style,hidden', [
    ('display:none;display:inline', False),
    ('display:inline;display:none', True),
    ('display:none !important;display:inline', True),
    ('display:none;display:inline !important', False),
    ('display:none!important;display:inline!important', False),
    ('display:inline!important;display:none!important', True),
    (' DISPLAY : none ! IMPORTANT ; DISPLAY : block ', True),
    ('visibility:hidden;visibility:visible', False),
    ('visibility:visible;visibility:hidden', True),
    ('visibility:hidden!important;visibility:visible', True),
    ('visibility:hidden!important;visibility:visible!important', False),
    ('display:none;visibility:visible', True),
    ('display:block;visibility:hidden', True),
    ('display: /* explanation */ none', True),
    ('display:none;/* display:block */', True),
    ('--sample:";display:none;";display:inline', False),
    ('--sample:func(;display:none;)', False),
    ('display:none;display:', True),
])
def test_inline_declaration_order_and_importance(style, hidden):
    from html import escape
    html='<article id="body">before<span style="'+escape(style, quote=True)+'">MARKER</span>after</article>'
    out=ghi.html_content(html,['#body'])
    assert ('MARKER' not in out['body']) is hidden
    assert 'before' in out['body'] and 'after' in out['body']
    # Candidate selection and rendering must share the same decision.
    selected='<article id="body" style="'+escape(style, quote=True)+'">MARKER</article>'
    if hidden:
        with pytest.raises(ghi.Error,match='html_body_not_found'):
            ghi.html_content(selected,['#body'])
    else:
        assert ghi.html_content(selected,['#body'])['body']=='MARKER'


@pytest.mark.parametrize('include_controls',[False,True])
def test_structural_aria_hidden_contract_is_not_visual_text(include_controls):
    html='<main id="body"><span aria-hidden="true" style="display:inline">visual icon</span><span>body</span></main>'
    out=ghi.html_content(html,['#body'],include_controls=include_controls)
    assert out['body']=='body'
    assert out['visibility']=='structural_only'
