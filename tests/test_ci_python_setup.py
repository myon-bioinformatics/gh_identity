"""Keep declared Python support, runtime CI, and contributor CI aligned."""
import json, re, tomllib, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def job_text(path, name):
    workflow = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(r"(?ms)^  " + re.escape(name) + r":\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", workflow)
    if match is None:
        raise AssertionError("Missing workflow job: " + name)
    return match.group(1)


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


if __name__ == "__main__":
    unittest.main()
