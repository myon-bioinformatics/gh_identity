"""PR consolidation primitives, with no live GitHub writes."""
import json
from unittest.mock import patch

import pytest
import gh_identity as g

BEFORE = {'number': 149, 'title': 'Old', 'body': '既存の説明\r\nsecond line',
          'state': 'open', 'base': {'ref': 'main'}, 'head': {'sha': 'a' * 40}}
BLOCK = '## Consolidated scope\n- #150 を取り込み\n- 1082 passed 🐍'
AFTER = {**BEFORE, 'title': 'Updated', 'body': BEFORE['body'].rstrip() + '\n\n' + BLOCK}


def test_append_and_title_use_one_patch_and_verify_exact_body():
    with patch.object(g, 'request', side_effect=[BEFORE, BEFORE, {}, AFTER]) as request:
        result = g.pr_edit('o/r', 149, title='Updated', append_body=BLOCK, write=True)
    assert result['status'] == 'verified'
    assert result['mutation_status'] == 'succeeded'
    assert [c.args[0] for c in request.call_args_list] == ['GET', 'GET', 'PATCH', 'GET']
    call = request.call_args_list[2]
    assert call.args[1:3] == ('repos/o/r/pulls/149', {'title': 'Updated', 'body': AFTER['body']})
    assert call.args[-1] is True  # mutation transport: no unsafe fallback retry


@pytest.mark.parametrize('old', [None, '', '既存'])
def test_append_dry_run_and_null_body(old):
    with patch.object(g, 'request', return_value={**BEFORE, 'body': old}) as request:
        result = g.pr_edit('o/r', 149, append_body=BLOCK)
    assert result['status'] == 'planned'
    assert result['fields']['body']['to'] == (old + '\n\n' if old else '') + BLOCK
    assert request.call_count == 1


def test_retry_does_not_duplicate_or_modify_existing_body():
    old = {**AFTER, 'body': AFTER['body'] + '\n\nLater note'}
    with patch.object(g, 'request', return_value=old) as request:
        result = g.pr_edit('o/r', 149, title='Updated', append_body=BLOCK, write=True)
    assert result['status'] == 'already_satisfied'
    assert request.call_count == 1


@pytest.mark.parametrize('changes', [{'body': 'someone edited'}, {'head': {'sha': 'b' * 40}},
                                     {'title': 'someone renamed'}])
def test_observed_concurrent_changes_refuse_patch(changes):
    with patch.object(g, 'request', side_effect=[BEFORE, {**BEFORE, **changes}]) as request:
        result = g.pr_edit('o/r', 149, append_body=BLOCK, write=True)
    assert result['status'] == 'stale'
    assert result['mutation_status'] == 'not_attempted'
    assert [c.args[0] for c in request.call_args_list] == ['GET', 'GET']


@pytest.mark.parametrize('error,status', [(g.Error('timeout', True), 'mutation_uncertain'),
                                         (g.Error('forbidden'), 'mutation_failed')])
def test_patch_failure_is_not_retried(error, status):
    with patch.object(g, 'request', side_effect=[BEFORE, error]) as request:
        result = g.pr_edit('o/r', 149, state='closed', write=True)
    assert result['status'] == status
    assert request.call_count == 2


@pytest.mark.parametrize('readback,status', [(g.Error('timeout'), 'verification_failed'),
    ({**BEFORE, 'body': 'wrong'}, 'verification_mismatch'), ({}, 'verification_failed')])
def test_failed_readback_keeps_successful_mutation(readback, status):
    with patch.object(g, 'request', side_effect=[BEFORE, {}, readback]) as request:
        result = g.pr_edit('o/r', 149, body='', write=True)
    assert result['status'] == status
    assert result['mutation_status'] == 'succeeded'
    assert request.call_count == 3


@pytest.mark.parametrize('kwargs', [{}, {'state': 'merged'}, {'body': '', 'append_body': BLOCK},
    {'append_body': ' \n'}, {'title': ''}, {'body': 1}])
def test_invalid_edit_never_requests(kwargs):
    with patch.object(g, 'request') as request, pytest.raises(ValueError):
        g.pr_edit('o/r', 149, **kwargs)
    request.assert_not_called()


def test_close_and_full_replacement_preserve_exact_newlines():
    body = '日本語\r\n\r\nreplacement\n'
    with patch.object(g, 'request', side_effect=[BEFORE, {}, {**BEFORE, 'body': body, 'state': 'closed'}]) as request:
        result = g.pr_edit('o/r', 149, body=body, state='closed', write=True)
    assert result['status'] == 'verified'
    assert request.call_args_list[1].args[2] == {'state': 'closed', 'body': body}


def test_cli_append_body_file_with_title(tmp_path, capsys):
    path = tmp_path / 'scope.md'
    path.write_text(BLOCK, encoding='utf-8')
    with patch.object(g, 'request', side_effect=[BEFORE, BEFORE, {}, AFTER]):
        assert g.main(['pr-edit', 'o/r', '149', '--append-body-file', str(path),
                       '--title', 'Updated', '--write']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'verified'


def test_cli_full_body_file_keeps_crlf_and_trailing_newline(tmp_path, capsys):
    path = tmp_path / 'body.md'
    body = '本文\r\nlast\n'
    path.write_bytes(body.encode())
    with patch.object(g, 'request', return_value=BEFORE):
        assert g.main(['pr-edit', 'o/r', '149', '--body-file', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['fields']['body']['to'] == body


def test_comment_body_file_uses_existing_dedup_and_verification(tmp_path, capsys):
    path = tmp_path / 'note.md'
    body = 'Merged into #149.\nClosing source PR.\n'
    path.write_text(body, encoding='utf-8')
    marker = '<!-- gh-identity:consolidate-149-150 -->'
    observed = {'id': 7, 'body': marker + '\n' + body,
        'issue_url': g.API + '/repos/o/r/issues/150',
        'html_url': 'https://github.com/o/r/pull/150#issuecomment-7'}
    with patch.object(g, 'pages', return_value=[]), patch.object(g, 'request', side_effect=[{'id': 7}, observed]):
        assert g.main(['comment', 'o/r', '150', '--body-file', str(path),
                       '--operation-key', 'consolidate-149-150', '--write']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'verified'


@pytest.mark.parametrize('status,code', [('planned', 0), ('already_satisfied', 0), ('verified', 0),
    ('stale', 1), ('mutation_uncertain', 1), ('verification_failed', 1), ('verification_mismatch', 1)])
def test_edit_cli_exit_codes(status, code, capsys):
    with patch.object(g, 'pr_edit', return_value={'status': status}):
        assert g.main(['pr-edit', 'o/r', '149', '--state', 'closed']) == code


@pytest.mark.parametrize('args', [[], ['text', '--body-file', 'x.md']])
def test_comment_body_missing_or_conflicting_exits_two(args, capsys):
    with patch.object(g, 'request') as request:
        assert g.main(['comment', 'o/r', '149', *args]) == 2
    request.assert_not_called()
