import gh_identity as ghi


def test_overview_without_checkout_or_tools(monkeypatch):
    monkeypatch.setattr(ghi, "tool_versions", lambda *a: {})
    monkeypatch.setattr(ghi, "local_position", lambda: {"git": None, "reason": "not_a_checkout"})
    result = ghi.overview()
    assert result["environment"]["git_version"] is None
    assert result["remote"] is None
    assert result["pull_requests"] is None
    assert result["issues"] is None
    assert result["ci"] is None
    assert result["next_actions"]


def test_overview_remote_and_partial_failures(monkeypatch):
    monkeypatch.setattr(ghi, "tool_versions", lambda *a: {"git": "2.45", "gh": "2.60"})
    monkeypatch.setattr(ghi, "local_position", lambda: {"git": {"remote": {"full_name": "owner/repo"}}})
    monkeypatch.setattr(ghi, "pull_requests", lambda *a, **k: {"pull_requests": []})
    monkeypatch.setattr(ghi, "issues", lambda *a, **k: (_ for _ in ()).throw(ghi.Error("permission_or_rate_limit")))
    monkeypatch.setattr(ghi, "run_history", lambda *a, **k: {"runs": []})
    result = ghi.overview()
    assert result["remote"]["repository"] == "owner/repo"
    assert result["pull_requests"] == {"pull_requests": []}
    assert result["issues"] is None
    assert result["ci"] == {"runs": []}


def test_no_arguments_dispatches_overview(monkeypatch, capsys):
    monkeypatch.setattr(ghi, "overview", lambda: {"schema": "gh-identity-overview/1"})
    assert ghi.main([]) == 0
    assert "gh-identity-overview/1" in capsys.readouterr().out
