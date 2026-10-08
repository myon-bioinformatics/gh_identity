"""Issue/PR discovery: identity, bounds, partial search, CLI and transports."""
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import gh_identity as g


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
