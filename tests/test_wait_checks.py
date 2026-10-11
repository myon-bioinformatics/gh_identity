"""wait-checks: first/last snapshot, per-check transitions, ignore, timeout."""
import json

import pytest
import gh_identity as g

SHA = "a" * 40


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
