"""merge-readiness: offline, with pr / review_observations / request patched."""
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


def test_when_invalid_timestamp():
    assert g._when("not-a-date") is None
    obs = observations(reviews=[review("gpt", "COMMENTED", "not-a-date", commit=HEAD, text="Should: x")])
    out = readiness(obs)
    assert not out["findings"][0]["after_head"] and not out["findings"][0]["superseded_by_approval"]


def test_picks_strongest_explicit_label_on_one_line():
    text = "Must: one\nShould: two"
    obs = observations(reviews=[review("gpt", "COMMENTED", "2026-10-11T00:00:00Z", commit=HEAD, text=text)])
    out = readiness(obs)
    assert out["findings"][0]["label"] == "Must"


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
