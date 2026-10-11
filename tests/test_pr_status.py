"""pr-status: one compact view of a PR head (observation, not a gate)."""
import json

import pytest
import gh_identity as g

SHA = "b" * 40


@pytest.fixture
def fake(monkeypatch):
    calls = {}
    monkeypatch.setattr(g, "pr", lambda r, n, **k: {"state": "open", "draft": True, "merged": False,
        "mergeable_state": "unstable", "head_sha": SHA, "head_ref": "work/x", "base_ref": "main", "url": "u"})

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
    assert fake == {"runs": (SHA, ("dynamic",)), "checks": SHA}
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
    assert capsys.readouterr().out.startswith("#7 open draft")
    assert fake["runs"] == (SHA, ("workflow_dispatch",))
