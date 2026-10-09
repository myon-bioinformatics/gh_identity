#!/usr/bin/env python3
"""Replay full saved REST responses through the content CLI (stdlib, POSIX).

Exit 0: all bodies match; 1: output/CLI mismatch; 2: invalid evidence or runner error.
This tests a recorded gh transport, not live GitHub connectivity.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshots', type=Path, help='directory containing manifest.json and raw responses')
    args = parser.parse_args()
    source = args.snapshots.resolve()
    cli = Path(__file__).resolve().parents[1] / 'gh_identity.py'
    try:
        manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
        if not isinstance(manifest, list) or not manifest:
            raise ValueError('nonempty manifest list required')
        mismatches = 0
        with tempfile.TemporaryDirectory(prefix='ghi-content-replay-') as temp:
            runner = Path(temp)
            # Only the external gh process is substituted. Production CLI parsing,
            # subprocess execution, response validation and JSON output all run.
            gh = runner / 'gh'
            gh.write_text('#!' + sys.executable + '\n'
                'import os, pathlib, sys\n'
                'if sys.argv[1:] != ["api", "--method", "GET", os.environ["GHI_REPLAY_ENDPOINT"]]:\n'
                ' sys.stderr.write("unexpected replay request\\n"); sys.exit(1)\n'
                'sys.stdout.buffer.write(pathlib.Path(os.environ["GHI_REPLAY_FILE"]).read_bytes())\n',
                encoding='utf-8')
            gh.chmod(0o700)
            for case in manifest:
                path = (source / case['file']).resolve()
                if not path.is_relative_to(source):
                    raise ValueError('snapshot escapes directory')
                raw = path.read_bytes()
                response = json.loads(raw)
                kind = case['kind']
                if kind not in ('pr', 'issue', 'commit'):
                    raise ValueError('unknown kind')
                expected = response['commit']['message'] if kind == 'commit' else response['body']
                if expected is not None and not isinstance(expected, str):
                    raise ValueError('invalid body')
                suffix = 'commits' if kind == 'commit' else 'issues'
                endpoint = f"repos/{case['repository']}/{suffix}/{case['identifier']}"
                if endpoint != case['endpoint']:
                    raise ValueError('manifest endpoint mismatch')
                env = os.environ.copy()
                env.update(PATH=str(runner) + os.pathsep + env.get('PATH', ''),
                           GHI_REPLAY_ENDPOINT=endpoint, GHI_REPLAY_FILE=str(path))
                for selected in (False, True):
                    command = [sys.executable, str(cli), '--transport', 'gh', 'content',
                               case['repository'], kind, str(case['identifier'])]
                    if selected:
                        command += ['--field', 'body']
                    result = subprocess.run(command, env=env, capture_output=True, timeout=30)
                    output = json.loads(result.stdout) if result.returncode == 0 else {}
                    same = (result.returncode == 0 and 'body' in output and
                            output['body'] == expected and output.get('kind') == kind and
                            output.get('repository') == case['repository'])
                    mismatches += not same
                    print(json.dumps({'endpoint': endpoint, 'body_only': selected,
                        'matched': same, 'cli_exit': result.returncode,
                        'raw_sha256': hashlib.sha256(raw).hexdigest(),
                        'body_chars': len(expected) if expected is not None else None,
                        'body_sha256': hashlib.sha256(expected.encode()).hexdigest() if expected is not None else None}))
        return 1 if mismatches else 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
