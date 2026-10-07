# gh_identity v0.1 implementation contract

## Decision

v0.1 is the first practical release, not an intentionally tiny preview.

Runtime requirement is **Python standard library only**. `gh` and `git` are preferred when available but are not mandatory. Public read operations can fall back to `urllib` against GitHub REST. A token is optional for public reads and required only when the selected GitHub operation/permission requires authentication.

## Transport

```text
public gh_identity API
  ├─ gh transport       preferred
  ├─ urllib REST        fallback / anonymous public read
  └─ Git bridge         local checkout identity; reuse canonical Git semantics
```

One public behavior contract is shared across transports. Transport-specific code must not independently redefine normalization, pagination, digest, provenance, write guards or error semantics.

## Identity

Keep repository, PR number, head/base SHA, comment/review ID, workflow/run ID + attempt, job/check identity, code SHA and data/result SHA distinct. Rebase/history rewrite invalidates old-head observations until reacquired.

## Output and context economy

Prefer structured output. With `gh`, use `--json` / built-in `--jq` rather than parsing human tables or requiring external jq. Long threads are digest-first: IDs, timestamps, author, character count, preview and URL, then selected bodies.

## Errors

Distinguish launcher failure, authentication, permission/rate limit, inaccessible/not-found, timeout, invalid JSON, cancellation, unsupported transport capability, mutation failure and mutation uncertainty. A write that may have succeeded must not be blindly retried.

## Reviews

Keep conversation comments, review submissions, inline review comments and review threads distinct. Preserve explicit Blocking / Should / Non-blocking / Nit labels without inventing severity from arbitrary prose. Severity and merge-gate are separate axes.

## Actions and evidence

Run ID, attempt and executed SHA are first-class. 0 checks is not green. Partial expected matrices are incomplete. JUnit/xprobe integration stores compact failure identity, not huge logs/tracebacks/secrets.

## Comments / inbox

Comment ID is stable command identity. Stable reply markers provide idempotency. Backfill is allowed; reruns must not duplicate replies. Unknown parser input remains undetermined rather than being forced onto a known command.

## Variables

Repository/environment/organization variables are GitHub configuration, not secrets. `gh variable set` or equivalent REST is supported with explicit write and post-write verification.

## Help-first discovery

An agent should not guess that GitHub CLI lacks a feature. Check installed `gh` help/JSON fields, then dedicated command, then `gh api` REST/GraphQL, then declare unsupported. Installed `gh` behavior is the execution truth for that environment.

## Existing assets to absorb

- browser-test-kit `scripts/gh_ops.py`: urllib fallback, pagination, PR/check/comment/review observation, digest, guarded merge, mutation uncertainty.
- Ironmate `github_catalog.py`, `github_comment.py`, `github_pr.py`: repo catalog/search, latest-created vs latest-updated, safe comment write, consumer wrapper.
- myon-bioinformatics `gh_workflow.py`: workflow preflight/dispatch, ref resolution, run receipt, executed-SHA verification.
- flutter_navigation_basic vendored `gh_ops.py` and `actions_latest.py`: portable consumer and Actions observation.
- mcp-toolcall-lab vendored gh_ops/MCP adapter: GHI remains canonical, MCP is an adapter.
- Aoi: comment identity/reply markers, variables, code-vs-results SHA, state concurrency and run receipts; research semantics stay in Aoi.
- existing git_inspector/repository-metadata producer: connect/reuse; do not fork mature local Git semantics.

## CI

```text
py_compile
  → explicit import smoke
  → CLI --help smoke
  → offline tests
  → controlled failure / JUnit / xprobe
  → opt-in live GitHub tests
```

Compile alone cannot catch `from module import nonexistent_name`; import smoke is mandatory.
