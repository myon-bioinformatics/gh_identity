"""pin-audit: commit pins checked with plain git against local remotes (no network, no token)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import gh_identity as g


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
