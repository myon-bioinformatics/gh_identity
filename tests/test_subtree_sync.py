"""subtree-sync-status / sync-provenance / split-preflight: local, read-only."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import gh_identity as g

ENV = ["-c", "user.email=t@e", "-c", "user.name=t"]
needs_subtree = pytest.mark.skipif(
    subprocess.run(["git", "subtree", "-h"], capture_output=True).returncode == 1,
    reason="git subtree not installed")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(root, files, msg):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, *ENV, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _refs(root):
    return _git(root, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.fixture
def mono(tmp_path):
    root = tmp_path / "mono"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, {"sub/a.py": "import os\n", "top.py": "import sub_mod\n"}, "one")
    _commit(root, {"sub/b.py": "x = 1\n"}, "two")
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
def test_in_sync_and_read_only(mono, tmp_path):
    other = _clone_split(mono, tmp_path)
    before = (_refs(mono), _refs(other))
    out = g.subtree_sync_status(mono, "sub", other)
    assert out["state"] == "in_sync" and out["split_in_other"] and out["write"] is False
    assert (_refs(mono), _refs(other)) == before


@needs_subtree
def test_monorepo_ahead(mono, tmp_path):
    other = _clone_split(mono, tmp_path)
    _commit(mono, {"sub/c.py": "y = 2\n"}, "three")
    _commit(mono, {"top.py": "z = 3\n"}, "outside only")
    out = g.subtree_sync_status(mono, "sub", other)
    assert out["state"] == "monorepo_ahead" and out["monorepo_ahead"] == 1 and out["other_ahead"] == 0


@needs_subtree
def test_other_ahead_and_other_only_paths(mono, tmp_path):
    other = _clone_split(mono, tmp_path)
    _commit(other, {"pyproject.toml": "[project]\n"}, "repo-only file")
    assert g.subtree_sync_status(mono, "sub", other)["state"] == "other_ahead"
    out = g.subtree_sync_status(mono, "sub", other, other_only_paths=["pyproject.toml"])
    assert out["state"] == "in_sync" and out["other_only_ignored"] == 1
    _commit(other, {"a.py": "import sys\n"}, "real change")
    out = g.subtree_sync_status(mono, "sub", other, other_only_paths=["pyproject.toml"])
    assert out["state"] == "other_ahead" and out["other_ahead"] == 1


@needs_subtree
def test_diverged_and_unrelated(mono, tmp_path):
    other = _clone_split(mono, tmp_path)
    _commit(other, {"a.py": "1\n"}, "other side")
    _commit(mono, {"sub/a.py": "2\n"}, "mono side")
    assert g.subtree_sync_status(mono, "sub", other)["state"] == "diverged"
    alien = tmp_path / "alien"
    alien.mkdir()
    _git(alien, "init", "-q", "-b", "main")
    _commit(alien, {"x": "x\n"}, "alien")
    assert g.subtree_sync_status(mono, "sub", alien)["state"] == "unrelated"


def test_sync_status_missing_subtree(mono, tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_git_subtree_available", lambda root: False)
    out = g.subtree_sync_status(mono, "sub", mono)
    assert out["state"] is None and out["reason"] == "subtree_unavailable"


def test_sync_status_rejects_bad_input(mono):
    with pytest.raises(ValueError):
        g.subtree_sync_status(mono, "--rejoin", mono)
    with pytest.raises(ValueError):
        g.subtree_sync_status(mono, "sub", mono, other_ref="--all")


@needs_subtree
def test_provenance_record_and_verify(mono, tmp_path):
    other = _clone_split(mono, tmp_path)
    rec = g.sync_provenance(mono, "sub", other, direction="push", generated_at="2026-01-01T00:00:00Z")
    assert rec["schema"] == "gh-identity-sync-provenance/1" and rec["state"] == "in_sync"
    assert rec["trailer"].startswith("Subtree-Split: " + rec["monorepo"]["sha"])
    out = tmp_path / "out"
    g.write_sync_provenance(rec, out)
    first = (out / "sync-provenance.json").read_bytes()
    g.write_sync_provenance(g.sync_provenance(mono, "sub", other, direction="push", generated_at="2026-01-01T00:00:00Z"), out)
    assert (out / "sync-provenance.json").read_bytes() == first
    _git(other, *ENV, "commit", "-q", "--allow-empty", "-m", "sync\n\n" + rec["trailer"])
    ok = g.verify_sync_provenance(other, "HEAD", monorepo_root=mono)
    assert ok["ok"] and ok["trailers"][0]["recomputed"] == rec["split_sha"]
    bad = rec["trailer"].replace(rec["split_sha"], "0" * 40)
    _git(other, *ENV, "commit", "-q", "--allow-empty", "-m", "sync\n\n" + bad)
    assert g.verify_sync_provenance(other, "HEAD", monorepo_root=mono)["reason"] == "mismatch"
    _git(other, *ENV, "commit", "-q", "--allow-empty", "-m", "plain")
    assert g.verify_sync_provenance(other, "HEAD")["reason"] == "no_trailer"


@pytest.fixture
def cross(tmp_path):
    """a/ and b/ import each other; pyproject and sys.path refer to a/."""
    root = tmp_path / "cross"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, {
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
def test_cli(mono, tmp_path):
    other = _clone_split(mono, tmp_path)
    run = lambda *a: subprocess.run([sys.executable, str(Path(g.__file__)), *a], capture_output=True, text=True)
    p = run("subtree-sync-status", "--root", str(mono), "--prefix", "sub", "--other-root", str(other))
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["state"] == "in_sync"
    p = run("split-preflight", "--root", str(mono), "--prefix", "sub")
    assert p.returncode == 0, p.stderr
    p = run("sync-provenance", "--root", str(mono), "--prefix", "sub", "--other-root", str(other), "--output-dir", str(tmp_path / "o"))
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "o" / "sync-provenance.json").exists()
