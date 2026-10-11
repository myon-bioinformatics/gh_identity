"""Gate-style head observations: runs for one head, checks, and PR staleness."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import gh_identity as ghi

SHA = "a" * 40


def _query(path):
    return parse_qs(urlsplit("https://api.github.test/" + path).query)


def _run(i, conclusion="success", status="completed", sha=SHA, name="CI"):
    return {"id": i, "run_attempt": 1, "workflow_id": 7, "name": name, "status": status,
            "conclusion": conclusion, "head_sha": sha, "head_branch": "b", "event": "pull_request"}


def test_run_history_filters_server_side_and_still_verifies(monkeypatch):
    seen = []

    def page(path, **kwargs):
        seen.append(path)
        # A wrong-head row from the server must still be dropped client-side.
        return {"workflow_runs": [_run(1), _run(2, sha="b" * 40)]}, {}

    monkeypatch.setattr(ghi, "_page", page)
    out = ghi.run_history("o/r", head_sha=SHA, event="pull_request")
    assert _query(seen[0])["head_sha"] == [SHA] and _query(seen[0])["event"] == ["pull_request"]
    assert [x["run_id"] for x in out["runs"]] == [1]
    assert (out["state"], out["by_conclusion"]) == ("green", {"success": 1})


@pytest.mark.parametrize(("rows", "state"), [
    ([], "missing"),
    ([_run(1), _run(2, conclusion=None, status="in_progress")], "pending"),
    ([_run(1), _run(2, conclusion="failure")], "failed"),
    ([_run(1), _run(2, conclusion="skipped")], "green"),
])
def test_run_history_state(monkeypatch, rows, state):
    monkeypatch.setattr(ghi, "_page", lambda path, **k: ({"workflow_runs": rows}, {}))
    assert ghi.run_history("o/r", head_sha=SHA)["state"] == state


def test_workflow_discovery_sends_head_sha_to_the_api(monkeypatch):
    seen = []
    monkeypatch.setattr(ghi, "workflow", lambda *a, **k: {"id": 7, "path": ".github/workflows/ci.yml"})
    monkeypatch.setattr(ghi, "_page", lambda path, **k: (seen.append(path) or {"workflow_runs": [_run(1)]}, {}))
    ghi.workflow_run_discovery("o/r", 7, head_sha=SHA)
    assert _query(seen[0])["head_sha"] == [SHA]


def test_summarize_checks_counts_by_conclusion():
    rows = [{"name": "a", "status": "completed", "conclusion": "success"},
            {"name": "b", "status": "completed", "conclusion": "skipped"},
            {"name": "c", "status": "in_progress", "conclusion": None}]
    assert ghi.summarize_checks(rows, 3)["by_conclusion"] == {"in_progress": 1, "skipped": 1, "success": 1}


@pytest.mark.parametrize(("state", "code"), [("green", 0), ("pending", 1), ("failed", 1), ("incomplete", 1)])
def test_checks_cli_exit_code_follows_state(monkeypatch, capsys, state, code):
    monkeypatch.setattr(ghi, "checks_for_sha", lambda *a, **k: {"state": state})
    assert ghi.main(["checks", "o/r", SHA]) == code
    assert json.loads(capsys.readouterr().out)["state"] == state


def test_head_runs_cli_rejects_a_non_sha(capsys):
    assert ghi.main(["head-runs", "o/r", "main"]) == 2


@pytest.mark.parametrize(("state", "stale", "code"), [("green", False, 0), ("green", True, 1), ("pending", False, 1)])
def test_observe_pr_cli_fails_on_stale_head_or_non_green_checks(monkeypatch, capsys, state, stale, code):
    monkeypatch.setattr(ghi, "observe_pr", lambda *a, **k: {"checks": {"state": state}, "stale": stale})
    assert ghi.main(["observe-pr", "o/r", "1"]) == code


def test_head_runs_can_exclude_dynamic_review_runs_and_says_so(monkeypatch):
    rows = [_run(1), {**_run(2, conclusion="failure"), "event": "dynamic", "name": "Running Copilot Code Review"}]
    monkeypatch.setattr(ghi, "_page", lambda path, **k: ({"workflow_runs": rows}, {}))
    assert ghi.run_history("o/r", head_sha=SHA)["state"] == "failed"   # default unchanged
    out = ghi.run_history("o/r", head_sha=SHA, exclude_events=("dynamic",))
    assert (out["state"], out["excluded_count"], out["filters"]["exclude_events"]) == ("green", 1, ["dynamic"])
