"""🔀 Pull requests: status, checks, reviews and comments, merge preview and readiness, labels, write receipts.

Sections (each was one file; `-k` selects inside them): pr_checks, pr_status, merge_preview, merge_readiness, merge_readiness_mutations, review_observations, comment_shortcuts, read_observations, wait_checks, checks_text, get_and_lists, labels_write, write_receipts.
"""

from pathlib import Path
from unittest.mock import patch
import contextlib
import copy
import io
import json
import os
import subprocess
import sys

import pytest

from conftest import FIXTURES, ROOT, SHA, _git_tester

import gh_identity as g


# ──── pr_checks ───────────────────────────────────────────────────

# pr-checks: several PR heads polled together, Copilot checks dropped, per-request budgets.


def run(name, status="completed", conclusion="success"):
    return {"id": hash(name) & 0xFFFF, "name": name, "status": status, "conclusion": conclusion}


def fake_checks(sequence):
    """sequence: {(repo, sha): [check lists per poll]}"""
    calls = []

    def checks(r, sha, min_checks=1, transport="auto", timeout=30):
        calls.append((r, g._BUDGET.get() is None, timeout))
        rows = sequence[r].pop(0) if len(sequence[r]) > 1 else sequence[r][0]
        return {"checks": rows}
    return checks, calls


def call(tokens, sequence, **kw):
    checks, calls = fake_checks(sequence)
    sleeps = []
    with patch.object(g, "pr", side_effect=lambda r, n, **k: {"head_sha": SHA}), \
            patch.object(g, "checks_for_sha", side_effect=checks):
        o = g.pr_checks(tokens, sleep=sleeps.append, clock=lambda: sum(sleeps), **kw)
    return o, calls, sleeps


def test_owner_shorthand_and_full_targets():
    assert g.parse_pr_check_targets(["Aoi#67", "o/r#2", "Ironmate", "123"], owner="o") == [
        ("o/Aoi", 67), ("o/r", 2), ("o/Ironmate", 123)]
    with pytest.raises(ValueError):
        g.parse_pr_check_targets(["Aoi#67"])


def test_copilot_ignored_and_attention_listed():
    seq = {"o/a": [[run("test"), run("lint", conclusion="skipped"), run("Copilot review", conclusion="failure")]],
           "o/b": [[run("test"), run("build", conclusion="failure")]]}
    o, _, _ = call(["o/a#1", "o/b#2"], seq)
    a, b = o["rows"]
    assert a["state"] == "green" and a["total"] == 2 and a["ignored"] == 1
    assert a["by_conclusion"] == {"skipped": 1, "success": 1}
    assert b["state"] == "failed" and b["attention"] == [{"status": "completed", "conclusion": "failure", "name": "build"}]
    assert o["state"] == "failed"
    text = g.render_pr_checks(o)
    assert "## o/a #1 green total=2 1 skipped 1 success" in text and "completed failure build" in text


def test_wait_polls_only_pending_and_each_request_bounded():
    seq = {"o/a": [[run("test")]],
           "o/b": [[], [run("test", "in_progress", None)], [run("test")]]}
    o, calls, sleeps = call(["o/a#1", "o/b#2"], seq, wait=600, interval=30, delay=120)
    assert o["state"] == "green" and o["polls"] == 3
    assert [r for r, _, _ in calls] == ["o/a", "o/b", "o/b", "o/b"]
    assert all(unscoped for _, unscoped, _ in calls)
    assert sleeps == [120, 30, 30]


def test_timeout_leaves_pending_and_cli_exit_codes():
    o, _, _ = call(["o/a#1"], {"o/a": [[run("test", "queued", None)]]}, wait=60, interval=30)
    assert o["state"] == "pending" and o["polls"] == 3
    with patch.object(g, "pr_checks", return_value={"state": "pending", "rows": []}) as m:
        assert g.main(["pr-checks", "o/a#1", "--wait", "900", "--timeout", "45"]) == 3
        assert m.call_args.kwargs["timeout"] == 45.0
    assert g.main(["pr-checks", "o/a#1", "--max-pages", "3"]) == 2


# ──── pr_status ───────────────────────────────────────────────────

# pr-status: one compact view of a PR head (observation, not a gate).


SHA_B = "b" * 40


@pytest.fixture
def fake(monkeypatch):
    calls = {}
    monkeypatch.setattr(g, "request", lambda method, path, **k: {
        "number": int(path.rsplit("/", 1)[1]), "repository_url": g.API + "/repos/o/r", "state": "open",
        "title": "T", "html_url": "https://github.com/o/r/pull/" + path.rsplit("/", 1)[1],
        "user": {"login": "u"}, "pull_request": {}})
    monkeypatch.setattr(g, "pr", lambda r, n, **k: {"state": "open", "draft": True, "merged": False,
        "mergeable_state": "unstable", "head_sha": SHA_B, "head_ref": "work/x", "base_ref": "main", "url": "u"})

    def runs(r, head_sha=None, exclude_events=(), **k):
        calls["runs"] = (head_sha, exclude_events)
        return {"state": "pending", "by_conclusion": {"success": 1, "in_progress": 1}, "excluded_count": 1,
                "runs": [{"name": "Flutter", "event": "pull_request", "run_id": 1, "conclusion": None, "status": "in_progress"},
                         {"name": "Lint", "event": "push", "run_id": 2, "conclusion": "success", "status": "completed"}]}
    monkeypatch.setattr(g, "run_history", runs)

    def checks(r, sha, min_checks=1, transport="auto", timeout=30):
        calls["checks"] = sha
        rows = [{"name": n, "status": s, "conclusion": c, "html_url": "c/" + n}
                for n, s, c in (("ok", "completed", "success"), ("skip", "completed", "skipped"),
                                ("ios", "in_progress", None), ("bot", "completed", "failure"))]
        return {**g.summarize_checks(rows, len(rows))}
    monkeypatch.setattr(g, "checks_for_sha", checks)
    monkeypatch.setattr(g, "comments", lambda r, n, **k: {"comments": [
        {"author": f"u{i}", "created_at": f"t{i}", "preview": f"comment {i}", "url": f"c{i}"} for i in range(5)]})
    monkeypatch.setattr(g, "reviews", lambda r, n, **k: {"reviews": [
        {"author": "gpt", "state": "COMMENTED", "submitted_at": "t9", "preview": "Should: 1", "url": "r1"}]})
    return calls


def test_observes_the_pr_head_and_lists_only_what_needs_attention(fake):
    out = g.pr_status("o/r", 7, last=2)
    assert fake == {"runs": (SHA_B, ("dynamic",)), "checks": SHA_B}
    assert out["pr"]["mergeable_state"] == "unstable" and out["runs"]["excluded_count"] == 1
    assert [x["name"] for x in out["checks"]["attention"]] == ["ios", "bot"]
    assert [x["author"] for x in out["comments"]] == ["u3", "u4"]
    assert out["reviews"] == [{"author": "gpt", "state": "COMMENTED", "submitted_at": "t9", "preview": "Should: 1", "url": "r1"}]


def test_text_rendering_is_one_screen(fake):
    text = g.render_pr_status(g.pr_status("o/r", 7, last=1))
    lines = text.splitlines()
    assert lines[0] == "#7 open draft merged=False unstable head=bbbbbbbb work/x -> main"
    assert "  Flutter pull_request 1 in_progress" in lines and "  ! bot failure" in lines
    assert lines[-1].startswith("review gpt t9 COMMENTED: Should: 1")


@pytest.mark.parametrize("last", [-1, 21, True])
def test_last_is_bounded(fake, last):
    with pytest.raises(ValueError):
        g.pr_status("o/r", 7, last=last)


def test_cli_json_text_and_exclude_override(fake, capsys):
    assert g.main(["pr-status", "o/r", "7"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "gh-identity-pr-status/1"
    assert g.main(["pr-status", "o/r", "7", "--text", "--exclude-event", "workflow_dispatch"]) == 0
    assert capsys.readouterr().out.startswith("o/r#7 T\n#7 open draft")
    assert fake["runs"] == (SHA_B, ("workflow_dispatch",))


@pytest.mark.parametrize("tokens,expected", [
    (["o/r", "7"], [("o/r", 7)]),
    (["o/r", "7", "8", "x/y", "1"], [("o/r", 7), ("o/r", 8), ("x/y", 1)]),
    (["o/r#7", "x/y#2"], [("o/r", 7), ("x/y", 2)]),
])
def test_targets_mix_repositories_and_numbers(tokens, expected):
    assert g.parse_status_targets(tokens) == expected


@pytest.mark.parametrize("tokens", [["7"], ["o/r"], ["o/r#x"], ["o/r", *map(str, range(1, 22))]])
def test_bad_targets(tokens):
    with pytest.raises(ValueError):
        g.parse_status_targets(tokens)


def test_many_mixes_pr_issue_and_errors(fake, monkeypatch):
    def issue_endpoint(method, path, **k):
        n = int(path.rsplit("/", 1)[1])
        if n == 404:
            raise g.Error("not_found")
        row = {"number": n, "repository_url": g.API + "/repos/o/r", "state": "open", "title": f"T{n}",
               "html_url": f"https://github.com/o/r/{'pull' if n == 7 else 'issues'}/{n}", "user": {"login": "u"},
               "updated_at": "t"}
        return {**row, "pull_request": {}} if n == 7 else row
    monkeypatch.setattr(g, "request", issue_endpoint)
    out = g.status_many(["o/r", "7", "9", "404"], last=1)
    kinds = [(i["number"], i.get("kind"), i.get("error")) for i in out["items"]]
    assert kinds == [(7, "pr", None), (9, "issue", None), (404, None, "not_found")]
    assert out["items"][0]["title"] == "T7" and out["items"][1]["comments"][0]["author"] == "u4"
    text = g.render_status(out)
    assert "o/r#9 issue open updated=t T9" in text and "o/r#404: error not_found" in text


def test_labels_are_reported_for_issues_and_prs(fake, monkeypatch):
    def endpoint(method, path, **k):
        n = int(path.rsplit("/", 1)[1])
        row = {"number": n, "repository_url": g.API + "/repos/o/r", "state": "open", "title": "T",
               "html_url": f"https://github.com/o/r/{'pull' if n == 7 else 'issues'}/{n}", "user": {"login": "u"},
               "labels": [{"name": "type:tracking"}, {"name": "priority:high"}, "junk", {"name": 3}]}
        return {**row, "pull_request": {}} if n == 7 else row
    monkeypatch.setattr(g, "request", endpoint)
    out = g.status_many(["o/r", "7", "9"], last=0)
    assert [i["labels"] for i in out["items"]] == [["priority:high", "type:tracking"]] * 2
    assert g.render_status(out).count("labels: priority:high, type:tracking") == 2


# ──── merge_preview ───────────────────────────────────────────────

# merge-preview: snapshot PR commits in a temporary repository, even when refs move.


@pytest.fixture
def remote(tmp_path):
    """main with a.txt/b.txt; PR 1 edits a (line 1), PR 2 edits a (same line), PR 3 edits b."""
    seed, bare = tmp_path / "seed", tmp_path / "remote.git"
    _git_tester(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "a.txt").write_text("one\ntwo\n")
    (seed / "b.txt").write_text("x\n")
    _git_tester(seed, "add", ".")
    _git_tester(seed, "commit", "-q", "-m", "base")
    for number, path, text in ((1, "a.txt", "ONE\ntwo\n"), (2, "a.txt", "uno\ntwo\n"), (3, "b.txt", "y\n")):
        _git_tester(seed, "switch", "-q", "-c", f"pr{number}", "main")
        (seed / path).write_text(text)
        _git_tester(seed, "commit", "-q", "-am", f"pr {number}")
    _git_tester(seed, "switch", "-q", "main")
    _git_tester(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    for number in (1, 2, 3):
        _git_tester(bare, "update-ref", f"refs/pull/{number}/head", f"refs/heads/pr{number}")
    return bare


def fake_merge_preview(remote):
    heads = {n: _git_tester(remote, "rev-parse", f"refs/pull/{n}/head") for n in (1, 2, 3)}

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
    with g.request_stub(fake_merge_preview(remote)):
        out = g.merge_preview("o/r", all_open=True, remote=str(remote))
    assert [a["status"] for a in out["alone"]] == ["clean", "clean", "clean"]
    assert [(s["number"], s["status"], s["files"]) for s in out["sequence"]] == [
        (1, "clean", []), (2, "conflict_skipped", ["a.txt"]), (3, "clean", [])]
    conflicts = [(p["a"], p["b"], p["files"]) for p in out["pairs"] if p["status"] == "conflict"]
    assert conflicts == [(1, 2, ["a.txt"])] and out["ok"] is False
    text = g.render_merge_preview(out)
    assert "conflicts" in text and "conflict_skipped #2 a.txt" in " ".join(text.split()) and "#1 x #2 a.txt" in text


def test_clean_subset_and_validation(remote):
    with g.request_stub(fake_merge_preview(remote)):
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
    api = fake_merge_preview(remote)
    with g.request_stub(api):
        expected = g.merge_preview("o/r", [1, 2, 3], remote=str(remote))
    old = expected["prs"][0]["head_sha"]
    tree = _git_tester(remote, "rev-parse", "pr3^{tree}")
    parent = old if update == "push" else _git_tester(remote, "rev-parse", "main")
    new = _git_tester(remote, "commit-tree", tree, "-p", parent, "-m", update)
    original = g._git_run
    fetches = []

    def run(root, args, **kwargs):
        if args[0] == "fetch" and kwargs.get("inherit_auth"):
            fetches.append(args)
            if timing == "before-fetch":
                _git_tester(remote, "update-ref", "refs/pull/1/head", new)
            result = original(root, args, **kwargs)
            if timing == "after-fetch":
                _git_tester(remote, "update-ref", "refs/pull/1/head", new)
            return result
        return original(root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", run)
    with g.request_stub(api):
        out = g.merge_preview("o/r", [1, 2, 3], remote=str(remote))
    assert len(fetches) == 1
    assert _git_tester(remote, "rev-parse", "refs/pull/1/head") == new != old
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
            other = _git_tester(root, "rev-parse", "refs/pr/3")
            _git_tester(root, "update-ref", "refs/pr/1", other)
        return result

    monkeypatch.setattr(g, "_git_run", run)
    monkeypatch.setattr(g, "merge_preview", lambda *a, **kw: original_preview(*a, **kw, remote=str(remote)))
    with g.request_stub(fake_merge_preview(remote)):
        code = g.main(["merge-preview", "o/r", "--all-open"] + (["--text"] if text else []))
    captured = capsys.readouterr()
    assert code == 2 and not captured.out
    assert json.loads(captured.err) == {"status": "error", "error": "pr_head_sha_mismatch"}
    assert comparisons == []


def test_all_modes_and_merge_parents_use_verified_shas(remote, monkeypatch):
    refs_before = _git_tester(remote, "show-ref")
    with g.request_stub(fake_merge_preview(remote)):
        expected = g.merge_preview("o/r", [1, 2, 3], remote=str(remote))
    heads = [p["head_sha"] for p in expected["prs"]]
    original = g._git_run
    merges, commits = [], []

    def run(root, args, **kwargs):
        if args[0] == "merge-tree":
            if not merges:
                # Even a ref change after verification must not change the inputs.
                _git_tester(root, "update-ref", "refs/pr/1", heads[2])
            merges.append(args[-2:])
        if args[0] == "commit-tree":
            commits.append(args)
        return original(root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", run)
    with g.request_stub(fake_merge_preview(remote)):
        out = g.merge_preview("o/r", [1, 2, 3], remote=str(remote))
    assert [m[1] for m in merges[:3]] == heads  # alone
    assert [m[1] for m in merges[3:6]] == heads  # sequence
    assert merges[6:] == [[heads[0], heads[1]], [heads[0], heads[2]], [heads[1], heads[2]]]
    assert [c[5] for c in commits] == [heads[0], heads[2]]
    for key in ("prs", "alone", "sequence", "pairs", "ok"):
        assert out[key] == expected[key]
    assert _git_tester(remote, "show-ref") == refs_before


@pytest.mark.parametrize("sha", [None, "", "a" * 39, "z" * 40, "refs/pull/1/head"])
def test_invalid_api_sha_is_rejected_before_fetch(remote, monkeypatch, sha):
    api = fake_merge_preview(remote)

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
    api = fake_merge_preview(remote)
    old = _git_tester(remote, "rev-parse", "refs/pull/1/head")
    # A force-push followed by pruning can make the API snapshot unavailable.
    _git_tester(remote, "update-ref", "refs/pull/1/head", "refs/heads/pr3")
    _git_tester(remote, "update-ref", "-d", "refs/heads/pr1")
    _git_tester(remote, "reflog", "expire", "--expire=now", "--all")
    _git_tester(remote, "gc", "--prune=now")
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
    with g.request_stub(fake_merge_preview(remote)):
        actual = g.main(["merge-preview", "o/r"] + [arg for n in numbers for arg in ("--pr", str(n))])
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert actual == code and not captured.err
    assert out["schema"] == "gh-identity-merge-preview/1" and out["write"] is False
    assert out["ok"] is (code == 0)


# ──── merge_readiness ─────────────────────────────────

# merge-readiness: offline, with pr / review_observations / request patched.


HEAD = "b" * 40


OLD = "a" * 40


def head(**over):
    row = {"state": "open", "draft": False, "merged": False, "mergeable_state": "clean", "head_sha": HEAD,
           "head_ref": "feat/x", "base_ref": "main", "url": "https://github.com/o/r/pull/7"}
    row.update(over)
    return row


def body(text):
    return {"severity_labels": g._explicit_severity(text), "preview": text[:240]}


def observations(*, reviews=(), conversation=(), inline=(), threads=(), thread_error=None):
    components = {k: {"complete": True, "truncated": False, "limit_reason": None}
                  for k in ("conversation", "review_submission", "inline_comment", "review_thread")}
    if thread_error:
        components["review_thread"] = {"complete": False, "truncated": False, "error": thread_error}
    return {"review_submission": list(reviews), "conversation": list(conversation), "inline_comment": list(inline),
            "review_thread": list(threads), "components": components, "complete": not thread_error}


def review(author, state, at, commit=OLD, text="", rid=1):
    return {"id": rid, "author": author, "state": state, "submitted_at": at, "commit_id": commit,
            "url": f"https://github.com/o/r/pull/7#pullrequestreview-{rid}", **body(text)}


def api(extra=None):
    def handler(method, path, payload=None, **_):
        if extra and path in extra:
            value = extra[path]
            if isinstance(value, Exception):
                raise value
            return value
        if path == f"repos/o/r/commits/{HEAD}":
            return {"commit": {"committer": {"date": "2026-10-10T00:00:00Z"}}}
        if path == f"repos/o/r/compare/{OLD}...{HEAD}":
            return {"ahead_by": 2, "commits": [{"commit": {"message": "fix A\n\nbody"}}, {"commit": {"message": "fix B"}}]}
        raise AssertionError(path)
    return handler


def readiness(obs, extra=None, **over):
    with patch.object(g, "pr", return_value=head(**over)), \
         patch.object(g, "review_observations", return_value=obs), \
         patch.object(g, "request", side_effect=api(extra)):
        return g.merge_readiness("o/r", 7)


def test_should_before_head_is_attention_with_head_move():
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z",
                                       text="from: gpt\n**Should \u2014 re-pin to the final merge commit.**")])
    out = readiness(obs)
    assert out["verdict"] == "attention"
    [finding] = out["findings"]
    assert finding["label"] == "Should" and not finding["after_head"] and not finding["superseded_by_approval"]
    assert out["head_moved_after_review"] == {"author": "gpt", "reviewed": OLD, "commits": 2, "messages": ["fix A", "fix B"]}
    assert out["reasons"] == ["1 Must/Blocking/Should finding(s) to confirm", "head moved 2 commit(s) after gpt's review"]
    text = g.render_merge_readiness(out)
    assert "Should   review_submission by gpt [code changed since]" in text and "fix A; fix B" in text


def test_must_after_head_and_changes_requested_block():
    obs = observations(reviews=[review("gpt", "CHANGES_REQUESTED", "2026-10-11T00:00:00Z", commit=HEAD,
                                       text="Must: add the test")])
    out = readiness(obs)
    assert out["verdict"] == "blocked" and out["findings"][0]["after_head"] and out["head_moved_after_review"] is None
    assert out["changes_requested"][0]["author"] == "gpt"
    assert "changes requested by gpt at bbbbbbb" in g.render_merge_readiness(out)


def test_must_after_head_blocks_without_changes_requested():
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text="Must: add the test")])
    out = readiness(obs)
    assert out["verdict"] == "blocked" and out["changes_requested"] == []


def test_later_approval_supersedes_and_clear():
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z", text="Blocking: x", rid=1),
                                review("gpt", "APPROVED", "2026-10-11T00:00:00Z", commit=HEAD, rid=2),
                                review("copilot-pull-request-reviewer[bot]", "COMMENTED", "2026-10-12T00:00:00Z", rid=3)])
    out = readiness(obs)
    assert out["findings"][0]["superseded_by_approval"] and out["verdict"] == "clear" and out["reasons"] == []
    assert "[superseded by approval]" in g.render_merge_readiness(out)


def test_approval_timestamp_uses_latest():
    obs = observations(reviews=[
        review("gpt", "APPROVED", "2026-10-08T00:00:00Z", rid=1),
        review("gpt", "APPROVED", "2026-10-12T00:00:00Z", commit=HEAD, rid=2),
        review("gpt", "COMMENTED", "2026-10-10T00:00:00Z", text="Should: fix", rid=3),
    ])
    out = readiness(obs)
    assert out["findings"][0]["superseded_by_approval"]


def test_supersede_requires_approval_after_finding():
    obs = observations(reviews=[
        review("gpt", "APPROVED", "2026-10-10T00:00:00Z", rid=1),
        review("gpt", "COMMENTED", "2026-10-10T00:00:00Z", text="Should: fix", rid=2),
    ])
    out = readiness(obs)
    assert not out["findings"][0]["superseded_by_approval"]


def test_dismissed_does_not_supersede_findings():
    obs = observations(reviews=[
        review("gpt", "COMMENTED", "2026-10-09T00:00:00Z", text="Should: fix", rid=1),
        review("gpt", "DISMISSED", "2026-10-12T00:00:00Z", commit=HEAD, rid=2),
    ])
    out = readiness(obs)
    assert not out["findings"][0]["superseded_by_approval"] and out["verdict"] == "attention"


def test_inline_findings_threads_and_states():
    inline = [{"id": 5, "author": "gpt", "created_at": "2026-10-09T00:00:00Z", "url": "u5", "thread_id": "T1",
               **body("- Blocking: wrong path")},
              {"id": 6, "author": "gpt", "created_at": "2026-10-09T00:00:00Z", "url": "u6", "thread_id": "T2",
               **body("Should: rename")},
              {"id": 7, "author": "gpt", "created_at": "2026-10-09T00:00:00Z", "url": "u7", "thread_id": None,
               **body("Nit: spacing")}]
    threads = [{"id": "T1", "is_resolved": False, "is_outdated": False, "path": "a.py", "line": 3, "url": "u5"},
               {"id": "T2", "is_resolved": True, "is_outdated": True, "path": "b.py", "line": 1, "url": "u6"},
               {"id": "T3", "is_resolved": False, "is_outdated": True, "path": "c.py", "line": 9, "url": "u9"}]
    conversation = [{"id": 8, "author": "me", "created_at": "2026-10-12T00:00:00Z", "url": "u8", **body("Should: later")}]
    out = readiness(observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z", commit=HEAD)],
                                 inline=inline, threads=threads, conversation=conversation),
                    draft=True, mergeable_state="dirty")
    assert out["verdict"] == "blocked"  # an unresolved, current thread carries a Blocking finding
    assert [(f["kind"], f["label"]) for f in out["findings"]] == [
        ("conversation", "Should"), ("inline_comment", "Blocking"), ("inline_comment", "Should")]
    assert out["open_threads"] == [{"path": "a.py", "line": 3, "url": "u5", "outdated": False},
                                   {"path": "c.py", "line": 9, "url": "u9", "outdated": True}]
    assert out["reasons"][:3] == ["draft", "mergeable_state dirty", "2 Must/Blocking/Should finding(s) to confirm"]
    assert "1 unresolved thread(s)" in out["reasons"]
    text = g.render_merge_readiness(out)
    assert "[thread resolved]" in text and "[newer than head]" in text and "open thread (outdated) c.py:9 u9" in text


def test_ccr_thread_fallback_unreviewed_and_merged():
    inline = [{"id": 11, "author": "x", "created_at": "2026-10-09T00:00:00Z", "url": "u11", "thread_id": None,
               **body("ok")}]
    obs = observations(inline=inline, thread_error="permission_or_rate_limit")
    route = "repos/o/r/pulls/7/ccr/review_threads"
    out = readiness(obs, {route: [{"resolved": False, "outdated": False, "path": "p", "line": 2, "comment_ids": [11]}]},
                    state="closed", merged=True)
    assert out["open_threads"] == [{"path": "p", "line": 2, "url": "u11", "outdated": False}]
    assert obs["inline_comment"][0]["thread_id"] == "ccr:11" and out["complete"]
    assert out["reasons"] == ["merged", "1 unresolved thread(s)", "no human review"]
    missing = readiness(observations(thread_error="permission_or_rate_limit"), {route: g.Error("not_found")},
                        state="closed")
    assert missing["reasons"][0] == "PR is closed" and not missing["complete"]
    assert missing["reasons"][-1] == "unobserved: review_thread permission_or_rate_limit"
    odd = readiness(observations(thread_error="graphql_error"), {route: [{"resolved": "no"}]})
    assert odd["open_threads"] == [] and not odd["complete"]
    empty_ids = readiness(observations(thread_error="graphql_error"), {route: [{"resolved": True}]})
    assert empty_ids["complete"] and empty_ids["open_threads"] == []


def test_compare_failure_is_unknown_move():
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z", commit="c" * 40)])
    out = readiness(obs, {f"repos/o/r/compare/{'c' * 40}...{HEAD}": g.Error("not_found")})
    assert out["head_moved_after_review"]["commits"] is None and "head moved ? commit(s)" in out["reasons"][-1]
    assert out["head_moved_after_review"]["messages"] == [] and "review of ccccccc: ?" in g.render_merge_readiness(out)


def test_empty_commit_message_in_head_move():
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z")])
    compare = {"ahead_by": 1, "commits": [{"commit": {"message": ""}}, {"commit": {}}]}
    out = readiness(obs, {f"repos/o/r/compare/{OLD}...{HEAD}": compare})
    assert out["head_moved_after_review"]["messages"] == ["", ""]
    assert "review of aaaaaaa: ; " in g.render_merge_readiness(out)


@pytest.mark.parametrize("text, labels", [
    ("**Should \u2014 re-pin first.**", ["Should"]), ("Must: add tests", ["Must"]),
    ("__Blocking__ \u2013 broken", ["Blocking"]), ("Should - rename", ["Should"]),
    ("Should\uff1a\u65e5\u672c\u8a9e", ["Should"]), ("Should-have list", []), ("Must 1 / Should 2", []),
    ("- Should 1\uff1arename the helper", ["Should"]),
])
def test_severity_labels_accept_dashes_and_must(text, labels):
    assert [x["label"] for x in g._explicit_severity(text)] == labels


def test_empty_blocking_section_is_not_a_finding():
    text = "## Blocking\nなし。\n\n## Should\n1. rename"
    assert [x["label"] for x in g._explicit_severity(text)] == ["Should"]
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text=text)])
    out = readiness(obs)
    assert out["verdict"] == "attention" and out["findings"][0]["label"] == "Should"


def test_bold_blocking_with_nashi_is_not_a_finding():
    text = "**Blocking**\nなし。\n\n**Should**\n1. rename"
    assert [x["label"] for x in g._explicit_severity(text)] == ["Should"]
    out = readiness(observations(reviews=[review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text=text)]))
    assert out["verdict"] == "attention"


def test_codeblock_inside_section_ignored():
    text = "## Should\n1. real\n```\n- Blocking: example\n```"
    assert [x["label"] for x in g._explicit_severity(text)] == ["Should"]
    out = readiness(observations(reviews=[review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text=text)]))
    assert out["verdict"] == "attention" and out["verdict"] != "blocked"


def test_non_severity_heading_ends_section():
    text = "## Blocking\nなし。\n## 確認したこと\n- CI は成功"
    assert g._explicit_severity(text) == []
    out = readiness(observations(reviews=[review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text=text)]))
    assert out["verdict"] == "clear"


def test_nested_bullets_are_one_finding():
    text = "## Should\n- parent\n  - child detail"
    labels = g._explicit_severity(text)
    assert len(labels) == 1 and labels[0]["label"] == "Should" and "child detail" in labels[0]["text"]


def test_section_skips_non_list_prose():
    text = "## Should\nPlease reconsider\n1. fix it"
    assert len(g._explicit_severity(text)) == 1 and g._explicit_severity(text)[0]["text"].startswith("1. fix")


def test_team_review_markdown_shape():
    text = ("## Blocking\nなし。\n## Should\n1. rename\n## Nit\n- typo\n"
            "## 確認したこと\n- CI は成功")
    assert [x["label"] for x in g._explicit_severity(text)] == ["Should", "Nit"]
    out = readiness(observations(reviews=[review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text=text)]))
    assert out["verdict"] == "attention" and len(out["findings"]) == 1 and out["findings"][0]["label"] == "Should"


def test_commented_does_not_supersede():
    obs = observations(reviews=[
        review("gpt", "COMMENTED", "2026-10-09T00:00:00Z", text="Should: fix", rid=1),
        review("gpt", "COMMENTED", "2026-10-12T00:00:00Z", commit=HEAD, rid=2),
    ])
    out = readiness(obs)
    assert not out["findings"][0]["superseded_by_approval"]


def test_outdated_thread_blocking_finding_is_not_blocker():
    inline = [{"id": 5, "author": "gpt", "created_at": "2026-10-09T00:00:00Z", "url": "u5", "thread_id": "T1",
               **body("- Blocking: path")}]
    threads = [{"id": "T1", "is_resolved": False, "is_outdated": True, "path": "a.py", "line": 3, "url": "u5"}]
    out = readiness(observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z", commit=HEAD)],
                                 inline=inline, threads=threads))
    assert out["verdict"] == "attention"


def test_unresolved_blocking_thread_before_head_still_blocks():
    inline = [{"id": 5, "author": "gpt", "created_at": "2026-10-09T00:00:00Z", "url": "u5", "thread_id": "T1",
               **body("- Blocking: path")}]
    threads = [{"id": "T1", "is_resolved": False, "is_outdated": False, "path": "a.py", "line": 3, "url": "u5"}]
    out = readiness(observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z", commit=HEAD)],
                                 inline=inline, threads=threads))
    assert out["verdict"] == "blocked"
    assert not out["findings"][0]["after_head"]


def test_head_message_hundred_char_cap():
    long = "z" * 150
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z")])
    compare = {"ahead_by": 1, "commits": [{"commit": {"message": long}}]}
    out = readiness(obs, {f"repos/o/r/compare/{OLD}...{HEAD}": compare})
    assert len(out["head_moved_after_review"]["messages"][0]) == 100


def test_when_invalid_timestamp():
    assert g._when("not-a-date") is None
    obs = observations(reviews=[review("gpt", "COMMENTED", "not-a-date", commit=HEAD, text="Should: x")])
    out = readiness(obs)
    assert not out["findings"][0]["after_head"] and not out["findings"][0]["superseded_by_approval"]


def test_paired_label_and_text_per_finding():
    text = "Must: one\nShould: two"
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text=text)])
    out = readiness(obs)
    assert [(f["label"], f["text"]) for f in out["findings"]] == [("Must", "Must: one"), ("Should", "Should: two")]


def test_after_head_strictly_after_commit_time():
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-10T00:00:00Z", commit=HEAD, text="Should: x")])
    out = readiness(obs)
    assert not out["findings"][0]["after_head"]


def test_changes_requested_on_old_commit_is_not_blocker():
    obs = observations(reviews=[review("gpt", "CHANGES_REQUESTED", "2026-10-11T00:00:00Z", commit=OLD, text="")])
    out = readiness(obs)
    assert out["verdict"] == "attention" and out["changes_requested"]


def test_dismissed_overrides_changes_requested():
    obs = observations(reviews=[
        review("gpt", "CHANGES_REQUESTED", "2026-10-09T00:00:00Z", commit=HEAD, rid=1),
        review("gpt", "DISMISSED", "2026-10-10T00:00:00Z", commit=HEAD, rid=2),
    ])
    out = readiness(obs)
    assert out["changes_requested"] == []


def test_mergeable_state_reasons():
    for state in ("unstable", "behind", "blocked"):
        obs = observations()
        out = readiness(obs, mergeable_state=state)
        assert f"mergeable_state {state}" in out["reasons"]


def test_outdated_open_threads_do_not_count_as_unresolved_reason():
    threads = [{"id": "T1", "is_resolved": False, "is_outdated": True, "path": "a.py", "line": 1, "url": "u"}]
    out = readiness(observations(threads=threads))
    assert not any("unresolved thread" in r for r in out["reasons"])


def test_finding_text_and_head_messages_truncated():
    long = "x" * 300
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text=f"Should: {long}")])
    out = readiness(obs)
    assert len(out["findings"][0]["text"]) == 200
    commits = [{"commit": {"message": f"m{i}"}} for i in range(7)]
    obs2 = observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z")])
    out2 = readiness(obs2, {f"repos/o/r/compare/{OLD}...{HEAD}": {"ahead_by": 7, "commits": commits}})
    assert out2["head_moved_after_review"]["messages"] == [f"m{i}" for i in range(2, 7)]


def test_ccr_comment_id_types_and_outdated_flag():
    inline = [{"id": 11, "author": "x", "created_at": "2026-10-09T00:00:00Z", "url": "u11", "thread_id": None, **body("x")}]
    obs = observations(inline=inline, thread_error="graphql_error")
    route = "repos/o/r/pulls/7/ccr/review_threads"
    out = readiness(obs, {route: [{"resolved": False, "outdated": True, "path": "p", "line": 2, "comment_ids": ["11"]}]})
    assert obs["inline_comment"][0]["thread_id"] is None
    assert not any("unresolved thread" in r for r in out["reasons"])
    inline2 = [{"id": 11, "author": "x", "created_at": "2026-10-09T00:00:00Z", "url": "u11", "thread_id": None, **body("x")}]
    obs2 = observations(inline=inline2, thread_error="graphql_error")
    out2 = readiness(obs2, {route: [{"resolved": False, "outdated": False, "path": "p", "line": 2, "comment_ids": [11, "12"]}]})
    assert obs2["review_thread"][0]["comment_ids"] == [11]
    assert obs2["inline_comment"][0]["thread_id"] == "ccr:11"
    assert out2["open_threads"]


def test_merge_readiness_is_bounded():
    assert hasattr(g.merge_readiness, "__wrapped__")
    assert hasattr(g.fleet_checks, "__wrapped__")


def test_cli_exit_codes(capsys):
    obs = observations(reviews=[review("gpt", "APPROVED", "2026-10-11T00:00:00Z", commit=HEAD)])
    with patch.object(g, "pr", return_value=head()), patch.object(g, "review_observations", return_value=obs), \
         patch.object(g, "request", side_effect=api()):
        assert g.main(["merge-readiness", "o/r", "7", "--text"]) == 0
        assert capsys.readouterr().out.startswith("o/r#7 feat/x@bbbbbbb -> main: clear")
        assert g.main(["merge-readiness", "o/r", "7"]) == 0
        assert json.loads(capsys.readouterr().out)["verdict"] == "clear"
    with patch.object(g, "pr", return_value=head(draft=True)), patch.object(g, "review_observations", return_value=obs), \
         patch.object(g, "request", side_effect=api()):
        assert g.main(["merge-readiness", "o/r", "7"]) == 3


def test_inline_explicit_label_inside_heading_remains_visible():
    text = "## Should\nMust: do not merge without tests"
    labels = g._explicit_severity(text)
    assert len(labels) == 1 and labels[0]["label"] == "Must"
    out = readiness(observations(reviews=[
        review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text=text)]))
    assert out["verdict"] == "blocked"
    assert out["findings"][0]["text"] == "Must: do not merge without tests"


# ──── merge_readiness_mutations ───────────────────────

# Subprocess mutation tests: each mutant must fail at least one targeted pytest node.


SOURCE = ROOT / "gh_identity.py"


MUTATIONS = [
    ("approval_uses_max", "approved_at[x[\"author\"]] = max(filter(None", "approved_at[x[\"author\"]] = min(filter(None",
     "tests/test_pr.py::test_approval_timestamp_uses_latest"),
    ("dismissed_not_approved", 'if x.get("state") == "APPROVED":',
     'if x.get("state") in ("APPROVED", "DISMISSED"):',
     "tests/test_pr.py::test_dismissed_does_not_supersede_findings"),
    ("paired_finding_text", '"text": " ".join(lab["text"].split())[:200]',
     '"text": " ".join(labels[-1]["text"].split())[:200]',
     "tests/test_pr.py::test_paired_label_and_text_per_finding"),
    ("commented_not_approved", 'if x.get("state") == "APPROVED":',
     'if x.get("state") in ("APPROVED", "COMMENTED", "CHANGES_REQUESTED"):',
     "tests/test_pr.py::test_commented_does_not_supersede"),
    ("blocking_thread_ignores_outdated",
     '(f["after_head"] or (f["thread"] and not f["thread"]["outdated"]))',
     '(f["after_head"] or f["thread"])',
     "tests/test_pr.py::test_outdated_thread_blocking_finding_is_not_blocker"),
    ("head_message_cap_100", ")[0][:100]", ")[0]",
     "tests/test_pr.py::test_head_message_hundred_char_cap"),
    ("after_head_strict_gt", "at > _when(head_at)", "at >= _when(head_at)",
     "tests/test_pr.py::test_after_head_strictly_after_commit_time"),
    ("supersede_strict_gt", "later > at", "later >= at",
     "tests/test_pr.py::test_supersede_requires_approval_after_finding"),
    ("changes_requested_head_only", 'blockers += [c for c in changes_requested if c["commit_id"] == sha]',
     "blockers += list(changes_requested)",
     "tests/test_pr.py::test_changes_requested_on_old_commit_is_not_blocker"),
    ("blocker_respects_outdated", 'not f["thread"]["outdated"]',
     "False",
     "tests/test_pr.py::test_inline_findings_threads_and_states"),
    ("should_not_block", 'f["label"] in ("Must", "Blocking")',
     'f["label"] in ("Must", "Blocking", "Should")',
     "tests/test_pr.py::test_empty_blocking_section_is_not_a_finding"),
    ("must_still_blocks", 'f["label"] in ("Must", "Blocking")',
     'f["label"] in ("Blocking",)',
     "tests/test_pr.py::test_must_after_head_blocks_without_changes_requested"),
    ("dismissed_latest", 'if x.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):',
     'if x.get("state") in ("APPROVED", "CHANGES_REQUESTED"):',
     "tests/test_pr.py::test_dismissed_overrides_changes_requested"),
    ("mergeable_unstable", '("dirty", "blocked", "behind", "unstable")',
     '("dirty", "blocked", "behind")',
     "tests/test_pr.py::test_mergeable_state_reasons"),
    ("mergeable_behind", '("dirty", "blocked", "behind", "unstable")',
     '("dirty", "blocked", "unstable")',
     "tests/test_pr.py::test_mergeable_state_reasons"),
    ("mergeable_blocked", '("dirty", "blocked", "behind", "unstable")',
     '("dirty", "behind", "unstable")',
     "tests/test_pr.py::test_mergeable_state_reasons"),
    ("unresolved_skips_outdated", "not t[\"outdated\"]",
     "True",
     "tests/test_pr.py::test_outdated_open_threads_do_not_count_as_unresolved_reason"),
    ("finding_text_200", '"text": " ".join(lab["text"].split())[:200]',
     '"text": " ".join(lab["text"].split())',
     "tests/test_pr.py::test_finding_text_and_head_messages_truncated"),
    ("head_messages_tail", "][-5:]", "][:5]",
     "tests/test_pr.py::test_finding_text_and_head_messages_truncated"),
    ("ccr_int_comment_ids", "ids = [i for i in x.get(\"comment_ids\") or [] if type(i) is int]",
     "ids = list(x.get(\"comment_ids\") or [])",
     "tests/test_pr.py::test_ccr_comment_id_types_and_outdated_flag"),
    ("ccr_thread_id_zero", "ids[0] if ids else len(threads)",
     "0",
     "tests/test_pr.py::test_ccr_comment_id_types_and_outdated_flag"),
    ("ccr_outdated_bool", '"is_outdated": bool(x.get("outdated"))',
     '"is_outdated": False',
     "tests/test_pr.py::test_ccr_comment_id_types_and_outdated_flag"),
    ("fleet_wait_parse", "return ns.wait", "return 0.0",
     "tests/test_fleet.py::test_wait_polls_outside_one_shared_scope"),
    ("merge_readiness_bounded", '"merge_readiness","fleet_checks"',
     '"fleet_checks"',
     "tests/test_pr.py::test_merge_readiness_is_bounded"),
    ("empty_blocking_section", 'if kind and kind[0]=="severity":',
     'if False and kind and kind[0]=="severity":',
     "tests/test_pr.py::test_empty_blocking_section_is_not_a_finding"),
    ("numbered_should_label", "(?:\\s+\\d+)?(?=\\s*(?:[:：—–]|-\\s|$))",
     "(?=\\s*(?:[:：—–]|-\\s|$))",
     "tests/test_pr.py::test_severity_labels_accept_dashes_and_must[- Should 1\\uff1arename the helper-Should]"),
    ("ignore_fence_in_section", "if _FENCE_START.match(lines[i]):i=_skip_fence(lines,i);continue",
     "if False and _FENCE_START.match(lines[i]):i=_skip_fence(lines,i);continue",
     "tests/test_pr.py::test_codeblock_inside_section_ignored"),
    ("non_severity_heading_break", 'if inner:break',
     "if False and inner:break",
     "tests/test_pr.py::test_non_severity_heading_ends_section"),
    ("nested_bullet_single", "if ni is not None and ni<=indent:break",
     "if ni is not None:break",
     "tests/test_pr.py::test_nested_bullets_are_one_finding"),
]


def _run_mutant(name, old, new, nodeid):
    text = SOURCE.read_text(encoding="utf-8")
    if old not in text:
        pytest.fail(f"{name}: anchor not found")
    mutant = text.replace(old, new, 1)
    if mutant == text:
        pytest.fail(f"{name}: replace had no effect")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "gh_identity.py").write_text(mutant, encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = td
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", nodeid, "-q", "--tb=no", "-o", f"pythonpath={td}"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
    if proc.returncode == 0:
        pytest.fail(f"{name}: mutation survived\nstdout={proc.stdout}\nstderr={proc.stderr}")


@pytest.mark.parametrize("name,old,new,nodeid", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_mutation_killed(name, old, new, nodeid):
    _run_mutant(name, old, new, nodeid)


# ──── review_observations ─────────────────────────────────────────

# Offline review identity, bounded pagination and failure contracts.


FIXTURE=FIXTURES['review_observations/responses']


def responses():
    return copy.deepcopy([FIXTURE[k] for k in ('conversation','review_submission','inline_comment','review_thread')])


def thread_page(ids=(3,), *, more=False, cursor=None, total=None, thread='T1'):
    result=copy.deepcopy(FIXTURE['review_thread'])
    connection=result['data']['repository']['pullRequest']['reviewThreads']
    row=connection['nodes'][0]
    row['id']=thread
    row['comments']['nodes']=[{'databaseId':i,'url':f'https://github.com/o/r/pull/7#discussion_r{i}'} for i in ids]
    row['comments']['totalCount']=len(ids) if total is None else total
    row['comments']['pageInfo']={'hasNextPage':more,'endCursor':cursor}
    return result


def test_four_kinds_reuse_apis_and_preserve_observed_links():
    with patch.object(g,'request',side_effect=responses()) as request, \
         patch.object(g,'comments',wraps=g.comments) as comments, \
         patch.object(g,'reviews',wraps=g.reviews) as reviews:
        result=g.review_observations('o/r',7,transport='urllib')
    assert result['complete'] and not result['truncated']
    comments.assert_called_once();reviews.assert_called_once()
    assert [x[0][0] for x in request.call_args_list]==['GET','GET','GET','POST']
    gql=request.call_args_list[-1]
    assert gql.args[1]=='graphql' and gql.args[2]['query'].startswith('query(')
    assert 'mutation' not in gql.args[2]['query']
    assert gql.kwargs['transport']=='urllib'
    assert result['inline_comment'][0]['thread_id']=='T1'
    assert result['inline_comment'][0]['diff_hunk'].startswith('@@')
    assert result['review_submission'][0]['url'].endswith('review-2')
    assert result['review_submission'][0]['severity_labels']==[]
    assert [x['label'] for x in result['inline_comment'][0]['severity_labels']]==['Blocking','Nit']
    assert result['review_thread'][0]['is_resolved'] is True
    assert result['review_thread'][0]['url'].endswith('discussion_r3')
    assert result['conversation'][0]['preview'].startswith('説明です 😀')


@pytest.mark.parametrize('body,labels',[
    ('This should be blocking, but is not labelled.',[]),
    ('CHANGES_REQUESTED',[]),('Blocking 0 / Should 1',[]),
    ('**Should**: handle this',['Should']),('Non-blocking: optional',['Non-blocking']),
    ('- Nit: spelling',['Nit']),('### Blocking: failure',['Blocking']),
    ('Blocking\nShould',['Blocking','Should']),
    ('```text\nBlocking: example\n```',[]),
])
def test_only_explicit_labels(body,labels):
    assert [x['label'] for x in g._explicit_severity(body)]==labels


@pytest.mark.parametrize('api', [g.inline_comments,g.review_threads,g.review_observations])
@pytest.mark.parametrize('kw',[{'max_items':0},{'max_pages':True},{'max_items':1.2}])
def test_invalid_limits_before_transport(api,kw):
    with patch.object(g,'request') as request:
        with pytest.raises(ValueError):api('o/r',7,**kw)
    request.assert_not_called()


def test_rest_link_pagination_and_exact_terminal_limit():
    first=g.Page([{'id':1,'body':'a'}],{'Link':'<https://api.github.com/repos/o/r/pulls/7/comments?per_page=100&page=2>; rel="next"'})
    second=g.Page([{'id':2,'body':'b'}],{})
    with patch.object(g,'request',side_effect=[first,second]) as request:
        result=g.inline_comments('o/r',7,max_items=2)
    assert result['complete'] and len(result['comments'])==2
    assert request.call_args_list[1].args[1].endswith('page=2')


@pytest.mark.parametrize('limit,reason',[({'max_items':1},'max_items'),({'max_pages':1,'max_items':200},'max_pages')])
def test_rest_partial(limit,reason):
    first=[{'id':i} for i in range(1,101)]
    with patch.object(g,'request',return_value=first):
        result=g.inline_comments('o/r',7,**limit)
    assert not result['complete'] and result['truncated']
    assert result['limit_reason']==reason


def test_nested_comment_pagination():
    first=thread_page((3,),more=True,cursor='C1',total=2)
    second={'data':{'node':{'comments':{'nodes':[{'databaseId':4,'url':'https://github.com/o/r/pull/7#discussion_r4'}],'totalCount':2,'pageInfo':{'hasNextPage':False,'endCursor':'C2'}}}}}
    with patch.object(g,'request',side_effect=[first,second]) as request:
        result=g.review_threads('o/r',7)
    assert result['complete']
    assert result['threads'][0]['comment_ids']==[3,4]
    assert request.call_args_list[1].args[2]['variables']=={'id':'T1','cursor':'C1'}


def test_nested_limit_never_claims_complete():
    with patch.object(g,'request',return_value=thread_page((3,),more=True,cursor='C1',total=2)):
        result=g.review_threads('o/r',7,max_pages=1)
    assert result['limit_reason']=='thread_comments'
    assert result['threads'][0]['comments_complete'] is False


def test_outer_thread_pagination():
    first=thread_page(thread='T1');second=thread_page((4,),thread='T2')
    c=first['data']['repository']['pullRequest']['reviewThreads']
    c['pageInfo']={'hasNextPage':True,'endCursor':'Tcursor'}
    with patch.object(g,'request',side_effect=[first,second]) as request:
        result=g.review_threads('o/r',7)
    assert result['complete'] and len(result['threads'])==2
    assert request.call_args_list[1].args[2]['variables']['cursor']=='Tcursor'


@pytest.mark.parametrize('error',['authentication_required','permission_or_rate_limit','not_found_or_inaccessible'])
def test_failed_component_is_unknown_not_empty_complete(error):
    values=responses();values[-1]=g.Error(error)
    with patch.object(g,'request',side_effect=values):result=g.review_observations('o/r',7)
    assert not result['complete'] and not result['truncated']
    assert result['components']['review_thread']['error']==error
    assert result['inline_comment'][0]['thread_id'] is None


def test_graphql_partial_data_and_errors_are_not_complete():
    values=responses();values[-1]['errors']=[{'type':'FORBIDDEN','message':'private details'}]
    with patch.object(g,'request',side_effect=values):result=g.review_observations('o/r',7)
    assert result['components']['review_thread']['error']=='permission_or_rate_limit'
    assert 'private details' not in json.dumps(result)


@pytest.mark.parametrize('bad,code',[
    ({'data':None},'invalid_json'),
    ({'data':{'repository':None}},'not_found_or_inaccessible'),
    ({'errors':[{'type':'OTHER'}]},'graphql_error'),
])
def test_graphql_invalid_response(bad,code):
    with patch.object(g,'request',return_value=bad):
        with pytest.raises(g.Error,match=code):g.review_threads('o/r',7)


def test_missing_cursor_and_wrong_member_count_fail_closed():
    for value in (thread_page(more=True),thread_page(total=2)):
        with patch.object(g,'request',return_value=value):
            with pytest.raises(g.Error,match='pagination_incomplete'):g.review_threads('o/r',7)


def test_duplicate_rest_identity_rejected():
    with patch.object(g,'request',return_value=[{'id':1},{'id':1}]):
        with pytest.raises(g.Error,match='ambiguous_comment_identity'):g.inline_comments('o/r',7)


@pytest.mark.parametrize('limit', ['max_pages','max_items','max_bytes'])
def test_shared_operation_budget_fails_closed(limit):
    with patch.object(g,'request',side_effect=responses()):
        with g.operation(**{limit:1}):
            with pytest.raises(g.Error,match=limit[4:]+'_limit'):
                # Mock request bypasses real byte accounting; pages still charge.
                if limit=='max_bytes':
                    g.pages('repos/o/r/issues/7/comments',requester=lambda *a,**k:FIXTURE['conversation'])
                else:g.review_observations('o/r',7)


def test_diff_tracks_resolution_and_preserves_unknown_removal():
    with patch.object(g,'request',side_effect=responses()):before=g.review_observations('o/r',7)
    after=copy.deepcopy(before)
    after['review_thread'][0]['is_resolved']=False
    diff=g.diff_pr_observations(before,after)
    assert diff['changes']['review_thread']['changed']==['T1']
    after['inline_comment']=[];after['components']['inline_comment']['complete']=False;after['complete']=False
    assert g.diff_pr_observations(before,after)['changes']['inline_comment']['removed']==[]
    after['number']=8
    with pytest.raises(ValueError,match='identity mismatch'):g.diff_pr_observations(before,after)


@pytest.mark.parametrize('complete,exit_code',[(True,0),(False,1)])
def test_cli_result_and_exit(capsys,complete,exit_code):
    result={'complete':complete}
    with patch.object(g,'review_observations',return_value=result) as api:
        assert g.main(['review-observations','o/r','7','--limit','4','--page-limit','2','--transport','urllib'])==exit_code
    assert json.loads(capsys.readouterr().out)==result
    assert api.call_args.kwargs['max_items']==4


def test_cli_permission_error_exit(capsys):
    with patch.object(g,'request',side_effect=g.Error('permission_or_rate_limit')):
        assert g.main(['review-threads','o/r','7'])==2
    assert 'permission_or_rate_limit' in capsys.readouterr().err


@pytest.mark.parametrize('transport',['gh','urllib'])
def test_graphql_uses_existing_transport_and_is_nonmutating(transport):
    with patch.object(g,'_gh' if transport=='gh' else '_url',return_value=thread_page()) as backend:
        assert g.review_threads('o/r',7,transport=transport)['complete']
    args=backend.call_args.args
    assert args[0:2]==('POST','graphql')
    assert args[2]['query'].startswith('query(')
    assert len(args)<5 or not args[4]


def test_exact_thread_limit_complete_at_terminal_page():
    with patch.object(g,'request',return_value=thread_page()):
        assert g.review_threads('o/r',7,max_items=1)['complete']


def test_thread_limit_with_observed_continuation_is_partial():
    page=thread_page()
    page['data']['repository']['pullRequest']['reviewThreads']['pageInfo']={'hasNextPage':True,'endCursor':'next'}
    with patch.object(g,'request',return_value=page):
        result=g.review_threads('o/r',7,max_items=1)
    assert result['limit_reason']=='max_items' and not result['complete']


def test_duplicate_threads_and_member_ids_rejected():
    page=thread_page();nodes=page['data']['repository']['pullRequest']['reviewThreads']['nodes']
    nodes.append(copy.deepcopy(nodes[0]))
    with patch.object(g,'request',return_value=page):
        with pytest.raises(g.Error,match='ambiguous_thread_identity'):g.review_threads('o/r',7)
    with patch.object(g,'request',return_value=thread_page((3,3))):
        with pytest.raises(g.Error,match='ambiguous_comment_identity'):g.review_threads('o/r',7)


@pytest.mark.parametrize('bad',[{'id':1,'body':5},{'id':1,'user':[]},None])
def test_malformed_rest_rows_raise_classified_error(bad):
    with patch.object(g,'request',return_value=[bad]):
        with pytest.raises(g.Error,match='invalid_json'):g.inline_comments('o/r',7)


@pytest.mark.parametrize('api',[g.inline_comments,g.review_threads,g.review_observations])
@pytest.mark.parametrize('number',[True,0,'bad',-1])
def test_invalid_pr_identity_before_transport(api,number):
    with patch.object(g,'request') as request:
        with pytest.raises(ValueError):api('o/r',number)
    request.assert_not_called()


def test_global_timeout_propagates_not_partial_success():
    values=responses();values[-1]=g.Error('operation_timeout')
    with patch.object(g,'request',side_effect=values):
        with pytest.raises(g.Error,match='operation_timeout'):g.review_observations('o/r',7)


def test_diff_suppresses_invented_additions_for_incomplete_baseline():
    with patch.object(g,'request',side_effect=responses()):after=g.review_observations('o/r',7)
    before=copy.deepcopy(after);before['inline_comment']=[]
    before['complete']=False;before['components']['inline_comment']['complete']=False
    assert g.diff_pr_observations(before,after)['changes']['inline_comment']['added']==[]


@pytest.mark.parametrize('command',['inline-comments','review-threads'])
def test_thin_cli_other_components(command,capsys):
    api='inline_comments' if command=='inline-comments' else 'review_threads'
    with patch.object(g,api,return_value={'complete':True}) as fn:
        assert g.main([command,'o/r','7','--limit','2'])==0
    assert fn.call_args.kwargs['max_items']==2
    assert json.loads(capsys.readouterr().out)['complete']


def test_numeric_string_pr_normalized_for_graphql_int_variable():
    with patch.object(g,'request',return_value=thread_page()) as request:
        result=g.review_threads('o/r','7')
    assert result['number']==7
    assert request.call_args.args[2]['variables']['number']==7


# ──── comment_shortcuts ───────────────────────────────────────────

REPO='myon-bioinformatics/mcp-toolcall-lab'


@pytest.mark.parametrize('args,path,ident', [
 (['review',REPO,'119','5495899602'],f'repos/{REPO}/pulls/119/reviews/5495899602',5495899602),
 (['inline-comment',REPO,'4252282158'],f'repos/{REPO}/pulls/comments/4252282158',4252282158),
 (['read-comment',f'https://github.com/{REPO}/pull/119#pullrequestreview-5495899602'],f'repos/{REPO}/pulls/119/reviews/5495899602',5495899602),
 (['read-comment',f'https://github.com/{REPO}/pull/119#discussion_r4252282158'],f'repos/{REPO}/pulls/comments/4252282158',4252282158),
 (['read-comment',f'https://github.com/{REPO}/issues/115#issuecomment-123'],f'repos/{REPO}/issues/comments/123',123),
])
def test_body_shortcuts(args,path,ident,monkeypatch,capsys):
 seen=[]
 def request(method,url,**kw):
  seen.append((method,url,kw['transport']))
  return {'id':ident,'body':'日本語\nsecond line','html_url':'https://github.com/example'}
 monkeypatch.setattr(g,'request',request)
 assert g.main(['--transport','urllib',*args])==0
 assert capsys.readouterr().out=='日本語\nsecond line\n'
 assert seen==[('GET',path,'urllib')]
 assert g.main([*args,'--json'])==0
 assert json.loads(capsys.readouterr().out)['body']=='日本語\nsecond line'


@pytest.mark.parametrize('args',[
 ['read-comment','https://evil.invalid/o/r/pull/1#discussion_r1'],
 ['read-comment','https://github.com/o/r/pull/1'],
 ['read-comment','https://github.com/o/r/pull/1?x=y#discussion_r1'],
 ['review','https://github.com/o/r/pull/1#discussion_r1'],
 ['inline-comment','o/r','0'],['review','o/r','1','-2'],
 ['review','../r','1','2'],['read-comment','o/r','2'],
])
def test_invalid_targets_do_not_request(args,monkeypatch,capsys):
 monkeypatch.setattr(g,'request',lambda *a,**k:pytest.fail('must not request'))
 assert g.main(args)==2
 assert json.loads(capsys.readouterr().err)['error']=='invalid_argument'


@pytest.mark.parametrize('data',[{}, {'id':1,'body':None},{'id':2,'body':'wrong'},{'id':True,'body':''}])
def test_bad_response_fails_closed(data,monkeypatch,capsys):
 monkeypatch.setattr(g,'request',lambda *a,**k:data)
 assert g.main(['inline-comment','o/r','1'])==2
 assert json.loads(capsys.readouterr().err)['error']=='invalid_comment_response'


# ──── read_observations ───────────────────────────────────────────

# Regressions for read observation identity and normalization.


def job(**changes):
    return dict({'id': 91, 'run_id': 20, 'run_attempt': 2, 'steps': []}, **changes)


@pytest.mark.parametrize('rows,total,error', [
    ([job(run_id=999)], 1, 'job_identity_mismatch'),
    ([job(run_id=True)], 1, 'job_identity_mismatch'),
    ([job(run_attempt=3)], 1, 'attempt_mismatch'),
    ([job(run_attempt=None)], 1, 'invalid_attempt_identity'),
    ([job(run_attempt=True)], 1, 'invalid_attempt_identity'),
    ([job(id=True)], 1, 'invalid_job_identity'),
    ([job(id=0)], 1, 'invalid_job_identity'),
    ([job(id='91')], 1, 'invalid_job_identity'),
    ([job(), job()], 2, 'ambiguous_job_identity'),
    ([None], 1, 'invalid_json'),
    ([job(steps=[None])], 1, 'invalid_json'),
    ([job(steps=None)], 1, 'invalid_json'),
    ([job()], True, 'pagination_incomplete'),
    ([], -1, 'pagination_incomplete'),
])
def test_jobs_reject_inconsistent_observation(rows, total, error):
    with patch.object(g, 'request', return_value={'total_count': total, 'jobs': rows}):
        with pytest.raises(g.Error) as exc:
            g.jobs('o/r', 20, attempt=2)
    assert exc.value.code == error


def test_duplicate_jobs_across_pages_are_rejected():
    first = [job(id=n) for n in range(1, 101)]
    with patch.object(g, 'request', side_effect=[
        {'total_count': 101, 'jobs': first}, {'total_count': 101, 'jobs': [job(id=1)]}
    ]):
        with pytest.raises(g.Error, match='ambiguous_job_identity'):
            g.jobs('o/r', 20, attempt=2)


def test_unspecified_attempt_preserves_observed_attempts():
    # Latest jobs can include jobs retained from an earlier partial rerun.
    rows = [job(id=91, run_attempt=1), job(id=92, run_attempt=2)]
    with patch.object(g, 'request', return_value={'total_count': 2, 'jobs': rows}):
        result = g.jobs('o/r', 20)
    assert result['complete'] is True
    assert result['attempt'] is None
    assert [x['attempt'] for x in result['jobs']] == [1, 2]


@pytest.mark.parametrize('last,ids', [(None, [1, 2]), (0, []), (1, [2]), (2, [1, 2]), (10, [1, 2])])
def test_comment_last_limit(last, ids):
    with patch.object(g, 'pages', return_value=[{'id': 1, 'body': '一'}, {'id': 2, 'body': '二'}]):
        result = g.comments('o/r', 1, last=last)
    assert result['total'] == 2
    assert result['shown'] == len(ids)
    assert [c['id'] for c in result['comments']] == ids


@pytest.mark.parametrize('last', [-1, True, False, '1', 1.5])
def test_bad_comment_limit_fails_before_transport(last):
    with patch.object(g, 'pages') as pages:
        with pytest.raises(ValueError):
            g.comments('o/r', 1, last=last)
    pages.assert_not_called()


@pytest.mark.parametrize('fields,expected', [
    ({'url': 'api-url', 'html_url': 'web-url'}, 'web-url'),
    ({'url': 'normalized-web-url'}, 'normalized-web-url'),
    ({'html_url': 'web-url'}, 'web-url'),
])
def test_check_url_normalization_through_transport_and_pure_helper(fields, expected):
    row = dict(id=1, name='unit', status='completed', conclusion='success', **fields)
    original = dict(row)
    pure = g.summarize_checks([row], 1)
    with patch.object(g, 'request', return_value={'total_count': 1, 'check_runs': [row]}):
        fetched = g.checks_for_sha('o/r', 'a' * 40)
    assert pure['checks'][0]['url'] == fetched['checks'][0]['url'] == expected
    assert pure['state'] == fetched['state'] == 'green'
    assert row == original


# ──── wait_checks ─────────────────────────────────────────────────

# wait-checks: first/last snapshot, per-check transitions, ignore, timeout.


def _run(name, status, conclusion=None, ident=None):
    return {"id": ident or name, "name": name, "status": status, "conclusion": conclusion, "html_url": "u"}


def _fake(sequence):
    polls = iter(sequence)

    def checks(r, sha, min_checks=1, transport="auto", timeout=30):
        rows = next(polls)
        return {"repository": r, "sha": sha, **g.summarize_checks(rows, len(rows), min_checks)}
    return checks


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


SEQUENCE = [
    [_run("pytest", "in_progress"), _run("flutter", "queued"), _run("bot", "completed", "failure")],
    [_run("pytest", "completed", "success"), _run("flutter", "in_progress"), _run("bot", "completed", "failure")],
    [_run("pytest", "completed", "success"), _run("flutter", "completed", "success"), _run("bot", "completed", "failure")],
]


def test_reports_initial_final_and_transitions(monkeypatch):
    monkeypatch.setattr(g, "checks_for_sha", _fake(SEQUENCE))
    clock, lines = Clock(), []
    out = g.wait_checks("o/r", SHA, interval=60, ignore=["bot"], sleep=clock.sleep, clock=clock,
                        progress=lines.append)
    assert out["state"] == "green" and out["polls"] == 3 and not out["timed_out"]
    assert out["initial"] == {"at_seconds": 0.0, "state": "pending", "count": 2,
                              "by_conclusion": {"in_progress": 1, "queued": 1}}
    assert out["final"]["by_conclusion"] == {"success": 2} and out["final"]["at_seconds"] == 120.0
    assert [(t["name"], t["from"], t["to"], t["at_seconds"]) for t in out["transitions"]] == [
        ("pytest", "in_progress", "success", 60.0), ("flutter", "queued", "in_progress", 60.0),
        ("flutter", "in_progress", "success", 120.0)]
    assert out["ignored"] == [{"name": "bot", "conclusion": "failure", "status": "completed"}]
    assert clock.slept == [60, 60]
    assert "pytest: in_progress -> success" in lines[1] and "waiting (1): flutter" in lines[1]


def test_ignored_failure_still_fails_when_not_ignored(monkeypatch):
    monkeypatch.setattr(g, "checks_for_sha", _fake(SEQUENCE))
    clock = Clock()
    out = g.wait_checks("o/r", SHA, interval=60, sleep=clock.sleep, clock=clock)
    # A completed failure is final at once, even while others still run.
    assert (out["state"], out["polls"], out["failed_checks"]) == ("failed", 1, ["bot"])


def test_times_out_with_last_snapshot(monkeypatch):
    monkeypatch.setattr(g, "checks_for_sha", _fake([SEQUENCE[0]] * 10))
    clock = Clock()
    out = g.wait_checks("o/r", SHA, interval=60, wait_timeout=150, ignore=["bot"], sleep=clock.sleep, clock=clock)
    assert out["timed_out"] and out["state"] == "pending" and out["polls"] == 3 and out["transitions"] == []


def test_check_appearing_later_is_a_transition(monkeypatch):
    monkeypatch.setattr(g, "checks_for_sha", _fake([[_run("a", "completed", "success")],
                                                    [_run("a", "completed", "success"), _run("b", "queued")],
                                                    [_run("a", "completed", "success"), _run("b", "completed", "success")]]))
    clock = Clock()
    # Without --min-checks the first poll would already be green with one check.
    early = g.wait_checks("o/r", SHA, interval=10, sleep=clock.sleep, clock=clock)
    assert (early["state"], early["polls"]) == ("green", 1)
    monkeypatch.setattr(g, "checks_for_sha", _fake([[_run("a", "completed", "success")],
                                                    [_run("a", "completed", "success"), _run("b", "queued")],
                                                    [_run("a", "completed", "success"), _run("b", "completed", "success")]]))
    out = g.wait_checks("o/r", SHA, min_checks=2, interval=10, sleep=clock.sleep, clock=clock)
    assert [(t["name"], t["from"], t["to"]) for t in out["transitions"]] == [("b", "absent", "queued"), ("b", "queued", "success")]


@pytest.mark.parametrize("kwargs", [{"sha": "abc"}, {"interval": 1}, {"interval": True}, {"wait_timeout": 0},
                                    {"wait_timeout": 4000}])
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        g.wait_checks("o/r", kwargs.pop("sha", SHA), **kwargs)


def test_cli_progress_on_stderr_json_on_stdout_and_exit(monkeypatch, capsys):
    monkeypatch.setattr(g, "checks_for_sha", _fake(SEQUENCE))
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    assert g.main(["wait-checks", "o/r", SHA, "--interval", "5", "--ignore-check", "bot"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["state"] == "green" and "[0s] pending" in err
    monkeypatch.setattr(g, "checks_for_sha", _fake(SEQUENCE))
    assert g.main(["wait-checks", "o/r", SHA, "--interval", "5"]) == 1
    capsys.readouterr()
    assert g.main(["wait-checks", "o/r", SHA, "--max-pages", "3"]) == 2


def test_min_checks_counts_ignored_checks_too(monkeypatch):
    # 2 checks, 1 ignored, min_checks=2: the remaining success must be green.
    monkeypatch.setattr(g, "checks_for_sha", _fake([[_run("ok", "completed", "success"),
                                                     _run("bot", "completed", "failure")]]))
    clock = Clock()
    out = g.wait_checks("o/r", SHA, min_checks=2, interval=10, ignore=["bot"], sleep=clock.sleep, clock=clock)
    assert (out["state"], out["polls"], out["timed_out"]) == ("green", 1, False)


def test_min_checks_still_waits_for_checks_not_yet_registered(monkeypatch):
    monkeypatch.setattr(g, "checks_for_sha", _fake([[_run("ok", "completed", "success"), _run("bot", "completed", "failure")]] * 3))
    clock = Clock()
    out = g.wait_checks("o/r", SHA, min_checks=3, interval=10, wait_timeout=25, ignore=["bot"],
                        sleep=clock.sleep, clock=clock)
    assert out["state"] == "pending" and out["timed_out"]


# ──── checks_text ─────────────────────────────────────────────────

# checks: several SHAs and a uniq -c style --text digest of what needs attention.


def _summary(sha, rows):
    return {"schema": "gh-identity-checks/1", "repository": "o/r", "sha": sha,
            **g.summarize_checks(rows, len(rows)), "observed_at": "t"}


ROWS = [
    {"id": 1, "name": "test (3.12)", "status": "completed", "conclusion": "success"},
    {"id": 2, "name": "test", "status": "completed", "conclusion": "failure"},
    {"id": 3, "name": "test", "status": "completed", "conclusion": "failure"},
    {"id": 4, "name": "lint", "status": "completed", "conclusion": "skipped"},
    {"id": 5, "name": "e2e", "status": "in_progress", "conclusion": None},
]


def test_text_groups_and_hides_quiet_checks():
    text = g.render_checks(_summary("a" * 40, ROWS))
    lines = text.splitlines()
    assert lines[0].startswith("aaaaaaaaaaaa pending 5 ")
    assert lines[1:] == ["    1 in_progress - e2e", "    2 completed failure test"]


def test_several_shas_report_the_first_non_green_state(monkeypatch):
    green = [{"id": 1, "name": "ci", "status": "completed", "conclusion": "success"}]
    data = {"a" * 40: green, "b" * 40: ROWS}
    monkeypatch.setattr(g, "checks_for_sha", lambda r, sha, *a, **k: _summary(sha, data[sha]))
    out = g.checks_for_shas("o/r", list(data))
    assert out["schema"] == "gh-identity-checks-list/1" and out["state"] == "pending"
    text = g.render_checks(out).splitlines()
    assert text[0].endswith("green 1 success=1") and len(text) == 4
    single = g.checks_for_shas("o/r", ["a" * 40])
    assert single["schema"] == "gh-identity-checks/1"


def test_cli_text_and_gate_exit(monkeypatch, capsys):
    monkeypatch.setattr(g, "checks_for_sha", lambda r, sha, *a, **k: _summary(sha, ROWS))
    assert g.main(["checks", "o/r", "c" * 40, "--text"]) == 1
    assert "2 completed failure test" in capsys.readouterr().out


# ──── get_and_lists ───────────────────────────────────────────────

# get: read-only GET with query options and field picking; --text for prs/issues.


def test_query_is_encoded_and_fields_are_picked(monkeypatch):
    seen = []
    rows = [{"number": 3, "head": {"ref": "work/x"}, "title": "T 1"}, {"number": 2, "head": None, "title": "T&2"}]
    monkeypatch.setattr(g, "request", lambda m, path, **k: seen.append((m, path)) or rows)
    out = g.get("repos/o/r/pulls", ["state=open", "per_page=2"], ["number", "head.ref"])
    assert seen == [("GET", "repos/o/r/pulls?state=open&per_page=2")]
    assert out["data"] == [{"number": 3, "head.ref": "work/x"}, {"number": 2, "head.ref": None}]
    assert g.render_get(out) == "3 work/x\n2 -"


def test_wrapped_lists_are_unwrapped_when_picking(monkeypatch):
    body = {"total_count": 2, "check_runs": [{"name": "ci", "status": "completed", "conclusion": "failure"},
                                             {"name": "e2e", "status": "queued", "conclusion": None}]}
    monkeypatch.setattr(g, "request", lambda *a, **k: body)
    out = g.get("repos/o/r/commits/abc/check-runs", [("per_page", "100")], ["status", "conclusion", "name"])
    assert g.render_get(out).splitlines() == ["completed failure ci", "queued - e2e"]
    assert g.get("repos/o/r/commits/abc/check-runs")["data"] == body  # no pick: whole object


@pytest.mark.parametrize(("path", "query"), [
    ("repos/o/r/pulls?state=open", []),   # options belong in query, not the path
    ("repos/o/r/../x", []),
    ("/repos/o/r", []),
    ("graphql", []),
    ("repos/o/r/pulls", ["state"]),
    ("repos/o/r/pulls", ["bad-key=1"]),
])
def test_invalid_path_or_query_is_rejected(monkeypatch, path, query):
    monkeypatch.setattr(g, "request", lambda *a, **k: pytest.fail("must not request"))
    with pytest.raises(ValueError):
        g.get(path, query)


def test_text_lists_for_prs_and_issues():
    prs = {"pull_requests": [{"number": 9, "state": "open", "draft": True, "head_ref": "w/a", "title": "A"}],
           "count": 1, "truncated": True}
    assert g.render_list(prs) == "#9 open draft w/a A\n(truncated after 1)"
    issues = {"issues": [{"number": 4, "state": "open", "labels": ["p:high"], "title": "B"}], "count": 1}
    assert g.render_list(issues) == "#4 open [p:high] B"


def test_cli_get_text(monkeypatch, capsys):
    monkeypatch.setattr(g, "request", lambda *a, **k: [{"number": 1, "title": "x"}])
    assert g.main(["get", "repos/o/r/pulls", "-q", "state=open", "--pick", "number", "--text"]) == 0
    assert capsys.readouterr().out == "1\n"
    assert g.main(["get", "repos/o/r/pulls?state=open"]) == 2


# ──── labels_write ────────────────────────────────────────────────

# labels: intentional add/remove of existing labels; planned unless --write.


class FakeGitHub:
    def __init__(self, labels, existing):
        self.labels, self.existing, self.writes = list(labels), set(existing), []

    def __call__(self, method, path, payload=None, transport="auto", timeout=30, mutating=False):
        if method == "GET" and "/labels/" in path and "/issues/" not in path:
            name = g.urllib.parse.unquote(path.rsplit("/", 1)[1])
            if name not in self.existing:
                raise g.Error("not_found_or_inaccessible")
            return {"name": name}
        if method == "GET":
            return {"number": 5, "repository_url": g.API + "/repos/o/r", "state": "open", "title": "T",
                    "html_url": "https://github.com/o/r/issues/5", "user": {"login": "u"},
                    "labels": [{"name": n} for n in self.labels]}
        assert mutating
        self.writes.append((method, path, payload))
        if method == "POST":
            self.labels += [n for n in payload["labels"] if n not in self.labels]
        else:
            self.labels.remove(g.urllib.parse.unquote(path.rsplit("/", 1)[1]))
        return {}


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub(["status:in-progress", "type:tracking"], {"priority:high", "status:in-progress", "type:tracking", "a b"})
    monkeypatch.setattr(g, "request", fake)
    return fake


def test_planned_by_default_changes_nothing(gh):
    out = g.set_labels("o/r", 5, add=["priority:high"], remove=["status:in-progress"])
    assert out["status"] == "planned" and gh.writes == []
    assert out["target"] == ["priority:high", "type:tracking"]


def test_write_adds_removes_and_verifies(gh):
    out = g.set_labels("o/r", 5, add=["priority:high", "a b"], remove=["status:in-progress"], write=True)
    assert out["status"] == "verified" and out["after"] == ["a b", "priority:high", "type:tracking"]
    assert gh.writes[0] == ("POST", "repos/o/r/issues/5/labels", {"labels": ["priority:high", "a b"]})
    assert gh.writes[1][:2] == ("DELETE", "repos/o/r/issues/5/labels/status%3Ain-progress")


def test_unknown_label_is_rejected_not_created(gh):
    out = g.set_labels("o/r", 5, add=["prio:typo"], write=True)
    assert out["status"] == "rejected_missing_labels" and out["missing_labels"] == ["prio:typo"] and gh.writes == []


def test_nothing_to_do_is_already_satisfied(gh):
    out = g.set_labels("o/r", 5, add=["type:tracking"], remove=["priority:high"], write=True)
    assert out["status"] == "already_satisfied" and gh.writes == []


def test_mismatch_after_write_is_reported(gh, monkeypatch):
    real = gh.__call__
    def drop_post(method, path, payload=None, *a, **k):
        if method == "POST":
            gh.writes.append((method, path, payload))
            return {}
        return real(method, path, payload, *a, **k)
    monkeypatch.setattr(g, "request", drop_post)
    out = g.set_labels("o/r", 5, add=["priority:high"], write=True)
    assert out["status"] == "verification_mismatch" and not out["verified"]


@pytest.mark.parametrize("kwargs", [{}, {"add": ["x"], "remove": ["x"]}, {"add": [""]}, {"add": ["a\nb"]}])
def test_invalid_requests(gh, kwargs):
    with pytest.raises(ValueError):
        g.set_labels("o/r", 5, **kwargs)


def test_cli_exit_codes_labels_write(gh, capsys):
    assert g.main(["labels", "o/r", "5", "--add", "priority:high", "--remove", "status:in-progress"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert g.main(["labels", "o/r", "5", "--add", "nope"]) == 1
    capsys.readouterr()
    assert g.main(["labels", "o/r", "5", "--add", "priority:high", "--write"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"


# ──── write_receipts ──────────────────────────────────────────────

# Offline regression tests for the mutation/verification boundary.


MARKER = '<!-- gh-identity:receipt-test -->'


BODY = MARKER + '\n日本語の本文 🐍'


URL = 'https://github.com/o/r/issues/1#issuecomment-7'


def observed(**changes):
    return dict({'id': 7, 'body': BODY, 'issue_url': g.API + '/repos/o/r/issues/1',
                 'html_url': URL}, **changes)


def post_with(responses):
    with patch.object(g, 'pages', return_value=[]), patch.object(g, 'request', side_effect=responses) as req:
        result = g.post_comment('o/r', 1, '日本語の本文 🐍', write=True, marker=MARKER)
    return result, req.call_args_list


@pytest.mark.parametrize('url', [URL, 'https://github.com/o/r/pull/1#issuecomment-7'])
def test_comment_verifies_exact_readback_and_real_newline(url):
    result, calls = post_with([{'id': 7}, observed(html_url=url)])
    assert result['status'] == result['verification_status'] == 'verified'
    assert result['mutation_status'] == 'succeeded'
    assert result['url'] == url
    assert [call.args[0] for call in calls] == ['POST', 'GET']
    assert calls[0].args[2] == {'body': BODY}
    assert calls[1].args[1] == 'repos/o/r/issues/comments/7'


@pytest.mark.parametrize('response', [None, {}, [], {'id': None}, {'id': True}, {'id': 0}, {'id': '7'}])
def test_invalid_post_identity_is_uncertain_without_retry(response):
    result, calls = post_with([response])
    assert result['status'] == 'mutation_uncertain'
    assert result['mutation_status'] == 'uncertain'
    assert len(calls) == 1


@pytest.mark.parametrize('response', [None, {}, observed(id=8), observed(id=True),
    observed(body=MARKER), observed(body='different'),
    observed(issue_url=g.API + '/repos/other/r/issues/1'),
    observed(html_url='https://github.com/o/r/issues/2#issuecomment-7')])
def test_mismatched_readback_preserves_write_without_verifying(response):
    result, calls = post_with([{'id': 7}, response])
    assert result['status'] == 'verification_mismatch'
    assert result['mutation_status'] == 'succeeded'
    assert result['id'] == 7
    assert not result['verified']
    assert len(calls) == 2


def test_readback_timeout_preserves_successful_write_and_never_reposts():
    result, calls = post_with([{'id': 7}, g.Error('timeout')])
    assert result['status'] == 'verification_failed'
    assert result['mutation_status'] == 'succeeded'
    assert result['id'] == 7
    assert [c.args[0] for c in calls] == ['POST', 'GET']


@pytest.mark.parametrize('uncertain,status', [(True, 'mutation_uncertain'), (False, 'mutation_failed')])
def test_comment_cli_failure_is_nonzero(uncertain, status):
    with patch.object(g, 'pages', return_value=[]), patch.object(g, 'request', side_effect=g.Error('timeout', uncertain)) as req:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = g.main(['comment', 'o/r', '1', 'body', '--operation-key', 'receipt-test', '--write'])
    assert rc == 1
    assert json.loads(out.getvalue())['status'] == status
    assert req.call_count == 1


@pytest.mark.parametrize('readback,status', [(g.Error('timeout'), 'verification_failed'), ({'value': 'wrong'}, 'verification_mismatch')])
def test_variable_cli_verification_failure_is_nonzero(readback, status):
    with patch.object(g, 'request', side_effect=[None, readback]):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = g.main(['variable-set', 'o/r', 'A', 'B', '--write'])
    receipt = json.loads(out.getvalue())
    assert rc == 1
    assert receipt['status'] == status
    assert receipt['mutation_status'] == 'succeeded'


@pytest.mark.parametrize('status,expected', [('planned', 0), ('already_exists', 0), ('verified', 0),
    ('mutation_failed', 1), ('mutation_uncertain', 1), ('verification_failed', 1),
    ('verification_mismatch', 1), ('unknown_future_status', 1)])
@pytest.mark.parametrize('command,api', [(['comment', 'o/r', '1', 'body'], 'post_comment'),
    (['variable-set', 'o/r', 'A', 'B'], 'set_variable')])
def test_cli_receipt_exit_contract(command, api, status, expected):
    with patch.object(g, api, return_value={'status': status}), contextlib.redirect_stdout(io.StringIO()):
        assert g.main(command) == expected


def test_comment_body_file_uses_existing_dedup_and_verification(tmp_path):
    path = tmp_path / 'note.md'
    body = 'Merged into #149.\nClosing source PR.\n'
    path.write_text(body, encoding='utf-8')
    marker = '<!-- gh-identity:consolidate-149-150 -->'
    observed = {'id': 7, 'body': marker + '\n' + body,
        'issue_url': g.API + '/repos/o/r/issues/150',
        'html_url': 'https://github.com/o/r/pull/150#issuecomment-7'}
    with patch.object(g, 'pages', return_value=[]), patch.object(g, 'request', side_effect=[{'id': 7}, observed]):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert g.main(['comment', 'o/r', '150', '--body-file', str(path),
                           '--operation-key', 'consolidate-149-150', '--write']) == 0
    assert json.loads(out.getvalue())['status'] == 'verified'


def test_comment_body_missing_or_conflicting_exits_two():
    for args in ([], ['text', '--body-file', 'x.md']):
        with patch.object(g, 'request') as request:
            with contextlib.redirect_stdout(io.StringIO()):
                assert g.main(['comment', 'o/r', '149', *args]) == 2
        request.assert_not_called()
