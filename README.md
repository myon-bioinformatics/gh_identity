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
A canonical markdown converter fix and recheck remain outstanding.

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

The export/extraction path was exercised. Gradio is unavailable in the current
execution environment, so its server/browser path is prepared but not verified.
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
