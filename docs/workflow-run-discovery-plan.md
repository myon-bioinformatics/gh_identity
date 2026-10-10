# Workflow-scoped run discovery — Issue #20

## Scope
Add a bounded, read-only Python API and thin CLI adapter to discover runs for an **explicit workflow ID or path**. Reuse existing `workflow()`, `run_history()`, `run()`, and `jobs()` instead of duplicating identity and pagination code.

## Acceptance criteria
- [ ] Explicit workflow ID/path, with unambiguous resolution and validation
- [ ] Branch, exact head SHA and event filters; old runs and rerun attempts included
- [ ] Bounded pages/items/bytes/time with truthful `complete` / `truncated`
- [ ] Stable JSON schema and CLI exit/error contract, preserving gh/urllib public read
- [ ] Offline fixtures for filtering, pagination boundaries, missing workflow, reruns, malformed responses and failures
- [ ] README and SPEC updated; Python 3.10–3.14 and 3.x same-head CI green

## Boundaries
Do not mark the other Issue #20 checklist entries complete. Do not change writes or reimplement step links/logs. Do not merge until implementation, tests and CI are verified.

Refs #20. Depends on merged #27.
