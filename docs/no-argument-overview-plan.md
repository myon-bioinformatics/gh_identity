# Showing the current position on a no-argument run — implementation plan

Status: proposal. This PR only organizes the spec; it does not yet change CLI behavior.
Base main: `16df861e8f44ea8c9da69416af1272e6bc1afb5a` (2026-10-11).

## Goal and entry point

Just by making the first call `python gh_identity.py`, the operator and the AI should
grasp, from short standard output, "which repo and which position we are at, how far
GitHub has progressed, and what was said most recently". This cuts the effort of calling
help, Git, and gh over and over to stitch information back together, and the tokens spent
reading full logs and diffs.

The current `_main()` requires a subcommand, so a no-argument run prints usage and exits 2.
Existing individual commands are kept; only an omitted subcommand branches to overview.
`--help` still prints the feature list and exits, without calling the API.
A run with only budget/transport options is also treated as overview. The internal worker branch is kept.

- `python gh_identity.py`: reads from cwd and writes short text to stdout.
- `python gh_identity.py overview`: an explicit entry point for the same processing.
- `python gh_identity.py --json`: structured JSON of the same observation.
- `python gh_identity.py overview --offline`: local observation only.
- `python gh_identity.py overview --repo OWNER/REPO`: explicit choice outside Git or when the remote is ambiguous.

Only overview (including the no-argument run) defaults to text; existing subcommands stay JSON.
README/SPEC state this difference explicitly. `--json` is defined in a shared argparse parent
and applied to both the top-level and overview parsers. With default=argparse.SUPPRESS,
the child parser's default does not override the parent's value; both positions and
duplicate use are covered by fixtures.

Text and JSON render the result of the same Python API `overview(...)`.
The JSON output and gate exit codes of existing subcommands are not changed.

## Information shown by default

| Item | Content | Misreading avoided |
|---|---|---|
| Observation | UTC observation time, online/offline, complete/partial | Don't confuse an old cache with current GitHub |
| Workspace | cwd, repo, reason for the choice, branch or detached, local HEAD | Don't treat the script's location as the working repo |
| Local changes | Counts of staged/unstaged/untracked, fetch limit | Don't make readers open contents just because it's dirty; no full diffs |
| Latest local commit | Short HEAD SHA, subject, date | Don't present the commit date as the fetch date |
| upstream | upstream ref, ahead/behind against the local tracking ref | Don't call it GitHub's latest state |
| GitHub baseline | The repository's default branch and its latest SHA/commit subject | The default branch is not always main |
| Comparison with baseline | Whether local SHA and GitHub baseline SHA match; ahead/behind when comparable | Don't conclude "rebase needed" from a different SHA alone |
| Current PR | Number/title/URL/head/base/draft of the open PR for the current branch | Don't pick a fork's different PR by branch name alone |
| Recent PRs | 1 most recently created open PR, 1 most recently updated open PR, up to 3 others | Don't mix created and updated order into one "latest" |
| Recent Issues | Most recently updated Issue chosen from both open and closed, with number, title, state, updated_at, URL; up to 2 others | Don't substitute PR updates for Issue updates; don't call closed ones unfinished |
| Latest Issue comment | Author, created_at/updated_at, full text, and source URL for the focus Issue | Don't confuse the Issue's updated_at with the latest comment time |
| Latest comment | Author/time/full text/URL of the focus PR's latest general comment | Don't mix reviews, inline comments, or threads into general comments |
| Reviews | Latest review submission, observed unresolved thread count and completeness | Don't translate CHANGES_REQUESTED into severity |
| CI on the same head | Check status/breakdown, plus run status and URL when needed | Don't treat 0 checks as green; don't use a success from another SHA |
| Where to look next | Up to 3 individual commands filled with the actual repo/number/SHA | Don't list full help or every feature each time |

The "focus PR" is the current branch's open PR when it can be identified uniquely, otherwise
the most recently updated open PR. If the two differ, up to one latest general comment is
shown for each. The most recently created PR is shown separately.
When the same PR appears in several slots, the display is merged and every selection reason is recorded.
When the current branch has several candidates, the candidates are shown instead of inventing a unique one.
A full history search including closed/merged PRs is left to additional commands and is not run by default.

Issues are independent of PRs: up to 3 recent ones, open and closed, are shown in descending
updated_at order. Recent updates to closed Issues are observed too, with the state stated explicitly.
PRs included in the REST Issue list are excluded so the same PR is not repeated in the Issue slot.
The "focus Issue" is the most recently updated Issue, and up to one of its latest general comments is shown.
The focus Issue's body is also included in full, so the Issue's direction and remaining work are visible even without comments.
Even if updated_at changed because of a body edit, close/reopen, labels, etc., it is not
assumed that "a new comment was posted" or "the remaining work is done".
The latest comment separates the newest post by created_at from updated_at (edit time).
A link between the current PR and an Issue is shown only when an explicit reference or GitHub's
relation data is observed; similar numbers or titles are not linked arbitrarily. The Issue slot is shown even with no PR.

## Sample default output

The following uses a fictional repo, SHAs, and counts; it is not a real observation.

```text
GHI overview | observed_at=2026-10-11T06:00:00Z | partial
repo: example/demo  (source: branch upstream remote)
cwd: /work/demo
local: work/fix-a @ aaaaaaa | staged=1 unstaged=0 untracked=2
local HEAD: 2026-10-11T05:00:00Z Add boundary check
upstream (local tracking ref): origin/work/fix-a | ahead=1 behind=0
GitHub default: main @ bbbbbbb | Update README
vs GitHub default: different | ahead/behind=unknown (remote SHA not local)
current PR: #42 Add boundary check | open, draft=false | head=aaaaaaa
  https://github.com/example/demo/pull/42
latest-created open PR: #43 Add example | created_at=2026-10-11T05:40:00Z
latest-updated open PR: #42 | updated_at=2026-10-11T05:55:00Z
latest conversation (#42): reviewer, 2026-10-11T05:55:00Z
  "Boundary cases are confirmed. Please update the README example too."
  https://github.com/example/demo/pull/42#issuecomment-123
latest-updated Issue: #40 Confirm rollout | open | updated_at=2026-10-11T05:58:00Z
  body: "Remaining work is tracked in the Issue. Added new verification results."
  https://github.com/example/demo/issues/40
latest Issue comment (#40): maintainer | created_at=2026-10-11T05:58:00Z
  "Recorded the items needing extra verification in the Issue body."
  https://github.com/example/demo/issues/40#issuecomment-124
reviews (#42): COMMENTED | unresolved threads=unknown (permission denied)
checks @ aaaaaaa: pending | success=5 in_progress=1
other open PRs (shown=1, total=unknown): #41 Update docs
next: python gh_identity.py observe-pr example/demo 42
next: python gh_identity.py review-observations example/demo 42
```

Standard output keeps summary metadata to roughly 30 lines, and the body of each selected
latest comment is printed in full with no arguments. This covers the latest general comments
of up to 2 PRs and 1 focus Issue.
The focus Issue's body is also printed in full, catching direction recorded in the body rather than a comment.
Other Issues/PRs show only title, state, time, and URL; full history is not expanded.
The metadata guideline is not applied to comment bodies, and bodies get no display limit such
as 240 characters / 8 KiB. No extra option or second call is required to read the full text.
Bodies are not replaced by AI summaries, leading excerpts, or extracted key lines. Headers such as from/to are kept.
In stdout, each body is shown with its delimiter, kind/id, author, created_at/updated_at, source URL,
and the original's chars/body_sha256, making the full-text range explicit.
Newlines, paragraphs, and whitespace in the body are kept. Dangerous terminal control characters
are shown in a reversible escaped form instead of being silently removed. The text rule is first
"\ → \\", then C0 other than LF/TAB (including CR), DEL, and C1 become "\xNN" (2-digit lowercase hex).
LF/TAB are kept as-is. A literal "\x1b" is distinguished from a real ESC, and a reverse-conversion
fixture checks that the original is restored. JSON keeps the complete original in body.
Remote URLs containing secrets, tokens, and raw API error messages are never printed.
Byte/time budgets for fetching are kept, but a partially fetched body is never presented as "full text".
When a body exceeds the budget, that item is marked as not fully fetched, with the cause, id, and source URL.
This is a failure state of incomplete fetching, not a rule to replace bodies with excerpts in normal operation.
When run in CI, full bodies on stdout are saved in Actions logs, etc. README/SPEC state that this is
the same for private repos. The user-requested full-text display is kept, and CI verification uses
synthetic fixtures so private real bodies are not written to logs.

## Deciding repo and ref

1. Use an explicit `--repo` if given. Also show any difference from the cwd-derived repo.
2. Choose the target remote from cwd's Git root and branch/upstream.
3. With no upstream, use a remote that uniquely identifies a GitHub repo.
   If several candidates remain, show only the candidates and point to `--repo`.
4. Outside Git, a verifiable CI `GITHUB_REPOSITORY` is a candidate.
   Local HEAD and CI's `GITHUB_SHA` are recorded as separate observations, never silently swapped.
5. Even when the repo cannot be determined, local information and the reason go to stdout.
   A search across all account repos is never started from a non-Git directory.

Only the verified OWNER/REPO is taken from a URL. Remote credentials are never shown.
Local refs and GitHub's default branch are kept in separate fields.
If the object for a SHA from GitHub is not present locally, ahead/behind is unknown.
It is not filled in by an automatic fetch; the user can choose the next step if needed.

## Fetching, limits, and state

- Reuse the existing `repository` / `resolve_ref` / `pull_requests` / `issues` / `issue` / `comments` /
  `reviews` / `review_observations` / `checks_for_sha` / `head-runs` family.
  Keep a shared observation so the same PR, head, and metadata are not refetched between calls.
- Local observation reuses the existing bounded Git inspection path.
  Don't copy the current `local_identity` custom subprocess path or add new parsers.
  Missing root/remote/ref comparisons are added to the shared, verified wrapper.
- Update-order candidates read up to 2 pages of `issues?state=all&sort=updated&direction=desc&per_page=100`
  and separate PRs from Issues by the `pull_request` key. The same response is shared to pick
  open PR candidates and open/closed Issue candidates separately. The most recently created open PR
  is fetched with one page of `pulls?state=open&sort=created&direction=desc`.
  A thin candidate fetch reusing existing normalization is added, so the same API is not fetched twice for different uses.
- If both pages are all PRs and no Issue is found, don't report "no Issues".
  Keep `selection_status=not_found_within_window`, `latest=null`,
  `pages_fetched=N`, `exhausted=false`, and `next_page`;
  text says "latest Issue: not observed (not found within N pages)".
  `selection_status=none` is allowed only after the end of all pages is confirmed.
  Permission errors/timeouts are a separate `selection_status=error`. The candidate display limit
  and the completeness of latest-candidate selection/listing are separate fields.
- For the latest general comment, read the response Link of `issues/{n}/comments?per_page=1&page=1`,
  and if `rel="last"` exists, fetch that last page once.
  For a static list this gets the full body in at most 2 calls per item (normally 1 for 0/1 comments).
  Full-history `comments(last=1)` is not used, and no invalid direction parameter is relied on.
  The focus Issue body reuses the body from update-order candidates, avoiding an extra Issue GET.
- The last-page approach does not atomically guarantee the latest item when posts/deletions happen concurrently.
  If the last response has yet another next, mark incompleteness as `latest_observed`;
  an empty last page, inconsistent Link, or observed concurrent updates are also not called complete.
  Automatic re-search must not exceed the budget. Page numbers are not treated as permanent counts.
- The shared gh/urllib transport needs an extension to extract response headers in a bounded way.
  Don't assume Link can be read from the current JSON-only transport.
  rel=last is validated for HTTPS/api.github.com, the same repo/number/path, per_page=1, and a positive page;
  arbitrary URLs are not followed. API auth is never forwarded to external URLs.
- The latest review submission also uses bounded last-page fetching, reserving up to 2 calls.
  The kind (general comment/review/inline/thread) is kept, and importance is not guessed from free text.
- Record the created/updated order and IDs guaranteed by the API. Tie-breaking for equal timestamps
  applies only within the observation; a total order including items outside the pages is not guaranteed.
- Change the initial budget for the whole operation to 20 seconds, 20 HTTP requests, and 2 MiB received.
  HTTP requests include failures/transport fallbacks, and the budget is not reset per API.
  The designed ceiling for basic fetching is the 19 calls in the table below, with 1 spare.
  If rate limits/timeouts/extra pagination are needed, stop that item as partial/unknown.

| Fetch | Max HTTP requests | Sharing/limit |
|---|---:|---|
| repository/default branch name | 1 | Shared metadata |
| Latest commit on the default branch | 1 | SHA/subject fetched together |
| Open PR candidates for the current branch | 1 | Match head repo/ref; candidates if ambiguous |
| Most recently created open PR | 1 | 1 page |
| Mixed most-recently-updated PR/Issue candidates | 2 | Up to 2 pages shared for both uses |
| Missing metadata for the focus PR | 1 | 0 if current PR fetch/candidates cover it |
| Latest general comments (up to 2 PRs + 1 Issue) | 6 | Up to 2 per item; the same PR is merged |
| Latest review submission of the focus PR | 2 | Last-page fetch, keeping state/ID |
| Checks on the focus PR's same head | 1 | 1 page; incomplete if more |
| Thread summary of the focus PR | 1 | 1 page/20 items; no extra reply fetches |
| Head recheck of PRs with comments | 2 | 1 each, to detect staleness |
| Total | 19 | Operation limit 20, 1 spare |

- Up to 3 PR candidates and 3 Issue candidates each. Thread details are not fanned out in overview.
  Where current functions such as `checks_for_sha` read all pages, add a bounded call path for
  overview, and never misreport a list as fully fetched. Run/log/artifact fetches are not run in
  the default overview; existing commands are shown as the next entry points.
- If GraphQL is unavailable (403, etc.), stop after one attempt, with no retry and no guessing of
  resolution state from REST. Keep `unknown (graphql unavailable)` and the original error class.
  Don't conclude a proxy cause from a response that can't be identified as permission/rate limit/proxy.
- Prioritize in this order: local → default branch → PR/Issue selection → full bodies of both →
  head recheck → CI → review → thread. If extra pagination or fallbacks reduce headroom, leave
  review/thread unobserved and prioritize bodies; normal full-text display is not turned into an opt-out.
- If the network/API fails, keep and print the local/remote observations already obtained.
  Distinguish 401, 403, 404, timeout, missing, truncated, and not-run, and don't replace them with 0 items.
- A head change while reading PR selection/CI/comments is marked `stale=true` by the final head
  recheck of the focus PR. If detailed observation runs out of budget, record it as unconfirmed.
  overview is not a gate for automatic merge/rebase; strict per-item checks are done with the existing CLI.
- Proposed exit codes: 0 = main observations complete for the chosen online/offline mode,
  1 = summary printed but partly unknown/partial, 2 = invalid input or the summary itself could not be built.
  CI failed/pending or a dirty tree is an "observed state", separate from a failure of overview itself.

The default entry point is read-only. It never runs fetch/pull/rebase/reset/checkout/dispatch/comment/merge.
After overview, users are free to choose existing Git/gh or write APIs.
It is meant as a handy first read, not something enforced via `--help` or "always use this".

## Acceptance criteria and implementation split

- Fixtures for: no arguments / repo at cwd, subdirectory, non-Git, detached HEAD, unborn HEAD,
  dirty, no Git, no upstream, multiple remotes, fork, and CI environment mismatch.
- Distinguish a default branch other than main, a remote SHA with no local object, and an unknown
  age of the local tracking ref. Don't invent behind counts or rebase needs from a SHA mismatch alone.
- Verify no PR, multiple branch matches, different latest-created and latest-updated PRs, equal times,
  no comments, over 100 comments, page limits, permission errors, partial results, and head changes mid-run.
- Verify Issues only with no PR, a closed Issue as most recently updated, body-only Issue updates,
  edits to old comments, no Issue comments, pages mixed with PRs, hitting Issue search/list limits,
  both pages all PRs, confirmed-end 0 items, and comments added/removed at the end.
  Distinguish unobserved from 0 items, and never invent PR/Issue links.
- Fixtures include long bodies with the conclusion at the end, bodies starting with from/to headers,
  Japanese/emoji/newlines, short bodies, and empty bodies. Verify that with no arguments the original
  is kept to the end and not cut at 240 characters or 8 KiB, plus chars/hash/source identity and the failure display for fetch limits.
- Verify the stdout metadata line limit with full bodies kept, removal of secret-bearing remotes/terminal
  control characters, stderr separation, matching text/JSON observations, reverse conversion of
  backslash/real ESC/CR/LF/TAB, both `--json` positions, zero retries on GraphQL failure, exit codes, and existing CLI compatibility.
- With a fake transport/Git fixture, verify zero external API calls for help/offline, zero mutations
  in every run, overall request/byte/time limits, the 19-call maximum configuration and fallbacks
  that never exceed 20, body-first cutoffs, and suppression of duplicate fetches.
- Make the first example at the top of README `python gh_identity.py`. Add the contract to SPEC and
  confirm with pytest/JUnit on Python 3.10–3.14 plus the latest stable CI.
- Implement in this order: (1) bounded overview API, no-argument branch, and renderer; (2) efficient
  latest-comment fetching; (3) fixtures/docs/CI. Remaining review-observation/dispatch work is not mixed in and marked done.

At this PR's stage, this is only a record of the proposal. Until implementation and real-data
verification of fetch counts, time, and output size are done, the no-argument overview is not described as available.

## Review follow-up record (2026-10-11)

Applied S1/S2/N1–N4 from review 5482275860 to the spec. Made explicit: the HTTP budget of 19 calls
plus 1 spare, unobserved state for the mixed Issue window, last-page comments with Link handling,
reversible escaping, text only for overview, no GraphQL retries, and full-text logging in CI. Implementation and behavior checks remain follow-ups.

API references: [Issue comments](https://docs.github.com/en/rest/issues/comments),
[pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api).
