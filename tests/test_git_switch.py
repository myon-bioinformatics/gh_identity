"""git switch: planned by default, performed only with write, verified after."""
import json
import subprocess

import pytest
import gh_identity as g


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    _git(tmp_path, "branch", "feature")
    return tmp_path


def test_plan_changes_nothing(repo):
    out = g.git_switch(repo, "feature")
    assert (out["status"], out["command"]) == ("planned", ["git", "switch", "--no-guess", "feature"])
    assert _git(repo, "branch", "--show-current") == "main"


def test_switch_existing_then_back_to_previous(repo):
    out = g.git_switch(repo, "feature", write=True)
    assert out["status"] == "verified" and out["after"]["branch"] == "feature"
    back = g.git_switch(repo, previous=True, write=True)
    assert (back["target"], back["status"], back["after"]["branch"]) == ("main", "verified", "main")


def test_create_switches_to_a_new_branch_at_the_same_commit(repo):
    out = g.git_switch(repo, "topic/new", create=True, write=True)
    assert out["status"] == "verified"
    assert out["after"] == {"branch": "topic/new", "head_sha": out["before"]["head_sha"]}


@pytest.mark.parametrize(("branch", "create", "error"), [
    ("missing", False, g.GitInspectionError),   # never guessed from a remote
    ("feature", True, g.GitInspectionError),    # already exists
    ("bad..name", True, ValueError),
    ("-x", False, ValueError),                  # option injection
])
def test_invalid_targets_fail_before_any_change(repo, branch, create, error):
    with pytest.raises(error):
        g.git_switch(repo, branch, create=create, write=True)
    assert _git(repo, "branch", "--show-current") == "main"


def test_no_previous_branch_is_an_error(repo):
    with pytest.raises(g.GitInspectionError):
        g.git_switch(repo, previous=True)


def test_conflicting_local_change_is_reported_not_forced(repo):
    (repo / "f.txt").write_text("main\n")
    _git(repo, "add", "f.txt")
    _git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "f on main")
    _git(repo, "switch", "-q", "feature")
    (repo / "f.txt").write_text("untracked would be overwritten\n")
    out = g.git_switch(repo, "main", write=True)
    assert (out["status"], out["mutation_status"]) == ("mutation_failed", "failed")
    assert _git(repo, "branch", "--show-current") == "feature"


def test_cli_plans_by_default_and_writes_only_with_flag(repo, capsys):
    assert g.main(["git-switch", "--root", str(repo), "feature"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert _git(repo, "branch", "--show-current") == "main"
    assert g.main(["git-switch", "--root", str(repo), "-c", "made", "--write"]) == 0
    assert _git(repo, "branch", "--show-current") == "made"
    assert g.main(["git-switch", "--root", str(repo), "-", "--write"]) == 0
    assert _git(repo, "branch", "--show-current") == "main"
    assert g.main(["git-switch", "--root", str(repo), "feature", "-c", "x"]) == 2


def test_previous_detached_position_is_refused(repo):
    _git(repo, "switch", "-q", "--detach")
    _git(repo, "switch", "-q", "main")
    with pytest.raises(g.GitInspectionError, match="not a branch"):
        g.git_switch(repo, previous=True, write=True)
    assert _git(repo, "branch", "--show-current") == "main"
