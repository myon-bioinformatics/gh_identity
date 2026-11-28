"""📄 Content and search: REST bodies, recorded responses, issue/PR search, HTML/DOM extraction.

Sections (each was one file; `-k` selects inside them): issue_search, content, content_snapshot_cli, html_content, html_page_patterns, browser_dom, simple_site_dom, global_site_dom, chat_surface_dom.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.parse import urlsplit
import json
import os
import sys

import pytest

from conftest import FIXTURES, ROOT, SHA

import gh_identity as g


def load_vendored(name, relpath):
    """Load a vendored file by path under a repo-unique name (cli_args README: Loading a vendored copy)."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / relpath)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


run_cli = load_vendored("gh_identity_cli_args", "vendor/cli_args.py").run_cli


# ──── issue_search ────────────────────────────────────────────────

# Issue/PR discovery: identity, bounds, partial search, CLI and transports.


def row(n=1, kind='issue', repository='owner/repo', **changes):
    value = dict(number=n, repository_url=g.API + '/repos/' + repository,
                 html_url=f'https://github.com/{repository}/{"pull" if kind == "pr" else "issues"}/{n}',
                 title='日本語 🧪', body='本文\nsecond line', state='open',
                 user={'login': 'author'}, created_at='2026-01-01T00:00:00Z')
    if kind == 'pr':
        value['pull_request'] = {'url': f'{g.API}/repos/{repository}/pulls/{n}'}
    return dict(value, **changes)


def response(items, total=None, incomplete=False):
    return dict(items=items, total_count=len(items) if total is None else total,
                incomplete_results=incomplete)


def test_issue_body_identity_and_pr_rejection():
    with patch.object(g, 'request', return_value=row()) as call:
        out = g.issue('owner/repo', 1)
    assert out['body'] == '本文\nsecond line'
    assert out['repository'] == 'owner/repo' and out['state'] == 'open'
    assert call.call_args.args == ('GET', 'repos/owner/repo/issues/1')
    for bad in (row(kind='pr'), row(2), row(repository='other/repo'), row(state='oops'),
                row(html_url='https://evil.invalid/'), row(number=True), row(body=[])):
        with patch.object(g, 'request', return_value=bad), pytest.raises(g.Error):
            g.issue('owner/repo', 1)


def test_issue_list_excludes_prs_and_continues_old_pages():
    first = [row(n, 'pr') for n in range(1, 101)]
    with patch.object(g, 'request', side_effect=[first, [row(101)]]) as call:
        out = g.issues('owner/repo', state='all')
    assert out['complete'] and out['pages_fetched'] == 2
    assert [r['number'] for r in out['issues']] == [101]
    assert 'state=all' in call.call_args_list[0].args[1]


def test_list_limits_links_and_duplicates():
    with patch.object(g, 'request', return_value=[row(1), row(2)]):
        out = g.issues('owner/repo', max_items=1)
    assert out['truncated'] and out['count'] == 1
    with patch.object(g, 'request', return_value=g.Page([row()], {})):
        assert g.issues('owner/repo')['complete']
    with patch.object(g, 'request', return_value=[row(), row()]), pytest.raises(g.Error, match='pagination_incomplete'):
        g.issues('owner/repo')
    with patch.object(g, 'request', return_value=[row(n, 'pr') for n in range(1, 101)]):
        assert g.issues('owner/repo', max_pages=1)['truncated']


@pytest.mark.parametrize('kind', ['issue', 'pr'])
@pytest.mark.parametrize('transport', ['gh', 'urllib'])
def test_search_shared_normalization_and_encoded_query(kind, transport):
    query = 'user:owner is:closed label:"日本語 label" author:someone'
    with patch.object(g, '_gh' if transport == 'gh' else '_url', return_value=response([row(kind=kind)])) as call:
        out = g.search(query, kind=kind, sort='created', order='asc', transport=transport)
    params = parse_qs(urlsplit(call.call_args.args[1]).query)
    assert params['q'] == [query + ' is:' + kind]
    assert params['sort'] == ['created'] and params['order'] == ['asc']
    assert out['complete'] and out['scope'] == 'accessible_search_results'
    assert out['items'][0]['title'] == '日本語 🧪'
    assert 'body' not in out['items'][0]


def test_auto_fallback_and_cross_repository_identity():
    with patch.object(g, 'gh_available', return_value=True), patch.object(g, '_gh', side_effect=g.Error('authentication_required')), patch.object(g, '_url', return_value=response([row(kind='pr'), row(kind='pr', repository='owner/other')])):
        out = g.search('user:owner is:open')
    assert {r['repository'] for r in out['items']} == {'owner/repo', 'owner/other'}


def test_search_pagination_and_truncation():
    first = [row(n, 'pr') for n in range(1, 101)]
    with patch.object(g, 'request', side_effect=[response(first, 101), response([row(101, 'pr')], 101)]):
        out = g.search('org:example', max_items=200)
    assert out['complete'] and out['count'] == 101
    with patch.object(g, 'request', return_value=response(first, 101)):
        assert g.search('org:example', max_pages=1)['truncated']
        assert g.search('org:example', max_items=50)['count'] == 50
    with patch.object(g, 'request', return_value=response([], 0)):
        assert g.search('org:example')['complete']
    with patch.object(g, 'request', return_value=response([row(kind='pr')], 10)):
        assert not g.search('org:example')['complete']
    with patch.object(g, 'request', return_value=response([row(kind='pr')], incomplete=True)):
        out = g.search('org:example')
    assert out['incomplete_results'] and out['truncated'] and not out['complete']


def test_thousand_search_cap():
    batches = [response([row(n, 'pr') for n in range(p * 100 + 1, (p + 1) * 100 + 1)], 1001) for p in range(10)]
    with patch.object(g, 'request', side_effect=batches) as call:
        out = g.search('org:example', max_items=2000, max_pages=20)
    assert call.call_count == 10 and out['count'] == 1000 and out['truncated']


@pytest.mark.parametrize('bad', [None, {}, response([], True), response([], incomplete='false'),
    response([row(kind='pr')], 0), response([row()]), response([row(kind='pr'), row(kind='pr')])])
def test_invalid_search_observations(bad):
    with patch.object(g, 'request', return_value=bad), pytest.raises(g.Error):
        g.search('user:owner')


def test_changed_total_fails_and_budget_counts_filtered_prs():
    first = [row(n, 'pr') for n in range(1, 101)]
    with patch.object(g, 'request', side_effect=[response(first, 101), response([], 100)]), pytest.raises(g.Error, match='pagination_incomplete'):
        g.search('user:owner', max_items=200)
    with patch.object(g, 'request', return_value=first), g.operation(max_items=50), pytest.raises(g.Error, match='items_limit'):
        g.issues('owner/repo')
    with patch.object(g, 'request', return_value=response(first, 101)), g.operation(max_pages=1), pytest.raises(g.Error, match='pages_limit'):
        g.search('user:owner', max_items=200)


@pytest.mark.parametrize('fn,args,kwargs', [
    (g.issue, ('owner/repo', True), {}), (g.issue, ('owner/repo', 0), {}),
    (g.issues, ('owner/repo',), {'state': 'merged'}),
    (g.search, ('',), {}), (g.search, ('x',), {'kind': 'all'}),
    (g.search, ('x',), {'sort': 'bad'}), (g.search, ('x',), {'order': 'bad'}),
    (g.search, ('x',), {'max_items': True}), (g.issues, ('owner/repo',), {'max_pages': 0})])
def test_invalid_inputs_before_transport(fn, args, kwargs):
    with patch.object(g, 'request') as call, pytest.raises(ValueError):
        fn(*args, **kwargs)
    call.assert_not_called()


@pytest.mark.parametrize('argv,payload,key', [
    (['issue', 'owner/repo', '1'], row(), 'body'),
    (['issues', 'owner/repo', '--state', 'closed'], [row(state='closed')], 'issues'),
    (['search', 'org:owner is:open', '--kind', 'pr', '--sort', 'created', '--limit', '10'], response([row(kind='pr')]), 'items'),
    (['prs', 'owner/repo', '--state', 'all'], [], 'pull_requests')])
def test_cli(argv, payload, key, capsys):
    with patch.object(g, 'request', return_value=payload):
        assert g.main(argv) == 0
    assert key in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize('code', ['permission_or_rate_limit', 'not_found_or_inaccessible', 'invalid_json', 'operation_timeout'])
def test_transport_failures_never_empty_success(code, capsys):
    with patch.object(g, 'request', side_effect=g.Error(code)):
        assert g.main(['search', 'user:owner']) == 2
    assert json.loads(capsys.readouterr().err)['error'] == code


# ──── content ─────────────────────────────────────────────────────

# Exact content selection, search handoff and shared bounded transports.


def commit(**changes):
    return dict(sha=SHA, html_url=f'https://github.com/owner/repo/commit/{SHA}',
                commit={'message': '件名 🧪\n\n本文\r\n末尾\n'}, **changes)


@pytest.mark.parametrize('transport', ['gh', 'urllib'])
@pytest.mark.parametrize('kind', ['pr', 'issue', 'commit'])
def test_full_content_transport_parity(kind, transport):
    data = commit() if kind == 'commit' else row(kind=kind)
    with patch.object(g, '_gh' if transport == 'gh' else '_url', return_value=data) as call:
        out = g.content('owner/repo', kind, SHA if kind == 'commit' else 1, transport=transport)
    assert out['body'] == (data['commit']['message'] if kind == 'commit' else data['body'])
    assert out['kind'] == kind and out['repository'] == 'owner/repo'
    assert out['observed_at'] and out['url'] == data['html_url']
    assert call.call_args.args[:2] == ('GET', f'repos/owner/repo/commits/{SHA}' if kind == 'commit' else 'repos/owner/repo/issues/1')


def test_search_to_selected_content_and_offline_fields():
    hit = row(kind='pr')
    with patch.object(g, 'request', side_effect=[response([hit]), dict(hit, body='new body')]) as call:
        found = g.search('user:owner')['items'][0]
        out = g.content_from_hit(found)
        selected = g.select_content(out, ['body'])
        assert call.call_count == 2
    assert selected['body'] == 'new body' and 'title' not in selected
    assert selected['repository'] == 'owner/repo' and selected['number'] == 1
    assert selected['observed_at'] == out['observed_at']
    with pytest.raises(ValueError):g.select_content(out, ['nonexistent'])


@pytest.mark.parametrize('body', [None, '', '日本語\n\n😀\r\n'])
def test_null_empty_and_multiline_are_not_conflated(body):
    with patch.object(g, 'request', return_value=row(kind='pr', body=body)):
        assert g.content('owner/repo', 'pr', 1)['body'] == body


@pytest.mark.parametrize('data', [row(kind='issue'), row(2, 'pr'), row(kind='pr', repository='other/repo'),
                                 row(kind='pr', body=[]), row(kind='pr', html_url='https://wrong/')])
def test_pr_identity_and_body_mismatch(data):
    with patch.object(g, 'request', return_value=data), pytest.raises(g.Error):
        g.content('owner/repo', 'pr', 1)


def test_missing_body_is_not_successful_empty_content():
    data = row(kind='pr');del data['body']
    with patch.object(g, 'request', return_value=data), pytest.raises(g.Error):
        g.content('owner/repo', 'pr', 1)


@pytest.mark.parametrize('change', [{'sha': 'b'*40}, {'html_url': f'https://github.com/other/repo/commit/{SHA}'},
                                   {'commit': {}}, {'commit': {'message': None}}, {'html_url': None}])
def test_commit_identity_and_missing_message(change):
    data = commit();data.update(change)
    with patch.object(g, 'request', return_value=data), pytest.raises(g.Error):
        g.content('owner/repo', 'commit', SHA)


@pytest.mark.parametrize('kind,identifier', [('pr', True), ('issue', 0), ('pr', '1'),
                                          ('commit', 'main'), ('commit', 'a'*7), ('other', 1)])
def test_bad_selectors_fail_before_io(kind, identifier):
    with patch.object(g, 'request') as call, pytest.raises(ValueError):
        g.content('owner/repo', kind, identifier)
    call.assert_not_called()


def test_fallback_and_cumulative_bytes_fail_closed():
    with patch.object(g, 'gh_available', return_value=True), patch.object(g, '_gh', side_effect=g.Error('authentication_required')), patch.object(g, '_url', return_value=row(kind='pr')):
        assert g.content('owner/repo', 'pr', 1)['body']
    # The bounded HTTP worker reports oversized responses with exit 9.
    with patch.object(g, '_process', return_value=(9, b'', b'')) as worker:
        with g.operation(max_bytes=100), pytest.raises(g.Error, match='bytes_limit'):
            g.content('owner/repo', 'pr', 1, transport='urllib')
    assert json.loads(worker.call_args.args[1])['cap'] == 100


def test_cli_selection_and_error(capsys):
    with patch.object(g, 'request', return_value=commit()):
        assert g.main(['content', 'owner/repo', 'commit', SHA, '--field', 'body']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['sha'] == SHA and out['body'].endswith('\n') and 'title' not in out
    with patch.object(g, 'request', side_effect=g.Error('permission_denied')):
        assert g.main(['content', 'owner/repo', 'pr', '1']) == 2
    assert 'permission_denied' in capsys.readouterr().err


# ──── content_snapshot_cli ────────────────────────────────────────

# Replay recorded public GitHub responses through the real CLI (no network).


@pytest.mark.skipif(os.name != 'posix', reason='recorded gh executable uses a POSIX shebang')
@pytest.mark.parametrize('case', FIXTURES['content_snapshots'], ids=lambda c: f"{c['repository']}:{c['kind']}:{c['identifier']}")
def test_recorded_public_content_through_cli(case, tmp_path):
    # Only the external gh process is substituted: CLI parsing, subprocess transport,
    # response validation and JSON output are the production paths.
    response = json.loads(case['response'])
    kind = case['kind']
    assert kind in ('pr', 'issue', 'commit')
    expected = response['commit']['message'] if kind == 'commit' else response['body']
    endpoint = f"repos/{case['repository']}/{'commits' if kind == 'commit' else 'issues'}/{case['identifier']}"
    assert endpoint == case['endpoint']
    (tmp_path / 'response.json').write_text(case['response'], encoding='utf-8')
    gh = tmp_path / 'gh'
    gh.write_text('#!' + sys.executable + '\n'
                  'import os, pathlib, sys\n'
                  'if sys.argv[1:] != ["api", "--method", "GET", os.environ["GHI_REPLAY_ENDPOINT"]]:\n'
                  ' sys.stderr.write("unexpected replay request\\n"); sys.exit(1)\n'
                  'sys.stdout.buffer.write(pathlib.Path(os.environ["GHI_REPLAY_FILE"]).read_bytes())\n', encoding='utf-8')
    gh.chmod(0o700)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ.get('PATH', ''),
               GHI_REPLAY_ENDPOINT=endpoint, GHI_REPLAY_FILE=str(tmp_path / 'response.json'))
    for body_only in (False, True):
        result = run_cli(ROOT / 'gh_identity.py', '--transport', 'gh', 'content',
                         case['repository'], kind, str(case['identifier']), *(['--field', 'body'] if body_only else []),
                         env=env, timeout=30)
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert (output['body'], output.get('kind'), output.get('repository')) == (expected, kind, case['repository'])


# ──── html_content ────────────────────────────────────────────────

HTML='''<html><head><style>#body {color:red}</style></head><body>
<nav>Repository menu</nav><article id="description"><p>日本語 &amp; <strong>本文</strong></p>
<style>.x { color: red }</style><script>evil()</script><p hidden>secret</p>
<div aria-hidden="true">hidden</div><div style="display:none">invisible</div>
<pre>def main():\n    return "#CSS"\n</pre><p><a href="/">リンク</a><br>次行</p>
<img alt="説明"><button>Copy</button></article><article class="comment-body">別コメント</article></body></html>'''


def test_structural_text_and_code():
    out=g.html_content(HTML,['#missing','#description'])
    assert out['selector']=='#description'
    assert '日本語 & 本文' in out['body']
    assert 'def main():\n    return "#CSS"\n' in out['body']
    assert 'リンク\n次行' in out['body']
    for unwanted in ['Repository menu','color:red','color: red','evil()','secret','hidden','invisible','Copy','別コメント']:
        assert unwanted not in out['body']
    assert out['identity_verified'] is False
    assert out['visibility']=='structural_only'


def test_ambiguous_candidate_does_not_fall_back():
    with pytest.raises(g.Error,match='ambiguous_html_body'):
        g.html_content('<div class="body">one</div><div class="body">two</div><p id="other">x</p>', ['.body','#other'])


def test_no_page_fallback_or_hidden_candidate():
    with pytest.raises(g.Error,match='html_body_not_found'):
        g.html_content('<div hidden><p id="body">hidden</p></div><main>Sign in</main>',['#body'])


@pytest.mark.parametrize('selector',['div p','[id=body]','', '#body > p'])
def test_unsupported_selectors(selector):
    with pytest.raises(ValueError):g.html_content(HTML,[selector])


def test_limits():
    with pytest.raises(ValueError):g.html_content(HTML,['#description'],max_bytes=3)
    with pytest.raises(ValueError):g.html_content('<div>'*260,['div'])


def test_cli_saved_dom(tmp_path):
    path=tmp_path/'page.html';path.write_text(HTML,encoding='utf-8')
    cli=Path(g.__file__)
    run=run_cli(str(cli), 'html-content', str(path), '--selector', '#description', '--source-kind', 'dom')
    assert run.returncode==0,run.stderr
    assert json.loads(run.stdout)['body']==g.html_content(HTML,['#description'])['body']
    bad=run_cli(str(cli), 'html-content', str(path), '--selector', '#absent')
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
    out=g.html_content(html,['#body'])
    assert ('MARKER' not in out['body']) is hidden
    assert 'before' in out['body'] and 'after' in out['body']
    # Candidate selection and rendering must share the same decision.
    selected='<article id="body" style="'+escape(style, quote=True)+'">MARKER</article>'
    if hidden:
        with pytest.raises(g.Error,match='html_body_not_found'):
            g.html_content(selected,['#body'])
    else:
        assert g.html_content(selected,['#body'])['body']=='MARKER'


@pytest.mark.parametrize('include_controls',[False,True])
def test_structural_aria_hidden_contract_is_not_visual_text(include_controls):
    html='<main id="body"><span aria-hidden="true" style="display:inline">visual icon</span><span>body</span></main>'
    out=g.html_content(html,['#body'],include_controls=include_controls)
    assert out['body']=='body'
    assert out['visibility']=='structural_only'


# ──── html_page_patterns ──────────────────────────────────────────

# Modeled page families, not captured GitHub DOM compatibility evidence.


@pytest.mark.parametrize('page,selectors,expected,excluded', [
    ('<main><article id="wiki-body"><h1>Wiki</h1><p>導入 &amp; 設定</p><ul><li>手順1</li></ul></article><aside>Pages一覧</aside></main>', ['#wiki-body'], ['Wiki','導入 & 設定','手順1'], ['Pages一覧']),
    ('<main><nav>main / src</nav><pre id="file-content"><code>def main():\n    return 1\n</code></pre><footer>Edit file</footer></main>', ['#file-content'], ['def main():\n    return 1\n'], ['main / src','Edit file']),
    ('<article id="description"><p>最初の本文</p></article><article class="comment-body">返信A</article><article class="comment-body">返信B</article>', ['#description'], ['最初の本文'], ['返信A','返信B']),
    ('<section id="commit-message"><h2>変更の要約</h2><pre>理由\n\nSigned-off-by: Example</pre></section><div>diff stats</div>', ['#commit-message'], ['変更の要約','理由\n\nSigned-off-by: Example'], ['diff stats']),
    ('<section class="release-notes"><h2>v1</h2><ul><li>修正</li></ul><table><tr><th>項目</th><th>値</th></tr><tr><td>A</td><td>1</td></tr></table></section><aside>Assets download</aside>', ['#missing','.release-notes'], ['v1','修正','項目\t値','A\t1'], ['Assets download']),
    ('<article id="readme"><details><summary>設定</summary><p>詳細</p></details><p><code>#CSS</code>を保持</p></article>', ['#readme'], ['設定','詳細','#CSS'], []),
])
def test_modeled_page_families(page, selectors, expected, excluded):
    out=g.html_content(page, selectors)
    for text in expected:
        assert text in out['body']
    for text in excluded:
        assert text not in out['body']
    assert out['visibility']=='structural_only'
    assert out['identity_verified'] is False


def test_multiple_wiki_containers_fail_closed():
    with pytest.raises(g.Error,match='ambiguous_html_body'):
        g.html_content('<div id="wiki-body">A</div><div id="wiki-body">B</div>', ['#wiki-body'])


def test_login_page_is_not_a_successful_body():
    with pytest.raises(g.Error,match='html_body_not_found'):
        g.html_content('<main><h1>Sign in</h1><form>Login</form></main>', ['#wiki-body'])


# ──── browser_dom ─────────────────────────────────────────────────

# Replay actual browser DOM fragments without repeat GitHub access.


CAPTURE=FIXTURES['browser_dom/file-code']


@pytest.mark.parametrize('case',CAPTURE['cases'])
def test_live_file_preview_dom_matches_browser_text(case,tmp_path):
    path=tmp_path/'captured.html'
    path.write_text(case['html'],encoding='utf-8')
    result=run_cli(g.__file__, 'html-content', str(path), '--selector', 'pre', '--source-kind', 'dom')
    assert result.returncode==0,result.stderr
    observation=json.loads(result.stdout)
    assert observation['body']==case['visible']
    assert observation['source_kind']=='dom'


# OBS Studio Wiki fragments seen live on 2026-10-09, rebuilt in memory (no .html files).
WIKI_LINKS = [("https://obsproject.com/kb", "Knowledge Base"), ("http://discord.gg/obsproject", "OBS Studio Discord"),
              ("https://obsproject.com/chat", "OBS Community Chat"),
              ("https://obsproject.com/forum/#obs-studio-support.3", "OBS Studio Forum")]


def wiki_links_html(links=WIKI_LINKS):
    return "<ul>\n" + "".join(f'<li><a href="{url}" rel="nofollow">{text}</a></li>\n' for url, text in links) + "</ul>\n"


def test_live_wiki_paragraph_text():
    html = "<p><strong>OBS Studio</strong> is a free and open source software for video recording and live streaming.</p>\n"
    result=g.html_content(html,['p'],source_kind='dom')
    assert result['body']=='OBS Studio is a free and open source software for video recording and live streaming.'


@pytest.mark.parametrize('body',['x','x\n','x\n\n','\nx\n','\t日本語\n  x\n'])
def test_selected_pre_preserves_all_whitespace(body):
    assert g.html_content('<pre>'+body+'</pre>',['pre'])['body']==body


def test_live_wiki_link_list_text_order():
    result=g.html_content(wiki_links_html(),['ul'],source_kind='dom')
    expected='\n'.join(text for _, text in WIKI_LINKS)
    assert result['body'].split()==expected.split()


# ──── simple_site_dom ─────────────────────────────────────────────

# Small live DOM captures outside GitHub; no repeated site access.


CAPTURE_simple_site_dom=FIXTURES['browser_dom/simple-sites']


@pytest.mark.parametrize('case',CAPTURE_simple_site_dom['cases'])
def test_live_simple_site_fragments(case):
    result=g.html_content(case['html'],[case['selector']],source_kind='dom')
    if case['comparison']=='exact':
        assert result['body']==case['visible']==case['expected']
    else:
        assert ' '.join(result['body'].split())==case['expected']
        assert result['body'].strip()!=case['visible']
    assert result['visibility']=='structural_only'


def test_frameset_is_not_a_captured_body():
    # Structural model of observed parent; child documents require separate capture.
    parent='<frameset cols="18,82"><frame src="menu.htm" name="left"><frame src="top.htm" name="right"></frameset>'
    with pytest.raises(g.Error,match='html_body_not_found'):
        g.html_content(parent,['article','main','body'],source_kind='dom')


# ──── global_site_dom ─────────────────────────────────────────────

# Recorded small DOM regions; site-independent extraction, no live requests.


CASES=FIXTURES['browser_dom/global-sites']['cases']


@pytest.mark.parametrize('case',CASES,ids=[c['name'] for c in CASES])
def test_global_dom_via_cli(case,tmp_path):
    path=tmp_path/'capture.html';path.write_text(case['html'],encoding='utf-8')
    run=run_cli(g.__file__, 'html-content', str(path), '--selector', case['selector'], '--source-kind', 'dom')
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


# ──── chat_surface_dom ────────────────────────────────────────────

# DOM-derived structural cases; no login, chat submission or network access.


CASES_chat_surface_dom=FIXTURES['browser_dom/chat-surfaces']['cases']


@pytest.mark.parametrize('case',CASES_chat_surface_dom,ids=[c['name'] for c in CASES_chat_surface_dom])
def test_chat_surfaces_cli(case,tmp_path):
    path=tmp_path/'page.html';path.write_text(case['html'],encoding='utf-8')
    args=['html-content',str(path),'--selector',case['selector'],'--source-kind','dom']
    if case.get('include_controls'):args.append('--include-controls')
    run=run_cli(g.__file__,*args)
    assert run.returncode==0,run.stderr
    assert json.loads(run.stdout)['body']==case['expected']


def test_accordion_is_opt_in_and_data_hidden_is_not_hidden():
    case=CASES_chat_surface_dom[2]
    assert g.html_content(case['html'],['h3'])['body']==''
    assert g.html_content(case['html'],['h3'],include_controls=True)['body']==case['expected']
    assert g.html_content('<h3><button hidden>hidden</button>visible</h3>',['h3'],include_controls=True)['body']=='visible'


def test_control_option_requires_boolean():
    with pytest.raises(ValueError):g.html_content('<p>x</p>',['p'],include_controls='yes')


# ──── html_roundtrip (was scripts/check_html_roundtrip.py) ───────────────

def _roundtrip_modules(tmp_path, drift_code=False):
    """A stand-in markdown.py and web_ui.py: identity conversion, optionally adding a newline to code."""
    markdown = tmp_path / "markdown.py"
    markdown.write_text(f'''
from types import SimpleNamespace
import re
def html_to_markdown(html): return html
def markdown_to_html(md): return md.replace("</code>", "\\\\n</code>") if {drift_code!r} else md
def parse_html_dom(html):
    nodes = [SimpleNamespace(kind="text", text=t, tag=None, children=None, attrs=None) for t in re.split(r"<[^>]+>", html) if t]
    return SimpleNamespace(kind="element", tag=None, attrs={{}}, children=nodes, text="")
''', encoding="utf-8")
    web = tmp_path / "web_ui.py"
    web.write_text('''
def shared_stylesheets(base): return [base + "/ui.css"]
def shared_scripts(base): return [base + "/ui.js"]
def render_document(*, trusted_html, css, stylesheets, scripts):
    return "<html><head><style>" + css + "</style></head><body>" + trusted_html + "</body></html>"
''', encoding="utf-8")
    return markdown, web


def test_html_roundtrip_through_other_projects_modules(tmp_path, capsys):
    markdown, web = _roundtrip_modules(tmp_path)
    out = g.html_roundtrip(markdown, web)
    assert out["ok"] is True and [c["case"] for c in out["cases"]] == list(g.HTML_ROUNDTRIP_CASES)
    assert all(c["web_ui_wrapper_match"] is True for c in out["cases"]) and len(out["web_ui_sha256"]) == 64
    assert g.html_roundtrip(markdown)["cases"][0]["web_ui_wrapper_match"] is None
    (tmp_path / "drift").mkdir()
    drifting, _ = _roundtrip_modules(tmp_path / "drift", drift_code=True)
    failed = g.html_roundtrip(drifting)
    assert failed["ok"] is False and [c["case"] for c in failed["cases"] if not c["semantic_match"]] == ["code"]
    assert g.main(["html-roundtrip", str(markdown), "--web-ui-module", str(web)]) == 0
    assert g.main(["html-roundtrip", str(drifting)]) == 1
    assert g.main(["html-roundtrip", str(tmp_path / "missing.py")]) == 2
    capsys.readouterr()
