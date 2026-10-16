"""merge-preview: alone / sequence / pairs from refs/pull/N/head in a temporary repository."""
import subprocess

import pytest
import gh_identity as g


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@e", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """main with a.txt/b.txt; PR 1 edits a (line 1), PR 2 edits a (same line), PR 3 edits b."""
    seed, bare = tmp_path / "seed", tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "a.txt").write_text("one\ntwo\n")
    (seed / "b.txt").write_text("x\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")
    for number, path, text in ((1, "a.txt", "ONE\ntwo\n"), (2, "a.txt", "uno\ntwo\n"), (3, "b.txt", "y\n")):
        _git(seed, "switch", "-q", "-c", f"pr{number}", "main")
        (seed / path).write_text(text)
        _git(seed, "commit", "-q", "-am", f"pr {number}")
    _git(seed, "switch", "-q", "main")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    for number in (1, 2, 3):
        _git(bare, "update-ref", f"refs/pull/{number}/head", f"refs/heads/pr{number}")
    return bare


def fake(method, path, payload=None, **_):
    if path == "repos/o/r":
        return {"default_branch": "main"}
    if path.startswith("repos/o/r/pulls?"):
        return [{"number": 1}, {"number": 2}, {"number": 3}]
    number = int(path.rsplit("/", 1)[1])
    return {"title": f"pr {number}", "state": "open", "base": {"ref": "main"}, "head": {"sha": "0" * 40}}


def test_sequence_alone_and_pairs(remote):
    with g.request_stub(fake):
        out = g.merge_preview("o/r", all_open=True, remote=str(remote))
    assert [a["status"] for a in out["alone"]] == ["clean", "clean", "clean"]
    assert [(s["number"], s["status"], s["files"]) for s in out["sequence"]] == [
        (1, "clean", []), (2, "conflict_skipped", ["a.txt"]), (3, "clean", [])]
    conflicts = [(p["a"], p["b"], p["files"]) for p in out["pairs"] if p["status"] == "conflict"]
    assert conflicts == [(1, 2, ["a.txt"])] and out["ok"] is False
    text = g.render_merge_preview(out)
    assert "conflicts" in text and "conflict_skipped #2 a.txt" in " ".join(text.split()) and "#1 x #2 a.txt" in text


def test_clean_subset_and_validation(remote):
    with g.request_stub(fake):
        out = g.merge_preview("o/r", [1, 3], pairs=False, remote=str(remote))
        assert out["ok"] and out["pairs"] == [] and len(out["base_sha"]) == 40
        with pytest.raises(ValueError):
            g.merge_preview("o/r", [1], all_open=True, remote=str(remote))
        with pytest.raises(ValueError):
            g.merge_preview("o/r", [0], remote=str(remote))
        with pytest.raises(ValueError):
            g.merge_preview("o/r", list(range(1, 20)), remote=str(remote))
