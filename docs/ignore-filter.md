# Reuse fleet ignore membership sets

`fleet_checks` creates the ignore set once per repository response rather than
once per check. API requests, order and filtering for reusable ignore collections
remain unchanged. Iterables are consumed once per repository, rather than once per
check; callers should use a reusable collection for multiple repositories.
No dependencies, schema changes or GitHub requests added.

```sh
python -S -m unittest discover -s tests -p test_fleet_ignore_reuse.py
python -S -m scripts.benchmark_ignore_filter
```

The regression checks two mocked repository responses, one ignore iteration per
repository, unchanged retained counts and exactly two checks_for_sha calls.
The benchmark measures only the two filter expressions on synthetic 5000 checks
and 1000 ignored names, verifies output equality, and alternates seven timing
samples after warmup. CPython 3.12.14/Linux: median 59.935 → 0.242 ms.
Raw evidence is in ignore-filter-measurement.json. This intentionally large ignore
list is not representative of the small default list; no end-to-end audit, memory,
CI or build-time improvement is claimed. Full pytest runs in CI.
