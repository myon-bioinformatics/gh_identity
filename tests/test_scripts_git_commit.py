from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "git_commit.py"
spec = importlib.util.spec_from_file_location("git_commit", SCRIPT)
git_commit = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(git_commit)


class FakeRun:
    """Records argv/kwargs and replies in order: (returncode, stdout, stderr)."""

    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        code, stdout, stderr = self.replies.pop(0)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


def git_env(**overrides: str) -> dict:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
              GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
              GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
    env.update(overrides)
    return env


def init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=git_env())
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True, env=git_env())
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "initial"], check=True, env=git_env())


def run_cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-S", str(SCRIPT), *args], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(root), env=git_env())


def git_output(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
                          env=git_env()).stdout


def sha_line(stdout: str) -> str:
    """The full 40-char commit SHA line printed by main(), ignoring any --push output around it."""
    matches = [line for line in stdout.splitlines() if SHA_RE.match(line)]
    assert len(matches) == 1, f"expected exactly one SHA line, got: {stdout!r}"
    return matches[0]


# --- build_message / read_message -----------------------------------------

def test_read_message_strips_trailing_newlines(tmp_path: Path) -> None:
    path = tmp_path / "msg.txt"
    path.write_text("Subject\n\nBody\n\n\n", encoding="utf-8")
    assert git_commit.read_message(path) == "Subject\n\nBody"


def test_build_message_appends_new_trailers_with_blank_line() -> None:
    assert git_commit.build_message("Subject\n\nBody.\n", ["Key: value"]) == "Subject\n\nBody.\n\nKey: value\n"


def test_build_message_skips_trailer_already_present_in_base() -> None:
    base = "Subject\n\nKey: value\n"
    assert git_commit.build_message(base, ["Key: value"]) == base


def test_build_message_dedups_repeated_trailer_within_same_call() -> None:
    out = git_commit.build_message("Subject\n", ["Key: value", "Key: value"])
    assert out.count("Key: value") == 1


def test_build_message_with_no_trailers_returns_base_unchanged() -> None:
    assert git_commit.build_message("Subject\n", []) == "Subject\n"


# --- staged_files -----------------------------------------------------------

def test_staged_files_parses_name_only_output() -> None:
    run = FakeRun([(0, "a.py\nb.py\n", "")])
    assert git_commit.staged_files(run=run) == ["a.py", "b.py"]


# --- push_with_retry: network-looking errors only, exact backoff, never forces --

def test_push_with_retry_retries_only_network_errors_then_succeeds() -> None:
    run = FakeRun([
        (1, "", "fatal: unable to access 'https://x': Could not resolve host: x"),
        (1, "", "fatal: unable to access 'https://x': Connection timed out"),
        (0, "done\n", ""),
    ])
    sleeps: list = []
    ok, output = git_commit.push_with_retry(run=run, sleep=sleeps.append)
    assert ok is True
    assert "done" in output
    assert sleeps == [2.0, 4.0]
    assert len(run.calls) == 3
    for argv, _ in run.calls:
        assert argv == ["git", "push", "origin", "HEAD"]
        assert "--force" not in argv and "-f" not in argv


def test_push_with_retry_gives_up_after_all_delays() -> None:
    run = FakeRun([(1, "", "Connection timed out") for _ in range(5)])
    sleeps: list = []
    ok, output = git_commit.push_with_retry(run=run, sleep=sleeps.append)
    assert ok is False
    assert "Connection timed out" in output
    assert sleeps == [2.0, 4.0, 8.0, 16.0]
    assert len(run.calls) == 5


def test_push_with_retry_does_not_retry_non_network_errors() -> None:
    run = FakeRun([(1, "", "! [rejected] main -> main (non-fast-forward)")])
    sleeps: list = []
    ok, output = git_commit.push_with_retry(run=run, sleep=sleeps.append)
    assert ok is False
    assert sleeps == []
    assert len(run.calls) == 1


# --- main(): end-to-end against a real temp git repo -----------------------

def test_cli_stages_only_named_files_prints_status_diff_and_sha(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("new\n", encoding="utf-8")
    (tmp_path / "message.txt").write_text("Update files\n", encoding="utf-8")
    done = run_cli(tmp_path, "--message-file", "message.txt", "a.txt", "b.txt",
                   "--trailer", "Co-Authored-By: Claude <noreply@anthropic.com>")
    assert done.returncode == 0, done.stderr
    assert "M  a.txt" in done.stdout
    assert "A  b.txt" in done.stdout
    assert "2 files changed" in done.stdout
    assert sha_line(done.stdout) == git_output(tmp_path, "rev-parse", "HEAD").strip()
    message = git_output(tmp_path, "log", "-1", "--format=%B")
    assert "Update files" in message
    assert "Co-Authored-By: Claude <noreply@anthropic.com>" in message


def test_cli_refuses_when_other_files_already_staged(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("extra\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "extra.txt"], check=True, env=git_env())
    (tmp_path / "message.txt").write_text("msg\n", encoding="utf-8")
    done = run_cli(tmp_path, "--message-file", "message.txt", "a.txt")
    assert done.returncode == 2
    assert "extra.txt" in done.stderr
    assert git_output(tmp_path, "diff", "--cached", "--name-only").strip() == "extra.txt"


def test_cli_allow_staged_bypasses_the_check(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("extra\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "extra.txt"], check=True, env=git_env())
    (tmp_path / "message.txt").write_text("msg\n", encoding="utf-8")
    done = run_cli(tmp_path, "--message-file", "message.txt", "a.txt", "--allow-staged")
    assert done.returncode == 0, done.stderr
    committed = git_output(tmp_path, "diff", "--name-only", "HEAD~1", "HEAD")
    assert set(committed.split()) == {"a.txt", "extra.txt"}


def test_cli_refuses_empty_message_file(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "message.txt").write_text("   \n", encoding="utf-8")
    done = run_cli(tmp_path, "--message-file", "message.txt", "a.txt")
    assert done.returncode == 2
    assert "empty" in done.stderr


def test_cli_refuses_missing_message_file(tmp_path: Path) -> None:
    init_repo(tmp_path)
    done = run_cli(tmp_path, "--message-file", "nope.txt", "a.txt")
    assert done.returncode == 2
    assert "not found" in done.stderr


def test_cli_exit_1_when_git_add_fails(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "message.txt").write_text("msg\n", encoding="utf-8")
    done = run_cli(tmp_path, "--message-file", "message.txt", "does-not-exist.txt")
    assert done.returncode == 1
    assert done.stderr.strip() != ""


def test_cli_push_against_a_real_remote(tmp_path: Path) -> None:
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work.mkdir()
    init_repo(work)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True, env=git_env())
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "HEAD"], check=True, env=git_env())
    branch = git_output(work, "branch", "--show-current").strip()
    (work / "a.txt").write_text("changed\n", encoding="utf-8")
    (work / "message.txt").write_text("second commit\n", encoding="utf-8")
    done = run_cli(work, "--message-file", "message.txt", "a.txt", "--push")
    assert done.returncode == 0, done.stderr
    remote_sha = subprocess.run(["git", "-C", str(bare), "rev-parse", branch], capture_output=True, text=True,
                                check=True).stdout.strip()
    assert sha_line(done.stdout) == remote_sha


# --- --help ------------------------------------------------------------------

def test_cli_help_exits_zero_with_usage_on_stdout() -> None:
    done = subprocess.run([sys.executable, "-S", str(SCRIPT), "--help"], capture_output=True, text=True,
                          encoding="utf-8")
    assert done.returncode == 0
    assert done.stdout.lower().startswith("usage")


def test_module_help_via_dash_m_exits_zero_with_usage_on_stdout() -> None:
    done = subprocess.run([sys.executable, "-S", "-m", "git_commit", "--help"], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(SCRIPT.parent))
    assert done.returncode == 0, done.stderr
    assert done.stdout.lower().startswith("usage")


def test_cli_accepts_dot_slash_and_absolute_paths_for_already_staged_files(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.txt"], check=True, env=git_env())
    (tmp_path / "message.txt").write_text("msg\n", encoding="utf-8")
    assert run_cli(tmp_path, "--message-file", "message.txt", "./a.txt").returncode == 0
    (tmp_path / "a.txt").write_text("again\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.txt"], check=True, env=git_env())
    assert run_cli(tmp_path, "--message-file", "message.txt", str(tmp_path / "a.txt")).returncode == 0
