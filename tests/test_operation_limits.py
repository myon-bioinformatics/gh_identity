"""Operation budgets, injected pagination and real bounded transport tests."""
import contextlib
import io
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
import gh_identity as g

SHA = 'a' * 40
PATH = f'repos/o/r/commits/{SHA}/check-runs'


def row(n=1, conclusion='success'):
    return {'id': n, 'name': 'unit', 'status': 'completed', 'conclusion': conclusion,
            'html_url': f'https://github.com/o/r/check/{n}', 'output': {'annotations_count': n}}


def payload(rows, total=None):
    return {'check_runs': rows, 'total_count': len(rows) if total is None else total}


@pytest.mark.parametrize('max_pages,max_items,error', [(1, 1000, 'pages_limit'), (10, 99, 'items_limit')])
def test_never_ending_pagination_stops_before_false_green(max_pages, max_items, error):
    def get(*args, **kwargs):
        return payload([row(n) for n in range(100)], 10000)
    with g.operation(max_pages=max_pages, max_items=max_items):
        with pytest.raises(g.Error, match=error):
            g.checks_for_sha('o/r', SHA, requester=get)


def test_pages_limit_is_shared_across_calls():
    with g.operation(max_pages=1), patch.object(g, 'request', return_value=[]):
        assert g.pages('repos/o/r/issues') == []
        with pytest.raises(g.Error, match='pages_limit'):
            g.comments('o/r', 1)


def test_item_limit_counts_filtered_out_history():
    batch = {'workflow_runs': [{'head_sha': 'b' * 40}] * 100}
    with g.operation(max_items=99), patch.object(g, 'request', return_value=batch):
        with pytest.raises(g.Error, match='items_limit'):
            g.run_history('o/r', head_sha=SHA)


def test_operation_deadline_not_reset_per_page():
    clock = [100.0]
    timeouts = []
    def get(*args, **kwargs):
        timeouts.append(kwargs['timeout'])
        clock[0] += .6
        return [1] * 100
    with patch.object(g.time, 'monotonic', side_effect=lambda: clock[0]):
        with g.operation(timeout=1), pytest.raises(g.Error, match='operation_timeout'):
            g.pages('repos/o/r/issues', requester=get)
    assert timeouts == pytest.approx([1, .4])


@pytest.mark.parametrize('limits', [{'max_pages': 0}, {'max_items': True}, {'max_bytes': -1},
    {'timeout': 0}, {'timeout': float('nan')}, {'timeout': float('inf')}])
def test_invalid_budget(limits):
    with pytest.raises(ValueError):
        with g.operation(**limits):
            pytest.fail('invalid limits accepted')


def test_nested_operation_cannot_reset_limits():
    with g.operation(max_pages=1), pytest.raises(ValueError):
        with g.operation():
            pass


def test_link_pagination_keeps_injectable_client_and_normalized_identity():
    # Legacy Client adapters expose data/headers, preserving offline injection.
    responses = [
        g.Page(payload([row(1)], 2), {'Link': f'<https://api.github.com/{PATH}?per_page=100&page=2>; rel="next"'}),
        g.Page(payload([row(2, 'failure')], 2), {'link': ''}),
    ]
    with patch.object(g, 'request', side_effect=AssertionError('default transport used')):
        with patch('builtins.print'):
            calls = []
            def injected(method, path, **kwargs):
                calls.append((method, path))
                return responses.pop(0)
            result = g.checks_for_sha('o/r', SHA, requester=injected)
    assert result['complete'] is True
    assert result['state'] == 'failed'
    assert [r['id'] for r in result['checks']] == [1, 2]
    assert [r['annotations_count'] for r in result['checks']] == [1, 2]
    assert calls[-1] == ('GET', PATH + '?per_page=100&page=2')


def test_101_rows_without_link_uses_numbered_pagination():
    batches = [payload([row(n) for n in range(100)], 101), payload([row(101, 'failure')], 101)]
    with patch.object(g, 'request', side_effect=batches):
        result = g.checks_for_sha('o/r', SHA)
    assert result['count'] == 101 and result['state'] == 'failed'


@pytest.mark.parametrize('target', [
    'https://evil.invalid/anything?per_page=100&page=2',
    f'https://api.github.com/{PATH}?per_page=100&page=1',
    f'https://api.github.com/{PATH}?per_page=100&page=3',
    f'https://api.github.com/{PATH}?per_page=100&page=2&other=1',
])
def test_unsafe_or_cyclic_link_rejected_before_followup(target):
    calls = []
    def get(*args, **kwargs):
        calls.append(args)
        return g.Page(payload([row()], 2), {'link': f'<{target}>; rel="next"'})
    with pytest.raises(g.Error, match='invalid_pagination_link'):
        g.checks_for_sha('o/r', SHA, requester=get)
    assert len(calls) == 1


@pytest.mark.parametrize('total', [None, True, -1, '1'])
def test_missing_or_invalid_total_cannot_be_complete(total):
    with pytest.raises(g.Error, match='pagination_incomplete'):
        g.checks_for_sha('o/r', SHA, requester=lambda *a, **k: payload([row()], total) if total is not None else {'check_runs': [row()]})


def test_changed_total_rejected():
    batches = [payload([row(n) for n in range(100)], 101), payload([row(101)], 102)]
    with patch.object(g, 'request', side_effect=batches), pytest.raises(g.Error, match='pagination_incomplete'):
        g.checks_for_sha('o/r', SHA)


def test_short_incomplete_response_is_not_green():
    result = g.checks_for_sha('o/r', SHA, requester=lambda *a, **k: payload([row()], 2))
    assert result['state'] == 'incomplete' and not result['complete']


def test_injected_http_error_propagates_without_partial_success():
    def get(*a, **k):
        raise g.Error('permission_or_rate_limit')
    with pytest.raises(g.Error, match='permission_or_rate_limit'):
        g.checks_for_sha('o/r', SHA, requester=get)


def test_injected_payload_bytes_are_counted():
    with g.operation(max_bytes=10), pytest.raises(g.Error, match='bytes_limit'):
        g.pages('repos/o/r/issues', requester=lambda *a, **k: ['long' * 20])


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_real_process_overflow_stops_both_pipes(stream):
    code = f'import sys,time;sys.{stream}.buffer.write(b"x"*1000000);sys.{stream}.flush();time.sleep(10)'
    started = time.monotonic()
    with g.operation(max_bytes=128, timeout=3), pytest.raises(g.Error, match='bytes_limit'):
        g._process([sys.executable, '-c', code], None, 3)
    assert time.monotonic() - started < 3


def test_real_process_timeout_marks_write_uncertain():
    with g.operation(timeout=.4), pytest.raises(g.Error) as exc:
        g._process([sys.executable, '-c', 'import time;time.sleep(10)'], None, 10, mutating=True)
    assert exc.value.code == 'operation_timeout' and exc.value.uncertain


def test_byte_budget_is_cumulative_across_processes():
    with g.operation(max_bytes=3):
        assert g._process([sys.executable, '-c', 'print(1)'], None, 3)[1] == b'1\n'
        with pytest.raises(g.Error, match='bytes_limit'):
            g._process([sys.executable, '-c', 'print(1)'], None, 3)


@pytest.mark.parametrize('output,expected', [('print("{}")', {}), ('print("日本語")', 'invalid_json')])
def test_gh_actual_pipe_decode(output, expected):
    real = g._process
    def fake_gh(argv, stdin, timeout, env=None, mutating=False):
        assert argv[:4] == ['gh', 'api', '--method', 'GET']
        assert env['GH_PROMPT_DISABLED'] == '1' and 'GH_REPO' not in env
        return real([sys.executable, '-c', output], stdin, timeout, env, mutating)
    with patch.object(g, '_process', side_effect=fake_gh):
        if isinstance(expected, str):
            with pytest.raises(g.Error, match=expected):g._gh('GET', 'repos/o/r')
        else:assert g._gh('GET', 'repos/o/r') == expected


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):pass
    def do_GET(self):
        if self.path == '/slow':time.sleep(2)
        status = int(self.path[1:]) if self.path[1:].isdigit() else 200
        body = b'x' * 10000 if self.path == '/large' else b'{"ok":true}'
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):pass


@pytest.fixture(scope='module')
def local_http():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown();server.server_close();thread.join()


def test_real_urllib_worker_and_json(local_http):
    with patch.object(g, 'API', local_http), patch.object(g, 'token', return_value=None):
        assert g.request('GET', 'ok', transport='urllib') == {'ok': True}


@pytest.mark.parametrize('status,code', [(401, 'authentication_required'), (403, 'permission_or_rate_limit'),
    (404, 'not_found_or_inaccessible'), (422, 'rejected'), (500, 'server_error')])
def test_urllib_http_error_mapping(local_http, status, code):
    with patch.object(g, 'API', local_http), patch.object(g, 'token', return_value=None):
        with pytest.raises(g.Error, match=code):g.request('GET', str(status), transport='urllib')


def test_urllib_response_capacity(local_http):
    with patch.object(g, 'API', local_http), patch.object(g, 'token', return_value=None):
        with g.operation(max_bytes=128), pytest.raises(g.Error, match='bytes_limit'):
            g.request('GET', 'large', transport='urllib')


def test_urllib_hard_wall_deadline(local_http):
    started = time.monotonic()
    with patch.object(g, 'API', local_http), patch.object(g, 'token', return_value=None):
        with g.operation(timeout=.5), pytest.raises(g.Error):
            g.request('GET', 'slow', transport='urllib')
    assert time.monotonic() - started < 1.8


def test_cli_limit_is_applied_without_success_output():
    with patch.object(g, 'request', return_value=[{}] * 100):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            assert g.main(['comments', 'o/r', '1', '--max-pages', '1']) == 2
    assert out.getvalue() == ''
    assert json.loads(err.getvalue())['error'] == 'pages_limit'


def test_explicit_headers_without_next_finish_exactly_full_page():
    calls = []
    def get(*args, **kwargs):
        calls.append(args)
        return g.Page(payload([row(n) for n in range(100)]), {})
    with g.operation(max_pages=1):
        result = g.checks_for_sha('o/r', SHA, requester=get)
    assert result['complete'] and result['count'] == 100 and len(calls) == 1


def test_explicit_link_end_with_count_mismatch_is_incomplete():
    result = g.checks_for_sha('o/r', SHA,
        requester=lambda *a, **k: g.Page(payload([row()], 2), {}))
    assert result['state'] == 'incomplete'


def test_auto_fallback_shares_deadline_and_bytes():
    seen = []
    def gh(*a):
        g._BUDGET.get().charge('bytes', 5)
        raise g.Error('authentication_required')
    def url(*a):
        seen.append(g._BUDGET.get().bytes)
        g._BUDGET.get().charge('bytes', 6)
    with patch.object(g, 'gh_available', return_value=True), patch.object(g, '_gh', side_effect=gh), patch.object(g, '_url', side_effect=url):
        with g.operation(max_bytes=10), pytest.raises(g.Error, match='bytes_limit'):
            g.request('GET', 'repos/o/r')
    assert seen == [5]


@pytest.mark.parametrize('timeout', [-1, True, float('nan'), float('inf')])
def test_nested_request_validates_timeout_before_transport(timeout):
    with g.operation(), patch.object(g, '_gh') as transport:
        with pytest.raises(ValueError):
            g.request('GET', 'repos/o/r', timeout=timeout)
        transport.assert_not_called()
