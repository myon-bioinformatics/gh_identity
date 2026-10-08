"""Regressions for read observation identity and normalization."""
from unittest.mock import patch

import pytest
import gh_identity as g


def job(**changes):
    return dict({'id': 91, 'run_id': 20, 'run_attempt': 2, 'steps': []}, **changes)


@pytest.mark.parametrize('rows,total,error', [
    ([job(run_id=999)], 1, 'job_identity_mismatch'),
    ([job(run_id=True)], 1, 'job_identity_mismatch'),
    ([job(run_attempt=3)], 1, 'attempt_mismatch'),
    ([job(run_attempt=None)], 1, 'invalid_attempt_identity'),
    ([job(run_attempt=True)], 1, 'invalid_attempt_identity'),
    ([job(id=True)], 1, 'invalid_job_identity'),
    ([job(id=0)], 1, 'invalid_job_identity'),
    ([job(id='91')], 1, 'invalid_job_identity'),
    ([job(), job()], 2, 'ambiguous_job_identity'),
    ([None], 1, 'invalid_json'),
    ([job(steps=[None])], 1, 'invalid_json'),
    ([job(steps=None)], 1, 'invalid_json'),
    ([job()], True, 'pagination_incomplete'),
    ([], -1, 'pagination_incomplete'),
])
def test_jobs_reject_inconsistent_observation(rows, total, error):
    with patch.object(g, 'request', return_value={'total_count': total, 'jobs': rows}):
        with pytest.raises(g.Error) as exc:
            g.jobs('o/r', 20, attempt=2)
    assert exc.value.code == error


def test_duplicate_jobs_across_pages_are_rejected():
    first = [job(id=n) for n in range(1, 101)]
    with patch.object(g, 'request', side_effect=[
        {'total_count': 101, 'jobs': first}, {'total_count': 101, 'jobs': [job(id=1)]}
    ]):
        with pytest.raises(g.Error, match='ambiguous_job_identity'):
            g.jobs('o/r', 20, attempt=2)


def test_unspecified_attempt_preserves_observed_attempts():
    # Latest jobs can include jobs retained from an earlier partial rerun.
    rows = [job(id=91, run_attempt=1), job(id=92, run_attempt=2)]
    with patch.object(g, 'request', return_value={'total_count': 2, 'jobs': rows}):
        result = g.jobs('o/r', 20)
    assert result['complete'] is True
    assert result['attempt'] is None
    assert [x['attempt'] for x in result['jobs']] == [1, 2]


@pytest.mark.parametrize('last,ids', [(None, [1, 2]), (0, []), (1, [2]), (2, [1, 2]), (10, [1, 2])])
def test_comment_last_limit(last, ids):
    with patch.object(g, 'pages', return_value=[{'id': 1, 'body': '一'}, {'id': 2, 'body': '二'}]):
        result = g.comments('o/r', 1, last=last)
    assert result['total'] == 2
    assert result['shown'] == len(ids)
    assert [c['id'] for c in result['comments']] == ids


@pytest.mark.parametrize('last', [-1, True, False, '1', 1.5])
def test_bad_comment_limit_fails_before_transport(last):
    with patch.object(g, 'pages') as pages:
        with pytest.raises(ValueError):
            g.comments('o/r', 1, last=last)
    pages.assert_not_called()


@pytest.mark.parametrize('fields,expected', [
    ({'url': 'api-url', 'html_url': 'web-url'}, 'web-url'),
    ({'url': 'normalized-web-url'}, 'normalized-web-url'),
    ({'html_url': 'web-url'}, 'web-url'),
])
def test_check_url_normalization_through_transport_and_pure_helper(fields, expected):
    row = dict(id=1, name='unit', status='completed', conclusion='success', **fields)
    original = dict(row)
    pure = g.summarize_checks([row], 1)
    with patch.object(g, 'request', return_value={'total_count': 1, 'check_runs': [row]}):
        fetched = g.checks_for_sha('o/r', 'a' * 40)
    assert pure['checks'][0]['url'] == fetched['checks'][0]['url'] == expected
    assert pure['state'] == fetched['state'] == 'green'
    assert row == original
