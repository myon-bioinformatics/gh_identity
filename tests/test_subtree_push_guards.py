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


def test_push_guard_lists_child_only_paths(monkeypatch, world):
    mono, child = world["mono"], world["child"]
    split = g.git_subtree_split(mono, "sub")["sha"]
    monkeypatch.setattr(g, "_git_is_ancestor", lambda *a, **k: True)
    monkeypatch.setattr(g, "_fetch_remote_commit", lambda *a, **k: None)
    tip = "1111111111111111111111111111111111111111"
    monkeypatch.setattr(g, "_commit_root_tree", lambda r, c: "split-tree" if c == split else "child-tree")
    monkeypatch.setattr(g, "_git_tree_paths",
                        lambda r, t: {"a.py"} if t == "split-tree" else {"a.py", "only-on-child.txt"})
    listing = "1111111111111111111111111111111111111111 refs/heads/trunk\n"
    net = lambda args, ok=(0,): (listing.encode(), False, 0)
    with pytest.raises(ValueError, match="child-only paths"):
        g._subtree_push_guards(mono, str(child), "sub", split, "trunk", net, compare_ref="trunk")


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
