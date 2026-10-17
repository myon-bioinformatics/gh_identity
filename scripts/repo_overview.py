#!/usr/bin/env python3
"""One-command repository orientation: about, key files, then the file list.

Prints an ``=== about ===`` section (package.json/pyproject.toml
description plus the README's opening paragraph) when any of those are
found, then the README, agent notes (CLAUDE.md / AGENTS.md), and build/test
configuration (package.json, pyproject.toml, requirements*.txt,
playwright.config.*, Dockerfile, compose files, GitHub workflows, ...) under
``=== path ===`` headers, then every file path. Each file is capped at
``--max-lines`` and the list at ``--max-files``, so the output stays usable in
a log or an agent context (``UNBOUNDED_TOOL_OUTPUT``). The file list comes
from ``git ls-files`` (tracked plus untracked-but-not-ignored) when ROOT is a
git work tree, else from a directory walk; dependency, cache, and result
directories are skipped either way.

``--stats`` adds an ``=== stats ===`` section: totals and per-extension
file/line/byte counts (top 15 by lines). ``--churn N`` adds an
``=== churn ===`` section: the top N files by commit count from ``git log``
(git work trees only; a plain directory prints a one-line note instead),
optionally limited to commits after some point in time with ``--since``.
Commit count is only an approximation of "important, recently touched"
files: a file edited in many tiny commits ranks above one rewritten once.

Stdlib only; runs under ``python -S``::

    python -S scripts/repo_overview.py
    python -S scripts/repo_overview.py ../other-repo --max-lines 40 --no-files
    python -S scripts/repo_overview.py --include 'docs/*.md'
    python -S scripts/repo_overview.py --stats --churn 10 --since '3 months ago'

Exit codes: 0 = OK, 2 = ROOT is not a directory.

Provenance: browser-test-kit ``scripts/repo_overview.py`` (blob 08cefe25) at
``myon-bioinformatics/browser-test-kit`` main 61872b2d (2026-10-17).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path


def _load_gh_identity():
    """Load gh_identity from the repo root or a vendored sibling (stdlib-only)."""
    here = Path(__file__).resolve()
    for candidate in (here.parents[1] / "gh_identity.py", here.parent / "gh_identity.py"):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("_repo_overview_gh_identity", candidate)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader
            spec.loader.exec_module(module)
            return module
    raise ImportError("gh_identity.py not found next to or above repo_overview.py")


gh_identity = _load_gh_identity()

KEY_FILES = (
    "README*", "CLAUDE.md", "AGENTS.md", "CONTRIBUTING*",
    "package.json", "pyproject.toml", "setup.cfg", "setup.py", "requirements*.txt", "Pipfile", "tox.ini",
    "noxfile.py", "Makefile", "Dockerfile*", "docker-compose*.y*ml", "compose*.y*ml",
    "playwright.config.*", "tsconfig.json", "vite.config.*", "go.mod", "Cargo.toml",
    ".tool-versions", ".python-version", ".nvmrc", ".github/workflows/*.y*ml",
)
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
                       ".pytest_cache", ".ruff_cache", "dist", "build", "test-results", "playwright-report"})


def _skipped(relative: str) -> bool:
    return any(part in SKIP_DIRS for part in Path(relative).parts[:-1])


_PYPROJECT_DESCRIPTION_RE = re.compile(r'^\s*description\s*=\s*"([^"]*)"', re.MULTILINE)
_BADGE_RE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\)")
README_MAX_CHARS = 300


def _package_json_description(root: Path) -> str | None:
    path = root / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    description = data.get("description") if isinstance(data, dict) else None
    return description.strip() if isinstance(description, str) and description.strip() else None


def _pyproject_description(root: Path) -> str | None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _PYPROJECT_DESCRIPTION_RE.search(text)
    return match.group(1).strip() if match and match.group(1).strip() else None


def _is_heading(line: str) -> bool:
    """ATX (``# ...``) or setext (``===``/``---`` underline) heading line."""
    stripped = line.lstrip()
    return stripped.startswith("#") or (len(stripped) >= 3 and set(stripped) <= {"=", "-"})


def _readme_paragraphs(text: str) -> list[list[str]]:
    """Group README lines into paragraphs, dropping heading and blank lines as separators."""
    paragraphs: list[list[str]] = [[]]
    for line in text.splitlines():
        if not line.strip() or _is_heading(line):
            if paragraphs[-1]:
                paragraphs.append([])
        else:
            paragraphs[-1].append(line.strip())
    return [paragraph for paragraph in paragraphs if paragraph]


def _readme_paragraph(root: Path, max_chars: int = README_MAX_CHARS) -> str | None:
    text = None
    for path in sorted(root.glob("README*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            continue
        text = data.decode("utf-8", "replace")
        break
    if text is None:
        return None
    for paragraph in _readme_paragraphs(text):
        if all(_BADGE_RE.sub("", line).strip() == "" for line in paragraph):
            continue
        return " ".join(paragraph)[:max_chars]
    return None


def about(root: Path) -> str | None:
    """The ``=== about ===`` section, or ``None`` when nothing is found."""
    lines = []
    package_description = _package_json_description(root)
    if package_description:
        lines.append(f"package.json: {package_description}")
    pyproject_description = _pyproject_description(root)
    if pyproject_description:
        lines.append(f"pyproject.toml: {pyproject_description}")
    readme_paragraph = _readme_paragraph(root)
    if readme_paragraph:
        lines.append(f"README: {readme_paragraph}")
    return "\n".join(["=== about ===", *lines]) if lines else None


def list_files(root: Path) -> list[str]:
    """Repository files relative to ``root``, sorted, without dependency/cache/result directories."""
    try:
        inventory = gh_identity.git_ls_files(root, include_untracked=True)
    except gh_identity.GitInspectionError:
        files = set()
        for directory, subdirs, names in os.walk(root):
            subdirs[:] = [name for name in subdirs if name not in SKIP_DIRS]
            files.update(Path(directory, name).relative_to(root).as_posix() for name in names)
    else:
        if inventory["truncated"]:
            # A walk would include ignored files; never substitute it for a
            # successful but incomplete Git observation or hide partial output.
            raise RuntimeError("repository file inventory exceeds shared inspector limits")
        files = set(inventory["paths"])
    return sorted(path for path in files if not _skipped(path) and (root / path).is_file())


def key_files(root: Path, patterns: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root).as_posix()
            if path.is_file() and relative not in found and not _skipped(relative):
                found.append(relative)
    return found


def render_file(root: Path, relative: str, max_lines: int) -> str:
    data = (root / relative).read_bytes()
    if b"\0" in data[:4096]:
        return f"=== {relative} (binary, {len(data)} bytes; not shown) ==="
    lines = data.decode("utf-8", "replace").splitlines()
    head = f"=== {relative} ({len(lines)} line{'' if len(lines) == 1 else 's'}) ==="
    body = lines[:max_lines]
    if len(lines) > max_lines:
        body.append(f"[... truncated: showing {max_lines} of {len(lines)} lines (--max-lines)]")
    return "\n".join([head, *body])


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _human_bytes(size: int) -> str:
    for unit, threshold in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= threshold:
            return f"{round(size / threshold)} {unit}"
    return f"{size} B"


def _stats_row(label: str, files: int, lines: int, size: int) -> str:
    return f"{label}  {_plural(files, 'file')}  {_plural(lines, 'line')}  {_human_bytes(size)}"


STATS_TOP_EXTENSIONS = 15


def stats(root: Path) -> str:
    """The ``=== stats ===`` section: totals, then the top extensions by lines."""
    per_extension: dict[str, dict[str, int]] = {}
    total_files = total_lines = total_bytes = 0
    for relative in list_files(root):
        try:
            data = (root / relative).read_bytes()
        except OSError:
            continue
        extension = Path(relative).suffix or "(none)"
        entry = per_extension.setdefault(extension, {"files": 0, "lines": 0, "bytes": 0})
        entry["files"] += 1
        entry["bytes"] += len(data)
        total_files += 1
        total_bytes += len(data)
        if b"\0" not in data[:4096]:
            line_count = len(data.decode("utf-8", "replace").splitlines())
            entry["lines"] += line_count
            total_lines += line_count
    ranked = sorted(per_extension.items(), key=lambda kv: (-kv[1]["lines"], kv[0]))
    lines_out = ["=== stats ===", _stats_row("total", total_files, total_lines, total_bytes)]
    for extension, counts in ranked[:STATS_TOP_EXTENSIONS]:
        lines_out.append(_stats_row(extension, counts["files"], counts["lines"], counts["bytes"]))
    if len(ranked) > STATS_TOP_EXTENSIONS:
        lines_out.append(f"[... {len(ranked) - STATS_TOP_EXTENSIONS} more extensions "
                         f"(top {STATS_TOP_EXTENSIONS} shown)]")
    return "\n".join(lines_out)


def _churn_path(path: str) -> str:
    """Keep one display row per path; ordinary/Unicode paths stay readable."""
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return json.dumps(path, ensure_ascii=False)
    return path


def churn(root: Path, top: int, *, since: str | None = None) -> str:
    """The ``=== churn ===`` section, or a one-line note outside a git work tree."""
    try:
        history = gh_identity.git_log_numstat(root, since=since or None)
    except gh_identity.GitInspectionError:
        return "not a git work tree; skipping --churn"
    per_file: dict[str, dict[str, object]] = {}
    total_commits = len(history["commits"])
    for commit in history["commits"]:
        date = commit["date"]
        for file in commit["files"]:
            added, deleted, path = file["added"], file["deleted"], file["path"]
            if added is None or deleted is None:
                continue
            # Attribute a rename to its destination, without merging old-path
            # history: the shared API retains orig_path for other consumers.
            entry = per_file.setdefault(path, {"commits": 0, "added": 0, "deleted": 0, "last": date})
            entry["commits"] += 1
            entry["added"] += added
            entry["deleted"] += deleted
            entry["last"] = max(entry["last"], date)
    ranked = sorted(per_file.items(), key=lambda kv: (-kv[1]["commits"], kv[0]))
    lines_out = [f"=== churn ({_plural(total_commits, 'commit')} scanned) ==="]
    for path, entry in ranked[:top]:
        lines_out.append(f"  {_plural(entry['commits'], 'commit')}  +{entry['added']}/-{entry['deleted']}  "
                         f"{entry['last']}  {_churn_path(path)}")
    if len(ranked) > top:
        lines_out.append(f"[... {len(ranked) - top} more files (--churn {top} shows the top {top})]")
    if history["truncated"]:
        lines_out.append("[... history truncated by shared inspector limits "
                         "(10,000 commits / 1,000,000 bytes); ranking covers complete scanned commits only]")
    return "\n".join(lines_out)


def overview(root: Path, *, max_lines: int = 120, max_files: int = 400, files: bool = True,
             include: tuple[str, ...] = (), show_stats: bool = False, churn_top: int = 0,
             since: str | None = None) -> str:
    sections = []
    about_section = about(root)
    if about_section:
        sections.append(about_section)
    sections += [render_file(root, relative, max_lines) for relative in key_files(root, KEY_FILES + include)]
    if files:
        paths = list_files(root)
        shown = paths[:max_files]
        if len(paths) > max_files:
            shown.append(f"[... {len(paths) - max_files} more files (--max-files)]")
        sections.append("\n".join([f"=== files ({len(paths)}) ===", *shown]))
    if show_stats:
        sections.append(stats(root))
    if churn_top:
        sections.append(churn(root, churn_top, since=since))
    return "\n\n".join(sections)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a repository's key files and file list, bounded.")
    parser.add_argument("root", nargs="?", default=".", help="repository directory (default: .)")
    parser.add_argument("--max-lines", type=_positive, default=120, help="lines shown per file (default: 120)")
    parser.add_argument("--max-files", type=_positive, default=400, help="paths listed (default: 400)")
    parser.add_argument("--no-files", action="store_true", help="skip the file list")
    parser.add_argument("--include", action="append", default=[], metavar="GLOB",
                        help="extra key-file glob relative to ROOT (repeatable)")
    parser.add_argument("--stats", action="store_true", help="add per-extension file/line/byte stats")
    parser.add_argument("--churn", type=_positive, default=0, metavar="N",
                        help="add the top N files by commit count (git work trees only)")
    parser.add_argument("--since", metavar="WHEN",
                        help="limit --churn to commits after WHEN, e.g. '3 months ago' (needs --churn)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2
    print(overview(root.resolve(), max_lines=args.max_lines, max_files=args.max_files, files=not args.no_files,
                   include=tuple(args.include), show_stats=args.stats, churn_top=args.churn, since=args.since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
