# Repository metadata: minimal v2 and the v1 compatibility adapter

Status: implemented in `gh_identity.py` (`checkout_head`, `repository_metadata`,
`repository_metadata_v1`, `validate_repository_metadata_v1`, `tool_versions`,
`write_repository_metadata`) and the `repo-metadata` CLI. Stdlib only.

## What moved here and what did not

| Concern | Where it lives |
| --- | --- |
| Repository identity, checkout HEAD SHA, branch, commit time and subject | gh_identity (`checkout_head`, `repository_metadata`) |
| Source commit (PR head) as a separate, caller-stated field | gh_identity (`source.sha`) |
| v1 JSON shape that existing Flutter code and E2E read | gh_identity (`repository_metadata_v1`, compatibility only) |
| Tool versions (python, git, gh, node, npm, npx) | Optional: `tool_versions()` / `--tool`, used only for v1 |
| GitHub-reported size, working-tree size, artifact size | Caller-supplied (`measurements=`), v1 only |
| App version, per-screen sizes, routes (`build_metadata.json`) | Not here; the app's own build step |

## v2 (minimal)

```json
{
  "schema_version": "2.0",
  "repository": {"full_name": "myon-bioinformatics/flutter_navigation_basic"},
  "head": {
    "sha": "<40 or 64 lowercase hex>",
    "branch": "main",
    "detached": false,
    "timestamp": "2026-10-11T19:00:00+09:00",
    "subject": "Update repository metadata"
  },
  "source": {"sha": null},
  "generated_at": "2026-10-11T10:05:00Z"
}
```

- `head` is the **checkout identity**: what `git rev-parse HEAD` says at `root`.
  Environment variables never replace it (on `pull_request`, `GITHUB_SHA` is a
  temporary merge commit).
- `head.branch` is the current branch or `null`; `head.detached` makes the
  difference explicit. No branch name is guessed from the environment in v2.
- `source.sha` is the **source identity** the caller states (for example
  `github.event.pull_request.head.sha`). `null` means "not stated", not "same as head".
- `head.timestamp` is the committer date (`%cI`), `head.subject` the first line of `%s`.
- `short_sha` is not stored; it is `sha[:8]`.
- `generated_at` is the only value that changes between identical checkouts;
  pass `generated_at=` (`--generated-at`) to compare records byte for byte.

## v1 compatibility

`repository_metadata_v1()` produces the Ironmate `repository-metadata` schema
`1.0` record, validated with the same field rules as
`repository_metadata_contract.py`:

- same keys; `short_sha = sha[:8]`; lowercase full SHA; ISO 8601 times with zone;
- `branch` keeps v1's label rule: `GITHUB_HEAD_REF`, then `GITHUB_REF_NAME`, then
  the checkout branch, else `"detached"` (the SHA is still the checkout HEAD);
- `measurements` always has the three keys; a measured `0` stays `0`, unmeasured is `null`;
- `tooling` contains only what the caller supplied or explicitly requested.

Verified against the vendored Ironmate generator
(`flutter_navigation_basic/tool/python/vendor/repository_metadata_generator.py`):
identical records for a branch checkout and a detached checkout, each with no
env, `GITHUB_HEAD_REF` and `GITHUB_REF_NAME` (`IRONMATE_GENERATOR_DIR=... pytest
tests/test_gh_identity_repository_metadata.py`), and on the real Flutter checkout.

## Flutter migration (not done yet)

1. Vendor this `gh_identity.py` revision (already vendored there for other APIs).
2. In `tool/python/generate_repository_metadata.py`, replace
   `record_from_checkout(...)` + `write_metadata(...)` with
   `repository_metadata_v1(root, repository=..., expected_sha=..., tooling=tool_versions(("git","gh","node","npm","npx"), include_python=True))`
   + `write_repository_metadata(record, output_dir)`. Output bytes are the same
   apart from `generated_at`.
3. Keep `tool/src/repository_metadata.dart`, the diagnostics E2E and Pages
   `--expected-sha` unchanged; run them on that head.
4. Only after that is green, drop the two Ironmate files and their lock entries.
5. Moving Flutter to v2 is a separate step (the Dart reader requires `1.0` and `short_sha`).
