"""Shared helpers for the test_<purpose>.py files: `from conftest import ...`.

test_core.py stays stdlib-only (CI also runs it under `python -S`), so it does not import this.
Request doubles live in gh_identity itself: `request_stub({path: value}, calls)`.
"""
import json, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
# Recorded and synthetic observations (browser DOM captures, REST responses, job logs, ...).
# Each key's provenance is under "_about"; nothing here is a credential or a live fetch.
FIXTURES = json.loads((Path(__file__).parent / "fixtures.json").read_text(encoding="utf-8"))


def _git(root, *args, env=None):
    """git -C root ARGS (check=True); stripped stdout."""
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", env=env).stdout.strip()


def _git_tester(root, *args):
    """_git with a throwaway identity (t <t@e>), for commits in scratch repositories."""
    return _git(root, "-c", "user.email=t@e", "-c", "user.name=t", *args)
