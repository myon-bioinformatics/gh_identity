"""labels: intentional add/remove of existing labels; planned unless --write."""
import json

import pytest
import gh_identity as g


class FakeGitHub:
    def __init__(self, labels, existing):
        self.labels, self.existing, self.writes = list(labels), set(existing), []

    def __call__(self, method, path, payload=None, transport="auto", timeout=30, mutating=False):
        if method == "GET" and "/labels/" in path and "/issues/" not in path:
            name = g.urllib.parse.unquote(path.rsplit("/", 1)[1])
            if name not in self.existing:
                raise g.Error("not_found_or_inaccessible")
            return {"name": name}
        if method == "GET":
            return {"number": 5, "repository_url": g.API + "/repos/o/r", "state": "open", "title": "T",
                    "html_url": "https://github.com/o/r/issues/5", "user": {"login": "u"},
                    "labels": [{"name": n} for n in self.labels]}
        assert mutating
        self.writes.append((method, path, payload))
        if method == "POST":
            self.labels += [n for n in payload["labels"] if n not in self.labels]
        else:
            self.labels.remove(g.urllib.parse.unquote(path.rsplit("/", 1)[1]))
        return {}


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub(["status:in-progress", "type:tracking"], {"priority:high", "status:in-progress", "type:tracking", "a b"})
    monkeypatch.setattr(g, "request", fake)
    return fake


def test_planned_by_default_changes_nothing(gh):
    out = g.set_labels("o/r", 5, add=["priority:high"], remove=["status:in-progress"])
    assert out["status"] == "planned" and gh.writes == []
    assert out["target"] == ["priority:high", "type:tracking"]


def test_write_adds_removes_and_verifies(gh):
    out = g.set_labels("o/r", 5, add=["priority:high", "a b"], remove=["status:in-progress"], write=True)
    assert out["status"] == "verified" and out["after"] == ["a b", "priority:high", "type:tracking"]
    assert gh.writes[0] == ("POST", "repos/o/r/issues/5/labels", {"labels": ["priority:high", "a b"]})
    assert gh.writes[1][:2] == ("DELETE", "repos/o/r/issues/5/labels/status%3Ain-progress")


def test_unknown_label_is_rejected_not_created(gh):
    out = g.set_labels("o/r", 5, add=["prio:typo"], write=True)
    assert out["status"] == "rejected_missing_labels" and out["missing_labels"] == ["prio:typo"] and gh.writes == []


def test_nothing_to_do_is_already_satisfied(gh):
    out = g.set_labels("o/r", 5, add=["type:tracking"], remove=["priority:high"], write=True)
    assert out["status"] == "already_satisfied" and gh.writes == []


def test_mismatch_after_write_is_reported(gh, monkeypatch):
    real = gh.__call__
    def drop_post(method, path, payload=None, *a, **k):
        if method == "POST":
            gh.writes.append((method, path, payload))
            return {}
        return real(method, path, payload, *a, **k)
    monkeypatch.setattr(g, "request", drop_post)
    out = g.set_labels("o/r", 5, add=["priority:high"], write=True)
    assert out["status"] == "verification_mismatch" and not out["verified"]


@pytest.mark.parametrize("kwargs", [{}, {"add": ["x"], "remove": ["x"]}, {"add": [""]}, {"add": ["a\nb"]}])
def test_invalid_requests(gh, kwargs):
    with pytest.raises(ValueError):
        g.set_labels("o/r", 5, **kwargs)


def test_cli_exit_codes(gh, capsys):
    assert g.main(["labels", "o/r", "5", "--add", "priority:high", "--remove", "status:in-progress"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert g.main(["labels", "o/r", "5", "--add", "nope"]) == 1
    capsys.readouterr()
    assert g.main(["labels", "o/r", "5", "--add", "priority:high", "--write"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
