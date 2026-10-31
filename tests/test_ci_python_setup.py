"""Protect the moving stable lane without changing fixed-minor interpreter policy."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def job_text(path, name):
    workflow = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(r"(?ms)^  " + re.escape(name) + r":\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", workflow)
    if match is None:
        raise AssertionError("Missing workflow job: " + name)
    return match.group(1)


class TestCIPythonSetup(unittest.TestCase):
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
