"""duplicate-files: same basename across one owner's repositories, read-only, fake transport."""
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import gh_identity as g

UP_OLD, UP_NEW, OTHER = b"v1\n", b"v2\n", b"other\n"
CU, CC, CD = "a" * 40, "c" * 40, "d" * 40
OLD = "1" * 40


def sha(b):
    return g.git_blob_sha(b)


def lock(files):
    data = json.dumps({"schema": "vendor-lock/1", "files": files}).encode()
    return {"encoding": "base64", "content": base64.b64encode(data).decode()}


def entry(dest, data, source="tool.py", repository="me/up", commit=OLD):
    return {"repository": repository, "ref": commit, "commit": commit, "source": source, "destination": dest,
            "blob_sha": sha(data), "sha256": hashlib.sha256(data).hexdigest()}


def blob(path, data):
    return {"path": path, "mode": "100644", "type": "blob", "sha": sha(data)}


REPOS = [
    {"name": "up", "default_branch": "main", "owner": {"login": "me"}},
    {"name": "consumer", "default_branch": "main", "owner": {"login": "me"}},
    {"name": "drifted", "default_branch": "dev", "owner": {"login": "me"}},
    {"name": "secret", "private": True, "visibility": "private", "owner": {"login": "me"}},
    {"name": "internal", "visibility": "internal", "owner": {"login": "me"}},
    {"name": "old", "archived": True, "owner": {"login": "me"}},
    {"name": "forked", "fork": True, "default_branch": "main", "owner": {"login": "me"}},
]


def test_include_forks_is_configurable():
    with patch.object(g, "pages", return_value=REPOS):
        assert [r["name"] for r in g.owner_repositories("me")] == ["up", "consumer", "drifted"]
        assert [r["name"] for r in g.owner_repositories("me", include_forks=True)][-1] == "forked"
TREES = {
    CU: [blob("tool.py", UP_NEW), blob("README.md", b"r"), blob("LICENSE", b"l")],
    CC: [blob("vendor/tool.py", UP_OLD), blob("README.md", b"r"), blob("vendor.lock.json", b"x"), blob("only.py", b"o")],
    CD: [blob("scripts/tool.py", OTHER), blob("README.md", b"r2")],
}


def fake(method, path, **kw):
    assert method == "GET"
    table = {
        "repos/me/up/commits/main": {"sha": CU},
        "repos/me/consumer/commits/main": {"sha": CC},
        "repos/me/drifted/commits/dev": {"sha": CD},
        f"repos/me/consumer/contents/vendor.lock.json?ref={CC}": lock([entry("vendor/tool.py", UP_OLD)]),
        "repos/me/up": {"default_branch": "main", "visibility": "public"},
        "repos/me/drifted": {"default_branch": "dev", "visibility": "public"},
    }
    for c, rows in TREES.items():
        table[f"repos/{ {CU: 'me/up', CC: 'me/consumer', CD: 'me/drifted'}[c] }/git/trees/{c}?recursive=1"] = {"sha": "f" * 40, "tree": rows, "truncated": False}
    if path in table:
        return table[path]
    raise g.Error("not_found_or_inaccessible")


def run(**kw):
    with patch.object(g, "pages", return_value=REPOS) as pages, patch.object(g, "request", side_effect=fake):
        out = g.duplicate_files("me", **kw)
    return out, pages


def test_owner_filter_and_groups():
    out, pages = run()
    assert pages.call_args.args[0] == "users/me/repos?type=owner&sort=full_name"
    assert set(out["scanned"]) == {"me/up", "me/consumer", "me/drifted"} and out["write"] is False
    assert [x["basename"] for x in out["groups"]] == ["tool.py"]           # README.md / LICENSE ignored, only.py single
    grp = out["groups"][0]
    assert grp["repositories"] == 3 and grp["distinct_contents"] == 3 and not grp["identical"]
    assert set(grp["warnings"]) == {"content_differs", "unknown_provenance", "stale_pin"}


def test_provenance_origin_stale():
    files = {f["repository"]: f for f in run()[0]["groups"][0]["files"]}
    up, consumer, drifted = files["me/up"], files["me/consumer"], files["me/drifted"]
    assert consumer["provenance"] == {"repository": "me/up", "source": "tool.py", "commit": OLD, "lock_blob_sha": sha(UP_OLD)}
    assert consumer["stale"] is True and consumer["matches_lock"] is True
    assert up["origin"] is True and up["consumers"] == ["me/consumer"] and up["provenance"] is None
    assert drifted["provenance"] is None and drifted["origin"] is False and drifted["stale"] is None
    c = run()[0]["counts"]
    assert c["null_provenance"] == 2 and c["null_provenance_non_origin"] == 1 and c["stale"] == 1


def test_ignore_options_and_repos_override():
    out, pages = run(ignore=(), extra_ignore=("tool.py",))
    assert [x["basename"] for x in out["groups"]] == ["README.md"]
    out, pages = run(repos=["up", "drifted"])
    assert not pages.called and set(out["scanned"]) == {"me/up", "me/drifted"}
    assert out["groups"][0]["files"][0]["provenance"] is None


def test_identical_copies_and_errors():
    TREES[CD] = [blob("scripts/tool.py", UP_NEW)]
    try:
        out, _ = run(repos=["up", "drifted", "missing"])
    finally:
        TREES[CD] = [blob("scripts/tool.py", OTHER), blob("README.md", b"r2")]
    assert out["groups"][0]["identical"] is True and "content_differs" not in out["groups"][0]["warnings"]
    assert out["errors"] == [] and out["redacted"] == 1 and "missing" not in json.dumps(out)


def test_rejects_bad_owner():
    with pytest.raises(ValueError):
        g.owner_repositories("../x")


def test_markdown_and_check_exit(tmp_path):
    data = b"x = 1\n"
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "m.py").write_bytes(data)
    (tmp_path / "vendor.lock.json").write_text(json.dumps({"schema": "vendor-lock/1", "files": [entry("vendor/m.py", data, source="m.py")]}))
    cli = [sys.executable, str(Path(g.__file__)), "vendor-lock-check", "--root", str(tmp_path)]
    p = subprocess.run(cli + ["--markdown", "--ci-check"], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.startswith("### gh-identity-vendor-check/1: ok") and "| vendor/m.py | ok |" in p.stdout
    (tmp_path / "vendor" / "m.py").write_bytes(b"changed\n")
    assert subprocess.run(cli + ["--ci-check"], capture_output=True).returncode == 1
    assert subprocess.run(cli, capture_output=True).returncode == 0


def test_duplicates_markdown():
    out, _ = run()
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
    with patch.object(g, 'pages', return_value=REPOS), patch.object(g, 'request', side_effect=transport):
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
    with patch.object(g, 'pages', return_value=REPOS), patch.object(g, 'request', side_effect=transport):
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
    trees = TREES[CC] + [blob(placement, b"lock bytes")]
    def transport(method, path, **kw):
        if path == f"repos/me/consumer/contents/{placement}?ref={CC}":
            return lock([entry("vendor/tool.py", UP_OLD)])
        return fake(method, path, **kw)
    with patch.dict(TREES, {CC: trees}), patch.object(g, "pages", return_value=REPOS), \
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
        "vendor.lock.json": [entry("vendor/tool.py", UP_OLD)],
        "tool/python/vendor.lock.json": [entry("vendor/extra.py", UP_OLD, source="extra.py")],
        "python/vendor.lock.json": [entry("vendor/tool.py", UP_OLD)],
    }
    def transport(method, path, **kw):
        for placement, entries in locks.items():
            if path == f"repos/me/consumer/contents/{placement}?ref={CC}":
                return lock(entries)
        return fake(method, path, **kw)
    with patch.dict(TREES, {CU: trees_up, CC: trees_consumer}), \
         patch.object(g, "pages", return_value=REPOS), \
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
            return lock([entry("vendor/tool.py", UP_OLD)])
        if path == f"repos/me/consumer/contents/python/vendor.lock.json?ref={CC}":
            if failure == "fetch_failure":
                raise g.Error("http_403")
            if failure == "invalid_response":
                return {"encoding": "none"}
            if failure == "malformed_lock":
                return {"encoding": "base64", "content": "not-base64"}
            if failure == "conflicting_case":
                return lock([entry("VENDOR/tool.py", UP_NEW)])
            return lock([entry("vendor/tool.py", UP_NEW)])
        return fake(method, path, **kw)
    with patch.dict(TREES, {CC: trees}), patch.object(g, "pages", return_value=REPOS), \
         patch.object(g, "request", side_effect=transport):
        result = g.duplicate_files("me")
    assert result["complete"] is False
    assert result["errors"] == [{"repository": "me/consumer", "detail": expected}]
    assert "me/consumer" not in result["scanned"]
    assert all(f["repository"] != "me/consumer" for group in result["groups"] for f in group["files"])
    assert "INCOMPLETE" in g.render_duplicates_markdown(result)


def test_no_lock_is_unknown_provenance_not_collection_failure():
    with patch.object(g, "pages", return_value=REPOS), patch.object(g, "request", side_effect=fake):
        result = g.duplicate_files("me", lock_paths=("python/vendor.lock.json",))
    assert result["complete"] is True and result["errors"] == []
    assert result["scanned"]["me/consumer"]["lock_paths"] == []
    assert result["counts"]["null_provenance_non_origin"] > 0


def test_different_copy_sha_is_reported_separately_from_lock_provenance():
    trees = TREES[CC] + [blob("python/vendor.lock.json", b"lock")]
    def transport(method, path, **kw):
        if path == f"repos/me/consumer/contents/python/vendor.lock.json?ref={CC}":
            return lock([entry("vendor/tool.py", UP_NEW)])
        return fake(method, path, **kw)
    with patch.dict(TREES, {CC: trees}), patch.object(g, "pages", return_value=REPOS), \
         patch.object(g, "request", side_effect=transport):
        result = g.duplicate_files("me")
    group = next(grp for grp in result["groups"] if grp["basename"] == "tool.py")
    row = next(f for f in group["files"] if f["repository"] == "me/consumer")
    assert row["provenance"] is not None
    assert row["provenance"]["lock_blob_sha"] == sha(UP_NEW)
    assert row["matches_lock"] is False and row["stale"] is False
    assert "modified_after_lock" in group["warnings"]
    assert "unknown_provenance" in group["warnings"]  # unrelated drifted copy
