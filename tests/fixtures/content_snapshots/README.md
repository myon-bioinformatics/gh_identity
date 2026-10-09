# Recorded public GitHub responses

Captured 2026-10-09 through the GitHub connector's REST GET facility. Files retain
its full returned JSON text (with a final newline), not only body fields. Endpoint
and exact resource identity are in manifest.json. These are historical snapshots;
Issue and PR descriptions may subsequently change. They do not contain comment
threads, review threads, HTML pages, or a guarantee of complete commit diffs.

Run from the repository root on POSIX with Python 3.10+:

```sh
python scripts/check_content_snapshots.py tests/fixtures/content_snapshots
```

This invokes the real GHI CLI in subprocesses with a temporary recorded `gh api`
executable. No network request occurs during replay. CLI parsing, gh subprocess
transport, exact identity checks, JSON serialization, and full/selected body output
are exercised. Both body strings are compared exactly, including Unicode and
newlines; response and body SHA-256 values are reported. Exit 0 means all matched,
1 means a CLI/output mismatch, and 2 means invalid evidence or runner failure.
The runtime GHI module is not patched.

Initial result: six resources, two CLI modes each, twelve matches, exit 0.
Direct GHI urllib access from the capture environment timed out; this is not
live transport certification. To capture fresh responses in a network-enabled
environment, run `gh api --method GET ENDPOINT > FILE.json` for each manifest
endpoint and rerun this checker. Retain the original directory for comparison.
