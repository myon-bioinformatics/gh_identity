import pytest
import gh_identity as ghi


def _workflow(*args, **kwargs):
    return {"id": 7, "path": ".github/workflows/ci.yml"}


def _page(path, **kwargs):
    assert "/workflows/7/runs?" in path
    if "page=2" in path:
        return {"workflow_runs": []}, {}
    return {"workflow_runs": [
        {"id": 101, "run_attempt": 2, "workflow_id": 7, "head_sha": "a" * 40,
         "head_branch": "main", "event": "push", "status": "completed"},
        {"id": 102, "run_attempt": 1, "workflow_id": 7, "head_sha": "b" * 40,
         "head_branch": "feature", "event": "pull_request", "status": "completed"}
    ]}, {}


def test_workflow_discovery_filters(monkeypatch):
    monkeypatch.setattr(ghi, "workflow", _workflow)
    monkeypatch.setattr(ghi, "_page", _page)
    out = ghi.workflow_run_discovery("owner/repo", 7, branch="main", head_sha="a" * 40,
                                      event="push", page_size=5)
    assert out["complete"] and not out["truncated"]
    assert [(x["run_id"], x["attempt"]) for x in out["runs"]] == [(101, 2)]


def test_workflow_discovery_limit(monkeypatch):
    monkeypatch.setattr(ghi, "workflow", _workflow)
    monkeypatch.setattr(ghi, "_page", _page)
    out = ghi.workflow_run_discovery("owner/repo", 7, max_items=1, page_size=2)
    assert out["truncated"] and out["limit_reason"] == "max_items"
    assert out["next_page"] == 1
    assert out["next_offset"] == 1


@pytest.mark.parametrize("kwargs", [
    {"page_size": 101}, {"page_size": 0}, {"start_page": 0},
    {"max_pages": 0}, {"max_items": 0}, {"head_sha": "not-a-sha"},
])
def test_workflow_discovery_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        ghi.workflow_run_discovery("owner/repo", 7, **kwargs)


def test_workflow_discovery_rejects_wrong_workflow(monkeypatch):
    monkeypatch.setattr(ghi, "workflow", _workflow)
    monkeypatch.setattr(ghi, "_page", lambda *a, **k: ({"workflow_runs": [
        {"id": 101, "run_attempt": 1, "workflow_id": 8}]}, {}))
    with pytest.raises(ghi.Error):
        ghi.workflow_run_discovery("owner/repo", 7)

def test_resume_within_page_without_gap(monkeypatch):
    monkeypatch.setattr(ghi, "workflow", _workflow)
    monkeypatch.setattr(ghi, "_page", _page)
    first = ghi.workflow_run_discovery("owner/repo", 7, max_items=1, page_size=2)
    second = ghi.workflow_run_discovery("owner/repo", 7, max_items=2, page_size=2,
                                         start_page=first["next_page"],
                                         start_offset=first["next_offset"])
    assert [x["run_id"] for x in first["runs"] + second["runs"]] == [101, 102]
    assert second["complete"]


def test_resume_rejects_bad_offset():
    with pytest.raises(ValueError):
        ghi.workflow_run_discovery("owner/repo", 7, page_size=2, start_offset=2)
