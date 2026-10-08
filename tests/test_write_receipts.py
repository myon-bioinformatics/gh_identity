"""Offline regression tests for the mutation/verification boundary."""
import contextlib
import io
import json
from unittest.mock import patch

import pytest
import gh_identity as g

MARKER = '<!-- gh-identity:receipt-test -->'
BODY = MARKER + '\n日本語の本文 🐍'
URL = 'https://github.com/o/r/issues/1#issuecomment-7'


def observed(**changes):
    return dict({'id': 7, 'body': BODY, 'issue_url': g.API + '/repos/o/r/issues/1',
                 'html_url': URL}, **changes)


def post_with(responses):
    with patch.object(g, 'pages', return_value=[]), patch.object(g, 'request', side_effect=responses) as req:
        result = g.post_comment('o/r', 1, '日本語の本文 🐍', write=True, marker=MARKER)
    return result, req.call_args_list


@pytest.mark.parametrize('url', [URL, 'https://github.com/o/r/pull/1#issuecomment-7'])
def test_comment_verifies_exact_readback_and_real_newline(url):
    result, calls = post_with([{'id': 7}, observed(html_url=url)])
    assert result['status'] == result['verification_status'] == 'verified'
    assert result['mutation_status'] == 'succeeded'
    assert result['url'] == url
    assert [call.args[0] for call in calls] == ['POST', 'GET']
    assert calls[0].args[2] == {'body': BODY}
    assert calls[1].args[1] == 'repos/o/r/issues/comments/7'


@pytest.mark.parametrize('response', [None, {}, [], {'id': None}, {'id': True}, {'id': 0}, {'id': '7'}])
def test_invalid_post_identity_is_uncertain_without_retry(response):
    result, calls = post_with([response])
    assert result['status'] == 'mutation_uncertain'
    assert result['mutation_status'] == 'uncertain'
    assert len(calls) == 1


@pytest.mark.parametrize('response', [None, {}, observed(id=8), observed(id=True),
    observed(body=MARKER), observed(body='different'),
    observed(issue_url=g.API + '/repos/other/r/issues/1'),
    observed(html_url='https://github.com/o/r/issues/2#issuecomment-7')])
def test_mismatched_readback_preserves_write_without_verifying(response):
    result, calls = post_with([{'id': 7}, response])
    assert result['status'] == 'verification_mismatch'
    assert result['mutation_status'] == 'succeeded'
    assert result['id'] == 7
    assert not result['verified']
    assert len(calls) == 2


def test_readback_timeout_preserves_successful_write_and_never_reposts():
    result, calls = post_with([{'id': 7}, g.Error('timeout')])
    assert result['status'] == 'verification_failed'
    assert result['mutation_status'] == 'succeeded'
    assert result['id'] == 7
    assert [c.args[0] for c in calls] == ['POST', 'GET']


@pytest.mark.parametrize('uncertain,status', [(True, 'mutation_uncertain'), (False, 'mutation_failed')])
def test_comment_cli_failure_is_nonzero(uncertain, status):
    with patch.object(g, 'pages', return_value=[]), patch.object(g, 'request', side_effect=g.Error('timeout', uncertain)) as req:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = g.main(['comment', 'o/r', '1', 'body', '--operation-key', 'receipt-test', '--write'])
    assert rc == 1
    assert json.loads(out.getvalue())['status'] == status
    assert req.call_count == 1


@pytest.mark.parametrize('readback,status', [(g.Error('timeout'), 'verification_failed'), ({'value': 'wrong'}, 'verification_mismatch')])
def test_variable_cli_verification_failure_is_nonzero(readback, status):
    with patch.object(g, 'request', side_effect=[None, readback]):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = g.main(['variable-set', 'o/r', 'A', 'B', '--write'])
    receipt = json.loads(out.getvalue())
    assert rc == 1
    assert receipt['status'] == status
    assert receipt['mutation_status'] == 'succeeded'


@pytest.mark.parametrize('status,expected', [('planned', 0), ('already_exists', 0), ('verified', 0),
    ('mutation_failed', 1), ('mutation_uncertain', 1), ('verification_failed', 1),
    ('verification_mismatch', 1), ('unknown_future_status', 1)])
@pytest.mark.parametrize('command,api', [(['comment', 'o/r', '1', 'body'], 'post_comment'),
    (['variable-set', 'o/r', 'A', 'B'], 'set_variable')])
def test_cli_receipt_exit_contract(command, api, status, expected):
    with patch.object(g, api, return_value={'status': status}), contextlib.redirect_stdout(io.StringIO()):
        assert g.main(command) == expected
