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

## Read observation validation

`jobs()` rejects malformed or duplicate job identities and rows that do not
match the requested run or explicit attempt. When no attempt is specified,
observed per-job attempts are preserved and need not all match. This keeps
partial-rerun observations usable without asserting an unrequested attempt.

`comments(last=N)` accepts nonnegative integers: zero shows no comments while
retaining the observed total; negative values, booleans and other types fail
before transport. This is an output limit, not a collection-memory limit.
Check normalization prefers GitHub's human-facing `html_url`, falling back to
`url` for pre-normalized consumer rows.


## Operation limits and transport injection

Every public network operation shares a cumulative budget across its nested
requests, pagination and gh-to-urllib fallback: 100 collection pages, 10,000
collected items, 10,000,000 response bytes and 30 seconds by default. Filtered-out
collection rows still count. Page/item limits apply to collection helpers;
response bytes and elapsed time also cover individual requests. `timeout=` sets
the outer operation deadline, rather than restarting it on each page.

```python
with ghi.operation(max_pages=20, max_items=2000, max_bytes=2000000, timeout=15):
    checks = ghi.checks_for_sha("OWNER/REPO", sha)
    comments = ghi.comments("OWNER/REPO", 9, last=5)
```

Explicit scopes share limits across multiple calls and cannot be nested. CLI
flags `--max-pages`, `--max-items`, `--max-bytes`, and `--timeout` accept the same
limits before or after the command. Counts must be positive integers; time must
be positive and finite. Budget exhaustion raises `Error` with `pages_limit`,
`items_limit`, `bytes_limit`, or `operation_timeout` (CLI exit 2), never a complete
or green partial result. Once exceeded, a budget stays failed until its scope
exits: catching the exception cannot enable another request in that scope. A
blocked follow-up write has not started and is not marked uncertain.
Existing discovery helpers retain their explicit
`truncated` result for their own smaller discovery limits. A short check response
with a valid but unmatched total is `complete=false`, `state=incomplete`;
missing, invalid, changing or exceeded totals raise `pagination_incomplete`.

The built-in transports drain bounded stdout/stderr pipes and stop their child
process on the wall deadline. urllib runs in a private Python worker so stalled
DNS or response reads can be stopped too; credentials are sent through stdin.
This requires permission to start the current Python executable and access to
the module file. The byte budget includes gh diagnostics; it limits collected
response bytes, not total interpreter memory. An interrupted mutation remains
uncertain and must not be blindly retried.

`pages(..., requester=...)` and `checks_for_sha(..., requester=...)` accept a
callable with signature `requester(method, path, *, transport, timeout)` for
existing clients and offline fixtures. Return parsed JSON to use numbered
pagination, or `ghi.Page(data, headers)` to preserve Link pagination. Explicit
headers without a next link end the collection, even on a full page. Next links
must advance one page on the same GitHub API endpoint with unchanged filters.
This retains injectable client transports without duplicating check classification.

An adapter can wrap an existing client's response as follows:

```python
def requester(method, path, *, transport, timeout):
    # Configure your client's transport to enforce this remaining timeout.
    response = client.request(method, "/" + path)
    return ghi.Page(response.data, response.headers)

checks = ghi.checks_for_sha("OWNER/REPO", sha, requester=requester)
```

Injected callables are trusted: they must bound their own I/O and allocations and
honor the supplied remaining timeout. GHI checks elapsed time and charges the
serialized payload size after they return; it cannot interrupt arbitrary Python
callbacks. Existing consumer deployment/vendor updates remain tracked in #4;
this interface and its parity fixtures provide the migration seam.

## Issues and cross-repository PR search

```bash
# Exact Issue body, state and identity (a PR number is rejected)
python gh_identity.py issue myon-bioinformatics/gh_identity 20
# Repository Issues only, excluding PRs
python gh_identity.py issues myon-bioinformatics/gh_identity --state open
# Existing repository PR discovery, now reachable from the CLI
python gh_identity.py prs myon-bioinformatics/gh_identity --state all
# Cross-repository PR search under a user account
python gh_identity.py search 'user:myon-bioinformatics is:open' --kind pr
# Organization search; use the actual organization login
python gh_identity.py search 'org:YOUR_ORG is:closed is:unmerged' --kind pr --sort created
# Issue search by labels, author, text or date
python gh_identity.py search 'user:myon-bioinformatics is:open label:bug' --kind issue --limit 50
```

Python entry points are `issue(repo, number)`, `issues(repo, state="open")`,
`pull_requests(repo, state="open")`, and
`search(query, kind="pr", sort="updated", order="desc")`. All share the existing
stdlib transport, gh preference, urllib fallback, and cumulative operation budget.
No new runtime dependency or write operation is introduced.

Search accepts GitHub search syntax, including multiple `repo:` qualifiers,
`user:`/`org:`, `is:open`/`is:closed`, `author:`, `assignee:`, `label:`,
`created:`/`updated:`, and PR-specific `is:merged`/`is:unmerged`, `draft:`,
`head:`/`base:`. GHI appends the selected `is:pr` or `is:issue`; do not supply a
contradictory kind. Search hits retain repository + number + kind + URL, so the
same number in different repositories is unambiguous. A free-text `60` is not an
exact PR-number selector; use `pr OWNER/REPO 60` for exact identity.

`--sort` selects `updated`, `created`, `comments`, or `best-match`; `--order`
selects `asc`/`desc`. Discovery `--limit` and `--page-limit` default to 100 items
and 10 pages (`max_items`/`max_pages` in Python). These are separate from the
existing cumulative `--max-items`/`--max-pages`/`--max-bytes`/`--timeout` limits.
Global limits raise errors rather than returning a successful partial result.
Issue listing counts filtered PRs against those global budgets.

Discovery output includes `complete`/`truncated`; search also retains
`total_count`, `incomplete_results`, and `scope: accessible_search_results`.
GitHub search exposes at most 1,000 results per query and may omit inaccessible
or not-yet-indexed data. `complete` only means the observed search response total
was collected without reported incompleteness, **not** complete fleet enumeration
or a transactional snapshot. Changing totals and duplicate identities fail;
short/incomplete pages and discovery limits never become complete results.
Narrow large searches using repository/date filters. Permission, rate-limit and
transport failures remain errors, not an empty successful search.

Lists/searches omit full bodies; `issue` returns the selected Issue body. PR hits
are search observations, not full PR head/base/check or mergeability evidence;
use `pr`/`observe_pr` to re-read exact current identity before subsequent work.
See the [GitHub search contract](https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests).

## Local Git inspection

Read-only checkout observations now live in this standalone file, migrated from
parent `git_inspector.py` at `380d877`. Public APIs are `git_status`, `git_ls_files`,
`git_diff`, `git_log`, `git_log_numstat`, `git_show`, `git_blame`, `git_grep` and
`git_check_ignore`. Existing GitHub operations and `local_identity` remain separate.
CLI examples: `python gh_identity.py git-status --root PATH`,
`python gh_identity.py git-diff --root PATH`,
`python gh_identity.py git-blame --root PATH --path FILE`.
Each command has `--output-bytes`; record/path/count bounds remain available on
Python APIs. These local bounds are independent of GitHub pagination budgets.

The observations never fetch, stage, checkout, reset, commit or push. Git optional
locks and optional helpers are disabled; byte truncation is explicit. The checkout
configuration is trusted input, not an isolation boundary for hostile repositories.
Parent execution location does not imply parent ownership of this implementation.
A parent compatibility adapter can expose the old function names over these APIs
once its consumer lock explicitly selects this revision.
