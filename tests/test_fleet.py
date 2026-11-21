"""🌐 Across repositories and accounts: shared fleet checks (vendor/fleet_check.py), fleet status, inventories, repository facts, vendor/pin/parent, subtree sync, trees, repo-overview.

Sections (each was one file; `-k` selects inside them): fleet_check, fleet_status, fleet_ignore_reuse, fleet_wait, repository_inventory, repository_facts, public_github, parent_ref, pin_audit, vendor_tools, repository_metadata, repo_name_files, duplicate_files, subtree_push_guards, subtree_split, subtree_stale_split, subtree_sync, subtree_sync_run, tree, scripts_repo_overview.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch
from vendor import fleet_check as fleet
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tomllib
import unittest
import urllib.error

import pytest

from conftest import FIXTURES, ROOT, SHA, _git, _git_tester

import gh_identity as g


# ──── fleet_check ─────────────────────────────────────────────────

# Fleet contracts through the parent's shared checks (vendor/fleet_check.py): locks, pins, CI setup, benchmark, Node entry.


PARENT_WORKFLOW_PIN = "9e3da7ae1846e9604ebc24bb5178f7de094a4e77"


# Node sources and the shared checks stay on accepted upstream commits with exact bytes.
ACCEPTED = {
    "vendor/py_launch.cjs": "384f5dd532076062f7833038764e9a62322c387c",
    "vendor/fleet_check.py": "6c29503cf17eda856766ddfdf4d66a5b0965a8b1",
}


TOOL, ENV_VAR = "gh_identity.py", "GH_IDENTITY_PYTHON"


CASES = [{"id": "help", "args": ["--help"], "rc": 0}, {"id": "bogus", "args": ["--bogus"], "rc": 2}, {"id": "vendor-lock-missing", "args": ["vendor-lock-check", "--root", ".", "--lock", "no-such-lock.json"], "rc": 2}]


needs_node = pytest.mark.skipif(fleet.node() is None, reason="Node 26+ not available (set NODE)")


def job_text(path, name):
    return fleet.workflow_job(ROOT, name, path.rsplit("/", 1)[-1])


def test_vendor_lock_matches_checked_in_bytes_and_accepted_commits():
    rows = fleet.check_lock(ROOT, commits=ACCEPTED)
    launcher = rows["vendor/py_launch.cjs"]
    assert (launcher["repository"], launcher["source"]) == (fleet.PARENT, "py_launch.cjs")


def test_parent_workflow_uses_refs_are_reviewed_commit():
    # The .vendor-sync-tools checkout resolves the shared vendor tool at runtime; it is not a pin.
    assert set(fleet.parent_refs(ROOT, skip=(".vendor-sync-tools",))) == {PARENT_WORKFLOW_PIN}


class TestCIPythonSetup(unittest.TestCase):
    def test_python_support_metadata_and_ci_agree(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["requires-python"], ">=3.11")
        self.assertEqual(lock["requires-python"], project["project"]["requires-python"])
        self.assertEqual(project["project"]["dependencies"], [])
        self.assertIsNot(project["tool"]["uv"].get("package"), False)
        self.assertEqual(project["tool"]["uv"]["dependency-groups"]["dev"]["requires-python"], ">=3.11")
        expected = ["3.11", "3.12", "3.13", "3.14", "3.x"]
        for path, job, selector in (
            (".github/workflows/ci.yml", "test", "python-version"),
        ):
            with self.subTest(workflow=path):
                body = job_text(path, job)
                match = re.search(r"(?m)^        " + re.escape(selector) + r": (\[[^\n]+\])$", body)
                self.assertIsNotNone(match, "Missing Python version matrix")
                self.assertEqual(json.loads(match.group(1)), expected)
        # One matrix runs both lanes: the stdlib-only core under -S, then the full pytest suite.
        test = job_text(".github/workflows/ci.yml", "test")
        self.assertIn("python -S -m unittest discover -s tests -p test_core.py -v", test)
        self.assertLess(test.index("test_core.py"), test.index("python -m pytest"))
        self.assertFalse((ROOT / ".github/workflows/runtime-compatibility.yml").exists())

    def assert_latest_setup(self, path, job_name, selector):
        job = job_text(path, job_name)
        steps = re.split(r"(?m)^      - ", job)
        setup = next(step for step in steps if "uses: astral-sh/setup-uv@" in step)
        self.assertIn('python-version: "${{ ' + selector + " == '3.x' && '3' || " + selector + ' }}"', setup)
        self.assertIn("env:\n", setup)
        self.assertIn("UV_PYTHON_PREFERENCE: ${{ " + selector + " == '3.x' && 'only-managed' || 'managed' }}", setup)
        self.assertIn("activate-environment: true", setup)
        self.assertIn('"3.x"', job)

    def test_pytest_latest_is_managed_before_activation(self):
        self.assert_latest_setup(".github/workflows/ci.yml", "test", "matrix.python-version")
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("junit-py3.x/junit.xml", workflow)
        self.assertIn("name: junit-py${{ matrix.python-version }}", workflow)


@pytest.mark.parametrize("optimized", [False, True], ids=["normal", "optimized"])
@pytest.mark.parametrize("mismatch", [False, True], ids=["same", "mismatch"])
def test_benchmark_parity_in_both_interpreter_modes(optimized, mismatch):
    patch = "g['ignore_filter_indexed'] = lambda *args: ['different']" if mismatch else ""
    result = fleet.run_main(ROOT, "tests/test_core.py", patch, optimized=optimized)
    if mismatch:
        assert (result.returncode, result.stdout) == (1, ""), result.stderr
        assert "result mismatch" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("{"), result.stdout


def test_ci_executes_node_parity_instead_of_skipping():
    job = fleet.workflow_job(ROOT, "test")
    assert "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1" in job
    assert "node-version: '26'" in job
    assert "Require supported Node in parity CI" in job


def test_no_per_tool_wrapper_and_readme_documents_the_direct_command():
    assert not (ROOT / "gh_identity.cjs").exists()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"node vendor/py_launch.cjs --env-var {ENV_VAR} -- {TOOL}" in readme
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert pkg["engines"]["node"] == ">=26"


@needs_node
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_node_matches_python(case):
    via_node, via_python = fleet.node_parity(ROOT, TOOL, ENV_VAR, *case["args"])
    assert via_python[0] == case["rc"]
    assert via_node == via_python


@needs_node
def test_vendored_launcher_keeps_its_contract():
    fleet.launcher_contract(ROOT, ENV_VAR)


# ──── fleet_status ────────────────────────────────────────────────

# fleet-status / fleet-checks / fleet-pr: one row per local clone.


def _clone(tmp_path):
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    _git_tester(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git_tester(tmp_path, "clone", "-q", str(bare), str(work))
    _git_tester(work, "commit", "-q", "--allow-empty", "-m", "a")
    _git_tester(work, "push", "-q", "origin", "main")
    return bare, work


def test_status_plans_then_writes_tracking_for_a_pushed_branch(tmp_path):
    bare, work = _clone(tmp_path)
    _git_tester(work, "switch", "-q", "-c", "feat")
    _git_tester(work, "commit", "-q", "--allow-empty", "-m", "b")
    _git_tester(work, "push", "-q", str(bare), "feat")  # pushed without tracking: the "unpushed" false alarm
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
    _git_tester(work, "switch", "-q", "-c", "local-only")
    assert g.fleet_status([work], fix_tracking=True)["rows"][0]["tracking_fix"] == {"status": "remote_branch_missing"}
    out = g.fleet_status([tmp_path / "not-a-repo"])
    assert out["rows"][0]["ok"] is False and "error" in out["rows"][0]
    assert g.render_fleet_status(out).startswith("error not-a-repo")


def _github_clone(tmp_path):
    _, work = _clone(tmp_path)
    _git_tester(work, "remote", "set-url", "origin", "https://github.com/o/r.git")
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
    _git_tester(work, "switch", "-q", "-c", "feat")
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


# ──── fleet_ignore_reuse ──────────────────────────────────────────

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


class IgnoreReuseTests(unittest.TestCase):
    def test_once_per_repository_and_same_filtering(self):
        class Names:
            calls=0
            def __iter__(self):
                self.calls+=1
                return iter(['bot'])
        names=Names()
        checks=[{'id':i,'name':'bot' if i%2 else 'test','status':'completed','conclusion':'success'} for i in range(100)]
        with patch.object(g,'remote_repository',return_value={'full_name':'o/r'}), \
             patch.object(g,'checkout_head',return_value={'sha':'a'*40}), \
             patch.object(g,'checks_for_sha',return_value={'checks':checks}) as fetch:
            result=g.fleet_checks(['one','two'],ignore=names)
        self.assertEqual(names.calls,2)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual([r['count'] for r in result['rows']],[50,50])
        self.assertTrue(all(not r['failed'] for r in result['rows']))


# ──── fleet_wait ──────────────────────────────────────

# fleet-checks --wait: each poll runs in its own budget, so waiting longer than one poll's timeout works.


def run(argv, states):
    seen = []

    def fake(roots, *, ignore, steps, transport, timeout):
        seen.append((g._BUDGET.get(), timeout))
        state = states.pop(0)
        return {"schema": "gh-identity-fleet-checks/1", "state": state, "rows": [], "observed_at": "t"}
    with patch.object(g, "fleet_checks", side_effect=fake), patch.object(g.time, "sleep"):
        return g.main(argv), seen


def test_wait_polls_outside_one_shared_scope():
    code, seen = run(["fleet-checks", ".", "--wait", "600", "--interval", "1"], ["pending", "pending", "green"])
    assert code == 0 and len(seen) == 3
    assert all(budget is None for budget, _ in seen)  # the bounded fleet_checks opens one scope per poll
    assert {timeout for _, timeout in seen} == {30.0}


def test_wait_passes_timeout_per_poll_and_rejects_other_limits():
    code, seen = run(["fleet-checks", ".", "--wait", "600", "--timeout", "45"], ["failed"])
    assert code == 1 and seen == [(None, 45.0)]
    assert g.main(["fleet-checks", ".", "--wait", "60", "--max-pages", "3"]) == 2


def test_wait_equals_form_uses_per_poll_budget():
    code, seen = run(["fleet-checks", ".", "--wait=600", "--timeout", "45", "--interval", "1"], ["pending", "green"])
    assert code == 0 and len(seen) == 2
    assert all(budget is None for budget, _ in seen)
    assert {timeout for _, timeout in seen} == {45.0}


def test_without_wait_keeps_one_scope():
    code, seen = run(["fleet-checks", "."], ["green"])
    assert code == 0 and seen[0][0] is not None


@pytest.mark.parametrize("argv,per_poll", [
    (["fleet-checks", ".", "--wait", "0", "--wait", "600", "--interval", "1"], True),
    (["fleet-checks", ".", "--wait", "600", "--wait", "0"], False),
])
def test_wait_uses_argparse_not_string_heuristics(argv, per_poll):
    states = ["green"] if not per_poll else ["pending", "green"]
    code, seen = run(argv, states)
    assert code == 0
    if per_poll:
        assert len(seen) >= 2 and all(budget is None for budget, _ in seen)
    else:
        assert len(seen) == 1 and seen[0][0] is not None


def test_wai_typo_rejected_with_exit_2():
    with patch.object(g, "fleet_checks") as fake:
        assert g.main(["fleet-checks", ".", "--wai", "600"]) == 2
        fake.assert_not_called()


def test_fleet_checks_wait_value_invalid_and_missing():
    assert g._fleet_checks_wait_value(["fleet-checks", ".", "--wait", "abc"]) == 0.0
    assert g._fleet_checks_wait_value(["fleet-checks", ".", "--wait=abc"]) == 0.0
    assert g._fleet_checks_wait_value(["fleet-checks", ".", "--wait"]) == 0.0
    assert g._fleet_checks_wait_value(["wait-checks", "o/r", "a"]) is None
    assert g._fleet_checks_wait_value(["fleet-checks", ".", "--wait", "600"]) == 600.0


# ──── repository_inventory ────────────────────────────────────────

def _repos():
    return [
        {"name":"a","full_name":"demo/a","private":False,"archived":False,"size":40,"updated_at":"2026-01-01"},
        {"name":"b","full_name":"demo/b","private":False,"archived":False,"size":10,"updated_at":"2026-01-02"},
        {"name":"hidden","full_name":"demo/hidden","private":True,"archived":False,"size":100},
        {"name":"old","full_name":"demo/old","private":False,"archived":True,"size":100}
    ]


def test_inventory_public_unarchived_and_size_sort(monkeypatch):
    monkeypatch.setattr(g,"_page",lambda *a,**k:(_repos(),{}))
    out=g.repository_inventory("demo",fields=("name","size-kb"),sort="size-kb",order="desc")
    assert [r["name"] for r in out["repositories"]]==["a","b"]
    assert out["complete"] and out["sort_scope"]=="complete"
    assert all(r["run_count_status"]=="not_requested" for r in out["repositories"])


def test_inventory_opt_in_run_counts(monkeypatch):
    monkeypatch.setattr(g,"_page",lambda *a,**k:(_repos(),{}))
    calls=[]
    def request(method,path,**kwargs):
        calls.append(path)
        return {"total_count": 5 if "/a/" in path else 20}
    monkeypatch.setattr(g,"request",request)
    out=g.repository_inventory("demo",fields="name,run-count",sort="run-count",order="desc")
    assert [r["name"] for r in out["repositories"]]==["b","a"]
    assert len(calls)==2
    assert all(r["run_count_status"]=="available" for r in out["repositories"])


def test_inventory_permission_not_zero(monkeypatch):
    monkeypatch.setattr(g,"_page",lambda *a,**k:(_repos(),{}))
    def denied(*a,**k):raise g.Error("permission_or_rate_limit")
    monkeypatch.setattr(g,"request",denied)
    out=g.repository_inventory("demo",fields="run-count")
    assert all(r["run_count"] is None for r in out["repositories"])
    assert all(r["run_count_status"]=="permission_or_rate_limit" for r in out["repositories"])


def test_inventory_partial_limit(monkeypatch):
    monkeypatch.setattr(g,"_page",lambda *a,**k:(_repos(),{}))
    out=g.repository_inventory("demo",max_repos=1)
    assert out["truncated"] and out["limit_reason"]=="max_repos"
    assert out["sort_scope"]=="partial"


@pytest.mark.parametrize("kw",[
    {"fields":"bogus"},{"sort":"bogus"},{"order":"reverse"},
    {"max_repos":0},{"max_pages":0}
])
def test_inventory_invalid_options(kw):
    with pytest.raises(ValueError):
        g.repository_inventory("demo",**kw)


@pytest.mark.parametrize("metric,field,order,expected",[
    ("run-count","run_count","asc",["zero","five","twenty","missing"]),
    ("run-count","run_count","desc",["twenty","five","zero","missing"]),
    ("size-kb","size_kb","asc",["zero","five","twenty","missing"]),
    ("size-kb","size_kb","desc",["twenty","five","zero","missing"]),
    ("updated-at","updated_at","asc",["zero","five","twenty","missing"]),
    ("updated-at","updated_at","desc",["twenty","five","zero","missing"]),
])
def test_inventory_missing_metrics_sort_last(monkeypatch,metric,field,order,expected):
    sizes={"zero":0,"five":5,"twenty":20,"missing":None}
    dates={"zero":"2026-01-01T00:00:00Z","five":"2026-02-01T00:00:00Z",
           "twenty":"2026-03-01T00:00:00Z","missing":None}
    fixtures=[{"name":name,"full_name":"demo/"+name,"private":False,
               "archived":False,"size":size,"updated_at":dates[name]}
              for name,size in sizes.items()]
    monkeypatch.setattr(g,"_page",lambda *args,**kwargs:(fixtures,{}))
    calls=[]
    def get_count(method,path,**kwargs):
        calls.append(path)
        name=path.split("/")[2]
        if name=="missing":
            raise g.Error("permission_or_rate_limit")
        return {"total_count":sizes[name]}
    monkeypatch.setattr(g,"request",get_count)
    out=g.repository_inventory("demo",fields=(metric,),sort=metric,order=order)
    assert [row["name"] for row in out["repositories"]] == (
        ["zero","five","twenty","missing"] if metric=="size-kb" and order=="asc"
        else ["twenty","five","zero","missing"] if metric=="size-kb" and order=="desc"
        else expected
    )
    assert out["complete"] and not out["truncated"]
    assert out["sort_scope"]=="complete"
    if metric=="run-count":
        assert len(calls)==4
        assert out["repositories"][-1]["run_count"] is None
        assert out["repositories"][-1]["run_count_status"]=="permission_or_rate_limit"
        assert next(r for r in out["repositories"] if r["name"]=="zero")["run_count"]==0
    else:
        assert calls==[]


def test_inventory_missing_metrics_with_bounded_partial_results(monkeypatch):
    fixtures=[{"name":name,"full_name":"demo/"+name,"private":False,
               "archived":False,"size":size,"updated_at":None}
              for name,size in (("missing",None),("known",9),("extra",3))]
    monkeypatch.setattr(g,"_page",lambda *a,**k:(fixtures,{}))
    out=g.repository_inventory("demo",fields="size-kb",sort="size-kb",
                                 order="desc",max_repos=2)
    assert [r["name"] for r in out["repositories"]]==["known","missing"]
    assert out["count"]==2 and out["truncated"] and not out["complete"]
    assert out["limit_reason"]=="max_repos" and out["sort_scope"]=="partial"


@pytest.mark.parametrize("order,expected",[
    ("asc",["early","offset","late","invalid","missing"]),
    ("desc",["late","offset","early","invalid","missing"]),
])
def test_inventory_dates_normalized_and_malformed_last(monkeypatch,order,expected):
    timestamps={"early":"2026-01-01T00:00:00Z",
                "offset":"2026-01-01T10:00:00+09:00",
                "late":"2026-01-01T02:00:00Z",
                "invalid":"not-an-iso-date","missing":None}
    payload=[{"name":name,"full_name":"demo/"+name,"private":False,
              "archived":False,"size":0,"updated_at":date}
             for name,date in timestamps.items()]
    monkeypatch.setattr(g,"_page",lambda *a,**k:(payload,{}))
    out=g.repository_inventory("demo",sort="updated-at",order=order)
    assert [r["name"] for r in out["repositories"]]==expected
    assert out["complete"] and out["sort_scope"]=="complete"


@pytest.mark.parametrize("metric",["run-count","size-kb","updated-at"])
@pytest.mark.parametrize("order",["asc","desc"])
def test_inventory_equal_values_tiebreak_by_name(monkeypatch,metric,order):
    payload=[{"name":name,"full_name":"demo/"+name,"private":False,
              "archived":False,"size":4,"updated_at":"2026-01-01T00:00:00Z"}
             for name in ("zeta","alpha","beta")]
    monkeypatch.setattr(g,"_page",lambda *a,**k:(payload,{}))
    monkeypatch.setattr(g,"request",lambda *a,**k:{"total_count":4})
    out=g.repository_inventory("demo",sort=metric,order=order)
    assert [r["name"] for r in out["repositories"]]==["alpha","beta","zeta"]


@pytest.mark.parametrize("value",[
    123, {}, [], "2026-01-01T00:00:00", "not-a-date",
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
])
@pytest.mark.parametrize("order",["asc","desc"])
def test_inventory_unusable_dates_are_missing(monkeypatch,value,order):
    payload=[{"name":name,"full_name":"demo/"+name,"private":False,
              "archived":False,"updated_at":date}
             for name,date in (("missing",value),("known","2026-01-01T00:00:00Z"))]
    monkeypatch.setattr(g,"_page",lambda *a,**k:(payload,{}))
    out=g.repository_inventory("demo",sort="updated-at",order=order)
    assert [r["name"] for r in out["repositories"]]==["known","missing"]


@pytest.mark.parametrize("order",["asc","desc"])
def test_inventory_equal_instants_tiebreak_by_name(monkeypatch,order):
    payload=[{"name":name,"full_name":"demo/"+name,"private":False,
              "archived":False,"updated_at":date}
             for name,date in (("zeta","2026-01-01T00:00:00Z"),
                               ("alpha","2026-01-01T09:00:00+09:00"))]
    monkeypatch.setattr(g,"_page",lambda *a,**k:(payload,{}))
    out=g.repository_inventory("demo",sort="updated-at",order=order)
    assert [r["name"] for r in out["repositories"]]==["alpha","zeta"]


def test_inventory_exact_repo_limit_is_conservatively_partial(monkeypatch):
    monkeypatch.setattr(g,"_page",lambda *a,**k:(_repos()[:2],{}))
    out=g.repository_inventory("demo",max_repos=2)
    assert out["count"]==2 and out["truncated"] and not out["complete"]
    assert out["limit_reason"]=="max_repos" and out["sort_scope"]=="partial"


# ──── repository_facts ────────────────────────────────────────────

# Facts about repositories of any account: licence, stars, health, releases, quota.


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _repo(**over):
    d = {"id": 7, "full_name": "up/lib", "default_branch": "main", "visibility": "public", "archived": False,
         "html_url": "https://github.com/up/lib", "description": "a lib", "homepage": "",
         "license": {"spdx_id": "MIT", "name": "MIT License"}, "stargazers_count": 120, "forks_count": 9,
         "subscribers_count": 4, "open_issues_count": 3, "topics": ["cli", "python"], "language": "Python",
         "size": 88, "created_at": _ago(900), "updated_at": _ago(2), "pushed_at": _ago(3), "fork": False,
         "is_template": False, "disabled": False, "owner": {"type": "Organization"}}
    d.update(over)
    return d




def test_repo_reports_licence_and_stars_from_one_request():
    calls = []
    with g.request_stub({"repos/up/lib": _repo()}, calls):
        out = g.repository("up/lib")
    assert calls == ["repos/up/lib"]
    assert (out["license"], out["license_name"], out["stars"], out["forks"]) == ("MIT", "MIT License", 120, 9)
    assert (out["watchers"], out["open_issues"], out["topics"], out["language"]) == (4, 3, ["cli", "python"], "Python")
    assert out["homepage"] is None and out["owner_type"] == "Organization" and out["parent"] is None
    # The keys older callers read are unchanged.
    assert (out["schema"], out["id"], out["default_branch"], out["visibility"], out["archived"]) == (
        "gh-identity-repository/1", 7, "main", "public", False)


def test_repo_without_licence_or_counts_is_none_not_an_error():
    with g.request_stub({"repos/up/lib": {"id": 1, "license": None, "stargazers_count": "many"}}):
        out = g.repository("up/lib")
    assert out["license"] is None and out["stars"] is None and out["topics"] == []


def test_repo_include_all_sections():
    routes = {
        "repos/up/lib": _repo(),
        "repos/up/lib/releases?per_page=5": [
            {"tag_name": "v2.0.0-rc1", "prerelease": True, "published_at": _ago(1)},
            {"tag_name": "v1.4.0", "published_at": _ago(40), "assets": [{"name": "lib.whl", "size": 10, "download_count": 5}]}],
        "repos/up/lib/tags?per_page=5": [{"name": "v1.4.0", "commit": {"sha": SHA}}],
        "repos/up/lib/releases?per_page=10": [{"tag_name": "v1.4.0", "published_at": _ago(40)}],
        "repos/up/lib/tags?per_page=10": [{"name": "v1.4.0", "commit": {"sha": SHA}}],
        "repos/up/lib/commits/main": {"sha": SHA, "commit": {"committer": {"date": _ago(3)}}},
        f"repos/up/lib/commits/{SHA}/check-runs?per_page=100&page=1": {
            "total_count": 1, "check_runs": [{"id": 1, "name": "ci", "status": "completed", "conclusion": "success"}]},
        f"repos/up/lib/commits/{SHA}/status": {"total_count": 0, "state": "pending"},
        "repos/up/lib/languages": {"Python": 900, "Shell": 100},
        "repos/up/lib/contributors?anon=1&per_page=100&page=1": [
            {"login": "alice", "contributions": 50}, {"type": "Anonymous", "email": "x@example.invalid", "contributions": 2}],
        "repos/up/lib/community/profile": {"health_percentage": 71, "files": {"readme": {"url": "u"}, "license": {"key": "mit"}, "contributing": None}},
    }
    with g.request_stub(routes):
        out = g.repository("up/lib", include=("all",))
    health = out["health"]
    assert (health["status"], health["days_since_push"], health["latest_release"], health["days_since_release"]) == ("active", 3, "v1.4.0", 40)
    assert (health["head_sha"], health["ci_state"], health["open_issues"]) == (SHA, "green", 3)
    assert out["fork_diff"] == {"fork": False}
    assert out["languages"] == {"total_bytes": 1000, "languages": [
        {"name": "Python", "bytes": 900, "percent": 90.0}, {"name": "Shell", "bytes": 100, "percent": 10.0}]}
    assert out["contributors"] == {"count": 2, "complete": True, "accounts": 1, "top": [{"login": "alice", "contributions": 50}]}
    assert "example.invalid" not in json.dumps(out)
    assert out["community"] == {"health_percentage": 71, "files": {"contributing": False, "license": True, "readme": True}}
    assert out["releases"]["latest"]["tag"] == "v1.4.0" and out["releases"]["tags"] == [{"name": "v1.4.0", "sha": SHA}]
    text = g.render_repository(out)
    assert text.splitlines()[0].startswith("📦 up/lib  ⭐ 120  🍴 9  📄 MIT  🗣 Python")
    assert "💓 active · push 3d · release v1.4.0 40d · CI green · issues 3" in text
    assert "🧩 Python 90.0% · Shell 10.0%" in text and "👥 2  alice(50)" in text
    assert "contributing ✗ license ✓ readme ✓" in text


@pytest.mark.parametrize("over,stale_days,expected", [
    ({"archived": True}, 365, "archived"),
    ({"disabled": True}, 365, "disabled"),
    ({"pushed_at": _ago(400)}, 365, "stale"),
    ({"pushed_at": _ago(400)}, 500, "active"),
    ({"pushed_at": None}, 365, "unknown"),
])
def test_health_status(over, stale_days, expected):
    # No release, no readable head: the status still comes from the repository object.
    with g.request_stub({"repos/up/lib": _repo(**over), "repos/up/lib/releases?per_page=5": [], "repos/up/lib/tags?per_page=5": []}):
        health = g.repository("up/lib", include="health", stale_days=stale_days)["health"]
    assert health["status"] == expected
    assert health["latest_release"] is None and health["ci_error"] == "not_found_or_inaccessible"


def test_health_without_checks_is_none_not_pending():
    routes = {"repos/up/lib": _repo(), "repos/up/lib/releases?per_page=5": [], "repos/up/lib/tags?per_page=5": [],
              "repos/up/lib/commits/main": {"sha": SHA},
              f"repos/up/lib/commits/{SHA}/check-runs?per_page=100&page=1": {"total_count": 0, "check_runs": []}}
    routes[f"repos/up/lib/commits/{SHA}/status"] = {"total_count": 0, "state": "pending"}
    with g.request_stub(routes):
        assert g.repository("up/lib", include="health")["health"]["ci_state"] == "none"


def test_unreadable_section_is_reported_not_raised():
    routes = {"repos/up/lib": _repo(), "repos/up/lib/languages": g.Error("permission_or_rate_limit")}
    with g.request_stub(routes):
        out = g.repository("up/lib", include=("languages", "community"))
    assert out["languages"] == {"error": "permission_or_rate_limit"}
    assert out["community"] == {"error": "not_found_or_inaccessible"}
    assert "⚠️ languages: permission_or_rate_limit" in g.render_repository(out)


def test_fork_diff_compares_with_parent_default_branch():
    fork = _repo(full_name="me/lib", fork=True, default_branch="feat/x",
                 parent={"full_name": "up/lib", "default_branch": "main"})
    calls = []
    routes = {"repos/me/lib": fork,
              "repos/up/lib/compare/main...me:feat%2Fx?per_page=1": {"ahead_by": 2, "behind_by": 5, "status": "diverged"}}
    with g.request_stub(routes, calls):
        out = g.repository("me/lib", include=("fork-diff",))
    assert out["parent"] == "up/lib" and out["fork"] is True
    assert out["fork_diff"] == {"fork": True, "parent": "up/lib", "parent_branch": "main", "branch": "feat/x",
                                "ahead_by": 2, "behind_by": 5, "status": "diverged"}
    assert "🔱 fork of up/lib" in g.render_repository(out) and "🔱 up/lib +2 -5" in g.render_repository(out)


@pytest.mark.parametrize("kw", [{"include": ("stars",)}, {"stale_days": 0}, {"stale_days": True}])
def test_repo_rejects_bad_arguments(kw):
    with g.request_stub({"repos/up/lib": _repo()}), pytest.raises(ValueError):
        g.repository("up/lib", **kw)


def test_releases_latest_skips_drafts_and_prereleases():
    routes = {"repos/up/lib/releases?per_page=2": [
                  {"tag_name": "v3-draft", "draft": True}, {"tag_name": "v2-rc", "prerelease": True}],
              "repos/up/lib/tags?per_page=2": [{"name": "v2-rc", "commit": {"sha": SHA}}, {"name": "v1"}]}
    with g.request_stub(routes):
        out = g.releases("up/lib", max_items=2)
    assert out["latest"] is None and [x["tag"] for x in out["releases"]] == ["v3-draft", "v2-rc"]
    assert out["tags"] == [{"name": "v2-rc", "sha": SHA}, {"name": "v1", "sha": None}]
    assert out["complete"] is False  # both lists filled the limit: more may exist
    assert g.render_releases(out).splitlines()[:3] == ["🏷️ up/lib  latest none", "📝 v3-draft  ?  📎 0", "🧪 v2-rc  ?  📎 0"]
    with pytest.raises(ValueError):
        g.releases("up/lib", max_items=0)


def test_rate_limit_tells_quota_from_permission():
    reset = int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp())
    body = {"resources": {"core": {"limit": 60, "remaining": 0, "used": 60, "reset": reset},
                          "search": {"limit": 10, "remaining": 10, "used": 0, "reset": reset}}}
    with g.request_stub({"rate_limit": body}):
        out = g.rate_limit()
        caps = g.capabilities(rate_limit=True)
    assert out["authenticated"] is False and out["exhausted"] == ["core"]
    assert 0 < out["resources"]["core"]["resets_in_seconds"] <= 1800
    assert caps["rate_limit"]["resources"]["search"]["remaining"] == 10
    body["resources"]["core"].update(limit=5000, remaining=4990)
    with g.request_stub({"rate_limit": body}):
        assert g.rate_limit()["authenticated"] is True


def test_capabilities_stays_offline_by_default_and_survives_a_blocked_quota_read():
    def refuse(*a, **k):
        raise AssertionError("no request expected")
    with g.request_stub(refuse):
        assert "rate_limit" not in g.capabilities()
    with g.request_stub({"rate_limit": g.Error("permission_or_rate_limit")}):
        assert g.capabilities(rate_limit=True)["rate_limit"] == {"error": "permission_or_rate_limit"}


def _rows():
    return [
        {"name": "a", "full_name": "acme/a", "private": False, "archived": False, "size": 1, "stargazers_count": 5,
         "forks_count": 1, "license": {"spdx_id": "MIT"}, "language": "Python", "pushed_at": "2026-03-01T00:00:00Z"},
        {"name": "b", "full_name": "acme/b", "private": False, "archived": True, "size": 2, "stargazers_count": 50,
         "forks_count": 0, "license": None, "language": None, "pushed_at": "2026-01-01T00:00:00Z", "fork": True},
        {"name": "c", "full_name": "acme/c", "private": False, "archived": False, "size": 3, "stargazers_count": 20,
         "forks_count": 4, "license": {"spdx_id": "Apache-2.0"}, "language": "Go", "pushed_at": "2026-02-01T00:00:00+09:00"},
    ]


def test_inventory_new_fields_and_archived_opt_in():
    with g.request_stub({"users/acme/repos?per_page=100&page=1": _rows()}):
        out = g.repository_inventory("acme", fields="name,stars,license", sort="stars", order="desc")
        everything = g.repository_inventory("acme", sort="stars", order="desc", include_archived=True)
        by_push = g.repository_inventory("acme", sort="pushed-at", include_archived=True)
        by_licence = g.repository_inventory("acme", sort="license", include_archived=True)
    assert [(r["name"], r["stars"], r["license"]) for r in out["repositories"]] == [("c", 20, "Apache-2.0"), ("a", 5, "MIT")]
    assert out["kind"] == "users" and out["include_archived"] is False
    assert [(r["name"], r["archived"], r["fork"]) for r in everything["repositories"]] == [("b", True, True), ("c", False, False), ("a", False, False)]
    assert [r["name"] for r in by_push["repositories"]] == ["b", "c", "a"]
    assert [r["name"] for r in by_licence["repositories"]] == ["c", "a", "b"]  # no licence sorts last


def test_inventory_and_repos_fall_back_to_organisation():
    calls = []
    routes = {"orgs/acme/repos?per_page=100&page=1": _rows(), "orgs/acme/repos?sort=full_name&per_page=100&page=1": _rows()}
    with g.request_stub(routes, calls):
        inventory = g.repository_inventory("acme")
        listed = g.repositories("acme")
    assert inventory["kind"] == "orgs" and [r["name"] for r in inventory["repositories"]] == ["a", "c"]
    assert listed["kind"] == "orgs" and listed["count"] == 3
    assert listed["repositories"][0]["license"] == "MIT" and listed["repositories"][1]["stars"] == 50
    assert calls[0].startswith("users/acme/") and calls[1].startswith("orgs/acme/")
    with g.request_stub(routes), pytest.raises(g.Error) as user_only:
        g.repository_inventory("acme", kind="user")
    assert user_only.value.code == "not_found_or_inaccessible"
    with pytest.raises(ValueError):
        g.repositories("acme", kind="team")


def test_owner_repositories_rows_carry_brief_facts():
    with g.request_stub({"users/acme/repos?type=owner&sort=full_name&per_page=100&page=1": _rows()}):
        out = g.list_owner_repositories("acme", include_archived=True, include_forks=True)
    row = {r["full_name"]: r for r in out["repositories"]}["acme/b"]
    assert (row["archived"], row["fork"], row["stars"], row["license"]) == (True, True, 50, None)


def test_cli_repo_text_and_releases(capsys):
    routes = {"repos/up/lib": _repo(archived=True), "repos/up/lib/languages": {"Python": 1},
              "repos/up/lib/releases?per_page=3": [], "repos/up/lib/tags?per_page=3": []}
    with g.request_stub(routes):
        assert g.main(["repo", "up/lib", "--include", "languages", "--text"]) == 0
        text = capsys.readouterr().out
        assert g.main(["releases", "up/lib", "--limit", "3"]) == 0
        listed = json.loads(capsys.readouterr().out)
    assert "📄 MIT" in text and "🗃 archived" in text and "🧩 Python 100.0%" in text
    assert listed["schema"] == "gh-identity-releases/1" and listed["complete"] is True
    # choices are declared in argparse: an unknown section is a usage error, before any request.
    assert g.main(["repo", "--include", "stars", "up/lib"]) == 2
    with g.request_stub(routes):
        assert g.main(["repo", "--include", "languages", "--include", "community", "up/lib"]) == 0
    assert json.loads(capsys.readouterr().out)["community"] == {"error": "not_found_or_inaccessible"}


@pytest.mark.parametrize("answer,expected", [
    ({"license": {"spdx_id": "BSD-3-Clause"}}, "BSD-3-Clause"),
    ({"license": {"spdx_id": "NOASSERTION"}}, "NOASSERTION"),
    ({"license": None}, None),
    (g.Error("permission_or_rate_limit"), None),
])
def test_license_spdx_is_best_effort(answer, expected):
    with g.request_stub({f"repos/up/lib/license?ref={SHA}": answer}):
        assert g._license_spdx("up/lib", SHA) == expected


@pytest.mark.parametrize("check_state,legacy_state,count,expected", [
    (None, "failure", 1, "failed"), ("success", "failure", 1, "failed"),
    (None, "success", 1, "green"), ("success", "pending", 1, "pending"),
    ("failure", "success", 1, "failed"), (None, "pending", 0, "none"),
    ("success", "success", 101, "green"), ("success", "error", 1, "failed"),
])
def test_health_includes_combined_legacy_status(check_state, legacy_state, count, expected):
    checks = [] if check_state is None else [{"id": 1, "name": "ci", "status": "completed", "conclusion": check_state}]
    routes = {"repos/up/lib": _repo(), "repos/up/lib/releases?per_page=5": [],
              "repos/up/lib/tags?per_page=5": [], "repos/up/lib/commits/main": {"sha": SHA},
              f"repos/up/lib/commits/{SHA}/check-runs?per_page=100&page=1": {"total_count": len(checks), "check_runs": checks},
              f"repos/up/lib/commits/{SHA}/status": {"total_count": count, "state": legacy_state}}
    with g.request_stub(routes):
        assert g.repository("up/lib", include="health")["health"]["ci_state"] == expected


@pytest.mark.parametrize("response", [g.Error("permission_or_rate_limit"), {"total_count": True, "state": "success"}, {"total_count": 1, "state": "mystery"}])
def test_unreadable_legacy_status_does_not_claim_green(response):
    routes = {"repos/up/lib": _repo(), "repos/up/lib/releases?per_page=5": [],
              "repos/up/lib/tags?per_page=5": [], "repos/up/lib/commits/main": {"sha": SHA},
              f"repos/up/lib/commits/{SHA}/check-runs?per_page=100&page=1": {"total_count": 1, "check_runs": [{"id": 1, "status": "completed", "conclusion": "success"}]},
              f"repos/up/lib/commits/{SHA}/status": response}
    with g.request_stub(routes):
        health = g.repository("up/lib", include="health")["health"]
    assert health["ci_state"] is None
    assert "ci_error" in health


def test_capabilities_rate_limit_preserves_transport_and_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(g, "rate_limit", lambda **kw: calls.append(kw) or {})
    g.capabilities(rate_limit=True, transport="urllib", timeout=7)
    assert calls == [{"transport": "urllib", "timeout": 7}]


def test_capabilities_cli_forwards_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(g, "rate_limit", lambda **kw: calls.append(kw) or {})
    assert g.main(["--transport", "urllib", "capabilities", "--rate-limit"]) == 0
    assert calls == [{"transport": "urllib", "timeout": 30}]


# ──── public_github ───────────────────────────────────────────────

# Public GitHub URL helpers (Atom branch feed, raw.githubusercontent.com).


@pytest.fixture(autouse=False)
def no_live_http(monkeypatch):
    """Any unmocked HTTP/socket call fails immediately, including raw reads."""
    def blocked(*args, **kwargs):
        pytest.fail("unexpected live HTTP/socket access")
    monkeypatch.setattr(g.urllib.request, "urlopen", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(g, "_process", blocked)


def _atom(*shas):
    entries = "".join(f'<entry><id>tag:github.com,2008:Grit::Commit/{sha}</id></entry>' for sha in shas)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'


ATOM = _atom("a" * 40, "b" * 40)


@pytest.mark.usefixtures('no_live_http')
def test_public_github_branch_commits_and_head():
    with patch.object(g, "_public_github_get", return_value=ATOM.encode()):
        commits = g.public_github_branch_commits("me/up", "main")
        assert commits == ["a" * 40, "b" * 40]
        assert g.public_github_branch_head("me/up") == "a" * 40


@pytest.mark.usefixtures('no_live_http')
def test_public_github_raw_bytes_builds_url():
    captured = {}
    def fake(url, **kw):
        captured["url"] = url
        return b"data\n"
    with patch.object(g, "_public_github_get", side_effect=fake):
        assert g.public_github_raw_bytes("me/up", "pkg/mod.py", "c" * 40) == b"data\n"
    assert captured["url"] == (
        "https://raw.githubusercontent.com/me/up/" + "c" * 40 + "/pkg/mod.py")


@pytest.mark.usefixtures('no_live_http')
def test_public_github_repository_check_ok():
    body = b"x = 1\n"
    def fake(url, **kw):
        if url.endswith(".atom"):
            return ATOM.encode()
        return body
    with patch.object(g, "_public_github_get", side_effect=fake):
        out = g.public_github_repository_check("me/up", "b" * 40, source="mod.py")
    assert out["ok"] is True and out["atom"]["in_recent"] is True
    assert out["source"]["blob_sha"] == g.git_blob_sha(body)


@pytest.mark.usefixtures('no_live_http')
def test_public_github_repository_check_missing_commit_in_feed():
    with patch.object(g, "_public_github_get", return_value=ATOM.encode()):
        out = g.public_github_repository_check("me/up", "f" * 40)
    assert out["ok"] is False and out["atom"]["in_recent"] is False


@pytest.mark.usefixtures('no_live_http')
def test_public_github_repository_check_invalid_commit():
    with pytest.raises(ValueError):
        g.public_github_repository_check("me/up", "main")


COMMIT = "c" * 40


def _api_file_payload(body):
    meta = {"type": "file", "sha": g.git_blob_sha(body), "size": len(body)}
    blob = {"encoding": "base64", "content": base64.b64encode(body).decode()}
    return meta, blob


@pytest.mark.usefixtures('no_live_http')
def test_remote_file_raw_success_skips_api():
    body = b"from raw\n"
    with patch.object(g, "public_github_raw_bytes", return_value=body) as raw, \
         patch.object(g, "_url") as req, \
         patch.object(g, "gh_available", return_value=False):
        assert g._remote_file("o/up", "mod.py", COMMIT) == body
    raw.assert_called_once()
    req.assert_not_called()


@pytest.mark.usefixtures('no_live_http')
def test_remote_file_branch_ref_skips_raw_uses_api():
    body = b"api only\n"
    meta, blob = _api_file_payload(body)
    with patch.object(g, "public_github_raw_bytes") as raw, \
         patch.object(g, "request", side_effect=[meta, blob]) as req:
        assert g._remote_file("o/up", "mod.py", "main") == body
    raw.assert_not_called()
    assert req.call_count == 2


@pytest.mark.usefixtures('no_live_http')
@pytest.mark.parametrize("raw_code", [
    "permission_or_rate_limit", "rate_limit", "server_error", "operation_timeout", "transport_error", "not_found_or_inaccessible",
])
def test_remote_file_raw_errors_fallback_to_api(raw_code):
    body = b"v\n"
    meta, blob = _api_file_payload(body)
    with patch.object(g, "public_github_raw_bytes", side_effect=g.Error(raw_code)), \
         patch.object(g, "_url", side_effect=[meta, blob]) as req, \
         patch.object(g, "gh_available", return_value=False):
        assert g._remote_file("o/up", "mod.py", COMMIT) == body
    assert req.call_count == 2


@pytest.mark.usefixtures('no_live_http')
def test_remote_file_raw_404_api_403_is_permission():
    with patch.object(g, "public_github_raw_bytes", side_effect=g.Error("not_found_or_inaccessible")), \
         patch.object(g, "_url", side_effect=g.Error("permission_or_rate_limit")), \
         patch.object(g, "gh_available", return_value=False):
        with pytest.raises(g.Error) as exc:
            g._remote_file("o/up", "mod.py", COMMIT)
    assert exc.value.code == "permission_or_rate_limit"


@pytest.mark.usefixtures('no_live_http')
def test_remote_file_raw_404_api_404_is_not_found():
    with patch.object(g, "public_github_raw_bytes", side_effect=g.Error("not_found_or_inaccessible")), \
         patch.object(g, "_url", side_effect=g.Error("not_found_or_inaccessible")), \
         patch.object(g, "gh_available", return_value=False):
        with pytest.raises(g.Error) as exc:
            g._remote_file("o/up", "mod.py", COMMIT)
    assert exc.value.code == "not_found_or_inaccessible"


@pytest.mark.usefixtures('no_live_http')
def test_public_github_raw_bytes_url_encodes_ref():
    captured = {}
    def fake(url, **kw):
        captured["url"] = url
        return b"x"
    ref = "a" * 40
    with patch.object(g, "_public_github_get", side_effect=fake):
        g.public_github_raw_bytes("me/up", "f.py", ref)
    assert f"/{ref}/f.py" in captured["url"]
    with patch.object(g, "_public_github_get", side_effect=fake):
        g.public_github_raw_bytes("me/up", "f.py", "feature/foo")
    assert "/feature%2Ffoo/f.py" in captured["url"]


def _http_error(code):
    return urllib.error.HTTPError("https://example.test/", code, "msg", {}, io.BytesIO(b""))


@pytest.mark.usefixtures('no_live_http')
@pytest.mark.parametrize("status,expected", [
    (404, "not_found_or_inaccessible"),
    (403, "permission_or_rate_limit"),
    (401, "permission_or_rate_limit"),
    (429, "rate_limit"),
    (500, "server_error"),
    (502, "server_error"),
    (418, "invalid_response"),
])
def test_public_github_get_http_status_mapping(status, expected):
    with patch("urllib.request.urlopen", side_effect=_http_error(status)):
        with pytest.raises(g.Error) as exc:
            g._public_github_get("https://github.com/o/r/commits/main.atom")
    assert exc.value.code == expected


@pytest.mark.usefixtures('no_live_http')
def test_public_github_get_timeout_is_operation_timeout():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(TimeoutError())):
        with pytest.raises(g.Error) as exc:
            g._public_github_get("https://raw.githubusercontent.com/o/r/sha/f")
    assert exc.value.code == "operation_timeout"


@pytest.mark.usefixtures('no_live_http')
def test_public_github_get_200_returns_body():
    payload = b"ok"
    response = MagicMock()
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    response.getcode = MagicMock(return_value=200)
    response.read = MagicMock(return_value=payload)
    with patch("urllib.request.urlopen", return_value=response):
        assert g._public_github_get("https://example.test/x") == payload


@pytest.mark.usefixtures('no_live_http')
@pytest.mark.parametrize("transport", ["auto", "gh", "urllib"])
@pytest.mark.parametrize("injection", ["stub", "replacement"])
def test_remote_file_injected_request_wins_over_successful_raw(transport, injection):
    body = b"injected private file\n"
    meta, blob = _api_file_payload(body)
    handler = MagicMock(side_effect=[meta, blob])
    context = g.request_stub(handler) if injection == "stub" else patch.object(g, "request", handler)
    with patch.object(g, "public_github_raw_bytes", return_value=b"wrong raw body") as raw, context:
        assert g._remote_file("o/private", "mod.py", COMMIT, transport, 7) == body
    raw.assert_not_called()
    assert handler.call_count == 2
    for call in handler.call_args_list:
        assert call.kwargs["transport"] == transport
        assert call.kwargs["timeout"] == 7


@pytest.mark.usefixtures('no_live_http')
@pytest.mark.parametrize("transport,backend", [("gh", "_gh"), ("urllib", "_url")])
def test_remote_file_explicit_transport_never_tries_raw(transport, backend):
    body = b"selected transport\n"
    meta, blob = _api_file_payload(body)
    with patch.object(g, "public_github_raw_bytes", return_value=b"wrong") as raw, \
         patch.object(g, backend, side_effect=[meta, blob]) as send:
        assert g._remote_file("o/private", "mod.py", COMMIT, transport) == body
    raw.assert_not_called()
    assert send.call_count == 2


@pytest.mark.usefixtures('no_live_http')
@pytest.mark.parametrize("code", ["authentication_required", "permission_or_rate_limit", "transport_error", "operation_timeout"])
@pytest.mark.parametrize("injection", ["stub", "replacement"])
def test_remote_file_injected_errors_do_not_escape_to_network(code, injection):
    handler = MagicMock(side_effect=g.Error(code))
    context = g.request_stub(handler) if injection == "stub" else patch.object(g, "request", handler)
    with context, pytest.raises(g.Error) as exc:
        g._remote_file("o/private", "mod.py", COMMIT)
    assert exc.value.code == code
    handler.assert_called_once()


@pytest.mark.usefixtures('no_live_http')
def test_remote_file_stub_reset_restores_public_raw():
    body = b"api\n"
    meta, blob = _api_file_payload(body)
    with g.request_stub(MagicMock(side_effect=[meta, blob])):
        assert g._remote_file("o/private", "mod.py", COMMIT) == body
    with patch.object(g, "public_github_raw_bytes", return_value=b"public") as raw:
        assert g._remote_file("o/up", "mod.py", COMMIT) == b"public"
    raw.assert_called_once()


@pytest.mark.usefixtures('no_live_http')
@pytest.mark.parametrize("code", ["file_too_large", "bytes_limit", "invalid_response"])
def test_remote_file_nonfallback_raw_errors_propagate(code):
    with patch.object(g, "public_github_raw_bytes", side_effect=g.Error(code)), \
         patch.object(g, "_url") as api:
        with pytest.raises(g.Error) as exc:
            g._remote_file("o/up", "mod.py", COMMIT)
    assert exc.value.code == code
    api.assert_not_called()


@pytest.mark.usefixtures('no_live_http')
@pytest.mark.parametrize("raw_code", ["not_found_or_inaccessible", "permission_or_rate_limit"])
def test_remote_file_private_repository_uses_authenticated_api_after_raw_failure(raw_code):
    body = b"private content\n"
    meta, blob = _api_file_payload(body)
    replies = [(0, json.dumps(value).encode(), b"") for value in (meta, blob)]
    with patch.object(g, "public_github_raw_bytes", side_effect=g.Error(raw_code)) as raw, \
         patch.object(g, "gh_available", return_value=False), \
         patch.object(g, "token", return_value="fixture-token"), \
         patch.object(g, "_process", side_effect=replies) as worker:
        assert g._remote_file("o/private", "mod.py", COMMIT) == body
    raw.assert_called_once()
    assert worker.call_count == 2
    for call in worker.call_args_list:
        spec = json.loads(call.args[1])
        assert spec["headers"]["Authorization"] == "Bearer fixture-token"
        assert spec["url"].startswith(g.API + "/repos/o/private/")


# ──── parent_ref ──────────────────────────────────────────────────

# parent-pr.ref.json, first-parent pins, child-heads.json, redaction, enrollment progress (fake transport).


GOOD = {"schema": "parent-pr-ref/1", "repository": "me/parent", "pull_number": 78, "repository_id": 638801160,
        "optional": {"note": "x"}, "commit": "b" * 40}


DATA = b"x = 1\n"


def write(root, rel, obj):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)


def entry(dest, data, repository="me/up", source="m.py", commit="c" * 40):
    return {"repository": repository, "ref": commit, "commit": commit, "source": source, "destination": dest,
            "blob_sha": g.git_blob_sha(data), "sha256": hashlib.sha256(data).hexdigest()}


@pytest.fixture
def child(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "m.py").write_bytes(DATA)
    write(tmp_path, "vendor.lock.json", {"schema": "vendor-lock/1", "files": [entry("vendor/m.py", DATA)]})
    return tmp_path


# --- local parent_ref validation -------------------------------------------------

def test_parent_ref_missing_is_not_failure_by_default(child):
    out = g.vendor_lock_check(child)
    assert out["parent_ref"]["status"] == "missing" and out["ok"] is True


def test_parent_ref_templates_first_and_ok(child):
    write(child, "templates/parent-pr.ref.json", GOOD)
    write(child, "parent-pr.ref.json", {**GOOD, "commit": "0" * 40})
    out = g.vendor_lock_check(child)
    assert out["parent_ref"]["path"] == "templates/parent-pr.ref.json" and out["parent_ref"]["status"] == "ok"
    assert out["ok"] is True


@pytest.mark.parametrize("mutate,problem", [
    (lambda r: r.update(commit="0" * 40), "commit"),
    (lambda r: r.update(commit="B" * 40), "commit"),
    (lambda r: r.update(pull_number=0), "pull_number"),
    (lambda r: r.update(repository_id="638801160"), "repository_id"),
    (lambda r: r.update(schema="parent-pr-ref/2"), "schema"),
    (lambda r: r.update(repository="parent"), "repository"),
    (lambda r: r.update(extra=1), "unknown_fields"),
])
def test_parent_ref_invalid_fails_check(child, mutate, problem):
    ref = dict(GOOD)
    mutate(ref)
    write(child, "parent-pr.ref.json", ref)
    out = g.vendor_lock_check(child)
    assert out["parent_ref"]["status"] == "invalid" and problem in out["parent_ref"]["problems"] and out["ok"] is False


def test_parent_ref_explicit_path_required(child):
    assert g.vendor_lock_check(child, parent_ref="nope.json")["ok"] is False
    write(child, "parent-pr.ref.json", "{not json")
    assert g.vendor_lock_check(child)["parent_ref"]["problems"] == ["not_json"]


# --- first-parent ------------------------------------------------------------------

def commit(sha, *parents):
    return {"sha": sha, "parents": [{"sha": p} for p in parents]}


# main: M3 -> M2 -> M1 (first parents); M2 merges PR commits X2 -> X1 (second parent)
M1, M2, M3, X1, X2 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40)


HISTORY = [commit(M3, M2), commit(M2, M1, X2), commit(X2, X1), commit(X1, M1), commit(M1)]


def history_fake(method, path, **kw):
    if path == "repos/me/up":
        return {"default_branch": "main", "id": 1}
    if path.startswith("repos/me/up/commits?sha=main&per_page=100&page="):
        return HISTORY if path.endswith("page=1") else []
    raise g.Error("not_found_or_inaccessible")


@pytest.mark.parametrize("target,expected", [(M3, True), (M2, True), (M1, True), (X2, False), (X1, False), ("9" * 40, False)])
def test_first_parent_contains(target, expected):
    with patch.object(g, "request", side_effect=history_fake):
        assert g._first_parent_contains("me/up", target) is expected


def test_first_parent_undetermined_when_history_is_long():
    long = [commit(f"{i:040x}", f"{i + 1:040x}") for i in range(100)]
    with patch.object(g, "request", side_effect=lambda m, p, **k: {"default_branch": "main"} if p == "repos/me/up" else long):
        assert g._first_parent_contains("me/up", "f" * 40, max_pages=2) is None


def test_verify_remote_first_parent(child):
    lock = {"schema": "vendor-lock/1", "files": [entry("vendor/m.py", DATA, commit=X2)]}
    write(child, "vendor.lock.json", lock)
    with patch.object(g, "_remote_file", return_value=DATA), patch.object(g, "request", side_effect=history_fake):
        out = g.vendor_lock_verify_remote(child, first_parent=True)
    assert out["files"][0]["status"] == "ok" and out["files"][0]["first_parent"] is False and out["ok"] is False


# --- remote parent_ref --------------------------------------------------------------

def parent_fake(pr):
    def fake(method, path, **kw):
        if path == "repos/me/parent":
            return {"id": 638801160, "default_branch": "main"}
        if path == "repos/me/parent/pulls/78":
            return pr
        raise g.Error("not_found_or_inaccessible")
    return fake


@pytest.mark.parametrize("pr,status,problems", [
    ({"merged_at": "t", "merge_commit_sha": "b" * 40, "base": {"ref": "main"}, "head": {"sha": "e" * 40}}, "ok", []),
    ({"state": "open", "head": {"sha": "b" * 40}, "base": {"ref": "main"}}, "ok", []),
    ({"merged_at": "t", "merge_commit_sha": "e" * 40, "base": {"ref": "main"}, "head": {"sha": "b" * 40}}, "mismatch", ["commit_mismatch"]),
    ({"merged_at": "t", "merge_commit_sha": "b" * 40, "base": {"ref": "dev"}}, "mismatch", ["not_merged_into_default_branch"]),
    ({"state": "closed", "head": {"sha": "b" * 40}}, "mismatch", ["commit_mismatch", "pull_closed_unmerged"]),
])
def test_verify_parent_ref(pr, status, problems):
    with patch.object(g, "request", side_effect=parent_fake(pr)):
        out = g.verify_parent_ref(GOOD)
    assert out["status"] == status and out["problems"] == problems


def test_verify_parent_ref_repository_id():
    pr = {"state": "open", "head": {"sha": "b" * 40}}
    with patch.object(g, "request", side_effect=parent_fake(pr)):
        assert g.verify_parent_ref({**GOOD, "repository_id": 1})["problems"] == ["repository_id_mismatch"]


def test_verify_remote_with_parent_ref(child):
    write(child, "templates/parent-pr.ref.json", GOOD)
    pr = {"merged_at": "t", "merge_commit_sha": "b" * 40, "base": {"ref": "main"}}
    with patch.object(g, "_remote_file", return_value=DATA), patch.object(g, "request", side_effect=parent_fake(pr)):
        out = g.vendor_lock_verify_remote(child, check_parent_ref=True)
    assert out["parent_ref"]["remote"]["status"] == "ok" and out["ok"] is True


# --- child-heads.json ------------------------------------------------------------------

def test_load_child_heads(tmp_path):
    write(tmp_path, "h.json", {"schema": "child-heads/1", "ref": "main",
                               "heads": {"me/a": {"sha": "a" * 40, "repository_id": 1}}})
    assert g.load_child_heads(tmp_path / "h.json") == {"me/a": "a" * 40}
    write(tmp_path, "bad.json", {"schema": "child-heads/1", "heads": {"me/a": {"sha": "main"}}})
    with pytest.raises(ValueError):
        g.load_child_heads(tmp_path / "bad.json")


def test_duplicate_files_uses_heads():
    seen = []
    def fake(method, path, **kw):
        seen.append(path)
        if path == "repos/me/a":
            return {"visibility": "public", "default_branch": "main"}
        if path.startswith("repos/me/a/commits/"):
            return {"sha": path.rsplit("/", 1)[1]}
        if path.startswith("repos/me/a/git/trees/"):
            return {"tree": [], "truncated": False}
        raise g.Error("not_found_or_inaccessible")
    with patch.object(g, "request", side_effect=fake):
        out = g.duplicate_files("me", ["a"], heads={"me/a": "a" * 40})
    assert "repos/me/a/commits/" + "a" * 40 in seen and out["scanned"]["me/a"]["from_heads"] is True


# --- redaction ---------------------------------------------------------------------------

def test_public_targets_redacts_names():
    def fake(method, path, **kw):
        return {"repos/me/pub": {"visibility": "public", "default_branch": "main"},
                "repos/me/hidden": {"visibility": "private", "private": True},
                "repos/me/old": {"visibility": "public", "archived": True}}.get(path) or (_ for _ in ()).throw(g.Error("not_found_or_inaccessible"))
    with patch.object(g, "request", side_effect=fake):
        targets, redacted = g._public_targets("me", ["pub", "hidden", "old", "gone"])
    assert [t["name"] for t in targets] == ["pub"] and redacted == 3


# --- enrollment progress -------------------------------------------------------------------

def test_enrollment_progress():
    current = b"new\n"
    def lock(files):
        return {"encoding": "base64", "content": base64.b64encode(json.dumps({"schema": "vendor-lock/1", "files": files}).encode()).decode()}
    enrolled = entry("vendor/gh_identity.py", b"old\n", repository="me/up", source="gh_identity.py", commit=X2)
    fresh = entry("vendor/gh_identity.py", current, repository="me/up", source="gh_identity.py", commit=M3)
    table = {
        "repos/me/up": {"default_branch": "main", "visibility": "public"},
        "repos/me/up/commits/main": {"sha": M3},
        "repos/me/a/commits/main": {"sha": "a" * 40},
        "repos/me/b/commits/main": {"sha": "b" * 40},
        "repos/me/c/commits/main": {"sha": "c" * 40},
        "repos/me/a/git/trees/" + "a" * 40 + "?recursive=1": {"tree": [{"path": "vendor.lock.json", "type": "blob", "mode": "100644", "sha": "0" * 40}]},
        "repos/me/b/git/trees/" + "b" * 40 + "?recursive=1": {"tree": [{"path": "vendor.lock.json", "type": "blob", "mode": "100644", "sha": "0" * 40}]},
        "repos/me/c/git/trees/" + "c" * 40 + "?recursive=1": {"tree": []},
        "repos/me/a/contents/vendor.lock.json?ref=" + "a" * 40: lock([enrolled]),
        "repos/me/b/contents/vendor.lock.json?ref=" + "b" * 40: lock([fresh]),
    }
    def fake(method, path, **kw):
        if path in table:
            return table[path]
        if path.startswith("repos/me/up/commits?sha=main"):
            return HISTORY if path.endswith("page=1") else []
        raise g.Error("not_found_or_inaccessible")
    repos = [{"name": n, "default_branch": "main", "owner": {"login": "me"}} for n in ("up", "a", "b", "c")] + \
            [{"name": "secret", "private": True, "visibility": "private", "owner": {"login": "me"}}]
    with patch.object(g, "pages", return_value=repos), patch.object(g, "request", side_effect=fake), \
         patch.object(g, "_remote_file", return_value=current):
        out = g.enrollment_progress("me", provider="me/up", source="gh_identity.py")
    rows = {r["repository"]: r for r in out["repositories"]}
    assert set(rows) == {"me/a", "me/b", "me/c"} and "secret" not in json.dumps(out)
    assert rows["me/a"]["enrolled"] and rows["me/a"]["stale"] is True and rows["me/a"]["first_parent"] is False
    assert rows["me/b"]["stale"] is False and rows["me/b"]["first_parent"] is True
    assert rows["me/c"]["enrolled"] is False
    assert out["counts"] == {"repositories": 3, "enrolled": 2, "not_enrolled": 1, "stale": 1, "not_first_parent": 1,
                             "first_parent_unknown": 0, "errors": 0, "redacted": 0}


# --- subtree mainline ------------------------------------------------------------------------

needs_subtree = pytest.mark.skipif(subprocess.run(["git", "subtree", "-h"], capture_output=True).returncode == 1,
                                   reason="git subtree not installed")


@needs_subtree
def test_subtree_sync_mainline(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@e", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a").write_text("a\n")
    git("add", "-A"); git("commit", "-q", "-m", "one")
    git("switch", "-q", "-c", "topic")
    (tmp_path / "sub" / "b").write_text("b\n")
    git("add", "-A"); git("commit", "-q", "-m", "topic")
    topic = git("rev-parse", "HEAD")
    git("switch", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "merge", "topic")
    assert g.subtree_sync_status(tmp_path, "sub", tmp_path, mainline="main")["source_on_first_parent"] is True
    out = g.subtree_sync_status(tmp_path, "sub", tmp_path, revision=topic, mainline="main")
    assert out["source_on_first_parent"] is False
    assert g.subtree_sync_status(tmp_path, "sub", tmp_path)["source_on_first_parent"] is None


# ──── pin_audit ───────────────────────────────────────────────────

# pin-audit: commit pins checked with plain git against local remotes (no network, no token).


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@e", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    up = tmp_path / "remotes" / "me" / "up"
    up.mkdir(parents=True)
    git(up, "init", "-q", "-b", "main")
    (up / "tool.py").write_text("v1\n")
    (up / "other.py").write_text("o\n")
    git(up, "add", "-A"); git(up, "commit", "-q", "-m", "one")
    first = git(up, "rev-parse", "HEAD")
    git(up, "switch", "-q", "-c", "pr")
    (up / "other.py").write_text("o2\n")
    git(up, "commit", "-qam", "pr work")
    pr_head = git(up, "rev-parse", "HEAD")
    git(up, "switch", "-q", "main")
    git(up, "merge", "-q", "--no-ff", "-m", "Merge pull request #1", "pr")
    merge = git(up, "rev-parse", "HEAD")
    (up / "tool.py").write_text("v2\n")
    git(up, "commit", "-qam", "change tool")
    tip = git(up, "rev-parse", "HEAD")
    git(up, "switch", "-q", "-c", "side")
    (up / "x").write_text("x\n")
    git(up, "add", "-A"); git(up, "commit", "-qm", "unmerged")
    side = git(up, "rev-parse", "HEAD")
    git(up, "switch", "-q", "main")
    child = tmp_path / "child"
    (child / ".github" / "workflows").mkdir(parents=True)
    (child / ".github" / "workflows" / "ci.yml").write_text(
        f"jobs:\n  a:\n    uses: me/up/.github/workflows/x.yml@{merge}\n  b:\n    steps:\n"
        f"      - uses: actions/checkout@v4\n        with:\n          repository: me/up\n          ref: {pr_head}\n")
    (child / "vendor.lock.json").write_text(json.dumps({"schema": "vendor-lock/1", "files": [
        {"repository": "me/up", "commit": first, "source": "tool.py", "destination": "vendor/tool.py"},
        {"repository": "me/up", "commit": side, "source": "x", "destination": "vendor/x"},
        {"repository": "me/hidden", "commit": "f" * 40, "source": "a.py", "destination": "vendor/a.py"}]}))
    (child / "templates").mkdir()
    (child / "templates" / "parent-pr.ref.json").write_text(json.dumps(
        {"schema": "parent-pr-ref/1", "repository": "me/up", "pull_number": 1, "repository_id": 1, "commit": tip}))
    return {"root": child, "base": str(tmp_path / "remotes") + "/", "first": first, "pr_head": pr_head,
            "merge": merge, "tip": tip, "side": side}


def test_collect_pins(world):
    pins = g.collect_pins(world["root"])
    found = {(p["file"], p["commit"]) for p in pins}
    assert (".github/workflows/ci.yml", world["merge"]) in found and (".github/workflows/ci.yml", world["pr_head"]) in found
    assert ("templates/parent-pr.ref.json", world["tip"]) in found and ("vendor.lock.json", world["first"]) in found
    assert all(p["repository"] != "actions/checkout" for p in pins)


def test_pin_audit_states(world):
    out = g.pin_audit(world["root"], remote_base=world["base"])
    by = {r["commit"]: r for r in out["results"] if r["repository"]}
    assert by[world["merge"]]["state"] == "ok"
    assert by[world["tip"]]["state"] == "ok" and by[world["tip"]]["since"] == 0
    assert by[world["pr_head"]]["state"] == "not_final" and by[world["pr_head"]]["final"] == world["merge"]
    assert by[world["first"]]["state"] == "drift" and by[world["first"]]["changed"] == ["tool.py"]
    assert by[world["side"]]["state"] == "unmerged"
    assert out["ok"] is False and out["write"] is False


def test_unreadable_repository_is_redacted(world):
    out = g.pin_audit(world["root"], remote_base=world["base"])
    assert out["redacted"] == 1 and "hidden" not in json.dumps(out)
    assert [r["state"] for r in out["results"] if r["repository"] is None] == ["unavailable"]


def test_only_owner_and_cli_exit(world):
    assert g.pin_audit(world["root"], only_owners=["nobody"], remote_base=world["base"])["pins"] == 0
    p = subprocess.run([sys.executable, str(Path(g.__file__)), "pin-audit", "--root", str(world["root"]),
                        "--remote-base", world["base"], "--text"], capture_output=True, text=True)
    assert p.returncode == 1 and "not_final me/up@" in p.stdout and p.stdout.strip().splitlines()[-1].startswith("pins=")


def test_remote_base_validation(world):
    with pytest.raises(ValueError):
        g.pin_audit(world["root"], remote_base="ext::sh -c x")


def run_audit(root):
    return subprocess.run([sys.executable, "-S", str(Path(g.__file__)), "pin-audit", "--root", str(root)],
                          capture_output=True, text=True)


@pytest.mark.parametrize("name", ["vendor.lock.json", "nested/publication.json"])
@pytest.mark.parametrize("data", [b"{broken", b"\xff"])
def test_invalid_target_json_exits_two(tmp_path, name, data):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    result = run_audit(tmp_path)
    assert result.returncode == 2
    assert name in json.loads(result.stderr)["detail"]
    assert not result.stdout


def test_non_target_json_and_zero_pins(tmp_path):
    (tmp_path / "unrelated.json").write_text("{broken")
    (tmp_path / "vendor.lock.json").write_text('{"files": [], "other": {"value": 1}}')
    assert g.collect_pins(tmp_path) == []
    result = run_audit(tmp_path)
    assert result.returncode == 0 and json.loads(result.stdout)["pins"] == 0


def test_partial_corruption_fails_before_remote_audit(tmp_path):
    (tmp_path / "vendor.lock.json").write_text(json.dumps({"repository": "me/up", "commit": "a" * 40}))
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "publication.json").write_text("{broken")
    result = run_audit(tmp_path)
    assert result.returncode == 2 and "nested/publication.json" in result.stderr
    assert not result.stdout


def test_target_read_error_identifies_file(tmp_path, monkeypatch):
    import builtins
    (tmp_path / "vendor.lock.json").write_text("{}")
    real_open = builtins.open
    def unreadable(path, *args, **kwargs):
        if Path(path).name == "vendor.lock.json":
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", unreadable)
    with pytest.raises(g.PinInputError, match="vendor.lock.json: read_error"):
        g.collect_pins(tmp_path)
    assert g.main(["pin-audit", "--root", str(tmp_path)]) == 2


def test_pin_parent_fields_keep_existing_states(world):
    out = g.pin_audit(world["root"], remote_base=world["base"])
    by = {r["commit"]: r for r in out["results"] if r["repository"]}
    assert by[world["first"]]["parent_count"] == 0 and by[world["first"]]["kind"] == "root"
    assert by[world["first"]]["state"] == "drift" and by[world["first"]]["changed"] == ["tool.py"]
    assert by[world["merge"]]["parent_count"] == 2 and by[world["merge"]]["kind"] == "merge"
    assert by[world["merge"]]["state"] == "ok" and by[world["merge"]]["changed"] == []
    assert by[world["tip"]]["parent_count"] == 1 and by[world["tip"]]["kind"] == "single" and by[world["tip"]]["state"] == "ok"
    assert by[world["pr_head"]]["kind"] == "single" and by[world["pr_head"]]["state"] == "not_final"
    assert by[world["side"]]["parent_count"] == 1 and by[world["side"]]["state"] == "unmerged"
    hidden = [r for r in out["results"] if r["repository"] is None]
    assert hidden[0]["parent_count"] is None and hidden[0]["kind"] == "unknown"
    text = g.render_pin_audit(out)
    assert "hidden" not in text and "(redacted)" in text and "unknown/?" in text
    assert "not_final me/up@" in text


def test_require_merge_rejects_squash_and_root_only(world):
    out = g.pin_audit(world["root"], remote_base=world["base"], require_merge=True)
    by = {r["commit"]: r for r in out["results"] if r["repository"]}
    assert by[world["merge"]]["state"] == "ok"
    assert by[world["tip"]]["state"] == "not_merge" and by[world["tip"]]["parent_count"] == 1
    assert by[world["first"]]["state"] == "not_merge" and "parent_count=0" in by[world["first"]]["detail"]
    assert by[world["pr_head"]]["state"] == "not_final" and by[world["pr_head"]]["final"] == world["merge"]
    assert by[world["side"]]["state"] == "unmerged"
    assert out["ok"] is False and out["counts"]["not_merge"] == 2
    proc = subprocess.run([sys.executable, str(Path(g.__file__)), "pin-audit", "--root", str(world["root"]),
                           "--remote-base", world["base"], "--require-merge-commit", "--text"],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "not_merge me/up@" in proc.stdout and "hidden" not in proc.stdout


# ──── pin_audit merge fixture ─────────────────────────────────────


def _pin(repo, commit, path, line=None, paths=None):
    return {"repository": repo, "commit": commit, "file": path, "line": line, "entry": path, "paths": list(paths or [])}


class AuditGit:
    """Offline git double for one pin-audit bare session. Responses are (code, stdout bytes)."""

    def __init__(self, **kw):
        self.calls = []
        self.fetch_sha = kw.get("fetch_sha", (0, b""))
        self.cat_e = kw.get("cat_e", (0, b""))
        self.cat_p = kw.get("cat_p", (0, b"tree t\nparent p\n\nmsg\n"))
        self.merge = kw.get("merge", (0, b""))
        self.count = kw.get("count", b"1\n")
        self.first = kw.get("first", b"")
        self.ancestry = kw.get("ancestry", b"")
        self.diff = kw.get("diff", (0, b""))
        self.ls = kw.get("ls", b"ref: refs/heads/main\tHEAD\n")
        self.init = kw.get("init", (0, b""))
        self.fail = kw.get("fail")

    def __call__(self, args, cwd):
        self.calls.append(list(args))
        words = []
        index = 0
        while index < len(args):
            if args[index] == "-c":
                index += 2
                continue
            words.append(args[index])
            index += 1
        verb = words[0]
        if verb == "init":
            return self.init
        if verb == "ls-remote":
            return 0, self.ls
        if verb == "fetch":
            if any("refs/heads/" in word for word in words):
                return 0, b""
            return self.fetch_sha
        if verb == "rev-list" and "--first-parent" in words:
            return 0, self.first
        if verb == "rev-list" and "--count" in words:
            return 0, self.count
        if verb == "rev-list" and "--ancestry-path" in words:
            return 0, self.ancestry
        if verb == "cat-file" and "-e" in words:
            return self.cat_e
        if verb == "cat-file" and "-p" in words:
            return self.cat_p
        if verb == "merge-base":
            return self.merge
        if verb == "diff":
            if self.fail == "diff":
                return 4, b""
            return self.diff
        return 0, b""


A40, B40, C40 = "a" * 40, "b" * 40, "c" * 40


def test_scripted_audit_edges():
    on = AuditGit(first=f"{B40}\n{A40}\n".encode(), cat_p=(0, b"tree t\nparent p1\nparent p2\n\n"),
                  diff=(0, b"tool.py\n"))
    rows = g._audit_repository("me/up", [_pin("me/up", A40, "L", 3, [None, "tool.py"]), _pin("me/up", A40, "M")],
                               "/remote/me/up", 30, git_runner=on)
    assert rows[0]["state"] == "drift" and rows[0]["parent_count"] == 2 and rows[0]["kind"] == "merge"
    assert rows[0]["changed"] == ["tool.py"] and rows[0]["since"] == 1
    assert sum(1 for call in on.calls if "--first-parent" in call) == 1
    assert on.calls[0][:6] == ["-c", "credential.helper=", "-c", "core.askPass=", "-c", "http.extraHeader="]
    missing = AuditGit(cat_e=(1, b""), fetch_sha=(1, b""))
    row = g._audit_repository("me/up", [_pin("me/up", C40, "z")], "/remote/me/up", 30, git_runner=missing)[0]
    assert row["state"] == "unmerged" and row["detail"] == "commit not found on the remote"
    assert row["parent_count"] is None and row["kind"] == "unknown"
    unreachable = AuditGit(merge=(1, b""), cat_p=(0, b"tree t\nparent only\n\n"))
    row = g._audit_repository("me/up", [_pin("me/up", A40, "z")], "/remote/me/up", 30, git_runner=unreachable)[0]
    assert row["state"] == "unmerged" and row["detail"] == "not reachable from main" and row["parent_count"] == 1
    rejected = AuditGit(first=f"{B40}\n{A40}\n".encode(), cat_p=(0, b"tree t\nparent only\n\n"))
    row = g._audit_repository("me/up", [_pin("me/up", A40, "z")], "/remote/me/up", 30, require_merge=True, git_runner=rejected)[0]
    assert row["state"] == "not_merge" and row["changed"] == [] and "parent_count=1" in row["detail"]
    unknown = AuditGit(first=f"{A40}\n".encode(), cat_p=(1, b""), count=b"")
    row = g._audit_repository("me/up", [_pin("me/up", A40, "z")], "/remote/me/up", 30, require_merge=True, git_runner=unknown)[0]
    assert row["state"] == "unknown_parents" and row["parent_count"] is None and row["kind"] == "unknown" and row["since"] == 0
    plain = g._audit_repository("me/up", [_pin("me/up", A40, "z")], "/remote/me/up", 30, git_runner=unknown)[0]
    assert plain["state"] == "ok" and plain["parent_count"] is None
    final = AuditGit(first=f"{B40}\n{A40}\n".encode(), ancestry=f"{B40}\n".encode())
    row = g._audit_repository("me/up", [_pin("me/up", C40, "z")], "/remote/me/up", 30, git_runner=final)[0]
    assert row["state"] == "not_final" and row["final"] == B40
    tip = AuditGit(first=f"{B40}\n{A40}\n".encode(), ancestry=b"")
    row = g._audit_repository("me/up", [_pin("me/up", C40, "z")], "/remote/me/up", 30, git_runner=tip)[0]
    assert row["state"] == "not_final" and row["final"] == B40
    empty_line = AuditGit(first=b"", ls=b"abc\tHEAD\n")
    row = g._audit_repository("me/up", [_pin("me/up", C40, "z")], "/remote/me/up", 30, git_runner=empty_line)[0]
    assert row["branch"] == "main" and row["state"] == "not_final" and row["final"] is None
    with pytest.raises(g.Error) as raised:
        g._audit_repository("me/up", [_pin("me/up", A40, "z", paths=["f"])], "/remote/me/up", 30,
                            git_runner=AuditGit(first=f"{A40}\n".encode(), fail="diff"))
    assert raised.value.code == "git_failed"


def _history(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "tool.py").write_text("v1\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "root")
    root = git(work, "rev-parse", "HEAD")
    git(work, "switch", "-q", "-c", "pr")
    (work / "pr.txt").write_text("pr\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "pr")
    pr = git(work, "rev-parse", "HEAD")
    git(work, "switch", "-q", "main")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge pull request #7", "pr")
    merge = git(work, "rev-parse", "HEAD")
    git(work, "switch", "-q", "-c", "fix")
    (work / "tool.py").write_text("v2\n")
    git(work, "commit", "-qam", "fix")
    git(work, "switch", "-q", "main")
    git(work, "merge", "-q", "--squash", "fix")
    git(work, "commit", "-qm", "squash fix")
    squash = git(work, "rev-parse", "HEAD")
    tree = git(work, "rev-parse", "HEAD^{tree}")
    octopus = git(work, "commit-tree", tree, "-p", squash, "-p", pr, "-p", root, "-m", "octopus")
    git(work, "reset", "-q", "--hard", octopus)
    git(work, "switch", "-q", "-c", "side")
    (work / "side.txt").write_text("s\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "side")
    side = git(work, "rev-parse", "HEAD")
    git(work, "switch", "-q", "main")
    bare = tmp_path / "remotes" / "acme" / "widgets"
    bare.parent.mkdir(parents=True)
    git(tmp_path, "clone", "--bare", "-q", str(work), str(bare))
    return {"work": work, "bare": bare, "base": str(tmp_path / "remotes") + "/",
            "root": root, "pr": pr, "merge": merge, "squash": squash, "octopus": octopus, "side": side}


@pytest.fixture(scope="module")
def bare_world(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("bare-pins")
    made = _history(tmp_path)
    child = tmp_path / "child"
    (child / ".github" / "workflows").mkdir(parents=True)
    (child / "docs" / "nested").mkdir(parents=True)
    (child / ".git").mkdir()
    action = "e" * 40
    bare_sha = "c" * 40
    upper = "D" * 40
    (child / ".github" / "workflows" / "ci.yml").write_text(
        f"# loose {bare_sha}\njobs:\n  a:\n    uses: actions/checkout@{action}\n")
    (child / ".github" / "workflows" / "extra.yaml").write_text("name: extra\n")
    (child / ".git" / "vendor.lock.json").write_text(json.dumps({"repository": "acme/widgets", "commit": made["merge"]}))
    readme = "\n".join([
        f"Pin acme/widgets@{made['octopus']}",
        f"same line acme/widgets commit {made['squash']}",
        f"acme/widgets {upper}",
        f"https://github.com/acme/widgets/commit/{made['root']}",
        "http://www.github.com/acme/widgets is the same repository",
    ])
    (child / "README.md").write_text(readme + "\n")
    (child / "docs" / "guide.md").write_text(f"Repository: https://github.com/acme/widgets\n{made['side']}\n")
    (child / "docs" / "a.md").write_text(f"acme/widgets@{made['merge']}\n")
    (child / "docs" / "nested" / "extra.md").write_text(f"acme/widgets@{made['merge']}\n")
    (child / "docs" / "noise.md").write_text("\n".join([
        f"nothing but {bare_sha} here",
        "",
        bare_sha,
        "",
        "owner/.git@" + ("ab" * 20),
        "",
        "prefix/acme/widgets@" + ("cd" * 20),
        "",
        upper,
        "",
    ]))
    (child / "docs" / "notes.txt").write_text(f"acme/widgets@{made['merge']}\n")
    (child / "docs" / "linked.md").symlink_to(child / "docs" / "guide.md")
    (child / "vendor.lock.json").write_text(json.dumps({"files": [
        {"repository": "acme/widgets", "commit": made["root"], "source": "tool.py", "destination": "vendor/tool.py"},
        {"repository": "acme/widgets", "commit": made["merge"], "source": "pr.txt", "destination": "vendor/pr.txt"},
        {"repository": "acme/widgets", "commit": made["squash"]},
        {"repository": "acme/widgets", "commit": made["pr"]},
        {"repository": "acme/widgets", "commit": made["side"]},
        {"repository": "secret/hidden", "commit": "f" * 40, "source": "a.py"},
    ]}))
    made["child"] = child
    made["action"] = action
    made["bare_sha"] = bare_sha
    return made


def test_markdown_rule_and_workflow_actions_stay_put(bare_world):
    child = bare_world["child"]
    plain = g.collect_pins(child)
    commits = {p["commit"] for p in plain}
    assert bare_world["octopus"] not in commits and bare_world["action"] in commits
    assert bare_world["bare_sha"] not in commits
    assert all(not p["file"].startswith(".git/") for p in plain)
    marked = g.collect_pins(child, include_markdown=True)
    found = {(p["file"], p["commit"], p["repository"]) for p in marked}
    assert ("README.md", bare_world["octopus"], "acme/widgets") in found
    assert ("README.md", bare_world["squash"], "acme/widgets") in found
    assert ("README.md", bare_world["root"], "acme/widgets") in found
    assert ("docs/guide.md", bare_world["side"], "acme/widgets") in found
    assert ("docs/nested/extra.md", bare_world["merge"], "acme/widgets") in found
    assert bare_world["bare_sha"] not in {p["commit"] for p in marked}
    assert "D" * 40 not in {p["commit"] for p in marked}
    assert not any(p["repository"] == "owner/.git" or p["commit"] == "ab" * 20 for p in marked)
    assert not any(p["commit"] == "cd" * 20 for p in marked)
    assert not any(p["file"] == "docs/notes.txt" or p["file"] == "docs/linked.md" for p in marked)
    action_pins = [p for p in marked if p["commit"] == bare_world["action"]]
    assert len(action_pins) == 1 and action_pins[0]["file"].endswith("ci.yml")


def test_bare_repo_merge_squash_and_repo_filter(bare_world):
    base, child = bare_world["base"], bare_world["child"]
    out = g.pin_audit(child, remote_base=base, include_markdown=True)
    by = {r["commit"]: r for r in out["results"] if r["repository"] == "acme/widgets"}
    assert by[bare_world["merge"]]["parent_count"] == 2 and by[bare_world["merge"]]["kind"] == "merge"
    assert by[bare_world["merge"]]["state"] == "ok"
    assert by[bare_world["octopus"]]["parent_count"] == 3 and by[bare_world["octopus"]]["kind"] == "merge"
    assert by[bare_world["octopus"]]["state"] == "ok"
    assert by[bare_world["squash"]]["parent_count"] == 1 and by[bare_world["squash"]]["kind"] == "single"
    assert by[bare_world["squash"]]["state"] == "ok"
    assert by[bare_world["root"]]["parent_count"] == 0 and by[bare_world["root"]]["kind"] == "root"
    assert by[bare_world["root"]]["state"] == "drift" and by[bare_world["root"]]["changed"] == ["tool.py"]
    assert by[bare_world["pr"]]["state"] == "not_final" and by[bare_world["pr"]]["final"] == bare_world["merge"]
    assert by[bare_world["side"]]["state"] == "unmerged"
    dumped = json.dumps(out)
    assert out["redacted"] == 2
    for hidden in ("hidden", "secret", "checkout", "actions"):
        assert hidden not in dumped
    text = g.render_pin_audit(out)
    assert " +1 more" in text
    assert "(redacted)" in text and "unknown/?" in text
    filtered = g.pin_audit(child, remote_base=base, include_markdown=True, repos=["  acme/widgets  ", "  "])
    assert filtered["redacted"] == 0 and all(r["repository"] == "acme/widgets" for r in filtered["results"])
    assert "hidden" not in json.dumps(filtered)
    owned = g.pin_audit(child, remote_base=base, only_owners=["acme"])
    assert owned["redacted"] == 0 and {r["repository"] for r in owned["results"]} == {"acme/widgets"}
    with pytest.raises(ValueError):
        g.pin_audit(child, remote_base=base, repos=["not a repo"])
    proc = subprocess.run([sys.executable, str(Path(g.__file__)), "pin-audit", "--root", str(child),
                           "--remote-base", base, "--include-markdown", "--repo", " acme/widgets ",
                           "--repo", " ", "--json", "vendor.lock.json", "--text"],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "hidden" not in proc.stdout and "acme/widgets@" in proc.stdout
    bad = subprocess.run([sys.executable, str(Path(g.__file__)), "pin-audit", "--root", str(child),
                          "--repo", "nope"], capture_output=True, text=True)
    assert bad.returncode == 2 and not bad.stdout
    forced = g.pin_audit(child, remote_base=base, include_markdown=True, repos=["acme/widgets"], require_merge=True)
    states = {r["commit"]: r["state"] for r in forced["results"]}
    assert states[bare_world["merge"]] == "ok" and states[bare_world["octopus"]] == "ok"
    assert states[bare_world["squash"]] == "not_merge" and states[bare_world["root"]] == "not_merge"
    assert states[bare_world["pr"]] == "not_final" and states[bare_world["side"]] == "unmerged"
    assert forced["ok"] is False
    gate = subprocess.run([sys.executable, str(Path(g.__file__)), "pin-audit", "--root", str(child),
                           "--remote-base", base, "--include-markdown", "--repo", "acme/widgets",
                           "--require-merge-commit"], capture_output=True, text=True)
    assert gate.returncode == 1 and json.loads(gate.stdout)["counts"]["not_merge"] >= 1


def test_invalid_json_reason_is_reported_in_process(tmp_path):
    (tmp_path / "vendor.lock.json").write_text("{broken")
    with pytest.raises(g.PinInputError, match="vendor.lock.json: invalid_json_or_encoding"):
        g.collect_pins(tmp_path)
    (tmp_path / "vendor.lock.json").write_bytes(b"\xff")
    with pytest.raises(g.PinInputError, match="vendor.lock.json: invalid_json_or_encoding"):
        g.collect_pins(tmp_path)


def test_markdown_broken_readme_and_matcher_edges(tmp_path):
    (tmp_path / "README.md").write_bytes(b"\xff")
    assert g.collect_pins(tmp_path) == []
    with pytest.raises(g.PinInputError, match="README.md: read_error"):
        g.collect_pins(tmp_path, include_markdown=True)
    sha = "ab" * 20
    other = "cd" * 20
    left, right = "aa/bb", "cc/dd"
    tie = f"{left}   {sha}   {right}"
    pins = g._markdown_pins("README.md", "\n".join([
        f"zz/far {other}",
        f"aa/near {sha} zz/later",
        "owner/name@" + sha,
        "",
        sha,
        "",
        f"only/here {other}",
    ]))
    located = {(pin["line"], pin["repository"], pin["commit"]) for pin in pins}
    assert (2, "aa/near", sha) in located
    assert (3, "owner/name", sha) in located
    assert not any(pin["repository"] == "zz/far" and pin["commit"] == sha for pin in pins)
    assert not any(pin["repository"] == "zz/later" for pin in pins)
    assert not any(pin["line"] == 5 for pin in pins)
    assert (1, "zz/far", other) in located and (7, "only/here", other) in located
    tied = g._markdown_pins("docs/t.md", tie + "\n")
    assert [p["repository"] for p in tied] == ["aa/bb"]
    assert g._markdown_pins("docs/t.md", "") == []
    assert g._parent_count_text("") == 0
    assert g._parent_count_text("tree a\nparenthood\n\nparent in message\n") == 0
    assert g._parent_count_text("tree a\nparent b\nparent c\n\nparent d\n") == 2
    assert g._commit_kind(None) == "unknown" and g._commit_kind(0) == "root"
    assert g._commit_kind(1) == "single" and g._commit_kind(1, single="squash_or_single") == "squash_or_single"
    assert g._commit_kind(2) == "merge" and g._commit_kind(3) == "merge"
    assert g._pin_result_state(present=False, ancestor=True, on_line=True, parent_count=2, require_merge=True, changed=False) == "unmerged"
    assert g._pin_result_state(present=True, ancestor=False, on_line=True, parent_count=2, require_merge=True, changed=False) == "unmerged"
    assert g._pin_result_state(present=True, ancestor=True, on_line=False, parent_count=2, require_merge=True, changed=False) == "not_final"
    assert g._pin_result_state(present=True, ancestor=True, on_line=True, parent_count=1, require_merge=True, changed=True) == "not_merge"
    assert g._pin_result_state(present=True, ancestor=True, on_line=True, parent_count=0, require_merge=True, changed=False) == "not_merge"
    assert g._pin_result_state(present=True, ancestor=True, on_line=True, parent_count=2, require_merge=True, changed=True) == "drift"
    assert g._pin_result_state(present=True, ancestor=True, on_line=True, parent_count=2, require_merge=True, changed=False) == "ok"
    assert g._pin_result_state(present=True, ancestor=True, on_line=True, parent_count=None, require_merge=True, changed=False) == "unknown_parents"
    assert g._pin_result_state(present=True, ancestor=True, on_line=True, parent_count=None, require_merge=True, changed=True) == "unknown_parents"
    assert g._pin_result_state(present=True, ancestor=True, on_line=True, parent_count=None, require_merge=False, changed=True) == "drift"
    assert g._pin_result_state(present=True, ancestor=True, on_line=True, parent_count=1, require_merge=False, changed=False) == "ok"
    assert g._pin_result_state(present=True, ancestor=True, on_line=True, parent_count=1, require_merge=False, changed=True) == "drift"
    assert g._selected_repos(None) is None and g._selected_repos([]) is None
    assert g._selected_repos(["  ", ""]) is None
    assert g._selected_repos([" me/up "]) == {"me/up"}
    assert g._blank_to_none(None) is None and g._blank_to_none("  ") is None and g._blank_to_none(" a ") == "a"
    assert g._markdown_pin_file("README.md") and g._markdown_pin_file("docs/a/b.md")
    assert not g._markdown_pin_file("docs/a.txt") and not g._markdown_pin_file("nested/README.md")
    assert "--first-parent" not in __import__("inspect").getsource(g._audit_repository)
    sha = "ab" * 20
    zero = "0" * 40
    blocked = "\n".join([
        f"see and/or {sha}",
        "",
        f"docs/guide.md {sha}",
        "",
        f"acme/widgets {zero}",
        "",
        f"https://github.com/acme/widgets/issues/4 {sha}",
        "",
        f"https://github.com/acme/widgets/pull/9 {sha}",
        "",
        f"https://github.com/acme/widgets/commit/{sha}",
        "",
        "```",
        f"acme/widgets@{sha}",
        f"acme/widgets {sha}",
        "```",
        "",
        f"inline `acme/widgets@{sha}` and `{sha}` next to `acme/widgets`",
        "",
        f"../secret {sha}",
        "",
        f"acme/guide.md {sha}",
        "",
        f"see `https://github.com/acme/widgets` {sha}",
        "",
        f"https://github.com/and/or {sha}",
        "",
        f"acme/widgets@{zero}",
    ])
    kept = g._markdown_pins("README.md", blocked)
    assert g._markdown_pins("README.md", f"acme/widgets\n{sha}\n") == []
    assert {(p["repository"], p["commit"]) for p in kept} == {("acme/widgets", sha)}
    assert len(kept) == 3
    assert zero not in {p["commit"] for p in kept}
    with pytest.raises(ValueError):
        g._audit_repository("../secret", [_pin("../secret", sha, "z")], "/tmp/../secret", 30,
                            git_runner=lambda *a: (_ for _ in ()).throw(AssertionError("git")))


def test_cli_unknown_parents_fails_require_merge(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "f").write_text("a\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "root")
    (work / "f").write_text("b\n")
    git(work, "commit", "-qam", "squash-style")
    tip = git(work, "rev-parse", "HEAD")
    bare = tmp_path / "remotes" / "acme" / "widgets"
    bare.parent.mkdir(parents=True)
    git(tmp_path, "clone", "--bare", "-q", str(work), str(bare))
    child = tmp_path / "child"
    child.mkdir()
    (child / "vendor.lock.json").write_text(json.dumps({"repository": "acme/widgets", "commit": tip}))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    real = shutil.which("git")
    (bindir / "git").write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "for index, arg in enumerate(args):\n"
        "    if arg == 'cat-file' and index + 1 < len(args) and args[index + 1] == '-p':\n"
        "        raise SystemExit(1)\n"
        f"os.execv({real!r}, [{real!r}, *args])\n")
    (bindir / "git").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    base = str(tmp_path / "remotes") + "/"
    forced = subprocess.run([sys.executable, str(Path(g.__file__)), "pin-audit", "--root", str(child),
                             "--remote-base", base, "--require-merge-commit"],
                            capture_output=True, text=True, env=env)
    assert forced.returncode == 1, forced.stderr
    body = json.loads(forced.stdout)
    assert body["ok"] is False and body["results"][0]["state"] == "unknown_parents"
    assert body["results"][0]["parent_count"] is None and "secret" not in forced.stdout
    plain = subprocess.run([sys.executable, str(Path(g.__file__)), "pin-audit", "--root", str(child),
                            "--remote-base", base], capture_output=True, text=True, env=env)
    assert plain.returncode == 0 and json.loads(plain.stdout)["results"][0]["state"] == "ok"


# ──── vendor_tools ────────────────────────────────────────────────

# vendor-lock-check / vendor-lock-verify-remote / vendor-plan / vendor-matrix: read-only.


C1, C2 = "1" * 40, "2" * 40


MOD = b"import json\n\ndef f():\n    return 1\n"


LIC = b"MIT License\n"


def entry_vendor_tools(dest, data, repo="o/up", source="mod.py", commit=C1):
    return {"repository": repo, "ref": commit, "commit": commit, "source": source, "destination": dest,
            "blob_sha": g.git_blob_sha(data), "sha256": hashlib.sha256(data).hexdigest()}


def write_lock(root, files, name="vendor.lock.json"):
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps({"schema": "vendor-lock/1", "files": files}))


@pytest.fixture
def project(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "mod.py").write_bytes(MOD)
    (tmp_path / "vendor" / "up-LICENSE").write_bytes(LIC)
    write_lock(tmp_path, [entry_vendor_tools("vendor/mod.py", MOD), entry_vendor_tools("vendor/up-LICENSE", LIC, source="LICENSE")])
    return tmp_path


def test_git_blob_sha_matches_git(tmp_path):
    (tmp_path / "f").write_bytes(MOD)
    assert g.git_blob_sha(MOD) == subprocess.run(["git", "hash-object", str(tmp_path / "f")], capture_output=True, text=True).stdout.strip()


def test_check_ok(project):
    out = g.vendor_lock_check(project)
    assert out["ok"] and out["write"] is False and out["counts"] == {"ok": 2}


def test_check_mismatch_header_missing_unlocked(project):
    (project / "vendor" / "mod.py").write_bytes(b"# vendored from somewhere\n# source: o/up\n" + MOD)
    assert g.vendor_lock_check(project)["files"][0]["status"] == "header_modified"
    (project / "vendor" / "mod.py").write_bytes(MOD + b"x")
    assert g.vendor_lock_check(project)["files"][0]["status"] == "digest_mismatch"
    (project / "vendor" / "mod.py").unlink()
    (project / "vendor" / "extra.py").write_text("y = 1\n")
    (project / "vendor" / "README.md").write_text("notes\n")
    out = g.vendor_lock_check(project)
    status = {r["destination"]: r["status"] for r in out["files"]}
    assert status["vendor/mod.py"] == "missing" and status["vendor/extra.py"] == "unlocked"
    assert "vendor/README.md" not in status and out["ok"] is False


def test_check_symlink_destination(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "mod.py").write_bytes(MOD)
    (project / "vendor" / "mod.py").unlink()
    (project / "vendor" / "mod.py").symlink_to(outside / "mod.py")
    assert g.vendor_lock_check(project)["files"][0]["status"] == "symlink"


@pytest.mark.parametrize("mutate", [
    lambda l: l["files"].append(dict(l["files"][0])),                                  # duplicate destination
    lambda l: l["files"][0].update(destination="../x.py"),
    lambda l: l["files"][0].update(destination="vendor"),                              # overlapping
    lambda l: l["files"][0].update(commit="abc"),
    lambda l: l["files"][0].pop("sha256"),
    lambda l: l.update(schema="vendor-lock/2"),
])
def test_lock_validation_matches_vendor_sync(project, mutate):
    lock = json.loads((project / "vendor.lock.json").read_text())
    mutate(lock)
    (project / "vendor.lock.json").write_text(json.dumps(lock))
    with pytest.raises(ValueError):
        g.vendor_lock_check(project)


def test_check_rules_layout_and_pin(project):
    rules = {"schema": "vendor-rules/1", "root": "tool/python/vendor"}
    out = g.vendor_lock_check(project, rules=rules)
    assert {v["rule"] for v in out["violations"]} == {"layout"} and out["ok"] is False
    lock = json.loads((project / "vendor.lock.json").read_text())
    lock["files"][0]["ref"] = "main"
    (project / "vendor.lock.json").write_text(json.dumps(lock))
    out = g.vendor_lock_check(project, rules={"schema": "vendor-rules/1"})
    assert [v["rule"] for v in out["violations"]] == ["pinned_commit"]


@pytest.mark.parametrize("rules", [
    {"schema": "vendor-rules/2"},
    {"schema": "vendor-rules/1", "bogus": 1},
    {"schema": "vendor-rules/1", "require": {"stdlib_only": "yes"}},
    {"schema": "vendor-rules/1", "root": "../x"},
    {"schema": "vendor-rules/1", "overrides": [{"repository": "o/up", "source": "mod.py", "destination": "x/mod.py", "reason": " "}]},
])
def test_rules_validation(rules):
    with pytest.raises(ValueError):
        g.validate_vendor_rules(rules)


def fake_remote(files):
    def remote(repository, path, commit, transport="auto", timeout=30):
        try:
            return files[(repository, path, commit)]
        except KeyError:
            raise g.Error("not_a_file") from None
    return remote


def test_verify_remote(project):
    files = {("o/up", "mod.py", C1): MOD, ("o/up", "LICENSE", C1): b"changed\n"}
    with patch.object(g, "_remote_file", side_effect=fake_remote(files)):
        out = g.vendor_lock_verify_remote(project)
    assert [r["status"] for r in out["files"]] == ["ok", "digest_mismatch"] and out["ok"] is False
    with patch.object(g, "_remote_file", side_effect=fake_remote({})):
        assert g.vendor_lock_verify_remote(project)["counts"] == {"unavailable": 2}


def test_verify_remote_upstream_head(project):
    files = {("o/up", "mod.py", C1): MOD, ("o/up", "LICENSE", C1): LIC}
    with patch.object(g, "_remote_file", side_effect=fake_remote(files)), \
         patch.object(g, "request", return_value=[{"sha": C2}]):
        out = g.vendor_lock_verify_remote(project, upstream=True)
    assert out["ok"] and out["files"][0]["upstream_head"] == C2 and out["files"][0]["upstream_changed"] is True


def test_remote_file_checks_blob_identity():
    meta = {"type": "file", "sha": g.git_blob_sha(MOD), "size": len(MOD)}
    good = {"encoding": "base64", "content": base64.b64encode(MOD).decode()}
    with patch.object(g, "request", side_effect=[meta, good]) as req:
        assert g._remote_file("o/up", "mod.py", C1) == MOD
    assert req.call_args_list[0].args[1] == f"repos/o/up/contents/mod.py?ref={C1}"
    bad = {"encoding": "base64", "content": base64.b64encode(b"other").decode()}
    with patch.object(g, "request", side_effect=[meta, bad]), pytest.raises(g.Error):
        g._remote_file("o/up", "mod.py", C1)
    with pytest.raises(ValueError):
        g._remote_file("o/up", "../etc", C1)


UPSTREAM = {
    ("o/up", "mod.py", C1): MOD,
    ("o/up", "LICENSE", C1): LIC,
    ("o/up", "heavy.py", C1): b"import requests\n",
    ("o/up", "pkg.py", C1): b"from . import sibling\n",
    ("o/up", "unused.py", C1): b"x = 1\n",
    ("o/nolic", "mod2.py", C1): b"import os\n",
}


@pytest.fixture
def consumer(tmp_path):
    (tmp_path / "app.py").write_text("import mod\nimport heavy\nimport pkg\nimport mod2\n")
    return tmp_path


def _licence_api(method, path, **kwargs):
    # Only the licence lookup reaches the API in a plan; anything else is a test error.
    assert method == "GET" and path == f"repos/o/up/license?ref={C1}", path
    return {"license": {"spdx_id": "MIT"}}


def plan(consumer, candidates, rules=None):
    with patch.object(g, "_remote_file", side_effect=fake_remote(UPSTREAM)), g.request_stub(_licence_api):
        return {d["candidate"]: d for d in g.vendor_plan(consumer, candidates, rules)["decisions"]}


def test_plan_rules(consumer):
    cands = [f"o/up:mod.py@{C1}", f"o/up:heavy.py@{C1}", f"o/up:pkg.py@{C1}", f"o/up:unused.py@{C1}",
             "o/up:mod.py@main", f"o/up:missing.py@{C1}", f"o/nolic:mod2.py@{C1}"]
    out = plan(consumer, cands)
    assert out[cands[0]]["action"] == "place" and out[cands[0]]["destination"] == "vendor/mod.py"
    assert out[cands[0]]["license_destination"] == "vendor/up-LICENSE"
    assert out[cands[0]]["license_spdx"] == "MIT"
    assert "license_spdx" not in out[cands[6]]  # no LICENSE file: nothing to identify
    assert out[cands[0]]["blob_sha"] == g.git_blob_sha(MOD)
    assert out[cands[1]]["reasons"] == ["not_stdlib_only"]
    assert out[cands[2]]["reasons"] == ["relative_import"]
    assert out[cands[3]]["reasons"] == ["not_imported_by_project"]
    assert out[cands[4]]["reasons"] == ["not_pinned_to_commit"]
    assert out[cands[5]]["reasons"] == ["upstream_unavailable:not_a_file"]
    assert out[cands[6]]["reasons"] == ["license_missing"]


def test_plan_layout_override_deny_and_determinism(consumer):
    rules = {"schema": "vendor-rules/1", "root": "tool/python/vendor", "deny_sources": ["heavy.py"],
             "overrides": [{"repository": "o/up", "source": "pkg.py", "destination": "tests/vendor/pkg.py",
                            "reason": "test helper"}], "require": {"single_file": False}}
    cands = [f"o/up:mod.py@{C1}", f"o/up:heavy.py@{C1}", f"o/up:pkg.py@{C1}"]
    out = plan(consumer, cands, rules)
    assert out[cands[0]]["destination"] == "tool/python/vendor/mod.py"
    assert out[cands[1]]["reasons"] == ["denied_source"]
    assert out[cands[2]]["destination"] == "tests/vendor/pkg.py" and out[cands[2]]["rule"] == "override"
    assert plan(consumer, cands, rules) == out


def test_plan_rejects_bad_candidate(consumer):
    for bad in ("o/up:mod.py", "noslash:mod.py@" + C1, "o/up:../x.py@" + C1):
        with pytest.raises(ValueError):
            plan(consumer, [bad])


def _contents(lock):
    return {"encoding": "base64", "content": base64.b64encode(json.dumps(lock).encode()).decode()}


def test_matrix_layouts_and_pin_mismatch():
    a = {"schema": "vendor-lock/1", "files": [entry_vendor_tools("vendor/mod.py", MOD, commit=C1)]}
    b = {"schema": "vendor-lock/1", "files": [entry_vendor_tools("tool/python/vendor/mod.py", MOD, commit=C2)]}
    def fake(method, path, **kw):
        table = {"repos/o/a": {"visibility": "public"}, "repos/o/b": {"visibility": "public"},
                 "repos/o/c": {"visibility": "private", "private": True},
                 "repos/o/a/contents/vendor.lock.json": _contents(a),
                 "repos/o/b/contents/tool/python/vendor.lock.json": _contents(b)}
        if path in table:
            return table[path]
        raise g.Error("not_found_or_inaccessible")
    with patch.object(g, "request", side_effect=fake):
        out = g.vendor_matrix("o", ["a", "b", "c"])
    assert set(out["consumers"]) == {"o/a", "o/b"} and out["errors"] == [] and out["redacted"] == 1
    assert "o/c" not in json.dumps(out)
    assert out["layouts"] == {"vendor": 1, "tool/python/vendor": 1}
    assert out["pin_mismatch"] == [{"provider": "o/up", "source": "mod.py", "commits": [C1, C2], "consumers": ["o/a", "o/b"]}]


def test_matrix_rejects_bad_owner():
    with pytest.raises(ValueError):
        g.vendor_matrix("../x", ["a"])


def test_cli_check(project):
    p = subprocess.run([sys.executable, str(Path(g.__file__)), "vendor-lock-check", "--root", str(project)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["ok"] is True


# ──── repository_metadata ─────────────────────────────

# Repository metadata: checkout identity (v2) and the Ironmate v1 adapter.


GENERATED = "2026-10-11T10:05:00Z"


REPO = "example/demo"


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    env = dict(os.environ, GIT_COMMITTER_DATE="2026-10-11T19:00:00+09:00",
               GIT_AUTHOR_DATE="2026-10-11T19:00:00+09:00")
    _git(tmp_path, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty",
         "-m", "メタデータ: 件名 «quotes» & emoji 🚀", "-m", "body line is not the subject", env=env)
    return tmp_path


def test_v2_records_checkout_head_and_keeps_source_separate(repo):
    sha = _git(repo, "rev-parse", "HEAD")
    record = g.repository_metadata(repo, repository=REPO, source_sha="B" * 40, generated_at=GENERATED)
    assert record == {
        "schema_version": "2.0", "repository": {"full_name": REPO},
        "head": {"sha": sha, "branch": "main", "detached": False,
                 "timestamp": "2026-10-11T19:00:00+09:00",
                 "subject": "メタデータ: 件名 «quotes» & emoji 🚀"},
        "source": {"sha": "b" * 40}, "generated_at": GENERATED}


def test_checkout_head_ignores_github_sha(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    assert g.checkout_head(repo)["sha"] == _git(repo, "rev-parse", "HEAD")


def test_detached_head_is_null_branch_in_v2_and_detached_in_v1(repo):
    _git(repo, "checkout", "-q", "--detach")
    assert g.checkout_head(repo)["branch"] is None and g.checkout_head(repo)["detached"] is True
    v1 = g.repository_metadata_v1(repo, repository=REPO, env={}, generated_at=GENERATED)
    assert v1["head"]["branch"] == "detached"


@pytest.mark.parametrize("env,branch", [
    ({"GITHUB_HEAD_REF": "feature/x", "GITHUB_REF_NAME": "12/merge"}, "feature/x"),
    ({"GITHUB_REF_NAME": "release/1"}, "release/1"),
    ({}, "main"),
])
def test_v1_branch_label_rule_matches_ironmate(repo, env, branch):
    assert g.repository_metadata_v1(repo, repository=REPO, env=env, generated_at=GENERATED)["head"]["branch"] == branch


def test_v1_shape_and_serialization(repo, tmp_path):
    record = g.repository_metadata_v1(repo, repository=REPO, env={}, generated_at=GENERATED,
                                      tooling={"python": "3.12.3"}, measurements={"working_tree_bytes": 0})
    assert record["head"]["short_sha"] == record["head"]["sha"][:8]
    assert record["measurements"] == {"github_reported_size_bytes": None, "working_tree_bytes": 0,
                                      "release_artifact_bytes": None}  # measured 0 is not null
    out = tmp_path / "out"
    g.write_repository_metadata(record, out)
    pretty = (out / "repository-metadata.json").read_text(encoding="utf-8")
    line = (out / "repository-metadata.jsonl").read_text(encoding="utf-8")
    assert json.loads(pretty) == json.loads(line) == record
    assert line.count("\n") == 1 and "🚀" in line  # UTF-8, not \\u escapes
    assert str(repo) not in pretty  # no local absolute path


@pytest.mark.parametrize("kwargs,message", [
    ({"repository": "not-a-repo"}, "owner/name"),
    ({"repository": "a/b/c"}, "owner/name"),
    ({"repository": REPO, "source_sha": "abc"}, "source_sha"),
    ({"repository": REPO, "expected_sha": "g" * 40}, "expected_sha"),
    ({"repository": REPO, "generated_at": "2026-10-11T10:00:00"}, "timezone"),
    ({"repository": REPO, "generated_at": "yesterday"}, "ISO 8601"),
])
def test_invalid_inputs_are_rejected(repo, kwargs, message):
    with pytest.raises(ValueError, match=message):
        g.repository_metadata(repo, **kwargs)


def test_expected_sha_mismatch_fails_closed_before_writing(repo, tmp_path):
    out = tmp_path / "out"
    result = subprocess.run([sys.executable, "-S", g.__file__, "repo-metadata", "--root", str(repo),
                             "--repository", REPO, "--format", "v1", "--expected-sha", "f" * 40,
                             "--output-dir", str(out)], capture_output=True, text=True)
    assert result.returncode == 2 and not out.exists()
    sha = _git(repo, "rev-parse", "HEAD")
    ok = subprocess.run([sys.executable, "-S", g.__file__, "repo-metadata", "--root", str(repo),
                         "--repository", REPO, "--format", "v1", "--expected-sha", sha.upper(),
                         "--output-dir", str(out)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert json.loads((out / "repository-metadata.json").read_text(encoding="utf-8"))["head"]["sha"] == sha


def test_v1_validator_rejects_contract_breaks(repo):
    good = g.repository_metadata_v1(repo, repository=REPO, env={}, generated_at=GENERATED)
    for mutate in (lambda r: r["head"].update(short_sha="00000000"),
                   lambda r: r["head"].update(sha=r["head"]["sha"].upper()),
                   lambda r: r.update(extra=1),
                   lambda r: r["measurements"].update(working_tree_bytes=-1),
                   lambda r: r["measurements"].update(working_tree_bytes=True),
                   lambda r: r["tooling"].update(python="3.12; rm -rf /")):
        broken = json.loads(json.dumps(good))
        mutate(broken)
        with pytest.raises(ValueError):
            g.validate_repository_metadata_v1(broken)


def test_tool_versions_is_explicit_and_allowlisted():
    assert g.tool_versions() == {}
    with pytest.raises(ValueError):
        g.tool_versions(["bash"])
    assert set(g.tool_versions(["git"], include_python=True)) <= {"git", "python"}


def test_not_a_git_checkout_is_an_error_not_a_guess(tmp_path):
    with pytest.raises(g.GitInspectionError):
        g.checkout_head(tmp_path)


@pytest.mark.skipif(not os.environ.get("IRONMATE_GENERATOR_DIR"),
                    reason="set IRONMATE_GENERATOR_DIR to the vendored Ironmate generator to compare")
def test_v1_adapter_matches_ironmate_on_the_same_checkout(repo):
    sys.path.insert(0, os.environ["IRONMATE_GENERATOR_DIR"])
    from repository_metadata_generator import record_from_checkout
    for detach in (False, True):
        if detach:
            _git(repo, "checkout", "-q", "--detach")
        for env in ({}, {"GITHUB_HEAD_REF": "feature/x"}, {"GITHUB_REF_NAME": "main"}):
            old = record_from_checkout(repo, REPO, env=env, tooling={"python": "3.12.3"})
            new = g.repository_metadata_v1(repo, repository=REPO, env=env, tooling={"python": "3.12.3"},
                                           generated_at=old["generated_at"])
            assert new == old, (detach, env)


@pytest.mark.parametrize("version", ["3.12.3", "3.14.0rc1", "3.12.1.post1", "3.13.0.dev2",
                                     "3.12.1+local.1", "3.13.0a1.post2.dev3"])
def test_python_version_labels_match_ironmate_acceptance(monkeypatch, version):
    monkeypatch.setattr("platform.python_version", lambda: version)
    assert g.tool_versions(include_python=True) == {"python": version}


@pytest.mark.parametrize("version", ["not-a-version", "3.12\n", "3.12.1 extra", "x" * 40])
def test_malformed_python_version_is_omitted(monkeypatch, version):
    monkeypatch.setattr("platform.python_version", lambda: version)
    assert g.tool_versions(include_python=True) == {}


# ──── repo_name_files ─────────────────────────────────

# repo-name-files: files named after each public repository, read-only, fake transport.


P, A, B, C = "d" * 40, "a" * 40, "b" * 40, "c" * 40


def sha(data):
    return g.git_blob_sha(data)


TOOL_SRC = b"import json\nimport helper\nimport requests\nfrom os import path\n"


LIB_SRC = b"from . import inner\n"


REPOS = [
    {"name": "me", "default_branch": "main", "owner": {"login": "me"}},
    {"name": "Tool-Kit", "default_branch": "main", "owner": {"login": "me"}},
    {"name": "lib", "default_branch": "dev", "owner": {"login": "me"}},
    {"name": "broken", "default_branch": "main", "owner": {"login": "me"}},
    {"name": "odd", "default_branch": None, "owner": {"login": "me"}},
    {"name": "secret", "private": True, "visibility": "private", "owner": {"login": "me"}},
]


LOCK = json.dumps({"schema": "vendor-lock/1", "files": [
    {"repository": "me/lib", "source": "lib.js"},
    {"repository": "me/lib", "source": "LICENSE"},
    {"repository": "ME/Tool-Kit", "source": "tool_kit.py"},
]}).encode()


OTHER = json.dumps({"schema": "html-import-vendor/1", "sources": []}).encode()


FILES = {
    "me/Tool-Kit": {"tool_kit.py": TOOL_SRC, "helper.py": b"", "Tool-Kit.sh": b"echo\n", "sub/vendor.lock.json": LOCK},
    "me/lib": {"lib.js": b"x\n", "src/lib/lib.py": LIB_SRC, "src/lib/inner.py": b""},
    "me/odd": {"vendor.lock.json": OTHER, "x/vendor.lock.json": b"not json", "y/vendor.lock.json": b"{\"files\": 1}"},
    "me/me": {},
}


PARENT = {
    "vendor-catalog.json": {"schema": "vendor-catalog/1", "tools": [
        {"repository": "me/lib", "commit": A},
        {"repository": "me/Tool-Kit", "commit": A, "source": "tool_kit.py"},
        {"repository": "me/odd", "commit": A, "source": "odd.py"},
    ]},
    "vendor-consumers.json": {"schema": "vendor-consumers/1", "consumers": [
        {"repository": "me/odd", "entries": [{"repository": "me/Tool-Kit", "source": "scripts/x.py"}]},
    ]},
    "child-heads.json": {"schema": "child-heads/1", "heads": {"me/Tool-Kit": {"sha": A}, "me/lib": {"sha": B}}},
}


HEADS = {"me/me": P, "me/Tool-Kit": A, "me/lib": B, "me/odd": C}


def fake_request(method, path, **kwargs):
    for meta in REPOS:
        if path == "repos/me/" + meta["name"]:
            return dict(meta, visibility=meta.get("visibility", "public"), archived=False)
    for full, head in HEADS.items():
        if path.startswith(f"repos/{full}/commits/"):
            return {"sha": head}
        if path == f"repos/{full}/git/trees/{head}?recursive=1":
            return {"truncated": False, "tree": [
                {"path": p, "mode": "100644", "type": "blob", "sha": sha(d)} for p, d in FILES[full].items()]}
    raise g.Error("not_found_or_inaccessible")


def make_raw(parent=None, fail=()):
    parent = PARENT if parent is None else parent

    def raw(repository, path, ref, **kwargs):
        if path in fail:
            raise g.Error("server_error")
        if repository == "me/me" and path in parent:
            value = parent[path]
            return value if isinstance(value, bytes) else json.dumps(value).encode()
        if path in FILES.get(repository, {}):
            return FILES[repository][path]
        raise g.Error("not_found_or_inaccessible")
    return raw


def run_repo_name_files(raw=None, **kwargs):
    with patch.object(g, "pages", return_value=REPOS), patch.object(g, "request", fake_request), \
            patch.object(g, "public_github_raw_bytes", raw or make_raw()):
        return g.repo_name_files("me", **kwargs)


def rows(out):
    return {r["name"]: r for r in out["repositories"]}


def test_candidates_order_and_validation():
    names = g.repo_name_candidates("My-Tool.x")
    assert names[:5] == ["My-Tool.x.py", "My-Tool.x.js", "My-Tool.x.mjs", "My-Tool.x.sh", "My-Tool.x.dart"]
    assert "my_tool_x.py" in names and "src/my_tool_x/my-tool.x.dart" in names
    assert names.index("my_tool_x.py") < names.index("src/My_Tool_x/My-Tool.x.py")
    assert len(names) == len(set(names))
    assert g.repo_name_candidates("abc") == ["abc" + e for e in g.REPO_NAME_EXTENSIONS] + \
        ["src/abc/abc" + e for e in g.REPO_NAME_EXTENSIONS]
    for bad in ("", "..", "a/b", None):
        with pytest.raises(ValueError):
            g.repo_name_candidates(bad)


def test_classify_imports():
    files = {"pkg/a.py": "", "helper.py": "", "src/mod/__init__.py": "", "pkg/near.py": ""}
    out = g.classify_python_imports(b"import os, sys\nimport helper\nimport mod.x\nimport near\nimport yaml\nfrom . import z\n",
                                    "pkg/a.py", files)
    assert out == {"status": "ok", "stdlib": ["os", "sys"], "local": ["helper", "mod", "near"],
                   "external": ["yaml"], "relative": True}
    assert g.classify_python_imports(b"def (:\n", "a.py", {})["status"] == "syntax_error"
    assert g.classify_python_imports(b"x = 1\0", "a.py", {})["status"] == "syntax_error"


def test_full_scan_flags_and_hits():
    out = run_repo_name_files()
    assert out["schema"] == "gh-identity-repo-name-files/1" and out["write"] is False
    assert out["parent"] == {"repository": "me/me", "ref": "main", "commit": P, "errors": {}}
    r = rows(out)
    assert set(r) == {"me", "Tool-Kit", "lib", "broken", "odd"}
    tool = r["Tool-Kit"]
    assert [h["path"] for h in tool["found"]] == ["Tool-Kit.sh", "tool_kit.py"]
    sh, py = tool["found"]
    assert sh["raw_url"] == "https://raw.githubusercontent.com/me/Tool-Kit/main/Tool-Kit.sh"
    assert sh["raw_commit_url"] == "https://raw.githubusercontent.com/me/Tool-Kit/" + A + "/Tool-Kit.sh"
    assert sh["raw_status"] == "ok" and sh["raw_blob_match"] is True and sh["imports"] is None
    assert py["blob_sha"] == sha(TOOL_SRC) and py["catalogued"] is True and py["location"] == "top"
    assert py["imports"]["local"] == ["helper"] and py["imports"]["external"] == ["requests"]
    assert py["needs_bundle"] is True
    assert tool["flags"] == ["unregistered_source", "consumed_uncatalogued", "needs_bundle"]
    assert tool["details"]["unregistered_source"] == ["Tool-Kit.sh"]
    assert tool["details"]["consumed_uncatalogued"] == [
        {"source": "scripts/x.py", "consumed_by": ["vendor-consumers.json"]}]
    assert tool["vendor_locks"] == ["sub/vendor.lock.json"]
    lib = r["lib"]
    assert [h["path"] for h in lib["found"]] == ["lib.js", "src/lib/lib.py"]
    assert lib["found"][1]["location"] == "src" and lib["found"][1]["imports"]["relative"] is True
    assert lib["found"][0]["raw_url"].startswith("https://raw.githubusercontent.com/me/lib/dev/")
    assert lib["flags"] == ["unregistered_source", "catalog_default_mismatch", "consumed_uncatalogued", "needs_bundle"]
    assert lib["details"]["catalog_default_mismatch"] == ["lib.py"]
    assert lib["details"]["consumed_uncatalogued"] == [{"source": "lib.js", "consumed_by": ["me/Tool-Kit:sub/vendor.lock.json"]}]
    assert r["broken"]["error"] == "not_found_or_inaccessible" and r["broken"]["flags"] == []
    odd = r["odd"]
    assert odd["default_branch"] == "main" and odd["flags"] == ["fleet_missing"]
    assert odd["other_lock_schemas"] == [{"path": "vendor.lock.json", "schema": "html-import-vendor/1"}]
    assert {e["path"] for e in out["lock_errors"]} == {"x/vendor.lock.json", "y/vendor.lock.json"}
    assert r["me"]["flags"] == []
    assert out["flags"]["fleet_missing"] == ["odd"]
    assert out["complete"] is False and out["redacted"] == 0


def test_no_raw_and_raw_failure():
    out = run_repo_name_files(fetch_raw=False, repos=["lib"])
    hit = rows(out)["lib"]["found"][1]
    assert hit["raw_status"] == "not_fetched" and hit["imports"] is None and hit["needs_bundle"] is None
    assert hit["raw_commit_url"] == "https://raw.githubusercontent.com/me/lib/" + B + "/src/lib/lib.py"
    assert rows(out)["lib"]["found"][0]["needs_bundle"] is False
    assert "needs_bundle" not in rows(out)["lib"]["flags"]
    assert out["count"] == 1 and out["complete"] is True and out["raw_errors"] == []
    out = run_repo_name_files(raw=make_raw(fail=("src/lib/lib.py",)), repos=["lib"])
    hit = rows(out)["lib"]["found"][1]
    assert hit["raw_status"] == "server_error" and hit["raw_blob_sha"] is None and hit["needs_bundle"] is None
    assert out["raw_errors"] == [{"repository": "me/lib", "path": "src/lib/lib.py", "error": "server_error"}]
    assert out["complete"] is False


def test_raw_bytes_are_fetched_at_the_tree_commit():
    refs = []
    base = make_raw()

    def raw(repository, path, ref, **kwargs):
        refs.append((repository, ref))
        return base(repository, path, ref, **kwargs)

    out = run_repo_name_files(raw=raw, repos=["lib"])
    assert ("me/lib", B) in refs and ("me/lib", "dev") not in refs
    found = rows(out)["lib"]["found"]
    assert found[0]["raw_url"].startswith("https://raw.githubusercontent.com/me/lib/dev/")
    # The commit URL names the same commit the bytes were fetched at, and quotes each path segment.
    assert [h["raw_commit_url"] for h in found] == [
        "https://raw.githubusercontent.com/me/lib/" + B + "/lib.js",
        "https://raw.githubusercontent.com/me/lib/" + B + "/src/lib/lib.py"]
    assert out["complete"] is True


def test_blob_mismatch_is_not_classified_and_marks_incomplete():
    base = make_raw()

    def raw(repository, path, ref, **kwargs):
        if path == "src/lib/lib.py":
            return b"import requests\n"
        return base(repository, path, ref, **kwargs)

    out = run_repo_name_files(raw=raw, repos=["lib"])
    hit = rows(out)["lib"]["found"][1]
    assert hit["raw_blob_match"] is False and hit["imports"] is None and hit["needs_bundle"] is None
    assert out["raw_errors"] == [{"repository": "me/lib", "path": "src/lib/lib.py", "error": "blob_mismatch"}]
    assert out["complete"] is False


@pytest.mark.parametrize("name,value", [
    ("vendor-catalog.json", {"tools": "x"}),
    ("vendor-catalog.json", b"\xff"),
    ("vendor-catalog.json", {"tools": [{"repository": "me/lib", "source": None}]}),
    ("vendor-consumers.json", {"consumers": "x"}),
    ("vendor-consumers.json", {"consumers": [{"entries": [{"repository": 1}]}]}),
    ("vendor-consumers.json", {"consumers": [{"entries": None}]}),
    ("child-heads.json", {"heads": []}),
])
def test_parent_errors_are_recorded(name, value):
    parent = dict(PARENT, **{name: value})
    out = run_repo_name_files(raw=make_raw(parent=parent), repos=["lib", "odd"])
    assert out["parent"]["errors"] == {name: "invalid_json"} and out["complete"] is False
    r = rows(out)
    if name == "vendor-catalog.json":
        assert r["lib"]["found"][0]["catalogued"] is None
        assert "unregistered_source" not in r["lib"]["flags"]
    if name == "child-heads.json":
        assert "fleet_missing" not in r["odd"]["flags"]


def test_parent_fetch_failure_and_validation():
    out = run_repo_name_files(raw=make_raw(fail=("child-heads.json",)), repos=["odd"])
    assert out["parent"]["errors"] == {"child-heads.json": "server_error"}
    with pytest.raises(ValueError):
        g.repo_name_files("bad owner!")
    with pytest.raises(ValueError):
        g.repo_name_files("me", parent_ref="")
    with pytest.raises(ValueError):
        g.repo_name_files("me", parent="nope")


def test_cli_exit_codes(capsys):
    with patch.object(g, "pages", return_value=REPOS), patch.object(g, "request", fake_request), \
            patch.object(g, "public_github_raw_bytes", make_raw()):
        assert g.main(["repo-name-files", "me", "--repos", "lib", "--no-raw"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["count"] == 1 and out["parent"]["repository"] == "me/me"
        assert g.main(["repo-name-files", "me", "--parent", "me/me", "--parent-ref", "main"]) == 1
        assert json.loads(capsys.readouterr().out)["complete"] is False


# ──── duplicate_files ─────────────────────────────────────────────

# duplicate-files: same basename across one owner's repositories, read-only, fake transport.


UP_OLD, UP_NEW, OTHER_duplicate_files = b"v1\n", b"v2\n", b"other\n"


CU, CC, CD = "a" * 40, "c" * 40, "d" * 40


OLD = "1" * 40


def sha_duplicate_files(b):
    return g.git_blob_sha(b)


def lock(files):
    data = json.dumps({"schema": "vendor-lock/1", "files": files}).encode()
    return {"encoding": "base64", "content": base64.b64encode(data).decode()}


def entry_duplicate_files(dest, data, source="tool.py", repository="me/up", commit=OLD):
    return {"repository": repository, "ref": commit, "commit": commit, "source": source, "destination": dest,
            "blob_sha": sha_duplicate_files(data), "sha256": hashlib.sha256(data).hexdigest()}


def blob(path, data):
    return {"path": path, "mode": "100644", "type": "blob", "sha": sha_duplicate_files(data)}


REPOS_duplicate_files = [
    {"name": "up", "default_branch": "main", "owner": {"login": "me"}},
    {"name": "consumer", "default_branch": "main", "owner": {"login": "me"}},
    {"name": "drifted", "default_branch": "dev", "owner": {"login": "me"}},
    {"name": "secret", "private": True, "visibility": "private", "owner": {"login": "me"}},
    {"name": "internal", "visibility": "internal", "owner": {"login": "me"}},
    {"name": "old", "archived": True, "owner": {"login": "me"}},
    {"name": "forked", "fork": True, "default_branch": "main", "owner": {"login": "me"}},
]


def test_include_forks_is_configurable():
    with patch.object(g, "pages", return_value=REPOS_duplicate_files):
        assert [r["name"] for r in g.owner_repositories("me")] == ["up", "consumer", "drifted"]
        assert [r["name"] for r in g.owner_repositories("me", include_forks=True)][-1] == "forked"


TREES = {
    CU: [blob("tool.py", UP_NEW), blob("README.md", b"r"), blob("LICENSE", b"l")],
    CC: [blob("vendor/tool.py", UP_OLD), blob("README.md", b"r"), blob("vendor.lock.json", b"x"), blob("only.py", b"o")],
    CD: [blob("scripts/tool.py", OTHER_duplicate_files), blob("README.md", b"r2")],
}


def fake(method, path, **kw):
    assert method == "GET"
    table = {
        "repos/me/up/commits/main": {"sha": CU},
        "repos/me/consumer/commits/main": {"sha": CC},
        "repos/me/drifted/commits/dev": {"sha": CD},
        f"repos/me/consumer/contents/vendor.lock.json?ref={CC}": lock([entry_duplicate_files("vendor/tool.py", UP_OLD)]),
        "repos/me/up": {"default_branch": "main", "visibility": "public"},
        "repos/me/drifted": {"default_branch": "dev", "visibility": "public"},
    }
    for c, rows in TREES.items():
        table[f"repos/{ {CU: 'me/up', CC: 'me/consumer', CD: 'me/drifted'}[c] }/git/trees/{c}?recursive=1"] = {"sha": "f" * 40, "tree": rows, "truncated": False}
    if path in table:
        return table[path]
    raise g.Error("not_found_or_inaccessible")


def run_duplicate_files(**kw):
    with patch.object(g, "pages", return_value=REPOS_duplicate_files) as pages, patch.object(g, "request", side_effect=fake):
        out = g.duplicate_files("me", **kw)
    return out, pages


def test_owner_filter_and_groups():
    out, pages = run_duplicate_files()
    assert pages.call_args.args[0] == "users/me/repos?type=owner&sort=full_name"
    assert set(out["scanned"]) == {"me/up", "me/consumer", "me/drifted"} and out["write"] is False
    assert [x["basename"] for x in out["groups"]] == ["tool.py"]           # README.md / LICENSE ignored, only.py single
    grp = out["groups"][0]
    assert grp["repositories"] == 3 and grp["distinct_contents"] == 3 and not grp["identical"]
    assert set(grp["warnings"]) == {"content_differs", "unknown_provenance", "stale_pin"}


def test_provenance_origin_stale():
    files = {f["repository"]: f for f in run_duplicate_files()[0]["groups"][0]["files"]}
    up, consumer, drifted = files["me/up"], files["me/consumer"], files["me/drifted"]
    assert consumer["provenance"] == {"repository": "me/up", "source": "tool.py", "commit": OLD, "lock_blob_sha": sha_duplicate_files(UP_OLD)}
    assert consumer["stale"] is True and consumer["matches_lock"] is True
    assert up["origin"] is True and up["consumers"] == ["me/consumer"] and up["provenance"] is None
    assert drifted["provenance"] is None and drifted["origin"] is False and drifted["stale"] is None
    c = run_duplicate_files()[0]["counts"]
    assert c["null_provenance"] == 2 and c["null_provenance_non_origin"] == 1 and c["stale"] == 1


def test_ignore_options_and_repos_override():
    out, pages = run_duplicate_files(ignore=(), extra_ignore=("tool.py",))
    assert [x["basename"] for x in out["groups"]] == ["README.md"]
    out, pages = run_duplicate_files(repos=["up", "drifted"])
    assert not pages.called and set(out["scanned"]) == {"me/up", "me/drifted"}
    assert out["groups"][0]["files"][0]["provenance"] is None


def test_identical_copies_and_errors():
    TREES[CD] = [blob("scripts/tool.py", UP_NEW)]
    try:
        out, _ = run_duplicate_files(repos=["up", "drifted", "missing"])
    finally:
        TREES[CD] = [blob("scripts/tool.py", OTHER_duplicate_files), blob("README.md", b"r2")]
    assert out["groups"][0]["identical"] is True and "content_differs" not in out["groups"][0]["warnings"]
    assert out["errors"] == [] and out["redacted"] == 1 and "missing" not in json.dumps(out)


def test_rejects_bad_owner():
    with pytest.raises(ValueError):
        g.owner_repositories("../x")


def test_markdown_and_check_exit(tmp_path):
    data = b"x = 1\n"
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "m.py").write_bytes(data)
    (tmp_path / "vendor.lock.json").write_text(json.dumps({"schema": "vendor-lock/1", "files": [entry_duplicate_files("vendor/m.py", data, source="m.py")]}))
    cli = [sys.executable, str(Path(g.__file__)), "vendor-lock-check", "--root", str(tmp_path)]
    p = subprocess.run(cli + ["--markdown", "--ci-check"], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.startswith("### gh-identity-vendor-check/1: ok") and "| vendor/m.py | ok |" in p.stdout
    (tmp_path / "vendor" / "m.py").write_bytes(b"changed\n")
    assert subprocess.run(cli + ["--ci-check"], capture_output=True).returncode == 1
    assert subprocess.run(cli, capture_output=True).returncode == 0


def test_duplicates_markdown():
    out, _ = run_duplicate_files()
    md = g.render_duplicates_markdown(out, top=5)
    assert md.startswith("### Duplicate files (me): 3 repositories")
    assert "| tool.py | 3 | 3 | 2 | 1 | up | content_differs unknown_provenance stale_pin |" in md


@pytest.mark.parametrize('failure', ['truncated', 'tree_failure', 'lock_failure', 'missing_flag', 'normal'])
@pytest.mark.parametrize('markdown', [False, True])
def test_scan_completeness_cli(failure, markdown, capsys):
    def transport(method, path, **kw):
        if path == f'repos/me/consumer/git/trees/{CC}?recursive=1':
            if failure == 'tree_failure':
                raise g.Error('http_403')
            result = fake(method, path, **kw)
            if failure == 'truncated':
                result['truncated'] = True
            elif failure == 'missing_flag':
                result.pop('truncated')
            return result
        if failure == 'lock_failure' and '/contents/' in path:
            raise g.Error('http_403')
        if failure == 'truncated' and '/contents/' in path:
            pytest.fail('must exclude truncated tree before reading locks')
        return fake(method, path, **kw)
    with patch.object(g, 'pages', return_value=REPOS_duplicate_files), patch.object(g, 'request', side_effect=transport):
        assert g.main(['duplicate-files', '--owner', 'me'] + (['--markdown'] if markdown else [])) == (0 if failure == 'normal' else 1)
    output = capsys.readouterr().out
    if markdown:
        assert ('INCOMPLETE: 1 repositories failed' in output) == (failure != 'normal')
    else:
        report = json.loads(output)
        assert report['complete'] == (failure == 'normal')
        if failure != 'normal':
            assert set(report['scanned']) == {'me/up', 'me/drifted'}
            assert report['counts']['copies'] == 2
            assert not any(f['origin'] for group in report['groups'] for f in group['files'])
            expected = {'truncated': 'truncated_tree', 'missing_flag': 'invalid_tree_response'}.get(failure, 'http_403')
            assert report['errors'] == [{'repository': 'me/consumer', 'detail': expected}]


def test_all_truncated():
    def transport(method, path, **kw):
        result = fake(method, path, **kw)
        if '/git/trees/' in path:
            result['truncated'] = True
        return result
    with patch.object(g, 'pages', return_value=REPOS_duplicate_files), patch.object(g, 'request', side_effect=transport):
        report = g.duplicate_files('me')
    assert report['complete'] is False and len(report['errors']) == 3
    assert report['scanned'] == {} and report['groups'] == []
    assert 'INCOMPLETE: 3 repositories failed' in g.render_duplicates_markdown(report)


# A repository may use any of these placements independently. The report must
# identify Flutter's python/vendor.lock.json, not just vendor.lock.json at root.
@pytest.mark.parametrize("placement", [
    "vendor.lock.json", "tool/python/vendor.lock.json", "python/vendor.lock.json",
])
def test_all_supported_lock_placements_provide_provenance(placement):
    trees = [b for b in TREES[CC] if b["path"] != "vendor.lock.json"] + [blob(placement, b"lock bytes")]
    def transport(method, path, **kw):
        if path == f"repos/me/consumer/contents/{placement}?ref={CC}":
            return lock([entry_duplicate_files("vendor/tool.py", UP_OLD)])
        return fake(method, path, **kw)
    with patch.dict(TREES, {CC: trees}), patch.object(g, "pages", return_value=REPOS_duplicate_files), \
         patch.object(g, "request", side_effect=transport):
        result = g.duplicate_files("me")
    row = next(f for f in result["groups"][0]["files"] if f["repository"] == "me/consumer")
    assert result["complete"] is True and result["errors"] == []
    assert result["scanned"]["me/consumer"]["lock_paths"] == [placement]
    assert result["scanned"]["me/consumer"]["lock"] == placement
    assert row["provenance"]["repository"] == "me/up"
    assert row["provenance"]["commit"] == OLD
    assert row["matches_lock"] is True and row["stale"] is True


def test_multiple_locks_merge_distinct_destinations_and_identical_repeats():
    trees_up = TREES[CU] + [blob("extra.py", UP_NEW)]
    trees_consumer = TREES[CC] + [
        blob("python/vendor.lock.json", b"p"), blob("tool/python/vendor.lock.json", b"t"),
        blob("vendor/extra.py", UP_OLD)]
    locks = {
        "vendor.lock.json": [entry_duplicate_files("vendor/tool.py", UP_OLD)],
        "tool/python/vendor.lock.json": [entry_duplicate_files("vendor/extra.py", UP_OLD, source="extra.py")],
        "python/vendor.lock.json": [entry_duplicate_files("vendor/tool.py", UP_OLD)],
    }
    def transport(method, path, **kw):
        for placement, entries in locks.items():
            if path == f"repos/me/consumer/contents/{placement}?ref={CC}":
                return lock(entries)
        return fake(method, path, **kw)
    with patch.dict(TREES, {CU: trees_up, CC: trees_consumer}), \
         patch.object(g, "pages", return_value=REPOS_duplicate_files), \
         patch.object(g, "request", side_effect=transport):
        result = g.duplicate_files("me")
    assert result["complete"] is True
    assert result["scanned"]["me/consumer"]["lock_paths"] == list(locks)
    rows = {grp["basename"]: grp for grp in result["groups"]}
    for name in ("tool.py", "extra.py"):
        found = next(f for f in rows[name]["files"] if f["repository"] == "me/consumer")
        assert found["provenance"]["source"] == name
        assert found["stale"] is True
        assert found["matches_lock"] is True
    assert any(x["origin"] and x["repository"] == "me/up" for x in rows["extra.py"]["files"])


@pytest.mark.parametrize("failure,expected", [
    ("conflicting_sha", "conflicting_vendor_lock"),
    ("conflicting_case", "conflicting_vendor_lock"),
    ("fetch_failure", "http_403"),
    ("invalid_response", "invalid_vendor_lock_response"),
    ("malformed_lock", "invalid_vendor_lock"),
])
def test_multiple_lock_conflicts_and_failed_fetches_exclude_repository(failure, expected):
    trees = TREES[CC] + [blob("python/vendor.lock.json", b"lock")]
    def transport(method, path, **kw):
        if path == f"repos/me/consumer/contents/vendor.lock.json?ref={CC}":
            return lock([entry_duplicate_files("vendor/tool.py", UP_OLD)])
        if path == f"repos/me/consumer/contents/python/vendor.lock.json?ref={CC}":
            if failure == "fetch_failure":
                raise g.Error("http_403")
            if failure == "invalid_response":
                return {"encoding": "none"}
            if failure == "malformed_lock":
                return {"encoding": "base64", "content": "not-base64"}
            if failure == "conflicting_case":
                return lock([entry_duplicate_files("VENDOR/tool.py", UP_NEW)])
            return lock([entry_duplicate_files("vendor/tool.py", UP_NEW)])
        return fake(method, path, **kw)
    with patch.dict(TREES, {CC: trees}), patch.object(g, "pages", return_value=REPOS_duplicate_files), \
         patch.object(g, "request", side_effect=transport):
        result = g.duplicate_files("me")
    assert result["complete"] is False
    assert result["errors"] == [{"repository": "me/consumer", "detail": expected}]
    assert "me/consumer" not in result["scanned"]
    assert all(f["repository"] != "me/consumer" for group in result["groups"] for f in group["files"])
    assert "INCOMPLETE" in g.render_duplicates_markdown(result)


def test_no_lock_is_unknown_provenance_not_collection_failure():
    with patch.object(g, "pages", return_value=REPOS_duplicate_files), patch.object(g, "request", side_effect=fake):
        result = g.duplicate_files("me", lock_paths=("python/vendor.lock.json",))
    assert result["complete"] is True and result["errors"] == []
    assert result["scanned"]["me/consumer"]["lock_paths"] == []
    assert result["counts"]["null_provenance_non_origin"] > 0


def test_different_copy_sha_is_reported_separately_from_lock_provenance():
    trees = [b for b in TREES[CC] if b["path"] != "vendor.lock.json"] + [blob("python/vendor.lock.json", b"lock")]
    def transport(method, path, **kw):
        if path == f"repos/me/consumer/contents/python/vendor.lock.json?ref={CC}":
            return lock([entry_duplicate_files("vendor/tool.py", UP_NEW)])
        return fake(method, path, **kw)
    with patch.dict(TREES, {CC: trees}), patch.object(g, "pages", return_value=REPOS_duplicate_files), \
         patch.object(g, "request", side_effect=transport):
        result = g.duplicate_files("me")
    group = next(grp for grp in result["groups"] if grp["basename"] == "tool.py")
    row = next(f for f in group["files"] if f["repository"] == "me/consumer")
    assert row["provenance"] is not None
    assert row["provenance"]["lock_blob_sha"] == sha_duplicate_files(UP_NEW)
    assert row["matches_lock"] is False and row["stale"] is False
    assert "modified_after_lock" in group["warnings"]
    assert "unknown_provenance" in group["warnings"]  # unrelated drifted copy


def test_line_wrapped_github_base64_is_accepted():
    trees = TREES[CC] + [blob("python/vendor.lock.json", b"lock")]
    def transport(method, path, **kw):
        if path == f"repos/me/consumer/contents/python/vendor.lock.json?ref={CC}":
            encoded = lock([entry_duplicate_files("vendor/tool.py", UP_OLD)])["content"]
            return {"encoding": "base64", "content": "\n".join(encoded[i:i+60] for i in range(0, len(encoded), 60))}
        return fake(method, path, **kw)
    with patch.dict(TREES, {CC: trees}), patch.object(g, "pages", return_value=REPOS_duplicate_files), \
         patch.object(g, "request", side_effect=transport):
        result = g.duplicate_files("me")
    assert result["complete"] is True
    assert result["scanned"]["me/consumer"]["lock_paths"] == ["vendor.lock.json", "python/vendor.lock.json"]


# ──── subtree_push_guards ─────────────────────────────────────────

# Push guards and history_related semantics for subtree sync.


def _commit(root, files, msg):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def world_subtree_push_guards(tmp_path):
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _git(mono, "config", "user.email", "t@e")
    _git(mono, "config", "user.name", "t")
    _commit(mono, {"sub/a.py": "a = 1\n", "top.py": "x = 1\n"}, "one")
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    return {"mono": mono, "child": child}


@needs_subtree
def test_push_blocked_when_child_trunk_not_ancestor(world_subtree_push_guards):
    mono, child = world_subtree_push_guards["mono"], world_subtree_push_guards["child"]
    split = g.git_subtree_split(mono, "sub")["sha"]
    _git(mono, "push", "-q", str(child), f"{split}:refs/heads/trunk")
    work = world_subtree_push_guards["mono"].parent / "work"
    _git(world_subtree_push_guards["mono"].parent, "clone", "-q", str(child), str(work))
    _git(work, "config", "user.email", "t@e")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "trunk")
    _commit(work, {"ahead-on-child.py": "x\n"}, "child ahead")
    _git(work, "push", "-q", "origin", "HEAD:trunk")
    with pytest.raises(ValueError, match="not an ancestor"):
        g.subtree_sync(mono, "sub", str(child), "sync/new", direction="push", remote_compare_ref="trunk")


def test_push_guard_wrapper_reports_isolated_errors(monkeypatch, world_subtree_push_guards):
    mono, child = world_subtree_push_guards["mono"], world_subtree_push_guards["child"]
    seen = []

    def isolated(*args, **kwargs):
        seen.append(kwargs.get("ref_name"))
        raise ValueError("child-only paths not present in split: only-on-child.txt")

    monkeypatch.setattr(g, "_subtree_push_guards_isolated", isolated)
    listing = "1111111111111111111111111111111111111111 refs/heads/trunk\n"
    net = lambda args, ok=(0,): (listing.encode(), False, 0)
    with pytest.raises(ValueError, match="child-only paths"):
        g._subtree_push_guards(mono, "/nonexistent", "sub", "2" * 40, "trunk", net, compare_ref="trunk")
    assert seen == ["trunk"]


def _ls_net(root):
    return lambda args, ok=(0,): g._git_run(root, args, ok=ok, max_bytes=1024 * 1024)


@needs_subtree
def test_real_guard_detects_child_only_paths(world_subtree_push_guards):
    """The real isolated guard (no mocks): the child trunk has a path the new split dropped."""
    mono, child = world_subtree_push_guards["mono"], world_subtree_push_guards["child"]
    _commit(mono, {"sub/extra.txt": "x\n"}, "add extra")
    first = g.git_subtree_split(mono, "sub")["sha"]
    _git(mono, "push", "-q", str(child), f"{first}:refs/heads/trunk")
    _git(mono, "rm", "-q", "sub/extra.txt")
    _git(mono, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "drop extra")
    second = g.git_subtree_split(mono, "sub")["sha"]
    with pytest.raises(ValueError, match="child-only paths not present in split: extra.txt"):
        g._subtree_push_guards_isolated(mono, str(child), first, second, 60, ref_name="trunk")
    with pytest.raises(ValueError, match="child-only paths not present in split: extra.txt"):
        g._subtree_push_guards(mono, str(child), "sub", second, "trunk", _ls_net(mono), compare_ref="trunk")
    with pytest.raises(ValueError, match="child-only paths"):
        g.subtree_sync(mono, "sub", str(child), "sync/new", direction="push", remote_compare_ref="trunk")


@needs_subtree
def test_real_guard_passes_and_names_ref_on_ancestry_failure(world_subtree_push_guards):
    mono, child = world_subtree_push_guards["mono"], world_subtree_push_guards["child"]
    first = g.git_subtree_split(mono, "sub")["sha"]
    _git(mono, "push", "-q", str(child), f"{first}:refs/heads/trunk")
    _commit(mono, {"sub/b.py": "b = 1\n"}, "two")
    second = g.git_subtree_split(mono, "sub")["sha"]
    assert g._subtree_push_guards_isolated(mono, str(child), first, second, 60) == []
    out = g._subtree_push_guards(mono, str(child), "sub", second, "trunk", _ls_net(mono), compare_ref="trunk")
    assert out["compare_sha"] == first and out["checked_in"] == "temporary_clone"
    with pytest.raises(ValueError, match="remote ref trunk is not an ancestor"):
        g._subtree_push_guards_isolated(mono, str(child), second, first, 60, ref_name="trunk")


def test_isolated_guard_failure_branches(world_subtree_push_guards, monkeypatch, tmp_path):
    mono = world_subtree_push_guards["mono"]
    head = _git(mono, "rev-parse", "HEAD")
    for bad in (0, -1, float("inf"), float("nan"), True, "1"):
        with pytest.raises(ValueError, match="timeout"):
            g._subtree_push_guards_isolated(mono, str(world_subtree_push_guards["child"]), head, head, bad)
    with pytest.raises(ValueError, match="could not fetch remote ref"):
        g._subtree_push_guards_isolated(mono, str(tmp_path / "missing.git"), head, head, 60)
    real = g._git_run

    def no_clone(run_root, args, **kw):
        if args[:1] == ["clone"]:
            return b"", False, 128
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", no_clone)
    with pytest.raises(ValueError, match="could not clone"):
        g._subtree_push_guards_isolated(mono, str(world_subtree_push_guards["child"]), head, head, 60)
    monkeypatch.setattr(g, "_git_run", real)
    monkeypatch.setattr(g, "_commit_root_tree", lambda root, commit: None)
    with pytest.raises(ValueError, match="could not resolve trees"):
        g._subtree_push_guards_isolated(mono, str(mono), head, head, 60)


@needs_subtree
def test_push_refuses_branch_created_concurrently_even_as_fast_forward(world_subtree_push_guards, monkeypatch):
    """Between the existence check and the push another writer creates the branch at an
    ancestor of the split; without the empty lease the push would fast-forward it."""
    mono, child = world_subtree_push_guards["mono"], world_subtree_push_guards["child"]
    ancestor = g.git_subtree_split(mono, "sub")["sha"]
    _git(mono, "push", "-q", str(child), f"{ancestor}:refs/heads/trunk")
    _commit(mono, {"sub/b.py": "b = 1\n"}, "two")
    real = g._git_run
    pushes = []

    def racing(run_root, args, **kw):
        if args[:1] == ["push"] and kw.get("inherit_auth"):
            pushes.append(list(args))
            _git(child.parent, "--git-dir", str(child), "update-ref", "refs/heads/sync/race", ancestor)
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", racing)
    with pytest.raises(g.Error) as e:
        g.subtree_sync(mono, "sub", str(child), "sync/race", direction="push", write=True)
    assert e.value.code == "subtree_push_failed"
    assert pushes and pushes[0][1] == "--force-with-lease=refs/heads/sync/race:"
    assert _git(child.parent, "--git-dir", str(child), "rev-parse", "refs/heads/sync/race") == ancestor


@needs_subtree
@pytest.mark.parametrize("squash", [False, True])
def test_history_related_from_subtree_add_only_for_actual_child(tmp_path, squash):
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(child), str(work))
    _git(work, "config", "user.email", "t@e")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "trunk")
    _commit(work, {"a.py": "a = 1\n"}, "child one")
    _git(work, "push", "-q", "origin", "HEAD:trunk")
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _git(mono, "config", "user.email", "t@e")
    _git(mono, "config", "user.name", "t")
    _commit(mono, {"top.py": "x = 1\n"}, "mono one")
    before_add = _git(mono, "rev-parse", "HEAD")
    _git(mono, "subtree", "add", "-q", "--prefix=sub", *(["--squash"] if squash else []), str(child), "trunk")
    status = g.subtree_sync_status(mono, "sub", work)
    assert status["history_related"] and status["trees_match"]
    assert status["state"] == "in_sync"
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _commit(other, {"a.py": "a = 1\n"}, "unrelated same tree")
    status = g.subtree_sync_status(mono, "sub", other)
    assert status["history_related"] is False
    assert status["trees_match"] and status["state"] == "subtree_add_required"
    assert "git subtree add" in (status.get("guidance") or "")
    assert status["other_ahead"] == 1 and status["monorepo_ahead"] > 0
    assert not status["split_in_other"] and not status["other_head_in_split"]
    child_history = set(g._rev_list(work, _git(work, "rev-parse", "HEAD")))
    assert g._monorepo_has_subtree_trailers(mono, "sub", "HEAD", child_history)
    assert not g._monorepo_has_subtree_trailers(mono, "sub", before_add, child_history)


@needs_subtree
def test_history_related_mutation_falls_back_to_shared_shas_only(tmp_path, monkeypatch):
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _commit(mono, {"sub/a.py": "v\n", "top.py": "t\n"}, "one")
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    good = g.git_subtree_split(mono, "sub")["sha"]
    _git(other, "fetch", "-q", str(mono), good)
    _git(other, "reset", "-q", "--hard", good)
    monkeypatch.setattr(g, "_monorepo_has_subtree_trailers", lambda *a, **k: False)
    monkeypatch.setattr(g, "_git_merge_base_exists", lambda *a, **k: False)
    status = g.subtree_sync_status(mono, "sub", other)
    assert status["history_related"] and status["state"] == "in_sync"
    monkeypatch.setattr(g, "_subtree_history_related", lambda *a, **k: False)
    status = g.subtree_sync_status(mono, "sub", other)
    assert status["history_related"] is False
    assert status["trees_match"] and status["state"] == "subtree_add_required"
    assert status.get("guidance")


@needs_subtree
def test_push_tree_gate_called_once_per_write(world_subtree_push_guards, monkeypatch):
    mono, child = world_subtree_push_guards["mono"], world_subtree_push_guards["child"]
    calls = []
    real = g._subtree_push_split_tree_gate

    def gate(*a, **k):
        calls.append(1)
        if len(calls) > 1:
            raise g.Error("stale_subtree_split")
        return real(*a, **k)

    monkeypatch.setattr(g, "_subtree_push_split_tree_gate", gate)
    g.subtree_sync(mono, "sub", str(child), "sync/once", direction="push", write=True)
    assert len(calls) == 1


def test_explicit_compare_ref_missing_is_an_error_not_a_fallback(world_subtree_push_guards):
    calls = []

    def net(args, ok=(0,)):
        calls.append(args[-1])
        ref = args[-1]
        return ((("1" * 40) + "\t" + ref + "\n").encode() if ref == "refs/heads/main" else b"", False, 0)

    with pytest.raises(ValueError, match="remote compare ref release was not found"):
        g._subtree_push_guards(world_subtree_push_guards["mono"], str(world_subtree_push_guards["child"]), "sub", "2" * 40, "main", net,
                               compare_ref="release")
    assert calls == ["refs/heads/release"]


def test_default_compare_candidates_are_deduplicated(world_subtree_push_guards):
    calls = []

    def net(args, ok=(0,)):
        calls.append(args[-1])
        return b"", False, 2

    out = g._subtree_push_guards(world_subtree_push_guards["mono"], str(world_subtree_push_guards["child"]), "sub", "2" * 40, "main", net)
    assert out["checked_in"] == "ls-remote_only" and out["compare_ref"] is None
    assert calls == ["refs/heads/main", "refs/heads/master", "refs/heads/trunk"]


# ──── subtree_split ───────────────────────────────────────────────

# subtree-split: read-only `git subtree split --prefix` returning the split SHA.


def _refs(root):
    return _git(root, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.fixture
def repo_subtree_split(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("x\n")
    (tmp_path / "top.txt").write_text("t\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return tmp_path


@needs_subtree
def test_split_returns_sha_and_changes_no_refs(repo_subtree_split):
    before, head = _refs(repo_subtree_split), _git(repo_subtree_split, "rev-parse", "HEAD")
    out = g.git_subtree_split(repo_subtree_split, "sub/")
    assert out["reason"] is None and out["write"] is False and out["prefix"] == "sub"
    assert out["source"] == head
    assert _git(repo_subtree_split, "rev-parse", out["sha"] + "^{tree}") == _git(repo_subtree_split, "rev-parse", "HEAD:sub")
    assert _refs(repo_subtree_split) == before and _git(repo_subtree_split, "rev-parse", "HEAD") == head
    assert not (repo_subtree_split / ".git" / "subtree-cache").exists()
    assert _git(repo_subtree_split, "status", "--porcelain") == ""


@needs_subtree
def test_existing_subtree_cache_is_preserved(repo_subtree_split):
    keep = repo_subtree_split / ".git" / "subtree-cache" / "1"
    keep.mkdir(parents=True)
    g.git_subtree_split(repo_subtree_split, "sub")
    assert sorted(p.name for p in keep.parent.iterdir()) == ["1"]


@needs_subtree
def test_cli_subtree_split(repo_subtree_split):
    proc = subprocess.run([sys.executable, str(Path(g.__file__)), "subtree-split", "--root", str(repo_subtree_split), "--prefix", "sub"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert len(json.loads(proc.stdout)["sha"]) == 40


def test_missing_subtree_is_explicit(repo_subtree_split, monkeypatch):
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: False)
    out = g.git_subtree_split(repo_subtree_split, "sub")
    assert out["sha"] is None and out["reason"] == "subtree_unavailable"


def test_missing_prefix(repo_subtree_split):
    assert g.git_subtree_split(repo_subtree_split, "nope")["reason"] == "prefix_not_found"
    assert g.git_subtree_split(repo_subtree_split, "top.txt")["reason"] == "prefix_not_found"


@pytest.mark.parametrize("prefix", ["--rejoin", "-b", "sub/--branch=x", "", "/abs", "../out", "sub/../..", ".git", ".", "a\nb"])
def test_rejects_unsafe_prefix(repo_subtree_split, prefix):
    with pytest.raises(ValueError):
        g.git_subtree_split(repo_subtree_split, prefix)


@pytest.mark.parametrize("revision", ["--all", "-n", "nope", ""])
def test_rejects_bad_revision(repo_subtree_split, revision):
    with pytest.raises(ValueError):
        g.git_subtree_split(repo_subtree_split, "sub", revision)


def test_never_passes_ref_changing_options(repo_subtree_split, monkeypatch):
    calls = []
    real = g._git_run
    fake = "a" * 40
    def spy(root, args, **kw):
        calls.append(list(args))
        if args[:1] == ["subtree"]:
            return (fake + "\n").encode(), False, 0
        return real(root, args, **kw)
    def fake_match(monorepo_root, source, prefix, split_sha, *, split_root=None):
        if split_sha == fake:
            return {"prefix_tree": "t", "split_tree": "t", "trees_match": True}
        return g._subtree_split_matches_prefix_tree(monorepo_root, source, prefix, split_sha, split_root=split_root)
    monkeypatch.setattr(g, "_git_run", spy)
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: True)
    monkeypatch.setattr(g, "_subtree_split_matches_prefix_tree", fake_match)
    assert g.git_subtree_split(repo_subtree_split, "sub")["sha"] == fake
    sub = [c for c in calls if c[:1] == ["subtree"]][0]
    assert sub[:2] == ["subtree", "split"]
    assert not any(a.startswith(("--branch", "-b", "--rejoin", "--onto", "--annotate")) for a in sub)


# ──── subtree_stale_split ─────────────────────────────────────────

# Regression: git subtree split can exit 0 with a stale SHA; tree validation must catch it.


ENV = ["-c", "user.email=t@e", "-c", "user.name=t"]


def _commit_subtree_stale_split(root, files, msg):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, *ENV, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def mono(tmp_path):
    root = tmp_path / "mono"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    old = _commit_subtree_stale_split(root, {"sub/a.py": "v1\n", "top.py": "t\n"}, "one")
    _commit_subtree_stale_split(root, {"sub/a.py": "v2\n"}, "two")
    return root, old


@needs_subtree
def test_stale_subtree_split_corrected(mono, monkeypatch):
    root, stale_sha = mono
    real = g._git_run

    def spy(run_root, args, **kw):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", spy)
    out = g.git_subtree_split(root, "sub")
    check = g._subtree_split_matches_prefix_tree(root, out["source"], "sub", out["sha"])
    assert check["trees_match"]
    assert out["sha"] != stale_sha
    assert out["warning"] in ("subtree_split_stale", "subtree_split_stale_history_short")
    assert out["method"] == "filter-branch"


@needs_subtree
def test_stale_split_sync_status_in_sync_not_other_ahead(mono, tmp_path, monkeypatch):
    root, stale_sha = mono
    real = g._git_run

    def spy(run_root, args, **kw):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", spy)
    good = g.git_subtree_split(root, "sub")
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "fetch", "-q", str(root), good["sha"])
    _git(other, "reset", "-q", "--hard", good["sha"])
    status = g.subtree_sync_status(root, "sub", other)
    assert status["trees_match"] and status["state"] == "in_sync" and status["other_ahead"] == 0


@needs_subtree
def test_tree_check_mutation_breaks_stale_detection(mono, monkeypatch):
    """If tree validation is disabled, stale split is wrongly accepted."""
    root, stale_sha = mono
    real = g._git_run

    def spy(run_root, args, **kw):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", spy)

    def always_match(*args, **kwargs):
        return {"prefix_tree": "a", "split_tree": "a", "trees_match": True}

    monkeypatch.setattr(g, "_subtree_split_matches_prefix_tree", always_match)
    out = g.git_subtree_split(root, "sub")
    assert out["sha"] == stale_sha


@needs_subtree
def test_push_write_refuses_stale_split(world_subtree_stale_split, monkeypatch):
    mono, child = world_subtree_stale_split["mono"], world_subtree_stale_split["child"]
    net_calls = []

    def fake_split(root, prefix, revision="HEAD", *, timeout=600):
        return {"schema": "gh-identity-subtree-split/1", "prefix": "sub", "write": False,
                "source": _git(mono, "rev-parse", "HEAD"), "sha": "0" * 40, "reason": None,
                "method": "subtree"}

    real_run = g._git_run

    def net_spy(run_root, args, **kw):
        if kw.get("inherit_auth"):
            net_calls.append(args)
        return real_run(run_root, args, **kw)

    monkeypatch.setattr(g, "git_subtree_split", fake_split)
    monkeypatch.setattr(g, "_git_run", net_spy)
    monkeypatch.setattr(g, "_subtree_split_matches_prefix_tree",
                        lambda *a, **k: {"prefix_tree": "a" * 40, "split_tree": "b" * 40, "trees_match": False})
    with pytest.raises(g.Error) as err:
        g.subtree_sync(mono, "sub", str(child), "sync/stale-test", direction="push", write=True)
    assert err.value.code == "stale_subtree_split"
    assert not any(a[:1] == ["push"] for a in net_calls)


@needs_subtree
def test_push_uses_validated_split_not_subtree_push(mono, tmp_path, monkeypatch):
    """`git subtree push` re-splits internally; push the validated split SHA directly."""
    root, stale_sha = mono
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    mono, child = root, child
    good = g.git_subtree_split(mono, "sub")["sha"]
    stale = stale_sha
    real = g._git_run
    push_specs = []

    def spy(run_root, args, **kw):
        if args[:2] == ["subtree", "split"]:
            return (stale + "\n").encode(), False, 0
        if args[:2] == ["subtree", "push"]:
            raise AssertionError("git subtree push must not run")
        if args[:1] == ["push"] and kw.get("inherit_auth"):
            push_specs.append(args)
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", spy)
    out = g.subtree_sync(mono, "sub", str(child), "sync/x", direction="push", write=True)
    assert out["verified"]["ok"]
    assert _git(child, "rev-parse", "refs/heads/sync/x") == good
    assert _git(child, "rev-parse", "refs/heads/sync/x") != stale
    assert push_specs and push_specs[0][-1] == f"{good}:refs/heads/sync/x"
    assert push_specs[0][1] == "--force-with-lease=refs/heads/sync/x:"
    assert out["split"]["method"] == "filter-branch" and out["split"]["subtree_sha"] == stale
    assert out["split"]["fallback_used"] is True


@needs_subtree
def test_same_tree_unrelated_history_is_subtree_add_required(mono, tmp_path):
    root, _ = mono
    good = g.git_subtree_split(root, "sub")["sha"]
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _commit_subtree_stale_split(other, {"a.py": "v2\n"}, "independent history")
    status = g.subtree_sync_status(root, "sub", other)
    assert status["trees_match"] and not status["history_related"]
    assert status["state"] == "subtree_add_required"
    assert status["other_ahead"] > 0
    assert not status["split_in_other"] and not status["other_head_in_split"]
    assert status["monorepo_ahead"] == len(g._rev_list(root, good))
    rec = g.sync_provenance(root, "sub", other)
    assert rec["state"] == "subtree_add_required"
    assert rec["trees_match"] and rec["history_related"] is False


@needs_subtree
def test_push_revalidates_after_planning_and_never_writes_on_mismatch(world_subtree_stale_split, monkeypatch):
    """Planning passes; only the check immediately before push sees the mismatch."""
    mono, child = world_subtree_stale_split["mono"], world_subtree_stale_split["child"]
    real_check, real_guard, real_run = (g._subtree_split_matches_prefix_tree,
                                       g._subtree_push_guards, g._git_run)
    planning_done, checks, pushes = False, [], []

    def guard(*args, **kwargs):
        nonlocal planning_done
        result = real_guard(*args, **kwargs)
        planning_done = True
        return result

    def check(*args, **kwargs):
        checks.append(planning_done)
        result = real_check(*args, **kwargs)
        return {**result, "trees_match": False} if planning_done else result

    def run(root, args, **kwargs):
        if args[0] == "push" or args[:2] == ["subtree", "push"]:
            pushes.append(args)
        return real_run(root, args, **kwargs)

    monkeypatch.setattr(g, "_subtree_push_guards", guard)
    monkeypatch.setattr(g, "_subtree_split_matches_prefix_tree", check)
    monkeypatch.setattr(g, "_git_run", run)
    before = _git(child, "for-each-ref")
    with pytest.raises(g.Error) as err:
        g.subtree_sync(mono, "sub", str(child), "sync/revalidate", direction="push", write=True)
    assert err.value.code == "stale_subtree_split"
    assert False in checks and checks[-1] is True
    assert not pushes
    assert _git(child, "for-each-ref") == before


@needs_subtree
@pytest.mark.parametrize("command", ["split", "status", "preflight"])
@pytest.mark.parametrize("fetch_head_exists", [False, True])
@pytest.mark.parametrize("root_form", ["absolute", "relative", "current"])
def test_fallback_preserves_checkout_metadata(mono, tmp_path, monkeypatch, command, fetch_head_exists, root_form):
    root, stale_sha = mono
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _commit_subtree_stale_split(other, {"a.py": "v2\n"}, "other")
    _git(root, "pack-refs", "--all")
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir"))
    fetch_head = git_dir / "FETCH_HEAD"
    if fetch_head_exists:
        fetch_head.write_bytes(b"existing FETCH_HEAD\n")
    metadata = [git_dir / name for name in ("HEAD", "index", "packed-refs", "FETCH_HEAD", "config")]
    before = {p: p.read_bytes() if p.exists() else None for p in metadata}
    refs = _git(root, "for-each-ref")
    work = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and git_dir not in p.parents}
    real = g._git_run

    def run(run_root, args, **kwargs):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", run)
    root_arg = root
    if root_form == "relative":
        monkeypatch.chdir(root.parent)
        root_arg = Path(root.name)
    elif root_form == "current":
        monkeypatch.chdir(root)
        root_arg = Path(".")
    calls = {"split": lambda: g.git_subtree_split(root_arg, "sub"),
             "status": lambda: g.subtree_sync_status(root_arg, "sub", other),
             "preflight": lambda: g.split_preflight(root_arg, "sub")}
    result = calls[command]()
    assert result["reason"] is None
    assert result["fallback_used"] and result["fallback_elapsed_seconds"] > 0
    assert result.get("method", result.get("split_method")) == "filter-branch"
    split_sha = result.get("sha", result.get("split_sha"))
    assert g._subtree_split_matches_prefix_tree(root, result["source"], "sub", split_sha)["trees_match"]
    assert {p: p.read_bytes() if p.exists() else None for p in metadata} == before
    assert _git(root, "for-each-ref") == refs
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and git_dir not in p.parents} == work
    assert not (git_dir / "refs/original").exists()
    assert not (git_dir / "subtree-cache").exists()


@needs_subtree
def test_fallback_handles_unreferenced_revision(mono, monkeypatch):
    root, stale_sha = mono
    source = _commit_subtree_stale_split(root, {"sub/a.py": "unreferenced\n"}, "dangling source")
    _git(root, "reset", "--hard", "HEAD~1")
    real = g._git_run

    def run(run_root, args, **kwargs):
        if args[:2] == ["subtree", "split"]:
            return (stale_sha + "\n").encode(), False, 0
        return real(run_root, args, **kwargs)

    monkeypatch.setattr(g, "_git_run", run)
    result = g.git_subtree_split(root, "sub", source)
    assert result["reason"] is None and result["fallback_used"]
    assert result["source"] == source
    assert g._subtree_split_matches_prefix_tree(root, source, "sub", result["sha"])["trees_match"]


@pytest.mark.parametrize("key", ["GIT_DIR", "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "PATH"])
def test_extra_env_cannot_override_sanitized_environment(mono, key):
    with pytest.raises(ValueError, match="unsupported extra_env"):
        g._git_run(mono[0], ["rev-parse", "HEAD"], extra_env={key: "elsewhere"})


def test_split_and_fallback_share_timeout_budget(monkeypatch):
    now, budgets = [100.0], []
    monkeypatch.setattr(g, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: True)
    monkeypatch.setattr(g, "_prefix_tree_at", lambda *a: "c" * 40)
    monkeypatch.setattr(g, "_subtree_cache_cleanup", lambda root: contextlib.nullcontext())
    monkeypatch.setattr(g, "_subtree_split_matches_prefix_tree",
                        lambda *a: {"prefix_tree": "c" * 40, "split_tree": "d" * 40, "trees_match": False})

    def run(root, args, **kwargs):
        if args[0] == "rev-parse":
            return b"a" * 40, False, 0
        if args[0] == "cat-file":
            return b"tree", False, 0
        assert args[:2] == ["subtree", "split"]
        budgets.append(kwargs["timeout"])
        now[0] = 140.0
        return b"b" * 40, False, 0

    def fallback(root, prefix, source, timeout):
        budgets.append(timeout)
        now[0] = 150.0
        return None

    monkeypatch.setattr(g, "_git_run", run)
    monkeypatch.setattr(g, "_subtree_split_filter_branch", fallback)
    result = g.git_subtree_split(".", "sub", timeout=100)
    assert budgets == [100.0, 60.0]
    assert result["fallback_used"] and result["fallback_elapsed_seconds"] == 10.0
    assert result["reason"] == "stale_subtree_split"


@pytest.fixture
def world_subtree_stale_split(tmp_path):
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _git(mono, "config", "user.email", "t@e")
    _git(mono, "config", "user.name", "t")
    _commit_subtree_stale_split(mono, {"sub/a.py": "a = 1\n", "top.py": "x = 1\n"}, "one")
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    return {"mono": mono, "child": child}


# ──── subtree_sync ────────────────────────────────────────────────

# subtree-sync-status / sync-provenance / split-preflight: local, read-only.


def _commit_subtree_sync(root, files, msg):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, *ENV, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def mono_subtree_sync(tmp_path):
    root = tmp_path / "mono"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit_subtree_sync(root, {"sub/a.py": "import os\n", "top.py": "import sub_mod\n"}, "one")
    _commit_subtree_sync(root, {"sub/b.py": "x = 1\n"}, "two")
    return root


def _clone_split(mono, tmp_path):
    split = g.git_subtree_split(mono, "sub")["sha"]
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "fetch", "-q", str(mono), split)
    _git(other, "reset", "-q", "--hard", split)
    return other


@needs_subtree
def test_in_sync_and_read_only(mono_subtree_sync, tmp_path):
    other = _clone_split(mono_subtree_sync, tmp_path)
    before = (_refs(mono_subtree_sync), _refs(other))
    out = g.subtree_sync_status(mono_subtree_sync, "sub", other)
    assert out["state"] == "in_sync" and out["split_in_other"] and out["write"] is False
    assert (_refs(mono_subtree_sync), _refs(other)) == before


@needs_subtree
def test_monorepo_ahead(mono_subtree_sync, tmp_path):
    other = _clone_split(mono_subtree_sync, tmp_path)
    _commit_subtree_sync(mono_subtree_sync, {"sub/c.py": "y = 2\n"}, "three")
    _commit_subtree_sync(mono_subtree_sync, {"top.py": "z = 3\n"}, "outside only")
    out = g.subtree_sync_status(mono_subtree_sync, "sub", other)
    assert out["state"] == "monorepo_ahead" and out["monorepo_ahead"] == 1 and out["other_ahead"] == 0


@needs_subtree
def test_other_ahead_and_other_only_paths(mono_subtree_sync, tmp_path):
    other = _clone_split(mono_subtree_sync, tmp_path)
    _commit_subtree_sync(other, {"pyproject.toml": "[project]\n"}, "repo-only file")
    assert g.subtree_sync_status(mono_subtree_sync, "sub", other)["state"] == "other_ahead"
    out = g.subtree_sync_status(mono_subtree_sync, "sub", other, other_only_paths=["pyproject.toml"])
    assert out["state"] == "in_sync" and out["other_only_ignored"] == 1
    _commit_subtree_sync(other, {"a.py": "import sys\n"}, "real change")
    out = g.subtree_sync_status(mono_subtree_sync, "sub", other, other_only_paths=["pyproject.toml"])
    assert out["state"] == "other_ahead" and out["other_ahead"] == 1


@needs_subtree
def test_diverged_and_unrelated(mono_subtree_sync, tmp_path):
    other = _clone_split(mono_subtree_sync, tmp_path)
    _commit_subtree_sync(other, {"a.py": "1\n"}, "other side")
    _commit_subtree_sync(mono_subtree_sync, {"sub/a.py": "2\n"}, "mono side")
    assert g.subtree_sync_status(mono_subtree_sync, "sub", other)["state"] == "diverged"
    alien = tmp_path / "alien"
    alien.mkdir()
    _git(alien, "init", "-q", "-b", "main")
    _commit_subtree_sync(alien, {"x": "x\n"}, "alien")
    assert g.subtree_sync_status(mono_subtree_sync, "sub", alien)["state"] == "unrelated"


def test_sync_status_missing_subtree(mono_subtree_sync, tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: False)
    out = g.subtree_sync_status(mono_subtree_sync, "sub", mono_subtree_sync)
    assert out["state"] is None and out["reason"] == "subtree_unavailable"


def test_sync_status_rejects_bad_input(mono_subtree_sync):
    with pytest.raises(ValueError):
        g.subtree_sync_status(mono_subtree_sync, "--rejoin", mono_subtree_sync)
    with pytest.raises(ValueError):
        g.subtree_sync_status(mono_subtree_sync, "sub", mono_subtree_sync, other_ref="--all")


@needs_subtree
def test_provenance_record_and_verify(mono_subtree_sync, tmp_path):
    other = _clone_split(mono_subtree_sync, tmp_path)
    rec = g.sync_provenance(mono_subtree_sync, "sub", other, direction="push", generated_at="2026-01-01T00:00:00Z")
    assert rec["schema"] == "gh-identity-sync-provenance/1" and rec["state"] == "in_sync"
    assert rec["trailer"].startswith("Subtree-Split: " + rec["monorepo"]["sha"])
    out = tmp_path / "out"
    g.write_sync_provenance(rec, out)
    first = (out / "sync-provenance.json").read_bytes()
    g.write_sync_provenance(g.sync_provenance(mono_subtree_sync, "sub", other, direction="push", generated_at="2026-01-01T00:00:00Z"), out)
    assert (out / "sync-provenance.json").read_bytes() == first
    _git(other, *ENV, "commit", "-q", "--allow-empty", "-m", "sync\n\n" + rec["trailer"])
    ok = g.verify_sync_provenance(other, "HEAD", monorepo_root=mono_subtree_sync)
    assert ok["ok"] and ok["trailers"][0]["recomputed"] == rec["split_sha"]
    bad = rec["trailer"].replace(rec["split_sha"], "0" * 40)
    _git(other, *ENV, "commit", "-q", "--allow-empty", "-m", "sync\n\n" + bad)
    assert g.verify_sync_provenance(other, "HEAD", monorepo_root=mono_subtree_sync)["reason"] == "mismatch"
    _git(other, *ENV, "commit", "-q", "--allow-empty", "-m", "plain")
    assert g.verify_sync_provenance(other, "HEAD")["reason"] == "no_trailer"


@pytest.fixture
def cross(tmp_path):
    """a/ and b/ import each other; pyproject and sys.path refer to a/."""
    root = tmp_path / "cross"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit_subtree_sync(root, {
        "a/amod.py": "import json\nimport httpx\nfrom bmod import f\nimport sys\nsys.path.insert(0, 'b')\n",
        "a/tests/test_a.py": "import amod\n",
        "b/bmod.py": "from amod import g\nimport sys\nsys.path.insert(0, 'a/')\n",
        "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["a", "b"]\n',
        "run.toml": 'cmd = ["python", "a/amod.py"]\n',
    }, "init")
    return root


@needs_subtree
def test_split_preflight_lists_cross_references(cross):
    out = g.split_preflight(cross, "a")
    assert out["schema"] == "gh-identity-split-preflight/1" and out["write"] is False
    assert out["third_party_imports"] == ["httpx"]
    assert {"file": "b/bmod.py", "line": 1, "kind": "import", "target": "amod"} in out["inbound_refs"]
    assert any(r["kind"] == "sys.path" and r["file"] == "b/bmod.py" for r in out["inbound_refs"])
    assert "run.toml:1" in out["config_refs"] and "pyproject.toml:2" in out["config_refs"]
    assert {"file": "a/amod.py", "line": 3, "kind": "import", "target": "bmod"} in out["outbound_refs"]
    assert out["history_commits"] == 1 and out["ok"] is False
    assert {c["id"]: c["ok"] for c in out["checks"]}["split"] is True


def test_split_preflight_missing_prefix_and_validation(cross):
    assert g.split_preflight(cross, "nope")["reason"] == "prefix_not_found"
    for bad in ("../x", "-x", "/abs"):
        with pytest.raises(ValueError):
            g.split_preflight(cross, bad)


@needs_subtree
def test_cli_subtree_sync(mono_subtree_sync, tmp_path):
    other = _clone_split(mono_subtree_sync, tmp_path)
    run = lambda *a: subprocess.run([sys.executable, str(Path(g.__file__)), *a], capture_output=True, text=True)
    p = run("subtree-sync-status", "--root", str(mono_subtree_sync), "--prefix", "sub", "--other-root", str(other))
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["state"] == "in_sync"
    p = run("split-preflight", "--root", str(mono_subtree_sync), "--prefix", "sub")
    assert p.returncode == 0, p.stderr
    p = run("sync-provenance", "--root", str(mono_subtree_sync), "--prefix", "sub", "--other-root", str(other), "--output-dir", str(tmp_path / "o"))
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "o" / "sync-provenance.json").exists()


@pytest.mark.parametrize("call", [
    lambda m: g.subtree_sync_status(m, "--rejoin", m),
    lambda m: g.subtree_sync_status(m, "sub", m, other_ref="--all"),
    lambda m: g.subtree_sync_status(m, "sub", m, revision="--all"),
    lambda m: g.subtree_sync_status(m, "sub", m, other_ref="missing-ref"),
    lambda m: g.sync_provenance(m, "sub", m, other_ref="--all"),
    lambda m: g.git_subtree_split(m, "--rejoin"),
    lambda m: g.split_preflight(m, "--rejoin"),
])
def test_bad_input_rejected_without_git_subtree(mono_subtree_sync, monkeypatch, call):
    """Input validation must not depend on whether git subtree is installed."""
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: False)
    with pytest.raises(ValueError):
        call(mono_subtree_sync)


def test_unavailable_subtree_still_reports_other_head(mono_subtree_sync, monkeypatch):
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: False)
    out = g.subtree_sync_status(mono_subtree_sync, "sub", mono_subtree_sync)
    assert out["reason"] == "subtree_unavailable" and len(out["other_head"]) == 40


# ──── subtree_sync_run ────────────────────────────────────────────

# subtree-sync: plan-only by default, push to a new branch only, pull with verification (offline).


def _identity(root):
    _git(root, "config", "user.email", "t@e")
    _git(root, "config", "user.name", "t")


def _commit_subtree_sync_run(root, files, msg):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "--", *files)
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def world_subtree_sync_run(tmp_path):
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _identity(mono)
    _commit_subtree_sync_run(mono, {"sub/a.py": "a = 1\n", "top.py": "x = 1\n"}, "one")
    _commit_subtree_sync_run(mono, {"sub/b.py": "b = 2\n"}, "two")
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    return {"mono": mono, "child": child, "tmp": tmp_path}


def _seed_child(world):
    """Publish the current split of sub/ as the child's default branch `trunk`."""
    split = g.git_subtree_split(world["mono"], "sub")["sha"]
    _git(world["mono"], "push", "-q", str(world["child"]), f"{split}:refs/heads/trunk")
    return split


BAD = [
    dict(prefix="--rejoin", remote="/x", branch="b", direction="push"),
    dict(prefix="../up", remote="/x", branch="b", direction="push"),
    dict(prefix="sub", remote="https://user:tok@github.com/o/r", branch="b", direction="push"),
    dict(prefix="sub", remote="ssh://github.com/o/r", branch="b", direction="push"),
    dict(prefix="sub", remote="relative", branch="b", direction="push"),
    dict(prefix="sub", remote="/x", branch="--force", direction="push"),
    dict(prefix="sub", remote="/x", branch="a..b", direction="push"),
    dict(prefix="sub", remote="/x", branch="x.lock", direction="push"),
    dict(prefix="sub", remote="/x", branch="main", direction="push"),
    dict(prefix="sub", remote="/x", branch="master", direction="push"),
    dict(prefix="sub", remote="/x", branch="b", direction="sideways"),
    dict(prefix="sub", remote="/x", branch="b", direction="push", squash=True),
    dict(prefix="sub", remote=None, branch="b", direction="push"),
    dict(prefix="missing", remote="/x", branch="b", direction="push"),
]


@pytest.mark.parametrize("kwargs", BAD)
def test_bad_input_rejected_before_subtree_check(world_subtree_sync_run, monkeypatch, kwargs):
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: pytest.fail("availability checked first"))
    with pytest.raises(ValueError):
        g.subtree_sync(world_subtree_sync_run["mono"], **kwargs)


def test_missing_subtree_is_clear_error(world_subtree_sync_run, monkeypatch):
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: False)
    with pytest.raises(g.Error) as e:
        g.subtree_sync(world_subtree_sync_run["mono"], "sub", str(world_subtree_sync_run["child"]), "feature", direction="push")
    assert e.value.code == "subtree_unavailable"
    r = subprocess.run([sys.executable, "-S", str(Path(g.__file__)), "subtree-sync", "--root", str(world_subtree_sync_run["mono"]),
                        "--prefix", "sub", "--remote", str(world_subtree_sync_run["child"]), "--branch", "feature",
                        "--direction", "push"], capture_output=True, text=True, env={"PATH": "/nonexistent"})
    assert r.returncode == 2 and not r.stdout


def _local_repo_snapshot(root):
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir"))
    fetch_head = git_dir / "FETCH_HEAD"
    index = git_dir / "index"
    return {
        "refs": _refs(root),
        "head": _git(root, "rev-parse", "HEAD"),
        "status": subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                 capture_output=True, text=True).stdout,
        "fetch_head": fetch_head.read_text(encoding="utf-8") if fetch_head.exists() else None,
        "index_sha": g.git_blob_sha(index.read_bytes()) if index.exists() else None,
    }


@needs_subtree
def test_push_plan_changes_nothing(world_subtree_sync_run, monkeypatch):
    mono, child = world_subtree_sync_run["mono"], world_subtree_sync_run["child"]
    split = g.git_subtree_split(mono, "sub")["sha"]
    _git(mono, "push", "-q", str(child), f"{split}:refs/heads/trunk")
    before_mono, before_child = _local_repo_snapshot(mono), _refs(child)
    fetch_on_mono = []
    real = g._git_run

    def spy(run_root, args, **kw):
        if args[:1] == ["fetch"] and str(g._git_root(run_root)) == str(g._git_root(mono)):
            fetch_on_mono.append(list(args))
        return real(run_root, args, **kw)

    monkeypatch.setattr(g, "_git_run", spy)
    out = g.subtree_sync(mono, "sub", str(child), "sync/one", direction="push", remote_compare_ref="trunk")
    assert out["state"] == "planned" and out["write"] is False
    assert out["command"] == [
        "git", "push", "--force-with-lease=refs/heads/sync/one:", "--", str(child),
        f"{out['split_sha']}:refs/heads/sync/one",
    ]
    assert out["split"]["method"] == "subtree" and out["split"]["subtree_sha"] is None
    assert "--force-with-lease=refs/heads/<branch>:" in out["push_guard"]["branch_race_note"]
    assert out["provenance"]["trailer"] == f"Subtree-Split: {out['source']} {out['split_sha']} sub"
    assert out["split"]["fallback_used"] is False
    assert "branch_race_note" in out["push_guard"]
    assert _local_repo_snapshot(mono) == before_mono and _refs(child) == before_child
    assert not fetch_on_mono
    assert not (Path(_git(mono, "rev-parse", "--absolute-git-dir")) / "subtree-cache").exists()


@needs_subtree
def test_push_write_creates_new_branch_and_verifies(world_subtree_sync_run):
    out = g.subtree_sync(world_subtree_sync_run["mono"], "sub", str(world_subtree_sync_run["child"]), "sync/one", direction="push", write=True)
    assert out["state"] == "pushed" and out["verified"]["ok"]
    assert _git(world_subtree_sync_run["child"], "rev-parse", "refs/heads/sync/one") == out["split_sha"]
    assert not (Path(_git(world_subtree_sync_run["mono"], "rev-parse", "--absolute-git-dir")) / "subtree-cache").exists()
    with pytest.raises(ValueError, match="already exists"):
        g.subtree_sync(world_subtree_sync_run["mono"], "sub", str(world_subtree_sync_run["child"]), "sync/one", direction="push")


@needs_subtree
def test_push_refuses_remote_default_branch(world_subtree_sync_run):
    _seed_child(world_subtree_sync_run)
    with pytest.raises(ValueError, match="default branch"):
        g.subtree_sync(world_subtree_sync_run["mono"], "sub", str(world_subtree_sync_run["child"]), "trunk", direction="push", write=True)


def _added_world(tmp_path, squash):
    """Child repo with history, added into a monorepo by `git subtree add` (the normal pull setup)."""
    child = tmp_path / "child.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(child))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(child), str(work))
    _identity(work)
    _git(work, "checkout", "-q", "-b", "trunk")
    _commit_subtree_sync_run(work, {"a.py": "a = 1\n"}, "child one")
    _git(work, "push", "-q", "origin", "HEAD:trunk")
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q", "-b", "main")
    _identity(mono)
    _commit_subtree_sync_run(mono, {"top.py": "x = 1\n"}, "mono one")
    _git(mono, "subtree", "add", "-q", "--prefix=sub", *(["--squash"] if squash else []), str(child), "trunk")
    return {"mono": mono, "child": child, "work": work, "tmp": tmp_path}


@needs_subtree
@pytest.mark.parametrize("squash", [False, True])
def test_pull_plan_then_write_verifies(tmp_path, squash):
    world_subtree_sync_run = _added_world(tmp_path, squash)
    work = world_subtree_sync_run["work"]
    _commit_subtree_sync_run(work, {"c.py": "c = 3\n"}, "child change\n\nSubtree-Split: " + "0" * 40 + " " + "1" * 40 + " sub")
    _git(work, "push", "-q", "origin", "HEAD:trunk")
    tip = _git(work, "rev-parse", "HEAD")
    head_before = _git(world_subtree_sync_run["mono"], "rev-parse", "HEAD")
    plan = g.subtree_sync(world_subtree_sync_run["mono"], "sub", str(world_subtree_sync_run["child"]), "trunk", direction="pull", squash=squash)
    assert plan["state"] == "planned" and plan["remote_tip"] == tip and plan["local_branch"] == "main"
    assert _git(world_subtree_sync_run["mono"], "rev-parse", "HEAD") == head_before
    out = g.subtree_sync(world_subtree_sync_run["mono"], "sub", str(world_subtree_sync_run["child"]), "trunk", direction="pull", squash=squash,
                         write=True, message="sync sub from child")
    assert out["state"] == "pulled" and out["verified"]["ok"]
    assert (world_subtree_sync_run["mono"] / "sub" / "c.py").read_text() == "c = 3\n"
    assert not (Path(_git(world_subtree_sync_run["mono"], "rev-parse", "--absolute-git-dir")) / "subtree-cache").exists()
    existing = out["provenance"]["existing"]
    assert existing["ok"] is False and existing["trailers"][0]["split_sha"] == "1" * 40


@needs_subtree
def test_pull_refuses_dirty_tree(tmp_path):
    world_subtree_sync_run = _added_world(tmp_path, False)
    (world_subtree_sync_run["mono"] / "top.py").write_text("dirty\n")
    with pytest.raises(ValueError, match="uncommitted"):
        g.subtree_sync(world_subtree_sync_run["mono"], "sub", str(world_subtree_sync_run["child"]), "trunk", direction="pull")


@needs_subtree
def test_cli_plan_prints_command(world_subtree_sync_run):
    r = subprocess.run([sys.executable, "-S", str(Path(g.__file__)), "subtree-sync", "--root", str(world_subtree_sync_run["mono"]),
                        "--prefix", "sub", "--remote", str(world_subtree_sync_run["child"]), "--branch", "sync/x",
                        "--direction", "push"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "planned" and out["command"][:2] == ["git", "push"]
    assert _refs(world_subtree_sync_run["child"]) == ""


# ──── tree ────────────────────────────────────────────────────────

# Structure browsing is independent of clone, file contents and history.


C_tree, T, S, V, B_tree = ('a'*40, 'b'*40, 'c'*40, 'd'*40, 'e'*40)


def entry_tree(path, mode='100644', sha=B_tree):
    kind = {'040000':'tree', '160000':'commit'}.get(mode, 'blob')
    return dict(path=path, mode=mode, type=kind, sha=sha)


def response(sha, entries, truncated=False):
    return dict(sha=sha, tree=entries, truncated=truncated)


ROOT_TREE = response(T, [entry_tree('README.md'), entry_tree('src', '040000', S),
                    entry_tree('vendor', '040000', V), entry_tree('link', '120000'),
                    entry_tree('module', '160000')])


def fake_tree(method, path, **kwargs):
    assert method == 'GET'
    return {
        'repos/o/r/commits/main': {'sha': C_tree},
        'repos/o/r/git/trees/'+C_tree: ROOT_TREE,
        'repos/o/r/git/trees/'+S: response(S, [entry_tree('app.py'), entry_tree('app.pyc'), entry_tree('cache', '040000', V)]),
        'repos/o/r/git/trees/'+V: response(V, [entry_tree('cached.txt')]),
    }[path]


def test_default_is_root_metadata_only():
    with patch.object(g, 'request', side_effect=fake_tree) as get:
        out = g.tree('o/r')
    assert get.call_count == 2 and out['tree_requests'] == 1
    assert out['commit_sha'] == C_tree and out['tree_sha'] == T
    assert out['depth'] == 1 and out['complete']
    rows = {e['path']:e for e in out['entries']}
    assert rows['src']['expanded'] is False
    assert rows['link']['kind'] == 'symlink'
    assert rows['module']['kind'] == 'submodule'
    assert set(rows) == {'README.md', 'src', 'vendor', 'link', 'module'}


def test_recursive_excludes_directories_before_network_and_files_from_output():
    with patch.object(g, 'request', side_effect=fake_tree) as get:
        out = g.tree('o/r', depth=None, exclude_dirs=['vendor','cache'], exclude_files=['*.pyc','README.md'])
    assert {e['path'] for e in out['entries']} == {'src', 'src/app.py', 'link', 'module'}
    assert out['excluded_entries'] == 4 and out['complete']
    assert get.call_count == 3
    assert all(V not in call.args[1] for call in get.call_args_list)


def test_depth_and_path_patterns():
    with patch.object(g, 'request', side_effect=fake_tree):
        out = g.tree('o/r', depth=2, exclude_dirs=['src/cache'], exclude_files=['src/*.pyc'])
    assert {e['path'] for e in out['entries']} == {'README.md','src','src/app.py','vendor','vendor/cached.txt','link','module'}
    assert out['scope'] == 'selected_depth_and_exclusions'


def test_ref_resolves_once_then_uses_immutable_identity():
    with patch.object(g,'request',side_effect=[{'sha':C_tree}, ROOT_TREE]) as get:
        out=g.tree('o/r','feature/a')
    assert get.call_args_list[0].args[1].endswith('commits/feature%2Fa')
    assert get.call_args_list[1].args[1].endswith(C_tree)
    assert out['ref']=='feature/a'


@pytest.mark.parametrize('transport', ['gh','urllib'])
def test_shared_transport(transport):
    with patch.object(g, '_gh' if transport=='gh' else '_url', side_effect=[{'sha':C_tree},ROOT_TREE]):
        assert g.tree('o/r',transport=transport)['count']==5


def test_upstream_truncation_never_claims_complete():
    with patch.object(g,'request',side_effect=[{'sha':C_tree},response(T,[],True)]):
        out=g.tree('o/r')
    assert out['truncated'] and not out['complete']


@pytest.mark.parametrize('payload', [
    {}, response(T,[entry_tree('../x')]), response(T,[entry_tree('x'),entry_tree('x')]),
    response(T,[dict(entry_tree('x'),mode=[])]), response(T,[dict(entry_tree('x'),type='tree')]),
    response(T,[dict(entry_tree('x'),sha='short')]), response(T,[dict(entry_tree('x'),size=True)]),
    dict(response(T,[]),truncated=None),
])
def test_malformed_tree_fails_closed(payload):
    with patch.object(g,'request',side_effect=[{'sha':C_tree},payload]),pytest.raises(g.Error):
        g.tree('o/r')


def test_wrong_child_and_cycle_rejected():
    for root,child in [(ROOT_TREE,response(V,[])),
                       (response(T,[entry_tree('cycle','040000',T)]),response(T,[]))]:
        with patch.object(g,'request',side_effect=[{'sha':C_tree},root,child]),pytest.raises(g.Error):
            g.tree('o/r',depth=None,exclude_dirs=['vendor'])


@pytest.mark.parametrize('options', [{'depth':0},{'depth':True},{'exclude_dirs':'vendor'},
    {'exclude_dirs':['../x']},{'exclude_files':['/root']},{'exclude_dirs':['vendor/']}])
def test_invalid_arguments_before_network(options):
    with patch.object(g,'request') as get,pytest.raises(ValueError):g.tree('o/r',**options)
    get.assert_not_called()


def test_item_budget_counts_excluded_and_request_budget_bounds_walk():
    with patch.object(g,'request',side_effect=fake_tree):
        with g.operation(max_items=1),pytest.raises(g.Error,match='items_limit'):
            g.tree('o/r',exclude_dirs=['*'],exclude_files=['*'])
        with g.operation(max_pages=1),pytest.raises(g.Error,match='pages_limit'):
            g.tree('o/r',depth=None)


def test_cli_options(capsys):
    with patch.object(g,'request',side_effect=fake_tree):
        assert g.main(['tree','o/r','--recursive','--exclude-dir','vendor','--exclude-dir','cache','--exclude-file','*.pyc'])==0
    out=json.loads(capsys.readouterr().out)
    assert out['depth'] is None and 'src/app.pyc' not in {e['path'] for e in out['entries']}
    assert g.main(['tree','o/r','--depth','0'])==2
    assert 'invalid_argument' in capsys.readouterr().err


# ──── scripts_repo_overview ───────────────────────────────────────

CLI = ROOT / "gh_identity.py"


def make_tree(root: Path) -> None:
    files = {
        "README.md": "# demo\nline 2\nline 3\n",
        "package.json": '{"private": true}\n',
        "requirements-test.txt": "pytest\n",
        ".github/workflows/ci.yml": "name: ci\n",
        "src/app.py": "print('hi')\n",
        "node_modules/pkg/index.js": "junk\n",
        "test-results/run/evidence.json": "{}\n",
        "src/__pycache__/app.cpython-312.pyc": "junk\n",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "Dockerfile.bin").write_bytes(b"\x00\x01binary")


def git_env(**overrides: str) -> dict:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
              GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
              GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
    env.update(overrides)
    return env


def commit_file(root: Path, relative: str, content, date: str) -> None:
    """Write ``relative`` (str or bytes) and commit it alone, at a fixed author/committer date."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content) if isinstance(content, bytes) else path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--", relative], check=True, env=git_env())
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", f"commit {relative}"], check=True,
                   env=git_env(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date))


def test_key_files_then_file_list_without_dependency_or_result_dirs(tmp_path: Path) -> None:
    make_tree(tmp_path)
    out = g.repo_overview(tmp_path)
    headers = [line for line in out.splitlines() if line.startswith("=== ")]
    assert headers == [
        "=== about ===",
        "=== README.md (3 lines) ===",
        "=== package.json (1 line) ===",
        "=== requirements-test.txt (1 line) ===",
        "=== Dockerfile.bin (binary, 8 bytes; not shown) ===",
        "=== .github/workflows/ci.yml (1 line) ===",
        "=== files (6) ===",
    ]
    listed = out.split("=== files (6) ===\n", 1)[1].splitlines()
    assert listed == [".github/workflows/ci.yml", "Dockerfile.bin", "README.md", "package.json",
                      "requirements-test.txt", "src/app.py"]


def test_caps_lines_and_files(tmp_path: Path) -> None:
    make_tree(tmp_path)
    out = g.repo_overview(tmp_path, max_lines=1, max_files=2, include=("src/*.py",))
    assert "=== README.md (3 lines) ===\n# demo\n[... truncated: showing 1 of 3 lines (--max-lines)]" in out
    assert "=== src/app.py (1 line) ===" in out
    assert out.endswith("=== files (6) ===\n.github/workflows/ci.yml\nDockerfile.bin\n[... 4 more files (--max-files)]")


def test_git_work_tree_uses_ls_files_and_respects_gitignore(tmp_path: Path) -> None:
    make_tree(tmp_path)
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("noise\n", encoding="utf-8")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=env)
    paths = g.repo_overview_files(tmp_path)
    assert "debug.log" not in paths and ".gitignore" in paths and "src/app.py" in paths
    assert not any(path.startswith(("node_modules/", "test-results/")) for path in paths)


def test_cli_on_this_repository_runs_without_site_packages() -> None:
    done = subprocess.run([sys.executable, "-S", str(CLI), "repo-overview", str(ROOT), "--max-lines", "3", "--max-files", "5"],
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("=== about ===\n")
    assert "=== README.md (" in done.stdout
    assert "=== .github/workflows/ci.yml (" in done.stdout and "=== files (" in done.stdout
    missing = subprocess.run([sys.executable, "-S", str(CLI), "repo-overview", str(ROOT / "nope")], capture_output=True, text=True)
    assert missing.returncode == 2


def test_shared_combined_inventory_matches_previous_git_contract(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    tracked = ["z tracked.txt", "日本語/記録.txt", "tab\tname.txt", "line\nname.txt", "build/tracked.txt"]
    untracked = ["a untracked.txt", "日本語/未追跡.txt", "tab\tuntracked.txt", "line\nuntracked.txt", "node_modules/untracked.txt"]
    for name in tracked + untracked + ["ignored.log", "cache/ignored.txt"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\ncache/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", *tracked], check=True, env=git_env())
    old = subprocess.run(["git", "-C", str(tmp_path), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         check=True, capture_output=True, env=git_env()).stdout.decode("utf-8").split("\0")
    expected = sorted(set(tracked[:-1] + untracked[:-1] + [".gitignore"]))
    assert sorted({p for p in old if p and not g._ov_skipped(p)}) == expected
    assert g.repo_overview_files(tmp_path) == expected


def test_non_git_walk_fallback_is_sorted_and_skips_dirs(tmp_path: Path) -> None:
    make_tree(tmp_path)
    (tmp_path / "日本語.txt").write_text("fixture", encoding="utf-8")
    # Without Git, .gitignore is an ordinary file; walk never implements Git's
    # ignore rules. Preserve this existing fallback contract.
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("fixture", encoding="utf-8")
    assert g.repo_overview_files(tmp_path) == sorted([
        ".github/workflows/ci.yml", ".gitignore", "Dockerfile.bin", "README.md",
        "debug.log", "package.json", "requirements-test.txt", "src/app.py", "日本語.txt",
    ])


def test_missing_git_keeps_walk_fallback(tmp_path: Path, monkeypatch) -> None:
    make_tree(tmp_path)
    # Exercise the public observation with Git genuinely unavailable, without
    # depending on the vendored inspector's private process implementation.
    monkeypatch.setenv("PATH", "")
    with pytest.raises(g.GitInspectionError, match="git executable not found"):
        g.git_ls_files(tmp_path, include_untracked=True)
    assert "src/app.py" in g.repo_overview_files(tmp_path)
    assert not any(g._ov_skipped(p) for p in g.repo_overview_files(tmp_path))


def test_shared_inventory_sort_and_truncation_are_not_hidden(tmp_path: Path, monkeypatch) -> None:
    for name in ("a.txt", "z.txt"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    def inventory(root, *, include_untracked):
        assert root == tmp_path and include_untracked is True
        return {"paths": ["z.txt", "a.txt", "z.txt"], "truncated": False}
    monkeypatch.setattr(g, "git_ls_files", inventory)
    assert g.repo_overview_files(tmp_path) == ["a.txt", "z.txt"]
    monkeypatch.setattr(g, "git_ls_files",
                        lambda *args, **kwargs: {"paths": ["z.txt"], "truncated": True})
    with pytest.raises(RuntimeError, match="inventory exceeds"):
        g.repo_overview_files(tmp_path)


# --- about --------------------------------------------------------------

def test_about_combines_package_json_pyproject_and_readme(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "demo", "description": "A demo package.  "}\n',
                                           encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\ndescription = "A demo project"\n',
                                             encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# demo\n\n"
        "[![CI](https://img.shields.io/badge/CI-passing-green)](https://example.com/ci)\n\n"
        "This is the real description paragraph spanning\n"
        "two lines of markdown source.\n\n"
        "## More\n\nignored body text\n",
        encoding="utf-8",
    )
    assert g.repo_overview_about(tmp_path) == (
        "=== about ===\n"
        "package.json: A demo package.\n"
        "pyproject.toml: A demo project\n"
        "README: This is the real description paragraph spanning two lines of markdown source."
    )


def test_about_omitted_when_nothing_found(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    assert g.repo_overview_about(tmp_path) is None
    assert "=== about ===" not in g.repo_overview(tmp_path)


def test_about_ignores_package_json_without_description(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"private": true}\n', encoding="utf-8")
    assert g.repo_overview_about(tmp_path) is None


def test_about_readme_paragraph_truncated_to_300_chars(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(f"# t\n\n{'x' * 400}\n", encoding="utf-8")
    assert g.repo_overview_about(tmp_path) == "=== about ===\nREADME: " + "x" * 300


# --- stats ----------------------------------------------------------------

def test_stats_totals_and_per_extension_rows_sorted_by_lines(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("hello\nworld\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("license text\n", encoding="utf-8")
    (tmp_path / "img.bin").write_bytes(b"\x00\x01\x02\x03")
    assert g.repo_overview_stats(tmp_path).splitlines() == [
        "=== stats ===",
        "total  5 files  7 lines  45 B",
        ".py  2 files  4 lines  16 B",
        ".md  1 file  2 lines  12 B",
        "(none)  1 file  1 line  13 B",
        ".bin  1 file  0 lines  4 B",
    ]


def test_stats_truncates_to_top_15_extensions(tmp_path: Path) -> None:
    for i in range(17):
        (tmp_path / f"file{i}.ext{i:02d}").write_text("x\n", encoding="utf-8")
    lines = g.repo_overview_stats(tmp_path).splitlines()
    assert lines[0] == "=== stats ==="
    extension_rows = lines[2:-1]
    assert len(extension_rows) == 15
    assert extension_rows[0].startswith(".ext00 ") and extension_rows[-1].startswith(".ext14 ")
    assert lines[-1] == "[... 2 more extensions (top 15 shown)]"


# --- churn (git work trees only) ------------------------------------------

def test_churn_counts_commits_added_deleted_and_last_date(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    commit_file(tmp_path, "scripts/a.py", "print(1)\n", "2026-01-01T00:00:00+0000")
    commit_file(tmp_path, "scripts/a.py", "print(1)\nprint(2)\n", "2026-02-01T00:00:00+0000")
    commit_file(tmp_path, "scripts/b.py", "print('b')\n", "2026-03-01T00:00:00+0000")
    assert g.repo_overview_churn(tmp_path, 5).splitlines() == [
        "=== churn (3 commits scanned) ===",
        "  2 commits  +2/-0  2026-02-01  scripts/a.py",
        "  1 commit  +1/-0  2026-03-01  scripts/b.py",
    ]


def test_churn_skips_binary_numstat(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    commit_file(tmp_path, "scripts/a.py", "print(1)\n", "2026-01-01T00:00:00+0000")
    commit_file(tmp_path, "assets/logo.png", b"\x89PNG\x00\x01", "2026-01-02T00:00:00+0000")
    out = g.repo_overview_churn(tmp_path, 5)
    assert out.splitlines()[0] == "=== churn (2 commits scanned) ==="
    assert "logo.png" not in out
    assert "scripts/a.py" in out


def test_churn_truncates_to_top_n_and_notes_remainder(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    for i in range(3):
        commit_file(tmp_path, f"f{i}.py", f"x{i}\n", f"2026-01-0{i + 1}T00:00:00+0000")
    lines = g.repo_overview_churn(tmp_path, 2).splitlines()
    assert lines[0] == "=== churn (3 commits scanned) ==="
    assert len(lines) == 4
    assert lines[-1] == "[... 1 more files (--churn 2 shows the top 2)]"


def test_churn_since_filters_older_commits(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    commit_file(tmp_path, "old.py", "old\n", "2026-01-01T00:00:00+0000")
    commit_file(tmp_path, "new.py", "new\n", "2026-03-01T00:00:00+0000")
    out = g.repo_overview_churn(tmp_path, 5, since="2026-02-01")
    assert out.splitlines()[0] == "=== churn (1 commit scanned) ==="
    assert "new.py" in out and "old.py" not in out


def test_churn_outside_git_prints_one_line_note(tmp_path: Path) -> None:
    assert g.repo_overview_churn(tmp_path, 5) == "not a git work tree; skipping --churn"
    assert "=== churn" not in g.repo_overview(tmp_path, churn_top=5)


def test_cli_stats_and_churn_flags(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    commit_file(tmp_path, "a.py", "x\n", "2026-01-01T00:00:00+0000")
    done = subprocess.run([sys.executable, "-S", str(CLI), "repo-overview", str(tmp_path), "--no-files", "--stats",
                          "--churn", "5"], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    assert "=== stats ===" in done.stdout
    assert "=== churn (1 commit scanned) ===" in done.stdout
    assert "a.py" in done.stdout


def make_churn_migration_fixture(root: Path) -> None:
    subprocess.run(['git', 'init', '-q', str(root)], check=True, env=git_env())
    # A normal path is touched twice: count wins over path order.
    commit_file(root, 'z.txt', 'one\n', '2026-01-01T00:00:00+0000')
    commit_file(root, 'z.txt', 'one\ntwo\n', '2026-01-02T00:00:00+0000')
    for name in ['a.txt', '日本語.txt', 'tab\tname.txt', 'line\nname.txt', 'old.txt']:
        commit_file(root, name, 'fixture\n', '2026-02-01T00:00:00+0000')
    subprocess.run(['git', '-C', str(root), 'mv', '--', 'old.txt', 'new.txt'], check=True, env=git_env())
    subprocess.run(['git', '-C', str(root), 'commit', '-q', '-m', 'rename'], check=True,
                   env=git_env(GIT_AUTHOR_DATE='2026-03-01T00:00:00+0000',
                               GIT_COMMITTER_DATE='2026-03-01T00:00:00+0000'))
    commit_file(root, 'binary.bin', b'\x00binary', '2026-03-02T00:00:00+0000')
    subprocess.run(['git', '-C', str(root), 'commit', '--allow-empty', '-q', '-m', 'empty'], check=True,
                   env=git_env(GIT_AUTHOR_DATE='2026-03-03T00:00:00+0000',
                               GIT_COMMITTER_DATE='2026-03-03T00:00:00+0000'))


def test_churn_before_after_migration_fixture(tmp_path: Path) -> None:
    make_churn_migration_fixture(tmp_path)
    snapshots = FIXTURES['churn_migration']
    for case in snapshots['cases']:
        after = g.repo_overview_churn(tmp_path, 20, since=case['since'])
        assert after == case['after']
        # Complete commit count, binary exclusion and normal rows are unchanged.
        assert after.splitlines()[0] == case['before'].splitlines()[0]
        assert 'binary.bin' not in after and 'binary.bin' not in case['before']
        for row in case['before'].splitlines()[1:]:
            if row.endswith(('  a.txt', '  z.txt', '  old.txt')):
                assert row in after.splitlines()
    observation = g.git_log_numstat(tmp_path)
    renamed = [f for c in observation['commits'] for f in c['files'] if f['orig_path']]
    assert renamed == [{'path': 'new.txt', 'orig_path': 'old.txt', 'added': 0, 'deleted': 0}]
    binary = [f for c in observation['commits'] for f in c['files'] if f['path'] == 'binary.bin']
    assert binary == [{'path': 'binary.bin', 'orig_path': None, 'added': None, 'deleted': None}]
    assert '  1 commit  +0/-0  2026-03-01  new.txt' in snapshots['cases'][0]['after']
    assert 'old.txt => new.txt' in snapshots['cases'][0]['before']


@pytest.mark.parametrize('limit', ['count', 'byte'])
def test_churn_complete_commits_and_explicit_history_truncation(tmp_path: Path, monkeypatch, limit: str) -> None:
    make_churn_migration_fixture(tmp_path)
    identity = g
    original = identity.git_log_numstat
    if limit == 'count':
        kwargs = {'max_count': 2}
    else:
        # This boundary includes the newest empty commit and the following
        # binary commit, then cuts the rename destination. Only complete commits
        # may contribute to the scanned count or ranking.
        raw = subprocess.run(
            ['git', '-C', str(tmp_path), 'log', '--no-ext-diff', '--no-textconv', '--no-color',
             '-z', '--numstat', '--format=%x00%H%x00%cs', '--'],
            check=True, capture_output=True, env=git_env(),
        ).stdout
        kwargs = {'max_bytes': raw.index(b'new.txt') + 2}
    bounded = original(tmp_path, **kwargs)
    assert bounded['truncated'] and len(bounded['commits']) == 2
    def limited(root, *, since):
        return original(root, since=since, **kwargs)
    monkeypatch.setattr(identity, 'git_log_numstat', limited)
    out = g.repo_overview_churn(tmp_path, 20)
    assert out.splitlines() == [
        '=== churn (2 commits scanned) ===',
        '[... history truncated by shared inspector limits (10,000 commits / 1,000,000 bytes); '
        'ranking covers complete scanned commits only]',
    ]


def test_churn_missing_git_keeps_message(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('PATH', '')
    with pytest.raises(g.GitInspectionError, match='git executable not found'):
        g.git_log_numstat(tmp_path)
    assert g.repo_overview_churn(tmp_path, 5) == 'not a git work tree; skipping --churn'


def test_churn_delegates_since_and_keeps_top_note_with_history_note(tmp_path: Path, monkeypatch) -> None:
    def observe(root, *, since):
        assert root == tmp_path and since == '3 months ago'
        return {'commits': [{'date': '2026-01-01', 'files': [
            {'path': path, 'added': 1, 'deleted': 0} for path in ['z.txt', 'a.txt']]}], 'truncated': True}
    monkeypatch.setattr(g, 'git_log_numstat', observe)
    out = g.repo_overview_churn(tmp_path, 1, since='3 months ago')
    assert out.splitlines()[1] == '  1 commit  +1/-0  2026-01-01  a.txt'
    assert out.splitlines()[2] == '[... 1 more files (--churn 1 shows the top 1)]'
    assert 'history truncated' in out.splitlines()[3]


def test_about_tolerates_bad_metadata_and_skips_unreadable_readmes(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "README.d").mkdir()
    (tmp_path / "README.bin").write_bytes(b"\0binary")
    (tmp_path / "README.md").write_text("[![b](x)](y)\n\n# Title\n\nReal text.\n", encoding="utf-8")
    assert g.repo_overview_about(tmp_path) == "=== about ===\nREADME: Real text."
    (tmp_path / "README.md").write_text("![only](badge)\n", encoding="utf-8")
    assert g.repo_overview_about(tmp_path) is None
    real_read_text = Path.read_text

    def broken(self, *args, **kwargs):
        if self.name == "pyproject.toml":
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", broken)
    assert g._ov_pyproject_description(tmp_path) is None
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(PermissionError("denied")) if self.name == "README.md"
                        else real_read_bytes(self))
    assert g._ov_readme_paragraph(tmp_path) is None


def test_symlinks_leaving_root_are_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "README.md").symlink_to(outside / "secret.txt")
    (root / "Makefile").symlink_to(outside / "secret.txt")
    (root / "inside.txt").write_text("ok\n", encoding="utf-8")
    (root / "link-inside.txt").symlink_to(root / "inside.txt")
    text = g.repo_overview(root, show_stats=True)
    assert "SECRET" not in text and "=== Makefile" not in text
    assert g.repo_overview_files(root) == ["inside.txt", "link-inside.txt"]
    assert g.repo_overview_about(root) is None


def test_include_glob_must_stay_inside_root(tmp_path: Path, capsys) -> None:
    for bad in ("../*.md", "/etc/*"):
        with pytest.raises(ValueError):
            g._ov_key_files(tmp_path, (bad,))
        assert g.main(["repo-overview", str(tmp_path), "--include", bad]) == 2
    assert "must stay inside ROOT" in capsys.readouterr().err


def test_since_requires_churn(tmp_path: Path, capsys) -> None:
    assert g.main(["repo-overview", str(tmp_path), "--since", "1 week ago"]) == 2
    assert "--since needs --churn" in capsys.readouterr().err


def test_main_in_process_paths(tmp_path: Path, capsys) -> None:
    assert g.main(["repo-overview", str(tmp_path / "missing")]) == 2
    assert "not a directory" in capsys.readouterr().err
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    (tmp_path / "big.txt").write_bytes(b"x" * 2048)
    assert g.main(["repo-overview", str(tmp_path), "--no-files", "--stats", "--include", "Makefile"]) == 0
    out = capsys.readouterr().out
    assert "=== files" not in out and "=== Makefile (1 line) ===" in out and out.count("=== Makefile") == 1
    assert "2 KB" in out
    for bad in ("0", "x"):
        assert g.main(["repo-overview", str(tmp_path), "--max-lines", bad]) == 2
    with pytest.raises(ValueError):
        g.repo_overview(tmp_path, max_lines=0)


def test_human_bytes_units() -> None:
    assert g._ov_human_bytes(5) == "5 B"
    assert g._ov_human_bytes(3 * 1024 ** 2) == "3 MB"
    assert g._ov_human_bytes(2 * 1024 ** 3) == "2 GB"


def test_stats_skips_unreadable_files(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("1\n2\n", encoding="utf-8")
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(PermissionError("denied")) if self.name == "b.txt"
                        else real_read_bytes(self))
    assert g.repo_overview_stats(tmp_path).splitlines()[1] == "total  1 file  1 line  2 B"


def test_positive_accepts_valid_values(tmp_path: Path, capsys) -> None:
    assert g.main(["repo-overview", str(tmp_path), "--max-lines", "3", "--no-files"]) == 0
