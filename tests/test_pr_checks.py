"""pr-checks: several PR heads polled together, Copilot checks dropped, per-request budgets."""
from unittest.mock import patch

import pytest
import gh_identity as g

SHA = "a" * 40


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
