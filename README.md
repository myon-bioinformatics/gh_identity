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

## Exact PR, Issue and commit content

```bash
python gh_identity.py content OWNER/REPO pr 23 --field title --field body
python gh_identity.py content OWNER/REPO issue 20 --field body
python gh_identity.py content OWNER/REPO commit FULL_40_CHARACTER_SHA --field body
```

`content(repo, kind, identifier)` returns `gh-identity-content/1` with exact
repository/kind/number or SHA, URL, observed_at, title and full body. PR and
Issue body means the description, not comments. Commit body is the entire
commit message including its subject, blank lines and trailers. Null and empty
Issue/PR descriptions remain distinct; missing/malformed content is an error.
Commit selectors must be full SHAs; resolve a branch/ref first using
`resolve_ref`, then read that immutable identity. No clone is needed.

```python
import gh_identity as ghi
hits = ghi.search('repo:OWNER/REPO is:open', kind='pr')
if hits['items']:
    selected = ghi.content_from_hit(hits['items'][0])
    body_only = ghi.select_content(selected, ['body'])  # offline, no extra GET
```

Search/list results remain digest-first. `content_from_hit` re-reads the chosen
repository/number/kind and does not trust an old search body. It also accepts
an explicit commit identity with repository/kind/sha; commit search is not
implemented by this change. `select_content` selects exact top-level fields
and always retains identity/provenance; it does not interpret or execute text.
Unknown fields raise ValueError. All content calls share existing gh/urllib
transports and byte/time budgets. Permission/404/oversize failures are errors,
not successful empty content. `pr()` retains its compact state/head/base API.

### Git history versus GitHub discussion data

Git stores commit messages, trees and blobs, so local show/log/diff/blame can
reuse the established git_inspector APIs being consolidated in PR #23.
PR/Issue descriptions and discussions are GitHub records, read via gh/API;
cloning does not obtain those descriptions. `clone --depth 1` truncates history:
older blame and comparisons need the relevant objects/history fetched first.
The local reader does not clone/fetch on its own or pretend missing history is
complete. This content API introduces no subprocess Git or HTML parser.

Code patches, PR changed-file pagination, remote comparisons and review/comment
bodies are separate from descriptions and are not returned here. A REST commit
response's optional files/patch data is deliberately not presented as a complete
diff. These broader read contracts remain tracked in #20; local Git inspection
continues in #23. See [GitHub Issues REST](https://docs.github.com/en/rest/issues/issues#get-an-issue),
[GitHub Commits REST](https://docs.github.com/en/rest/commits/commits#get-a-commit),
and [Git shallow-clone behavior](https://git-scm.com/docs/git-clone).

## Repository structure with directory/file exclusions

```bash
# Top-level files and folders of main; no clone or blob download
python gh_identity.py tree OWNER/REPO
# Two levels, with selected folders and file patterns excluded
python gh_identity.py tree OWNER/REPO --ref main --depth 2 \
  --exclude-dir vendor --exclude-dir node_modules --exclude-file '*.lock'
# All included subtrees
python gh_identity.py tree OWNER/REPO --recursive \
  --exclude-dir 'docs/generated' --exclude-file '*.png'
```

The Python API is `tree(repo, ref="main", depth=1, exclude_dirs=(),
exclude_files=(), transport="auto", timeout=30)`; `depth=None` means recursive.
It resolves the ref once, then reads tree metadata at that commit and the
returned subtree SHAs. The output includes commit/tree SHA, full relative paths,
kind, Git mode and object SHA. It never fetches file bodies, follows symlinks or
submodules, or invokes clone, diff or blame. No local `.git` is required.
This feature is independent of the local history inspection work in PR #23.

Depth 1 means root entries, depth 2 adds their direct children. Directory rows
include `expanded` to distinguish visited directories from depth boundaries.
There are no implicit exclusions. Repeated `--exclude-dir`/`--exclude-file`
options use case-sensitive Python fnmatch patterns: patterns without `/` match
a basename at any depth; patterns containing `/` match the full relative path.
Use `vendor`, not `vendor/`, to exclude that folder. These are not gitignore
rules: no negation, and fnmatch `*` can span `/` in a full-path pattern.
Directories are pruned before any subtree request. File exclusions apply to
non-directory entries, including symlinks and submodule pointers.

`complete` refers only to `scope: selected_depth_and_exclusions`, not the whole
repository. If any GitHub tree response is truncated, `complete=false` and
`truncated=true`. Malformed/duplicate entries, mismatching child identities,
cycles, permission and transport failures raise errors. Existing operation
limits cover response bytes/time; tree requests charge max_pages and all
received entries (even excluded ones) charge max_items. Limit exhaustion raises
an error rather than claiming a complete list. `excluded_entries` counts only
observed excluded entries, not unseen children of pruned directories.

Tree metadata uses the existing gh/urllib read transport. Public repository
reads support the existing anonymous HTTP fallback. GitHub's
[Git Trees API](https://docs.github.com/en/rest/git/trees#get-a-tree) documents
tree modes, non-recursive retrieval and upstream truncation.

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

The observation helper requires a unique matching job, matching run/attempt and an existing step number. It does not infer the step number from list position. The pure builder formats explicitly supplied identities; it does not establish that a job, step or log line exists. A line anchor selects the GitHub UI location, not a log API endpoint. Step links are independent of log availability. The bounded job-log API below reuses these helpers; the other read contracts remain tracked in [Issue #20](https://github.com/myon-bioinformatics/gh_identity/issues/20).

## Bounded Actions job-log text

```bash
# RUN_ID and JOB_ID are numeric; select an exact attempt explicitly.
python gh_identity.py job-log OWNER/REPO RUN_ID JOB_ID --attempt 2

# Separate the log-body cap from the whole operation's byte/time budget.
# PRIVATE_VALUE is an already-set environment variable; its value stays off argv.
python gh_identity.py job-log OWNER/REPO RUN_ID JOB_ID --attempt 2 \
  --log-bytes 500000 --max-bytes 3000000 --timeout 20 \
  --redact-env PRIVATE_VALUE
```

```python
import os
import gh_identity as ghi

observation = ghi.job_log(
    "OWNER/REPO", 20, 91,
    attempt=2,
    max_bytes=500_000,
    redact=(os.environ["PRIVATE_VALUE"],),
    transport="auto",
    timeout=20,
)
if observation["complete"]:
    # Decide where to send the sanitized text; the API creates no log file.
    text = observation["text"]
else:
    # The body exceeded max_bytes. No possibly cut secret/prefix is returned.
    assert observation["truncated"] and observation["text"] is None
```

`job_log(repo, run_id, job_id, *, attempt, max_bytes=1_000_000, redact=(),
transport="auto", timeout=30)` requires a positive explicit attempt; it never
substitutes the latest attempt. It calls the existing `run()` and `jobs()` APIs
and validates the requested repository/run/attempt, a unique job in the complete
attempt observation, matching full head SHA, and a completed job before requesting
its log. A completed job may have failed; completion does not mean a green result.
`step_urls` are produced by the existing observation helper and pure URL builder.
They identify observed steps, not byte ranges or verified log-line locations.

The `gh-identity-job-log/1` result carries repository, run ID, job ID, attempt,
head SHA, observation time, `step_urls`, and the following log fields:

| Field | Meaning |
| --- | --- |
| `complete`, `truncated` | `true`/`false` for a complete downloaded body; `false`/`true` when the local log-byte cap is exceeded. |
| `text` | Normalized, redacted text only after the entire bounded response has been received; otherwise `null`. |
| `text_sha256` | SHA-256 of the returned sanitized UTF-8 text; `null` when text is withheld. It is not a raw-log hash. |
| `bytes_read`, `limit_bytes` | Raw body bytes observed and the requested local log cap. A declared oversized Content-Length can stop the read at zero bytes; otherwise an over-limit observation may include one probe byte. This does not claim the full remote size. |
| `redirects` | Number of validated download redirects followed. Signed download URLs are not returned. |
| `transport`, `credential_source` | Body transport is `urllib`; credential source is `environment`, `gh`, or `anonymous`. |
| `redaction`, `normalization` | `redaction.applied` reports whether sanitization ran, even with zero replacement operations; normalization reports BOM/ANSI removal. These flags are false when text is withheld. |

### Transports, credentials and redirects

Run/job metadata retains the existing `auto` policy: prefer `gh`, with `urllib`
fallback for missing/unauthenticated `gh`. The log body always uses a bounded stdlib
worker so redirect, decoding and error behavior are consistent across installed
`gh` versions. Thus `--transport gh` chooses metadata/authentication behavior;
it does not switch the raw body to `gh run view --log`.

The body request first uses `GH_TOKEN`, then `GITHUB_TOKEN`. With `auto` or `gh`,
an existing CLI credential can be read using `gh auth token --hostname github.com`
over bounded pipes when no environment token is available. `auto` permits an
anonymous public request if `gh` is unavailable or unauthenticated; explicit
`gh` reports that failure. Cancellation and other CLI failures remain errors.
Explicit `urllib` never requests a CLI credential.
The worker receives credentials through stdin, not command-line arguments.

GitHub documents anonymous access to public job logs. Private repositories need
appropriate access; fine-grained tokens require Actions read permission, while
classic PATs/OAuth tokens require the `repo` scope. The job-log endpoint returns
a short-lived redirect to a plain-text download. See the
[GitHub job-log API](https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run)
and [gh auth token](https://cli.github.com/manual/gh_auth_token).

GHI follows at most three manual redirects, restricted to HTTPS subdomains of
`actions.githubusercontent.com` or `blob.core.windows.net`. Only the initial
GitHub API request receives Authorization; redirected requests do not inherit
it. Missing, unsafe and excessive redirects are errors. Tokens and signed URLs
are omitted from public output and error diagnostics.

### Limits, normalization and failure handling

`max_bytes`/`--log-bytes` caps the raw log body before UTF-8 decoding. An oversized
body returns a truncated observation with `text=null` and `text_sha256=null`;
it does not return a prefix, which might cut a secret before it can be recognized.
CLI exit **1** means this explicit local-limit result. The outer operation's
`--max-bytes` and `--timeout` still apply cumulatively to metadata, credential
lookup and body retrieval. Exhausting them raises `bytes_limit` or
`operation_timeout` and exits **2**. CLI exit **0** means complete sanitized text.
Network, authentication, identity and malformed/incomplete-response exceptions
also exit **2**, never an empty or truncated successful body.

Normalization removes UTF-8 BOM characters, ANSI CSI/OSC/DCS sequences and unsafe
terminal control characters, and converts CRLF/CR newlines to LF before redaction.
Redaction covers supported GitHub token patterns, the current GitHub credential,
Authorization credentials, explicit literal values, Actions `add-mask` values/words,
private-key blocks and credential-like assignments/signed URL queries. Overlapping
matches are combined before a single replacement pass. This is not a detector for every
possible secret. Supply application-specific values with `redact=(...)`; this
accepts a list or tuple of up to 100 nonempty strings, at most 65,536 UTF-8 bytes
combined. The CLI accepts repeatable `--redact-env NAME` instead of secret values
on argv. An unset or empty named variable is an argument error.

HTTP 206, Content-Range, a body shorter than its declared Content-Length, and
broken chunked responses are `incomplete_log_response`, distinct from an
intentional local cap. HTTP 401 is `authentication_required`; 403 is
`permission_or_rate_limit` unless response headers identify rate limiting;
429 is `rate_limit`; 404 is `not_found_or_inaccessible`; 410 is
`log_not_available`; and 5xx is `server_error`. See [SPEC.md](SPEC.md#bounded-job-log-contract)
for identity, redirect, encoding and response-format errors. Only UTF-8 plain
text is decoded; compressed responses and UTF-16/32 BOMs fail with
`unsupported_log_encoding`, and invalid UTF-8 is `invalid_log_encoding`.

The API/CLI does not automatically write raw logs, signed URLs or secret hashes
to files or JUnit evidence. Offline regressions use synthetic log fixtures;
routine JUnit results contain test outcomes and compact identity. Live GitHub
verification is an explicit separate operation. This addition implements only
Issue #20's job-log slice; its other API and contract-validation work remains
unfinished.

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

### Full-response CLI replay

Six recorded public PR/Issue/commit REST responses are retained in
`tests/fixtures/content_snapshots`. Run
`python scripts/check_content_snapshots.py tests/fixtures/content_snapshots`
to compare full and body-selected CLI output against their exact body strings.
The stdlib POSIX runner reports hashes and exits 0 on agreement, 1 on CLI/output
mismatch, or 2 on invalid evidence/runner failure. A temporary recorded `gh`
process supplies the saved responses; this does not certify live connectivity.
See the fixture README for capture endpoints and fresh-capture instructions.

### Saved HTML / serialized DOM body text

```sh
python gh_identity.py html-content saved-page.html \
  --selector '#issue-description' --selector '.specific-description-body'
python gh_identity.py html-content saved-dom.html \
  --selector '#selected-body' --source-kind dom
```

These selectors are illustrative: inspect the saved document and choose its
actual unique description/container identifier. Supported selectors are simple
`#id`, `.class`, or tag names, in priority order; this is not a full CSS selector
engine. The first existing candidate must be unique. Multiple matches fail,
including multiple comment bodies; missing candidates never fall back to page
text. There is deliberately no unverified GitHub layout heuristic yet.

`html_content(text, selectors, source_kind="html")` is an offline stdlib API.
It removes script/style/template/noscript and navigation/header/footer/button/SVG
subtrees, explicit hidden/aria-hidden elements and simple inline display:none or
visibility:hidden. It decodes entities, keeps link text and image alt text, inserts
block/line/table boundaries, and preserves preformatted code whitespace. CSS
selectors inside code/text are content and remain intact. It does not execute JS,
load stylesheets, calculate layout/accessibility, or certify browser-visible text.
The `dom` label means the caller supplied serialized DOM; it does not launch a
browser. Caller-chosen containers may still contain unrelated UI elements.

Output records the selected pattern, input SHA-256 and source kind, with
`visibility: structural_only` and `identity_verified: false`; a saved page alone
is not authenticated PR/Issue/commit identity evidence. Runtime does no network
access. UTF-8 input defaults to a 10 MB limit (`--input-bytes`) and nesting is
bounded. Invalid/missing/ambiguous input exits 2. The usual REST `content` command
continues returning exact Markdown/message text rather than this normalized text.

The HTML tests currently use synthetic layouts with Japanese, entities, code,
hidden elements and adjacent comments. Real GitHub HTML/DOM layout validation is
still outstanding: the capture connector converts GitHub page URLs to REST JSON.
Saving HTML once allows repeated extraction without repeat requests; fetching an
HTML page is not necessarily smaller than fetching its API representation.

### HTML / Markdown round-trip probe

```sh
python scripts/check_html_roundtrip.py /path/to/vendor/markdown.py
```

This optional stdlib probe loads the explicitly supplied, trusted markdown module;
it adds no runtime vendor dependency. Six CSS-free HTML examples are converted to
Markdown, back to HTML, then Markdown again. It checks both normalized structural
content (including links and preformatted whitespace) and Markdown stability.
It is not a byte-for-byte HTML reversibility claim or a live GitHub layout test.
The plain-text `html-content` output is not the round-trip input: text extraction
already discards links/formatting. Retain the original HTML for this purpose.

Observed with mcp-toolcall-lab's locked markdown.py from
`c3063e0887c6eb6a531ee774793682ceff8a164d` on 2026-10-09:
paragraph/link, heading/emphasis, list, quote and table matched; code failed.
`<pre><code>x\n</code></pre>` gains a trailing newline in the conversion path.
All six Markdown representations stabilized, demonstrating why stability alone
is insufficient. The probe intentionally exits 1 for that source-preservation
failure; it is not included as a green CI gate. Exit 2 indicates runner failure.
That result applies to the recorded converter pin; it is not a claim about every
newer markdown revision. A canonical converter fix is being validated separately;
rerun the same probe with the exact adopted file before changing this baseline.

For optional cross-project wrapper checks, supply an existing trusted web-ui module:

```sh
python scripts/check_html_roundtrip.py /path/to/vendor/markdown.py \
  --web-ui-module /path/to/web_ui.py
```

This reuses web-ui's `render_document`, `shared_stylesheets`, and `shared_scripts`
to wrap each fragment with inline CSS, shared CSS references and opt-in module
script references. GHI extraction before/after wrapping must match exactly.
It prints module SHA-256 identities, requires no vendor changes, and performs no
network requests. CSS rendering and JS execution are not tested. With web-ui PR
#43 at `9ee0232494cb124ed24b79ef87980630bc550e31`, all six wrapper cases matched;
the independent Markdown code-newline failure still correctly yields exit 1.
This optional composition keeps rendering in web-ui, conversion in markdown,
and extraction in GHI; consumers can use the same runner with their adopted files.

Page-family regressions now model Wiki plus sidebar, file code plus controls,
PR/Issue description plus replies, commit message plus diff statistics, release
notes plus assets, and README details/code. Duplicate containers and sign-in pages
fail closed. These are deliberately synthetic, not recorded GitHub DOM selectors.
Public page text was inspected at https://github.com/obsproject/obs-studio/wiki
and https://github.com/python/cpython/blob/main/README.rst on 2026-10-09; this
confirmed the need to separate navigation/controls from content but did not supply
raw DOM compatibility evidence. Directly selecting `pre` now preserves its exact
leading/trailing newlines instead of trimming them as block boundaries.

Live-browser DOM evidence now exists for two code blocks in the public GHI
README file preview, captured once on 2026-10-09. See
`tests/fixtures/browser_dom/file-code.json` and `tests/test_browser_dom.py`.
The saved `outerHTML` includes actual nested GitHub syntax-highlight spans;
CLI extraction is compared exactly with the browser's recorded `innerText`.
No repeated network access is needed for replay. Scope is these two observed
file-preview fragments, not the whole page, file Code tab, or all GitHub layouts.
No login interaction was performed. This does not resolve the separate Markdown
round-trip newline drift or establish CSS/JS equivalence for arbitrary pages.

### Optional internal reproduction when a site cannot be inspected

`scripts/serve_dom_lab.py` provides an optional local Gradio fixture renderer.
Use it to separate local rendering/extraction behavior from an inaccessible site's
network or access failure; it is not a replacement for live-site evidence or a
CORS bypass. It does not fetch remote pages and binds only to 127.0.0.1, with public
sharing disabled. Supply an existing trusted markdown.py:

```sh
python scripts/serve_dom_lab.py /path/to/vendor/markdown.py
```

Install Gradio separately in a disposable test environment if needed; it is not
a GHI runtime dependency. Inspect `#dom-lab-body` in the rendered page and save
its DOM for offline comparison. For fixture-only generation without Gradio:

```sh
python scripts/serve_dom_lab.py /path/to/vendor/markdown.py --export-html fixture.html
python gh_identity.py html-content fixture.html --selector '#dom-lab-body'
```

At the initial capture, only the export/extraction path was exercised because
Gradio was unavailable. Later, web-ui PR #43 at `8ff5bfb` added and passed a
local Gradio + Chromium DOM integration test
([CI evidence](https://github.com/myon-bioinformatics/web-ui/actions/runs/37887560280)).
That validates web-ui's shared reproduction path, not this GHI-specific
`serve_dom_lab.py` server path, which remains unverified.
The built-in representative fixture does not reproduce an arbitrary site's DOM,
computed CSS, JavaScript state, authentication, or network restrictions.

A live OBS Studio Wiki inspection on 2026-10-09 also exposed an important scope
boundary: `#wiki-body` contains the "Add a custom footer" edit affordance, while
its `.markdown-body` child contains the article. Do not infer that a body-named
ID excludes all UI. The first article paragraph DOM is retained in
`tests/fixtures/browser_dom/wiki-paragraph.html` and matched to observed browser
text. Source: https://github.com/obsproject/obs-studio/wiki (public, no login).
This is a paragraph-level capture, not whole-Wiki certification. Selected `pre`
regressions cover zero, one and two trailing newlines, leading newlines, tabs,
indentation and Japanese text, independently of the Markdown converter.
The same capture also retains the four-link help sublist (`wiki-links.html`).
Its text/order is compared after whitespace normalization, explicitly excluding
browser-generated list markers and layout spacing from that claim.

Additional live DOM probes outside GitHub (2026-10-09) are in
`tests/fixtures/browser_dom/simple-sites.json`. Abe Hiroshi's official top page
uses Shift_JIS (observed `document.characterSet`), legacy font/table layout and
an entry frameset referring to `menu.htm` and `top.htm`. The child heading was
captured from the observed `top.htm` reference. Browser-serialized DOM is already
Unicode; this is not evidence that the UTF-8 file CLI decodes original Shift_JIS
HTTP bytes. Frame parent markup does not include child documents; no auto-fetch
or frame traversal has been added.

TOHO's plain news heading matches browser innerText exactly. Its image-backed
main heading supplies `Moments for Life` in image alt, which innerText omits but
GHI deliberately retains. That case checks alt plus text, not identical visible
text. A visually-hidden CSS class was also observed; generic class-based hiding
cannot be inferred without styles/computed layout. These small captures validate
specific tag patterns, not complete extraction or CSS fidelity for either site.

Global-site variation: small live Google company-info and Apple accessibility DOM
captures (2026-10-09) now cover colored inline spans with U+200B zero-width spaces,
a nested card with decorative SVG, and a paragraph with NBSP and JSON-valued data
attributes. See `global-sites.json` and `test_global_site_dom.py`. The CLI is
exercised against saved fragments without repeat HTTP access. Google inline text
matches exactly and retains U+200B. Card/Apple comparisons explicitly normalize
whitespace: GHI collapses NBSP outside pre, so these are not byte-exact browser
innerText claims. SVG and attribute data do not leak into text. No site-specific
parser branches, cookie actions, or authentication were introduced.

### Public chat UI structural cases

ChatGPT's logged-out top page, Gemini's consent dialog and Claude's login/FAQ
surface were inspected on 2026-10-09 without accepting consent, signing in,
entering input or sending chat messages. `chat-surfaces.json` retains documented
semantic projections of observed DOM (not verbatim captures): empty textarea
placeholder/label, custom-element icons under aria-hidden, and an accordion
heading whose text is inside a button. Volatile bindings/layout classes are
removed; relevant text/attributes remain. Source state is recorded per case.

`html-content --include-controls` / `html_content(..., include_controls=True)`
opts into button text, useful for FAQ headings. The default still excludes
buttons. Explicit hidden and aria-hidden subtrees stay excluded in either mode;
`data-hidden` alone is not treated as HTML `hidden`. Input labels/placeholders
are not substituted for empty user content, and no control is activated. These
are text extraction semantics, not a DOM interaction/accessibility tree API.
The option is recorded in output. These cases establish neither authenticated
chat transcript extraction nor whole-site support.

### Browser capture ownership

Browser launch/DOM acquisition now belongs to browser-test-kit PR #54:
https://github.com/myon-bioinformatics/browser-test-kit/pull/54

From that checkout:

```sh
python examples/capture_browser_dom.py https://chatgpt.com/ --selector h1 --output chatgpt-dom.json
```

It reuses the kit's existing Playwright/Chromium lane and adds a local browser
fixture test. GHI owns offline selected-body extraction and GitHub resource
identity/content; it does not ship the browser launcher or require Playwright.
Recorded DOM fixtures remain here as consumer regression evidence. Gradio fixture
reproduction and conversion checks remain optional test helpers, not GHI runtime
APIs. Neither PR is merged and browser execution validation belongs to the kit CI.

### Inline visibility correction (2026-10-09)

The previous structural reader removed a subtree if *any* inline declaration
said `display:none` or `visibility:hidden`, even if a later declaration restored
it. The reader now resolves repeated declarations for each property in order;
`!important` wins over ordinary declarations, and the last declaration of equal
importance wins. Comments and semicolons within quoted strings or functions do
not become extra declarations. This limited rule follows the
[CSS cascade order](https://www.w3.org/TR/css-cascade-5/#cascade-order).

This is still structural body extraction, not browser `innerText`: `aria-hidden`
remains excluded for API compatibility even when CSS would display its text.
Image alt text remains included, and controls remain opt-in. browser-test-kit's
page-text/capture path has a different visible-text comparison contract; do not
use equality between these APIs as proof of browser visibility. Use a recorded
browser `innerText` observation when that is the required reference.

The inline rule does not validate arbitrary CSS values, resolve custom properties,
stylesheet rules, inheritance, escapes, layout, or a descendant overriding an
ancestor's `visibility:hidden`. Unsupported values are unknown rather than a
computed-style result. No new CSS engine, dependency, transport, or browser
launcher is introduced. Regression cases exercise both candidate selection and
rendering so they cannot disagree about the corrected inline declarations.

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

## Workflow-scoped run discovery (PR #28)

```bash
python gh_identity.py workflow-runs OWNER/REPO .github/workflows/ci.yml --branch main --event push --page-size 50 --page-limit 3
python gh_identity.py workflow-runs OWNER/REPO 12345 --start-page 4 --limit 50
```

`workflow_run_discovery(repo, workflow_id, *, branch=None, head_sha=None, event=None, max_items=100, max_pages=10, page_size=100, start_page=1)` provides workflow-specific runs. JSON reports `complete`, `truncated`, `limit_reason` and `next_page`/`next_offset`. `next_page` is a page-level continuation, a page-and-offset continuation. Pass both `--start-page` and `--start-offset` to resume within a page without skipping entries. Repeated requests can observe a changed run listing.

Resume note: the CLI accepts `--start-offset N` together with `--start-page N`. A continuation is positional, not an immutable snapshot; newly inserted or deleted workflow runs may shift pages. Byte/time budget errors remain fail-closed and do not promise partial output.

## Optional public repository inventory (Issue #29)

```bash
python gh_identity.py repo-inventory myon-bioinformatics --fields name,size-kb
python gh_identity.py repo-inventory myon-bioinformatics --fields name,run-count --sort run-count --order desc --max-repos 20
```

`repository_inventory(owner, fields=("name",), sort="name", order="asc", max_repos=30, max_pages=5)` selects public, non-archived repositories. Size is GitHub's approximate repository metadata size in KiB, **not** source LOC. Run count is repository-wide Actions `total_count`, not workflow-specific. Run-count fan-out is opt-in; inaccessible values remain null with a reason, not zero. Limits are cumulative, and a partial inventory is not a complete ranking. Ascending and descending sorts place unavailable (null) metric values last; zero is a valid value, not missing. For `updated-at`, timezone-aware ISO timestamps are compared as UTC instants; missing or malformed timestamps sort last. Equal metric values use repository name ascending as a deterministic tie-breaker.

Reaching `max_repos` conservatively reports `truncated=true` and `sort_scope="partial"`, even when the available repository count happens to equal the limit; no extra request is made to prove exhaustion.
