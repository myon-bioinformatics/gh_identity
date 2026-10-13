import gh_identity as ghi


def test_capabilities_versions_present(monkeypatch):
    monkeypatch.setattr(ghi, "tool_versions", lambda commands: {"git": "2.47.1", "gh": "2.65.0"})
    monkeypatch.setattr(ghi, "gh_available", lambda: True)
    monkeypatch.setattr(ghi.shutil, "which", lambda command: "/usr/bin/" + command)
    out = ghi.capabilities()
    assert out["schema"] == "gh-identity-capabilities/1"
    assert out["git"] is True and out["gh"] is True
    assert out["git_version"] == "2.47.1"
    assert out["gh_version"] == "2.65.0"


def test_capabilities_missing_versions_are_null(monkeypatch):
    monkeypatch.setattr(ghi, "tool_versions", lambda commands: {})
    monkeypatch.setattr(ghi, "gh_available", lambda: False)
    monkeypatch.setattr(ghi.shutil, "which", lambda command: None)
    out = ghi.capabilities()
    assert out["git"] is False and out["gh"] is False
    assert out["git_version"] is None and out["gh_version"] is None


def test_capabilities_version_probe_is_bounded_and_nonfatal(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "error"

    monkeypatch.setattr(ghi.shutil, "which", lambda command: "/bin/" + command)
    monkeypatch.setattr(ghi, "gh_available", lambda: True)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(ghi.subprocess, "run", run)
    out = ghi.capabilities()
    assert out["git_version"] is None and out["gh_version"] is None
    assert len(calls) == 2
    assert all(kwargs["timeout"] == 5.0 for _, kwargs in calls)
    assert all(kwargs["check"] is False for _, kwargs in calls)


def test_capabilities_malformed_and_timeout_nonfatal(monkeypatch):
    monkeypatch.setattr(ghi.shutil, "which", lambda command: "/bin/" + command)
    monkeypatch.setattr(ghi, "gh_available", lambda: True)

    def run(args, **kwargs):
        if args[0].endswith("/git"):
            raise ghi.subprocess.TimeoutExpired(args, kwargs["timeout"])
        return ghi.subprocess.CompletedProcess(args, 0, "unexpected version", "")

    monkeypatch.setattr(ghi.subprocess, "run", run)
    out = ghi.capabilities()
    assert out["git_version"] is None and out["gh_version"] is None
