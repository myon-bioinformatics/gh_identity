"""Structure browsing is independent of clone, file contents and history."""
import json
from unittest.mock import patch

import pytest
import gh_identity as g

C, T, S, V, B = ('a'*40, 'b'*40, 'c'*40, 'd'*40, 'e'*40)


def entry(path, mode='100644', sha=B):
    kind = {'040000':'tree', '160000':'commit'}.get(mode, 'blob')
    return dict(path=path, mode=mode, type=kind, sha=sha)


def response(sha, entries, truncated=False):
    return dict(sha=sha, tree=entries, truncated=truncated)


ROOT = response(T, [entry('README.md'), entry('src', '040000', S),
                    entry('vendor', '040000', V), entry('link', '120000'),
                    entry('module', '160000')])


def fake(method, path, **kwargs):
    assert method == 'GET'
    return {
        'repos/o/r/commits/main': {'sha': C},
        'repos/o/r/git/trees/'+C: ROOT,
        'repos/o/r/git/trees/'+S: response(S, [entry('app.py'), entry('app.pyc'), entry('cache', '040000', V)]),
        'repos/o/r/git/trees/'+V: response(V, [entry('cached.txt')]),
    }[path]


def test_default_is_root_metadata_only():
    with patch.object(g, 'request', side_effect=fake) as get:
        out = g.tree('o/r')
    assert get.call_count == 2 and out['tree_requests'] == 1
    assert out['commit_sha'] == C and out['tree_sha'] == T
    assert out['depth'] == 1 and out['complete']
    rows = {e['path']:e for e in out['entries']}
    assert rows['src']['expanded'] is False
    assert rows['link']['kind'] == 'symlink'
    assert rows['module']['kind'] == 'submodule'
    assert set(rows) == {'README.md', 'src', 'vendor', 'link', 'module'}


def test_recursive_excludes_directories_before_network_and_files_from_output():
    with patch.object(g, 'request', side_effect=fake) as get:
        out = g.tree('o/r', depth=None, exclude_dirs=['vendor','cache'], exclude_files=['*.pyc','README.md'])
    assert {e['path'] for e in out['entries']} == {'src', 'src/app.py', 'link', 'module'}
    assert out['excluded_entries'] == 4 and out['complete']
    assert get.call_count == 3
    assert all(V not in call.args[1] for call in get.call_args_list)


def test_depth_and_path_patterns():
    with patch.object(g, 'request', side_effect=fake):
        out = g.tree('o/r', depth=2, exclude_dirs=['src/cache'], exclude_files=['src/*.pyc'])
    assert {e['path'] for e in out['entries']} == {'README.md','src','src/app.py','vendor','vendor/cached.txt','link','module'}
    assert out['scope'] == 'selected_depth_and_exclusions'


def test_ref_resolves_once_then_uses_immutable_identity():
    with patch.object(g,'request',side_effect=[{'sha':C}, ROOT]) as get:
        out=g.tree('o/r','feature/a')
    assert get.call_args_list[0].args[1].endswith('commits/feature%2Fa')
    assert get.call_args_list[1].args[1].endswith(C)
    assert out['ref']=='feature/a'


@pytest.mark.parametrize('transport', ['gh','urllib'])
def test_shared_transport(transport):
    with patch.object(g, '_gh' if transport=='gh' else '_url', side_effect=[{'sha':C},ROOT]):
        assert g.tree('o/r',transport=transport)['count']==5


def test_upstream_truncation_never_claims_complete():
    with patch.object(g,'request',side_effect=[{'sha':C},response(T,[],True)]):
        out=g.tree('o/r')
    assert out['truncated'] and not out['complete']


@pytest.mark.parametrize('payload', [
    {}, response(T,[entry('../x')]), response(T,[entry('x'),entry('x')]),
    response(T,[dict(entry('x'),mode=[])]), response(T,[dict(entry('x'),type='tree')]),
    response(T,[dict(entry('x'),sha='short')]), response(T,[dict(entry('x'),size=True)]),
    dict(response(T,[]),truncated=None),
])
def test_malformed_tree_fails_closed(payload):
    with patch.object(g,'request',side_effect=[{'sha':C},payload]),pytest.raises(g.Error):
        g.tree('o/r')


def test_wrong_child_and_cycle_rejected():
    for root,child in [(ROOT,response(V,[])),
                       (response(T,[entry('cycle','040000',T)]),response(T,[]))]:
        with patch.object(g,'request',side_effect=[{'sha':C},root,child]),pytest.raises(g.Error):
            g.tree('o/r',depth=None,exclude_dirs=['vendor'])


@pytest.mark.parametrize('options', [{'depth':0},{'depth':True},{'exclude_dirs':'vendor'},
    {'exclude_dirs':['../x']},{'exclude_files':['/root']},{'exclude_dirs':['vendor/']}])
def test_invalid_arguments_before_network(options):
    with patch.object(g,'request') as get,pytest.raises(ValueError):g.tree('o/r',**options)
    get.assert_not_called()


def test_item_budget_counts_excluded_and_request_budget_bounds_walk():
    with patch.object(g,'request',side_effect=fake):
        with g.operation(max_items=1),pytest.raises(g.Error,match='items_limit'):
            g.tree('o/r',exclude_dirs=['*'],exclude_files=['*'])
        with g.operation(max_pages=1),pytest.raises(g.Error,match='pages_limit'):
            g.tree('o/r',depth=None)


def test_cli_options(capsys):
    with patch.object(g,'request',side_effect=fake):
        assert g.main(['tree','o/r','--recursive','--exclude-dir','vendor','--exclude-dir','cache','--exclude-file','*.pyc'])==0
    out=json.loads(capsys.readouterr().out)
    assert out['depth'] is None and 'src/app.pyc' not in {e['path'] for e in out['entries']}
    assert g.main(['tree','o/r','--depth','0'])==2
    assert 'invalid_argument' in capsys.readouterr().err
