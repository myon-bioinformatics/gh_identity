"""Preserve CLI error contracts when content and local Git features combine."""
import json

import pytest

import gh_identity as ghi


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ghi.GitInspectionError("git failed"), "git_inspection_failed"),
        (OSError("cannot read saved HTML"), "invalid_argument"),
        (ValueError("invalid input"), "invalid_argument"),
        (ghi.Error("content_unavailable"), "content_unavailable"),
    ],
)
def test_cli_reports_merged_exception_types(monkeypatch, capsys, error, expected):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(ghi, "_html_content_file", fail)
    assert ghi.main(["html-content", "saved.html", "--selector", "#body"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "error", "error": expected}
