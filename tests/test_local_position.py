"""local-position: where am I (cwd, Git top level, HEAD, changes, remote)."""
import json
import subprocess

import pytest
import gh_identity as g


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return tmp_path


@pytest.mark.parametrize("url,full_name", [
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo", "owner/repo"),
    ("https://user:ghp_secret@github.com/owner/repo.git", "owner/repo"),
    ("git@github.com:owner/repo.git", "owner/repo"),
    ("ssh://git@github.com/owner/repo.git", "owner/repo"),
    ("http://proxy@127.0.0.1:8080/git/owner/repo", None),
    ("https://gitlab.com/owner/repo.git", None),
    ("https://github.com/owner/repo/extra", None),
])
def test_remote_repository_never_echoes_the_url(repo, url, full_name):
    _git(repo, "remote", "add", "origin", url)
    out = g.remote_repository(repo)
    assert out["full_name"] == full_name
    assert out["reason"] == (None if full_name else "not_github")
    assert "secret" not in json.dumps(out) and "127.0.0.1" not in json.dumps(out)


def test_missing_remote(repo):
    assert g.remote_repository(repo) == {"remote": "origin", "full_name": None, "reason": "no_remote"}
    with pytest.raises(ValueError):
        g.remote_repository(repo, remote="--upload-pack=x")


def test_position_counts_changes_and_relative_dir(repo):
    _git(repo, "remote", "add", "origin", "https://github.com/owner/repo.git")
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("new\n")
    (repo / "c.txt").write_text("staged\n")
    _git(repo, "add", "c.txt")
    (repo / "sub").mkdir()
    out = g.local_position(repo / "sub")
    git = out["git"]
    assert out["cwd"].endswith("/sub") and git["relative"] == "sub"
    assert git["changes"] == {"staged": 1, "unstaged": 1, "untracked": 1}
    assert git["head"]["sha"] == _git(repo, "rev-parse", "HEAD") and git["head"]["branch"] == "main"
    assert git["remote"]["full_name"] == "owner/repo"


def test_outside_a_checkout(tmp_path):
    out = g.local_position(tmp_path)
    assert out["git"] is None and out["reason"] == "not_a_checkout"


def test_cli(repo, capsys):
    assert g.main(["local-position", "--root", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out)["git"]["head"]["branch"] == "main"


def test_unborn_head_is_no_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    assert g.local_position(tmp_path)["reason"] == "no_commit"
