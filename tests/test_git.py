"""🌿 Local Git: inspection, position and overview, switch, work, merge trials, transports and auth.

Sections (each was one file; `-k` selects inside them): git_work, git_auth, git_inspection, git_switch, git_trials, local_position, default_overview.
"""

from pathlib import Path
import base64
import functools
import http.server
import io
import json
import os
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
import time

import pytest

from conftest import _git, _git_tester

import gh_identity as g


# ──── git_work ────────────────────────────────────────────────────

# git-work: a work branch from a PR head or remote branch, with its upstream config written directly.


@pytest.fixture
def clones(tmp_path):
    """A bare 'GitHub' with main, a feature branch and a PR head ref, plus a shallow single-branch clone."""
    seed, bare, work = tmp_path / "seed", tmp_path / "remote.git", tmp_path / "work"
    _git_tester(tmp_path, "init", "-q", "-b", "main", str(seed))
    for i in range(3):
        _git_tester(seed, "commit", "-q", "--allow-empty", "-m", f"main {i}")
    _git_tester(seed, "switch", "-q", "-c", "feature")
    _git_tester(seed, "commit", "-q", "--allow-empty", "-m", "feature")
    _git_tester(seed, "switch", "-q", "main")
    _git_tester(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    _git_tester(bare, "update-ref", "refs/pull/7/head", "refs/heads/feature")
    _git_tester(tmp_path, "clone", "-q", "--depth", "1", "--single-branch", "file://" + str(bare), str(work))
    return {"bare": bare, "work": work, "feature": _git_tester(bare, "rev-parse", "feature")}


def test_plan_changes_nothing(clones):
    work = clones["work"]
    out = g.git_work(work, "#7", "work/review-7")
    assert out["status"] == "planned" and out["shallow"] is True
    assert out["commands"][0] == ["git", "fetch", "--no-tags", "--depth", "1", "origin",
                                  "+refs/pull/7/head:refs/remotes/origin/pr/7"]
    assert out["commands"][1] == ["git", "switch", "-c", "work/review-7", "--no-track", "refs/remotes/origin/pr/7"]
    assert _git_tester(work, "branch", "--show-current") == "main"
    assert _git_tester(work, "branch", "--list", "work/review-7") == ""


def test_pr_head_in_shallow_single_branch_clone(clones):
    """`git branch -u origin/X` fails here ('not a branch'); git-work still ends tracked."""
    work = clones["work"]
    out = g.git_work(work, "#7", "work/review-7", write=True)
    assert out["status"] == "verified", out
    assert out["after"] == {"branch": "work/review-7", "head_sha": clones["feature"]}
    assert out["upstream_config"] == {"remote": "origin", "merge": "refs/heads/work/review-7"}
    assert _git_tester(work, "rev-parse", "--is-shallow-repository") == "true"  # not silently deepened
    # A plain push creates the remote branch, after which status shows the tracking pair.
    _git_tester(work, "commit", "-q", "--allow-empty", "-m", "review fix")
    assert out["refspec_added"] == "+refs/heads/work/review-7:refs/remotes/origin/work/review-7"
    _git_tester(work, "push", "-q")  # updates the tracking ref through the added refspec
    assert _git_tester(work, "status", "-sb").splitlines()[0] == "## work/review-7...origin/work/review-7"


def test_remote_branch_source_with_different_upstream(clones):
    work = clones["work"]
    out = g.git_work(work, "feature", "local-feature", upstream="feature", write=True)
    assert out["status"] == "verified" and out["source"] == {"kind": "branch", "ref": "refs/heads/feature"}
    assert out["tracking_ref"] == "refs/remotes/origin/feature"
    assert out["upstream_config"]["merge"] == "refs/heads/feature"
    assert _git_tester(work, "rev-parse", "--abbrev-ref", "@{u}") == "origin/feature"


def test_full_clone_refspec_is_not_duplicated(tmp_path, clones):
    full = tmp_path / "full"
    _git_tester(tmp_path, "clone", "-q", "file://" + str(clones["bare"]), str(full))
    out = g.git_work(full, "#7", "w", write=True)
    assert out["status"] == "verified" and out["refspec_added"] is None
    assert _git_tester(full, "config", "--get-all", "remote.origin.fetch") == "+refs/heads/*:refs/remotes/origin/*"


@pytest.mark.parametrize(("source", "branch", "kwargs", "error"), [
    ("#7", "main", {}, g.GitInspectionError),              # existing branch is refused, never reset
    ("#7", "bad..name", {}, ValueError),
    ("-x", "w", {}, ValueError),                            # option injection
    ("#7", "w", {"remote": "nowhere"}, g.GitInspectionError),
    ("#7", "w", {"remote": "a b"}, ValueError),
    ("#7", "w", {"depth": 0}, ValueError),
    (None, "w", {}, ValueError),
])
def test_invalid_input_fails_before_any_change(clones, source, branch, kwargs, error):
    work = clones["work"]
    with pytest.raises(error):
        g.git_work(work, source, branch, write=True, **kwargs)
    assert _git_tester(work, "branch", "--show-current") == "main"
    assert _git_tester(work, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/pr") == ""


def test_missing_pr_is_reported_not_raised(clones):
    out = g.git_work(clones["work"], "#999", "w", write=True)
    assert (out["status"], out["mutation_status"], out["completed_steps"]) == ("mutation_failed", "failed", [])
    assert _git_tester(clones["work"], "branch", "--show-current") == "main"


def test_cli_plan_and_exit_codes(clones, capsys):
    assert g.main(["git-work", "--root", str(clones["work"]), "pr/7", "w"]) == 0
    assert '"status": "planned"' in capsys.readouterr().out
    assert g.main(["git-work", "--root", str(clones["work"]), "#7", "main"]) == 2


def test_fetch_never_prompts_for_credentials(clones, monkeypatch):
    """A remote that would ask for a password fails fast as mutation_failed, nothing switched."""
    work = clones["work"]
    _git_tester(work, "remote", "set-url", "origin", "https://example.invalid/private.git")
    seen = {}
    real = g._git_spawn

    def spy(command, **kwargs):
        if "fetch" in command:
            seen.update(kwargs["env"])
        return real(command, **kwargs)
    monkeypatch.setattr(g, "_git_spawn", spy)
    out = g.git_work(work, "#7", "w", write=True, fetch_timeout=20)
    assert seen["GIT_TERMINAL_PROMPT"] == "0" and "BatchMode=yes" in seen["GIT_SSH_COMMAND"]
    assert (out["status"], out["completed_steps"]) == ("mutation_failed", [])
    assert _git_tester(work, "branch", "--show-current") == "main"


def test_stalled_fetch_is_killed_at_the_timeout(clones, tmp_path, monkeypatch):
    """A transfer that never finishes is bounded: the fetch is killed and reported, not awaited."""
    import time
    hang = tmp_path / "hang-upload-pack"
    hang.write_text("#!/bin/sh\nsleep 30\n")
    hang.chmod(0o755)
    work = clones["work"]
    _git_tester(work, "config", "remote.origin.uploadpack", str(hang))
    _git_tester(work, "remote", "set-url", "origin", str(clones["bare"]))  # local transport honours uploadpack
    start = time.monotonic()
    out = g.git_work(work, "#7", "w", write=True, fetch_timeout=1)
    assert time.monotonic() - start < 10
    assert out["status"] == "mutation_failed" and "timed out" in out["error"]
    assert _git_tester(work, "branch", "--show-current") == "main"


def test_fetch_timeout_must_be_positive(clones):
    with pytest.raises(ValueError):
        g.git_work(clones["work"], "#7", "w", fetch_timeout=0)


# ──── git_auth ────────────────────────────────────────────────────

# Real Git transports against controlled wrappers/helpers; no user credentials.


@pytest.fixture(autouse=False)
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


@pytest.mark.skipif(os.name != 'posix', reason='controlled POSIX transport wrappers')
@pytest.mark.usefixtures('isolated')
@pytest.mark.parametrize('source', ['command', 'local', 'global', 'system', 'executable'])
def test_ssh_success_and_priority(clones, isolated, tmp_path, monkeypatch, source):
    marker = tmp_path / 'called'
    good = script(tmp_path / 'ssh-good',
                  'echo called >> ' + shlex.quote(str(marker)) + '\n'
                  'for arg do last="$arg"; done\nexec sh -c "$last"\n')
    bad = script(tmp_path / 'ssh-bad', 'echo FAKE_SECRET_SSH >&2\nexit 71\n')
    work = clones['work']
    _git_tester(work, 'remote', 'set-url', 'origin', 'ssh://test.invalid' + str(clones['bare']))
    monkeypatch.setenv('GIT_SSH_VARIANT', 'ssh')
    if source == 'command':
        monkeypatch.setenv('GIT_SSH_COMMAND', good)
        monkeypatch.setenv('GIT_SSH', bad)
        _git_tester(work, 'config', 'core.sshCommand', bad)
    elif source == 'local':
        _git_tester(work, 'config', 'core.sshCommand', good)
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
    _git_tester(clones['bare'], 'update-server-info')
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
    _git_tester(work, 'fetch', '--unshallow')  # dumb HTTPS serves objects, not shallow negotiation
    _git_tester(work, 'config', 'http.sslCAInfo', str(cert))
    _git_tester(work, 'remote', 'set-url', 'origin', f'https://localhost:{server.server_port}/remote.git')
    try:
        yield seen
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.skipif(os.name != 'posix', reason='controlled POSIX transport wrappers')
@pytest.mark.usefixtures('isolated')
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
    _git_tester(clones['work'], 'config', 'core.askPass', ask)
    if mode == 'scoped':
        origin = _git_tester(clones['work'], 'remote', 'get-url', 'origin')
        cfg(isolated, 'credential.' + origin.rsplit('/', 1)[0] + '.helper', helper)
        cfg(isolated, 'credential.https://elsewhere.invalid.helper', bad)
    elif mode == 'local-reset':
        cfg(isolated, 'credential.helper', bad)
        _git_tester(clones['work'], 'config', '--add', 'credential.helper', '')
        _git_tester(clones['work'], 'config', '--add', 'credential.helper', helper)
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
        assert _git_tester(clones['work'], 'branch', '--show-current') == 'main'
    else:
        assert out['status'] == 'verified', out
        assert https_remote and marker.exists()


@pytest.mark.skipif(os.name != 'posix', reason='controlled POSIX transport wrappers')
@pytest.mark.usefixtures('isolated')
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


@pytest.mark.skipif(os.name != 'posix', reason='controlled POSIX transport wrappers')
@pytest.mark.usefixtures('isolated')
def test_ssh_failure_kills_children_and_hides_stderr(clones, isolated, tmp_path, monkeypatch):
    pidfile = tmp_path / 'child-pid'
    wrapper = script(tmp_path / 'ssh',
        'echo FAKE_SECRET_SSH >&2\nsleep 30 &\necho $! > ' + shlex.quote(str(pidfile)) + '\nwait\n')
    monkeypatch.setenv('GIT_SSH_COMMAND', wrapper)
    monkeypatch.setenv('GIT_SSH_VARIANT', 'ssh')
    monkeypatch.setenv('GIT_TRACE', str(tmp_path / 'trace'))
    monkeypatch.setenv('GIT_TRACE_CURL', str(tmp_path / 'curl-trace'))
    _git_tester(clones['work'], 'remote', 'set-url', 'origin', 'ssh://test.invalid' + str(clones['bare']))
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


@pytest.mark.skipif(os.name != 'posix', reason='controlled POSIX transport wrappers')
@pytest.mark.usefixtures('isolated')
@pytest.mark.parametrize('value', [float('nan'), float('inf'), True, 0])
def test_auth_timeout_validation(clones, value):
    with pytest.raises(ValueError):
        g.git_work(clones['work'], 'feature', 'auth-work', fetch_timeout=value)


# ──── git_inspection ──────────────────────────────────────────────

def git(root, *args, input=None):
    return subprocess.run(["git", "-C", str(root), *args], input=input,
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture User")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (root / "space 日本語.txt").write_text("hello\n", encoding="utf-8")
    git(root, "add", "--", "a.txt", "space 日本語.txt")
    git(root, "commit", "-qm", "initial")
    return root


def test_status_clean_modified_staged_untracked_and_rename(repo):
    assert g.git_status(repo) == {"clean": True, "records": [], "truncated": False}
    (repo / "a.txt").write_text("changed\ntwo\n", encoding="utf-8")
    (repo / "new file.txt").write_text("new", encoding="utf-8")
    dirty = g.git_status(repo)
    assert not dirty["clean"]
    assert {r["kind"] for r in dirty["records"]} >= {"1", "?"}
    git(repo, "add", "--", "a.txt")
    git(repo, "mv", "space 日本語.txt", "renamed 日本語.txt")
    staged = g.git_status(repo)
    renames = [r for r in staged["records"] if r["kind"] == "2"]
    assert len(renames) == 1
    assert renames[0]["path"] == "renamed 日本語.txt"
    assert renames[0]["orig_path"] == "space 日本語.txt"
    assert "renamed 日本語.txt" in renames[0]["record"]
    assert not any(r["record"] == "space 日本語.txt" for r in staged["records"])


def test_ls_files_is_nul_safe_and_bounded(repo):
    result = g.git_ls_files(repo)
    assert result["paths"] == ["a.txt", "space 日本語.txt"]
    assert not result["truncated"]
    assert g.git_ls_files(repo, max_files=1)["truncated"]


def test_ls_files_can_include_untracked_but_not_ignored(repo):
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (repo / "new 日本語.txt").write_text("new\n", encoding="utf-8")
    (repo / "ignored.log").write_text("ignored\n", encoding="utf-8")

    tracked = g.git_ls_files(repo)
    assert "new 日本語.txt" not in tracked["paths"]
    assert "ignored.log" not in tracked["paths"]

    observed = g.git_ls_files(repo, include_untracked=True)
    assert "a.txt" in observed["paths"]
    assert "space 日本語.txt" in observed["paths"]
    assert "new 日本語.txt" in observed["paths"]
    assert "ignored.log" not in observed["paths"]


def test_ls_files_combined_truncation_uses_git_output_prefix(repo):
    (repo / "0new.txt").write_text("new\n", encoding="utf-8")
    full = g.git_ls_files(repo, include_untracked=True)
    limited = g.git_ls_files(repo, include_untracked=True, max_files=1)
    # Git may emit --others before --cached. The contract is a bounded prefix
    # of Git's combined output, not a promise that tracked paths come first.
    assert limited == {"paths": full["paths"][:1], "truncated": True}
    assert "a.txt" in full["paths"]


def test_ls_files_include_untracked_requires_bool(repo):
    with pytest.raises(TypeError):
        g.git_ls_files(repo, include_untracked=1)


def test_diff_worktree_staged_revision_path_and_truncation(repo):
    first = git(repo, "rev-parse", "HEAD").strip()
    (repo / "a.txt").write_text("changed\ntwo\n", encoding="utf-8")
    worktree = g.git_diff(repo, path="a.txt")
    assert "+changed" in worktree["patch"]
    git(repo, "add", "--", "a.txt")
    assert "+changed" in g.git_diff(repo, staged=True)["patch"]
    git(repo, "commit", "-qm", "second")
    second = git(repo, "rev-parse", "HEAD").strip()
    assert "+changed" in g.git_diff(repo, base=first, head=second, path="a.txt")["patch"]
    assert g.git_diff(repo, base=first, head=second, max_bytes=10)["truncated"]
    with pytest.raises(ValueError):
        g.git_diff(repo, staged=True, base=first)
    with pytest.raises(ValueError):
        g.git_diff(repo, head=second)


def test_log_show_blame_and_grep(repo):
    rows = g.git_log(repo, max_count=1)["commits"]
    assert len(rows) == 1 and rows[0]["subject"] == "initial"
    shown = g.git_show(repo, "HEAD", path="a.txt")
    assert shown["content"] == "one\ntwo\n"
    blamed = g.git_blame(repo, "a.txt", start=1, end=1)
    assert "\tone" in blamed["porcelain"]
    matches = g.git_grep(repo, "hello")
    assert matches["paths"] == ["space 日本語.txt"]
    assert g.git_grep(repo, "absent")["paths"] == []


def test_paths_are_option_separated(repo):
    (repo / "--looks-like-option").write_text("x\n", encoding="utf-8")
    git(repo, "add", "--", "--looks-like-option")
    git(repo, "commit", "-qm", "option path")
    assert g.git_show(repo, "HEAD", path="--looks-like-option")["content"] == "x\n"
    assert g.git_blame(repo, "--looks-like-option")["porcelain"]


@pytest.mark.parametrize("revision", ["", "-n1", "bad\0ref"])
def test_revision_validation(repo, revision):
    with pytest.raises(ValueError):
        g.git_show(repo, revision)


def test_errors_are_not_clean(repo, tmp_path, monkeypatch):
    nonrepo = tmp_path / "not-a-repo"
    nonrepo.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(g.GitInspectionError):
        g.git_status(nonrepo)
    with pytest.raises(ValueError):
        g.git_status(tmp_path / "missing")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(g.GitInspectionError):
        g.git_status(repo)


def test_no_mutation_or_network_argv_reachable(monkeypatch, repo):
    seen = []
    real = g._git_spawn
    def recording(argv, **kwargs):
        seen.append(tuple(argv))
        return real(argv, **kwargs)
    monkeypatch.setattr(g, "_git_spawn", recording)
    g.git_status(repo)
    g.git_ls_files(repo)
    g.git_diff(repo)
    g.git_log(repo, max_count=1)
    g.git_log_numstat(repo, max_count=1)
    g.git_show(repo)
    g.git_blame(repo, "a.txt")
    g.git_grep(repo, "one")
    forbidden = {"add", "commit", "checkout", "switch", "restore", "reset",
                 "clean", "merge", "rebase", "fetch", "pull", "push"}
    for argv in seen:
        assert not forbidden.intersection(argv)


def test_log_numstat_unicode_controls_rename_binary_and_empty_commit(repo):
    names = ["tab\t日本語.txt", "line\nname.txt", " space.txt"]
    for name in names:
        (repo / name).write_text("one\ntwo\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"\0binary")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "special names")
    git(repo, "mv", "space 日本語.txt", "renamed\n日本語.txt")
    git(repo, "commit", "-qm", "rename")
    git(repo, "commit", "--allow-empty", "-qm", "empty")
    result = g.git_log_numstat(repo)
    assert not result["truncated"] and len(result["commits"]) == 4
    assert result["commits"][0]["files"] == []
    assert result["commits"][1]["files"] == [{
        "path": "renamed\n日本語.txt", "orig_path": "space 日本語.txt", "added": 0, "deleted": 0,
    }]
    rows = {r["path"]: r for r in result["commits"][2]["files"]}
    for name in names:
        assert rows[name]["added"] == 2 and rows[name]["deleted"] == 0
    assert rows["binary.bin"]["added"] is None and rows["binary.bin"]["deleted"] is None
    assert g.git_log_numstat(repo, max_count=2) == {
        "commits": result["commits"][:2], "truncated": True,
    }
    assert g.git_log_numstat(repo, since="2030-01-01") == {"commits": [], "truncated": False}


def test_log_numstat_preserves_mid_history_empty_and_no_ff_merge_commits(monkeypatch, repo):
    base_branch = git(repo, "branch", "--show-current").strip()

    (repo / "a.txt").write_text("before empty\n", encoding="utf-8")
    git(repo, "add", "--", "a.txt")
    git(repo, "commit", "-qm", "before empty")
    before_sha = git(repo, "rev-parse", "HEAD").strip()

    git(repo, "commit", "--allow-empty", "-qm", "middle empty")
    empty_sha = git(repo, "rev-parse", "HEAD").strip()

    git(repo, "checkout", "-qb", "numstat-feature")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "add", "--", "feature.txt")
    git(repo, "commit", "-qm", "feature change")
    feature_sha = git(repo, "rev-parse", "HEAD").strip()

    git(repo, "checkout", "-q", base_branch)
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    git(repo, "add", "--", "main.txt")
    git(repo, "commit", "-qm", "main change")
    main_sha = git(repo, "rev-parse", "HEAD").strip()

    git(repo, "merge", "--no-ff", "-qm", "merge feature", "numstat-feature")
    merge_sha = git(repo, "rev-parse", "HEAD").strip()

    (repo / "after.txt").write_text("after\n", encoding="utf-8")
    git(repo, "add", "--", "after.txt")
    git(repo, "commit", "-qm", "after merge")
    after_sha = git(repo, "rev-parse", "HEAD").strip()

    result = g.git_log_numstat(repo)
    assert not result["truncated"]
    observed = [commit["commit"] for commit in result["commits"]]
    expected = git(repo, "rev-list", "HEAD").splitlines()
    assert observed == expected

    by_sha = {commit["commit"]: commit for commit in result["commits"]}
    assert by_sha[empty_sha]["files"] == []
    assert by_sha[merge_sha]["files"] == []
    for sha in (before_sha, feature_sha, main_sha, after_sha):
        assert by_sha[sha]["files"]

    cut_points = {
        expected.index(empty_sha),
        expected.index(empty_sha) + 1,
        expected.index(merge_sha) + 1,
        len(expected),
    }
    for max_count in sorted(cut_points):
        bounded = g.git_log_numstat(repo, max_count=max_count)
        assert [commit["commit"] for commit in bounded["commits"]] == expected[:max_count]
        assert bounded["truncated"] is (max_count < len(expected))

    args = ["log", "--no-ext-diff", "--no-textconv", "--no-color",
            "-z", "--numstat", "--format=%x00%H%x00%cs",
            "--max-count=10001", "--"]
    raw, _, _ = g._git_run(repo, args)
    for limit in range(1, len(raw)):
        monkeypatch.setattr(g, "_git_run", lambda *a, **k: (raw[:limit], True, 0))
        truncated = g.git_log_numstat(repo)
        assert truncated["truncated"]
        assert truncated["commits"] == result["commits"][:len(truncated["commits"])]


def test_log_numstat_byte_bound_publishes_only_complete_commits(monkeypatch, repo):
    # Exhaustively cut an actual Git stream at every byte boundary, including
    # UTF-8 bytes, header delimiters and rename source/destination fields.
    git(repo, "mv", "space 日本語.txt", "renamed\n日本語.txt")
    git(repo, "commit", "-qm", "rename")
    raw, _, _ = g._git_run(repo, ["log", "-z", "--numstat", "--format=%x00%H%x00%cs"])
    full = g.git_log_numstat(repo)["commits"]
    for limit in range(1, len(raw)):
        monkeypatch.setattr(g, "_git_run", lambda *a, **k: (raw[:limit], True, 0))
        result = g.git_log_numstat(repo)
        assert result["truncated"]
        assert result["commits"] == full[:len(result["commits"])]


@pytest.mark.parametrize("since,exception", [(1, TypeError), ("", ValueError), ("x\0y", ValueError)])
def test_log_numstat_rejects_invalid_since(repo, since, exception):
    with pytest.raises(exception):
        g.git_log_numstat(repo, since=since)


def test_log_numstat_fixed_argv_suppresses_helpers_and_protects_since(monkeypatch, repo):
    seen = []
    monkeypatch.setattr(g, "_git_run", lambda root, args, **kwargs: (seen.append(args) or b"", False, 0))
    g.git_log_numstat(repo, since="--all")
    assert seen[0][-2:] == ["--since=--all", "--"]
    assert "--no-ext-diff" in seen[0] and "--no-textconv" in seen[0]


def test_check_ignore_is_nul_safe_and_distinguishes_not_ignored(repo):
    (repo / ".gitignore").write_text("build/\n*.secret\n", encoding="utf-8")
    result = g.git_check_ignore(repo, ["build/out", "space 日本語.txt", "x.secret"])
    assert [row["path"] for row in result["records"]] == [
        "build/out", "space 日本語.txt", "x.secret"]
    assert [row["status"] for row in result["records"]] == [
        "ignored", "not_ignored", "ignored"]
    assert result["records"][0]["pattern"] == "build/"
    assert not result["truncated"]


def test_check_ignore_bounds(repo):
    with pytest.raises(ValueError):
        g.git_check_ignore(repo, ["a", "b"], max_paths=1)
    with pytest.raises(ValueError):
        g.git_check_ignore(repo, ["long-name"], max_bytes=2)


def test_structured_nul_outputs_drop_partial_tail(monkeypatch, repo):
    original_run = g._git_run

    def truncated_status(root, args, **kwargs):
        if "status" in args:
            # One complete record followed by a type-2 record whose original
            # path is cut before its terminating NUL.
            raw = (
                b"? complete.txt\0"
                b"2 R. N... 100644 100644 100644 "
                + b"0" * 40 + b" " + b"1" * 40
                + b" R100 renamed.txt\0old-na"
            )
            return raw, True, 0
        return original_run(root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", truncated_status)
    result = g.git_status(repo)
    assert result["truncated"] is True
    assert result["records"] == [
        {"kind": "?", "record": "? complete.txt", "path": "complete.txt"}]


def test_nul_inventory_does_not_publish_partial_path(monkeypatch, repo):
    def truncated_ls(root, args, **kwargs):
        return b"a.txt\0partial-na", True, 0

    monkeypatch.setattr(g, "_git_run", truncated_ls)
    result = g.git_ls_files(repo)
    assert result == {"paths": ["a.txt"], "truncated": True}


def test_log_and_grep_do_not_publish_partial_records(repo):
    log_result = g.git_log(repo, max_count=1)
    assert log_result["commits"]
    assert g.git_log(repo, max_count=1, max_bytes=5) == {
        "commits": [], "truncated": True}
    assert g.git_grep(repo, "hello", max_bytes=3) == {
        "paths": [], "truncated": True}


def test_status_conflict_is_one_logical_record(repo):
    # Build a real content conflict without using inspector mutation paths.
    base = git(repo, "branch", "--show-current").strip()
    git(repo, "checkout", "-qb", "other")
    (repo / "a.txt").write_text("other\n", encoding="utf-8")
    git(repo, "commit", "-am", "other")
    git(repo, "checkout", "-q", base)
    (repo / "a.txt").write_text("main\n", encoding="utf-8")
    git(repo, "commit", "-am", "main")
    proc = subprocess.run(["git", "-C", str(repo), "merge", "other"],
                          capture_output=True, text=True)
    assert proc.returncode != 0
    result = g.git_status(repo)
    conflicts = [row for row in result["records"] if row["kind"] == "u"]
    assert len(conflicts) == 1
    assert conflicts[0]["path"] == "a.txt"


def test_truncated_dirty_status_is_never_reported_clean(repo):
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    result = g.git_status(repo, max_bytes=1)
    assert result["truncated"] is True
    assert result["clean"] is False


def test_check_ignore_handles_negation_and_truncated_unseen_paths(repo):
    (repo / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
    negated = g.git_check_ignore(repo, ["drop.log", "keep.log"])
    assert [row["status"] for row in negated["records"]] == [
        "ignored", "not_ignored"
    ]
    keep = negated["records"][1]
    assert keep["pattern"] == "!keep.log"

    truncated = g.git_check_ignore(
        repo, ["drop.log", "keep.log", "unseen.txt"], max_bytes=30
    )
    assert truncated["truncated"] is True
    assert all(
        row["status"] != "not_ignored"
        for row in truncated["records"]
        if row["path"] == "unseen.txt"
    )
    assert next(
        row for row in truncated["records"] if row["path"] == "unseen.txt"
    )["status"] == "not_measured"


def test_all_commands_disable_optional_locks_fsmonitor_and_routing_env(monkeypatch, repo):
    seen = []
    real = g._git_spawn

    def recording(argv, **kwargs):
        seen.append((tuple(argv), dict(kwargs["env"])))
        return real(argv, **kwargs)

    monkeypatch.setenv("GIT_DIR", "/sentinel/not-this-repo")
    monkeypatch.setenv("GIT_INDEX_FILE", "/sentinel/index")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "/sentinel/diff")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'alias.status=!false'")
    monkeypatch.setenv("GIT_COMMON_DIR", "/sentinel/common")
    monkeypatch.setenv("GIT_SHALLOW_FILE", "/sentinel/shallow")
    monkeypatch.setattr(g, "_git_spawn", recording)
    g.git_status(repo)
    g.git_ls_files(repo)
    g.git_diff(repo)
    g.git_log(repo, max_count=1)
    g.git_show(repo)
    g.git_blame(repo, "a.txt")
    g.git_grep(repo, "one")
    for argv, env in seen:
        assert "--no-optional-locks" in argv
        assert "core.fsmonitor=false" in argv
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert "GIT_DIR" not in env
        assert "GIT_INDEX_FILE" not in env
        assert "GIT_EXTERNAL_DIFF" not in env
        assert "GIT_CONFIG_PARAMETERS" not in env
        assert "GIT_COMMON_DIR" not in env
        assert "GIT_SHALLOW_FILE" not in env
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_SYSTEM"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_malformed_complete_type2_is_error(monkeypatch, repo):
    def malformed(root, args, **kwargs):
        return b"2 malformed\\0old.txt\\0", False, 0

    monkeypatch.setattr(g, "_git_run", malformed)
    with pytest.raises(g.GitInspectionError):
        g.git_status(repo)


def test_text_observations_are_json_safe_when_utf8_is_cut(monkeypatch, repo):
    def cut(root, args, **kwargs):
        return b"prefix-\xe3\x81", True, 0

    monkeypatch.setattr(g, "_git_run", cut)
    result = g.git_diff(repo, max_bytes=8)
    assert result["truncated"] is True
    assert "\ufffd" in result["patch"]
    result["patch"].encode("utf-8")


def test_check_ignore_preserves_duplicate_input_positions(repo):
    (repo / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    result = g.git_check_ignore(repo, ["a.tmp", "a.tmp", "plain.txt"])
    assert [row["path"] for row in result["records"]] == [
        "a.tmp", "a.tmp", "plain.txt"]
    assert [row["status"] for row in result["records"]] == [
        "ignored", "ignored", "not_ignored"]


def test_run_drains_large_stdout_and_stderr_with_bounded_retention(monkeypatch, repo):
    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"x" * 1_000_000)
            self.stderr = io.BytesIO(b"e" * 1_000_000)
            self.stdin = None
            self.waited = False

        def wait(self):
            self.waited = True
            return 0

    process = FakeProcess()
    monkeypatch.setattr(g, "_git_spawn", lambda *args, **kwargs: process)
    raw, truncated, code = g._git_run(repo, ["status"], max_bytes=32)
    assert raw == b"x" * 32
    assert truncated is True
    assert code == 0
    assert process.waited is True


def test_run_drains_stderr_before_reporting_failure(monkeypatch, repo):
    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"failure" * 100_000)
            self.stdin = None
            self.waited = False

        def wait(self):
            self.waited = True
            return 7

    process = FakeProcess()
    monkeypatch.setattr(g, "_git_spawn", lambda *args, **kwargs: process)
    with pytest.raises(g.GitInspectionError, match="status 7"):
        g._git_run(repo, ["status"], max_bytes=16)
    assert process.waited is True


def test_git_cli_observation_and_failure(repo, capsys):
    import json
    assert g.main(["git-status", "--root", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["clean"] is True
    assert g.main(["git-diff", "--root", str(repo), "--output-bytes", "0"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_argument"
    assert g.main(["git-status", "--root", str(repo / "missing")]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_argument"
    assert g.main(["git-status", "--root", str(repo.parent)]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "git_inspection_failed"


def test_cli_status_defaults_to_short(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert g.main(['git-status']) == 0
    assert capsys.readouterr().out == ''
    (repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    git(repo, 'mv', 'space 日本語.txt', 'new name.txt')
    (repo / 'untracked.txt').write_text('new', encoding='utf-8')
    assert g.main(['git-status']) == 0
    text = capsys.readouterr().out
    assert ' M a.txt\n' in text
    assert 'R  "space 日本語.txt" -> "new name.txt"\n' in text
    assert '?? untracked.txt\n' in text
    assert g.main(['git-status', '--json']) == 0
    import json
    assert json.loads(capsys.readouterr().out)['clean'] is False


def test_short_status_quotes_control_characters_and_marks_truncation():
    result = {'records':[{'kind':'?', 'path':'a\nb', 'record':'? a\nb'}], 'truncated':True}
    assert g.render_git_status(result) == '?? "a\\nb"\n(truncated)'


# ──── git_switch ──────────────────────────────────────────────────

# git switch: planned by default, performed only with write, verified after.


@pytest.fixture
def repo_git_switch(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    _git(tmp_path, "branch", "feature")
    return tmp_path


def test_plan_changes_nothing_git_switch(repo_git_switch):
    out = g.git_switch(repo_git_switch, "feature")
    assert (out["status"], out["command"]) == ("planned", ["git", "switch", "--no-guess", "feature"])
    assert _git(repo_git_switch, "branch", "--show-current") == "main"


def test_switch_existing_then_back_to_previous(repo_git_switch):
    out = g.git_switch(repo_git_switch, "feature", write=True)
    assert out["status"] == "verified" and out["after"]["branch"] == "feature"
    back = g.git_switch(repo_git_switch, previous=True, write=True)
    assert (back["target"], back["status"], back["after"]["branch"]) == ("main", "verified", "main")


def test_create_switches_to_a_new_branch_at_the_same_commit(repo_git_switch):
    out = g.git_switch(repo_git_switch, "topic/new", create=True, write=True)
    assert out["status"] == "verified"
    assert out["after"] == {"branch": "topic/new", "head_sha": out["before"]["head_sha"]}


@pytest.mark.parametrize(("branch", "create", "error"), [
    ("missing", False, g.GitInspectionError),   # never guessed from a remote
    ("feature", True, g.GitInspectionError),    # already exists
    ("bad..name", True, ValueError),
    ("-x", False, ValueError),                  # option injection
])
def test_invalid_targets_fail_before_any_change(repo_git_switch, branch, create, error):
    with pytest.raises(error):
        g.git_switch(repo_git_switch, branch, create=create, write=True)
    assert _git(repo_git_switch, "branch", "--show-current") == "main"


def test_no_previous_branch_is_an_error(repo_git_switch):
    with pytest.raises(g.GitInspectionError):
        g.git_switch(repo_git_switch, previous=True)


def test_conflicting_local_change_is_reported_not_forced(repo_git_switch):
    (repo_git_switch / "f.txt").write_text("main\n")
    _git(repo_git_switch, "add", "f.txt")
    _git(repo_git_switch, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "f on main")
    _git(repo_git_switch, "switch", "-q", "feature")
    (repo_git_switch / "f.txt").write_text("untracked would be overwritten\n")
    out = g.git_switch(repo_git_switch, "main", write=True)
    assert (out["status"], out["mutation_status"]) == ("mutation_failed", "failed")
    assert _git(repo_git_switch, "branch", "--show-current") == "feature"


def test_cli_plans_by_default_and_writes_only_with_flag(repo_git_switch, capsys):
    assert g.main(["git-switch", "--root", str(repo_git_switch), "feature"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert _git(repo_git_switch, "branch", "--show-current") == "main"
    assert g.main(["git-switch", "--root", str(repo_git_switch), "-c", "made", "--write"]) == 0
    assert _git(repo_git_switch, "branch", "--show-current") == "made"
    assert g.main(["git-switch", "--root", str(repo_git_switch), "-", "--write"]) == 0
    assert _git(repo_git_switch, "branch", "--show-current") == "main"
    assert g.main(["git-switch", "--root", str(repo_git_switch), "feature", "-c", "x"]) == 2


def test_previous_detached_position_is_refused(repo_git_switch):
    _git(repo_git_switch, "switch", "-q", "--detach")
    _git(repo_git_switch, "switch", "-q", "main")
    with pytest.raises(g.GitInspectionError, match="not a branch"):
        g.git_switch(repo_git_switch, previous=True, write=True)
    assert _git(repo_git_switch, "branch", "--show-current") == "main"


# ──── git_trials ──────────────────────────────────────

# merge-trial and git-activity: local and read-only.


@pytest.fixture
def repo_git_trials(tmp_path):
    root = tmp_path / "r"
    _git_tester(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (root / "img.bin").write_bytes(b"\0\1")
    _git_tester(root, "add", ".")
    _git_tester(root, "commit", "-q", "-m", "base")
    for branch, path, data in (("clean", "b.txt", b"new\n"), ("clash", "a.txt", b"ONE\ntwo\n"), ("bin", "img.bin", b"\0\2")):
        _git_tester(root, "switch", "-q", "-c", branch, "main")
        (root / path).write_bytes(data)
        _git_tester(root, "add", ".")
        _git_tester(root, "commit", "-q", "-m", branch)
    _git_tester(root, "switch", "-q", "main")
    (root / "a.txt").write_text("uno\ntwo\n", encoding="utf-8")
    _git_tester(root, "commit", "-q", "-am", "main edit")
    return root


def test_merge_trial_is_read_only(repo_git_trials):
    before = (_git_tester(repo_git_trials, "rev-parse", "HEAD"), _git_tester(repo_git_trials, "status", "--porcelain"))
    clean = g.merge_trial(repo_git_trials, "clean")
    assert clean["status"] == "clean" and clean["ok"] and clean["conflicts"] == []
    assert clean["changed"] == [{"path": "b.txt", "added": 1, "removed": 0}]
    clash = g.merge_trial(repo_git_trials, "clash")
    assert clash["status"] == "conflict" and not clash["ok"] and clash["conflicts"] == ["a.txt"]
    binary = g.merge_trial(repo_git_trials, "bin")
    assert binary["changed"] == [{"path": "img.bin", "added": None, "removed": None}]
    assert g.merge_trial(repo_git_trials, "main~1")["status"] == "already_merged"
    missing = g.merge_trial(repo_git_trials, "e7ef1e62299a327681320c0bcdb03f2c32320d97")
    assert missing["status"] == "missing_object" and missing["missing"] == ["ref"]
    assert (_git_tester(repo_git_trials, "rev-parse", "HEAD"), _git_tester(repo_git_trials, "status", "--porcelain")) == before
    text = g.render_merge_trial(clash)
    assert text.startswith("conflict clash@") and "conflict a.txt" in text
    assert "fetch it first" in g.render_merge_trial(missing)
    assert "img.bin +bin/-bin" in g.render_merge_trial(binary)
    for bad in ("", "-x", "a\nb"):
        with pytest.raises(ValueError):
            g.merge_trial(repo_git_trials, bad)


def test_merge_trial_lists_at_most_40_rows(repo_git_trials):
    _git_tester(repo_git_trials, "switch", "-q", "-c", "many", "main")
    for i in range(45):
        (repo_git_trials / f"f{i}.txt").write_text("x\n", encoding="utf-8")
    _git_tester(repo_git_trials, "add", ".")
    _git_tester(repo_git_trials, "commit", "-q", "-m", "many")
    _git_tester(repo_git_trials, "switch", "-q", "main")
    assert "... 5 more" in g.render_merge_trial(g.merge_trial(repo_git_trials, "many"))


def test_git_activity_objects_locks_and_processes(repo_git_trials, tmp_path):
    head = _git_tester(repo_git_trials, "rev-parse", "HEAD")
    out = g.git_activity(repo_git_trials, [head, head[:7], "e7ef1e62299a327681320c0bcdb03f2c32320d97"])
    assert out["head"] == head and [o["type"] for o in out["objects"]] == ["commit", "commit", "missing"]
    assert out["busy"] is False
    (repo_git_trials / ".git" / "index.lock").write_text("", encoding="utf-8")
    locked = g.git_activity(repo_git_trials)
    assert locked["busy"] and locked["locks"][0]["file"] == "index.lock"
    assert "lock index.lock" in g.render_git_activity(locked)
    os.remove(repo_git_trials / ".git" / "index.lock")
    none = g.git_activity(repo_git_trials, proc_root=str(tmp_path / "no-proc"))
    assert none["processes"] is None and "not observable" in g.render_git_activity(none)
    for bad in (["zz"], "abc"):
        with pytest.raises((ValueError, TypeError)):
            g.git_activity(repo_git_trials, bad)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc process scan is Linux-only")
def test_git_activity_sees_a_running_git_process(repo_git_trials):
    child = subprocess.Popen(["git", "hash-object", "--stdin"], cwd=repo_git_trials / ".git", stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL)
    try:
        out = g.git_activity(repo_git_trials)
        rows = [p for p in out["processes"] if p["pid"] == child.pid]
        assert out["busy"] and rows and rows[0]["argv"][1:] == ["hash-object", "--stdin"]
        assert rows[0]["elapsed_seconds"] >= 0
        assert f"pid {child.pid}" in g.render_git_activity(out)
    finally:
        child.communicate(b"x")


def test_cli(repo_git_trials, capsys):
    assert g.main(["merge-trial", str(repo_git_trials), "clash", "--text"]) == 1
    assert capsys.readouterr().out.startswith("conflict clash@")
    assert g.main(["merge-trial", str(repo_git_trials), "clean"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"
    assert g.main(["merge-trial", str(repo_git_trials), "0" * 40]) == 3
    capsys.readouterr()
    assert g.main(["git-activity", str(repo_git_trials), "--text"]) in (0, 3)
    assert capsys.readouterr().out.split()[0] in ("idle", "busy")


# ──── local_position ──────────────────────────────────────────────

# local-position: where am I (cwd, Git top level, HEAD, changes, remote).


@pytest.fixture
def repo_local_position(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return tmp_path


@pytest.mark.parametrize("url,full_name", [
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo", "owner/repo"),
    ("https://user:ghp_secret@github.com/owner/repo.git", "owner/repo"),
    ("git@github.com:owner/repo.git", "owner/repo"),
    ("ssh://git@github.com/owner/repo.git", "owner/repo"),
    ("http://proxy@127.0.0.1:8080/git/owner/repo", None),
    ("https://gitlab.com/owner/repo.git", None),
    ("https://github.com/owner/repo/extra", None),
])
def test_remote_repository_never_echoes_the_url(repo_local_position, url, full_name):
    _git(repo_local_position, "remote", "add", "origin", url)
    out = g.remote_repository(repo_local_position)
    assert out["full_name"] == full_name
    assert out["reason"] == (None if full_name else "not_github")
    assert "secret" not in json.dumps(out) and "127.0.0.1" not in json.dumps(out)


def test_missing_remote(repo_local_position):
    assert g.remote_repository(repo_local_position) == {"remote": "origin", "full_name": None, "reason": "no_remote"}
    with pytest.raises(ValueError):
        g.remote_repository(repo_local_position, remote="--upload-pack=x")


def test_position_counts_changes_and_relative_dir(repo_local_position):
    _git(repo_local_position, "remote", "add", "origin", "https://github.com/owner/repo.git")
    (repo_local_position / "a.txt").write_text("changed\n")
    (repo_local_position / "b.txt").write_text("new\n")
    (repo_local_position / "c.txt").write_text("staged\n")
    _git(repo_local_position, "add", "c.txt")
    (repo_local_position / "sub").mkdir()
    out = g.local_position(repo_local_position / "sub")
    git = out["git"]
    assert out["cwd"].endswith("/sub") and git["relative"] == "sub"
    assert git["changes"] == {"staged": 1, "unstaged": 1, "untracked": 1}
    assert git["head"]["sha"] == _git(repo_local_position, "rev-parse", "HEAD") and git["head"]["branch"] == "main"
    assert git["remote"]["full_name"] == "owner/repo"


def test_outside_a_checkout(tmp_path):
    out = g.local_position(tmp_path)
    assert out["git"] is None and out["reason"] == "not_a_checkout"


def test_cli_local_position(repo_local_position, capsys):
    assert g.main(["local-position", "--root", str(repo_local_position)]) == 0
    assert json.loads(capsys.readouterr().out)["git"]["head"]["branch"] == "main"


def test_unborn_head_is_no_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    assert g.local_position(tmp_path)["reason"] == "no_commit"


def test_missing_git_is_an_error_not_an_absence(repo_local_position, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(g, "_git_spawn", no_git)
    out = g.local_position(repo_local_position)
    assert (out["git"], out["reason"]) == (None, "git_error") and "not found" in out["detail"]


def test_permission_failure_is_an_error_not_an_absence(repo_local_position, monkeypatch):
    real = g._git_run
    def denied(root, args, **kwargs):
        if args[:2] == ["rev-parse", "--verify"]:
            raise g.GitInspectionError("PermissionError")
        return real(root, args, **kwargs)
    monkeypatch.setattr(g, "_git_run", denied)
    assert g.local_position(repo_local_position)["reason"] == "git_error"


def test_broken_dot_git_is_an_error_not_an_absence(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /nonexistent\n")
    out = g.local_position(tmp_path)
    assert out["reason"] == "git_error" and out["git"] is None


# ──── default_overview ────────────────────────────────────────────

def test_overview_without_checkout_or_tools(monkeypatch):
    monkeypatch.setattr(g, "tool_versions", lambda *a: {})
    monkeypatch.setattr(g, "local_position", lambda: {"git": None, "reason": "not_a_checkout"})
    result = g.overview()
    assert result["environment"]["git_version"] is None
    assert result["remote"] is None
    assert result["pull_requests"] is None
    assert result["issues"] is None
    assert result["ci"] is None
    assert result["next_actions"]


def test_overview_remote_and_partial_failures(monkeypatch):
    monkeypatch.setattr(g, "tool_versions", lambda *a: {"git": "2.45", "gh": "2.60"})
    monkeypatch.setattr(g, "local_position", lambda: {"git": {"remote": {"full_name": "owner/repo"}}})
    monkeypatch.setattr(g, "pull_requests", lambda *a, **k: {"pull_requests": []})
    monkeypatch.setattr(g, "issues", lambda *a, **k: (_ for _ in ()).throw(g.Error("permission_or_rate_limit")))
    monkeypatch.setattr(g, "run_history", lambda *a, **k: {"runs": []})
    result = g.overview()
    assert result["remote"]["repository"] == "owner/repo"
    assert result["pull_requests"] == {"pull_requests": []}
    assert result["issues"] is None
    assert result["ci"] == {"runs": []}
    assert result["environment"]["git_version"] == "2.45"
    assert result["environment"]["gh_version"] == "2.60"


@pytest.mark.parametrize("argv", [[], ["overview"]])
def test_default_output_excludes_local_placement(monkeypatch, capsys, argv):
    secret = "/home/private-user/private-checkout"
    position = {"cwd": secret, "detail": secret, "future_field": secret,
                "git": {"top_level": secret, "relative": "private-subdir",
                        "head": {"sha": "a" * 40, "branch": "main", "detached": False,
                                 "timestamp": "2026-10-13T00:00:00Z", "subject": secret},
                        "changes": {"staged": 1, "unstaged": 2, "untracked": 3, "path": secret},
                        "changes_truncated": True,
                        "remote": {"remote": "origin", "full_name": "owner/repo",
                                   "reason": None, "url": secret}}}
    monkeypatch.setattr(g, "local_position", lambda: position)
    monkeypatch.setattr(g, "tool_versions", lambda *a: {"git": "2.45", "gh": "2.60"})
    for name in ("pull_requests", "issues", "run_history"):
        monkeypatch.setattr(g, name, lambda *a, **k: {})
    assert g.main(argv) == 0
    output = capsys.readouterr().out
    assert secret not in output and "private-subdir" not in output
    result = json.loads(output)
    git = result["local"]["git"]
    assert set(result["local"]) == {"git"}
    assert set(git) == {"head", "changes", "changes_truncated", "remote"}
    assert git["changes"] == {"staged": 1, "unstaged": 2, "untracked": 3}
    assert git["changes_truncated"] is True
    assert result["remote"] == {"repository": "owner/repo"}
    assert position["cwd"] == secret and position["git"]["top_level"] == secret


def test_local_error_detail_is_not_published(monkeypatch):
    monkeypatch.setattr(g, "tool_versions", lambda *a: {})
    monkeypatch.setattr(g, "local_position", lambda: {
        "cwd": "C:\\Users\\private", "git": None, "reason": "git_error",
        "detail": "permission denied: C:\\Users\\private"})
    assert g.overview()["local"] == {"git": None, "reason": "git_error"}


@pytest.mark.parametrize("failed", ["pull_requests", "issues", "run_history"])
@pytest.mark.parametrize("error", [g.Error("permission_or_rate_limit"),
                                   OSError("private path"), ValueError("invalid response")])
def test_each_remote_failure_preserves_other_sections(monkeypatch, failed, error):
    monkeypatch.setattr(g, "tool_versions", lambda *a: {"git": "2.45", "gh": "2.60"})
    monkeypatch.setattr(g, "local_position", lambda: {"git": {
        "changes": {"staged": 1, "unstaged": 0, "untracked": 1},
        "remote": {"full_name": "owner/repo"}}})
    calls = []
    def probe(name):
        def run(*args, **kwargs):
            calls.append(name)
            assert args == ("owner/repo",)
            assert kwargs == {"max_items": 5, "max_pages": 1}
            if name == failed:
                raise error
            return {"observed": name}
        return run
    names = {"pull_requests": "pull_requests", "issues": "issues", "ci": "run_history"}
    for name in names.values():
        monkeypatch.setattr(g, name, probe(name))
    result = g.overview()
    assert calls == list(names.values())
    for key, name in names.items():
        assert result[key] == (None if name == failed else {"observed": name})
    assert result["environment"]["gh_version"] == "2.60"
    assert result["local"]["git"]["changes"]["staged"] == 1


def test_real_checkout_changes_and_explicit_position_contract(tmp_path, monkeypatch, capsys):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (tmp_path / "tracked").write_text("initial")
    git("add", "tracked")
    git("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")
    (tmp_path / "tracked").write_text("modified")
    (tmp_path / "staged").write_text("staged")
    git("add", "staged")
    (tmp_path / "untracked").write_text("untracked")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(g, "tool_versions", lambda *a: {})
    assert g.main([]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert str(tmp_path) not in output
    assert result["local"]["git"]["changes"] == {"staged": 1, "unstaged": 1, "untracked": 1}
    assert result["local"]["git"]["head"]["sha"] == git("rev-parse", "HEAD")
    assert g.main(["local-position"]) == 0
    explicit = json.loads(capsys.readouterr().out)
    assert explicit["cwd"] == str(tmp_path)
    assert explicit["git"]["top_level"] == str(tmp_path)
    assert explicit["git"]["relative"] == "."


def test_no_arguments_dispatches_overview(monkeypatch, capsys):
    monkeypatch.setattr(g, "overview", lambda: {"schema": "gh-identity-overview/1"})
    assert g.main([]) == 0
    assert "gh-identity-overview/1" in capsys.readouterr().out
