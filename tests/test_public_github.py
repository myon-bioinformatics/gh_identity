"""Public GitHub URL helpers (Atom branch feed, raw.githubusercontent.com)."""
from unittest.mock import patch

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


def test_remote_file_falls_back_to_api_on_public_miss():
    import base64
    meta = {"type": "file", "sha": g.git_blob_sha(b"v\n"), "size": 2}
    blob = {"encoding": "base64", "content": base64.b64encode(b"v\n").decode()}
    with patch.object(g, "public_github_raw_bytes", side_effect=g.Error("not_found_or_inaccessible")), \
         patch.object(g, "request", side_effect=[meta, blob]) as req:
        assert g._remote_file("o/up", "mod.py", "c" * 40) == b"v\n"
    assert req.call_count == 2
