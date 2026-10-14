"""subtree-split: read-only `git subtree split --prefix` returning the split SHA."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import gh_identity as g


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _refs(root):
    return _git(root, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("x\n")
    (tmp_path / "top.txt").write_text("t\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return tmp_path


needs_subtree = pytest.mark.skipif(
    subprocess.run(["git", "subtree", "-h"], capture_output=True).returncode == 1,
    reason="git subtree not installed")


@needs_subtree
def test_split_returns_sha_and_changes_no_refs(repo):
    before, head = _refs(repo), _git(repo, "rev-parse", "HEAD")
    out = g.git_subtree_split(repo, "sub/")
    assert out["reason"] is None and out["write"] is False and out["prefix"] == "sub"
    assert out["source"] == head
    assert _git(repo, "rev-parse", out["sha"] + "^{tree}") == _git(repo, "rev-parse", "HEAD:sub")
    assert _refs(repo) == before and _git(repo, "rev-parse", "HEAD") == head
    assert not (repo / ".git" / "subtree-cache").exists()
    assert _git(repo, "status", "--porcelain") == ""


@needs_subtree
def test_existing_subtree_cache_is_preserved(repo):
    keep = repo / ".git" / "subtree-cache" / "1"
    keep.mkdir(parents=True)
    g.git_subtree_split(repo, "sub")
    assert sorted(p.name for p in keep.parent.iterdir()) == ["1"]


@needs_subtree
def test_cli(repo):
    proc = subprocess.run([sys.executable, str(Path(g.__file__)), "subtree-split", "--root", str(repo), "--prefix", "sub"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert len(json.loads(proc.stdout)["sha"]) == 40


def test_missing_subtree_is_explicit(repo, monkeypatch):
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: False)
    out = g.git_subtree_split(repo, "sub")
    assert out["sha"] is None and out["reason"] == "subtree_unavailable"


def test_missing_prefix(repo):
    assert g.git_subtree_split(repo, "nope")["reason"] == "prefix_not_found"
    assert g.git_subtree_split(repo, "top.txt")["reason"] == "prefix_not_found"


@pytest.mark.parametrize("prefix", ["--rejoin", "-b", "sub/--branch=x", "", "/abs", "../out", "sub/../..", ".git", ".", "a\nb"])
def test_rejects_unsafe_prefix(repo, prefix):
    with pytest.raises(ValueError):
        g.git_subtree_split(repo, prefix)


@pytest.mark.parametrize("revision", ["--all", "-n", "nope", ""])
def test_rejects_bad_revision(repo, revision):
    with pytest.raises(ValueError):
        g.git_subtree_split(repo, "sub", revision)


def test_never_passes_ref_changing_options(repo, monkeypatch):
    calls = []
    real = g._git_run
    def spy(root, args, **kw):
        calls.append(list(args))
        if args[:1] == ["subtree"]:
            return (b"a" * 40 + b"\n", False, 0)
        return real(root, args, **kw)
    monkeypatch.setattr(g, "_git_run", spy)
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: True)
    assert g.git_subtree_split(repo, "sub")["sha"] == "a" * 40
    sub = [c for c in calls if c[:1] == ["subtree"]][0]
    assert sub[:2] == ["subtree", "split"]
    assert not any(a.startswith(("--branch", "-b", "--rejoin", "--onto", "--annotate")) for a in sub)
