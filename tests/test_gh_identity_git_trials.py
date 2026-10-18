"""merge-trial and git-activity: local and read-only."""
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


def test_cli(repo, capsys):
    assert g.main(["merge-trial", str(repo), "clash", "--text"]) == 1
    assert capsys.readouterr().out.startswith("conflict clash@")
    assert g.main(["merge-trial", str(repo), "clean"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"
    assert g.main(["merge-trial", str(repo), "0" * 40]) == 3
    capsys.readouterr()
    assert g.main(["git-activity", str(repo), "--text"]) in (0, 3)
    assert capsys.readouterr().out.split()[0] in ("idle", "busy")
