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

### Bounded job-log contract

The public Python entry point is:

```python
job_log(r, run_id, job_id, *, attempt, max_bytes=1_000_000,
        redact=(), transport="auto", timeout=30)
```

The CLI entry point is:

```text
job-log OWNER/REPO RUN_ID JOB_ID --attempt N
    [--log-bytes N] [--redact-env NAME ...]
    [--transport auto|gh|urllib] [--timeout SECONDS]
    [--max-pages N] [--max-items N] [--max-bytes N]
```

Repeated redaction options each supply one environment-variable name, for example
`--redact-env FIRST --redact-env SECOND`. The variable's value is a literal secret
to redact, not a regex. An unset or empty variable is invalid. Secret values must
not be supplied as CLI arguments. Python `redact` supplies a list or tuple of up
to 100 nonempty literal strings totaling at most 65,536 UTF-8 bytes. Run/job IDs
and the explicit attempt are positive identifiers; booleans are rejected. Local
byte limits are positive integers; timeouts are
positive finite numbers. Invalid arguments fail before network I/O.

#### Identity and scope

1. Read the exact run attempt with `run(r, run_id, attempt=attempt)` and obtain its
   full executed head SHA.
2. Read `jobs(r, run_id, attempt=attempt)` under the same operation budget. Require
   a complete observation, a unique requested job, matching repository/run/attempt
   and matching full head SHA. Job rows retain `head_sha` for this comparison.
3. Require the selected job to be completed before fetching its log. A failed,
   cancelled or skipped conclusion is not itself an identity failure.
4. Build links only for the observed job's step numbers using
   `step_url_from_jobs()` and `step_url()`. Do not infer step numbers from list
   position or equate a UI anchor with a downloadable log endpoint.
5. Download only `repos/{r}/actions/jobs/{job_id}/logs`. Do not combine logs across
   attempts/jobs, silently switch to the latest rerun, or fetch whole-run archives.

The result is one job's text observation. `complete` refers to bounded transfer
completion, not test success, a complete workflow matrix, or a transactional
GitHub snapshot. Step links establish observed step identity; they do not prove
that text or a particular log line exists. Per-step text parsing/retrieval is
outside this contract.

#### Result schema

The output schema is `gh-identity-job-log/1`.

| Field | Contract |
| --- | --- |
| `repository`, `run_id`, `job_id`, `attempt`, `head_sha` | Exact validated GitHub identity, including executed full SHA. |
| `observed_at` | UTC observation timestamp. |
| `step_urls` | List of `{number, url}` links built from validated observed steps. |
| `complete`, `truncated` | Complete body: `true`, `false`. Local log cap exceeded: `false`, `true`. |
| `text` | Complete normalized/redacted Unicode text, or `null` for a local-cap result. Empty complete text remains an empty string. |
| `text_sha256` | Digest of returned sanitized text encoded as UTF-8, or `null` when text is withheld. Never hash raw credentials as evidence. |
| `bytes_read` | Raw log bytes observed, including at most one probe byte beyond the local cap. May be zero when Content-Length already exceeds the cap; not a claimed total remote size. |
| `limit_bytes` | Requested local raw-body cap. |
| `redirects` | Count of validated followed redirects; does not expose redirect URLs. |
| `transport` | `urllib`, the body transport, including when metadata/authentication use `gh`. |
| `credential_source` | `environment`, `gh`, or `anonymous`; no token/account-secret value. |
| `redaction` | Boolean `applied` means sanitization ran. Integer `replacements` counts replacement operations, not distinct secrets; it can be zero. A withheld body uses `false` and `0`. |
| `normalization` | Boolean `bom_removed` and `ansi_removed`, describing text normalization. Both are `false` for a withheld body. |

An intentionally capped observation withholds all body text and its digest.
Returning a sanitized-looking prefix is insufficient: truncation could split a
token, Authorization field, mask declaration or private-key block before the
redactor can recognize it. No raw/prefix field or raw-log digest is included.

#### Transport and authentication

Metadata continues to use the existing gh-first/urllib-fallback behavior. Body
retrieval uses the private stdlib worker for one redirect/stream/error contract;
it does not parse `gh run view --log` output or inherit CLI-version formatting.
The chosen metadata transport and the reported body transport are different
concepts.

For body authorization, prefer `GH_TOKEN`, then `GITHUB_TOKEN`. If neither is set,
`auto`/`gh` may read the existing GitHub CLI credential with the bounded
`gh auth token --hostname github.com` command. `auto` permits an anonymous request
when the CLI is missing or unauthenticated; explicit `gh` preserves that failure.
Cancellation and other CLI failures remain errors instead of anonymous fallbacks.
Explicit `urllib` does not invoke `gh`. Credential output is bounded, kept in
memory, and supplied to the worker through stdin. Credentials and signed URLs
must not enter command-line arguments, error messages or provenance.

The initial GitHub API request may receive Authorization. Download redirects
never receive that header. Redirects are handled manually with a maximum of
three followed redirects. Only HTTPS subdomains of `actions.githubusercontent.com`
or `blob.core.windows.net` are allowed download destinations. Invalid/missing
locations, unsupported destinations and excess redirects are distinguishable
errors, not opportunities to retry through a less restricted client.

GitHub documents this endpoint as an expiring redirect to plain-text job logs,
with unauthenticated access for public resources. Fine-grained credentials for
private repositories need Actions read permission; classic PATs/OAuth credentials
need repository access with `repo` scope. See
[GitHub workflow jobs REST](https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run)
and [GitHub CLI auth token](https://cli.github.com/manual/gh_auth_token).

#### Byte/time limits and failure semantics

The local `max_bytes`/`--log-bytes` cap applies to raw downloaded log bytes before
decoding or normalization. The reader may probe one extra byte to distinguish an
exactly-at-limit complete body from an oversized body. A Content-Length that
already exceeds the cap stops reading immediately with `bytes_read=0`. Metadata,
credential lookup, redirects and log transfer remain inside the existing cumulative
operation deadline/byte budget. The public `timeout` does not restart for each
request. Existing byte accounting also includes worker stdout/stderr; do not
assume the global byte budget equals the body cap. A local-cap observation does
not override a failed global budget.

| Condition | API/CLI behavior |
| --- | --- |
| Complete validated body | Sanitized result; exit `0`. |
| Local log cap exceeded | `complete=false`, `truncated=true`, `text=null`, `text_sha256=null`; exit `1`. |
| Cumulative byte budget exceeded | `Error("bytes_limit")`; exit `2`. |
| Operation deadline or network timeout | `Error("operation_timeout")`; exit `2`. |
| Invalid arguments | `ValueError` (CLI `invalid_argument`); exit `2`, no network I/O. |
| Invalid run SHA/identity | `invalid_run_identity` or existing `run()` validation error; exit `2`. |
| Missing job, inconsistent job identity or unfinished job | `job_not_found`, `job_identity_mismatch`, or `job_not_completed`; exit `2`. |
| Invalid/ambiguous steps or incomplete job metadata | `invalid_step_identity` or existing `jobs()`/step-helper validation error; exit `2`. |
| HTTP 401 | `authentication_required`; exit `2`. |
| HTTP 403 | `permission_or_rate_limit`, or `rate_limit` when remaining/retry headers identify a rate limit; exit `2`. |
| HTTP 429 | `rate_limit`; exit `2`. |
| HTTP 404 | `not_found_or_inaccessible`; exit `2`; no inference about the existence of inaccessible logs. |
| HTTP 410 | `log_not_available`; exit `2`. |
| HTTP 5xx | `server_error`; exit `2`. |
| HTTP 206/Content-Range, short Content-Length body, broken chunked response | `incomplete_log_response`; exit `2`, not a local-cap observation. |
| Missing redirect location | `invalid_log_redirect`; exit `2`. |
| Unsafe redirect destination | `unsafe_log_redirect`; exit `2`, no unrestricted fallback. |
| Excess or cyclic redirect | `log_redirect_limit`; exit `2`. |
| Unsupported compression or UTF-16/32 BOM | `unsupported_log_encoding`; exit `2`, no automatic decompression or alternate decoding. |
| Invalid UTF-8 body | `invalid_log_encoding`; exit `2`. |
| Malformed/contradictory headers, unsupported content type or unexpected HTTP status | `invalid_log_response`; exit `2`. |
| Invalid credential response | `invalid_authentication_response`; exit `2`, no credential value in the diagnostic. |
| Connection or worker transport failure | `transport_error` or existing process/gh error; exit `2`. |

Error messages remain compact classifications; they do not include remote error
bodies, downloaded log fragments, credentials or signed request URLs. Failed
authentication is not an empty successful log. A network cutoff is not a
deliberately truncated result. Do not infer total remote size from a bounded read.

#### Text normalization, redaction and evidence

Decode the complete body strictly as UTF-8, remove BOM characters, ANSI
CSI/OSC/DCS sequences and unsafe C0/C1 terminal controls, and convert CRLF/CR to
LF before redaction. The `ansi_removed` flag covers removed terminal control
sequences/characters; newline normalization alone does not set it. The built-in
policy covers supported GitHub token patterns, the current GitHub credential,
Authorization credential values, explicitly provided literals, Actions `add-mask`
declarations (case-insensitive, including whitespace-separated words and command-data
escapes), private-key blocks, credential-like assignments and signed URL query
values. Normalize before matching so terminal controls cannot split a matched
secret. Collect and merge overlapping matches on the original normalized text
before replacing them; one mask must not disable another rule. Application-specific
secrets require caller-supplied literal values;
the policy makes no universal secret-detection guarantee.

Return and hash only sanitized complete text. No helper automatically saves raw
logs, signed URLs or secret hashes to a file, stdout diagnostic or JUnit report.
The CLI JSON is a deliberate output of sanitized text; downstream evidence
collection should retain compact identity/outcome, not unconditionally copy the
whole observation. Offline fixtures use synthetic values and cover exact
identity, caps/timeouts, redaction, BOM/ANSI, redirects, authentication and
incomplete responses. Live GitHub checks are separate and opt-in.

The existing CI matrix targets Python 3.10, 3.11, 3.12, 3.13 and 3.14, plus
moving `3.x` with `check-latest`. Passing a given PR's CI is evidence for that
exact head and tested interpreters, not a promise about an untested future
release. This job-log addition addresses only its slice of Issue #20. Other
pending read APIs and contract-validation items remain open; this implementation
does not close Issue #20.

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

## Workflow discovery (PR #28)

`workflow_run_discovery` resolves a workflow ID/path via `workflow()`, then uses the workflow-scoped Actions runs endpoint with branch/event server-side filters and an exact head SHA client-side filter. Limits are per operation. The result schema is `gh-identity-workflow-runs/1` with `runs`, `complete`, `truncated`, `limit_reason`, `pages_fetched` and `next_page`/`next_offset`. `max_items` may stop within a page: `next_page` and `next_offset` together identify the continuation position. No silent limit escalation or guaranteed snapshot isolation is promised. Authentication, timeouts, and byte limits may raise errors rather than return partial records.

Resume note: the CLI accepts `--start-offset N` together with `--start-page N`. A continuation is positional, not an immutable snapshot; newly inserted or deleted workflow runs may shift pages. Byte/time budget errors remain fail-closed and do not promise partial output.
