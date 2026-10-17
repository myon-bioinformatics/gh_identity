"""merge-readiness and pr-retarget: offline, with pr / review_observations / request patched."""
import json
from unittest.mock import patch

import pytest
import gh_identity as g

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
        if path == f"repos/o/r/commits/{HEAD}":
            return {"commit": {"committer": {"date": "2026-10-10T00:00:00Z"}}}
        if path == f"repos/o/r/compare/{OLD}...{HEAD}":
            return {"ahead_by": 2, "commits": [{"commit": {"message": "fix A\n\nbody"}}, {"commit": {"message": "fix B"}}]}
        if extra and path in extra:
            value = extra[path]
            if isinstance(value, Exception):
                raise value
            return value
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


def test_later_approval_supersedes_and_clear():
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-09T00:00:00Z", text="Blocking: x", rid=1),
                                review("gpt", "APPROVED", "2026-10-11T00:00:00Z", commit=HEAD, rid=2),
                                review("copilot-pull-request-reviewer[bot]", "COMMENTED", "2026-10-12T00:00:00Z", rid=3)])
    out = readiness(obs)
    assert out["findings"][0]["superseded_by_approval"] and out["verdict"] == "clear" and out["reasons"] == []
    assert "[superseded by approval]" in g.render_merge_readiness(out)


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


def retarget(over, compare, *, write=False, allow=False, after="main"):
    calls = []

    def handler(method, path, payload=None, **_):
        calls.append((method, path, payload))
        if method == "GET" and "/compare/" in path:
            if isinstance(compare, Exception):
                raise compare
            return compare
        if method == "PATCH":
            return {}
        raise AssertionError(path)
    heads = [head(**over), head(**{**over, "base_ref": after})]
    with patch.object(g, "pr", side_effect=heads), patch.object(g, "request", side_effect=handler):
        return g.pr_retarget("o/r", 7, "main", write=write, allow_unmerged=allow), calls


def test_pr_retarget_plans_refuses_and_writes():
    out, calls = retarget({"base_ref": "feat/parent"}, {"ahead_by": 0})
    assert out["status"] == "planned" and out["old_base_unmerged_commits"] == 0 and "warning" not in out
    assert calls == [("GET", "repos/o/r/compare/main...feat%2Fparent", None)]
    out, calls = retarget({"base_ref": "feat/parent"}, {"ahead_by": 0}, write=True)
    assert out["status"] == "verified" and calls[-1] == ("PATCH", "repos/o/r/pulls/7", {"base": "main"})
    assert g.render_pr_retarget(out) == "verified o/r#7 base feat/parent -> main"
    out, calls = retarget({"base_ref": "feat/parent"}, {"ahead_by": 3}, write=True)
    assert out["status"] == "refused_unmerged_base" and all(c[0] == "GET" for c in calls)
    assert "(feat/parent has 3 commit(s) not in main; they would join the diff)" in g.render_pr_retarget(out)
    out, _ = retarget({"base_ref": "gone"}, g.Error("not_found"), write=True, allow=True, after="gone")
    assert out["status"] == "verification_mismatch" and out["compare_error"] == "not_found"
    assert out["warning"] == "could not compare gone with main"


def test_pr_retarget_noops_and_validation():
    out, calls = retarget({}, {"ahead_by": 0})
    assert out["status"] == "already_satisfied" and calls == []
    out, _ = retarget({"state": "closed", "base_ref": "x"}, {"ahead_by": 0})
    assert out["status"] == "not_open"
    with pytest.raises(ValueError):
        g.pr_retarget("o/r", 7, " ")
    with pytest.raises(ValueError):
        g.pr_retarget("o/r", 0)


@pytest.mark.parametrize("text, labels", [
    ("**Should \u2014 re-pin first.**", ["Should"]), ("Must: add tests", ["Must"]),
    ("__Blocking__ \u2013 broken", ["Blocking"]), ("Should - rename", ["Should"]),
    ("Should\uff1a\u65e5\u672c\u8a9e", ["Should"]), ("Should-have list", []), ("Must 1 / Should 2", []),
])
def test_severity_labels_accept_dashes_and_must(text, labels):
    assert [x["label"] for x in g._explicit_severity(text)] == labels


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
    capsys.readouterr()
    with patch.object(g, "pr", return_value=head(base_ref="feat/p")), \
         patch.object(g, "request", return_value={"ahead_by": 1}):
        assert g.main(["pr-retarget", "o/r", "7", "--text"]) == 0
        assert capsys.readouterr().out.startswith("planned o/r#7 base feat/p -> main")
        assert g.main(["pr-retarget", "o/r", "7", "--write"]) == 1
        assert json.loads(capsys.readouterr().out)["status"] == "refused_unmerged_base"
