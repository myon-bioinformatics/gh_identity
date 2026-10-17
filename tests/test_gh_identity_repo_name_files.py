"""repo-name-files: files named after each public repository, read-only, fake transport."""
import json
from unittest.mock import patch

import pytest
import gh_identity as g

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


def run(raw=None, **kwargs):
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
    out = run()
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
    out = run(fetch_raw=False, repos=["lib"])
    hit = rows(out)["lib"]["found"][1]
    assert hit["raw_status"] == "not_fetched" and hit["imports"] is None and hit["needs_bundle"] is None
    assert hit["raw_commit_url"] == "https://raw.githubusercontent.com/me/lib/" + B + "/src/lib/lib.py"
    assert rows(out)["lib"]["found"][0]["needs_bundle"] is False
    assert "needs_bundle" not in rows(out)["lib"]["flags"]
    assert out["count"] == 1 and out["complete"] is True and out["raw_errors"] == []
    out = run(raw=make_raw(fail=("src/lib/lib.py",)), repos=["lib"])
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

    out = run(raw=raw, repos=["lib"])
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

    out = run(raw=raw, repos=["lib"])
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
    out = run(raw=make_raw(parent=parent), repos=["lib", "odd"])
    assert out["parent"]["errors"] == {name: "invalid_json"} and out["complete"] is False
    r = rows(out)
    if name == "vendor-catalog.json":
        assert r["lib"]["found"][0]["catalogued"] is None
        assert "unregistered_source" not in r["lib"]["flags"]
    if name == "child-heads.json":
        assert "fleet_missing" not in r["odd"]["flags"]


def test_parent_fetch_failure_and_validation():
    out = run(raw=make_raw(fail=("child-heads.json",)), repos=["odd"])
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
