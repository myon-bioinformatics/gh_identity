"""Regression: git subtree split can exit 0 with a stale SHA; tree validation must catch it."""
import subprocess
import sys
import contextlib
from pathlib import Path
from types import SimpleNamespace

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
    assert not any(a[:1] == ["push"] for a in net_calls)


@needs_subtree
def test_push_uses_validated_split_not_subtree_push(mono, tmp_path, monkeypatch):
    """`git subtree push` re-splits internally; push the validated split SHA directly."""
    root, stale_sha = mono
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    mono, child = root, child
    good = g.git_subtree_split(mono, "sub")["sha"]
    stale = stale_sha
    real = g._git_run
    push_specs = []

    def spy(run_root, args, **kw):
        if args[:2] == ["subtree", "split"]:
            return (stale + "\n").encode(), False, 0
        if args[:2] == ["subtree", "push"]:
            raise AssertionError("git subtree push must not run")
        if args[:1] == ["push"] and kw.get("inherit_auth"):
            push_specs.append(args)
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", spy)
    out = g.subtree_sync(mono, "sub", str(child), "sync/x", direction="push", write=True)
    assert out["verified"]["ok"]
    assert _git(child, "rev-parse", "refs/heads/sync/x") == good
    assert _git(child, "rev-parse", "refs/heads/sync/x") != stale
    assert push_specs and push_specs[0][-1] == f"{good}:refs/heads/sync/x"


@needs_subtree
def test_same_tree_unrelated_history_is_subtree_add_required(mono, tmp_path):
    root, _ = mono
    good = g.git_subtree_split(root, "sub")["sha"]
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _commit(other, {"a.py": "v2\n"}, "independent history")
    status = g.subtree_sync_status(root, "sub", other)
    assert status["trees_match"] and not status["history_related"]
    assert status["state"] == "subtree_add_required"
    assert status["other_ahead"] > 0
    assert not status["split_in_other"] and not status["other_head_in_split"]
    assert status["monorepo_ahead"] == len(g._rev_list(root, good))
    rec = g.sync_provenance(root, "sub", other)
    assert rec["state"] == "subtree_add_required"
    assert rec["trees_match"] and rec["history_related"] is False


@needs_subtree
def test_push_revalidates_after_planning_and_never_writes_on_mismatch(world, monkeypatch):
    """Planning passes; only the check immediately before push sees the mismatch."""
    mono, child = world["mono"], world["child"]
    real_check, real_guard, real_run = (g._subtree_split_matches_prefix_tree,
                                       g._subtree_push_guards, g._git_run)
    planning_done, checks, pushes = False, [], []

    def guard(*args, **kwargs):
        nonlocal planning_done
        result = real_guard(*args, **kwargs)
        planning_done = True
        return result

    def check(*args, **kwargs):
        checks.append(planning_done)
        result = real_check(*args, **kwargs)
        return {**result, "trees_match": False} if planning_done else result

    def run(root, args, **kwargs):
        if args[0] == "push" or args[:2] == ["subtree", "push"]:
            pushes.append(args)
        return real_run(root, args, **kwargs)

    monkeypatch.setattr(g, "_subtree_push_guards", guard)
    monkeypatch.setattr(g, "_subtree_split_matches_prefix_tree", check)
    monkeypatch.setattr(g, "_git_run", run)
    before = _git(child, "for-each-ref")
    with pytest.raises(g.Error) as err:
        g.subtree_sync(mono, "sub", str(child), "sync/revalidate", direction="push", write=True)
    assert err.value.code == "stale_subtree_split"
    assert False in checks and checks[-1] is True
    assert not pushes
    assert _git(child, "for-each-ref") == before


@needs_subtree
@pytest.mark.parametrize("command", ["split", "status", "preflight"])
@pytest.mark.parametrize("fetch_head_exists", [False, True])
def test_fallback_preserves_checkout_metadata(mono, tmp_path, monkeypatch, command, fetch_head_exists):
    root, stale_sha = mono
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _commit(other, {"a.py": "v2\n"}, "other")
    _git(root, "pack-refs", "--all")
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir"))
    fetch_head = git_dir / "FETCH_HEAD"
    if fetch_head_exists:
        fetch_head.write_bytes(b"existing FETCH_HEAD\n")
    metadata = [git_dir / name for name in ("HEAD", "index", "packed-refs", "FETCH_HEAD", "config")]
    before = {p: p.read_bytes() if p.exists() else None for p in metadata}
    refs = _git(root, "for-each-ref")
    work = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and git_dir not in p.parents}
    real = g._git_run

    def run(run_root, args, **kwargs):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", run)
    calls = {"split": lambda: g.git_subtree_split(root, "sub"),
             "status": lambda: g.subtree_sync_status(root, "sub", other),
             "preflight": lambda: g.split_preflight(root, "sub")}
    result = calls[command]()
    assert result["reason"] is None
    assert result["fallback_used"] and result["fallback_elapsed_seconds"] > 0
    assert result.get("method", result.get("split_method")) == "filter-branch"
    assert {p: p.read_bytes() if p.exists() else None for p in metadata} == before
    assert _git(root, "for-each-ref") == refs
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and git_dir not in p.parents} == work
    assert not (git_dir / "refs/original").exists()
    assert not (git_dir / "subtree-cache").exists()


@needs_subtree
def test_fallback_handles_unreferenced_revision(mono, monkeypatch):
    root, stale_sha = mono
    source = _commit(root, {"sub/a.py": "unreferenced\n"}, "dangling source")
    _git(root, "reset", "--hard", "HEAD~1")
    real = g._git_run

    def run(run_root, args, **kwargs):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", run)
    result = g.git_subtree_split(root, "sub", source)
    assert result["reason"] is None and result["fallback_used"]
    assert result["source"] == source
    assert g._subtree_split_matches_prefix_tree(root, source, "sub", result["sha"])["trees_match"]


@pytest.mark.parametrize("key", ["GIT_DIR", "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "PATH"])
def test_extra_env_cannot_override_sanitized_environment(mono, key):
    with pytest.raises(ValueError, match="unsupported extra_env"):
        g._git_run(mono[0], ["rev-parse", "HEAD"], extra_env={key: "elsewhere"})


def test_split_and_fallback_share_timeout_budget(monkeypatch):
    now, budgets = [100.0], []
    monkeypatch.setattr(g, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: True)
    monkeypatch.setattr(g, "_prefix_tree_at", lambda *a: "c" * 40)
    monkeypatch.setattr(g, "_subtree_cache_cleanup", lambda root: contextlib.nullcontext())
    monkeypatch.setattr(g, "_subtree_split_matches_prefix_tree",
                        lambda *a: {"prefix_tree": "c" * 40, "split_tree": "d" * 40, "trees_match": False})

    def run(root, args, **kwargs):
        if args[0] == "rev-parse":
            return b"a" * 40, False, 0
        if args[0] == "cat-file":
            return b"tree", False, 0
        assert args[:2] == ["subtree", "split"]
        budgets.append(kwargs["timeout"])
        now[0] = 140.0
        return b"b" * 40, False, 0

    def fallback(root, prefix, source, timeout):
        budgets.append(timeout)
        now[0] = 150.0
        return None

    monkeypatch.setattr(g, "_git_run", run)
    monkeypatch.setattr(g, "_subtree_split_filter_branch", fallback)
    result = g.git_subtree_split(".", "sub", timeout=100)
    assert budgets == [100.0, 60.0]
    assert result["fallback_used"] and result["fallback_elapsed_seconds"] == 10.0
    assert result["reason"] == "stale_subtree_split"


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
