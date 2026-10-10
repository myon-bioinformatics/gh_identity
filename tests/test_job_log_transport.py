"""Real isolated workers against offline HTTP fixtures; all values are synthetic."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

import gh_identity as g


SHA = "a" * 40
SYNTHETIC_TOKEN = "ghp_OfflineFixtureOnly123456789"
SYNTHETIC_SECRET = "offline-fixture-canary"


class LogHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.endswith("/attempts/2"):
            body = json.dumps({"id": 20, "run_attempt": 2, "head_sha": SHA}).encode()
            media = "application/json"
        elif "/attempts/2/jobs?" in self.path:
            body = json.dumps({"total_count": 1, "jobs": [{
                "id": 91, "run_id": 20, "run_attempt": 2, "head_sha": SHA,
                "status": "completed", "steps": [{"number": 1}],
            }]}).encode()
            media = "application/json"
        else:
            media = "text/plain; charset=utf-8"
            if self.path == "/invalid":
                body = b"x" * 900 + b"\xff"
            elif self.path == "/partial":
                body = b"x" * 900
            elif self.path == "/oversize":
                body = b"x" * 100
            elif self.path == "/slow":
                body = b"x" * 100
            else:
                body = ("\ufeff日本語\r\n" + SYNTHETIC_TOKEN + "\n" +
                        SYNTHETIC_SECRET[:7] + "\x1b[31m" + SYNTHETIC_SECRET[7:] + "\x1b[0m\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", media)
        if self.path != "/oversize":
            self.send_header("Content-Length", str(len(body) + (50 if self.path == "/partial" else 0)))
        self.end_headers()
        try:
            if self.path == "/slow":
                for value in body:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    time.sleep(.04)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def log_http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), LogHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def download(path, **kwargs):
    options = {"max_bytes": 1000, "redact": (SYNTHETIC_SECRET,), "transport": "urllib", "timeout": 3}
    options.update(kwargs)
    return g._download_job_log(path, **options)


def test_real_full_api_uses_metadata_and_returns_only_sanitized_body(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    result = g.job_log("o/r", 20, 91, attempt=2, redact=(SYNTHETIC_SECRET,), transport="urllib")
    assert result["text"] == "日本語\n[REDACTED]\n[REDACTED]\n"
    assert result["head_sha"] == SHA
    assert result["step_urls"] == [{"number": 1, "url": g.step_url("o/r", 20, 91, 1)}]
    assert result["transport"] == "urllib" and result["credential_source"] == "anonymous"


def test_real_cli_passes_only_environment_names_and_prints_sanitized_json(log_http):
    env = os.environ.copy()
    env.update(GH_TOKEN=SYNTHETIC_TOKEN, LOG_FIXTURE_SECRET=SYNTHETIC_SECRET)
    code = "import sys,gh_identity as g;g.API=sys.argv[1];raise SystemExit(g.main(sys.argv[2:]))"
    argv = [sys.executable, "-c", code, log_http, "job-log", "o/r", "20", "91", "--attempt", "2",
            "--redact-env", "LOG_FIXTURE_SECRET", "--transport", "urllib"]
    result = subprocess.run(argv, env=env, capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stderr == b""
    assert SYNTHETIC_TOKEN not in result.stdout.decode() and SYNTHETIC_SECRET not in result.stdout.decode()
    assert json.loads(result.stdout)["credential_source"] == "environment"
    assert all(SYNTHETIC_SECRET not in item and SYNTHETIC_TOKEN not in item for item in argv)


def test_worker_oversize_returns_no_secret_fragment(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    with g.operation():
        result = download("oversize", max_bytes=20)
    assert result["complete"] is False and result["truncated"] is True
    assert result["text"] is None and result["text_sha256"] is None
    assert result["bytes_read"] == 21


@pytest.mark.parametrize("path,error", [("invalid", "invalid_log_encoding"), ("partial", "incomplete_log_response")])
def test_failed_body_bytes_are_cumulative_and_cannot_be_retried_past_budget(log_http, monkeypatch, path, error):
    monkeypatch.setattr(g, "API", log_http)
    with g.operation(max_bytes=1200):
        with pytest.raises(g.Error, match=error):
            download(path)
        assert g._BUDGET.get().bytes >= 900
        with pytest.raises(g.Error, match="bytes_limit"):
            download(path)
        with pytest.raises(g.Error, match="bytes_limit"):
            g._BUDGET.get().remaining()


def test_real_slow_drip_is_stopped_by_outer_deadline(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    started = time.monotonic()
    with g.operation(timeout=.35):
        with pytest.raises(g.Error, match="operation_timeout"):
            download("slow", timeout=3)
        with pytest.raises(g.Error, match="operation_timeout"):
            g._BUDGET.get().remaining()
    assert time.monotonic() - started < 2


def test_normalized_body_cannot_hide_received_bytes_from_global_cap(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    with g.operation(max_bytes=10), pytest.raises(g.Error, match="bytes_limit"):
        download("logs")


def test_gh_credential_uses_host_pinned_command_and_stdin_to_worker(log_http, monkeypatch):
    monkeypatch.setattr(g, "API", log_http)
    calls = []
    real = g._process
    def process(argv, payload, timeout, env=None, mutating=False):
        calls.append((argv, payload))
        if argv[0] == "gh":
            assert argv == ["gh", "auth", "token", "--hostname", "github.com"]
            assert env["GH_PROMPT_DISABLED"] == "1" and env["GH_HOST"] == "github.com"
            return 0, SYNTHETIC_TOKEN.encode() + b"\n", b""
        assert SYNTHETIC_TOKEN not in " ".join(argv)
        assert json.loads(payload)["headers"]["Authorization"] == "Bearer " + SYNTHETIC_TOKEN
        return real(argv, payload, timeout, env, mutating)
    with g.operation(), patch.object(g, "_process", side_effect=process):
        result = download("logs", transport="gh")
    assert result["credential_source"] == "gh"
    assert result["text"] == "日本語\n[REDACTED]\n[REDACTED]\n"
    assert len(calls) == 2


@pytest.mark.parametrize("transport,status,stderr,expected", [
    ("auto", 1, b"no oauth token found for github.com", "anonymous"),
    ("auto", 4, b"authentication needed", "anonymous"),
    ("gh", 1, b"no oauth token found for github.com", "authentication_required"),
    ("auto", 2, b"cancelled", "cancelled"),
    ("gh", 2, b"cancelled", "cancelled"),
    ("auto", 1, b"unrelated failure", "gh_failed"),
    ("gh", 1, b"unrelated failure", "gh_failed"),
])
def test_gh_fallback_only_handles_missing_authentication(transport, status, stderr, expected):
    with g.operation(), patch.object(g, "gh_available", return_value=True), patch.object(g, "_process", return_value=(status, b"", stderr)):
        if expected == "anonymous":
            assert g._job_log_credential(transport, 1) == (None, "anonymous")
        else:
            with pytest.raises(g.Error, match=expected):
                g._job_log_credential(transport, 1)


@pytest.mark.parametrize("text,values,expected", [
    ("foobar", ("foo\x1b[31m", "foobar"), "[REDACTED]"),
    ("ghp_AAAAfooBBBB", ("foo",), "[REDACTED]"),
    ("ghp_OfflineSynthetic", ("ghp_",), "[REDACTED]"),
    ("token=offline-value", ("token",), "[REDACTED]=[REDACTED]"),
    ("-----BEGIN RSA PRIVATE KEY-----\nfixture\n-----END RSA PRIVATE KEY-----", ("RSA",), "[REDACTED]"),
    ("Authorization : Bearer offline-value", (), "Authorization : Bearer [REDACTED]"),
], ids=["normalized-overlap", "token-interior", "token-prefix", "assignment-key", "pem-label", "authorization-spacing"])
def test_redaction_rules_do_not_disable_each_other(text, values, expected):
    assert g._redact_job_log(text, values)[0] == expected


def test_add_mask_honors_case_words_and_workflow_data_escapes():
    text = "::ADD-MASK::Mona The Octocat%0Asecond%25value\nMona\nOctocat\nsecond%value\n"
    result, _ = g._redact_job_log(text, ())
    assert result == "::ADD-MASK::[REDACTED]\n[REDACTED]\n[REDACTED]\n[REDACTED]\n"


def test_metadata_gh_http_401_retains_authentication_classification():
    with patch.object(g, "_process", return_value=(1, b"", b"Bad credentials (HTTP 401)")):
        with pytest.raises(g.Error, match="authentication_required"):
            g.run("o/r", 20, attempt=2, transport="gh")
