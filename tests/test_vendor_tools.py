"""vendor-lock-check / vendor-lock-verify-remote / vendor-plan / vendor-matrix: read-only."""
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import gh_identity as g

C1, C2 = "1" * 40, "2" * 40
MOD = b"import json\n\ndef f():\n    return 1\n"
LIC = b"MIT License\n"


def entry(dest, data, repo="o/up", source="mod.py", commit=C1):
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
    write_lock(tmp_path, [entry("vendor/mod.py", MOD), entry("vendor/up-LICENSE", LIC, source="LICENSE")])
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


def plan(consumer, candidates, rules=None):
    with patch.object(g, "_remote_file", side_effect=fake_remote(UPSTREAM)):
        return {d["candidate"]: d for d in g.vendor_plan(consumer, candidates, rules)["decisions"]}


def test_plan_rules(consumer):
    cands = [f"o/up:mod.py@{C1}", f"o/up:heavy.py@{C1}", f"o/up:pkg.py@{C1}", f"o/up:unused.py@{C1}",
             "o/up:mod.py@main", f"o/up:missing.py@{C1}", f"o/nolic:mod2.py@{C1}"]
    out = plan(consumer, cands)
    assert out[cands[0]]["action"] == "place" and out[cands[0]]["destination"] == "vendor/mod.py"
    assert out[cands[0]]["license_destination"] == "vendor/up-LICENSE"
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
    a = {"schema": "vendor-lock/1", "files": [entry("vendor/mod.py", MOD, commit=C1)]}
    b = {"schema": "vendor-lock/1", "files": [entry("tool/python/vendor/mod.py", MOD, commit=C2)]}
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
