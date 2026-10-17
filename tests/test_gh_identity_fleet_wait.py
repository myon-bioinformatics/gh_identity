"""fleet-checks --wait: each poll runs in its own budget, so waiting longer than one poll's timeout works."""
from unittest.mock import patch

import pytest
import gh_identity as g


def run(argv, states):
    seen = []

    def fake(roots, *, ignore, steps, transport, timeout):
        seen.append((g._BUDGET.get(), timeout))
        state = states.pop(0)
        return {"schema": "gh-identity-fleet-checks/1", "state": state, "rows": [], "observed_at": "t"}
    with patch.object(g, "fleet_checks", side_effect=fake), patch.object(g.time, "sleep"):
        return g.main(argv), seen


def test_wait_polls_outside_one_shared_scope():
    code, seen = run(["fleet-checks", ".", "--wait", "600", "--interval", "1"], ["pending", "pending", "green"])
    assert code == 0 and len(seen) == 3
    assert all(budget is None for budget, _ in seen)  # the bounded fleet_checks opens one scope per poll
    assert {timeout for _, timeout in seen} == {30.0}


def test_wait_passes_timeout_per_poll_and_rejects_other_limits():
    code, seen = run(["fleet-checks", ".", "--wait", "600", "--timeout", "45"], ["failed"])
    assert code == 1 and seen == [(None, 45.0)]
    assert g.main(["fleet-checks", ".", "--wait", "60", "--max-pages", "3"]) == 2


def test_wait_equals_form_uses_per_poll_budget():
    code, seen = run(["fleet-checks", ".", "--wait=600", "--timeout", "45", "--interval", "1"], ["pending", "green"])
    assert code == 0 and len(seen) == 2
    assert all(budget is None for budget, _ in seen)
    assert {timeout for _, timeout in seen} == {45.0}


def test_without_wait_keeps_one_scope():
    code, seen = run(["fleet-checks", "."], ["green"])
    assert code == 0 and seen[0][0] is not None


@pytest.mark.parametrize("argv,per_poll", [
    (["fleet-checks", ".", "--wait", "0", "--wait", "600", "--interval", "1"], True),
    (["fleet-checks", ".", "--wait", "600", "--wait", "0"], False),
    (["fleet-checks", ".", "--wai", "600"], False),
    (["fleet-checks", ".", "--wa=600"], False),
])
def test_wait_uses_argparse_not_string_heuristics(argv, per_poll):
    states = ["green"] if not per_poll else ["pending", "green"]
    code, seen = run(argv, states)
    assert code == 0
    if per_poll:
        assert len(seen) == 2 and all(budget is None for budget, _ in seen)
    else:
        assert len(seen) == 1 and seen[0][0] is not None


def test_fleet_checks_wait_value_invalid_and_missing():
    assert g._fleet_checks_wait_value(["fleet-checks", ".", "--wait", "abc"]) == 0.0
    assert g._fleet_checks_wait_value(["fleet-checks", ".", "--wait=abc"]) == 0.0
    assert g._fleet_checks_wait_value(["fleet-checks", ".", "--wait"]) == 0.0
    assert g._fleet_checks_wait_value(["wait-checks", "o/r", "a"]) is None
