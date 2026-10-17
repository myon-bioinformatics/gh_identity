"""merge-trial, git-activity (local, read-only) and commit-files (Git Data API, stubbed)."""
import hashlib
import json
import os
import subprocess
import sys

import pytest
import gh_identity as g


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@e", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "r"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (root / "img.bin").write_bytes(b"\0\1")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    for branch, path, data in (("clean", "b.txt", b"new\n"), ("clash", "a.txt", b"ONE\ntwo\n"), ("bin", "img.bin", b"\0\2")):
        _git(root, "switch", "-q", "-c", branch, "main")
        (root / path).write_bytes(data)
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", branch)
    _git(root, "switch", "-q", "main")
    (root / "a.txt").write_text("uno\ntwo\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "main edit")
    return root


def test_merge_trial_is_read_only(repo):
    before = (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain"))
    clean = g.merge_trial(repo, "clean")
    assert clean["status"] == "clean" and clean["ok"] and clean["conflicts"] == []
    assert clean["changed"] == [{"path": "b.txt", "added": 1, "removed": 0}]
    clash = g.merge_trial(repo, "clash")
    assert clash["status"] == "conflict" and not clash["ok"] and clash["conflicts"] == ["a.txt"]
    binary = g.merge_trial(repo, "bin")
    assert binary["changed"] == [{"path": "img.bin", "added": None, "removed": None}]
    assert g.merge_trial(repo, "main~1")["status"] == "already_merged"
    missing = g.merge_trial(repo, "e7ef1e62299a327681320c0bcdb03f2c32320d97")
    assert missing["status"] == "missing_object" and missing["missing"] == ["ref"]
    assert (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain")) == before
    text = g.render_merge_trial(clash)
    assert text.startswith("conflict clash@") and "conflict a.txt" in text
    assert "fetch it first" in g.render_merge_trial(missing)
    assert "img.bin +bin/-bin" in g.render_merge_trial(binary)
    for bad in ("", "-x", "a\nb"):
        with pytest.raises(ValueError):
            g.merge_trial(repo, bad)


def test_merge_trial_lists_at_most_40_rows(repo):
    _git(repo, "switch", "-q", "-c", "many", "main")
    for i in range(45):
        (repo / f"f{i}.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "many")
    _git(repo, "switch", "-q", "main")
    assert "... 5 more" in g.render_merge_trial(g.merge_trial(repo, "many"))


def test_git_activity_objects_locks_and_processes(repo, tmp_path):
    head = _git(repo, "rev-parse", "HEAD")
    out = g.git_activity(repo, [head, head[:7], "e7ef1e62299a327681320c0bcdb03f2c32320d97"])
    assert out["head"] == head and [o["type"] for o in out["objects"]] == ["commit", "commit", "missing"]
    assert out["busy"] is False
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")
    locked = g.git_activity(repo)
    assert locked["busy"] and locked["locks"][0]["file"] == "index.lock"
    assert "lock index.lock" in g.render_git_activity(locked)
    os.remove(repo / ".git" / "index.lock")
    none = g.git_activity(repo, proc_root=str(tmp_path / "no-proc"))
    assert none["processes"] is None and "not observable" in g.render_git_activity(none)
    for bad in (["zz"], "abc"):
        with pytest.raises((ValueError, TypeError)):
            g.git_activity(repo, bad)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc process scan is Linux-only")
def test_git_activity_sees_a_running_git_process(repo):
    child = subprocess.Popen(["git", "hash-object", "--stdin"], cwd=repo / ".git", stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL)
    try:
        out = g.git_activity(repo)
        rows = [p for p in out["processes"] if p["pid"] == child.pid]
        assert out["busy"] and rows and rows[0]["argv"][1:] == ["hash-object", "--stdin"]
        assert rows[0]["elapsed_seconds"] >= 0
        assert f"pid {child.pid}" in g.render_git_activity(out)
    finally:
        child.communicate(b"x")


class Remote:
    """In-memory Git Data API for one branch."""

    def __init__(self, files):
        self.blobs = {g._blob_sha(v): v for v in files.values()}
        self.trees = {"t0": {p: g._blob_sha(v) for p, v in files.items()}}
        self.commits = {"c0": {"tree": "t0", "parents": []}}
        self.head = "c0"
        self.calls = []
        self.fail_patch = False
        self.truncated = False
        self.bad_blob = False

    def __call__(self, method, path, payload=None, **_):
        self.calls.append((method, path))
        if method == "GET" and "/git/ref/heads/" in path:
            return {"object": {"sha": self.head}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": self.commits[path.rsplit("/", 1)[1]]["tree"]}}
        if method == "GET" and "/git/trees/" in path:
            sha = path.rsplit("/", 1)[1].split("?")[0]
            return {"truncated": self.truncated, "tree": [{"path": p, "type": "blob", "mode": "100644", "sha": s}
                                                          for p, s in self.trees[sha].items()]}
        if method == "POST" and path.endswith("/git/blobs"):
            import base64
            data = base64.b64decode(payload["content"])
            sha = "0" * 40 if self.bad_blob else g._blob_sha(data)
            self.blobs[sha] = data
            return {"sha": sha}
        if method == "POST" and path.endswith("/git/trees"):
            tree = dict(self.trees[payload["base_tree"]])
            for e in payload["tree"]:
                if e["sha"] is None:
                    tree.pop(e["path"], None)
                else:
                    tree[e["path"]] = e["sha"]
            name = f"t{len(self.trees)}"
            self.trees[name] = tree
            return {"sha": name}
        if method == "POST" and path.endswith("/git/commits"):
            name = f"c{len(self.commits)}"
            self.commits[name] = {"tree": payload["tree"], "parents": payload["parents"]}
            return {"sha": name}
        if method == "PATCH":
            if self.fail_patch:
                raise g.Error("not_fast_forward")
            assert payload["force"] is False
            self.head = payload["sha"]
            return {}
        raise AssertionError((method, path))


def test_commit_files_plan_write_and_verify():
    remote = Remote({"keep.txt": b"same", "old.txt": b"bye", "edit.txt": b"v1"})
    files = {"keep.txt": b"same", "edit.txt": b"v2", "dir/new.txt": b"hi"}
    with g.request_stub(remote):
        plan = g.commit_files("o/r", "feat", files, "msg", deletes=["old.txt", "never.txt"])
        assert plan["status"] == "planned" and remote.head == "c0"
        assert {p["path"]: p["change"] for p in plan["plan"]} == {
            "dir/new.txt": "new", "edit.txt": "changed", "keep.txt": "unchanged", "old.txt": "delete", "never.txt": "absent"}
        assert not any(m != "GET" for m, _ in remote.calls)
        done = g.commit_files("o/r", "feat", files, "msg", deletes=["old.txt"], expect_head="c0", write=True)
    assert done["status"] == "verified" and remote.head == done["commit"]
    tree = remote.trees[remote.commits[remote.head]["tree"]]
    assert tree == {"keep.txt": g._blob_sha(b"same"), "edit.txt": g._blob_sha(b"v2"), "dir/new.txt": g._blob_sha(b"hi")}
    assert sum(1 for m, p in remote.calls if m == "POST" and p.endswith("/git/blobs")) == 2  # unchanged not re-uploaded
    assert remote.commits[remote.head]["parents"] == ["c0"]
    assert "verified o/r@feat head c0" in g.render_commit_files(done)


def test_commit_files_refusals_and_failures():
    remote = Remote({"a.txt": b"x"})
    with g.request_stub(remote):
        assert g.commit_files("o/r", "b", {"a.txt": b"x"}, "m")["status"] == "already_satisfied"
        assert g.commit_files("o/r", "b", {"a.txt": b"y"}, "m", expect_head="ffff")["status"] == "head_moved"
        remote.fail_patch = True
        failed = g.commit_files("o/r", "b", {"a.txt": b"y"}, "m", write=True)
        assert failed["status"] == "ref_update_failed" and failed["error"] == "not_fast_forward" and remote.head == "c0"
        remote.fail_patch, remote.truncated = False, True
        unknown = g.commit_files("o/r", "b", {"a.txt": b"y"}, "m", deletes=["z"])
        assert {p["change"] for p in unknown["plan"]} == {"unknown"}
        remote.truncated, remote.bad_blob = False, True
        assert g.commit_files("o/r", "b", {"a.txt": b"y"}, "m", write=True)["status"] == "verification_mismatch"


@pytest.mark.parametrize("kwargs", [
    {"branch": ""}, {"branch": "-x"}, {"message": " "}, {"files": {}}, {"files": {"../x": b""}},
    {"files": {"/x": b""}}, {"files": {".git/config": b""}}, {"files": {"a\\b": b""}},
    {"files": {"a": b""}, "deletes": ["a"]}, {"files": {"a": "text"}},
    {"files": {f"f{i}": b"" for i in range(101)}},
])
def test_commit_files_validation(kwargs):
    args = {"branch": "b", "files": {"a.txt": b"x"}, "message": "m", **kwargs}
    with g.request_stub(Remote({})), pytest.raises((ValueError, TypeError)):
        g.commit_files("o/r", args.pop("branch"), args.pop("files"), args.pop("message"), **args)


def test_cli(repo, tmp_path, capsys, monkeypatch):
    assert g.main(["merge-trial", str(repo), "clash", "--text"]) == 1
    assert capsys.readouterr().out.startswith("conflict clash@")
    assert g.main(["merge-trial", str(repo), "clean"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"
    assert g.main(["merge-trial", str(repo), "0" * 40]) == 3
    capsys.readouterr()
    assert g.main(["git-activity", str(repo), "--text"]) in (0, 3)
    assert capsys.readouterr().out.split()[0] in ("idle", "busy")
    local = tmp_path / "local.txt"
    local.write_bytes(b"new")
    monkeypatch.chdir(tmp_path)
    remote = Remote({"a.txt": b"x"})
    with g.request_stub(remote):
        assert g.main(["commit-files", "o/r", "--branch", "b", "--file", f"a.txt={local}", "--file", "local.txt",
                       "--message", "m", "--text"]) == 0
        assert capsys.readouterr().out.startswith("planned o/r@b")
        assert g.main(["commit-files", "o/r", "--branch", "b", "--file", "local.txt", "--message", "m",
                       "--expect-head", "zz"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "head_moved"
