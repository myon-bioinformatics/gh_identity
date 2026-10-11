"""Run artifacts: identity-checked listing and bounded single-member reads."""
import base64
import hashlib
import io
import json
import zipfile

import pytest
import gh_identity as g
from test_job_logs import Opener, Response

SIGNED = "https://productionresultssa1.blob.core.windows.net/a.zip?sig=x"


def _zip(**members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_binary_reader_follows_storage_redirect_without_credentials():
    body = _zip(**{"r.json": "{}"})
    opener = Opener(Response(status=302, headers={"Location": SIGNED}),
                    Response(body, headers={"Content-Type": "application/zip"}))
    spec = {"url": g.API + "/repos/o/r/actions/artifacts/9/zip", "headers": {"Authorization": "Bearer t"},
            "timeout": 10, "max_bytes": 1 << 20, "cap": 1 << 20, "redact": [], "binary": True}
    out = g._read_job_log(spec, opener=opener)
    assert base64.b64decode(out["data_b64"]) == body and out["sha256"] == hashlib.sha256(body).hexdigest()
    after = dict((k.lower(), v) for k, v in opener.calls[1][0].header_items())
    assert "authorization" not in after


def test_text_reader_still_rejects_zip_content_type():
    opener = Opener(Response(b"PK", headers={"Content-Type": "application/zip"}))
    spec = {"url": g.API + "/x", "headers": {}, "timeout": 10, "max_bytes": 64, "cap": 64, "redact": []}
    with pytest.raises(g.Error):
        g._read_job_log(spec, opener=opener)


def _meta(**changes):
    return {"id": 9, "name": "junit-flutter-refactor", "expired": False,
            "workflow_run": {"id": 5, "head_sha": "a" * 40}, **changes}


def _patch(monkeypatch, body, meta=None):
    monkeypatch.setattr(g, "request", lambda *a, **k: meta or _meta())
    monkeypatch.setattr(g, "_download_job_log", lambda *a, **k: {
        "complete": True, "data_b64": base64.b64encode(body).decode(), "sha256": "z", "bytes_read": len(body)})


def test_artifact_file_returns_member_text_and_digest(monkeypatch):
    _patch(monkeypatch, _zip(**{"route-verification.json": '{"result": "success"}', "other.xml": "<x/>"}))
    out = g.artifact_file("o/r", 9, "route-verification.json")
    assert out["complete"] and json.loads(out["text"]) == {"result": "success"}
    assert out["sha256"] == hashlib.sha256(b'{"result": "success"}').hexdigest()
    assert (out["run_id"], out["head_sha"], out["name"]) == (5, "a" * 40, "junit-flutter-refactor")


def test_member_larger_than_the_cap_is_not_expanded(monkeypatch):
    _patch(monkeypatch, _zip(**{"big.txt": "x" * 5000}))
    out = g.artifact_file("o/r", 9, "big.txt", max_bytes=100)
    assert out["truncated"] and out["text"] is None


@pytest.mark.parametrize(("meta", "member", "error"), [
    (None, "missing.json", "artifact_member_not_found"),
    (_meta(expired=True), "r.json", "artifact_expired"),
    (_meta(id=10), "r.json", "artifact_identity_mismatch"),
])
def test_artifact_file_fails_closed(monkeypatch, meta, member, error):
    _patch(monkeypatch, _zip(**{"r.json": "{}"}), meta)
    with pytest.raises(g.Error) as exc:
        g.artifact_file("o/r", 9, member)
    assert exc.value.code == error


@pytest.mark.parametrize("member", ["../etc/passwd", "/abs", ""])
def test_unsafe_member_names_are_rejected(member):
    with pytest.raises(ValueError):
        g.artifact_file("o/r", 9, member)


def test_corrupt_zip_is_an_archive_error(monkeypatch):
    _patch(monkeypatch, b"not a zip")
    with pytest.raises(g.Error) as exc:
        g.artifact_file("o/r", 9, "r.json")
    assert exc.value.code == "invalid_artifact_archive"


def test_artifact_listing_requires_rows_of_the_requested_run(monkeypatch):
    rows = [{"id": 1, "name": "a", "workflow_run": {"id": 5, "head_sha": "a" * 40}}]
    monkeypatch.setattr(g, "_page", lambda path, **k: ({"total_count": 1, "artifacts": rows}, {}))
    assert g.artifacts("o/r", 5)["complete"]
    with pytest.raises(g.Error):
        g.artifacts("o/r", 6)
