"""Exact content selection, search handoff and shared bounded transports."""
import json
from unittest.mock import patch

import pytest
import gh_identity as g
from test_issue_search import row, response

SHA = 'a' * 40


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
