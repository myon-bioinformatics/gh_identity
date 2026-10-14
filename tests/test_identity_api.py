"""Generic GitHub identity APIs: repo / owner / PR / commit-pulls / first-parent / request_stub."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import gh_identity as g

M1, M2, M3, X2 = "1" * 40, "2" * 40, "3" * 40, "5" * 40


def commit(sha, *parents):
    return {"sha": sha, "parents": [{"sha": p} for p in parents]}


HISTORY = [commit(M3, M2), commit(M2, M1, X2), commit(X2, M1), commit(M1)]


def test_request_stub_overrides_transport():
    with g.request_stub(lambda *a, **k: {"ok": True, "path": a[1]}):
        assert g.request("GET", "repos/x/y") == {"ok": True, "path": "repos/x/y"}
    with patch.object(g, "_gh", side_effect=AssertionError("live")), \
         patch.object(g, "_url", side_effect=AssertionError("live")), \
         pytest.raises(AssertionError):
        g.request("GET", "repos/x/y", transport="urllib")


def test_repo_identity_public_and_not_public():
    def fake(method, path, **kw):
        if path == "repos/me/pub":
            return {"id": 1, "visibility": "public", "archived": False, "fork": False, "default_branch": "main",
                    "owner": {"type": "User"}, "private": False}
        if path == "repos/me/pub/commits/main":
            return {"sha": M3}
        if path == "repos/me/hid":
            return {"id": 2, "visibility": "private", "private": True, "archived": False, "default_branch": "main"}
        raise g.Error("not_found_or_inaccessible")
    with g.request_stub(fake):
        out = g.repo_identity("me/pub")
        assert out["repository_id"] == 1 and out["head"] == M3 and out["write"] is False
        with pytest.raises(g.Error) as e:
            g.repo_identity("me/hid")
        assert e.value.code == "not_public"


def test_list_owner_repositories_user_and_org():
    user = [{"full_name": "me/a", "id": 1, "visibility": "public", "archived": False, "fork": False, "default_branch": "main"},
            {"full_name": "me/secret", "visibility": "private", "private": True},
            {"full_name": "me/old", "visibility": "public", "archived": True},
            {"full_name": "me/fork", "visibility": "public", "fork": True, "archived": False}]
    with patch.object(g, "pages_bounded", return_value=(user, True, 1)) as pages:
        out = g.list_owner_repositories("me", kind="user")
    assert pages.call_args.args[0].startswith("users/me/repos")
    assert [r["full_name"] for r in out["repositories"]] == ["me/a"] and out["count"] == 1
    with patch.object(g, "pages_bounded", return_value=(user, True, 1)) as pages:
        out = g.list_owner_repositories("me", kind="org", include_forks=True, include_archived=True)
    assert pages.call_args.args[0].startswith("orgs/me/repos") and out["count"] == 3


def test_list_owner_auto_falls_back_to_org():
    def pages(path, *a, **k):
        if path.startswith("users/"):
            raise g.Error("not_found_or_inaccessible")
        return [{"full_name": "org/x", "id": 9, "visibility": "public", "archived": False, "fork": False}], True, 1
    with patch.object(g, "pages_bounded", side_effect=pages):
        out = g.list_owner_repositories("org", kind="auto")
    assert out["kind"] == "orgs" and out["repositories"][0]["full_name"] == "org/x"


def test_pr_identity_merged_and_open():
    def fake(method, path, **kw):
        if path == "repos/me/r":
            return {"id": 7, "default_branch": "main"}
        if path == "repos/me/r/pulls/3":
            return {"merged_at": "t", "merge_commit_sha": M3, "state": "closed",
                    "head": {"sha": X2, "ref": "pr"}, "base": {"sha": M1, "ref": "main"}}
        if path == "repos/me/r/pulls/4":
            return {"state": "open", "head": {"sha": X2, "ref": "pr"}, "base": {"sha": M2, "ref": "main"}}
        raise g.Error("not_found_or_inaccessible")
    with g.request_stub(fake):
        m = g.pr_identity("me/r", 3)
        assert m["state"] == "merged" and m["merge_commit_sha"] == M3 and m["merged_into_default"] is True
        o = g.pr_identity("me/r", 4)
        assert o["state"] == "open" and o["merge_commit_sha"] is None and o["head"]["sha"] == X2


def test_commit_pulls():
    with g.request_stub(lambda m, p, **k: [{"number": 9, "merged_at": "t", "merge_commit_sha": M3,
                                             "base": {"ref": "main"}, "head": {"sha": X2}}]):
        out = g.commit_pulls("me/r", X2)
    assert out["count"] == 1 and out["pulls"][0]["number"] == 9 and out["pulls"][0]["state"] == "merged"
    with pytest.raises(ValueError):
        g.commit_pulls("me/r", "short")


def test_first_parent_api_mode():
    def fake(method, path, **kw):
        if path == "repos/me/up":
            return {"default_branch": "main"}
        if path.startswith("repos/me/up/commits?sha=main"):
            return HISTORY if path.endswith("page=1") else []
        raise g.Error("not_found_or_inaccessible")
    with g.request_stub(fake):
        assert g.first_parent_check("me/up", M3)["contains"] is True
        assert g.first_parent_check("me/up", X2)["contains"] is False


def test_first_parent_exact_local_remote(tmp_path):
    up = tmp_path / "remotes" / "me" / "up"
    up.mkdir(parents=True)
    def git(*a):
        return subprocess.run(["git", "-C", str(up), "-c", "user.email=t@e", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (up / "a").write_text("a\n"); git("add", "-A"); git("commit", "-qm", "one")
    main1 = git("rev-parse", "HEAD")
    git("switch", "-q", "-c", "pr"); (up / "b").write_text("b\n"); git("add", "-A"); git("commit", "-qm", "pr")
    pr = git("rev-parse", "HEAD")
    git("switch", "-q", "main"); git("merge", "-q", "--no-ff", "-m", "merge", "pr")
    merge = git("rev-parse", "HEAD")
    base = str(tmp_path / "remotes") + "/"
    assert g.first_parent_check("me/up", merge, exact=True, remote_base=base)["contains"] is True
    assert g.first_parent_check("me/up", pr, exact=True, remote_base=base)["contains"] is False
    assert g.first_parent_check("me/up", main1, exact=True, remote_base=base)["contains"] is True


def test_cli_repo_identity():
    with g.request_stub(lambda m, p, **k: {"id": 1, "visibility": "public", "archived": False, "fork": False,
                                            "default_branch": "main", "owner": {"type": "User"}, "private": False}
                         if p == "repos/me/pub" else {"sha": M3}):
        import subprocess, sys
        p = subprocess.run([sys.executable, "-c",
                            "import gh_identity as g\n"
                            "with g.request_stub(lambda m,p,**k: "
                            "{'id':1,'visibility':'public','archived':False,'fork':False,"
                            "'default_branch':'main','owner':{'type':'User'},'private':False} "
                            "if p=='repos/me/pub' else {'sha':'" + M3 + "'}):\n"
                            " print(g.repo_identity('me/pub')['head'])"],
                           capture_output=True, text=True)
        assert p.returncode == 0 and p.stdout.strip() == M3


def test_list_owner_repositories_max_pages_truncates():
    calls = []
    def page(path, transport, timeout, requester):
        calls.append(path)
        return ([{"full_name": f"me/r{len(calls)}", "id": len(calls), "visibility": "public",
                  "archived": False, "fork": False}] * 1, {"link": '<https://api.github.com/x?page=2>; rel="next"'})
    with patch.object(g, "_page", side_effect=page), \
         patch.object(g, "_next_page", side_effect=lambda p, h, n: p + "&more"):
        out = g.list_owner_repositories("me", kind="user", max_pages=1)
    assert len(calls) == 1
    assert out["complete"] is False and out["pages_fetched"] == 1 and out["truncated_reason"] == "max_pages"
    with patch.object(g, "_page", return_value=([], {})), patch.object(g, "_next_page", return_value=None):
        out = g.list_owner_repositories("me", kind="user", max_pages=1)
    assert out["complete"] is True and out["truncated_reason"] is None
    with pytest.raises(ValueError):
        g.list_owner_repositories("me", kind="user", max_pages=0)


def test_repo_identity_public_archived_allowed():
    def fake(method, path, **kw):
        if path == "repos/me/old":
            return {"id": 3, "visibility": "public", "private": False, "archived": True, "fork": False,
                    "default_branch": "main", "owner": {"type": "User"}}
        return {"sha": M3}
    with g.request_stub(fake):
        out = g.repo_identity("me/old")
    assert out["archived"] is True and out["repository_id"] == 3 and out["head"] == M3
