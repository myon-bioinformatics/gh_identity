"""Push guards and history_related semantics for subtree sync."""
import subprocess

import pytest
import gh_identity as g

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
    _git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


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


@needs_subtree
def test_push_blocked_when_child_trunk_not_ancestor(world):
    mono, child = world["mono"], world["child"]
    split = g.git_subtree_split(mono, "sub")["sha"]
    _git(mono, "push", "-q", str(child), f"{split}:refs/heads/trunk")
    work = world["mono"].parent / "work"
    _git(world["mono"].parent, "clone", "-q", str(child), str(work))
    _git(work, "config", "user.email", "t@e")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "trunk")
    _commit(work, {"ahead-on-child.py": "x\n"}, "child ahead")
    _git(work, "push", "-q", "origin", "HEAD:trunk")
    with pytest.raises(ValueError, match="not an ancestor"):
        g.subtree_sync(mono, "sub", str(child), "sync/new", direction="push", remote_compare_ref="trunk")


def test_push_guard_wrapper_reports_isolated_errors(monkeypatch, world):
    mono, child = world["mono"], world["child"]
    seen = []

    def isolated(*args, **kwargs):
        seen.append(kwargs.get("ref_name"))
        raise ValueError("child-only paths not present in split: only-on-child.txt")

    monkeypatch.setattr(g, "_subtree_push_guards_isolated", isolated)
    listing = "1111111111111111111111111111111111111111 refs/heads/trunk\n"
    net = lambda args, ok=(0,): (listing.encode(), False, 0)
    with pytest.raises(ValueError, match="child-only paths"):
        g._subtree_push_guards(mono, "/nonexistent", "sub", "2" * 40, "trunk", net, compare_ref="trunk")
    assert seen == ["trunk"]


def _ls_net(root):
    return lambda args, ok=(0,): g._git_run(root, args, ok=ok, max_bytes=1024 * 1024)


@needs_subtree
def test_real_guard_detects_child_only_paths(world):
    """The real isolated guard (no mocks): the child trunk has a path the new split dropped."""
    mono, child = world["mono"], world["child"]
    _commit(mono, {"sub/extra.txt": "x\n"}, "add extra")
    first = g.git_subtree_split(mono, "sub")["sha"]
    _git(mono, "push", "-q", str(child), f"{first}:refs/heads/trunk")
    _git(mono, "rm", "-q", "sub/extra.txt")
    _git(mono, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "drop extra")
    second = g.git_subtree_split(mono, "sub")["sha"]
    with pytest.raises(ValueError, match="child-only paths not present in split: extra.txt"):
        g._subtree_push_guards_isolated(mono, str(child), first, second, 60, ref_name="trunk")
    with pytest.raises(ValueError, match="child-only paths not present in split: extra.txt"):
        g._subtree_push_guards(mono, str(child), "sub", second, "trunk", _ls_net(mono), compare_ref="trunk")
    with pytest.raises(ValueError, match="child-only paths"):
        g.subtree_sync(mono, "sub", str(child), "sync/new", direction="push", remote_compare_ref="trunk")


@needs_subtree
def test_real_guard_passes_and_names_ref_on_ancestry_failure(world):
    mono, child = world["mono"], world["child"]
    first = g.git_subtree_split(mono, "sub")["sha"]
    _git(mono, "push", "-q", str(child), f"{first}:refs/heads/trunk")
    _commit(mono, {"sub/b.py": "b = 1\n"}, "two")
    second = g.git_subtree_split(mono, "sub")["sha"]
    assert g._subtree_push_guards_isolated(mono, str(child), first, second, 60) == []
    out = g._subtree_push_guards(mono, str(child), "sub", second, "trunk", _ls_net(mono), compare_ref="trunk")
    assert out["compare_sha"] == first and out["checked_in"] == "temporary_clone"
    with pytest.raises(ValueError, match="remote ref trunk is not an ancestor"):
        g._subtree_push_guards_isolated(mono, str(child), second, first, 60, ref_name="trunk")


def test_isolated_guard_failure_branches(world, monkeypatch, tmp_path):
    mono = world["mono"]
    head = _git(mono, "rev-parse", "HEAD")
    for bad in (0, -1, float("inf"), float("nan"), True, "1"):
        with pytest.raises(ValueError, match="timeout"):
            g._subtree_push_guards_isolated(mono, str(world["child"]), head, head, bad)
    with pytest.raises(ValueError, match="could not fetch remote ref"):
        g._subtree_push_guards_isolated(mono, str(tmp_path / "missing.git"), head, head, 60)
    real = g._git_run

    def no_clone(run_root, args, **kw):
        if args[:1] == ["clone"]:
            return b"", False, 128
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", no_clone)
    with pytest.raises(ValueError, match="could not clone"):
        g._subtree_push_guards_isolated(mono, str(world["child"]), head, head, 60)
    monkeypatch.setattr(g, "_git_run", real)
    monkeypatch.setattr(g, "_commit_root_tree", lambda root, commit: None)
    with pytest.raises(ValueError, match="could not resolve trees"):
        g._subtree_push_guards_isolated(mono, str(mono), head, head, 60)


@needs_subtree
def test_push_refuses_branch_created_concurrently_even_as_fast_forward(world, monkeypatch):
    """Between the existence check and the push another writer creates the branch at an
    ancestor of the split; without the empty lease the push would fast-forward it."""
    mono, child = world["mono"], world["child"]
    ancestor = g.git_subtree_split(mono, "sub")["sha"]
    _git(mono, "push", "-q", str(child), f"{ancestor}:refs/heads/trunk")
    _commit(mono, {"sub/b.py": "b = 1\n"}, "two")
    real = g._git_run
    pushes = []

    def racing(run_root, args, **kw):
        if args[:1] == ["push"] and kw.get("inherit_auth"):
            pushes.append(list(args))
            _git(child.parent, "--git-dir", str(child), "update-ref", "refs/heads/sync/race", ancestor)
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", racing)
    with pytest.raises(g.Error) as e:
        g.subtree_sync(mono, "sub", str(child), "sync/race", direction="push", write=True)
    assert e.value.code == "subtree_push_failed"
    assert pushes and pushes[0][1] == "--force-with-lease=refs/heads/sync/race:"
    assert _git(child.parent, "--git-dir", str(child), "rev-parse", "refs/heads/sync/race") == ancestor


@needs_subtree
@pytest.mark.parametrize("squash", [False, True])
def test_history_related_from_subtree_add_only_for_actual_child(tmp_path, squash):
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(child), str(work))
    _git(work, "config", "user.email", "t@e")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "trunk")
    _commit(work, {"a.py": "a = 1\n"}, "child one")
    _git(work, "push", "-q", "origin", "HEAD:trunk")
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _git(mono, "config", "user.email", "t@e")
    _git(mono, "config", "user.name", "t")
    _commit(mono, {"top.py": "x = 1\n"}, "mono one")
    before_add = _git(mono, "rev-parse", "HEAD")
    _git(mono, "subtree", "add", "-q", "--prefix=sub", *(["--squash"] if squash else []), str(child), "trunk")
    status = g.subtree_sync_status(mono, "sub", work)
    assert status["history_related"] and status["trees_match"]
    assert status["state"] == "in_sync"
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _commit(other, {"a.py": "a = 1\n"}, "unrelated same tree")
    status = g.subtree_sync_status(mono, "sub", other)
    assert status["history_related"] is False
    assert status["trees_match"] and status["state"] == "subtree_add_required"
    assert "git subtree add" in (status.get("guidance") or "")
    assert status["other_ahead"] == 1 and status["monorepo_ahead"] > 0
    assert not status["split_in_other"] and not status["other_head_in_split"]
    child_history = set(g._rev_list(work, _git(work, "rev-parse", "HEAD")))
    assert g._monorepo_has_subtree_trailers(mono, "sub", "HEAD", child_history)
    assert not g._monorepo_has_subtree_trailers(mono, "sub", before_add, child_history)


@needs_subtree
def test_history_related_mutation_falls_back_to_shared_shas_only(tmp_path, monkeypatch):
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _commit(mono, {"sub/a.py": "v\n", "top.py": "t\n"}, "one")
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    good = g.git_subtree_split(mono, "sub")["sha"]
    _git(other, "fetch", "-q", str(mono), good)
    _git(other, "reset", "-q", "--hard", good)
    monkeypatch.setattr(g, "_monorepo_has_subtree_trailers", lambda *a, **k: False)
    monkeypatch.setattr(g, "_git_merge_base_exists", lambda *a, **k: False)
    status = g.subtree_sync_status(mono, "sub", other)
    assert status["history_related"] and status["state"] == "in_sync"
    monkeypatch.setattr(g, "_subtree_history_related", lambda *a, **k: False)
    status = g.subtree_sync_status(mono, "sub", other)
    assert status["history_related"] is False
    assert status["trees_match"] and status["state"] == "subtree_add_required"
    assert status.get("guidance")


@needs_subtree
def test_push_tree_gate_called_once_per_write(world, monkeypatch):
    mono, child = world["mono"], world["child"]
    calls = []
    real = g._subtree_push_split_tree_gate

    def gate(*a, **k):
        calls.append(1)
        if len(calls) > 1:
            raise g.Error("stale_subtree_split")
        return real(*a, **k)

    monkeypatch.setattr(g, "_subtree_push_split_tree_gate", gate)
    g.subtree_sync(mono, "sub", str(child), "sync/once", direction="push", write=True)
    assert len(calls) == 1


def test_explicit_compare_ref_missing_is_an_error_not_a_fallback(world):
    calls = []

    def net(args, ok=(0,)):
        calls.append(args[-1])
        ref = args[-1]
        return ((("1" * 40) + "\t" + ref + "\n").encode() if ref == "refs/heads/main" else b"", False, 0)

    with pytest.raises(ValueError, match="remote compare ref release was not found"):
        g._subtree_push_guards(world["mono"], str(world["child"]), "sub", "2" * 40, "main", net,
                               compare_ref="release")
    assert calls == ["refs/heads/release"]


def test_default_compare_candidates_are_deduplicated(world):
    calls = []

    def net(args, ok=(0,)):
        calls.append(args[-1])
        return b"", False, 2

    out = g._subtree_push_guards(world["mono"], str(world["child"]), "sub", "2" * 40, "main", net)
    assert out["checked_in"] == "ls-remote_only" and out["compare_ref"] is None
    assert calls == ["refs/heads/main", "refs/heads/master", "refs/heads/trunk"]
