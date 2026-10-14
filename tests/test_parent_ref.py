"""parent-pr.ref.json, first-parent pins, child-heads.json, redaction, enrollment progress (fake transport)."""
import base64
import hashlib
import json
import subprocess
from unittest.mock import patch

import pytest
import gh_identity as g

GOOD = {"schema": "parent-pr-ref/1", "repository": "me/parent", "pull_number": 78, "repository_id": 638801160,
        "optional": {"note": "x"}, "commit": "b" * 40}
DATA = b"x = 1\n"


def write(root, rel, obj):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)


def entry(dest, data, repository="me/up", source="m.py", commit="c" * 40):
    return {"repository": repository, "ref": commit, "commit": commit, "source": source, "destination": dest,
            "blob_sha": g.git_blob_sha(data), "sha256": hashlib.sha256(data).hexdigest()}


@pytest.fixture
def child(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "m.py").write_bytes(DATA)
    write(tmp_path, "vendor.lock.json", {"schema": "vendor-lock/1", "files": [entry("vendor/m.py", DATA)]})
    return tmp_path


# --- local parent_ref validation -------------------------------------------------

def test_parent_ref_missing_is_not_failure_by_default(child):
    out = g.vendor_lock_check(child)
    assert out["parent_ref"]["status"] == "missing" and out["ok"] is True


def test_parent_ref_templates_first_and_ok(child):
    write(child, "templates/parent-pr.ref.json", GOOD)
    write(child, "parent-pr.ref.json", {**GOOD, "commit": "0" * 40})
    out = g.vendor_lock_check(child)
    assert out["parent_ref"]["path"] == "templates/parent-pr.ref.json" and out["parent_ref"]["status"] == "ok"
    assert out["ok"] is True


@pytest.mark.parametrize("mutate,problem", [
    (lambda r: r.update(commit="0" * 40), "commit"),
    (lambda r: r.update(commit="B" * 40), "commit"),
    (lambda r: r.update(pull_number=0), "pull_number"),
    (lambda r: r.update(repository_id="638801160"), "repository_id"),
    (lambda r: r.update(schema="parent-pr-ref/2"), "schema"),
    (lambda r: r.update(repository="parent"), "repository"),
    (lambda r: r.update(extra=1), "unknown_fields"),
])
def test_parent_ref_invalid_fails_check(child, mutate, problem):
    ref = dict(GOOD)
    mutate(ref)
    write(child, "parent-pr.ref.json", ref)
    out = g.vendor_lock_check(child)
    assert out["parent_ref"]["status"] == "invalid" and problem in out["parent_ref"]["problems"] and out["ok"] is False


def test_parent_ref_explicit_path_required(child):
    assert g.vendor_lock_check(child, parent_ref="nope.json")["ok"] is False
    write(child, "parent-pr.ref.json", "{not json")
    assert g.vendor_lock_check(child)["parent_ref"]["problems"] == ["not_json"]


# --- first-parent ------------------------------------------------------------------

def commit(sha, *parents):
    return {"sha": sha, "parents": [{"sha": p} for p in parents]}


# main: M3 -> M2 -> M1 (first parents); M2 merges PR commits X2 -> X1 (second parent)
M1, M2, M3, X1, X2 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40)
HISTORY = [commit(M3, M2), commit(M2, M1, X2), commit(X2, X1), commit(X1, M1), commit(M1)]


def history_fake(method, path, **kw):
    if path == "repos/me/up":
        return {"default_branch": "main", "id": 1}
    if path.startswith("repos/me/up/commits?sha=main&per_page=100&page="):
        return HISTORY if path.endswith("page=1") else []
    raise g.Error("not_found_or_inaccessible")


@pytest.mark.parametrize("target,expected", [(M3, True), (M2, True), (M1, True), (X2, False), (X1, False), ("9" * 40, False)])
def test_first_parent_contains(target, expected):
    with patch.object(g, "request", side_effect=history_fake):
        assert g._first_parent_contains("me/up", target) is expected


def test_first_parent_undetermined_when_history_is_long():
    long = [commit(f"{i:040x}", f"{i + 1:040x}") for i in range(100)]
    with patch.object(g, "request", side_effect=lambda m, p, **k: {"default_branch": "main"} if p == "repos/me/up" else long):
        assert g._first_parent_contains("me/up", "f" * 40, max_pages=2) is None


def test_verify_remote_first_parent(child):
    lock = {"schema": "vendor-lock/1", "files": [entry("vendor/m.py", DATA, commit=X2)]}
    write(child, "vendor.lock.json", lock)
    with patch.object(g, "_remote_file", return_value=DATA), patch.object(g, "request", side_effect=history_fake):
        out = g.vendor_lock_verify_remote(child, first_parent=True)
    assert out["files"][0]["status"] == "ok" and out["files"][0]["first_parent"] is False and out["ok"] is False


# --- remote parent_ref --------------------------------------------------------------

def parent_fake(pr):
    def fake(method, path, **kw):
        if path == "repos/me/parent":
            return {"id": 638801160, "default_branch": "main"}
        if path == "repos/me/parent/pulls/78":
            return pr
        raise g.Error("not_found_or_inaccessible")
    return fake


@pytest.mark.parametrize("pr,status,problems", [
    ({"merged_at": "t", "merge_commit_sha": "b" * 40, "base": {"ref": "main"}, "head": {"sha": "e" * 40}}, "ok", []),
    ({"state": "open", "head": {"sha": "b" * 40}, "base": {"ref": "main"}}, "ok", []),
    ({"merged_at": "t", "merge_commit_sha": "e" * 40, "base": {"ref": "main"}, "head": {"sha": "b" * 40}}, "mismatch", ["commit_mismatch"]),
    ({"merged_at": "t", "merge_commit_sha": "b" * 40, "base": {"ref": "dev"}}, "mismatch", ["not_merged_into_default_branch"]),
    ({"state": "closed", "head": {"sha": "b" * 40}}, "mismatch", ["commit_mismatch", "pull_closed_unmerged"]),
])
def test_verify_parent_ref(pr, status, problems):
    with patch.object(g, "request", side_effect=parent_fake(pr)):
        out = g.verify_parent_ref(GOOD)
    assert out["status"] == status and out["problems"] == problems


def test_verify_parent_ref_repository_id():
    pr = {"state": "open", "head": {"sha": "b" * 40}}
    with patch.object(g, "request", side_effect=parent_fake(pr)):
        assert g.verify_parent_ref({**GOOD, "repository_id": 1})["problems"] == ["repository_id_mismatch"]


def test_verify_remote_with_parent_ref(child):
    write(child, "templates/parent-pr.ref.json", GOOD)
    pr = {"merged_at": "t", "merge_commit_sha": "b" * 40, "base": {"ref": "main"}}
    with patch.object(g, "_remote_file", return_value=DATA), patch.object(g, "request", side_effect=parent_fake(pr)):
        out = g.vendor_lock_verify_remote(child, check_parent_ref=True)
    assert out["parent_ref"]["remote"]["status"] == "ok" and out["ok"] is True


# --- child-heads.json ------------------------------------------------------------------

def test_load_child_heads(tmp_path):
    write(tmp_path, "h.json", {"schema": "child-heads/1", "ref": "main",
                               "heads": {"me/a": {"sha": "a" * 40, "repository_id": 1}}})
    assert g.load_child_heads(tmp_path / "h.json") == {"me/a": "a" * 40}
    write(tmp_path, "bad.json", {"schema": "child-heads/1", "heads": {"me/a": {"sha": "main"}}})
    with pytest.raises(ValueError):
        g.load_child_heads(tmp_path / "bad.json")


def test_duplicate_files_uses_heads():
    seen = []
    def fake(method, path, **kw):
        seen.append(path)
        if path == "repos/me/a":
            return {"visibility": "public", "default_branch": "main"}
        if path.startswith("repos/me/a/commits/"):
            return {"sha": path.rsplit("/", 1)[1]}
        if path.startswith("repos/me/a/git/trees/"):
            return {"tree": [], "truncated": False}
        raise g.Error("not_found_or_inaccessible")
    with patch.object(g, "request", side_effect=fake):
        out = g.duplicate_files("me", ["a"], heads={"me/a": "a" * 40})
    assert "repos/me/a/commits/" + "a" * 40 in seen and out["scanned"]["me/a"]["from_heads"] is True


# --- redaction ---------------------------------------------------------------------------

def test_public_targets_redacts_names():
    def fake(method, path, **kw):
        return {"repos/me/pub": {"visibility": "public", "default_branch": "main"},
                "repos/me/hidden": {"visibility": "private", "private": True},
                "repos/me/old": {"visibility": "public", "archived": True}}.get(path) or (_ for _ in ()).throw(g.Error("not_found_or_inaccessible"))
    with patch.object(g, "request", side_effect=fake):
        targets, redacted = g._public_targets("me", ["pub", "hidden", "old", "gone"])
    assert [t["name"] for t in targets] == ["pub"] and redacted == 3


# --- enrollment progress -------------------------------------------------------------------

def test_enrollment_progress():
    current = b"new\n"
    def lock(files):
        return {"encoding": "base64", "content": base64.b64encode(json.dumps({"schema": "vendor-lock/1", "files": files}).encode()).decode()}
    enrolled = entry("vendor/gh_identity.py", b"old\n", repository="me/up", source="gh_identity.py", commit=X2)
    fresh = entry("vendor/gh_identity.py", current, repository="me/up", source="gh_identity.py", commit=M3)
    table = {
        "repos/me/up": {"default_branch": "main", "visibility": "public"},
        "repos/me/up/commits/main": {"sha": M3},
        "repos/me/a/commits/main": {"sha": "a" * 40},
        "repos/me/b/commits/main": {"sha": "b" * 40},
        "repos/me/c/commits/main": {"sha": "c" * 40},
        "repos/me/a/git/trees/" + "a" * 40 + "?recursive=1": {"tree": [{"path": "vendor.lock.json", "type": "blob", "mode": "100644", "sha": "0" * 40}]},
        "repos/me/b/git/trees/" + "b" * 40 + "?recursive=1": {"tree": [{"path": "vendor.lock.json", "type": "blob", "mode": "100644", "sha": "0" * 40}]},
        "repos/me/c/git/trees/" + "c" * 40 + "?recursive=1": {"tree": []},
        "repos/me/a/contents/vendor.lock.json?ref=" + "a" * 40: lock([enrolled]),
        "repos/me/b/contents/vendor.lock.json?ref=" + "b" * 40: lock([fresh]),
    }
    def fake(method, path, **kw):
        if path in table:
            return table[path]
        if path.startswith("repos/me/up/commits?sha=main"):
            return HISTORY if path.endswith("page=1") else []
        raise g.Error("not_found_or_inaccessible")
    repos = [{"name": n, "default_branch": "main", "owner": {"login": "me"}} for n in ("up", "a", "b", "c")] + \
            [{"name": "secret", "private": True, "visibility": "private", "owner": {"login": "me"}}]
    with patch.object(g, "pages", return_value=repos), patch.object(g, "request", side_effect=fake), \
         patch.object(g, "_remote_file", return_value=current):
        out = g.enrollment_progress("me", provider="me/up", source="gh_identity.py")
    rows = {r["repository"]: r for r in out["repositories"]}
    assert set(rows) == {"me/a", "me/b", "me/c"} and "secret" not in json.dumps(out)
    assert rows["me/a"]["enrolled"] and rows["me/a"]["stale"] is True and rows["me/a"]["first_parent"] is False
    assert rows["me/b"]["stale"] is False and rows["me/b"]["first_parent"] is True
    assert rows["me/c"]["enrolled"] is False
    assert out["counts"] == {"repositories": 3, "enrolled": 2, "not_enrolled": 1, "stale": 1, "not_first_parent": 1,
                             "first_parent_unknown": 0, "errors": 0, "redacted": 0}


# --- subtree mainline ------------------------------------------------------------------------

needs_subtree = pytest.mark.skipif(subprocess.run(["git", "subtree", "-h"], capture_output=True).returncode == 1,
                                   reason="git subtree not installed")


@needs_subtree
def test_subtree_sync_mainline(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@e", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a").write_text("a\n")
    git("add", "-A"); git("commit", "-q", "-m", "one")
    git("switch", "-q", "-c", "topic")
    (tmp_path / "sub" / "b").write_text("b\n")
    git("add", "-A"); git("commit", "-q", "-m", "topic")
    topic = git("rev-parse", "HEAD")
    git("switch", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "merge", "topic")
    assert g.subtree_sync_status(tmp_path, "sub", tmp_path, mainline="main")["source_on_first_parent"] is True
    out = g.subtree_sync_status(tmp_path, "sub", tmp_path, revision=topic, mainline="main")
    assert out["source_on_first_parent"] is False
    assert g.subtree_sync_status(tmp_path, "sub", tmp_path)["source_on_first_parent"] is None
