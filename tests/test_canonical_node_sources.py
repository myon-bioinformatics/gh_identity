"""Node sources must use accepted upstream main commits and exact bytes."""
import hashlib
import json
from pathlib import Path


def test_canonical_node_sources():
    root = Path(__file__).resolve().parents[1]
    entries = {e["destination"]: e for e in json.loads((root / "vendor.lock.json").read_text())["files"]}
    expected = {'vendor/py_launch.cjs': '384f5dd532076062f7833038764e9a62322c387c'}
    for path, commit in expected.items():
        entry = entries[path]
        assert entry["ref"] == entry["commit"] == commit
        data = (root / path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == entry["blob_sha"]
