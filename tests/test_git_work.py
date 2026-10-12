"""git-work: a work branch from a PR head or remote branch, with its upstream config written directly."""
import subprocess

import pytest
import gh_identity as g


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@e", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clones(tmp_path):
    """A bare 'GitHub' with main, a feature branch and a PR head ref, plus a shallow single-branch clone."""
    seed, bare, work = tmp_path / "seed", tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    for i in range(3):
        _git(seed, "commit", "-q", "--allow-empty", "-m", f"main {i}")
    _git(seed, "switch", "-q", "-c", "feature")
    _git(seed, "commit", "-q", "--allow-empty", "-m", "feature")
    _git(seed, "switch", "-q", "main")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    _git(bare, "update-ref", "refs/pull/7/head", "refs/heads/feature")
    _git(tmp_path, "clone", "-q", "--depth", "1", "--single-branch", "file://" + str(bare), str(work))
    return {"bare": bare, "work": work, "feature": _git(bare, "rev-parse", "feature")}


def test_plan_changes_nothing(clones):
    work = clones["work"]
    out = g.git_work(work, "#7", "work/review-7")
    assert out["status"] == "planned" and out["shallow"] is True
    assert out["commands"][0] == ["git", "fetch", "--no-tags", "--depth", "1", "origin",
                                  "+refs/pull/7/head:refs/remotes/origin/pr/7"]
    assert out["commands"][1] == ["git", "switch", "-c", "work/review-7", "--no-track", "refs/remotes/origin/pr/7"]
    assert _git(work, "branch", "--show-current") == "main"
    assert _git(work, "branch", "--list", "work/review-7") == ""


def test_pr_head_in_shallow_single_branch_clone(clones):
    """`git branch -u origin/X` fails here ('not a branch'); git-work still ends tracked."""
    work = clones["work"]
    out = g.git_work(work, "#7", "work/review-7", write=True)
    assert out["status"] == "verified", out
    assert out["after"] == {"branch": "work/review-7", "head_sha": clones["feature"]}
    assert out["upstream_config"] == {"remote": "origin", "merge": "refs/heads/work/review-7"}
    assert _git(work, "rev-parse", "--is-shallow-repository") == "true"  # not silently deepened
    # A plain push creates the remote branch, after which status shows the tracking pair.
    _git(work, "commit", "-q", "--allow-empty", "-m", "review fix")
    assert out["refspec_added"] == "+refs/heads/work/review-7:refs/remotes/origin/work/review-7"
    _git(work, "push", "-q")  # updates the tracking ref through the added refspec
    assert _git(work, "status", "-sb").splitlines()[0] == "## work/review-7...origin/work/review-7"


def test_remote_branch_source_with_different_upstream(clones):
    work = clones["work"]
    out = g.git_work(work, "feature", "local-feature", upstream="feature", write=True)
    assert out["status"] == "verified" and out["source"] == {"kind": "branch", "ref": "refs/heads/feature"}
    assert out["tracking_ref"] == "refs/remotes/origin/feature"
    assert out["upstream_config"]["merge"] == "refs/heads/feature"
    assert _git(work, "rev-parse", "--abbrev-ref", "@{u}") == "origin/feature"


def test_full_clone_refspec_is_not_duplicated(tmp_path, clones):
    full = tmp_path / "full"
    _git(tmp_path, "clone", "-q", "file://" + str(clones["bare"]), str(full))
    out = g.git_work(full, "#7", "w", write=True)
    assert out["status"] == "verified" and out["refspec_added"] is None
    assert _git(full, "config", "--get-all", "remote.origin.fetch") == "+refs/heads/*:refs/remotes/origin/*"


@pytest.mark.parametrize(("source", "branch", "kwargs", "error"), [
    ("#7", "main", {}, g.GitInspectionError),              # existing branch is refused, never reset
    ("#7", "bad..name", {}, ValueError),
    ("-x", "w", {}, ValueError),                            # option injection
    ("#7", "w", {"remote": "nowhere"}, g.GitInspectionError),
    ("#7", "w", {"remote": "a b"}, ValueError),
    ("#7", "w", {"depth": 0}, ValueError),
    (None, "w", {}, ValueError),
])
def test_invalid_input_fails_before_any_change(clones, source, branch, kwargs, error):
    work = clones["work"]
    with pytest.raises(error):
        g.git_work(work, source, branch, write=True, **kwargs)
    assert _git(work, "branch", "--show-current") == "main"
    assert _git(work, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/pr") == ""


def test_missing_pr_is_reported_not_raised(clones):
    out = g.git_work(clones["work"], "#999", "w", write=True)
    assert (out["status"], out["mutation_status"], out["completed_steps"]) == ("mutation_failed", "failed", [])
    assert _git(clones["work"], "branch", "--show-current") == "main"


def test_cli_plan_and_exit_codes(clones, capsys):
    assert g.main(["git-work", "--root", str(clones["work"]), "pr/7", "w"]) == 0
    assert '"status": "planned"' in capsys.readouterr().out
    assert g.main(["git-work", "--root", str(clones["work"]), "#7", "main"]) == 2


def test_fetch_never_prompts_for_credentials(clones, monkeypatch):
    """A remote that would ask for a password fails fast as mutation_failed, nothing switched."""
    work = clones["work"]
    _git(work, "remote", "set-url", "origin", "https://example.invalid/private.git")
    seen = {}
    real = g._git_spawn

    def spy(command, **kwargs):
        if "fetch" in command:
            seen.update(kwargs["env"])
        return real(command, **kwargs)
    monkeypatch.setattr(g, "_git_spawn", spy)
    out = g.git_work(work, "#7", "w", write=True, fetch_timeout=20)
    assert seen["GIT_TERMINAL_PROMPT"] == "0" and "BatchMode=yes" in seen["GIT_SSH_COMMAND"]
    assert (out["status"], out["completed_steps"]) == ("mutation_failed", [])
    assert _git(work, "branch", "--show-current") == "main"


def test_stalled_fetch_is_killed_at_the_timeout(clones, tmp_path, monkeypatch):
    """A transfer that never finishes is bounded: the fetch is killed and reported, not awaited."""
    import time
    hang = tmp_path / "hang-upload-pack"
    hang.write_text("#!/bin/sh\nsleep 30\n")
    hang.chmod(0o755)
    work = clones["work"]
    _git(work, "config", "remote.origin.uploadpack", str(hang))
    _git(work, "remote", "set-url", "origin", str(clones["bare"]))  # local transport honours uploadpack
    start = time.monotonic()
    out = g.git_work(work, "#7", "w", write=True, fetch_timeout=1)
    assert time.monotonic() - start < 10
    assert out["status"] == "mutation_failed" and "timed out" in out["error"]
    assert _git(work, "branch", "--show-current") == "main"


def test_fetch_timeout_must_be_positive(clones):
    with pytest.raises(ValueError):
        g.git_work(clones["work"], "#7", "w", fetch_timeout=0)
