"""Facts about repositories of any account: licence, stars, health, releases, quota."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import gh_identity as ghi


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _repo(**over):
    d = {"id": 7, "full_name": "up/lib", "default_branch": "main", "visibility": "public", "archived": False,
         "html_url": "https://github.com/up/lib", "description": "a lib", "homepage": "",
         "license": {"spdx_id": "MIT", "name": "MIT License"}, "stargazers_count": 120, "forks_count": 9,
         "subscribers_count": 4, "open_issues_count": 3, "topics": ["cli", "python"], "language": "Python",
         "size": 88, "created_at": _ago(900), "updated_at": _ago(2), "pushed_at": _ago(3), "fork": False,
         "is_template": False, "disabled": False, "owner": {"type": "Organization"}}
    d.update(over)
    return d


SHA = "a" * 40


def _api(routes, calls=None):
    """request stub: exact path -> value; an Exception value is raised; unknown paths are 404."""
    def handler(method, path, **kwargs):
        assert method == "GET"
        if calls is not None:
            calls.append(path)
        if path not in routes:
            raise ghi.Error("not_found_or_inaccessible")
        value = routes[path]
        if isinstance(value, Exception):
            raise value
        return value
    return handler


def test_repo_reports_licence_and_stars_from_one_request():
    calls = []
    with ghi.request_stub(_api({"repos/up/lib": _repo()}, calls)):
        out = ghi.repository("up/lib")
    assert calls == ["repos/up/lib"]
    assert (out["license"], out["license_name"], out["stars"], out["forks"]) == ("MIT", "MIT License", 120, 9)
    assert (out["watchers"], out["open_issues"], out["topics"], out["language"]) == (4, 3, ["cli", "python"], "Python")
    assert out["homepage"] is None and out["owner_type"] == "Organization" and out["parent"] is None
    # The keys older callers read are unchanged.
    assert (out["schema"], out["id"], out["default_branch"], out["visibility"], out["archived"]) == (
        "gh-identity-repository/1", 7, "main", "public", False)


def test_repo_without_licence_or_counts_is_none_not_an_error():
    with ghi.request_stub(_api({"repos/up/lib": {"id": 1, "license": None, "stargazers_count": "many"}})):
        out = ghi.repository("up/lib")
    assert out["license"] is None and out["stars"] is None and out["topics"] == []


def test_repo_include_all_sections():
    routes = {
        "repos/up/lib": _repo(),
        "repos/up/lib/releases?per_page=5": [
            {"tag_name": "v2.0.0-rc1", "prerelease": True, "published_at": _ago(1)},
            {"tag_name": "v1.4.0", "published_at": _ago(40), "assets": [{"name": "lib.whl", "size": 10, "download_count": 5}]}],
        "repos/up/lib/tags?per_page=5": [{"name": "v1.4.0", "commit": {"sha": SHA}}],
        "repos/up/lib/releases?per_page=10": [{"tag_name": "v1.4.0", "published_at": _ago(40)}],
        "repos/up/lib/tags?per_page=10": [{"name": "v1.4.0", "commit": {"sha": SHA}}],
        "repos/up/lib/commits/main": {"sha": SHA, "commit": {"committer": {"date": _ago(3)}}},
        f"repos/up/lib/commits/{SHA}/check-runs?per_page=100&page=1": {
            "total_count": 1, "check_runs": [{"id": 1, "name": "ci", "status": "completed", "conclusion": "success"}]},
        f"repos/up/lib/commits/{SHA}/status": {"total_count": 0, "state": "pending"},
        "repos/up/lib/languages": {"Python": 900, "Shell": 100},
        "repos/up/lib/contributors?anon=1&per_page=100&page=1": [
            {"login": "alice", "contributions": 50}, {"type": "Anonymous", "email": "x@example.invalid", "contributions": 2}],
        "repos/up/lib/community/profile": {"health_percentage": 71, "files": {"readme": {"url": "u"}, "license": {"key": "mit"}, "contributing": None}},
    }
    with ghi.request_stub(_api(routes)):
        out = ghi.repository("up/lib", include=("all",))
    health = out["health"]
    assert (health["status"], health["days_since_push"], health["latest_release"], health["days_since_release"]) == ("active", 3, "v1.4.0", 40)
    assert (health["head_sha"], health["ci_state"], health["open_issues"]) == (SHA, "green", 3)
    assert out["fork_diff"] == {"fork": False}
    assert out["languages"] == {"total_bytes": 1000, "languages": [
        {"name": "Python", "bytes": 900, "percent": 90.0}, {"name": "Shell", "bytes": 100, "percent": 10.0}]}
    assert out["contributors"] == {"count": 2, "complete": True, "accounts": 1, "top": [{"login": "alice", "contributions": 50}]}
    assert "example.invalid" not in json.dumps(out)
    assert out["community"] == {"health_percentage": 71, "files": {"contributing": False, "license": True, "readme": True}}
    assert out["releases"]["latest"]["tag"] == "v1.4.0" and out["releases"]["tags"] == [{"name": "v1.4.0", "sha": SHA}]
    text = ghi.render_repository(out)
    assert text.splitlines()[0].startswith("📦 up/lib  ⭐ 120  🍴 9  📄 MIT  🗣 Python")
    assert "💓 active · push 3d · release v1.4.0 40d · CI green · issues 3" in text
    assert "🧩 Python 90.0% · Shell 10.0%" in text and "👥 2  alice(50)" in text
    assert "contributing ✗ license ✓ readme ✓" in text


@pytest.mark.parametrize("over,stale_days,expected", [
    ({"archived": True}, 365, "archived"),
    ({"disabled": True}, 365, "disabled"),
    ({"pushed_at": _ago(400)}, 365, "stale"),
    ({"pushed_at": _ago(400)}, 500, "active"),
    ({"pushed_at": None}, 365, "unknown"),
])
def test_health_status(over, stale_days, expected):
    # No release, no readable head: the status still comes from the repository object.
    with ghi.request_stub(_api({"repos/up/lib": _repo(**over), "repos/up/lib/releases?per_page=5": [], "repos/up/lib/tags?per_page=5": []})):
        health = ghi.repository("up/lib", include="health", stale_days=stale_days)["health"]
    assert health["status"] == expected
    assert health["latest_release"] is None and health["ci_error"] == "not_found_or_inaccessible"


def test_health_without_checks_is_none_not_pending():
    routes = {"repos/up/lib": _repo(), "repos/up/lib/releases?per_page=5": [], "repos/up/lib/tags?per_page=5": [],
              "repos/up/lib/commits/main": {"sha": SHA},
              f"repos/up/lib/commits/{SHA}/check-runs?per_page=100&page=1": {"total_count": 0, "check_runs": []}}
    routes[f"repos/up/lib/commits/{SHA}/status"] = {"total_count": 0, "state": "pending"}
    with ghi.request_stub(_api(routes)):
        assert ghi.repository("up/lib", include="health")["health"]["ci_state"] == "none"


def test_unreadable_section_is_reported_not_raised():
    routes = {"repos/up/lib": _repo(), "repos/up/lib/languages": ghi.Error("permission_or_rate_limit")}
    with ghi.request_stub(_api(routes)):
        out = ghi.repository("up/lib", include=("languages", "community"))
    assert out["languages"] == {"error": "permission_or_rate_limit"}
    assert out["community"] == {"error": "not_found_or_inaccessible"}
    assert "⚠️ languages: permission_or_rate_limit" in ghi.render_repository(out)


def test_fork_diff_compares_with_parent_default_branch():
    fork = _repo(full_name="me/lib", fork=True, default_branch="feat/x",
                 parent={"full_name": "up/lib", "default_branch": "main"})
    calls = []
    routes = {"repos/me/lib": fork,
              "repos/up/lib/compare/main...me:feat%2Fx?per_page=1": {"ahead_by": 2, "behind_by": 5, "status": "diverged"}}
    with ghi.request_stub(_api(routes, calls)):
        out = ghi.repository("me/lib", include=("fork-diff",))
    assert out["parent"] == "up/lib" and out["fork"] is True
    assert out["fork_diff"] == {"fork": True, "parent": "up/lib", "parent_branch": "main", "branch": "feat/x",
                                "ahead_by": 2, "behind_by": 5, "status": "diverged"}
    assert "🔱 fork of up/lib" in ghi.render_repository(out) and "🔱 up/lib +2 -5" in ghi.render_repository(out)


@pytest.mark.parametrize("kw", [{"include": ("stars",)}, {"stale_days": 0}, {"stale_days": True}])
def test_repo_rejects_bad_arguments(kw):
    with ghi.request_stub(_api({"repos/up/lib": _repo()})), pytest.raises(ValueError):
        ghi.repository("up/lib", **kw)


def test_releases_latest_skips_drafts_and_prereleases():
    routes = {"repos/up/lib/releases?per_page=2": [
                  {"tag_name": "v3-draft", "draft": True}, {"tag_name": "v2-rc", "prerelease": True}],
              "repos/up/lib/tags?per_page=2": [{"name": "v2-rc", "commit": {"sha": SHA}}, {"name": "v1"}]}
    with ghi.request_stub(_api(routes)):
        out = ghi.releases("up/lib", max_items=2)
    assert out["latest"] is None and [x["tag"] for x in out["releases"]] == ["v3-draft", "v2-rc"]
    assert out["tags"] == [{"name": "v2-rc", "sha": SHA}, {"name": "v1", "sha": None}]
    assert out["complete"] is False  # both lists filled the limit: more may exist
    assert ghi.render_releases(out).splitlines()[:3] == ["🏷️ up/lib  latest none", "📝 v3-draft  ?  📎 0", "🧪 v2-rc  ?  📎 0"]
    with pytest.raises(ValueError):
        ghi.releases("up/lib", max_items=0)


def test_rate_limit_tells_quota_from_permission():
    reset = int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp())
    body = {"resources": {"core": {"limit": 60, "remaining": 0, "used": 60, "reset": reset},
                          "search": {"limit": 10, "remaining": 10, "used": 0, "reset": reset}}}
    with ghi.request_stub(_api({"rate_limit": body})):
        out = ghi.rate_limit()
        caps = ghi.capabilities(rate_limit=True)
    assert out["authenticated"] is False and out["exhausted"] == ["core"]
    assert 0 < out["resources"]["core"]["resets_in_seconds"] <= 1800
    assert caps["rate_limit"]["resources"]["search"]["remaining"] == 10
    body["resources"]["core"].update(limit=5000, remaining=4990)
    with ghi.request_stub(_api({"rate_limit": body})):
        assert ghi.rate_limit()["authenticated"] is True


def test_capabilities_stays_offline_by_default_and_survives_a_blocked_quota_read():
    def refuse(*a, **k):
        raise AssertionError("no request expected")
    with ghi.request_stub(refuse):
        assert "rate_limit" not in ghi.capabilities()
    with ghi.request_stub(_api({"rate_limit": ghi.Error("permission_or_rate_limit")})):
        assert ghi.capabilities(rate_limit=True)["rate_limit"] == {"error": "permission_or_rate_limit"}


def _rows():
    return [
        {"name": "a", "full_name": "acme/a", "private": False, "archived": False, "size": 1, "stargazers_count": 5,
         "forks_count": 1, "license": {"spdx_id": "MIT"}, "language": "Python", "pushed_at": "2026-03-01T00:00:00Z"},
        {"name": "b", "full_name": "acme/b", "private": False, "archived": True, "size": 2, "stargazers_count": 50,
         "forks_count": 0, "license": None, "language": None, "pushed_at": "2026-01-01T00:00:00Z", "fork": True},
        {"name": "c", "full_name": "acme/c", "private": False, "archived": False, "size": 3, "stargazers_count": 20,
         "forks_count": 4, "license": {"spdx_id": "Apache-2.0"}, "language": "Go", "pushed_at": "2026-02-01T00:00:00+09:00"},
    ]


def test_inventory_new_fields_and_archived_opt_in():
    with ghi.request_stub(_api({"users/acme/repos?per_page=100&page=1": _rows()})):
        out = ghi.repository_inventory("acme", fields="name,stars,license", sort="stars", order="desc")
        everything = ghi.repository_inventory("acme", sort="stars", order="desc", include_archived=True)
        by_push = ghi.repository_inventory("acme", sort="pushed-at", include_archived=True)
        by_licence = ghi.repository_inventory("acme", sort="license", include_archived=True)
    assert [(r["name"], r["stars"], r["license"]) for r in out["repositories"]] == [("c", 20, "Apache-2.0"), ("a", 5, "MIT")]
    assert out["kind"] == "users" and out["include_archived"] is False
    assert [(r["name"], r["archived"], r["fork"]) for r in everything["repositories"]] == [("b", True, True), ("c", False, False), ("a", False, False)]
    assert [r["name"] for r in by_push["repositories"]] == ["b", "c", "a"]
    assert [r["name"] for r in by_licence["repositories"]] == ["c", "a", "b"]  # no licence sorts last


def test_inventory_and_repos_fall_back_to_organisation():
    calls = []
    routes = {"orgs/acme/repos?per_page=100&page=1": _rows(), "orgs/acme/repos?sort=full_name&per_page=100&page=1": _rows()}
    with ghi.request_stub(_api(routes, calls)):
        inventory = ghi.repository_inventory("acme")
        listed = ghi.repositories("acme")
    assert inventory["kind"] == "orgs" and [r["name"] for r in inventory["repositories"]] == ["a", "c"]
    assert listed["kind"] == "orgs" and listed["count"] == 3
    assert listed["repositories"][0]["license"] == "MIT" and listed["repositories"][1]["stars"] == 50
    assert calls[0].startswith("users/acme/") and calls[1].startswith("orgs/acme/")
    with ghi.request_stub(_api(routes)), pytest.raises(ghi.Error) as user_only:
        ghi.repository_inventory("acme", kind="user")
    assert user_only.value.code == "not_found_or_inaccessible"
    with pytest.raises(ValueError):
        ghi.repositories("acme", kind="team")


def test_owner_repositories_rows_carry_brief_facts():
    with ghi.request_stub(_api({"users/acme/repos?type=owner&sort=full_name&per_page=100&page=1": _rows()})):
        out = ghi.list_owner_repositories("acme", include_archived=True, include_forks=True)
    row = {r["full_name"]: r for r in out["repositories"]}["acme/b"]
    assert (row["archived"], row["fork"], row["stars"], row["license"]) == (True, True, 50, None)


def test_cli_repo_text_and_releases(capsys):
    routes = {"repos/up/lib": _repo(archived=True), "repos/up/lib/languages": {"Python": 1},
              "repos/up/lib/releases?per_page=3": [], "repos/up/lib/tags?per_page=3": []}
    with ghi.request_stub(_api(routes)):
        assert ghi.main(["repo", "up/lib", "--include", "languages", "--text"]) == 0
        text = capsys.readouterr().out
        assert ghi.main(["releases", "up/lib", "--limit", "3"]) == 0
        listed = json.loads(capsys.readouterr().out)
    assert "📄 MIT" in text and "🗃 archived" in text and "🧩 Python 100.0%" in text
    assert listed["schema"] == "gh-identity-releases/1" and listed["complete"] is True
    # choices are declared in argparse: an unknown section is a usage error, before any request.
    assert ghi.main(["repo", "--include", "stars", "up/lib"]) == 2
    with ghi.request_stub(_api(routes)):
        assert ghi.main(["repo", "--include", "languages", "--include", "community", "up/lib"]) == 0
    assert json.loads(capsys.readouterr().out)["community"] == {"error": "not_found_or_inaccessible"}


@pytest.mark.parametrize("answer,expected", [
    ({"license": {"spdx_id": "BSD-3-Clause"}}, "BSD-3-Clause"),
    ({"license": {"spdx_id": "NOASSERTION"}}, "NOASSERTION"),
    ({"license": None}, None),
    (ghi.Error("permission_or_rate_limit"), None),
])
def test_license_spdx_is_best_effort(answer, expected):
    with ghi.request_stub(_api({f"repos/up/lib/license?ref={SHA}": answer})):
        assert ghi._license_spdx("up/lib", SHA) == expected


@pytest.mark.parametrize("check_state,legacy_state,count,expected", [
    (None, "failure", 1, "failed"), ("success", "failure", 1, "failed"),
    (None, "success", 1, "green"), ("success", "pending", 1, "pending"),
    ("failure", "success", 1, "failed"), (None, "pending", 0, "none"),
    ("success", "success", 101, "green"), ("success", "error", 1, "failed"),
])
def test_health_includes_combined_legacy_status(check_state, legacy_state, count, expected):
    checks = [] if check_state is None else [{"id": 1, "name": "ci", "status": "completed", "conclusion": check_state}]
    routes = {"repos/up/lib": _repo(), "repos/up/lib/releases?per_page=5": [],
              "repos/up/lib/tags?per_page=5": [], "repos/up/lib/commits/main": {"sha": SHA},
              f"repos/up/lib/commits/{SHA}/check-runs?per_page=100&page=1": {"total_count": len(checks), "check_runs": checks},
              f"repos/up/lib/commits/{SHA}/status": {"total_count": count, "state": legacy_state}}
    with ghi.request_stub(_api(routes)):
        assert ghi.repository("up/lib", include="health")["health"]["ci_state"] == expected


@pytest.mark.parametrize("response", [ghi.Error("permission_or_rate_limit"), {"total_count": True, "state": "success"}, {"total_count": 1, "state": "mystery"}])
def test_unreadable_legacy_status_does_not_claim_green(response):
    routes = {"repos/up/lib": _repo(), "repos/up/lib/releases?per_page=5": [],
              "repos/up/lib/tags?per_page=5": [], "repos/up/lib/commits/main": {"sha": SHA},
              f"repos/up/lib/commits/{SHA}/check-runs?per_page=100&page=1": {"total_count": 1, "check_runs": [{"id": 1, "status": "completed", "conclusion": "success"}]},
              f"repos/up/lib/commits/{SHA}/status": response}
    with ghi.request_stub(_api(routes)):
        health = ghi.repository("up/lib", include="health")["health"]
    assert health["ci_state"] is None
    assert "ci_error" in health


def test_capabilities_rate_limit_preserves_transport_and_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(ghi, "rate_limit", lambda **kw: calls.append(kw) or {})
    ghi.capabilities(rate_limit=True, transport="urllib", timeout=7)
    assert calls == [{"transport": "urllib", "timeout": 7}]


def test_capabilities_cli_forwards_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(ghi, "rate_limit", lambda **kw: calls.append(kw) or {})
    assert ghi.main(["--transport", "urllib", "capabilities", "--rate-limit"]) == 0
    assert calls == [{"transport": "urllib", "timeout": 30}]
