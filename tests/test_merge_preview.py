"""merge-preview: snapshot PR commits in a temporary repository, even when refs move."""
import json
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


def fake(remote):
    heads = {n: _git(remote, "rev-parse", f"refs/pull/{n}/head") for n in (1, 2, 3)}

    def request(method, path, payload=None, **_):
        assert method == "GET"
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if path.startswith("repos/o/r/pulls?"):
            return [{"number": 1}, {"number": 2}, {"number": 3}]
        number = int(path.rsplit("/", 1)[1])
        return {"title": f"pr {number}", "state": "open", "base": {"ref": "main"}, "head": {"sha": heads[number]}}

    return request


def test_sequence_alone_and_pairs(remote):
    with g.request_stub(fake(remote)):
        out = g.merge_preview("o/r", all_open=True, remote=str(remote))
    assert [a["status"] for a in out["alone"]] == ["clean", "clean", "clean"]
    assert [(s["number"], s["status"], s["files"]) for s in out["sequence"]] == [
        (1, "clean", []), (2, "conflict_skipped", ["a.txt"]), (3, "clean", [])]
    conflicts = [(p["a"], p["b"], p["files"]) for p in out["pairs"] if p["status"] == "conflict"]
    assert conflicts == [(1, 2, ["a.txt"])] and out["ok"] is False
    text = g.render_merge_preview(out)
    assert "conflicts" in text and "conflict_skipped #2 a.txt" in " ".join(text.split()) and "#1 x #2 a.txt" in text


def test_clean_subset_and_validation(remote):
    with g.request_stub(fake(remote)):
        out = g.merge_preview("o/r", [1, 3], pairs=False, remote=str(remote))
        assert out["ok"] and out["pairs"] == [] and len(out["base_sha"]) == 40
        with pytest.raises(ValueError):
            g.merge_preview("o/r", [1], all_open=True, remote=str(remote))
        with pytest.raises(ValueError):
            g.merge_preview("o/r", [0], remote=str(remote))
        with pytest.raises(ValueError):
            g.merge_preview("o/r", list(range(1, 20)), remote=str(remote))


@pytest.mark.parametrize("update", ["push", "force-push"])
@pytest.mark.parametrize("timing", ["before-fetch", "after-fetch"])
def test_preview_keeps_api_snapshot_when_pr_updates(remote, monkeypatch, update, timing):
    api = fake(remote)
    with g.request_stub(api):
        expected = g.merge_preview("o/r", [1, 2, 3], remote=str(remote))
    old = expected["prs"][0]["head_sha"]
    tree = _git(remote, "rev-parse", "pr3^{tree}")
    parent = old if update == "push" else _git(remote, "rev-parse", "main")
    new = _git(remote, "commit-tree", tree, "-p", parent, "-m", update)
    original = g._git_run
    fetches = []

    def run(root, args, **kwargs):
        if args[0] == "fetch" and kwargs.get("inherit_auth"):
            fetches.append(args)
            if timing == "before-fetch":
                _git(remote, "update-ref", "refs/pull/1/head", new)
            result = original(root, args, **kwargs)
            if timing == "after-fetch":
                _git(remote, "update-ref", "refs/pull/1/head", new)
            return result
        return original(root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", run)
    with g.request_stub(api):
        out = g.merge_preview("o/r", [1, 2, 3], remote=str(remote))
    assert len(fetches) == 1
    assert _git(remote, "rev-parse", "refs/pull/1/head") == new != old
    for key in ("prs", "base_sha", "alone", "sequence", "pairs", "ok"):
        assert out[key] == expected[key]


@pytest.mark.parametrize("text", [False, True])
def test_fetched_head_mismatch_aborts_before_any_comparison(remote, monkeypatch, capsys, text):
    original_run, original_preview = g._git_run, g.merge_preview
    comparisons = []

    def run(root, args, **kwargs):
        if args[0] in ("merge-tree", "commit-tree"):
            comparisons.append(args)
        result = original_run(root, args, **kwargs)
        if args[0] == "fetch" and kwargs.get("inherit_auth"):
            # Simulate a ref moving during retrieval to another fetched PR commit.
            other = _git(root, "rev-parse", "refs/pr/3")
            _git(root, "update-ref", "refs/pr/1", other)
        return result

    monkeypatch.setattr(g, "_git_run", run)
    monkeypatch.setattr(g, "merge_preview", lambda *a, **kw: original_preview(*a, **kw, remote=str(remote)))
    with g.request_stub(fake(remote)):
        code = g.main(["merge-preview", "o/r", "--all-open"] + (["--text"] if text else []))
    captured = capsys.readouterr()
    assert code == 2 and not captured.out
    assert json.loads(captured.err) == {"status": "error", "error": "pr_head_sha_mismatch"}
    assert comparisons == []


def test_all_modes_and_merge_parents_use_verified_shas(remote, monkeypatch):
    refs_before = _git(remote, "show-ref")
    with g.request_stub(fake(remote)):
        expected = g.merge_preview("o/r", [1, 2, 3], remote=str(remote))
    heads = [p["head_sha"] for p in expected["prs"]]
    original = g._git_run
    merges, commits = [], []

    def run(root, args, **kwargs):
        if args[0] == "merge-tree":
            if not merges:
                # Even a ref change after verification must not change the inputs.
                _git(root, "update-ref", "refs/pr/1", heads[2])
            merges.append(args[-2:])
        if args[0] == "commit-tree":
            commits.append(args)
        return original(root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", run)
    with g.request_stub(fake(remote)):
        out = g.merge_preview("o/r", [1, 2, 3], remote=str(remote))
    assert [m[1] for m in merges[:3]] == heads  # alone
    assert [m[1] for m in merges[3:6]] == heads  # sequence
    assert merges[6:] == [[heads[0], heads[1]], [heads[0], heads[2]], [heads[1], heads[2]]]
    assert [c[5] for c in commits] == [heads[0], heads[2]]
    for key in ("prs", "alone", "sequence", "pairs", "ok"):
        assert out[key] == expected[key]
    assert _git(remote, "show-ref") == refs_before


@pytest.mark.parametrize("sha", [None, "", "a" * 39, "z" * 40, "refs/pull/1/head"])
def test_invalid_api_sha_is_rejected_before_fetch(remote, monkeypatch, sha):
    api = fake(remote)

    def invalid(method, path, **kwargs):
        result = api(method, path, **kwargs)
        if path == "repos/o/r/pulls/1":
            result["head"]["sha"] = sha
        return result

    def no_git(*args, **kwargs):
        pytest.fail("invalid API commit must not reach Git")

    monkeypatch.setattr(g, "_git_run", no_git)
    with g.request_stub(invalid), pytest.raises(g.Error, match="^invalid_pr_head_sha$"):
        g.merge_preview("o/r", [1], remote=str(remote))


def test_unavailable_snapshot_fails_without_fetching_moving_ref(remote, monkeypatch, capsys):
    api = fake(remote)
    old = _git(remote, "rev-parse", "refs/pull/1/head")
    # A force-push followed by pruning can make the API snapshot unavailable.
    _git(remote, "update-ref", "refs/pull/1/head", "refs/heads/pr3")
    _git(remote, "update-ref", "-d", "refs/heads/pr1")
    _git(remote, "reflog", "expire", "--expire=now", "--all")
    _git(remote, "gc", "--prune=now")
    assert subprocess.run(["git", "-C", str(remote), "cat-file", "-e", old], capture_output=True).returncode != 0
    original_run, original_preview = g._git_run, g.merge_preview
    fetches = []

    def run(root, args, **kwargs):
        assert args[0] not in ("merge-tree", "commit-tree")
        if args[0] == "fetch" and kwargs.get("inherit_auth"):
            fetches.append(args)
        return original_run(root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", run)
    monkeypatch.setattr(g, "merge_preview", lambda *a, **kw: original_preview(*a, **kw, remote=str(remote)))
    with g.request_stub(api):
        code = g.main(["merge-preview", "o/r", "--pr", "1"])
    captured = capsys.readouterr()
    assert code == 2 and not captured.out
    assert json.loads(captured.err) == {"status": "error", "error": "git_inspection_failed"}
    assert len(fetches) == 1
    assert all("refs/pull/" not in arg for arg in fetches[0])


@pytest.mark.parametrize(("numbers", "code"), [([1, 3], 0), ([1, 2, 3], 1)])
def test_cli_json_and_exit_contract(remote, monkeypatch, capsys, numbers, code):
    original = g.merge_preview
    monkeypatch.setattr(g, "merge_preview", lambda *a, **kw: original(*a, **kw, remote=str(remote)))
    with g.request_stub(fake(remote)):
        actual = g.main(["merge-preview", "o/r"] + [arg for n in numbers for arg in ("--pr", str(n))])
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert actual == code and not captured.err
    assert out["schema"] == "gh-identity-merge-preview/1" and out["write"] is False
    assert out["ok"] is (code == 0)
