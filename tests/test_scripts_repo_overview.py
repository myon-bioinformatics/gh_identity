from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "repo_overview.py"
spec = importlib.util.spec_from_file_location("repo_overview", SCRIPT)
repo_overview = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(repo_overview)


def make_tree(root: Path) -> None:
    files = {
        "README.md": "# demo\nline 2\nline 3\n",
        "package.json": '{"private": true}\n',
        "requirements-test.txt": "pytest\n",
        ".github/workflows/ci.yml": "name: ci\n",
        "src/app.py": "print('hi')\n",
        "node_modules/pkg/index.js": "junk\n",
        "test-results/run/evidence.json": "{}\n",
        "src/__pycache__/app.cpython-312.pyc": "junk\n",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "Dockerfile.bin").write_bytes(b"\x00\x01binary")


def git_env(**overrides: str) -> dict:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
              GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
              GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
    env.update(overrides)
    return env


def commit_file(root: Path, relative: str, content, date: str) -> None:
    """Write ``relative`` (str or bytes) and commit it alone, at a fixed author/committer date."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content) if isinstance(content, bytes) else path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--", relative], check=True, env=git_env())
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", f"commit {relative}"], check=True,
                   env=git_env(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date))


def test_key_files_then_file_list_without_dependency_or_result_dirs(tmp_path: Path) -> None:
    make_tree(tmp_path)
    out = repo_overview.overview(tmp_path)
    headers = [line for line in out.splitlines() if line.startswith("=== ")]
    assert headers == [
        "=== about ===",
        "=== README.md (3 lines) ===",
        "=== package.json (1 line) ===",
        "=== requirements-test.txt (1 line) ===",
        "=== Dockerfile.bin (binary, 8 bytes; not shown) ===",
        "=== .github/workflows/ci.yml (1 line) ===",
        "=== files (6) ===",
    ]
    listed = out.split("=== files (6) ===\n", 1)[1].splitlines()
    assert listed == [".github/workflows/ci.yml", "Dockerfile.bin", "README.md", "package.json",
                      "requirements-test.txt", "src/app.py"]


def test_caps_lines_and_files(tmp_path: Path) -> None:
    make_tree(tmp_path)
    out = repo_overview.overview(tmp_path, max_lines=1, max_files=2, include=("src/*.py",))
    assert "=== README.md (3 lines) ===\n# demo\n[... truncated: showing 1 of 3 lines (--max-lines)]" in out
    assert "=== src/app.py (1 line) ===" in out
    assert out.endswith("=== files (6) ===\n.github/workflows/ci.yml\nDockerfile.bin\n[... 4 more files (--max-files)]")


def test_git_work_tree_uses_ls_files_and_respects_gitignore(tmp_path: Path) -> None:
    make_tree(tmp_path)
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("noise\n", encoding="utf-8")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=env)
    paths = repo_overview.list_files(tmp_path)
    assert "debug.log" not in paths and ".gitignore" in paths and "src/app.py" in paths
    assert not any(path.startswith(("node_modules/", "test-results/")) for path in paths)


def test_cli_on_this_repository_runs_without_site_packages() -> None:
    done = subprocess.run([sys.executable, "-S", str(SCRIPT), str(ROOT), "--max-lines", "3", "--max-files", "5"],
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("=== about ===\n")
    assert "=== README.md (" in done.stdout
    assert "=== .github/workflows/ci.yml (" in done.stdout and "=== files (" in done.stdout
    missing = subprocess.run([sys.executable, "-S", str(SCRIPT), str(ROOT / "nope")], capture_output=True, text=True)
    assert missing.returncode == 2


def test_shared_combined_inventory_matches_previous_git_contract(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    tracked = ["z tracked.txt", "日本語/記録.txt", "tab\tname.txt", "line\nname.txt", "build/tracked.txt"]
    untracked = ["a untracked.txt", "日本語/未追跡.txt", "tab\tuntracked.txt", "line\nuntracked.txt", "node_modules/untracked.txt"]
    for name in tracked + untracked + ["ignored.log", "cache/ignored.txt"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\ncache/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", *tracked], check=True, env=git_env())
    old = subprocess.run(["git", "-C", str(tmp_path), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         check=True, capture_output=True, env=git_env()).stdout.decode("utf-8").split("\0")
    expected = sorted(set(tracked[:-1] + untracked[:-1] + [".gitignore"]))
    assert sorted({p for p in old if p and not repo_overview._skipped(p)}) == expected
    assert repo_overview.list_files(tmp_path) == expected


def test_non_git_walk_fallback_is_sorted_and_skips_dirs(tmp_path: Path) -> None:
    make_tree(tmp_path)
    (tmp_path / "日本語.txt").write_text("fixture", encoding="utf-8")
    # Without Git, .gitignore is an ordinary file; walk never implements Git's
    # ignore rules. Preserve this existing fallback contract.
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("fixture", encoding="utf-8")
    assert repo_overview.list_files(tmp_path) == sorted([
        ".github/workflows/ci.yml", ".gitignore", "Dockerfile.bin", "README.md",
        "debug.log", "package.json", "requirements-test.txt", "src/app.py", "日本語.txt",
    ])


def test_missing_git_keeps_walk_fallback(tmp_path: Path, monkeypatch) -> None:
    make_tree(tmp_path)
    # Exercise the public observation with Git genuinely unavailable, without
    # depending on the vendored inspector's private process implementation.
    monkeypatch.setenv("PATH", "")
    with pytest.raises(repo_overview.gh_identity.GitInspectionError, match="git executable not found"):
        repo_overview.gh_identity.git_ls_files(tmp_path, include_untracked=True)
    assert "src/app.py" in repo_overview.list_files(tmp_path)
    assert not any(repo_overview._skipped(p) for p in repo_overview.list_files(tmp_path))


def test_shared_inventory_sort_and_truncation_are_not_hidden(tmp_path: Path, monkeypatch) -> None:
    for name in ("a.txt", "z.txt"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    def inventory(root, *, include_untracked):
        assert root == tmp_path and include_untracked is True
        return {"paths": ["z.txt", "a.txt", "z.txt"], "truncated": False}
    monkeypatch.setattr(repo_overview.gh_identity, "git_ls_files", inventory)
    assert repo_overview.list_files(tmp_path) == ["a.txt", "z.txt"]
    monkeypatch.setattr(repo_overview.gh_identity, "git_ls_files",
                        lambda *args, **kwargs: {"paths": ["z.txt"], "truncated": True})
    with pytest.raises(RuntimeError, match="inventory exceeds"):
        repo_overview.list_files(tmp_path)


# --- about --------------------------------------------------------------

def test_about_combines_package_json_pyproject_and_readme(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "demo", "description": "A demo package.  "}\n',
                                           encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\ndescription = "A demo project"\n',
                                             encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# demo\n\n"
        "[![CI](https://img.shields.io/badge/CI-passing-green)](https://example.com/ci)\n\n"
        "This is the real description paragraph spanning\n"
        "two lines of markdown source.\n\n"
        "## More\n\nignored body text\n",
        encoding="utf-8",
    )
    assert repo_overview.about(tmp_path) == (
        "=== about ===\n"
        "package.json: A demo package.\n"
        "pyproject.toml: A demo project\n"
        "README: This is the real description paragraph spanning two lines of markdown source."
    )


def test_about_omitted_when_nothing_found(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    assert repo_overview.about(tmp_path) is None
    assert "=== about ===" not in repo_overview.overview(tmp_path)


def test_about_ignores_package_json_without_description(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"private": true}\n', encoding="utf-8")
    assert repo_overview.about(tmp_path) is None


def test_about_readme_paragraph_truncated_to_300_chars(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(f"# t\n\n{'x' * 400}\n", encoding="utf-8")
    assert repo_overview.about(tmp_path) == "=== about ===\nREADME: " + "x" * 300


# --- stats ----------------------------------------------------------------

def test_stats_totals_and_per_extension_rows_sorted_by_lines(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("hello\nworld\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("license text\n", encoding="utf-8")
    (tmp_path / "img.bin").write_bytes(b"\x00\x01\x02\x03")
    assert repo_overview.stats(tmp_path).splitlines() == [
        "=== stats ===",
        "total  5 files  7 lines  45 B",
        ".py  2 files  4 lines  16 B",
        ".md  1 file  2 lines  12 B",
        "(none)  1 file  1 line  13 B",
        ".bin  1 file  0 lines  4 B",
    ]


def test_stats_truncates_to_top_15_extensions(tmp_path: Path) -> None:
    for i in range(17):
        (tmp_path / f"file{i}.ext{i:02d}").write_text("x\n", encoding="utf-8")
    lines = repo_overview.stats(tmp_path).splitlines()
    assert lines[0] == "=== stats ==="
    extension_rows = lines[2:-1]
    assert len(extension_rows) == 15
    assert extension_rows[0].startswith(".ext00 ") and extension_rows[-1].startswith(".ext14 ")
    assert lines[-1] == "[... 2 more extensions (top 15 shown)]"


# --- churn (git work trees only) ------------------------------------------

def test_churn_counts_commits_added_deleted_and_last_date(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    commit_file(tmp_path, "scripts/a.py", "print(1)\n", "2026-01-01T00:00:00+0000")
    commit_file(tmp_path, "scripts/a.py", "print(1)\nprint(2)\n", "2026-02-01T00:00:00+0000")
    commit_file(tmp_path, "scripts/b.py", "print('b')\n", "2026-03-01T00:00:00+0000")
    assert repo_overview.churn(tmp_path, 5).splitlines() == [
        "=== churn (3 commits scanned) ===",
        "  2 commits  +2/-0  2026-02-01  scripts/a.py",
        "  1 commit  +1/-0  2026-03-01  scripts/b.py",
    ]


def test_churn_skips_binary_numstat(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    commit_file(tmp_path, "scripts/a.py", "print(1)\n", "2026-01-01T00:00:00+0000")
    commit_file(tmp_path, "assets/logo.png", b"\x89PNG\x00\x01", "2026-01-02T00:00:00+0000")
    out = repo_overview.churn(tmp_path, 5)
    assert out.splitlines()[0] == "=== churn (2 commits scanned) ==="
    assert "logo.png" not in out
    assert "scripts/a.py" in out


def test_churn_truncates_to_top_n_and_notes_remainder(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    for i in range(3):
        commit_file(tmp_path, f"f{i}.py", f"x{i}\n", f"2026-01-0{i + 1}T00:00:00+0000")
    lines = repo_overview.churn(tmp_path, 2).splitlines()
    assert lines[0] == "=== churn (3 commits scanned) ==="
    assert len(lines) == 4
    assert lines[-1] == "[... 1 more files (--churn 2 shows the top 2)]"


def test_churn_since_filters_older_commits(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    commit_file(tmp_path, "old.py", "old\n", "2026-01-01T00:00:00+0000")
    commit_file(tmp_path, "new.py", "new\n", "2026-03-01T00:00:00+0000")
    out = repo_overview.churn(tmp_path, 5, since="2026-02-01")
    assert out.splitlines()[0] == "=== churn (1 commit scanned) ==="
    assert "new.py" in out and "old.py" not in out


def test_churn_outside_git_prints_one_line_note(tmp_path: Path) -> None:
    assert repo_overview.churn(tmp_path, 5) == "not a git work tree; skipping --churn"
    assert "=== churn" not in repo_overview.overview(tmp_path, churn_top=5)


def test_cli_stats_and_churn_flags(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env())
    commit_file(tmp_path, "a.py", "x\n", "2026-01-01T00:00:00+0000")
    done = subprocess.run([sys.executable, "-S", str(SCRIPT), str(tmp_path), "--no-files", "--stats",
                          "--churn", "5"], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    assert "=== stats ===" in done.stdout
    assert "=== churn (1 commit scanned) ===" in done.stdout
    assert "a.py" in done.stdout


def make_churn_migration_fixture(root: Path) -> None:
    subprocess.run(['git', 'init', '-q', str(root)], check=True, env=git_env())
    # A normal path is touched twice: count wins over path order.
    commit_file(root, 'z.txt', 'one\n', '2026-01-01T00:00:00+0000')
    commit_file(root, 'z.txt', 'one\ntwo\n', '2026-01-02T00:00:00+0000')
    for name in ['a.txt', '日本語.txt', 'tab\tname.txt', 'line\nname.txt', 'old.txt']:
        commit_file(root, name, 'fixture\n', '2026-02-01T00:00:00+0000')
    subprocess.run(['git', '-C', str(root), 'mv', '--', 'old.txt', 'new.txt'], check=True, env=git_env())
    subprocess.run(['git', '-C', str(root), 'commit', '-q', '-m', 'rename'], check=True,
                   env=git_env(GIT_AUTHOR_DATE='2026-03-01T00:00:00+0000',
                               GIT_COMMITTER_DATE='2026-03-01T00:00:00+0000'))
    commit_file(root, 'binary.bin', b'\x00binary', '2026-03-02T00:00:00+0000')
    subprocess.run(['git', '-C', str(root), 'commit', '--allow-empty', '-q', '-m', 'empty'], check=True,
                   env=git_env(GIT_AUTHOR_DATE='2026-03-03T00:00:00+0000',
                               GIT_COMMITTER_DATE='2026-03-03T00:00:00+0000'))


def test_churn_before_after_migration_fixture(tmp_path: Path) -> None:
    make_churn_migration_fixture(tmp_path)
    snapshots = json.loads((ROOT / 'tests/fixtures/churn_migration.json').read_text(encoding='utf-8'))
    for case in snapshots['cases']:
        after = repo_overview.churn(tmp_path, 20, since=case['since'])
        assert after == case['after']
        # Complete commit count, binary exclusion and normal rows are unchanged.
        assert after.splitlines()[0] == case['before'].splitlines()[0]
        assert 'binary.bin' not in after and 'binary.bin' not in case['before']
        for row in case['before'].splitlines()[1:]:
            if row.endswith(('  a.txt', '  z.txt', '  old.txt')):
                assert row in after.splitlines()
    observation = repo_overview.gh_identity.git_log_numstat(tmp_path)
    renamed = [f for c in observation['commits'] for f in c['files'] if f['orig_path']]
    assert renamed == [{'path': 'new.txt', 'orig_path': 'old.txt', 'added': 0, 'deleted': 0}]
    binary = [f for c in observation['commits'] for f in c['files'] if f['path'] == 'binary.bin']
    assert binary == [{'path': 'binary.bin', 'orig_path': None, 'added': None, 'deleted': None}]
    assert '  1 commit  +0/-0  2026-03-01  new.txt' in snapshots['cases'][0]['after']
    assert 'old.txt => new.txt' in snapshots['cases'][0]['before']


@pytest.mark.parametrize('limit', ['count', 'byte'])
def test_churn_complete_commits_and_explicit_history_truncation(tmp_path: Path, monkeypatch, limit: str) -> None:
    make_churn_migration_fixture(tmp_path)
    identity = repo_overview.gh_identity
    original = identity.git_log_numstat
    if limit == 'count':
        kwargs = {'max_count': 2}
    else:
        # This boundary includes the newest empty commit and the following
        # binary commit, then cuts the rename destination. Only complete commits
        # may contribute to the scanned count or ranking.
        raw = subprocess.run(
            ['git', '-C', str(tmp_path), 'log', '--no-ext-diff', '--no-textconv', '--no-color',
             '-z', '--numstat', '--format=%x00%H%x00%cs', '--'],
            check=True, capture_output=True, env=git_env(),
        ).stdout
        kwargs = {'max_bytes': raw.index(b'new.txt') + 2}
    bounded = original(tmp_path, **kwargs)
    assert bounded['truncated'] and len(bounded['commits']) == 2
    def limited(root, *, since):
        return original(root, since=since, **kwargs)
    monkeypatch.setattr(identity, 'git_log_numstat', limited)
    out = repo_overview.churn(tmp_path, 20)
    assert out.splitlines() == [
        '=== churn (2 commits scanned) ===',
        '[... history truncated by shared inspector limits (10,000 commits / 1,000,000 bytes); '
        'ranking covers complete scanned commits only]',
    ]


def test_churn_missing_git_keeps_message(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('PATH', '')
    with pytest.raises(repo_overview.gh_identity.GitInspectionError, match='git executable not found'):
        repo_overview.gh_identity.git_log_numstat(tmp_path)
    assert repo_overview.churn(tmp_path, 5) == 'not a git work tree; skipping --churn'


def test_churn_delegates_since_and_keeps_top_note_with_history_note(tmp_path: Path, monkeypatch) -> None:
    def observe(root, *, since):
        assert root == tmp_path and since == '3 months ago'
        return {'commits': [{'date': '2026-01-01', 'files': [
            {'path': path, 'added': 1, 'deleted': 0} for path in ['z.txt', 'a.txt']]}], 'truncated': True}
    monkeypatch.setattr(repo_overview.gh_identity, 'git_log_numstat', observe)
    out = repo_overview.churn(tmp_path, 1, since='3 months ago')
    assert out.splitlines()[1] == '  1 commit  +1/-0  2026-01-01  a.txt'
    assert out.splitlines()[2] == '[... 1 more files (--churn 1 shows the top 1)]'
    assert 'history truncated' in out.splitlines()[3]


def test_about_tolerates_bad_metadata_and_skips_unreadable_readmes(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "README.d").mkdir()
    (tmp_path / "README.bin").write_bytes(b"\0binary")
    (tmp_path / "README.md").write_text("[![b](x)](y)\n\n# Title\n\nReal text.\n", encoding="utf-8")
    assert repo_overview.about(tmp_path) == "=== about ===\nREADME: Real text."
    (tmp_path / "README.md").write_text("![only](badge)\n", encoding="utf-8")
    assert repo_overview.about(tmp_path) is None
    real_read_text = Path.read_text

    def broken(self, *args, **kwargs):
        if self.name == "pyproject.toml":
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", broken)
    assert repo_overview._pyproject_description(tmp_path) is None
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(PermissionError("denied")) if self.name == "README.md"
                        else real_read_bytes(self))
    assert repo_overview._readme_paragraph(tmp_path) is None


def test_symlinks_leaving_root_are_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "README.md").symlink_to(outside / "secret.txt")
    (root / "Makefile").symlink_to(outside / "secret.txt")
    (root / "inside.txt").write_text("ok\n", encoding="utf-8")
    (root / "link-inside.txt").symlink_to(root / "inside.txt")
    text = repo_overview.overview(root, show_stats=True)
    assert "SECRET" not in text and "=== Makefile" not in text
    assert repo_overview.list_files(root) == ["inside.txt", "link-inside.txt"]
    assert repo_overview.about(root) is None


def test_include_glob_must_stay_inside_root(tmp_path: Path, capsys) -> None:
    for bad in ("../*.md", "/etc/*"):
        with pytest.raises(ValueError):
            repo_overview.key_files(tmp_path, (bad,))
        with pytest.raises(SystemExit) as exit_info:
            repo_overview.main([str(tmp_path), "--include", bad])
        assert exit_info.value.code == 2
    assert "--include must stay inside ROOT" in capsys.readouterr().err


def test_since_requires_churn(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        repo_overview.main([str(tmp_path), "--since", "1 week ago"])
    assert exit_info.value.code == 2 and "--since needs --churn" in capsys.readouterr().err


def test_main_in_process_paths(tmp_path: Path, capsys) -> None:
    assert repo_overview.main([str(tmp_path / "missing")]) == 2
    assert "not a directory" in capsys.readouterr().err
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    (tmp_path / "big.txt").write_bytes(b"x" * 2048)
    assert repo_overview.main([str(tmp_path), "--no-files", "--stats", "--include", "Makefile"]) == 0
    out = capsys.readouterr().out
    assert "=== files" not in out and "=== Makefile (1 line) ===" in out and out.count("=== Makefile") == 1
    assert "2 KB" in out
    for bad in ("0", "x"):
        with pytest.raises(SystemExit):
            repo_overview.main([str(tmp_path), "--max-lines", bad])
    with pytest.raises(argparse.ArgumentTypeError):
        repo_overview._positive("0")


def test_human_bytes_units() -> None:
    assert repo_overview._human_bytes(5) == "5 B"
    assert repo_overview._human_bytes(3 * 1024 ** 2) == "3 MB"
    assert repo_overview._human_bytes(2 * 1024 ** 3) == "2 GB"


def test_stats_skips_unreadable_files(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("1\n2\n", encoding="utf-8")
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(PermissionError("denied")) if self.name == "b.txt"
                        else real_read_bytes(self))
    assert repo_overview.stats(tmp_path).splitlines()[1] == "total  1 file  1 line  2 B"


def test_positive_accepts_valid_values() -> None:
    assert repo_overview._positive("3") == 3


def _load_copy(directory: Path):
    copy = directory / "scripts" / "repo_overview.py"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(SCRIPT.read_bytes())
    copy_spec = importlib.util.spec_from_file_location("repo_overview_copy", copy)
    module = importlib.util.module_from_spec(copy_spec)
    assert copy_spec.loader
    copy_spec.loader.exec_module(module)
    return module


def test_loads_vendored_sibling_or_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ImportError, match="gh_identity.py not found"):
        _load_copy(tmp_path / "none")
    sibling = tmp_path / "vendored"
    (sibling / "scripts").mkdir(parents=True)
    (sibling / "scripts" / "gh_identity.py").write_bytes((ROOT / "gh_identity.py").read_bytes())
    assert _load_copy(sibling).gh_identity.__file__ == str(sibling / "scripts" / "gh_identity.py")
