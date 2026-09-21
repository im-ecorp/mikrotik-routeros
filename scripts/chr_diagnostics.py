#!/usr/bin/env python3
"""Read-only runner-context probe of one original dispatch; never rebaseline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

try:
    from scripts.chr_matrix import MatrixRegistry, check_preserved, failure_details, load_manifest, require_snapshot
    from scripts.image_release import REPOSITORY
except ModuleNotFoundError:
    from chr_matrix import MatrixRegistry, check_preserved, failure_details, load_manifest, require_snapshot
    from image_release import REPOSITORY

ORIGINAL_RUN_ID = '35607084875'
ORIGINAL_SOURCE_SHA = '858d85fd17ff720df88a65b44ec65ecf198dd8dd'
# Exact bytes from chr-matrix-snapshot in the original Actions run, not a new baseline.
SNAPSHOT_SHA256 = '4deddff21e16cc5a22d5d1d5c134253395c3e2feb88d1086f699c3c16091772d'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, default=Path('matrix-snapshot/snapshot.json'))
    parser.add_argument('--output-dir', type=Path, default=Path('diagnostic-output'))
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {'status': 'started', 'mode': 'read_only', 'original_run_id': ORIGINAL_RUN_ID,
              'source_sha': ORIGINAL_SOURCE_SHA}
    stage = 'dispatch_validation'
    try:
        executor = os.environ.get('GITHUB_SHA', '')
        if (os.environ.get('GITHUB_REF') != 'refs/heads/main' or
                os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
                os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or
                not re.fullmatch(r'[0-9a-f]{40}', executor) or
                os.environ.get('ORIGINAL_RUN_ID') != ORIGINAL_RUN_ID or
                os.environ.get('ORIGINAL_SOURCE_SHA') != ORIGINAL_SOURCE_SHA):
            raise ValueError('Diagnostic dispatch identity rejected')
        report['executor_sha'] = executor
        stage = 'manifest_validation'
        data = load_manifest(Path(__file__).resolve().parents[1] / 'config/chr-versions.json')
        stage = 'snapshot_read'
        raw = args.snapshot.read_bytes()
        if hashlib.sha256(raw).hexdigest() != SNAPSHOT_SHA256:
            raise ValueError('Original snapshot bytes differ')
        report['snapshot_sha256'] = SNAPSHOT_SHA256
        snapshot = json.loads(raw)
        stage = 'snapshot_validation'
        require_snapshot(snapshot, data, ORIGINAL_SOURCE_SHA, ORIGINAL_RUN_ID, True)
        stage = 'preservation_check'
        # Pull-scoped HTTP GETs only. No copier, Docker, subprocess or login helper.
        check_preserved(MatrixRegistry(), snapshot, report)
        report.update(status='success', preserved_checked=8)
        return 0
    except Exception as error:
        report.update(status='failed', failure=failure_details(error, stage))
        return 1
    finally:
        text = json.dumps(report, indent=2) + '\n'
        (args.output_dir / 'report.json').write_text(text)
        print(text, end='')


if __name__ == '__main__':
    raise SystemExit(main())
