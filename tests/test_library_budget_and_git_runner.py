"""Library calls outside operation() and the pluggable git runner for first_parent_check."""
import json
import os
import subprocess

import pytest
import gh_identity as g

M3 = "3" * 40
REPOS = [{"full_name": "me/a", "id": 1, "visibility": "public", "private": False, "archived": False, "fork": False}]


def stub(method, path, **kw):
    if path.startswith(("users/me/repos", "orgs/me/repos")):
        return REPOS if "page=1" in path else []
    if path.startswith("repos/me/a/commits/" + M3 + "/pulls"):
        return [{"number": 4, "merged_at": "t", "base": {"ref": "main"}, "head": {"sha": M3}}]
    if path == "repos/me/a":
        return {"id": 1, "visibility": "public", "private": False, "archived": False, "default_branch": "main"}
    if path == "repos/me/a/pulls/4":
        return {"state": "closed", "merged_at": "t", "merge_commit_sha": M3, "head": {}, "base": {"ref": "main"}}
    if path.startswith("repos/me/a/commits/main"):
        return {"sha": M3}
    raise g.Error("not_found_or_inaccessible")


def test_paginated_library_calls_work_without_operation():
    assert g._BUDGET.get() is None
    with g.request_stub(stub):
        assert g.pages("users/me/repos") == REPOS
        items, complete, n = g.pages_bounded("users/me/repos", max_pages=3)
        assert items == REPOS and complete and n == 1
        assert g.list_owner_repositories("me", kind="user")["count"] == 1
        assert g.list_owner_repositories("me")["count"] == 1
        assert g.commit_pulls("me/a", M3)["pulls"][0]["number"] == 4
        assert g.pr_identity("me/a", 4)["merge_commit_sha"] == M3
        assert g.repo_identity("me/a")["head"] == M3
    assert g._BUDGET.get() is None


def test_library_calls_reuse_an_explicit_operation():
    with g.operation(max_pages=1):
        with g.request_stub(stub):
            g.pages_bounded("users/me/repos")
            with pytest.raises(g.Error) as e:
                g.pages_bounded("users/me/repos")
    assert e.value.code == "pages_limit"


class FakeGit:
    def __init__(self, listed, ls_code=0):
        self.calls, self.listed, self.ls_code = [], listed, ls_code

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        verb = next(a for a in args if not a.startswith("-") and "=" not in a)
        if verb == "ls-remote":
            return self.ls_code, b"ref: refs/heads/trunk\tHEAD\n" + M3.encode() + b"\tHEAD\n"
        if verb == "rev-list":
            return 0, ("\n".join(self.listed) + "\n").encode()
        return 0, b""


def test_git_runner_is_injectable_offline_and_credential_free(monkeypatch):
    token = "ghp_" + "x" * 36
    monkeypatch.setenv("GH_TOKEN", token)
    monkeypatch.setattr(g, "_git_run", lambda *a, **k: pytest.fail("default runner used"))
    fake = FakeGit([M3])
    out = g.first_parent_check("me/a", M3, exact=True, git_runner=fake,
                               remote="https://github.com/me/a.git")
    assert out["contains"] is True and out["branch"] == "trunk"
    dumped = json.dumps([c for c, _ in fake.calls])
    assert token not in dumped and "x-access-token" not in dumped.lower() and "authorization" not in dumped.lower()
    assert "https://github.com/me/a.git" in dumped
    for args, cwd in fake.calls:
        assert args[:6] == ["-c", "credential.helper=", "-c", "core.askPass=", "-c", "http.extraHeader="]
        assert cwd == fake.calls[0][1]
    assert not os.path.exists(fake.calls[0][1])
    assert g.first_parent_check("me/a", M3, exact=True, git_runner=FakeGit(["1" * 40]))["contains"] is False


def test_git_runner_remote_failure_and_bad_remotes():
    with pytest.raises(g.Error) as e:
        g.first_parent_check("me/a", M3, exact=True, git_runner=FakeGit([], ls_code=128))
    assert e.value.code == "git_remote_unavailable"
    for bad in ("https://user:pw@github.com/me/a", "https://tok@github.com/me/a", "ssh://github.com/me/a",
                "-upload-pack=x", "relative/path"):
        with pytest.raises(ValueError):
            g.first_parent_check("me/a", M3, exact=True, remote=bad, git_runner=FakeGit([M3]))
    with pytest.raises(ValueError):
        g.first_parent_check("me/a", M3, exact=True, branch="--evil", git_runner=FakeGit([M3]))


def test_custom_subprocess_runner_against_local_remote(tmp_path):
    up = tmp_path / "up"
    up.mkdir()
    def run(*a):
        return subprocess.run(["git", "-C", str(up), "-c", "user.email=t@e", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q", "-b", "main"); (up / "f").write_text("1"); run("add", "-A"); run("commit", "-qm", "1")
    run("switch", "-q", "-c", "pr"); (up / "g").write_text("2"); run("add", "-A"); run("commit", "-qm", "2")
    inter = run("rev-parse", "HEAD")
    run("switch", "-q", "main"); run("merge", "-q", "--no-ff", "-m", "m", "pr")
    merge = run("rev-parse", "HEAD")
    def runner(args, cwd):
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           env={"PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                                "GIT_CONFIG_GLOBAL": os.devnull, "HOME": str(tmp_path)})
        return p.returncode, p.stdout
    assert g.first_parent_check("me/a", merge, exact=True, remote=str(up), git_runner=runner)["contains"] is True
    assert g.first_parent_check("me/a", inter, exact=True, remote=str(up), git_runner=runner)["contains"] is False
