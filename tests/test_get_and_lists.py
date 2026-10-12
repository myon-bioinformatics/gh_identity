"""get: read-only GET with query options and field picking; --text for prs/issues."""
import pytest
import gh_identity as g


def test_query_is_encoded_and_fields_are_picked(monkeypatch):
    seen = []
    rows = [{"number": 3, "head": {"ref": "work/x"}, "title": "T 1"}, {"number": 2, "head": None, "title": "T&2"}]
    monkeypatch.setattr(g, "request", lambda m, path, **k: seen.append((m, path)) or rows)
    out = g.get("repos/o/r/pulls", ["state=open", "per_page=2"], ["number", "head.ref"])
    assert seen == [("GET", "repos/o/r/pulls?state=open&per_page=2")]
    assert out["data"] == [{"number": 3, "head.ref": "work/x"}, {"number": 2, "head.ref": None}]
    assert g.render_get(out) == "3 work/x\n2 -"


def test_wrapped_lists_are_unwrapped_when_picking(monkeypatch):
    body = {"total_count": 2, "check_runs": [{"name": "ci", "status": "completed", "conclusion": "failure"},
                                             {"name": "e2e", "status": "queued", "conclusion": None}]}
    monkeypatch.setattr(g, "request", lambda *a, **k: body)
    out = g.get("repos/o/r/commits/abc/check-runs", [("per_page", "100")], ["status", "conclusion", "name"])
    assert g.render_get(out).splitlines() == ["completed failure ci", "queued - e2e"]
    assert g.get("repos/o/r/commits/abc/check-runs")["data"] == body  # no pick: whole object


@pytest.mark.parametrize(("path", "query"), [
    ("repos/o/r/pulls?state=open", []),   # options belong in query, not the path
    ("repos/o/r/../x", []),
    ("/repos/o/r", []),
    ("graphql", []),
    ("repos/o/r/pulls", ["state"]),
    ("repos/o/r/pulls", ["bad-key=1"]),
])
def test_invalid_path_or_query_is_rejected(monkeypatch, path, query):
    monkeypatch.setattr(g, "request", lambda *a, **k: pytest.fail("must not request"))
    with pytest.raises(ValueError):
        g.get(path, query)


def test_text_lists_for_prs_and_issues():
    prs = {"pull_requests": [{"number": 9, "state": "open", "draft": True, "head_ref": "w/a", "title": "A"}],
           "count": 1, "truncated": True}
    assert g.render_list(prs) == "#9 open draft w/a A\n(truncated after 1)"
    issues = {"issues": [{"number": 4, "state": "open", "labels": ["p:high"], "title": "B"}], "count": 1}
    assert g.render_list(issues) == "#4 open [p:high] B"


def test_cli_get_text(monkeypatch, capsys):
    monkeypatch.setattr(g, "request", lambda *a, **k: [{"number": 1, "title": "x"}])
    assert g.main(["get", "repos/o/r/pulls", "-q", "state=open", "--pick", "number", "--text"]) == 0
    assert capsys.readouterr().out == "1\n"
    assert g.main(["get", "repos/o/r/pulls?state=open"]) == 2
