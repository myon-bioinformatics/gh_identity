"""Offline contracts for exact, bounded, sanitized Actions job log reads."""
import copy
import hashlib
import http.client
import json
import socket
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import pytest
import gh_identity as g


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/job_logs/observation.json').read_text(encoding='utf-8'))
SHA = 'a' * 40
RUN_PATH = 'repos/o/r/actions/runs/20/attempts/2'
JOBS_PATH = RUN_PATH + '/jobs?per_page=100&page=1'
LOG_PATH = 'repos/o/r/actions/jobs/91/logs'
API_URL = 'https://api.github.com/' + LOG_PATH
SIGNED_URL = 'https://fixture.actions.githubusercontent.com/logs/91?sig=FIXTURE_ONLY_SIGNATURE'
AUTH = 'FIXTURE_ONLY_AUTH_TOKEN'


def observation(run=None, jobs=None):
    """Keep real run/jobs validators; replace only HTTP JSON observations."""
    values = copy.deepcopy(FIXTURE)
    if run is not None:
        values['run'] = copy.deepcopy(run)
    if jobs is not None:
        values['jobs'] = jobs

    def request(method, path, **kwargs):
        assert method == 'GET'
        return {RUN_PATH: values['run'], JOBS_PATH: values['jobs']}[path]
    return request


def download_record(text='tests passed\n', max_bytes=1_000_000, truncated=False):
    return {
        'text': None if truncated else text,
        'text_sha256': None if truncated else hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'complete': not truncated, 'truncated': truncated,
        'bytes_read': max_bytes + 1 if truncated else len(text.encode('utf-8')),
        'limit_bytes': max_bytes,
        'normalization': {'bom_removed': False, 'ansi_removed': False},
        'redaction': {'applied': False, 'replacements': 0},
        'redirects': 1, 'transport': 'urllib', 'credential_source': 'anonymous',
    }


def test_api_observes_exact_attempt_and_reuses_observed_step_links():
    with patch.object(g, 'request', side_effect=observation()) as request, \
         patch.object(g, 'run', wraps=g.run) as run, \
         patch.object(g, 'jobs', wraps=g.jobs) as jobs, \
         patch.object(g, 'step_url_from_jobs', wraps=g.step_url_from_jobs) as links, \
         patch.object(g, 'step_url', wraps=g.step_url) as builder, \
         patch.object(g, '_download_job_log', return_value=download_record()) as download:
        result = g.job_log('o/r', 20, 91, attempt=2, transport='urllib')
    assert [call.args[1] for call in request.call_args_list] == [RUN_PATH, JOBS_PATH]
    assert run.call_count == jobs.call_count == 1
    assert run.call_args.kwargs['attempt'] == jobs.call_args.kwargs['attempt'] == 2
    assert links.call_count == builder.call_count == 2
    assert download.call_args.args == (LOG_PATH,)
    assert download.call_args.kwargs['transport'] == 'urllib'
    assert result['schema'] == 'gh-identity-job-log/1'
    assert (result['repository'], result['run_id'], result['job_id'], result['attempt'], result['head_sha']) == ('o/r', 20, 91, 2, SHA)
    assert result['complete'] is True and result['truncated'] is False
    assert result['step_urls'] == [
        {'number': 1, 'url': 'https://github.com/o/r/actions/runs/20/job/91#step:1'},
        {'number': 3, 'url': 'https://github.com/o/r/actions/runs/20/job/91#step:3'},
    ]
    assert result['transport'] == 'urllib' and result['credential_source'] == 'anonymous'


@pytest.mark.parametrize('change,error', [
    ('wrong_run', 'invalid_json'),
    ('wrong_run_attempt', 'attempt_mismatch'),
    ('missing_sha', 'invalid_run_identity'),
    ('wrong_job_run', 'job_identity_mismatch'),
    ('wrong_job_attempt', 'attempt_mismatch'),
    ('wrong_job_sha', 'job_identity_mismatch'),
    ('running_job', 'job_not_completed'),
    ('missing_job', 'job_not_found'),
    ('incomplete_jobs', 'pagination_incomplete'),
    ('duplicate_job', 'ambiguous_job_identity'),
    ('duplicate_step', 'ambiguous_step_identity'),
])
def test_bad_identity_or_incomplete_observation_blocks_log_request(change, error):
    data = copy.deepcopy(FIXTURE)
    run, jobs = data['run'], data['jobs']
    job = jobs['jobs'][0]
    if change == 'wrong_run': run['id'] = 21
    elif change == 'wrong_run_attempt': run['run_attempt'] = 3
    elif change == 'missing_sha': run.pop('head_sha')
    elif change == 'wrong_job_run': job['run_id'] = 21
    elif change == 'wrong_job_attempt': job['run_attempt'] = 3
    elif change == 'wrong_job_sha': job['head_sha'] = 'b' * 40
    elif change == 'running_job': job['status'] = 'in_progress'
    elif change == 'missing_job': jobs.update(total_count=0, jobs=[])
    elif change == 'incomplete_jobs': jobs['total_count'] = 2
    elif change == 'duplicate_job': jobs.update(total_count=2, jobs=[job, copy.deepcopy(job)])
    elif change == 'duplicate_step': job['steps'].append(copy.deepcopy(job['steps'][0]))
    with patch.object(g, 'request', side_effect=observation(run, jobs)), \
         patch.object(g, '_download_job_log') as download, pytest.raises(g.Error) as exc:
        g.job_log('o/r', 20, 91, attempt=2)
    assert exc.value.code == error
    download.assert_not_called()


@pytest.mark.parametrize('arguments', [
    {'attempt': None}, {'attempt': True}, {'attempt': 0}, {'attempt': -1},
    {'max_bytes': 0}, {'max_bytes': True}, {'timeout': float('nan')},
    {'redact': 'not-a-sequence'}, {'redact': ['']}, {'transport': 'invalid'},
])
def test_bad_api_arguments_fail_before_network(arguments):
    options = dict(attempt=2, **{k: v for k, v in arguments.items() if k != 'attempt'})
    if 'attempt' in arguments:
        options['attempt'] = arguments['attempt']
    with patch.object(g, 'request') as request, patch.object(g, '_download_job_log') as download:
        with pytest.raises(ValueError):
            g.job_log('o/r', 20, 91, **options)
    request.assert_not_called()
    download.assert_not_called()


def test_operation_deadline_is_shared_with_identity_observation():
    clock = [100.0]
    def request(*args, **kwargs):
        clock[0] += 2
        return copy.deepcopy(FIXTURE['run'])
    with patch.object(g.time, 'monotonic', side_effect=lambda: clock[0]), \
         patch.object(g, 'request', side_effect=request) as metadata, \
         patch.object(g, '_download_job_log') as download:
        with g.operation(timeout=1), pytest.raises(g.Error, match='operation_timeout'):
            g.job_log('o/r', 20, 91, attempt=2, timeout=30)
    assert metadata.call_count == 1
    download.assert_not_called()


@pytest.mark.parametrize('transport,available,expected', [
    ('gh', True, 'gh'), ('urllib', True, 'urllib'), ('auto', False, 'urllib'),
])
def test_metadata_keeps_existing_gh_and_urllib_transport_selection(transport, available, expected):
    values = [copy.deepcopy(FIXTURE['run']), copy.deepcopy(FIXTURE['jobs'])]
    with patch.object(g, 'gh_available', return_value=available), \
         patch.object(g, '_gh', side_effect=values if expected == 'gh' else AssertionError('unexpected gh')) as gh, \
         patch.object(g, '_url', side_effect=values if expected == 'urllib' else AssertionError('unexpected urllib')) as url, \
         patch.object(g, '_download_job_log', return_value=download_record()) as download:
        result = g.job_log('o/r', 20, 91, attempt=2, transport=transport)
    assert (gh.call_count, url.call_count) == ((2, 0) if expected == 'gh' else (0, 2))
    assert download.call_args.kwargs['transport'] == transport
    assert result['complete'] is True


def test_cli_reads_redaction_values_from_environment(capsys, monkeypatch):
    monkeypatch.setenv('GHI_JOB_LOG_FIXTURE_SECRET', FIXTURE['redact'][0])
    with patch.object(g, 'request', side_effect=observation()), \
         patch.object(g, '_download_job_log', return_value=download_record()) as download:
        code = g.main(['job-log', 'o/r', '20', '91', '--attempt', '2', '--log-bytes', '1024',
                       '--redact-env', 'GHI_JOB_LOG_FIXTURE_SECRET', '--transport', 'urllib'])
    captured = capsys.readouterr()
    assert code == 0 and captured.err == ''
    result = json.loads(captured.out)
    assert result['schema'] == 'gh-identity-job-log/1' and result['attempt'] == 2
    assert download.call_args.kwargs['max_bytes'] == 1024
    assert FIXTURE['redact'][0] in download.call_args.kwargs['redact']
    assert FIXTURE['redact'][0] not in captured.out + captured.err


def test_cli_local_cap_is_distinct_from_error_and_never_prints_prefix(capsys):
    with patch.object(g, 'request', side_effect=observation()), \
         patch.object(g, '_download_job_log', return_value=download_record(max_bytes=16, truncated=True)):
        code = g.main(['job-log', 'o/r', '20', '91', '--attempt', '2', '--log-bytes', '16'])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert code == 1 and captured.err == ''
    assert result['complete'] is False and result['truncated'] is True
    assert result['text'] is None and result['text_sha256'] is None


def test_cli_transport_failure_prints_only_static_error(capsys):
    with patch.object(g, 'request', side_effect=observation()), \
         patch.object(g, '_download_job_log', side_effect=g.Error('authentication_required')):
        code = g.main(['job-log', 'o/r', '20', '91', '--attempt', '2'])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ''
    assert json.loads(captured.err) == {'status': 'error', 'error': 'authentication_required'}


@pytest.mark.parametrize('value', [None, ''])
def test_cli_missing_redaction_value_fails_before_network(capsys, monkeypatch, value):
    if value is None: monkeypatch.delenv('GHI_MISSING_FIXTURE_SECRET', raising=False)
    else: monkeypatch.setenv('GHI_MISSING_FIXTURE_SECRET', value)
    with patch.object(g, 'request') as request:
        code = g.main(['job-log', 'o/r', '20', '91', '--attempt', '2',
                       '--redact-env', 'GHI_MISSING_FIXTURE_SECRET'])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ''
    assert json.loads(captured.err)['error'] == 'invalid_argument'
    request.assert_not_called()


def test_cli_attempt_is_required(capsys):
    with patch.object(g, 'request') as request:
        assert g.main(['job-log', 'o/r', '20', '91']) == 2
    assert capsys.readouterr().out == ''
    request.assert_not_called()


class Response:
    """Small read chunks make boundary behavior observable without a socket."""
    def __init__(self, body=b'', status=200, headers=None, chunk_size=7, read_error=None):
        self.body, self.status, self.chunk_size = body, status, chunk_size
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value
        self.closed = False
        self.read_sizes = []
        self.read_error = read_error
    def getcode(self):
        return self.status
    def read(self, size):
        assert isinstance(size, int) and size > 0, 'unbounded log read'
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        count = min(size, self.chunk_size)
        value, self.body = self.body[:count], self.body[count:]
        return value
    def close(self):
        self.closed = True


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def spec(max_bytes=1024, cap=None, redact=()):
    return {
        'url': API_URL, 'headers': {'Authorization': 'Bearer ' + AUTH},
        'timeout': 10, 'max_bytes': max_bytes,
        'cap': max_bytes if cap is None else cap, 'redact': list(redact),
    }


def test_reader_normalizes_bom_ansi_then_redacts_and_hashes_sanitized_utf8():
    raw = FIXTURE['log'].encode('utf-8')
    response = Response(raw, headers={'Content-Length': str(len(raw)), 'Content-Type': 'text/plain; charset=utf-8'})
    result = g._read_job_log(spec(redact=FIXTURE['redact']), opener=Opener(response))
    assert result['complete'] is True and result['truncated'] is False
    assert result['text'] == FIXTURE['sanitized']
    assert result['text_sha256'] == hashlib.sha256(FIXTURE['sanitized'].encode('utf-8')).hexdigest()
    assert result['text_sha256'] != hashlib.sha256(raw).hexdigest()
    assert result['redaction']['applied'] is True
    assert result['redaction']['replacements'] >= 1
    assert result['normalization'] == {'bom_removed': True, 'ansi_removed': True}
    assert result['bytes_read'] == len(raw)
    assert response.closed and response.read_sizes


@pytest.mark.parametrize('size,truncated', [(15, False), (16, False), (17, True)])
def test_reader_byte_limit_boundary_never_releases_partial_secret(size, truncated):
    response = Response(b'x' * size)
    result = g._read_job_log(spec(max_bytes=16, redact=('x' * 17,)), opener=Opener(response))
    assert result['truncated'] is truncated and result['complete'] is not truncated
    assert result['bytes_read'] <= 17 and max(response.read_sizes) <= 17
    if truncated:
        assert result['text'] is None and result['text_sha256'] is None
        assert result['redaction'] == {'applied': False, 'replacements': 0}
        assert result['normalization'] == {'bom_removed': False, 'ansi_removed': False}
    else:
        assert result['text'] == 'x' * size
    assert response.closed


def test_declared_local_overflow_returns_no_body_without_reading():
    response = Response(b'FIXTURE_ONLY_TOKEN', headers={'Content-Length': '10000'})
    result = g._read_job_log(spec(max_bytes=16), opener=Opener(response))
    assert result['truncated'] and result['text'] is None and result['text_sha256'] is None
    assert result['bytes_read'] == 0 and response.read_sizes == [] and response.closed


def test_global_remaining_capacity_is_an_error_not_local_truncation():
    response = Response(b'x' * 17)
    with pytest.raises(g.Error, match='bytes_limit'):
        g._read_job_log(spec(max_bytes=32, cap=16), opener=Opener(response))
    assert response.closed and max(response.read_sizes) <= 17


@pytest.mark.parametrize('status,headers,error', [
    (401, {}, 'authentication_required'),
    (403, {}, 'permission_or_rate_limit'),
    (403, {'X-RateLimit-Remaining': '0'}, 'rate_limit'),
    (429, {'Retry-After': '60'}, 'rate_limit'),
    (404, {}, 'not_found_or_inaccessible'),
    (410, {}, 'log_not_available'),
    (500, {}, 'server_error'),
])
def test_http_errors_are_distinct_and_never_read_untrusted_error_body(status, headers, error):
    response = Response(b'FIXTURE_ONLY_SECRET_ERROR_BODY', status=status, headers=headers)
    with pytest.raises(g.Error) as exc:
        g._read_job_log(spec(), opener=Opener(response))
    assert exc.value.code == str(exc.value) == error
    assert response.read_sizes == [] and response.closed


@pytest.mark.parametrize('status,headers,body,error', [
    (206, {}, b'partial', 'incomplete_log_response'),
    (200, {'Content-Range': 'bytes 0-6/999'}, b'partial', 'incomplete_log_response'),
    (200, {'Content-Length': '10'}, b'short', 'incomplete_log_response'),
    (200, {'Content-Length': 'not-a-length'}, b'body', 'invalid_log_response'),
    (200, {'Content-Encoding': 'gzip'}, b'compressed', 'unsupported_log_encoding'),
    (200, {}, b'\xff\xfeA\x00', 'unsupported_log_encoding'),
    (200, {}, b'invalid-\xff-utf8', 'invalid_log_encoding'),
])
def test_incomplete_invalid_and_unsupported_responses_cannot_claim_complete(status, headers, body, error):
    response = Response(body, status=status, headers=headers)
    with pytest.raises(g.Error) as exc:
        g._read_job_log(spec(), opener=Opener(response))
    assert exc.value.code == error and response.closed


@pytest.mark.parametrize('phase,failure,error', [
    ('read', socket.timeout('FIXTURE_ONLY_SECRET_TIMEOUT'), 'operation_timeout'),
    ('open', urllib.error.URLError(socket.timeout('FIXTURE_ONLY_SECRET_TIMEOUT')), 'operation_timeout'),
    ('open', urllib.error.URLError('FIXTURE_ONLY_SECRET_ADDRESS'), 'transport_error'),
    ('read', http.client.IncompleteRead(b'FIXTURE_ONLY_SECRET_PARTIAL', 100), 'incomplete_log_response'),
])
def test_transport_and_stream_errors_have_static_diagnostics_and_no_partial_log(phase, failure, error):
    response = Response(read_error=failure)
    opener = Opener(failure if phase == 'open' else response)
    with pytest.raises(g.Error) as exc:
        g._read_job_log(spec(), opener=opener)
    assert exc.value.code == str(exc.value) == error
    assert 'FIXTURE_ONLY' not in str(exc.value)
    if phase == 'read':
        assert response.closed


def test_redirect_strips_authorization_and_does_not_expose_signed_url():
    first = Response(status=302, headers={'Location': SIGNED_URL})
    second = Response(b'tests passed\n')
    opener = Opener(first, second)
    result = g._read_job_log(spec(), opener=opener)
    assert result['redirects'] == 1 and result['text'] == 'tests passed\n'
    before, after = [dict((k.lower(), v) for k, v in request.header_items()) for request, _ in opener.calls]
    assert before['authorization'] == 'Bearer ' + AUTH
    assert 'authorization' not in after
    assert [request.get_method() for request, _ in opener.calls] == ['GET', 'GET']
    assert SIGNED_URL not in json.dumps(result) and AUTH not in json.dumps(result)
    assert first.closed and second.closed


def test_urllib_httperror_redirect_is_followed_without_reading_body():
    headers = Message()
    headers['Location'] = SIGNED_URL
    error_body = Response(b'FIXTURE_ONLY_SECRET_REDIRECT_BODY')
    redirect = urllib.error.HTTPError(API_URL, 302, 'Found', headers, error_body)
    result = g._read_job_log(spec(), opener=Opener(redirect, Response(b'OK')))
    assert result['text'] == 'OK' and result['redirects'] == 1
    assert error_body.read_sizes == [] and error_body.closed


@pytest.mark.parametrize('target', [
    'http://fixture.actions.githubusercontent.com/logs',
    'https://evil.invalid/logs',
    'https://fixture.actions.githubusercontent.com.evil.invalid/logs',
    'https://user:password@fixture.actions.githubusercontent.com/logs',
    'https://fixture.blob.core.windows.net:444/logs',
    'https://fixture.actions.githubusercontent.com/logs#fragment',
])
def test_unsafe_redirect_is_rejected_before_second_request(target):
    response = Response(status=302, headers={'Location': target})
    opener = Opener(response)
    with pytest.raises(g.Error, match='unsafe_log_redirect'):
        g._read_job_log(spec(), opener=opener)
    assert len(opener.calls) == 1 and response.closed


def test_missing_redirect_location_is_distinct_from_unsafe_destination():
    response = Response(status=302)
    with pytest.raises(g.Error, match='invalid_log_redirect'):
        g._read_job_log(spec(), opener=Opener(response))
    assert response.closed


def test_redirect_count_is_bounded_and_each_hop_has_no_credentials():
    responses = [Response(status=302, headers={'Location':
                 f'https://fixture.blob.core.windows.net/logs/{n}?sig=FIXTURE_ONLY_{n}'}) for n in range(4)]
    opener = Opener(*responses)
    with pytest.raises(g.Error, match='log_redirect_limit'):
        g._read_job_log(spec(), opener=opener)
    assert len(opener.calls) == 4
    assert all(response.closed for response in responses)
    assert all(not request.has_header('Authorization') for request, _ in opener.calls[1:])
