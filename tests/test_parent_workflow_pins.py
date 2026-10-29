"""Local rollout contract for reusable workflow uses: refs.

Adopted vendor locks are deliberately independent. Runtime resolution of the
shared vendor tool is not a uses: pin and is outside this contract.
"""
import re
from pathlib import Path

PARENT = "myon-bioinformatics/myon-bioinformatics"
PARENT_WORKFLOW_PIN = "9e3da7ae1846e9604ebc24bb5178f7de094a4e77"
ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".github/workflows").is_dir())
USES_REF = re.compile(
    r"uses:\s*['\"]?" + re.escape(PARENT) + r"/[^\s@'\"]+@([^\s'\"]+)"
)


def test_parent_workflow_uses_refs_are_reviewed_commit():
    pins = []
    for path in sorted((ROOT / ".github/workflows").glob("*")):
        if path.suffix not in (".yml", ".yaml"):
            continue
        pins.extend(USES_REF.findall(path.read_text(encoding="utf-8")))
    assert pins, "shared parent uses: references must remain observable"
    assert set(pins) == {PARENT_WORKFLOW_PIN}, pins
    assert all(re.fullmatch(r"[0-9a-f]{40}", pin) for pin in pins)
