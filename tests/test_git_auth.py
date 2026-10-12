"""Real Git transports against controlled wrappers/helpers; no user credentials."""
import base64
import functools
import http.server
import json
import os
import shlex
import shutil
import ssl
import subprocess
import threading
import time

import pytest
import gh_identity as g
from test_git_work import clones, _git

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='controlled POSIX transport wrappers')


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith('GIT_') or key.startswith('GCM_') or key.startswith('SSH_'):
            monkeypatch.delenv(key, raising=False)
    config = tmp_path / 'global'
    config.write_text('')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', os.devnull)
    return config


def script(path, body):
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)
    return str(path)


def cfg(path, key, value):
    subprocess.run(['git', 'config', '--file', str(path), '--add', key, value], check=True)


@pytest.mark.parametrize('source', ['command', 'local', 'global', 'system', 'executable'])
def test_ssh_success_and_priority(clones, isolated, tmp_path, monkeypatch, source):
    marker = tmp_path / 'called'
    good = script(tmp_path / 'ssh-good',
                  'echo called >> ' + shlex.quote(str(marker)) + '\n'
                  'for arg do last="$arg"; done\nexec sh -c "$last"\n')
    bad = script(tmp_path / 'ssh-bad', 'echo FAKE_SECRET_SSH >&2\nexit 71\n')
    work = clones['work']
    _git(work, 'remote', 'set-url', 'origin', 'ssh://test.invalid' + str(clones['bare']))
    monkeypatch.setenv('GIT_SSH_VARIANT', 'ssh')
    if source == 'command':
        monkeypatch.setenv('GIT_SSH_COMMAND', good)
        monkeypatch.setenv('GIT_SSH', bad)
        _git(work, 'config', 'core.sshCommand', bad)
    elif source == 'local':
        _git(work, 'config', 'core.sshCommand', good)
        cfg(isolated, 'core.sshCommand', bad)
        monkeypatch.setenv('GIT_SSH', bad)
    elif source == 'global':
        cfg(isolated, 'core.sshCommand', good)
        monkeypatch.setenv('GIT_SSH', bad)
    elif source == 'system':
        system = tmp_path / 'system'
        cfg(system, 'core.sshCommand', good)
        monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(system))
        monkeypatch.setenv('GIT_SSH', bad)
    else:
        monkeypatch.setenv('GIT_SSH', good)
    out = g.git_work(work, 'feature', 'auth-work', write=True, fetch_timeout=5)
    assert out['status'] == 'verified', out
    assert out['after']['head_sha'] == clones['feature']
    assert marker.exists()
    assert 'FAKE_SECRET' not in json.dumps(out)


@pytest.fixture
def https_remote(clones, tmp_path):
    if not shutil.which('openssl'):
        pytest.skip('openssl needed for ephemeral loopback TLS certificate')
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                   check=True, capture_output=True)
    _git(clones['bare'], 'update-server-info')
    seen = []
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            expected = 'Basic ' + base64.b64encode(b'test-user:FAKE_TEST_PASSWORD').decode()
            if self.headers.get('Authorization') != expected:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="test"')
                self.end_headers()
                return
            seen.append(self.path)
            super().do_GET()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                functools.partial(Handler, directory=str(tmp_path)))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    work = clones['work']
    _git(work, 'fetch', '--unshallow')  # dumb HTTPS serves objects, not shallow negotiation
    _git(work, 'config', 'http.sslCAInfo', str(cert))
    _git(work, 'remote', 'set-url', 'origin', f'https://localhost:{server.server_port}/remote.git')
    try:
        yield seen
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize('mode', ['global', 'scoped', 'local-reset', 'empty', 'fail', 'hang'])
def test_https_helper_paths(clones, isolated, tmp_path, monkeypatch, https_remote, mode):
    marker = tmp_path / 'helper-called'
    helper = script(tmp_path / 'helper',
        'echo "$1" >> ' + shlex.quote(str(marker)) + '\n'
        'cat >/dev/null\nif [ "$1" = get ]; then\n'
        'printf "username=test-user\\npassword=FAKE_TEST_PASSWORD\\n\\n"\nfi\n')
    bad = script(tmp_path / 'bad', 'echo FAKE_TEST_PASSWORD >&2\ncat >/dev/null\nexit 1\n')
    prompt = tmp_path / 'prompt-called'
    ask = script(tmp_path / 'ask', 'touch ' + shlex.quote(str(prompt)) + '\nexit 1\n')
    monkeypatch.setenv('GIT_ASKPASS', ask)
    _git(clones['work'], 'config', 'core.askPass', ask)
    if mode == 'scoped':
        origin = _git(clones['work'], 'remote', 'get-url', 'origin')
        cfg(isolated, 'credential.' + origin.rsplit('/', 1)[0] + '.helper', helper)
        cfg(isolated, 'credential.https://elsewhere.invalid.helper', bad)
    elif mode == 'local-reset':
        cfg(isolated, 'credential.helper', bad)
        _git(clones['work'], 'config', '--add', 'credential.helper', '')
        _git(clones['work'], 'config', '--add', 'credential.helper', helper)
    elif mode == 'global':
        included = tmp_path / 'included'
        cfg(included, 'credential.helper', '')
        cfg(included, 'credential.helper', helper)
        cfg(isolated, 'include.path', str(included))
        cfg(isolated, 'url.https://elsewhere.invalid/.insteadOf', 'https://localhost:')
        cfg(isolated, 'core.fsmonitor', bad)
    elif mode == 'fail':
        cfg(isolated, 'credential.helper', bad)
    elif mode == 'hang':
        cfg(isolated, 'credential.helper', script(tmp_path / 'hang', 'sleep 30\n'))
    start = time.monotonic()
    out = g.git_work(clones['work'], 'feature', 'auth-work', write=True, fetch_timeout=1 if mode == 'hang' else 5)
    assert time.monotonic() - start < 10
    assert not prompt.exists()
    assert 'FAKE_TEST_PASSWORD' not in json.dumps(out)
    if mode in ('empty', 'fail', 'hang'):
        assert out['status'] == 'mutation_failed'
        assert out['completed_steps'] == []
        assert _git(clones['work'], 'branch', '--show-current') == 'main'
    else:
        assert out['status'] == 'verified', out
        assert https_remote and marker.exists()


def test_auth_snapshot_is_bounded_and_removed(clones, isolated, tmp_path, monkeypatch):
    cfg(isolated, 'credential.helper', 'x' * 5000)
    with pytest.raises(g.GitInspectionError, match='output limit'):
        g._git_run(clones['work'], ['fetch', 'origin'], max_bytes=100, timeout=2, inherit_auth=True)
    paths = []
    real = g._git_spawn
    def spy(command, **kwargs):
        path = kwargs['env'].get('GIT_CONFIG_GLOBAL')
        if path and 'ghi-auth-' in path:
            paths.append(path)
            assert os.stat(path).st_mode & 0o777 == 0o600
        return real(command, **kwargs)
    monkeypatch.setattr(g, '_git_spawn', spy)
    out = g.git_work(clones['work'], 'feature', 'auth-work', write=True)
    assert out['status'] == 'verified'
    assert paths and all(not os.path.exists(path) for path in paths)


def test_ssh_failure_kills_children_and_hides_stderr(clones, isolated, tmp_path, monkeypatch):
    pidfile = tmp_path / 'child-pid'
    wrapper = script(tmp_path / 'ssh',
        'echo FAKE_SECRET_SSH >&2\nsleep 30 &\necho $! > ' + shlex.quote(str(pidfile)) + '\nwait\n')
    monkeypatch.setenv('GIT_SSH_COMMAND', wrapper)
    monkeypatch.setenv('GIT_SSH_VARIANT', 'ssh')
    monkeypatch.setenv('GIT_TRACE', str(tmp_path / 'trace'))
    monkeypatch.setenv('GIT_TRACE_CURL', str(tmp_path / 'curl-trace'))
    _git(clones['work'], 'remote', 'set-url', 'origin', 'ssh://test.invalid' + str(clones['bare']))
    # Remove trace output from test setup itself; only production behavior is checked.
    for path in (tmp_path / 'trace', tmp_path / 'curl-trace'):
        path.unlink(missing_ok=True)
    out = g.git_work(clones['work'], 'feature', 'auth-work', write=True, fetch_timeout=0.5)
    assert out['status'] == 'mutation_failed' and 'timed out' in out['error']
    assert 'FAKE_SECRET_SSH' not in json.dumps(out)
    assert not (tmp_path / 'trace').exists() and not (tmp_path / 'curl-trace').exists()
    if os.path.exists('/proc'):
        stat = '/proc/' + pidfile.read_text().strip() + '/stat'
        # A reparented zombie is dead and cannot keep transfer pipes open.
        assert not os.path.exists(stat) or open(stat).read().split()[2] == 'Z'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), True, 0])
def test_auth_timeout_validation(clones, value):
    with pytest.raises(ValueError):
        g.git_work(clones['work'], 'feature', 'auth-work', fetch_timeout=value)
