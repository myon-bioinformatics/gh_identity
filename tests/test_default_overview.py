import json
import subprocess

import pytest
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
    assert result["environment"]["git_version"] == "2.45"
    assert result["environment"]["gh_version"] == "2.60"


@pytest.mark.parametrize("argv", [[], ["overview"]])
def test_default_output_excludes_local_placement(monkeypatch, capsys, argv):
    secret = "/home/private-user/private-checkout"
    position = {"cwd": secret, "detail": secret, "future_field": secret,
                "git": {"top_level": secret, "relative": "private-subdir",
                        "head": {"sha": "a" * 40, "branch": "main", "detached": False,
                                 "timestamp": "2026-10-13T00:00:00Z", "subject": secret},
                        "changes": {"staged": 1, "unstaged": 2, "untracked": 3, "path": secret},
                        "changes_truncated": True,
                        "remote": {"remote": "origin", "full_name": "owner/repo",
                                   "reason": None, "url": secret}}}
    monkeypatch.setattr(ghi, "local_position", lambda: position)
    monkeypatch.setattr(ghi, "tool_versions", lambda *a: {"git": "2.45", "gh": "2.60"})
    for name in ("pull_requests", "issues", "run_history"):
        monkeypatch.setattr(ghi, name, lambda *a, **k: {})
    assert ghi.main(argv) == 0
    output = capsys.readouterr().out
    assert secret not in output and "private-subdir" not in output
    result = json.loads(output)
    git = result["local"]["git"]
    assert set(result["local"]) == {"git"}
    assert set(git) == {"head", "changes", "changes_truncated", "remote"}
    assert git["changes"] == {"staged": 1, "unstaged": 2, "untracked": 3}
    assert git["changes_truncated"] is True
    assert result["remote"] == {"repository": "owner/repo"}
    assert position["cwd"] == secret and position["git"]["top_level"] == secret


def test_local_error_detail_is_not_published(monkeypatch):
    monkeypatch.setattr(ghi, "tool_versions", lambda *a: {})
    monkeypatch.setattr(ghi, "local_position", lambda: {
        "cwd": "C:\\Users\\private", "git": None, "reason": "git_error",
        "detail": "permission denied: C:\\Users\\private"})
    assert ghi.overview()["local"] == {"git": None, "reason": "git_error"}


@pytest.mark.parametrize("failed", ["pull_requests", "issues", "run_history"])
@pytest.mark.parametrize("error", [ghi.Error("permission_or_rate_limit"),
                                   OSError("private path"), ValueError("invalid response")])
def test_each_remote_failure_preserves_other_sections(monkeypatch, failed, error):
    monkeypatch.setattr(ghi, "tool_versions", lambda *a: {"git": "2.45", "gh": "2.60"})
    monkeypatch.setattr(ghi, "local_position", lambda: {"git": {
        "changes": {"staged": 1, "unstaged": 0, "untracked": 1},
        "remote": {"full_name": "owner/repo"}}})
    calls = []
    def probe(name):
        def run(*args, **kwargs):
            calls.append(name)
            assert args == ("owner/repo",)
            assert kwargs == {"max_items": 5, "max_pages": 1}
            if name == failed:
                raise error
            return {"observed": name}
        return run
    names = {"pull_requests": "pull_requests", "issues": "issues", "ci": "run_history"}
    for name in names.values():
        monkeypatch.setattr(ghi, name, probe(name))
    result = ghi.overview()
    assert calls == list(names.values())
    for key, name in names.items():
        assert result[key] == (None if name == failed else {"observed": name})
    assert result["environment"]["gh_version"] == "2.60"
    assert result["local"]["git"]["changes"]["staged"] == 1


def test_real_checkout_changes_and_explicit_position_contract(tmp_path, monkeypatch, capsys):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (tmp_path / "tracked").write_text("initial")
    git("add", "tracked")
    git("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")
    (tmp_path / "tracked").write_text("modified")
    (tmp_path / "staged").write_text("staged")
    git("add", "staged")
    (tmp_path / "untracked").write_text("untracked")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ghi, "tool_versions", lambda *a: {})
    assert ghi.main([]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert str(tmp_path) not in output
    assert result["local"]["git"]["changes"] == {"staged": 1, "unstaged": 1, "untracked": 1}
    assert result["local"]["git"]["head"]["sha"] == git("rev-parse", "HEAD")
    assert ghi.main(["local-position"]) == 0
    explicit = json.loads(capsys.readouterr().out)
    assert explicit["cwd"] == str(tmp_path)
    assert explicit["git"]["top_level"] == str(tmp_path)
    assert explicit["git"]["relative"] == "."


def test_no_arguments_dispatches_overview(monkeypatch, capsys):
    monkeypatch.setattr(ghi, "overview", lambda: {"schema": "gh-identity-overview/1"})
    assert ghi.main([]) == 0
    assert "gh-identity-overview/1" in capsys.readouterr().out
