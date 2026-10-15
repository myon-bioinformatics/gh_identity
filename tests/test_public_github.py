"""Public GitHub URL helpers (Atom branch feed, raw.githubusercontent.com)."""
import base64
import io
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import gh_identity as g

def _atom(*shas):
    entries = "".join(f'<entry><id>tag:github.com,2008:Grit::Commit/{sha}</id></entry>' for sha in shas)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'


ATOM = _atom("a" * 40, "b" * 40)


def test_public_github_branch_commits_and_head():
    with patch.object(g, "_public_github_get", return_value=ATOM.encode()):
        commits = g.public_github_branch_commits("me/up", "main")
        assert commits == ["a" * 40, "b" * 40]
        assert g.public_github_branch_head("me/up") == "a" * 40


def test_public_github_raw_bytes_builds_url():
    captured = {}
    def fake(url, **kw):
        captured["url"] = url
        return b"data\n"
    with patch.object(g, "_public_github_get", side_effect=fake):
        assert g.public_github_raw_bytes("me/up", "pkg/mod.py", "c" * 40) == b"data\n"
    assert captured["url"] == (
        "https://raw.githubusercontent.com/me/up/" + "c" * 40 + "/pkg/mod.py")


def test_public_github_repository_check_ok():
    body = b"x = 1\n"
    def fake(url, **kw):
        if url.endswith(".atom"):
            return ATOM.encode()
        return body
    with patch.object(g, "_public_github_get", side_effect=fake):
        out = g.public_github_repository_check("me/up", "b" * 40, source="mod.py")
    assert out["ok"] is True and out["atom"]["in_recent"] is True
    assert out["source"]["blob_sha"] == g.git_blob_sha(body)


def test_public_github_repository_check_missing_commit_in_feed():
    with patch.object(g, "_public_github_get", return_value=ATOM.encode()):
        out = g.public_github_repository_check("me/up", "f" * 40)
    assert out["ok"] is False and out["atom"]["in_recent"] is False


def test_public_github_repository_check_invalid_commit():
    with pytest.raises(ValueError):
        g.public_github_repository_check("me/up", "main")


COMMIT = "c" * 40


def _api_file_payload(body):
    meta = {"type": "file", "sha": g.git_blob_sha(body), "size": len(body)}
    blob = {"encoding": "base64", "content": base64.b64encode(body).decode()}
    return meta, blob


def test_remote_file_raw_success_skips_api():
    body = b"from raw\n"
    with patch.object(g, "public_github_raw_bytes", return_value=body) as raw, \
         patch.object(g, "request") as req:
        assert g._remote_file("o/up", "mod.py", COMMIT) == body
    raw.assert_called_once()
    req.assert_not_called()


def test_remote_file_branch_ref_skips_raw_uses_api():
    body = b"api only\n"
    meta, blob = _api_file_payload(body)
    with patch.object(g, "public_github_raw_bytes") as raw, \
         patch.object(g, "request", side_effect=[meta, blob]) as req:
        assert g._remote_file("o/up", "mod.py", "main") == body
    raw.assert_not_called()
    assert req.call_count == 2


@pytest.mark.parametrize("raw_code", [
    "permission_or_rate_limit", "rate_limit", "server_error", "operation_timeout",
])
def test_remote_file_raw_errors_fallback_to_api(raw_code):
    body = b"v\n"
    meta, blob = _api_file_payload(body)
    with patch.object(g, "public_github_raw_bytes", side_effect=g.Error(raw_code)), \
         patch.object(g, "request", side_effect=[meta, blob]) as req:
        assert g._remote_file("o/up", "mod.py", COMMIT) == body
    assert req.call_count == 2


def test_remote_file_raw_404_api_403_is_permission():
    with patch.object(g, "public_github_raw_bytes", side_effect=g.Error("not_found_or_inaccessible")), \
         patch.object(g, "request", side_effect=g.Error("permission_or_rate_limit")):
        with pytest.raises(g.Error) as exc:
            g._remote_file("o/up", "mod.py", COMMIT)
    assert exc.value.code == "permission_or_rate_limit"


def test_remote_file_raw_404_api_404_is_not_found():
    with patch.object(g, "public_github_raw_bytes", side_effect=g.Error("not_found_or_inaccessible")), \
         patch.object(g, "request", side_effect=g.Error("not_found_or_inaccessible")):
        with pytest.raises(g.Error) as exc:
            g._remote_file("o/up", "mod.py", COMMIT)
    assert exc.value.code == "not_found_or_inaccessible"


def test_public_github_raw_bytes_url_encodes_ref():
    captured = {}
    def fake(url, **kw):
        captured["url"] = url
        return b"x"
    ref = "a" * 40
    with patch.object(g, "_public_github_get", side_effect=fake):
        g.public_github_raw_bytes("me/up", "f.py", ref)
    assert f"/{ref}/f.py" in captured["url"]
    with patch.object(g, "_public_github_get", side_effect=fake):
        g.public_github_raw_bytes("me/up", "f.py", "feature/foo")
    assert "/feature%2Ffoo/f.py" in captured["url"]


def _http_error(code):
    return urllib.error.HTTPError("https://example.test/", code, "msg", {}, io.BytesIO(b""))


@pytest.mark.parametrize("status,expected", [
    (404, "not_found_or_inaccessible"),
    (403, "permission_or_rate_limit"),
    (401, "permission_or_rate_limit"),
    (429, "rate_limit"),
    (500, "server_error"),
    (502, "server_error"),
    (418, "invalid_response"),
])
def test_public_github_get_http_status_mapping(status, expected):
    with patch("urllib.request.urlopen", side_effect=_http_error(status)):
        with pytest.raises(g.Error) as exc:
            g._public_github_get("https://github.com/o/r/commits/main.atom")
    assert exc.value.code == expected


def test_public_github_get_timeout_is_operation_timeout():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(TimeoutError())):
        with pytest.raises(g.Error) as exc:
            g._public_github_get("https://raw.githubusercontent.com/o/r/sha/f")
    assert exc.value.code == "operation_timeout"


def test_public_github_get_200_returns_body():
    payload = b"ok"
    response = MagicMock()
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    response.getcode = MagicMock(return_value=200)
    response.read = MagicMock(return_value=payload)
    with patch("urllib.request.urlopen", return_value=response):
        assert g._public_github_get("https://example.test/x") == payload
