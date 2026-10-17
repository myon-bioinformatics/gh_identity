"""subtree-sync: plan-only by default, push to a new branch only, pull with verification (offline)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import gh_identity as g

needs_subtree = pytest.mark.skipif(
    subprocess.run(["git", "subtree", "-h"], capture_output=True).returncode == 1,
    reason="git subtree not installed")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(root):
    _git(root, "config", "user.email", "t@e")
    _git(root, "config", "user.name", "t")


def _commit(root, files, msg):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "--", *files)
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _refs(root):
    return _git(root, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.fixture
def world(tmp_path):
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _identity(mono)
    _commit(mono, {"sub/a.py": "a = 1\n", "top.py": "x = 1\n"}, "one")
    _commit(mono, {"sub/b.py": "b = 2\n"}, "two")
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    return {"mono": mono, "child": child, "tmp": tmp_path}


def _seed_child(world):
    """Publish the current split of sub/ as the child's default branch `trunk`."""
    split = g.git_subtree_split(world["mono"], "sub")["sha"]
    _git(world["mono"], "push", "-q", str(world["child"]), f"{split}:refs/heads/trunk")
    return split


BAD = [
    dict(prefix="--rejoin", remote="/x", branch="b", direction="push"),
    dict(prefix="../up", remote="/x", branch="b", direction="push"),
    dict(prefix="sub", remote="https://user:tok@github.com/o/r", branch="b", direction="push"),
    dict(prefix="sub", remote="ssh://github.com/o/r", branch="b", direction="push"),
    dict(prefix="sub", remote="relative", branch="b", direction="push"),
    dict(prefix="sub", remote="/x", branch="--force", direction="push"),
    dict(prefix="sub", remote="/x", branch="a..b", direction="push"),
    dict(prefix="sub", remote="/x", branch="x.lock", direction="push"),
    dict(prefix="sub", remote="/x", branch="main", direction="push"),
    dict(prefix="sub", remote="/x", branch="master", direction="push"),
    dict(prefix="sub", remote="/x", branch="b", direction="sideways"),
    dict(prefix="sub", remote="/x", branch="b", direction="push", squash=True),
    dict(prefix="sub", remote=None, branch="b", direction="push"),
    dict(prefix="missing", remote="/x", branch="b", direction="push"),
]


@pytest.mark.parametrize("kwargs", BAD)
def test_bad_input_rejected_before_subtree_check(world, monkeypatch, kwargs):
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: pytest.fail("availability checked first"))
    with pytest.raises(ValueError):
        g.subtree_sync(world["mono"], **kwargs)


def test_missing_subtree_is_clear_error(world, monkeypatch):
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: False)
    with pytest.raises(g.Error) as e:
        g.subtree_sync(world["mono"], "sub", str(world["child"]), "feature", direction="push")
    assert e.value.code == "subtree_unavailable"
    r = subprocess.run([sys.executable, "-S", str(Path(g.__file__)), "subtree-sync", "--root", str(world["mono"]),
                        "--prefix", "sub", "--remote", str(world["child"]), "--branch", "feature",
                        "--direction", "push"], capture_output=True, text=True, env={"PATH": "/nonexistent"})
    assert r.returncode == 2 and not r.stdout


@needs_subtree
def test_push_plan_changes_nothing(world):
    before_mono, before_child = _refs(world["mono"]), _refs(world["child"])
    out = g.subtree_sync(world["mono"], "sub", str(world["child"]), "sync/one", direction="push")
    assert out["state"] == "planned" and out["write"] is False
    assert out["command"] == [
        "git", "push", "--", str(world["child"]), f"{out['split_sha']}:refs/heads/sync/one",
    ]
    assert out["provenance"]["trailer"] == f"Subtree-Split: {out['source']} {out['split_sha']} sub"
    assert _refs(world["mono"]) == before_mono and _refs(world["child"]) == before_child
    assert not (Path(_git(world["mono"], "rev-parse", "--absolute-git-dir")) / "subtree-cache").exists()


@needs_subtree
def test_push_write_creates_new_branch_and_verifies(world):
    out = g.subtree_sync(world["mono"], "sub", str(world["child"]), "sync/one", direction="push", write=True)
    assert out["state"] == "pushed" and out["verified"]["ok"]
    assert _git(world["child"], "rev-parse", "refs/heads/sync/one") == out["split_sha"]
    assert not (Path(_git(world["mono"], "rev-parse", "--absolute-git-dir")) / "subtree-cache").exists()
    with pytest.raises(ValueError, match="already exists"):
        g.subtree_sync(world["mono"], "sub", str(world["child"]), "sync/one", direction="push")


@needs_subtree
def test_push_refuses_remote_default_branch(world):
    _seed_child(world)
    with pytest.raises(ValueError, match="default branch"):
        g.subtree_sync(world["mono"], "sub", str(world["child"]), "trunk", direction="push", write=True)


def _added_world(tmp_path, squash):
    """Child repo with history, added into a monorepo by `git subtree add` (the normal pull setup)."""
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(child), str(work))
    _identity(work)
    _git(work, "checkout", "-q", "-b", "trunk")
    _commit(work, {"a.py": "a = 1\n"}, "child one")
    _git(work, "push", "-q", "origin", "HEAD:trunk")
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _identity(mono)
    _commit(mono, {"top.py": "x = 1\n"}, "mono one")
    _git(mono, "subtree", "add", "-q", "--prefix=sub", *(["--squash"] if squash else []), str(child), "trunk")
    return {"mono": mono, "child": child, "work": work, "tmp": tmp_path}


@needs_subtree
@pytest.mark.parametrize("squash", [False, True])
def test_pull_plan_then_write_verifies(tmp_path, squash):
    world = _added_world(tmp_path, squash)
    work = world["work"]
    _commit(work, {"c.py": "c = 3\n"}, "child change\n\nSubtree-Split: " + "0" * 40 + " " + "1" * 40 + " sub")
    _git(work, "push", "-q", "origin", "HEAD:trunk")
    tip = _git(work, "rev-parse", "HEAD")
    head_before = _git(world["mono"], "rev-parse", "HEAD")
    plan = g.subtree_sync(world["mono"], "sub", str(world["child"]), "trunk", direction="pull", squash=squash)
    assert plan["state"] == "planned" and plan["remote_tip"] == tip and plan["local_branch"] == "main"
    assert _git(world["mono"], "rev-parse", "HEAD") == head_before
    out = g.subtree_sync(world["mono"], "sub", str(world["child"]), "trunk", direction="pull", squash=squash,
                         write=True, message="sync sub from child")
    assert out["state"] == "pulled" and out["verified"]["ok"]
    assert (world["mono"] / "sub" / "c.py").read_text() == "c = 3\n"
    assert not (Path(_git(world["mono"], "rev-parse", "--absolute-git-dir")) / "subtree-cache").exists()
    existing = out["provenance"]["existing"]
    assert existing["ok"] is False and existing["trailers"][0]["split_sha"] == "1" * 40


@needs_subtree
def test_pull_refuses_dirty_tree(tmp_path):
    world = _added_world(tmp_path, False)
    (world["mono"] / "top.py").write_text("dirty\n")
    with pytest.raises(ValueError, match="uncommitted"):
        g.subtree_sync(world["mono"], "sub", str(world["child"]), "trunk", direction="pull")


@needs_subtree
def test_cli_plan_prints_command(world):
    r = subprocess.run([sys.executable, "-S", str(Path(g.__file__)), "subtree-sync", "--root", str(world["mono"]),
                        "--prefix", "sub", "--remote", str(world["child"]), "--branch", "sync/x",
                        "--direction", "push"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "planned" and out["command"][:2] == ["git", "push"]
    assert _refs(world["child"]) == ""
