# Workflow-scoped run discovery — Issue #20

## Design decision
Provide a callable, small Python function within `gh_identity.py`, plus a thin CLI JSON adapter. This is not a new server/API product: Python is the implementation core and CLI is the user-facing entry point.

## Scope and acceptance criteria
- [ ] Accept explicit workflow **numeric ID or path** (e.g. `.github/workflows/tests.yml`); resolve and verify identity with existing `workflow()`
- [ ] Filter runs by branch, **exact** head SHA and event; support old runs and preserve run ID and attempt for reruns
- [ ] Reuse existing `run_history()` / `run()` / `jobs()` where contract-compatible; avoid a second incompatible pager
- [ ] Surface clear `complete`, `truncated`, `limit_reason`, pages/items/bytes used, and opaque continuation information where feasible. **Do not present partial results as exhaustive.**
- [ ] Implement bounded automatic continuation/retry within total page/item/byte/time budgets. Prefer server-side filters, smaller page size / smaller response projection when supported, and page-by-page retrieval. If a cap is reached, stop safely and return partial results plus resumable cursor/page; do not silently raise user limits or loop forever.
- [ ] HTTP 403/429, secondary rate limits, permissions, invalid JSON, malformed pagination, and cancellation/timeout remain distinct failures; use limited backoff only when server-supplied retry hints and remaining time budget permit
- [ ] A hard aggregate cap cannot be bypassed by merely splitting requests into smaller pages; on size limit, reduce page size **only where API supports it**, or return a truthful partial result. Avoid duplicated results or gaps between resumed requests and document potential drift when underlying run history changes.
- [ ] Preserve both authenticated gh and anonymous public urllib transport behavior and bounded payload handling
- [ ] Offline regression fixtures for workflow ID/path, filters, history, reruns, pagination/size caps, resume, malformed data, backoff and exhaustion
- [ ] README/SPEC API/CLI examples and output/exit contracts; same-head Python 3.10–3.14 plus 3.x CI

## Explicit boundaries
Keep job log, workflow steps and other Issue #20 items independent. Avoid speculative complete-run reconstruction or unbounded automatic recovery. Keep PR as Draft until code, offline regressions, docs and same-head CI are complete. Do not merge without validation.

Related: Issue #20; merged PR #27.
