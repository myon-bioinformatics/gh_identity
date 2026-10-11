"""Repository metadata: checkout identity (v2) and the Ironmate v1 adapter."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import gh_identity as g

GENERATED = "2026-10-11T10:05:00Z"
REPO = "example/demo"


def _git(root, *args, env=None):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    env = dict(os.environ, GIT_COMMITTER_DATE="2026-10-11T19:00:00+09:00",
               GIT_AUTHOR_DATE="2026-10-11T19:00:00+09:00")
    _git(tmp_path, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty",
         "-m", "メタデータ: 件名 «quotes» & emoji 🚀", "-m", "body line is not the subject", env=env)
    return tmp_path


def test_v2_records_checkout_head_and_keeps_source_separate(repo):
    sha = _git(repo, "rev-parse", "HEAD")
    record = g.repository_metadata(repo, repository=REPO, source_sha="B" * 40, generated_at=GENERATED)
    assert record == {
        "schema_version": "2.0", "repository": {"full_name": REPO},
        "head": {"sha": sha, "branch": "main", "detached": False,
                 "timestamp": "2026-10-11T19:00:00+09:00",
                 "subject": "メタデータ: 件名 «quotes» & emoji 🚀"},
        "source": {"sha": "b" * 40}, "generated_at": GENERATED}


def test_checkout_head_ignores_github_sha(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    assert g.checkout_head(repo)["sha"] == _git(repo, "rev-parse", "HEAD")


def test_detached_head_is_null_branch_in_v2_and_detached_in_v1(repo):
    _git(repo, "checkout", "-q", "--detach")
    assert g.checkout_head(repo)["branch"] is None and g.checkout_head(repo)["detached"] is True
    v1 = g.repository_metadata_v1(repo, repository=REPO, env={}, generated_at=GENERATED)
    assert v1["head"]["branch"] == "detached"


@pytest.mark.parametrize("env,branch", [
    ({"GITHUB_HEAD_REF": "feature/x", "GITHUB_REF_NAME": "12/merge"}, "feature/x"),
    ({"GITHUB_REF_NAME": "release/1"}, "release/1"),
    ({}, "main"),
])
def test_v1_branch_label_rule_matches_ironmate(repo, env, branch):
    assert g.repository_metadata_v1(repo, repository=REPO, env=env, generated_at=GENERATED)["head"]["branch"] == branch


def test_v1_shape_and_serialization(repo, tmp_path):
    record = g.repository_metadata_v1(repo, repository=REPO, env={}, generated_at=GENERATED,
                                      tooling={"python": "3.12.3"}, measurements={"working_tree_bytes": 0})
    assert record["head"]["short_sha"] == record["head"]["sha"][:8]
    assert record["measurements"] == {"github_reported_size_bytes": None, "working_tree_bytes": 0,
                                      "release_artifact_bytes": None}  # measured 0 is not null
    out = tmp_path / "out"
    g.write_repository_metadata(record, out)
    pretty = (out / "repository-metadata.json").read_text(encoding="utf-8")
    line = (out / "repository-metadata.jsonl").read_text(encoding="utf-8")
    assert json.loads(pretty) == json.loads(line) == record
    assert line.count("\n") == 1 and "🚀" in line  # UTF-8, not \\u escapes
    assert str(repo) not in pretty  # no local absolute path


@pytest.mark.parametrize("kwargs,message", [
    ({"repository": "not-a-repo"}, "owner/name"),
    ({"repository": "a/b/c"}, "owner/name"),
    ({"repository": REPO, "source_sha": "abc"}, "source_sha"),
    ({"repository": REPO, "expected_sha": "g" * 40}, "expected_sha"),
    ({"repository": REPO, "generated_at": "2026-10-11T10:00:00"}, "timezone"),
    ({"repository": REPO, "generated_at": "yesterday"}, "ISO 8601"),
])
def test_invalid_inputs_are_rejected(repo, kwargs, message):
    with pytest.raises(ValueError, match=message):
        g.repository_metadata(repo, **kwargs)


def test_expected_sha_mismatch_fails_closed_before_writing(repo, tmp_path):
    out = tmp_path / "out"
    result = subprocess.run([sys.executable, "-S", g.__file__, "repo-metadata", "--root", str(repo),
                             "--repository", REPO, "--format", "v1", "--expected-sha", "f" * 40,
                             "--output-dir", str(out)], capture_output=True, text=True)
    assert result.returncode == 2 and not out.exists()
    sha = _git(repo, "rev-parse", "HEAD")
    ok = subprocess.run([sys.executable, "-S", g.__file__, "repo-metadata", "--root", str(repo),
                         "--repository", REPO, "--format", "v1", "--expected-sha", sha.upper(),
                         "--output-dir", str(out)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert json.loads((out / "repository-metadata.json").read_text(encoding="utf-8"))["head"]["sha"] == sha


def test_v1_validator_rejects_contract_breaks(repo):
    good = g.repository_metadata_v1(repo, repository=REPO, env={}, generated_at=GENERATED)
    for mutate in (lambda r: r["head"].update(short_sha="00000000"),
                   lambda r: r["head"].update(sha=r["head"]["sha"].upper()),
                   lambda r: r.update(extra=1),
                   lambda r: r["measurements"].update(working_tree_bytes=-1),
                   lambda r: r["measurements"].update(working_tree_bytes=True),
                   lambda r: r["tooling"].update(python="3.12; rm -rf /")):
        broken = json.loads(json.dumps(good))
        mutate(broken)
        with pytest.raises(ValueError):
            g.validate_repository_metadata_v1(broken)


def test_tool_versions_is_explicit_and_allowlisted():
    assert g.tool_versions() == {}
    with pytest.raises(ValueError):
        g.tool_versions(["bash"])
    assert set(g.tool_versions(["git"], include_python=True)) <= {"git", "python"}


def test_not_a_git_checkout_is_an_error_not_a_guess(tmp_path):
    with pytest.raises(g.GitInspectionError):
        g.checkout_head(tmp_path)


@pytest.mark.skipif(not os.environ.get("IRONMATE_GENERATOR_DIR"),
                    reason="set IRONMATE_GENERATOR_DIR to the vendored Ironmate generator to compare")
def test_v1_adapter_matches_ironmate_on_the_same_checkout(repo):
    sys.path.insert(0, os.environ["IRONMATE_GENERATOR_DIR"])
    from repository_metadata_generator import record_from_checkout
    for detach in (False, True):
        if detach:
            _git(repo, "checkout", "-q", "--detach")
        for env in ({}, {"GITHUB_HEAD_REF": "feature/x"}, {"GITHUB_REF_NAME": "main"}):
            old = record_from_checkout(repo, REPO, env=env, tooling={"python": "3.12.3"})
            new = g.repository_metadata_v1(repo, repository=REPO, env=env, tooling={"python": "3.12.3"},
                                           generated_at=old["generated_at"])
            assert new == old, (detach, env)
