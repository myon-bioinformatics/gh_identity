"""checks: several SHAs and a uniq -c style --text digest of what needs attention."""
import gh_identity as g


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
