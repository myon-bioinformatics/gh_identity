import pytest
import gh_identity as ghi


def _repos():
    return [
        {"name":"a","full_name":"demo/a","private":False,"archived":False,"size":40,"updated_at":"2026-01-01"},
        {"name":"b","full_name":"demo/b","private":False,"archived":False,"size":10,"updated_at":"2026-01-02"},
        {"name":"hidden","full_name":"demo/hidden","private":True,"archived":False,"size":100},
        {"name":"old","full_name":"demo/old","private":False,"archived":True,"size":100}
    ]


def test_inventory_public_unarchived_and_size_sort(monkeypatch):
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(_repos(),{}))
    out=ghi.repository_inventory("demo",fields=("name","size-kb"),sort="size-kb",order="desc")
    assert [r["name"] for r in out["repositories"]]==["a","b"]
    assert out["complete"] and out["sort_scope"]=="complete"
    assert all(r["run_count_status"]=="not_requested" for r in out["repositories"])


def test_inventory_opt_in_run_counts(monkeypatch):
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(_repos(),{}))
    calls=[]
    def request(method,path,**kwargs):
        calls.append(path)
        return {"total_count": 5 if "/a/" in path else 20}
    monkeypatch.setattr(ghi,"request",request)
    out=ghi.repository_inventory("demo",fields="name,run-count",sort="run-count",order="desc")
    assert [r["name"] for r in out["repositories"]]==["b","a"]
    assert len(calls)==2
    assert all(r["run_count_status"]=="available" for r in out["repositories"])


def test_inventory_permission_not_zero(monkeypatch):
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(_repos(),{}))
    def denied(*a,**k):raise ghi.Error("permission_or_rate_limit")
    monkeypatch.setattr(ghi,"request",denied)
    out=ghi.repository_inventory("demo",fields="run-count")
    assert all(r["run_count"] is None for r in out["repositories"])
    assert all(r["run_count_status"]=="permission_or_rate_limit" for r in out["repositories"])


def test_inventory_partial_limit(monkeypatch):
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(_repos(),{}))
    out=ghi.repository_inventory("demo",max_repos=1)
    assert out["truncated"] and out["limit_reason"]=="max_repos"
    assert out["sort_scope"]=="partial"


@pytest.mark.parametrize("kw",[
    {"fields":"bogus"},{"sort":"bogus"},{"order":"reverse"},
    {"max_repos":0},{"max_pages":0}
])
def test_inventory_invalid_options(kw):
    with pytest.raises(ValueError):
        ghi.repository_inventory("demo",**kw)
