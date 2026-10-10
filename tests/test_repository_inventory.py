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


@pytest.mark.parametrize("metric,field,order,expected",[
    ("run-count","run_count","asc",["zero","five","twenty","missing"]),
    ("run-count","run_count","desc",["twenty","five","zero","missing"]),
    ("size-kb","size_kb","asc",["zero","five","twenty","missing"]),
    ("size-kb","size_kb","desc",["twenty","five","zero","missing"]),
    ("updated-at","updated_at","asc",["zero","five","twenty","missing"]),
    ("updated-at","updated_at","desc",["twenty","five","zero","missing"]),
])
def test_inventory_missing_metrics_sort_last(monkeypatch,metric,field,order,expected):
    sizes={"zero":0,"five":5,"twenty":20,"missing":None}
    dates={"zero":"2026-01-01T00:00:00Z","five":"2026-02-01T00:00:00Z",
           "twenty":"2026-03-01T00:00:00Z","missing":None}
    fixtures=[{"name":name,"full_name":"demo/"+name,"private":False,
               "archived":False,"size":size,"updated_at":dates[name]}
              for name,size in sizes.items()]
    monkeypatch.setattr(ghi,"_page",lambda *args,**kwargs:(fixtures,{}))
    calls=[]
    def get_count(method,path,**kwargs):
        calls.append(path)
        name=path.split("/")[2]
        if name=="missing":
            raise ghi.Error("permission_or_rate_limit")
        return {"total_count":sizes[name]}
    monkeypatch.setattr(ghi,"request",get_count)
    out=ghi.repository_inventory("demo",fields=(metric,),sort=metric,order=order)
    assert [row["name"] for row in out["repositories"]] == (
        ["zero","five","twenty","missing"] if metric=="size-kb" and order=="asc"
        else ["twenty","five","zero","missing"] if metric=="size-kb" and order=="desc"
        else expected
    )
    assert out["complete"] and not out["truncated"]
    assert out["sort_scope"]=="complete"
    if metric=="run-count":
        assert len(calls)==4
        assert out["repositories"][-1]["run_count"] is None
        assert out["repositories"][-1]["run_count_status"]=="permission_or_rate_limit"
        assert next(r for r in out["repositories"] if r["name"]=="zero")["run_count"]==0
    else:
        assert calls==[]


def test_inventory_missing_metrics_with_bounded_partial_results(monkeypatch):
    fixtures=[{"name":name,"full_name":"demo/"+name,"private":False,
               "archived":False,"size":size,"updated_at":None}
              for name,size in (("missing",None),("known",9),("extra",3))]
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(fixtures,{}))
    out=ghi.repository_inventory("demo",fields="size-kb",sort="size-kb",
                                 order="desc",max_repos=2)
    assert [r["name"] for r in out["repositories"]]==["known","missing"]
    assert out["count"]==2 and out["truncated"] and not out["complete"]
    assert out["limit_reason"]=="max_repos" and out["sort_scope"]=="partial"


@pytest.mark.parametrize("order,expected",[
    ("asc",["early","offset","late","invalid","missing"]),
    ("desc",["late","offset","early","invalid","missing"]),
])
def test_inventory_dates_normalized_and_malformed_last(monkeypatch,order,expected):
    timestamps={"early":"2026-01-01T00:00:00Z",
                "offset":"2026-01-01T10:00:00+09:00",
                "late":"2026-01-01T02:00:00Z",
                "invalid":"not-an-iso-date","missing":None}
    payload=[{"name":name,"full_name":"demo/"+name,"private":False,
              "archived":False,"size":0,"updated_at":date}
             for name,date in timestamps.items()]
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(payload,{}))
    out=ghi.repository_inventory("demo",sort="updated-at",order=order)
    assert [r["name"] for r in out["repositories"]]==expected
    assert out["complete"] and out["sort_scope"]=="complete"


@pytest.mark.parametrize("metric",["run-count","size-kb","updated-at"])
@pytest.mark.parametrize("order",["asc","desc"])
def test_inventory_equal_values_tiebreak_by_name(monkeypatch,metric,order):
    payload=[{"name":name,"full_name":"demo/"+name,"private":False,
              "archived":False,"size":4,"updated_at":"2026-01-01T00:00:00Z"}
             for name in ("zeta","alpha","beta")]
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(payload,{}))
    monkeypatch.setattr(ghi,"request",lambda *a,**k:{"total_count":4})
    out=ghi.repository_inventory("demo",sort=metric,order=order)
    assert [r["name"] for r in out["repositories"]]==["alpha","beta","zeta"]


@pytest.mark.parametrize("value",[
    123, {}, [], "2026-01-01T00:00:00", "not-a-date",
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
])
@pytest.mark.parametrize("order",["asc","desc"])
def test_inventory_unusable_dates_are_missing(monkeypatch,value,order):
    payload=[{"name":name,"full_name":"demo/"+name,"private":False,
              "archived":False,"updated_at":date}
             for name,date in (("missing",value),("known","2026-01-01T00:00:00Z"))]
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(payload,{}))
    out=ghi.repository_inventory("demo",sort="updated-at",order=order)
    assert [r["name"] for r in out["repositories"]]==["known","missing"]


@pytest.mark.parametrize("order",["asc","desc"])
def test_inventory_equal_instants_tiebreak_by_name(monkeypatch,order):
    payload=[{"name":name,"full_name":"demo/"+name,"private":False,
              "archived":False,"updated_at":date}
             for name,date in (("zeta","2026-01-01T00:00:00Z"),
                               ("alpha","2026-01-01T09:00:00+09:00"))]
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(payload,{}))
    out=ghi.repository_inventory("demo",sort="updated-at",order=order)
    assert [r["name"] for r in out["repositories"]]==["alpha","zeta"]


def test_inventory_exact_repo_limit_is_conservatively_partial(monkeypatch):
    monkeypatch.setattr(ghi,"_page",lambda *a,**k:(_repos()[:2],{}))
    out=ghi.repository_inventory("demo",max_repos=2)
    assert out["count"]==2 and out["truncated"] and not out["complete"]
    assert out["limit_reason"]=="max_repos" and out["sort_scope"]=="partial"
