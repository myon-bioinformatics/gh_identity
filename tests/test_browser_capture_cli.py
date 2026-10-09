"""Dependency-free CLI contract checks, not browser execution certification."""
from pathlib import Path
import subprocess
import sys
import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/capture_browser_dom.py'

@pytest.mark.parametrize('args,code',[(['--help'],0),(['file:///etc/passwd','--output','unused.json'],2),(['https://example.com','--output','unused.json','--timeout-ms','0'],2)])
def test_cli_preflight_without_browser(args,code,tmp_path):
    run=subprocess.run([sys.executable,str(SCRIPT),*args],cwd=tmp_path,capture_output=True,text=True)
    assert run.returncode==code
    assert not list(tmp_path.iterdir())
