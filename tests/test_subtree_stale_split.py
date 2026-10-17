"""Regression: git subtree split can exit 0 with a stale SHA; tree validation must catch it."""
import subprocess
import sys
from pathlib import Path

import pytest
import gh_identity as g

ENV = ["-c", "user.email=t@e", "-c", "user.name=t"]
needs_subtree = pytest.mark.skipif(
    subprocess.run(["git", "subtree", "-h"], capture_output=True).returncode == 1,
    reason="git subtree not installed")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(root, files, msg):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, *ENV, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def mono(tmp_path):
    root = tmp_path / "mono"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    old = _commit(root, {"sub/a.py": "v1\n", "top.py": "t\n"}, "one")
    _commit(root, {"sub/a.py": "v2\n"}, "two")
    return root, old


@needs_subtree
def test_stale_subtree_split_corrected(mono, monkeypatch):
    root, stale_sha = mono
    real = g._git_run

    def spy(run_root, args, **kw):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", spy)
    out = g.git_subtree_split(root, "sub")
    check = g._subtree_split_matches_prefix_tree(root, out["source"], "sub", out["sha"])
    assert check["trees_match"]
    assert out["sha"] != stale_sha
    assert out["warning"] in ("subtree_split_stale", "subtree_split_stale_history_short")
    assert out["method"] == "filter-branch"


@needs_subtree
def test_stale_split_sync_status_in_sync_not_other_ahead(mono, tmp_path, monkeypatch):
    root, stale_sha = mono
    real = g._git_run

    def spy(run_root, args, **kw):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", spy)
    good = g.git_subtree_split(root, "sub")
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "fetch", "-q", str(root), good["sha"])
    _git(other, "reset", "-q", "--hard", good["sha"])
    status = g.subtree_sync_status(root, "sub", other)
    assert status["trees_match"] and status["state"] == "in_sync" and status["other_ahead"] == 0


@needs_subtree
def test_tree_check_mutation_breaks_stale_detection(mono, monkeypatch):
    """If tree validation is disabled, stale split is wrongly accepted."""
    root, stale_sha = mono
    real = g._git_run

    def spy(run_root, args, **kw):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", spy)

    def always_match(*args, **kwargs):
        return {"prefix_tree": "a", "split_tree": "a", "trees_match": True}

    monkeypatch.setattr(g, "_subtree_split_matches_prefix_tree", always_match)
    out = g.git_subtree_split(root, "sub")
    assert out["sha"] == stale_sha


@needs_subtree
def test_push_write_refuses_stale_split(world, monkeypatch):
    mono, child = world["mono"], world["child"]
    net_calls = []

    def fake_split(root, prefix, revision="HEAD", *, timeout=600):
        return {"schema": "gh-identity-subtree-split/1", "prefix": "sub", "write": False,
                "source": _git(mono, "rev-parse", "HEAD"), "sha": "0" * 40, "reason": None,
                "method": "subtree"}

    real_run = g._git_run

    def net_spy(run_root, args, **kw):
        if kw.get("inherit_auth"):
            net_calls.append(args)
        return real_run(run_root, args, **kw)

    monkeypatch.setattr(g, "git_subtree_split", fake_split)
    monkeypatch.setattr(g, "_git_run", net_spy)
    monkeypatch.setattr(g, "_subtree_split_matches_prefix_tree",
                        lambda *a, **k: {"prefix_tree": "a" * 40, "split_tree": "b" * 40, "trees_match": False})
    with pytest.raises(g.Error) as err:
        g.subtree_sync(mono, "sub", str(child), "sync/stale-test", direction="push", write=True)
    assert err.value.code == "stale_subtree_split"
    assert not any(a[:2] == ["subtree", "push"] for a in net_calls)


@pytest.fixture
def world(tmp_path):
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _git(mono, "config", "user.email", "t@e")
    _git(mono, "config", "user.name", "t")
    _commit(mono, {"sub/a.py": "a = 1\n", "top.py": "x = 1\n"}, "one")
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    return {"mono": mono, "child": child}
