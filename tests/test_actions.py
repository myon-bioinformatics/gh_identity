"""⚙️ GitHub Actions: workflow runs, head gates, job logs, artifacts.

Sections (each was one file; `-k` selects inside them): job_logs, job_log_transport, artifacts, workflow_run_discovery, head_gates.
"""

from email.message import Message
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.parse import urlsplit
import base64
import copy
import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import zipfile

import pytest

from conftest import FIXTURES, SHA

import gh_identity as g


# ──── job_logs ────────────────────────────────────────────────────

# Offline contracts for exact, bounded, sanitized Actions job log reads.


FIXTURE = FIXTURES['job_logs/observation']


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
    ('run_id_float', 'invalid_run_identity'),
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
    elif change == 'run_id_float': run['id'] = 20.0
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


# ──── job_log_transport ───────────────────────────────────────────

# Real isolated workers against offline HTTP fixtures; all values are synthetic.


SYNTHETIC_TOKEN = "ghp_OfflineFixtureOnly123456789"


SYNTHETIC_SECRET = "offline-fixture-canary"


class LogHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.endswith("/attempts/2"):
            body = json.dumps({"id": 20, "run_attempt": 2, "head_sha": SHA}).encode()
            media = "application/json"
        elif "/attempts/2/jobs?" in self.path:
            body = json.dumps({"total_count": 1, "jobs": [{
                "id": 91, "run_id": 20, "run_attempt": 2, "head_sha": SHA,
                "status": "completed", "steps": [{"number": 1}],
            }]}).encode()
            media = "application/json"
        else:
            media = "text/plain; charset=utf-8"
            if self.path == "/invalid":
                body = b"x" * 900 + b"\xff"
            elif self.path == "/partial":
                body = b"x" * 900
            elif self.path == "/oversize":
                body = b"x" * 100
            elif self.path == "/slow":
                body = b"x" * 100
            else:
                body = ("\ufeff日本語\r\n" + SYNTHETIC_TOKEN + "\n" +
                        SYNTHETIC_SECRET[:7] + "\x1b[31m" + SYNTHETIC_SECRET[7:] + "\x1b[0m\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", media)
        if self.path != "/oversize":
            self.send_header("Content-Length", str(len(body) + (50 if self.path == "/partial" else 0)))
        self.end_headers()
        try:
            if self.path == "/slow":
                for value in body:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    time.sleep(.04)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def log_http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), LogHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture(autouse=False)
def no_real_credentials(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def download(path, **kwargs):
    options = {"max_bytes": 1000, "redact": (SYNTHETIC_SECRET,), "transport": "urllib", "timeout": 3}
    options.update(kwargs)
    return g._download_job_log(path, **options)


@pytest.mark.usefixtures('no_real_credentials')
def test_real_full_api_uses_metadata_and_returns_only_sanitized_body(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    result = g.job_log("o/r", 20, 91, attempt=2, redact=(SYNTHETIC_SECRET,), transport="urllib")
    assert result["text"] == "日本語\n[REDACTED]\n[REDACTED]\n"
    assert result["head_sha"] == SHA
    assert result["step_urls"] == [{"number": 1, "url": g.step_url("o/r", 20, 91, 1)}]
    assert result["transport"] == "urllib" and result["credential_source"] == "anonymous"


@pytest.mark.usefixtures('no_real_credentials')
def test_real_cli_passes_only_environment_names_and_prints_sanitized_json(log_http):
    env = os.environ.copy()
    env.update(GH_TOKEN=SYNTHETIC_TOKEN, LOG_FIXTURE_SECRET=SYNTHETIC_SECRET)
    code = "import sys,gh_identity as g;g.API=sys.argv[1];raise SystemExit(g.main(sys.argv[2:]))"
    argv = [sys.executable, "-c", code, log_http, "job-log", "o/r", "20", "91", "--attempt", "2",
            "--redact-env", "LOG_FIXTURE_SECRET", "--transport", "urllib"]
    result = subprocess.run(argv, env=env, capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stderr == b""
    assert SYNTHETIC_TOKEN not in result.stdout.decode() and SYNTHETIC_SECRET not in result.stdout.decode()
    assert json.loads(result.stdout)["credential_source"] == "environment"
    assert all(SYNTHETIC_SECRET not in item and SYNTHETIC_TOKEN not in item for item in argv)


@pytest.mark.usefixtures('no_real_credentials')
def test_worker_oversize_returns_no_secret_fragment(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    with g.operation():
        result = download("oversize", max_bytes=20)
    assert result["complete"] is False and result["truncated"] is True
    assert result["text"] is None and result["text_sha256"] is None
    assert result["bytes_read"] == 21


@pytest.mark.usefixtures('no_real_credentials')
@pytest.mark.parametrize("path,error", [("invalid", "invalid_log_encoding"), ("partial", "incomplete_log_response")])
def test_failed_body_bytes_are_cumulative_and_cannot_be_retried_past_budget(log_http, monkeypatch, path, error):
    monkeypatch.setattr(g, "API", log_http)
    with g.operation(max_bytes=1200):
        with pytest.raises(g.Error, match=error):
            download(path)
        assert g._BUDGET.get().bytes >= 900
        with pytest.raises(g.Error, match="bytes_limit"):
            download(path)
        with pytest.raises(g.Error, match="bytes_limit"):
            g._BUDGET.get().remaining()


@pytest.mark.usefixtures('no_real_credentials')
def test_real_slow_drip_is_stopped_by_outer_deadline(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    started = time.monotonic()
    with g.operation(timeout=.35):
        with pytest.raises(g.Error, match="operation_timeout"):
            download("slow", timeout=3)
        with pytest.raises(g.Error, match="operation_timeout"):
            g._BUDGET.get().remaining()
    assert time.monotonic() - started < 2


@pytest.mark.usefixtures('no_real_credentials')
def test_normalized_body_cannot_hide_received_bytes_from_global_cap(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    with g.operation(max_bytes=10), pytest.raises(g.Error, match="bytes_limit"):
        download("logs")


@pytest.mark.usefixtures('no_real_credentials')
def test_gh_credential_uses_host_pinned_command_and_stdin_to_worker(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    calls = []
    real = g._process
    def process(argv, payload, timeout, env=None, mutating=False):
        calls.append((argv, payload))
        if argv[0] == "gh":
            assert argv == ["gh", "auth", "token", "--hostname", "github.com"]
            assert env["GH_PROMPT_DISABLED"] == "1" and env["GH_HOST"] == "github.com"
            return 0, SYNTHETIC_TOKEN.encode() + b"\n", b""
        assert SYNTHETIC_TOKEN not in " ".join(argv)
        assert json.loads(payload)["headers"]["Authorization"] == "Bearer " + SYNTHETIC_TOKEN
        return real(argv, payload, timeout, env, mutating)
    with g.operation(), patch.object(g, "_process", side_effect=process):
        result = download("logs", transport="gh")
    assert result["credential_source"] == "gh"
    assert result["text"] == "日本語\n[REDACTED]\n[REDACTED]\n"
    assert len(calls) == 2


@pytest.mark.usefixtures('no_real_credentials')
@pytest.mark.parametrize("transport,status,stderr,expected", [
    ("auto", 1, b"no oauth token found for github.com", "anonymous"),
    ("auto", 4, b"authentication needed", "anonymous"),
    ("gh", 1, b"no oauth token found for github.com", "authentication_required"),
    ("auto", 2, b"cancelled", "cancelled"),
    ("gh", 2, b"cancelled", "cancelled"),
    ("auto", 1, b"unrelated failure", "gh_failed"),
    ("gh", 1, b"unrelated failure", "gh_failed"),
])
def test_gh_fallback_only_handles_missing_authentication(transport, status, stderr, expected):
    with g.operation(), patch.object(g, "gh_available", return_value=True), patch.object(g, "_process", return_value=(status, b"", stderr)):
        if expected == "anonymous":
            assert g._job_log_credential(transport, 1) == (None, "anonymous")
        else:
            with pytest.raises(g.Error, match=expected):
                g._job_log_credential(transport, 1)


@pytest.mark.usefixtures('no_real_credentials')
@pytest.mark.parametrize("text,values,expected", [
    ("foobar", ("foo\x1b[31m", "foobar"), "[REDACTED]"),
    ("ghp_AAAAfooBBBB", ("foo",), "[REDACTED]"),
    ("ghp_OfflineSynthetic", ("ghp_",), "[REDACTED]"),
    ("token=offline-value", ("token",), "[REDACTED]=[REDACTED]"),
    ("-----BEGIN RSA PRIVATE KEY-----\nfixture\n-----END RSA PRIVATE KEY-----", ("RSA",), "[REDACTED]"),
    ("Authorization : Bearer offline-value", (), "Authorization : Bearer [REDACTED]"),
], ids=["normalized-overlap", "token-interior", "token-prefix", "assignment-key", "pem-label", "authorization-spacing"])
def test_redaction_rules_do_not_disable_each_other(text, values, expected):
    assert g._redact_job_log(text, values)[0] == expected


@pytest.mark.usefixtures('no_real_credentials')
def test_add_mask_honors_case_words_and_workflow_data_escapes():
    text = "::ADD-MASK::Mona The Octocat%0Asecond%25value\nMona\nOctocat\nsecond%value\n"
    result, _ = g._redact_job_log(text, ())
    assert result == "::ADD-MASK::[REDACTED]\n[REDACTED]\n[REDACTED]\n[REDACTED]\n"


@pytest.mark.usefixtures('no_real_credentials')
def test_metadata_gh_http_401_retains_authentication_classification():
    with patch.object(g, "_process", return_value=(1, b"", b"Bad credentials (HTTP 401)")):
        with pytest.raises(g.Error, match="authentication_required"):
            g.run("o/r", 20, attempt=2, transport="gh")


# ──── artifacts ───────────────────────────────────────────────────

# Run artifacts: identity-checked listing and bounded single-member reads.


SIGNED = "https://productionresultssa1.blob.core.windows.net/a.zip?sig=x"


def _zip(**members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_binary_reader_follows_storage_redirect_without_credentials():
    body = _zip(**{"r.json": "{}"})
    opener = Opener(Response(status=302, headers={"Location": SIGNED}),
                    Response(body, headers={"Content-Type": "application/zip"}))
    spec = {"url": g.API + "/repos/o/r/actions/artifacts/9/zip", "headers": {"Authorization": "Bearer t"},
            "timeout": 10, "max_bytes": 1 << 20, "cap": 1 << 20, "redact": [], "binary": True}
    out = g._read_job_log(spec, opener=opener)
    assert base64.b64decode(out["data_b64"]) == body and out["sha256"] == hashlib.sha256(body).hexdigest()
    after = dict((k.lower(), v) for k, v in opener.calls[1][0].header_items())
    assert "authorization" not in after


def test_text_reader_still_rejects_zip_content_type():
    opener = Opener(Response(b"PK", headers={"Content-Type": "application/zip"}))
    spec = {"url": g.API + "/x", "headers": {}, "timeout": 10, "max_bytes": 64, "cap": 64, "redact": []}
    with pytest.raises(g.Error):
        g._read_job_log(spec, opener=opener)


def _meta(**changes):
    return {"id": 9, "name": "junit-flutter-refactor", "expired": False,
            "workflow_run": {"id": 5, "head_sha": "a" * 40}, **changes}


def _patch(monkeypatch, body, meta=None):
    monkeypatch.setattr(g, "request", lambda *a, **k: meta or _meta())
    monkeypatch.setattr(g, "_download_job_log", lambda *a, **k: {
        "complete": True, "data_b64": base64.b64encode(body).decode(), "sha256": "z", "bytes_read": len(body)})


def test_artifact_file_returns_member_text_and_digest(monkeypatch):
    _patch(monkeypatch, _zip(**{"route-verification.json": '{"result": "success"}', "other.xml": "<x/>"}))
    out = g.artifact_file("o/r", 9, "route-verification.json")
    assert out["complete"] and json.loads(out["text"]) == {"result": "success"}
    assert out["sha256"] == hashlib.sha256(b'{"result": "success"}').hexdigest()
    assert (out["run_id"], out["head_sha"], out["name"]) == (5, "a" * 40, "junit-flutter-refactor")


def test_member_larger_than_the_cap_is_not_expanded(monkeypatch):
    _patch(monkeypatch, _zip(**{"big.txt": "x" * 5000}))
    out = g.artifact_file("o/r", 9, "big.txt", max_bytes=100)
    assert out["truncated"] and out["text"] is None


@pytest.mark.parametrize(("meta", "member", "error"), [
    (None, "missing.json", "artifact_member_not_found"),
    (_meta(expired=True), "r.json", "artifact_expired"),
    (_meta(id=10), "r.json", "artifact_identity_mismatch"),
])
def test_artifact_file_fails_closed(monkeypatch, meta, member, error):
    _patch(monkeypatch, _zip(**{"r.json": "{}"}), meta)
    with pytest.raises(g.Error) as exc:
        g.artifact_file("o/r", 9, member)
    assert exc.value.code == error


@pytest.mark.parametrize("member", ["../etc/passwd", "/abs", ""])
def test_unsafe_member_names_are_rejected(member):
    with pytest.raises(ValueError):
        g.artifact_file("o/r", 9, member)


def test_corrupt_zip_is_an_archive_error(monkeypatch):
    _patch(monkeypatch, b"not a zip")
    with pytest.raises(g.Error) as exc:
        g.artifact_file("o/r", 9, "r.json")
    assert exc.value.code == "invalid_artifact_archive"


def test_artifact_listing_requires_rows_of_the_requested_run(monkeypatch):
    rows = [{"id": 1, "name": "a", "workflow_run": {"id": 5, "head_sha": "a" * 40}}]
    monkeypatch.setattr(g, "_page", lambda path, **k: ({"total_count": 1, "artifacts": rows}, {}))
    assert g.artifacts("o/r", 5)["complete"]
    with pytest.raises(g.Error):
        g.artifacts("o/r", 6)


# ──── workflow_run_discovery ──────────────────────────────────────

def _workflow(*args, **kwargs):
    return {"id": 7, "path": ".github/workflows/ci.yml"}


def _page(path, **kwargs):
    assert "/workflows/7/runs?" in path
    if parse_qs(urlsplit("https://api.github.test/" + path).query).get("page") == ["2"]:
        return {"workflow_runs": []}, {}
    return {"workflow_runs": [
        {"id": 101, "run_attempt": 2, "workflow_id": 7, "head_sha": "a" * 40,
         "head_branch": "main", "event": "push", "status": "completed"},
        {"id": 102, "run_attempt": 1, "workflow_id": 7, "head_sha": "b" * 40,
         "head_branch": "feature", "event": "pull_request", "status": "completed"}
    ]}, {}


def test_workflow_discovery_filters(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    monkeypatch.setattr(g, "_page", _page)
    out = g.workflow_run_discovery("owner/repo", 7, branch="main", head_sha="a" * 40,
                                      event="push", page_size=5)
    assert out["complete"] and not out["truncated"]
    assert [(x["run_id"], x["attempt"]) for x in out["runs"]] == [(101, 2)]


def test_workflow_discovery_limit(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    monkeypatch.setattr(g, "_page", _page)
    out = g.workflow_run_discovery("owner/repo", 7, max_items=1, page_size=2)
    assert out["truncated"] and out["limit_reason"] == "max_items"
    assert out["next_page"] == 1
    assert out["next_offset"] == 1


@pytest.mark.parametrize("kwargs", [
    {"page_size": 101}, {"page_size": 0}, {"start_page": 0},
    {"max_pages": 0}, {"max_items": 0}, {"head_sha": "not-a-sha"},
])
def test_workflow_discovery_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        g.workflow_run_discovery("owner/repo", 7, **kwargs)


def test_workflow_discovery_rejects_wrong_workflow(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    monkeypatch.setattr(g, "_page", lambda *a, **k: ({"workflow_runs": [
        {"id": 101, "run_attempt": 1, "workflow_id": 8}]}, {}))
    with pytest.raises(g.Error):
        g.workflow_run_discovery("owner/repo", 7)


def test_resume_within_page_without_gap(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    monkeypatch.setattr(g, "_page", _page)
    first = g.workflow_run_discovery("owner/repo", 7, max_items=1, page_size=2)
    second = g.workflow_run_discovery("owner/repo", 7, max_items=2, page_size=2,
                                         start_page=first["next_page"],
                                         start_offset=first["next_offset"])
    assert [x["run_id"] for x in first["runs"] + second["runs"]] == [101, 102]
    assert second["complete"]


def test_resume_rejects_bad_offset():
    with pytest.raises(ValueError):
        g.workflow_run_discovery("owner/repo", 7, page_size=2, start_offset=2)


def test_page_limit_returns_resume_position(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    monkeypatch.setattr(g, "_page", _page)
    out = g.workflow_run_discovery("owner/repo", 7, page_size=2, max_pages=1)
    assert out["truncated"] and not out["complete"]
    assert out["limit_reason"] == "max_pages"
    assert (out["next_page"], out["next_offset"]) == (2, 0)


def test_resume_at_page_boundary(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    monkeypatch.setattr(g, "_page", _page)
    out = g.workflow_run_discovery("owner/repo", 7, page_size=2, max_items=2)
    assert out["truncated"]
    assert (out["next_page"], out["next_offset"]) == (2, 0)
    resumed = g.workflow_run_discovery("owner/repo", 7, page_size=2,
                                          start_page=2, start_offset=0)
    assert resumed["complete"] and resumed["runs"] == []


def test_invalid_run_identity_fails_closed(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    monkeypatch.setattr(g, "_page", lambda *a, **k: ({"workflow_runs": [
        {"id": True, "run_attempt": 1, "workflow_id": 7}]}, {}))
    with pytest.raises(g.Error):
        g.workflow_run_discovery("owner/repo", 7)


def test_duplicate_run_identity_fails_closed(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    row = {"id": 100, "run_attempt": 1, "workflow_id": 7}
    monkeypatch.setattr(g, "_page", lambda *a, **k: ({"workflow_runs": [row, row]}, {}))
    with pytest.raises(g.Error):
        g.workflow_run_discovery("owner/repo", 7)


def test_transport_failure_propagates(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    def broken(*args, **kwargs):
        raise g.Error("operation_timeout")
    monkeypatch.setattr(g, "_page", broken)
    with pytest.raises(g.Error):
        g.workflow_run_discovery("owner/repo", 7)


def test_malformed_page_fails_closed(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    monkeypatch.setattr(g, "_page", lambda *a, **k: ({"workflow_runs": None}, {}))
    with pytest.raises(g.Error):
        g.workflow_run_discovery("owner/repo", 7)


def test_three_pages_resume_across_boundary(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    records = [
        {"id": 201+i, "run_attempt": 1, "workflow_id": 7,
         "head_sha": "a"*40, "head_branch": "main", "event": "push"}
        for i in range(5)
    ]
    visited = []
    def page(path, **kwargs):
        q = parse_qs(urlsplit("https://api.github.test/" + path).query)
        number = int(q["page"][0])
        visited.append(number)
        return {"workflow_runs": records[(number-1)*2:number*2]}, {}
    monkeypatch.setattr(g, "_page", page)
    cursor = (1, 0)
    found = []
    for _ in range(4):
        out = g.workflow_run_discovery("owner/repo", 7, page_size=2,
                 max_items=2, start_page=cursor[0], start_offset=cursor[1])
        found.extend(row["run_id"] for row in out["runs"])
        if out["complete"]:
            break
        cursor = (out["next_page"], out["next_offset"])
    assert found == [201, 202, 203, 204, 205]
    assert visited == [1, 2, 3]


def test_cumulative_byte_limit_fails_closed(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    def page(path, **kwargs):
        g._BUDGET.get().charge("bytes", 32)
        return {"workflow_runs": []}, {}
    monkeypatch.setattr(g, "_page", page)
    with g.operation(max_bytes=16):
        with pytest.raises(g.Error) as exc:
            g.workflow_run_discovery("owner/repo", 7)
    assert exc.value.code == "bytes_limit"


def test_expired_deadline_fails_closed(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    def page(path, **kwargs):
        g._BUDGET.get().deadline = 0
        g._BUDGET.get().remaining()
    monkeypatch.setattr(g, "_page", page)
    with pytest.raises(g.Error) as exc:
        g.workflow_run_discovery("owner/repo", 7)
    assert exc.value.code == "operation_timeout"


def test_real_http_worker_byte_limit(monkeypatch):
    """Exercise urllib worker and parent byte cap using a local HTTP fixture."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps({"workflow_runs": [{"id": 1, "run_attempt": 1,
                       "workflow_id": 7, "padding": "x"*20000}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(g, "API", "http://127.0.0.1:%d" % server.server_port)
    monkeypatch.setattr(g, "token", lambda: None)
    try:
        with g.operation(max_bytes=4096):
            with pytest.raises(g.Error) as exc:
                g._page("repos/owner/repo/actions/workflows/7/runs?page=1",
                          transport="urllib", timeout=5)
        assert exc.value.code == "bytes_limit"
    finally:
        server.shutdown()
        server.server_close()


def test_real_http_worker_timeout(monkeypatch):
    """Slow local server must not bypass the parent's overall deadline."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.3)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"workflow_runs":[]}')
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(g, "API", "http://127.0.0.1:%d" % server.server_port)
    monkeypatch.setattr(g, "token", lambda: None)
    try:
        with g.operation(timeout=0.05):
            with pytest.raises(g.Error) as exc:
                g._page("repos/owner/repo/actions/workflows/7/runs?page=1",
                          transport="urllib", timeout=5)
        assert exc.value.code == "operation_timeout"
    finally:
        server.shutdown()
        server.server_close()


def test_changed_history_rejects_out_of_range_resume(monkeypatch):
    monkeypatch.setattr(g, "workflow", _workflow)
    monkeypatch.setattr(g, "_page", lambda *a, **k: ({"workflow_runs": [
        {"id": 100, "run_attempt": 1, "workflow_id": 7}]}, {}))
    with pytest.raises(g.Error) as exc:
        g.workflow_run_discovery("owner/repo", 7, page_size=3,
                                   start_page=1, start_offset=2)
    assert exc.value.code == "stale_continuation"


# ──── head_gates ──────────────────────────────────────────────────

# Gate-style head observations: runs for one head, checks, and PR staleness.


def _query(path):
    return parse_qs(urlsplit("https://api.github.test/" + path).query)


def _run(i, conclusion="success", status="completed", sha=SHA, name="CI"):
    return {"id": i, "run_attempt": 1, "workflow_id": 7, "name": name, "status": status,
            "conclusion": conclusion, "head_sha": sha, "head_branch": "b", "event": "pull_request"}


def test_run_history_filters_server_side_and_still_verifies(monkeypatch):
    seen = []

    def page(path, **kwargs):
        seen.append(path)
        # A wrong-head row from the server must still be dropped client-side.
        return {"workflow_runs": [_run(1), _run(2, sha="b" * 40)]}, {}

    monkeypatch.setattr(g, "_page", page)
    out = g.run_history("o/r", head_sha=SHA, event="pull_request")
    assert _query(seen[0])["head_sha"] == [SHA] and _query(seen[0])["event"] == ["pull_request"]
    assert [x["run_id"] for x in out["runs"]] == [1]
    assert (out["state"], out["by_conclusion"]) == ("green", {"success": 1})


@pytest.mark.parametrize(("rows", "state"), [
    ([], "missing"),
    ([_run(1), _run(2, conclusion=None, status="in_progress")], "pending"),
    ([_run(1), _run(2, conclusion="failure")], "failed"),
    ([_run(1), _run(2, conclusion="skipped")], "green"),
])
def test_run_history_state(monkeypatch, rows, state):
    monkeypatch.setattr(g, "_page", lambda path, **k: ({"workflow_runs": rows}, {}))
    assert g.run_history("o/r", head_sha=SHA)["state"] == state


def test_workflow_discovery_sends_head_sha_to_the_api(monkeypatch):
    seen = []
    monkeypatch.setattr(g, "workflow", lambda *a, **k: {"id": 7, "path": ".github/workflows/ci.yml"})
    monkeypatch.setattr(g, "_page", lambda path, **k: (seen.append(path) or {"workflow_runs": [_run(1)]}, {}))
    g.workflow_run_discovery("o/r", 7, head_sha=SHA)
    assert _query(seen[0])["head_sha"] == [SHA]


def test_summarize_checks_counts_by_conclusion():
    rows = [{"name": "a", "status": "completed", "conclusion": "success"},
            {"name": "b", "status": "completed", "conclusion": "skipped"},
            {"name": "c", "status": "in_progress", "conclusion": None}]
    assert g.summarize_checks(rows, 3)["by_conclusion"] == {"in_progress": 1, "skipped": 1, "success": 1}


@pytest.mark.parametrize(("state", "code"), [("green", 0), ("pending", 1), ("failed", 1), ("incomplete", 1)])
def test_checks_cli_exit_code_follows_state(monkeypatch, capsys, state, code):
    monkeypatch.setattr(g, "checks_for_sha", lambda *a, **k: {"state": state})
    assert g.main(["checks", "o/r", SHA]) == code
    assert json.loads(capsys.readouterr().out)["state"] == state


def test_head_runs_cli_rejects_a_non_sha(capsys):
    assert g.main(["head-runs", "o/r", "main"]) == 2


@pytest.mark.parametrize(("state", "stale", "code"), [("green", False, 0), ("green", True, 1), ("pending", False, 1)])
def test_observe_pr_cli_fails_on_stale_head_or_non_green_checks(monkeypatch, capsys, state, stale, code):
    monkeypatch.setattr(g, "observe_pr", lambda *a, **k: {"checks": {"state": state}, "stale": stale})
    assert g.main(["observe-pr", "o/r", "1"]) == code


def test_head_runs_can_exclude_dynamic_review_runs_and_says_so(monkeypatch):
    rows = [_run(1), {**_run(2, conclusion="failure"), "event": "dynamic", "name": "Running Copilot Code Review"}]
    monkeypatch.setattr(g, "_page", lambda path, **k: ({"workflow_runs": rows}, {}))
    assert g.run_history("o/r", head_sha=SHA)["state"] == "failed"   # default unchanged
    out = g.run_history("o/r", head_sha=SHA, exclude_events=("dynamic",))
    assert (out["state"], out["excluded_count"], out["filters"]["exclude_events"]) == ("green", 1, ["dynamic"])
