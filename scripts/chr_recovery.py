#!/usr/bin/env python3
"""Bounded recovery of seven failed versions; retain the original qualification identity."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

try:
    from scripts import chr_matrix as matrix
    from scripts.chr_diagnostics import ORIGINAL_RUN_ID, ORIGINAL_SOURCE_SHA, SNAPSHOT_SHA256
except ModuleNotFoundError:
    import chr_matrix as matrix
    from chr_diagnostics import ORIGINAL_RUN_ID, ORIGINAL_SOURCE_SHA, SNAPSHOT_SHA256

FAILED_VERSIONS = ('6.49.21', '6.49.22', '7.23.5', '7.24.2', '7.24.3', '7.24.4', '7.25beta5')

# SHA-256 of untouched successful artifacts from original run 35607084875.
ORIGINAL_REPORT_HASHES = {
    '6.49.17': '51597cd0360de846a69c22e5e0f1344d6d58cc4c11a0360a4d3080d2f7853f19',
    '6.49.18': 'fdc2147569fa9acd161fa305cfd17d6dadfffd37b68b9814eb15df76c7cb9e80',
    '6.49.19': 'cb5eb8f9cec84fdb017bf3cd3b55c2d8d9577733bde7eb9fdb18cd5f842831ee',
    '6.49.20': '5249469f51c9b19e088b070ba9f9778ce7023cb7675e33fa8cccb6e85445a9e5',
    '7.21.5': '49a3606fc70463ff8559b40cc8864922dde7eb94affb9ab80d05e88147cd3764',
    '7.23.4': 'b93a76cad02cb66a09584cabf0ed3b54a8c9f0bf22d1d5afa01299f2bd70b979',
    '7.23.6': '00752d48872cc0b2613fc37a47c71e83e166127355660a5e8bfb8bfce1338472',
    '7.23.7': '1480daa0cc61d77287733e0de3a4eb30f868400c9db92a049382e4573758b887',
    '7.24': '7bca80557dec77a206f9e05a41e1adfab8e0dc1a5cb75f01b1d9fa3e96db3998',
    '7.24.1': 'd9d50a3ca55a23ac1ce1a3f3ab8d370c81cff4d3fdafe2b15228ba1aa4b48ac5',
}
HARNESS_SHA256 = '084197d067d6c98bab936bcec7ffd6a23e9455bab2098e769740d8510f123159'


def source_identity(source):
    def git(*args):
        return subprocess.run(['git', '-C', str(source), *args], check=True,
                              capture_output=True, timeout=60).stdout.strip()
    if (git('rev-parse', 'HEAD').decode() != ORIGINAL_SOURCE_SHA or
            git('status', '--porcelain', '--untracked-files=all', '--ignored')):
        raise ValueError('Original source tree is not pristine')
    digest = hashlib.sha256((source / 'tests/docker-integration.py').read_bytes()).hexdigest()
    if digest != HARNESS_SHA256:
        raise ValueError('Original harness differs')
    return digest


def load_originals(directory):
    reports = []
    for version, expected in ORIGINAL_REPORT_HASHES.items():
        raw = (directory / ('chr-matrix-version-' + version) / 'report.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Original successful report bytes differ')
        report = json.loads(raw)
        if report.get('version') != version:
            raise ValueError('Original report version differs')
        reports.append(report)
    return reports


def load_inputs(source, path, originals, report):
    report['stage'] = 'snapshot_read'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SNAPSHOT_SHA256:
        raise ValueError('Original snapshot bytes differ')
    report['stage'] = 'harness_identity'
    harness_hash = source_identity(source)
    report['stage'] = 'manifest_validation'
    data = matrix.load_manifest(source / 'config/chr-versions.json')
    snapshot = json.loads(raw)
    report['stage'] = 'snapshot_validation'
    matrix.require_snapshot(snapshot, data, ORIGINAL_SOURCE_SHA, ORIGINAL_RUN_ID, True)
    reports = load_originals(originals)
    return data, snapshot, reports, harness_hash


def preflight(registry, data, snapshot, originals, harness_hash, report):
    """Read every original success and every remaining destination before login/writes."""
    matrix.require_snapshot(snapshot, data, ORIGINAL_SOURCE_SHA, ORIGINAL_RUN_ID, True)
    original_data = dict(data, versions=[r for r in data['versions'] if r['version'] not in FAILED_VERSIONS])
    qualified = matrix.require_reports(original_data, originals, ORIGINAL_SOURCE_SHA,
                                       ORIGINAL_RUN_ID, harness_hash)
    report['stage'] = 'preservation_check'
    matrix.check_preserved(registry, snapshot, report)
    report['stage'] = 'source_preflight'
    desired = {}
    readbacks = []
    for row in data['versions']:
        version = row['version']; tag = matrix.source_tag(ORIGINAL_SOURCE_SHA, version)
        existing = {image: registry.manifest(image, tag)[0] for image in (matrix.HUB, matrix.GHCR)}
        digests = {d for d in existing.values() if d is not None}
        if len(digests) > 1:
            raise ValueError('Cross-registry immutable sources disagree')
        digest = next(iter(digests), None)
        desired[version] = digest
        for image, current in existing.items():
            baseline = snapshot['before'][f'{image}:{tag}']
            if baseline is not None and baseline != current:
                raise ValueError('Immutable source drifted')
            if current is not None:
                matrix.verify_image(registry, image, tag, current, row, ORIGINAL_SOURCE_SHA)
        if version in qualified:
            digest = qualified[version]['digest']
            for image in (matrix.HUB, matrix.GHCR):
                for alias in (tag, version, 'v' + version):
                    readbacks.append(matrix.verify_image(registry, image, alias, digest, row, ORIGINAL_SOURCE_SHA))
        else:
            for image in (matrix.HUB, matrix.GHCR):
                for alias in (version, 'v' + version):
                    current = registry.manifest(image, alias)[0]
                    baseline = snapshot['before'][f'{image}:{alias}']
                    if current != baseline and (digest is None or current != digest):
                        raise ValueError('Target drifted from the original dispatch snapshot')
    for image in (matrix.HUB, matrix.GHCR):
        current = registry.manifest(image, 'latest')[0]
        baseline = snapshot['before'][f'{image}:latest']
        if current != baseline and (desired[data['latest']] is None or current != desired[data['latest']]):
            raise ValueError('Target drifted from the original dispatch snapshot')
    report.update(reused_versions=sorted(qualified), original_readbacks=readbacks)

def identity():
    executor = os.environ.get('GITHUB_SHA', '')
    run = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if (os.environ.get('GITHUB_REF') != 'refs/heads/main' or
            os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
            os.environ.get('GITHUB_REPOSITORY') != matrix.REPOSITORY or
            os.environ.get('APPROVE_VERSION_OVERWRITE') != 'true' or
            not re.fullmatch(r'[0-9a-f]{40}', executor) or executor == ORIGINAL_SOURCE_SHA or
            not re.fullmatch(r'[1-9][0-9]*', run) or run == ORIGINAL_RUN_ID or
            not re.fullmatch(r'[1-9][0-9]*', attempt)):
        raise ValueError('Recovery dispatch identity rejected')
    return {'executor_sha': executor, 'run_id': run, 'run_attempt': attempt,
            'original_run_id': ORIGINAL_RUN_ID, 'snapshot_sha256': SNAPSHOT_SHA256}


def recover_variant(registry, source, output, row, snapshot, harness_hash, report):
    version = row['version']; tag = matrix.source_tag(ORIGINAL_SOURCE_SHA, version)
    allowed = {(image, alias) for image in (matrix.HUB, matrix.GHCR) for alias in (tag, version, 'v' + version)}
    before = {f'{image}:{alias}': snapshot['before'][f'{image}:{alias}'] for image, alias in allowed}

    def build(row, tag):
        source_identity(source)
        if any(registry.manifest(image, tag)[0] is not None for image in (matrix.HUB, matrix.GHCR)):
            raise ValueError('Immutable source appeared before build')
        metadata = output / 'build-metadata.json'
        matrix.run_command(['docker', 'buildx', 'build', '--platform', ','.join(matrix.PLATFORMS),
            '--push', '--provenance=mode=max', '--sbom=true',
            '--build-arg', 'ROUTEROS_VERSION=' + version,
            '--build-arg', 'ROUTEROS_SHA256=' + row['checksumSHA256'],
            '--build-arg', 'SOURCE_REVISION=' + ORIGINAL_SOURCE_SHA,
            '--build-arg', 'WRAPPER_VERSION=current-chr',
            '--build-arg', 'SOURCE_URL=https://github.com/' + matrix.REPOSITORY,
            '--metadata-file', str(metadata), '--tag', matrix.HUB + ':' + tag,
            '--file', str(source / 'Dockerfile'), str(source)], 2400, stage='build')
        return json.loads(metadata.read_text())['containerimage.digest']

    def runtime(image):
        source_identity(source)
        matrix.run_command(['docker', 'pull', '--platform', 'linux/amd64', image], 600, stage='pull')
        evidence = output / 'private-runtime'
        try:
            matrix.run_command(['python3', str(source / 'tests/docker-integration.py'), '--run',
                '--image', image, '--output-dir', str(evidence)], 960, stage='runtime')
        finally:
            path = evidence / 'report.json'
            if path.exists():
                try:
                    raw = json.loads(path.read_text())
                    if not isinstance(raw, dict):
                        raise ValueError('Expected harness report object')
                except (OSError, ValueError):
                    report['runtime'] = {'status': 'failed', 'failure': {
                        'stage': 'harness', 'reason': 'invalid_report'}}
                else:
                    # Identical allowlist to the instrumented executor; never copy raw errors.
                    report['runtime'] = {k: raw[k] for k in ('status', 'image', 'image_id', 'image_labels',
                        'checks', 'shutdowns', 'cleanup_errors', 'harness_sha256', 'duration_seconds',
                        'dhcp_diagnostics', 'health_diagnostics', 'network_diagnostics') if k in raw}
                    if raw.get('status') != 'passed':
                        report['runtime']['failure'] = matrix.runtime_failure(raw)
        return report['runtime']

    report['stage'] = 'registry_login'
    with matrix.MatrixCopy(allowed) as copier:
        matrix.publish_version(registry, copier.copy_image, build, runtime, row, ORIGINAL_SOURCE_SHA,
                               before, True, harness_hash, report)
    report['stage'] = 'preservation_check'
    matrix.check_preserved(registry, snapshot, report)


def latest_variant_attempts(provenance):
    """Use all executed jobs, not available uploads or rerun dependency summaries."""
    run_url = f'https://api.github.com/repos/{matrix.REPOSITORY}/actions/runs/{provenance["run_id"]}'
    result = subprocess.run(['gh', 'api', '--hostname', 'github.com', '--paginate', '--slurp',
        f'repos/{matrix.REPOSITORY}/actions/runs/{provenance["run_id"]}/jobs?filter=all&per_page=100'],
        check=True, capture_output=True, text=True, timeout=120)
    pages = json.loads(result.stdout)
    jobs = [job for page in pages for job in page['jobs']]
    if (not pages or any(page['total_count'] != len(jobs) for page in pages) or
            len({job['id'] for job in jobs}) != len(jobs)):
        raise ValueError('Incomplete or duplicate Actions job evidence')
    names = {f'variant ({version})': version for version in FAILED_VERSIONS}
    latest = {}
    seen = set()
    for job in jobs:
        attempt = job.get('run_attempt')
        if (job.get('run_id') != int(provenance['run_id']) or job.get('run_url') != run_url or
                job.get('head_sha') != provenance['executor_sha'] or
                type(attempt) is not int or not 1 <= attempt <= int(provenance['run_attempt'])):
            raise ValueError('Foreign or future Actions job evidence')
        version = names.get(job['name'])
        if version is None:
            if job['name'] not in ('preflight', 'aggregate'):
                raise ValueError('Unknown recovery job')
            continue
        if (version, attempt) in seen:
            raise ValueError('Duplicate variant attempt')
        seen.add((version, attempt))
        if version not in latest or attempt > latest[version]['run_attempt']:
            latest[version] = job
    if (set(latest) != set(FAILED_VERSIONS) or
            any(job.get('status') != 'completed' or job.get('conclusion') != 'success'
                for job in latest.values())):
        raise ValueError('Latest variant execution did not succeed')
    return {version: str(job['run_attempt']) for version, job in latest.items()}


def load_recovered(directory, provenance):
    attempts = latest_variant_attempts(provenance)
    selected = {}
    seen = set()
    for path in sorted(directory.glob('*/report.json')):
        report = json.loads(path.read_text())
        version = report.get('version')
        origin = report.get('recovery', {})
        attempt = origin.get('run_attempt', '')
        if (version not in FAILED_VERSIONS or not isinstance(attempt, str) or
                not re.fullmatch(r'[1-9][0-9]*', attempt) or int(attempt) > int(provenance['run_attempt']) or
                any(origin.get(k) != v for k, v in provenance.items() if k != 'run_attempt') or
                path.parent.name != f'chr-recovery-version-{version}-{attempt}' or
                (version, attempt) in seen):
            raise ValueError('Foreign or duplicate recovery artifact')
        seen.add((version, attempt))
        if version not in selected or int(attempt) > int(selected[version]['recovery']['run_attempt']):
            selected[version] = report
    if (set(selected) != set(FAILED_VERSIONS) or
            any(report['recovery']['run_attempt'] != attempts[version]
                for version, report in selected.items())):
        raise ValueError('Missing latest executed recovery artifact')
    return [selected[v] for v in FAILED_VERSIONS]


def recover_aggregate(registry, data, originals, recovered, snapshot, harness_hash, report):
    reports = originals + recovered
    # Validate complete qualification before even creating a write-scoped copier.
    matrix.require_reports(data, reports, ORIGINAL_SOURCE_SHA, ORIGINAL_RUN_ID, harness_hash)
    report['stage'] = 'aggregation'
    with matrix.MatrixCopy({(image, 'latest') for image in (matrix.HUB, matrix.GHCR)}) as copier:
        matrix.aggregate(registry, copier.copy_image, data, reports, ORIGINAL_SOURCE_SHA,
                         ORIGINAL_RUN_ID, harness_hash, snapshot, True, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('preflight', 'variant', 'aggregate'))
    parser.add_argument('--source-tree', type=Path, default=Path('original-source'))
    parser.add_argument('--snapshot', type=Path, default=Path('original-snapshot/snapshot.json'))
    parser.add_argument('--originals', type=Path, default=Path('original-reports'))
    parser.add_argument('--reports', type=Path, default=Path('recovery-reports'))
    parser.add_argument('--output-dir', type=Path, default=Path('recovery-output'))
    args = parser.parse_args(argv)
    # A repeated invocation must never erase its predecessor's evidence.
    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print('{"status":"failed","reason":"evidence_exists"}')
        return 1
    report = {'status': 'started', 'command': args.command, 'source_sha': ORIGINAL_SOURCE_SHA,
              'run_id': ORIGINAL_RUN_ID, 'stage': 'dispatch_validation'}
    try:
        report['recovery'] = identity()
        source = args.source_tree.resolve()
        output = args.output_dir.resolve()
        if output.is_relative_to(source):
            raise ValueError('Evidence must be outside original source tree')
        if args.command == 'variant':
            report['stage'] = 'version_validation'
            version = os.environ.get('CHR_VERSION', '')
            if version not in FAILED_VERSIONS:
                raise ValueError('Version is outside the seven-version recovery scope')
            report['version'] = version
        data, snapshot, originals, harness_hash = load_inputs(source, args.snapshot, args.originals, report)
        registry = matrix.MatrixRegistry()
        preflight(registry, data, snapshot, originals, harness_hash, report)
        if args.command == 'preflight':
            report.update(status='success', count=7)
        elif args.command == 'variant':
            row = next(r for r in data['versions'] if r['version'] == version)
            recover_variant(registry, source, output, row, snapshot, harness_hash, report)
        else:
            report['stage'] = 'aggregation'
            recovered = load_recovered(args.reports, report['recovery'])
            report['recovery_attempts'] = {r['version']: r['recovery']['run_attempt'] for r in recovered}
            recover_aggregate(registry, data, originals, recovered, snapshot, harness_hash, report)
        return 0
    except Exception as error:
        report.update(status='failed', failure=matrix.failure_details(error, report['stage']))
        return 1
    finally:
        (args.output_dir / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'status': report['status'], 'command': args.command}))


if __name__ == '__main__':
    raise SystemExit(main())
