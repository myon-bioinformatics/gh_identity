# gh_identity

Single-file, stdlib-only Python interface for portable GitHub identity and operations.

- **No third-party Python runtime dependency**
- Prefers authenticated **`gh`** when available
- Falls back to stdlib **`urllib`** for supported REST operations, including anonymous public reads
- Keeps repository / PR / SHA / review / comment / workflow / run identity explicit
- Bounded, digest-first output for long GitHub threads
- Guarded writes with dry-run/verification and mutation-uncertainty handling
- Designed to absorb the proven GitHub-operation patterns currently spread across browser-test-kit, Ironmate, Flutter, mcp-toolcall-lab and Aoi

## Python compatibility

Python 3.10 and newer is the compatibility target; there is no runtime upper-version gate. CI checks 3.10–3.14 and `3.x` (the latest stable Python 3 available to setup-python), with `check-latest` enabled. The moving job follows new stable releases without adding a hard-coded upper bound. Passing CI verifies the tested versions; it does not guarantee untested future releases.

## Quick start

```bash
python gh_identity.py capabilities
python gh_identity.py repo myon-bioinformatics/gh_identity
python gh_identity.py comments OWNER/REPO 9
python gh_identity.py variable-get OWNER/REPO NAME

# Explicit write
python gh_identity.py variable-set OWNER/REPO NAME VALUE --write
```

Use `--transport gh` or `--transport urllib` to force a transport; default is `auto`.

See [SPEC.md](SPEC.md) for the v0.1 contract.

## Actions step permalinks

```python
import gh_identity as ghi

# Pure URL construction from explicit IDs, without network calls:
url = ghi.step_url("OWNER/REPO", 20, 91, 7, line=1)

# Validate against a complete observation of the exact run attempt:
observation = ghi.jobs("OWNER/REPO", 20, attempt=2)
url = ghi.step_url_from_jobs(observation, 91, 7, line=1)
```

```bash
python gh_identity.py step-url OWNER/REPO 20 91 7 --line 1
```

The observation helper requires a unique matching job, matching run/attempt and an existing step number. It does not infer the step number from list position. The pure builder formats explicitly supplied identities; it does not establish that a job, step or log line exists. A line anchor selects the GitHub UI location, not a log API endpoint. Step links are independent of log availability and bounded log retrieval remains a separate Issue #17 phase.

## Write receipts and CLI exit codes

Comment writes require an operation marker. A successful POST alone does not
produce a verified receipt: the returned positive comment ID is read back and
its repository/issue identity, exact body (including marker), and web URL must
match. Invalid POST identity is `mutation_uncertain`; failed readback is
`verification_failed`; differing readback is `verification_mismatch`. A receipt
retains a known comment ID and successful mutation state even if verification
fails. The implementation never automatically reposts after these outcomes.

For `comment` and `variable-set`, CLI exit 0 means `planned`, `already_exists`,
or `verified`; other receipt states return 1. Argument/transport exceptions
handled by the CLI return 2. Always inspect the receipt: a nonzero exit does
not mean a write did not happen, and must not trigger a blind retry.

This covers existing comment and variable-set operations only. Variable deletion,
workflow dispatch and guarded PR merge remain separate unfinished work in Issue #2.
