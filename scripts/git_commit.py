#!/usr/bin/env python3
"""Stage an explicit file list and commit it, with optional trailers and push.

Wraps ``git add -- FILE...`` + ``git commit`` so a caller commits an exact,
reviewed file list instead of ``git add -A``. Prints ``git status --short``
and ``git diff --cached --stat`` right after staging, so the caller sees
exactly what will be committed, then the new commit's SHA.

Refuses (exit 2) an empty or missing ``--message-file``, and refuses to run
when files other than FILE... are already staged (``--allow-staged``
overrides this check). Each ``--trailer "Key: value"`` (repeatable) is
appended to the message, separated by a blank line; a trailer whose exact
text is already present anywhere in the message is left alone (not
duplicated). ``--push`` runs ``git push origin HEAD`` afterwards (remote and
branch are fixed: the current branch to ``origin``), retrying
only network-looking failures after 2s/4s/8s/16s; it never forces.

Stdlib only; runs under ``python -S``::

    python -S scripts/git_commit.py --message-file /tmp/msg.txt src/app.py
    python -S scripts/git_commit.py --message-file msg.txt a.py b.py \
        --trailer "Reviewed-by: Jane Doe <jane@example.com>" --push

Exit codes: 0 = committed (and pushed, if asked); 1 = a git command failed
(add/commit/push); 2 = usage error (missing/empty message, unexpected
staged files without --allow-staged).

Provenance: browser-test-kit ``scripts/git_commit.py`` (blob d8272b21) at
``myon-bioinformatics/browser-test-kit`` main 61872b2d (2026-10-17).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

NETWORK_ERROR_HINTS = (
    "could not resolve host", "connection timed out", "connection refused", "network is unreachable",
    "temporary failure in name resolution", "could not read from remote repository",
    "the remote end hung up unexpectedly", "early eof", "rpc failed", "failed to connect",
    "operation timed out", "connection reset", "ssl_read", "no route to host", "socket is not connected",
)

PUSH_RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)


def _run(argv: list[str], run: Runner, **kwargs) -> "subprocess.CompletedProcess[str]":
    return run(argv, capture_output=True, text=True, **kwargs)


def _normalize(path: str) -> str:
    """``./a.py``, ``a.py`` and ``/abs/repo/a.py`` (from the repo root) -> ``a.py``."""
    if os.path.isabs(path):
        path = os.path.relpath(path)
    return Path(os.path.normpath(path)).as_posix()


def staged_files(run: Runner = subprocess.run) -> list[str]:
    done = _run(["git", "diff", "--cached", "--name-only"], run)
    return [line for line in done.stdout.splitlines() if line]


def read_message(path: Path) -> str:
    return path.read_text(encoding="utf-8").rstrip("\n")


def build_message(base: str, trailers: list[str]) -> str:
    """``base`` with any ``trailers`` not already present appended, blank-line separated.

    A trailer is skipped if its exact text is already in ``base``, or already queued
    earlier in this same ``trailers`` list (so repeating a trailer is a no-op)."""
    body = base.rstrip("\n")
    seen = {line.strip() for line in body.splitlines()}
    to_add = []
    for trailer in trailers:
        key = trailer.strip()
        if key in seen:
            continue
        to_add.append(trailer)
        seen.add(key)
    if not to_add:
        return body + "\n"
    return body + "\n\n" + "\n".join(to_add) + "\n"


def _looks_like_network_error(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in NETWORK_ERROR_HINTS)


def push_with_retry(*, delays: tuple[float, ...] = PUSH_RETRY_DELAYS, run: Runner = subprocess.run,
                    sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str]:
    """``git push origin HEAD``, retrying only network-looking failures. Never forces."""
    remaining = list(delays)
    while True:
        done = _run(["git", "push", "origin", "HEAD"], run)
        output = done.stdout + done.stderr
        if done.returncode == 0:
            return True, output
        if not remaining or not _looks_like_network_error(output):
            return False, output
        sleep(remaining.pop(0))


def main(argv: list[str] | None = None, *, run: Runner = subprocess.run,
         sleep: Callable[[float], None] = time.sleep) -> int:
    parser = argparse.ArgumentParser(
        description="Stage an explicit file list and commit it, with optional trailers and push.")
    parser.add_argument("--message-file", required=True, metavar="FILE", help="path to the commit message")
    parser.add_argument("files", nargs="+", metavar="FILE", help="paths to stage and commit")
    parser.add_argument("--trailer", action="append", default=[], metavar="KEY: VALUE",
                        help="trailer line to append if not already present (repeatable)")
    parser.add_argument("--allow-staged", action="store_true",
                        help="allow files other than FILE... to already be staged")
    parser.add_argument("--push", action="store_true", help="git push origin HEAD after committing")
    args = parser.parse_args(argv)

    message_path = Path(args.message_file)
    if not message_path.is_file():
        print(f"error: message file not found: {args.message_file}", file=sys.stderr)
        return 2
    base_message = read_message(message_path)
    if not base_message.strip():
        print(f"error: message file is empty: {args.message_file}", file=sys.stderr)
        return 2

    if not args.allow_staged:
        # Compare repo-relative POSIX paths so ./a.py, a.py and an absolute path all match.
        wanted = {_normalize(path) for path in args.files}
        unexpected = sorted(path for path in staged_files(run=run) if _normalize(path) not in wanted)
        if unexpected:
            print("error: files other than FILE... are already staged (use --allow-staged to proceed anyway):",
                 file=sys.stderr)
            for path in unexpected:
                print(f"  {path}", file=sys.stderr)
            return 2

    added = _run(["git", "add", "--", *args.files], run)
    if added.returncode != 0:
        sys.stderr.write(added.stdout)
        sys.stderr.write(added.stderr)
        return 1

    print(_run(["git", "status", "--short"], run).stdout, end="")
    print(_run(["git", "diff", "--cached", "--stat"], run).stdout, end="")

    message = build_message(base_message, args.trailer)
    commit = _run(["git", "commit", "--file", "-"], run, input=message)
    if commit.returncode != 0:
        sys.stderr.write(commit.stdout)
        sys.stderr.write(commit.stderr)
        return 1

    sha = _run(["git", "rev-parse", "HEAD"], run).stdout.strip()
    print(sha)

    if args.push:
        ok, output = push_with_retry(run=run, sleep=sleep)
        (sys.stdout if ok else sys.stderr).write(output)
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
