"""Run the recorded public-response CLI probe as one integration smoke check."""
from pathlib import Path
import os
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.name != 'posix', reason='recorded gh executable uses a POSIX shebang')
def test_recorded_public_content_through_cli():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'scripts/check_content_snapshots.py'),
                             str(root / 'tests/fixtures/content_snapshots')],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
