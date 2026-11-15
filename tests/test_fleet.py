"""Fleet contracts through the parent's shared checks (vendor/fleet_check.py): locks, pins, CI setup, benchmark, Node entry."""
import json, re, tomllib, unittest

import pytest

from vendor import fleet_check as fleet

ROOT = fleet.repo_root(__file__)
PARENT_WORKFLOW_PIN = "9e3da7ae1846e9604ebc24bb5178f7de094a4e77"
# Node sources and the shared checks stay on accepted upstream commits with exact bytes.
ACCEPTED = {
    "vendor/py_launch.cjs": "384f5dd532076062f7833038764e9a62322c387c",
    "vendor/fleet_check.py": "45ed741f7e1f60179934cee6fa1e16a887f97cb0",
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
            (".github/workflows/runtime-compatibility.yml", "runtime", "python"),
        ):
            with self.subTest(workflow=path):
                body = job_text(path, job)
                match = re.search(r"(?m)^        " + re.escape(selector) + r": (\[[^\n]+\])$", body)
                self.assertIsNotNone(match, "Missing Python version matrix")
                self.assertEqual(json.loads(match.group(1)), expected)
        runtime = job_text(".github/workflows/runtime-compatibility.yml", "runtime")
        self.assertIn("python -S -m unittest discover -s tests -p test_gh_identity.py -v", runtime)

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

    def test_runtime_latest_is_managed_before_activation(self):
        self.assert_latest_setup(".github/workflows/runtime-compatibility.yml", "runtime", "matrix.python")



@pytest.mark.parametrize("optimized", [False, True], ids=["normal", "optimized"])
@pytest.mark.parametrize("mismatch", [False, True], ids=["same", "mismatch"])
def test_benchmark_parity_in_both_interpreter_modes(optimized, mismatch):
    patch = "g['indexed'] = lambda *args: ['different']" if mismatch else ""
    result = fleet.run_main(ROOT, "scripts/benchmark_ignore_filter.py", patch, optimized=optimized)
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
