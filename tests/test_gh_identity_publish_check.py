"""publish_check: deploy/http/bytes/text checks, waiting, and the unreachable verdict."""
import functools
import http.server
import importlib.util
import json
import subprocess
import threading
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publish_check", ROOT / "scripts" / "publish_check.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


@pytest.fixture()
def site(tmp_path):
    served = tmp_path / "served"
    (served / "tools").mkdir(parents=True)
    (served / "tools" / "panel.js").write_text("export const v = 2;\n")
    (served / "index.html").write_text("<script src='tools/panel.js'></script>")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/", served
    server.shutdown()


def api_for(sha="abc1234def", state="success"):
    def api(path):
        return [{"id": 7, "sha": sha}] if path.endswith("per_page=1") and "statuses" not in path else [{"state": state}]
    return api


def run(argv, **kw):
    kw.setdefault("api", api_for())
    kw.setdefault("sleep", lambda s: None)
    return pc.main(argv, **kw)


def test_all_angles_ok(site, capsys):
    base, served = site
    code = run(["--site", base, "tools/panel.js", "", "--local-root", str(served),
                "--contains", "panel.js", "--contains-on", "", "--repo", "o/r", "--commit", "abc1234"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ok deploy: abc1234 success" in out and "ok bytes:" in out and "ok text: 'panel.js'" in out
    assert out.strip().endswith("verdict=ok")


def test_stale_bytes_and_missing_text_fail(site, tmp_path, capsys):
    base, _served = site
    local = tmp_path / "local" / "tools"
    local.mkdir(parents=True)
    (local / "panel.js").write_text("export const v = 3;\n")
    code = run(["--site", base, "tools/panel.js", "--local-root", str(tmp_path / "local"), "--contains", "v = 3"])
    out = capsys.readouterr().out
    assert code == 1 and "stale bytes:" in out and "fail text: 'v = 3'" in out


def test_404_and_deploy_states(site, capsys):
    base, _ = site
    assert run(["--site", base, "nope.js", "--commit", "x"]) == 1
    assert "fail http 404" in capsys.readouterr().out
    assert pc.deploy_check("o/r", "zzz", api_for())[0] == "stale"
    assert pc.deploy_check("o/r", None, api_for(state="in_progress"))[0] == "stale"
    assert pc.deploy_check("o/r", None, api_for(state="failure"))[0] == "fail"
    assert pc.deploy_check("o/r", None, lambda p: [])[0] == "fail"
    def boom(path):
        raise RuntimeError("403 proxy")
    assert pc.deploy_check("o/r", None, boom)[0] == "deploy-unavailable"


def test_wait_retries_until_ok():
    calls = []
    def get(url):
        calls.append(url)
        return (200 if len(calls) >= 3 else 404), b"x", ""
    sleeps = []
    code = pc.main(["https://example.invalid/a.js", "--wait", "100", "--interval", "1"], get=get, sleep=sleeps.append)
    assert code == 0 and len(calls) == 3 and len(sleeps) == 2
    assert all("publish_check=" in u for u in calls)


def test_unreachable_is_its_own_verdict_and_does_not_wait(capsys):
    get = lambda url: (None, b"", "connect_rejected")
    sleeps = []
    code = pc.main(["https://blocked.example/x", "--wait", "100", "--no-bust"], get=get, sleep=sleeps.append)
    out = capsys.readouterr().out
    assert code == 3 and sleeps == [] and "unreachable http: https://blocked.example/x (connect_rejected)" in out
    # unreachable site but a failed deploy is a failure, not "unreachable"
    assert pc.main(["https://blocked.example/x", "--repo", "o/r", "--commit", "x"], get=get,
                   api=api_for(state="failure"), sleep=lambda s: None) == 1


def test_usage_errors():
    with pytest.raises(SystemExit):
        pc.main([])
    with pytest.raises(SystemExit):
        pc.main(["relative/path.js"])


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("https://api.github.com", 401, "Bad credentials", {}, None),
    urllib.error.HTTPError("https://api.github.com", 403, "rate limit", {}, None),
    urllib.error.HTTPError("https://api.github.com", 429, "rate limit", {}, None),
    json.JSONDecodeError("invalid JSON", "x", 0),
    subprocess.TimeoutExpired(["gh", "api"], 60),
    RuntimeError("gh: authentication failed"),
])
def test_http_success_with_api_failure_keeps_independent_evidence(site, capsys, error):
    base, served = site
    def api(path):
        raise error
    code = run(["--site", base, "", "--local-root", str(served), "--contains", "panel.js",
                "--repo", "o/r", "--commit", "abc1234"], api=api)
    out = capsys.readouterr().out
    assert code == 1
    assert "deploy-unavailable deploy:" in out
    assert "ok http 200:" in out and "ok bytes:" in out and "ok text:" in out
    assert "verdict=unreachable" not in out
    # API-only and combined API/HTTP failures cannot claim a successful check.
    assert run(["--repo", "o/r", "--commit", "abc1234"], api=api) == 1
    assert run([base, "--repo", "o/r", "--commit", "abc1234"], api=api,
               get=lambda url: (None, b"", "proxy refused")) == 1


@pytest.mark.parametrize("payload", [None, {}, "bad", [None], [{}],
                                    [{"id": 7, "sha": None}]])
def test_malformed_deployments_are_unavailable(payload):
    result = pc.deploy_check("o/r", "abc1234", lambda path: payload)
    assert result[0] == "deploy-unavailable"
    assert pc.verdict([result]) == 1


@pytest.mark.parametrize("payload", [None, {}, "bad", [None], [{}],
                                    [{"state": None}], [{"state": "unknown"}]])
def test_malformed_statuses_are_unavailable(payload):
    def api(path):
        return payload if "/statuses?" in path else [{"id": 7, "sha": "abc1234def"}]
    assert pc.deploy_check("o/r", "abc1234", api)[0] == "deploy-unavailable"


@pytest.mark.parametrize("state", ["pending", "queued", "in_progress"])
def test_http_success_with_unfinished_deploy_is_not_published(state, capsys):
    assert run(["https://example.invalid/", "--repo", "o/r", "--commit", "abc1234"],
               api=api_for(state=state), get=lambda url: (200, b"ok", "")) == 1
    assert "stale deploy:" in capsys.readouterr().out


def test_api_failure_and_pending_deploy_retry_until_published(capsys):
    calls = []
    def api(path):
        if "/statuses?" in path:
            return [{"state": "pending" if len(calls) == 2 else "success"}]
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("rate limit")
        return [{"id": 7, "sha": "abc1234def"}]
    sleeps = []
    code = run(["https://example.invalid/", "--repo", "o/r", "--commit", "abc1234",
                "--wait", "100", "--interval", "1"], api=api,
               get=lambda url: (200, b"ok", ""), sleep=sleeps.append)
    assert code == 0 and sleeps == [1, 1] and len(calls) == 3
    assert "attempts=3 verdict=ok" in capsys.readouterr().out


def test_http_unreachable_with_successful_deploy_stops_early(capsys):
    sleeps = []
    code = run(["https://example.invalid/", "--repo", "o/r", "--commit", "abc1234",
                "--wait", "100"], get=lambda url: (None, b"", "proxy refused"), sleep=sleeps.append)
    out = capsys.readouterr().out
    assert code == 3 and sleeps == []
    assert "ok deploy:" in out and "unreachable http:" in out
