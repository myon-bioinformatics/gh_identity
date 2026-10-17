"""fleet-status / fleet-checks / fleet-pr: one row per local clone."""
import subprocess

import gh_identity as g


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@e", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _clone(tmp_path):
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    _git(work, "commit", "-q", "--allow-empty", "-m", "a")
    _git(work, "push", "-q", "origin", "main")
    return bare, work


def test_status_plans_then_writes_tracking_for_a_pushed_branch(tmp_path):
    bare, work = _clone(tmp_path)
    _git(work, "switch", "-q", "-c", "feat")
    _git(work, "commit", "-q", "--allow-empty", "-m", "b")
    _git(work, "push", "-q", str(bare), "feat")  # pushed without tracking: the "unpushed" false alarm
    row = g.fleet_status([work])["rows"][0]
    assert row["upstream"] is None and row["ok"] is False
    planned = g.fleet_status([work], fix_tracking=True)["rows"][0]["tracking_fix"]
    assert planned["status"] == "planned" and ["git", "fetch", "--no-tags", "origin", "feat"] in planned["commands"]
    fixed = g.fleet_status([work], fix_tracking=True, write=True)
    row = fixed["rows"][0]
    assert row["tracking_fix"]["status"] == "verified" and row["upstream"] == "origin/feat"
    assert (row["ahead"], row["behind"], row["ok"]) == (0, 0, True) and fixed["ok"]
    (work / "x").write_text("1")
    assert g.render_fleet_status(g.fleet_status([work])).endswith("changed=0 untracked=1")


def test_status_reports_missing_remote_branch_and_errors(tmp_path):
    _, work = _clone(tmp_path)
    _git(work, "switch", "-q", "-c", "local-only")
    assert g.fleet_status([work], fix_tracking=True)["rows"][0]["tracking_fix"] == {"status": "remote_branch_missing"}
    out = g.fleet_status([tmp_path / "not-a-repo"])
    assert out["rows"][0]["ok"] is False and "error" in out["rows"][0]
    assert g.render_fleet_status(out).startswith("error not-a-repo")


def _github_clone(tmp_path):
    _, work = _clone(tmp_path)
    _git(work, "remote", "set-url", "origin", "https://github.com/o/r.git")
    return work


def test_checks_steps_and_annotations(tmp_path):
    work = _github_clone(tmp_path)

    def fake(method, path, payload=None, **_):
        if "/check-runs?" in path:
            return {"total_count": 3, "check_runs": [
                {"id": 1, "name": "python", "status": "completed", "conclusion": "failure"},
                {"id": 2, "name": "node", "status": "completed", "conclusion": "success"},
                {"id": 3, "name": "copilot-pull-request-reviewer", "status": "completed", "conclusion": "failure"}]}, {}
        if path.endswith("actions/jobs/1"):
            return {"steps": [{"name": "Run pytest", "conclusion": "failure"}, {"name": "ok", "conclusion": "success"}]}
        if path.endswith("check-runs/1/annotations"):
            return [{"annotation_level": "failure", "path": ".github", "start_line": 9, "message": "Process completed with exit code 1."},
                    {"annotation_level": "failure", "path": "tests/x.py", "start_line": 3, "message": "assert  1 == 2"},
                    {"annotation_level": "notice", "path": ".github", "start_line": 1, "message": "probe summary"}]
        raise AssertionError(path)

    def page_fake(method, path, payload=None, **kw):
        result = fake(method, path, payload, **kw)
        return result[0] if isinstance(result, tuple) else result
    with g.request_stub(page_fake):
        out = g.fleet_checks([work], steps=True)
    row = out["rows"][0]
    assert out["state"] == "failed" and row["count"] == 2
    assert row["failed"] == [{"id": 1, "name": "python", "conclusion": "failure", "failed_steps": ["Run pytest"],
                              "annotations": [{"level": "failure", "path": "tests/x.py", "line": 3, "message": "assert 1 == 2"}]}]
    text = g.render_fleet_checks(out)
    assert "x python (failure)" in text and "step: Run pytest" in text and "probe summary" not in text


def test_pr_plans_reports_existing_and_writes(tmp_path):
    work = _github_clone(tmp_path)
    _git(work, "switch", "-q", "-c", "feat")
    calls = []

    def fake(method, path, payload=None, **kw):
        calls.append((method, path, payload, kw.get("mutating")))
        if method == "GET":
            return []
        return {"number": 9, "html_url": "https://github.com/o/r/pull/9"}

    with g.request_stub(fake):
        assert g.fleet_pr([work], "t", "body")["rows"][0]["status"] == "planned"
        written = g.fleet_pr([work], "t", "body", write=True)
    assert written["status"] == "verified" and written["rows"][0]["number"] == 9
    assert calls[-1][:2] == ("POST", "repos/o/r/pulls") and calls[-1][2]["head"] == "feat" and calls[-1][3] is True
    with g.request_stub(lambda method, path, payload=None, **kw: [{"number": 4, "html_url": "u4"}]):
        existing = g.fleet_pr([work], "t", "body", write=True)
    assert existing["rows"][0]["status"] == "already_exists"
    assert g.render_fleet_pr(existing) == "already_exists o/r #4 u4"


def test_cli(tmp_path, capsys):
    _, work = _clone(tmp_path)
    assert g.main(["fleet-status", str(work), "--text"]) == 0
    assert capsys.readouterr().out.startswith("ok   work main@")
    body = tmp_path / "b.md"
    body.write_text("x")
    assert g.main(["fleet-pr", str(work), "--title", "t", "--body-file", str(body)]) == 1  # no GitHub origin
