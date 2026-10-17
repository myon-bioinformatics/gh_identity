"""Subprocess mutation tests: each mutant must fail at least one targeted pytest node."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "gh_identity.py"

MUTATIONS = [
    ("approval_uses_max", "approved_at[x[\"author\"]] = max(filter(None", "approved_at[x[\"author\"]] = min(filter(None",
     "tests/test_gh_identity_merge_readiness.py::test_approval_timestamp_uses_latest"),
    ("dismissed_not_approved", 'if x.get("state") == "APPROVED":',
     'if x.get("state") in ("APPROVED", "DISMISSED"):',
     "tests/test_gh_identity_merge_readiness.py::test_dismissed_does_not_supersede_findings"),
    ("paired_finding_text", '"text": " ".join(lab["text"].split())[:200]',
     '"text": " ".join(labels[-1]["text"].split())[:200]',
     "tests/test_gh_identity_merge_readiness.py::test_paired_label_and_text_per_finding"),
    ("commented_not_approved", 'if x.get("state") == "APPROVED":',
     'if x.get("state") in ("APPROVED", "COMMENTED", "CHANGES_REQUESTED"):',
     "tests/test_gh_identity_merge_readiness.py::test_commented_does_not_supersede"),
    ("blocking_thread_ignores_outdated",
     '(f["after_head"] or (f["thread"] and not f["thread"]["outdated"]))',
     '(f["after_head"] or f["thread"])',
     "tests/test_gh_identity_merge_readiness.py::test_outdated_thread_blocking_finding_is_not_blocker"),
    ("head_message_cap_100", ")[0][:100]", ")[0]",
     "tests/test_gh_identity_merge_readiness.py::test_head_message_hundred_char_cap"),
    ("after_head_strict_gt", "at > _when(head_at)", "at >= _when(head_at)",
     "tests/test_gh_identity_merge_readiness.py::test_after_head_strictly_after_commit_time"),
    ("supersede_strict_gt", "later > at", "later >= at",
     "tests/test_gh_identity_merge_readiness.py::test_supersede_requires_approval_after_finding"),
    ("changes_requested_head_only", 'blockers += [c for c in changes_requested if c["commit_id"] == sha]',
     "blockers += list(changes_requested)",
     "tests/test_gh_identity_merge_readiness.py::test_changes_requested_on_old_commit_is_not_blocker"),
    ("blocker_respects_outdated", 'not f["thread"]["outdated"]',
     "False",
     "tests/test_gh_identity_merge_readiness.py::test_inline_findings_threads_and_states"),
    ("should_not_block", 'f["label"] in ("Must", "Blocking")',
     'f["label"] in ("Must", "Blocking", "Should")',
     "tests/test_gh_identity_merge_readiness.py::test_empty_blocking_section_is_not_a_finding"),
    ("must_still_blocks", 'f["label"] in ("Must", "Blocking")',
     'f["label"] in ("Blocking",)',
     "tests/test_gh_identity_merge_readiness.py::test_must_after_head_blocks_without_changes_requested"),
    ("dismissed_latest", 'if x.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):',
     'if x.get("state") in ("APPROVED", "CHANGES_REQUESTED"):',
     "tests/test_gh_identity_merge_readiness.py::test_dismissed_overrides_changes_requested"),
    ("mergeable_unstable", '("dirty", "blocked", "behind", "unstable")',
     '("dirty", "blocked", "behind")',
     "tests/test_gh_identity_merge_readiness.py::test_mergeable_state_reasons"),
    ("mergeable_behind", '("dirty", "blocked", "behind", "unstable")',
     '("dirty", "blocked", "unstable")',
     "tests/test_gh_identity_merge_readiness.py::test_mergeable_state_reasons"),
    ("mergeable_blocked", '("dirty", "blocked", "behind", "unstable")',
     '("dirty", "behind", "unstable")',
     "tests/test_gh_identity_merge_readiness.py::test_mergeable_state_reasons"),
    ("unresolved_skips_outdated", "not t[\"outdated\"]",
     "True",
     "tests/test_gh_identity_merge_readiness.py::test_outdated_open_threads_do_not_count_as_unresolved_reason"),
    ("finding_text_200", '"text": " ".join(lab["text"].split())[:200]',
     '"text": " ".join(lab["text"].split())',
     "tests/test_gh_identity_merge_readiness.py::test_finding_text_and_head_messages_truncated"),
    ("head_messages_tail", "][-5:]", "][:5]",
     "tests/test_gh_identity_merge_readiness.py::test_finding_text_and_head_messages_truncated"),
    ("ccr_int_comment_ids", "ids = [i for i in x.get(\"comment_ids\") or [] if type(i) is int]",
     "ids = list(x.get(\"comment_ids\") or [])",
     "tests/test_gh_identity_merge_readiness.py::test_ccr_comment_id_types_and_outdated_flag"),
    ("ccr_thread_id_zero", "ids[0] if ids else len(threads)",
     "0",
     "tests/test_gh_identity_merge_readiness.py::test_ccr_comment_id_types_and_outdated_flag"),
    ("ccr_outdated_bool", '"is_outdated": bool(x.get("outdated"))',
     '"is_outdated": False',
     "tests/test_gh_identity_merge_readiness.py::test_ccr_comment_id_types_and_outdated_flag"),
    ("fleet_wait_parse", "return ns.wait", "return 0.0",
     "tests/test_gh_identity_fleet_wait.py::test_wait_polls_outside_one_shared_scope"),
    ("merge_readiness_bounded", '"merge_readiness","fleet_checks"',
     '"fleet_checks"',
     "tests/test_gh_identity_merge_readiness.py::test_merge_readiness_is_bounded"),
    ("empty_blocking_section", 'if kind and kind[0]=="severity":',
     'if False and kind and kind[0]=="severity":',
     "tests/test_gh_identity_merge_readiness.py::test_empty_blocking_section_is_not_a_finding"),
    ("numbered_should_label", "(?:\\s+\\d+)?(?=\\s*(?:[:：—–]|-\\s|$))",
     "(?=\\s*(?:[:：—–]|-\\s|$))",
     "tests/test_gh_identity_merge_readiness.py::test_severity_labels_accept_dashes_and_must[- Should 1\\uff1arename the helper-Should]"),
    ("ignore_fence_in_section", "if _FENCE_START.match(lines[i]):i=_skip_fence(lines,i);continue",
     "if False and _FENCE_START.match(lines[i]):i=_skip_fence(lines,i);continue",
     "tests/test_gh_identity_merge_readiness.py::test_codeblock_inside_section_ignored"),
    ("non_severity_heading_break", 'if inner:break',
     "if False and inner:break",
     "tests/test_gh_identity_merge_readiness.py::test_non_severity_heading_ends_section"),
    ("nested_bullet_single", "if ni is not None and ni<=indent:break",
     "if ni is not None:break",
     "tests/test_gh_identity_merge_readiness.py::test_nested_bullets_are_one_finding"),
]


def _run_mutant(name, old, new, nodeid):
    text = SOURCE.read_text(encoding="utf-8")
    if old not in text:
        pytest.fail(f"{name}: anchor not found")
    mutant = text.replace(old, new, 1)
    if mutant == text:
        pytest.fail(f"{name}: replace had no effect")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "gh_identity.py").write_text(mutant, encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = td
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", nodeid, "-q", "--tb=no", "-o", f"pythonpath={td}"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
    if proc.returncode == 0:
        pytest.fail(f"{name}: mutation survived\nstdout={proc.stdout}\nstderr={proc.stderr}")


@pytest.mark.parametrize("name,old,new,nodeid", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_mutation_killed(name, old, new, nodeid):
    _run_mutant(name, old, new, nodeid)
