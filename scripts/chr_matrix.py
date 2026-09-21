#!/usr/bin/env python3
"""Reviewed website CHR scope; independent of the tested-default publisher."""
import json
from pathlib import Path
import re

try:
    from scripts.image_release import HUB, GHCR, REPOSITORY, Registry, RegistryError, Skopeo, platforms
except ModuleNotFoundError:  # Direct CLI invocation.
    from image_release import HUB, GHCR, REPOSITORY, Registry, RegistryError, Skopeo, platforms

PLATFORMS = ['linux/amd64', 'linux/arm64']
PRESERVED_TAGS = ('7.21.4', 'v7.21.4', '7.21.4-r1.0.0',
                  'sha-f6108c7672800618fd78e407ef79fcd15f69eef8')


class MatrixRegistry(Registry):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.configs = {}

    def config(self, image, digest):
        # Cache only digest-verified immutable configs, never mutable tag reads.
        key = (image, digest)
        if key not in self.configs:
            self.configs[key] = super().config(image, digest)
        return self.configs[key]

    def request(self, image, path):
        self.attempt = 1
        result = super().request(image, path)
        if result[0] == 401:
            # Build plus full guest qualification can outlive a pull token.
            self.tokens.pop(image, None)
            self.attempt = 2
            result = super().request(image, path)
        return result


def expected_labels(row, sha):
    return {'org.opencontainers.image.version': 'current-chr',
            'org.opencontainers.image.revision': sha,
            'org.opencontainers.image.source': 'https://github.com/' + REPOSITORY,
            'io.mikrotik-routeros.seed.version': row['version'],
            'io.mikrotik-routeros.seed.sha256': row['checksumSHA256']}


# Exact private harness error -> public codes. Never interpolate/copy the error:
# even a known message with an arbitrary suffix must use the unknown fallback.
HARNESS_FAILURES = {
    'Persisted identity differs': ('persistence', 'identity_mismatch'),
    'Unexpected password reset on persisted guest': ('authentication', 'persisted_password_reset'),
    'Timed out: Docker health healthy': ('health', 'healthy_timeout'),
    'Recovered DHCP lease differs from Docker address': ('dhcp', 'recovery_address_mismatch'),
    'Guest HTTP response not successful': ('protocols', 'http_response_failed'),
    'Fresh guest version differs from image seed version label': ('seed', 'version_mismatch'),
    'Serial timeout at login completion; authentication is not logged': ('authentication', 'serial_timeout'),
    'Guest shutdown was not clean; see shutdown evidence': ('shutdown', 'unclean_shutdown'),
    'Isolation requires zero replies and a counted firewall drop': ('isolation', 'drop_check_failed'),
    'Cold disk backup differs from independent readback': ('backup', 'readback_mismatch'),
    'Timed out: Docker health unhealthy': ('health', 'unhealthy_timeout'),
    'DHCP disable was not confirmed': ('dhcp', 'disable_unconfirmed'),
    'DHCP enable was not confirmed': ('dhcp', 'enable_unconfirmed'),
    'Guest did not obtain Docker IP via DHCP': ('dhcp', 'initial_address_mismatch'),
    'Guest SSH banner missing': ('protocols', 'ssh_banner_missing'),
    'Guest UDP DNS record differs': ('protocols', 'dns_record_mismatch'),
    'Image seed version label is missing or empty': ('seed', 'label_missing'),
    'Restart changed guest version': ('persistence', 'restart_version_mismatch'),
    'Recreation changed guest version': ('persistence', 'recreate_version_mismatch'),
    'Missing guest-origin shutdown event': ('shutdown', 'guest_event_missing'),
    'Container still running after stop': ('shutdown', 'container_still_running'),
    'Stable disk missing': ('backup', 'disk_missing'),
    'Cold copy requires a stopped guest': ('backup', 'guest_not_stopped'),
    'Missing scoped firewall counter': ('isolation', 'counter_missing'),
    'Bridge isolation options differ': ('isolation', 'bridge_options_mismatch'),
    'Expected IPv4 non-internal bridge for Docker port publishing': ('isolation', 'bridge_type_mismatch'),
    'Integration exceeded 840 seconds or was interrupted': ('harness', 'interrupted'),
    'Timed out: serial socket': ('serial', 'socket_timeout'),
    'Fresh-password flow not exercised': ('authentication', 'fresh_password_flow_missing'),
}
for _label in ('login', 'password', 'login completion', 'repeat fresh password', 'fresh password completion'):
    HARNESS_FAILURES[f'Serial timeout at {_label}; authentication is not logged'] = ('authentication', 'serial_timeout')
    HARNESS_FAILURES[f'QEMU serial closed at {_label}'] = ('authentication', 'serial_closed')
for _label in ('tagged command completion', 'tagged command prompt', 'IT_IDENTITY', 'IT_IDENTITY prompt',
               'IT_VERSION', 'IT_VERSION prompt', 'IT_ADDRESS', 'IT_ADDRESS prompt', 'IT_DHCP',
               'IT_DHCP prompt', 'IT_ISOLATION', 'IT_ISOLATION prompt'):
    HARNESS_FAILURES[f'Serial timeout at {_label}; authentication is not logged'] = ('serial', 'readback_timeout')
    HARNESS_FAILURES[f'QEMU serial closed at {_label}'] = ('serial', 'readback_closed')
for _port in ('80/tcp', '22/tcp', '53/udp'):
    for _message, _reason in (('Missing published port', 'published_port_missing'),
                             ('Port is not exclusively loopback bound', 'non_loopback_binding'),
                             ('Missing host port', 'host_port_missing')):
        HARNESS_FAILURES[f'{_message}: {_port}'] = ('protocols', _reason)


def runtime_failure(raw):
    error = raw.get('error')
    codes = {('RuntimeError: ' + message): value for message, value in HARNESS_FAILURES.items()}
    stage, reason = codes.get(error, ('harness', 'unclassified_failure')) if isinstance(error, str) else (
        'harness', 'unclassified_failure')
    return {'stage': stage, 'reason': reason}


def require_runtime(report, row, sha, image, harness_hash):
    labels = report.get('image_labels', {})
    if (report.get('status') != 'passed' or report.get('image') != image or
            report.get('cleanup_errors') != [] or report.get('harness_sha256') != harness_hash or
            any(labels.get(k) != v for k, v in expected_labels(row, sha).items()) or
            not any(c.get('fresh_guest_matches_seed') is True and c.get('seed_version') == row['version']
                    for c in report.get('checks', [])) or
            [s.get('label') for s in report.get('shutdowns', [])] != ['restart', 'recreate', 'final']):
        raise ValueError('Exact-digest full runtime qualification is incomplete')


def require_reports(data, reports, sha, run_id, harness_hash, *, source_image=HUB):
    expected = {r['version']: r for r in data['versions']}
    if len(reports) != len(expected) or {r.get('version') for r in reports} != set(expected):
        raise ValueError('Require exactly one report for each of the 17 versions')
    for report in reports:
        if (report.get('status') != 'success' or report.get('source_sha') != sha or
                report.get('run_id') != run_id or report.get('platforms') != PLATFORMS or
                report.get('runtime_tested') != ['linux/amd64'] or
                report.get('build_only') != ['linux/arm64'] or
                not re.fullmatch(r'sha256:[0-9a-f]{64}', report.get('digest', ''))):
            raise ValueError('Unqualified or foreign matrix report')
        require_runtime(report['runtime'], expected[report['version']], sha,
                        source_image + '@' + report['digest'], harness_hash)
    return {r['version']: r for r in reports}


def validate_manifest(data):
    expected = ({f'6.49.{n}' for n in range(17, 23)} | {'7.21.5', '7.24', '7.25beta5'} |
                {f'7.23.{n}' for n in range(4, 8)} | {f'7.24.{n}' for n in range(1, 5)})
    rows = data.get('versions', [])
    if data.get('schema') != 1 or data.get('latest') != '7.24.4':
        raise ValueError('Unreviewed manifest schema or latest')
    if len(rows) != 17 or {r['version'] for r in rows} != expected:
        raise ValueError('Manifest must contain exactly the 17 reviewed website versions')
    for row in rows:
        version = row['version']
        if (row.get('architecture') != 'x86' or
                row.get('url') != f'https://download.mikrotik.com/routeros/{version}/chr-{version}.vdi.zip' or
                not re.fullmatch(r'[0-9a-f]{64}', row.get('checksumSHA256', '')) or
                not row.get('channels') or
                set(row['channels']) - {'stable', 'longTerm', 'development'}):
            raise ValueError('Invalid reviewed x86 VDI artifact')
    return data


def load_manifest(path):
    return validate_manifest(json.loads(Path(path).read_text()))


def source_tag(sha, version):
    if not re.fullmatch(r'[0-9a-f]{40}', sha) or not re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+|beta[0-9]+)?', version):
        raise ValueError('Invalid source identity')
    return f'sha-{sha}-chr-{version}'


def verify_image(registry, image, tag, digest, row, sha):
    actual, manifest = registry.manifest(image, tag)
    if actual != digest or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest or ''):
        raise ValueError('Source or destination digest mismatch')
    platforms(manifest)
    for entry in manifest['manifests']:
        if entry['platform']['os'] == 'unknown':
            continue
        config = registry.config(image, entry['digest'])
        if any(config.get(k) != entry['platform'][k] for k in ('os', 'architecture')):
            raise ValueError('Platform config contradicts index')
        labels = config.get('config', {}).get('Labels', {})
        if any(labels.get(k) != v for k, v in expected_labels(row, sha).items()):
            raise ValueError('Source/seed labels differ from reviewed build')
    return {'image': f'{image}:{tag}', 'digest': digest, 'platforms': PLATFORMS}


def publish_version(registry, copy_image, build, runtime, row, sha, before, approve, harness_hash, report):
    version = row['version']; tag = source_tag(sha, version)
    report.update(status='preflight', stage='source_preflight', version=version, source_sha=sha,
                  platforms=PLATFORMS, runtime_tested=['linux/amd64'], build_only=['linux/arm64'],
                  before=before, images=[])
    # Existing sources are immutable: validate and reuse, never rebuild on reruns.
    existing = {image: registry.manifest(image, tag)[0] for image in (HUB, GHCR)}
    digests = {d for d in existing.values() if d is not None}
    if len(digests) > 1:
        raise ValueError('Cross-registry immutable sources disagree')
    for image, current in existing.items():
        baseline = before[f'{image}:{tag}']
        if baseline is not None and current != baseline:
            raise ValueError('Immutable source drifted')
    if digests:
        digest = next(iter(digests))
        source = next(image for image, d in existing.items() if d)
        verify_image(registry, source, tag, digest, row, sha)
        if existing[HUB] is None:
            copy_image(source, digest, HUB, tag)
            verify_image(registry, HUB, tag, digest, row, sha)
    else:
        report['status'] = 'building'
        report['stage'] = 'building'
        digest = build(row, tag)
    report['digest'] = digest
    report['stage'] = 'source_readback'
    verify_image(registry, HUB, tag, digest, row, sha)
    report['status'] = 'runtime'
    report['stage'] = 'runtime'
    result = runtime(HUB + '@' + digest)
    require_runtime(result, row, sha, HUB + '@' + digest, harness_hash)
    report['runtime'] = result
    targets = [(image, alias) for image in (HUB, GHCR) for alias in (tag, version, 'v' + version)]
    report['stage'] = 'alias_preflight'
    # All aliases preflight before any alias write. No unknown drift accepted.
    for image, alias in targets:
        current = registry.manifest(image, alias)[0]
        require_target(current, before[f'{image}:{alias}'], digest, approve if alias != tag else False)
    report['status'] = 'promoting'
    report['stage'] = 'promoting'
    for image, alias in targets:
        current = registry.manifest(image, alias)[0]
        require_target(current, before[f'{image}:{alias}'], digest, approve if alias != tag else False)
        if current != digest:
            copy_image(HUB, digest, image, alias)
        report['images'].append(verify_image(registry, image, alias, digest, row, sha))
    # Re-read earlier writes too; copies across registries cannot be atomic.
    report['stage'] = 'final_readback'
    report['images'] = [verify_image(registry, image, alias, digest, row, sha) for image, alias in targets]
    report['status'] = 'success'
    return report


def check_preserved(registry, snapshot, report=None):
    for index, (ref, expected) in enumerate(snapshot['preserved'].items(), 1):
        image, tag = ref.rsplit(':', 1)
        if report is not None:
            # Snapshot is validated first; expose a bounded ordinal, not a raw reference.
            report['stage_details'] = {'reference_index': min(index, 8), 'reference_count': 8}
        if registry.manifest(image, tag)[0] != expected:
            raise ValueError('Out-of-scope reference changed')


def aggregate(registry, copy_image, data, reports, sha, run_id, harness_hash, snapshot, approve, report):
    report.update(status='checking', source_sha=sha, run_id=run_id,
                  latest_before={f'{image}:latest': snapshot['before'][f'{image}:latest'] for image in (HUB, GHCR)},
                  images=[], latest_after={})
    qualified = require_reports(data, reports, sha, run_id, harness_hash)
    def read_all():
        records = []
        for row in data['versions']:
            digest = qualified[row['version']]['digest']
            for image in (HUB, GHCR):
                for tag in (row['version'], 'v' + row['version'], source_tag(sha, row['version'])):
                    records.append(verify_image(registry, image, tag, digest, row, sha))
        check_preserved(registry, snapshot)
        return records
    report['images'] = read_all()  # All 17 sources/aliases must exist in both registries first.
    row = next(r for r in data['versions'] if r['version'] == data['latest'])
    desired = qualified[data['latest']]['digest']
    for image in (HUB, GHCR):
        require_target(registry.manifest(image, 'latest')[0], snapshot['before'][f'{image}:latest'], desired, approve)
    report['status'] = 'promoting-latest'
    for image in (HUB, GHCR):
        current = registry.manifest(image, 'latest')[0]
        require_target(current, snapshot['before'][f'{image}:latest'], desired, approve)
        if current != desired:
            copy_image(HUB, desired, image, 'latest')
        report['latest_after'][image + ':latest'] = verify_image(registry, image, 'latest', desired, row, sha)['digest']
    report['images'] = read_all()
    for image in (HUB, GHCR):
        verify_image(registry, image, 'latest', desired, row, sha)
    report.update(status='success', count=len(qualified), latest_version=row['version'],
                  runtime_tested=['linux/amd64'], build_only=['linux/arm64'])
    return report


def require_target(current, baseline, desired, approve):
    if current == desired:
        return  # Idempotent recovery; no write.
    if current != baseline:
        raise ValueError('Target drifted from the original dispatch snapshot')
    if current is not None and approve is not True:
        raise ValueError('Existing alias requires explicit overwrite approval')


# CLI effects are isolated here; policy above is exercised without registry writes.
import argparse
import hashlib
import os
import subprocess
import tempfile


class CommandFailure(RuntimeError):
    def __init__(self, stage, exit_code, *, timed_out=False, output_reason=None):
        super().__init__('Bounded subprocess failed')
        self.failure = {'stage': stage, 'reason': 'timeout' if timed_out else 'nonzero_exit',
                        'exit_code': exit_code, 'timed_out': timed_out}
        if output_reason in {reason for _, reason in OUTPUT_CODES}:
            self.failure['output_reason'] = output_reason


# Presence of fixed markers is a diagnostic hint, never a qualification decision.
OUTPUT_CODES = (
    (b'permission_denied: write_package', 'registry_permission_denied'),
    (b'toomanyrequests', 'registry_rate_limited'),
    (b'no space left on device', 'storage_full'),
    (b'checksum did not match', 'checksum_mismatch'),
    (b'tls handshake timeout', 'transport_timeout'),
)


def classify_output(output):
    # Read only the last 64 KiB of the private temporary output; emit no excerpts.
    output.seek(0, 2)
    output.seek(max(0, output.tell() - 65536))
    data = output.read(65536).lower()
    return next((reason for marker, reason in OUTPUT_CODES if marker in data), None)


def run_command(command, timeout, *, stage):
    if stage not in ('build', 'pull', 'runtime', 'copy'):
        raise ValueError('Unknown subprocess stage')
    # Never emit raw build, registry, guest, or authentication subprocess output.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(command, stdout=output, stderr=output, timeout=timeout)
        except subprocess.TimeoutExpired:
            # TimeoutExpired embeds argv and may carry output; neither is evidence.
            raise CommandFailure(stage, None, timed_out=True) from None
        if result.returncode:
            reason = classify_output(output) if stage in ('build', 'copy') else None
            raise CommandFailure(stage, result.returncode, output_reason=reason)


class MatrixCopy(Skopeo):
    """Reuse private auth lifecycle, NOT the hardcoded 7.21.4 recovery scope."""
    def __init__(self, allowed):
        self.allowed = set(allowed)

    def copy_image(self, source, digest, image, tag):
        if (source not in (HUB, GHCR) or (image, tag) not in self.allowed or
                not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)):
            raise ValueError('Copy is outside matrix scope')
        run_command(['skopeo', 'copy', '--all', '--preserve-digests', '--authfile', self.authfile,
                     f'docker://{source}@{digest}', f'docker://{image}:{tag}'], 1800, stage='copy')


def create_snapshot(registry, data, sha, run_id, approve):
    before = {}; preserved = {}
    for image in (HUB, GHCR):
        latest = registry.manifest(image, 'latest')[0]
        if latest is None:
            raise ValueError('Cannot establish old latest snapshot')
        before[f'{image}:latest'] = latest
        for row in data['versions']:
            for tag in (row['version'], 'v' + row['version'], source_tag(sha, row['version'])):
                before[f'{image}:{tag}'] = registry.manifest(image, tag)[0]
        # Read-only preservation of the separately authorized historical release.
        for tag in PRESERVED_TAGS:
            preserved[f'{image}:{tag}'] = registry.manifest(image, tag)[0]
    return {'source_sha': sha, 'run_id': run_id, 'manifest': data,
            'approve_version_overwrite': approve, 'before': before, 'preserved': preserved}


def require_snapshot(snapshot, data, sha, run_id, approve):
    if (snapshot['source_sha'] != sha or snapshot['run_id'] != run_id or
            snapshot['manifest'] != data or snapshot['approve_version_overwrite'] is not approve):
        raise ValueError('Snapshot must be the original dispatch scope and approval')
    targets = {f'{image}:{tag}' for image in (HUB, GHCR) for row in data['versions']
               for tag in (row['version'], 'v' + row['version'], source_tag(sha, row['version']))}
    targets.update(f'{image}:latest' for image in (HUB, GHCR))
    if set(snapshot['before']) != targets:
        raise ValueError('Snapshot target set is incomplete')
    if set(snapshot['preserved']) != {f'{image}:{tag}' for image in (HUB, GHCR) for tag in PRESERVED_TAGS}:
        raise ValueError('Snapshot preservation scope is incomplete')
    if any(snapshot['before'][f'{image}:latest'] is None for image in (HUB, GHCR)):
        raise ValueError('Missing old latest baseline')
    for value in list(snapshot['before'].values()) + list(snapshot['preserved'].values()):
        if value is not None and not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
            raise ValueError('Invalid baseline digest')


POLICY_FAILURES = {
    'Snapshot must be the original dispatch scope and approval': 'snapshot_identity_mismatch',
    'Snapshot target set is incomplete': 'snapshot_targets_mismatch',
    'Snapshot preservation scope is incomplete': 'snapshot_preservation_mismatch',
    'Missing old latest baseline': 'latest_baseline_missing',
    'Invalid baseline digest': 'baseline_digest_invalid',
    'Out-of-scope reference changed': 'preserved_reference_changed',
    'Source or destination digest mismatch': 'image_digest_mismatch',
    'Target drifted from the original dispatch snapshot': 'target_drift',
    'Existing alias requires explicit overwrite approval': 'overwrite_not_approved',
}


def failure_details(error, stage):
    stages = {'dispatch_validation', 'manifest_validation', 'version_validation', 'snapshot_read',
              'snapshot_validation', 'snapshot_create', 'preservation_check', 'harness_identity',
              'registry_login', 'publication', 'aggregation', 'matrix_output', 'source_preflight',
              'building', 'source_readback', 'runtime', 'alias_preflight', 'promoting', 'final_readback'}
    stage = stage if stage in stages else 'unknown'
    if isinstance(error, RegistryError):
        return {'stage': stage, **error.diagnostic}
    if isinstance(error, CommandFailure):
        return error.failure
    if type(error) is ValueError and len(error.args) == 1 and isinstance(error.args[0], str):
        reason = POLICY_FAILURES.get(error.args[0])
        if reason:
            return {'stage': stage, 'reason': reason}
    reason = ('invalid_json' if isinstance(error, json.JSONDecodeError) else
              'file_missing' if isinstance(error, FileNotFoundError) else
              'missing_field' if isinstance(error, KeyError) else
              'invalid_shape' if isinstance(error, TypeError) else
              'policy_rejected' if isinstance(error, ValueError) else 'unclassified_failure')
    return {'stage': stage, 'reason': reason}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'variant', 'aggregate'))
    parser.add_argument('--manifest', type=Path, default=Path('config/chr-versions.json'))
    parser.add_argument('--snapshot', type=Path, default=Path('matrix-snapshot/snapshot.json'))
    parser.add_argument('--reports', type=Path, default=Path('matrix-reports'))
    parser.add_argument('--output-dir', type=Path, default=Path('matrix-output'))
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {'status': 'started', 'command': args.command}
    stage = 'dispatch_validation'
    try:
        sha = os.environ.get('GITHUB_SHA', '')
        run_id = os.environ.get('GITHUB_RUN_ID', '')
        approval = os.environ.get('APPROVE_VERSION_OVERWRITE', '')
        if (os.environ.get('GITHUB_REF') != 'refs/heads/main' or
                os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or
                os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
                not re.fullmatch(r'[0-9a-f]{40}', sha) or not run_id.isdecimal() or
                approval not in ('true', 'false')):
            raise ValueError('Only explicitly approved manual main dispatch is allowed')
        approve = approval == 'true'
        stage = 'manifest_validation'
        data = load_manifest(args.manifest)
        if args.command == 'variant':
            stage = 'version_validation'
            version = os.environ.get('CHR_VERSION', '')
            row = next((r for r in data['versions'] if r['version'] == version), None)
            if row is None:
                raise ValueError('Version is outside reviewed scope')
            report['version'] = row['version']
        registry = MatrixRegistry()
        report.update(source_sha=sha, run_id=run_id,
                      run_url=f'https://github.com/{REPOSITORY}/actions/runs/{run_id}')
        if args.command == 'prepare':
            stage = 'snapshot_read'
            if os.environ.get('GITHUB_RUN_ATTEMPT') == '1':
                if args.snapshot.exists():
                    raise ValueError('Never replace an existing dispatch snapshot')
                stage = 'snapshot_create'
                snapshot = create_snapshot(registry, data, sha, run_id, approve)
                args.snapshot.parent.mkdir(parents=True, exist_ok=True)
                args.snapshot.write_text(json.dumps(snapshot, indent=2) + '\n')
            else:
                snapshot = json.loads(args.snapshot.read_text())
            stage = 'snapshot_validation'
            require_snapshot(snapshot, data, sha, run_id, approve)
            stage = 'preservation_check'
            check_preserved(registry, snapshot, report)
            stage = 'matrix_output'
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('matrix=' + json.dumps({'include': [{'version': r['version']} for r in data['versions']]}) + '\n')
            report.update(status='success', count=len(data['versions']), snapshot=snapshot)
            return 0
        stage = 'snapshot_read'
        snapshot = json.loads(args.snapshot.read_text())
        stage = 'snapshot_validation'
        require_snapshot(snapshot, data, sha, run_id, approve)
        stage = 'preservation_check'
        check_preserved(registry, snapshot, report)
        stage = 'harness_identity'
        harness_hash = hashlib.sha256(Path('tests/docker-integration.py').read_bytes()).hexdigest()
        if args.command == 'variant':
            tag = source_tag(sha, version)
            allowed = {(image, alias) for image in (HUB, GHCR) for alias in (version, 'v' + version, tag)}
            before = {f'{image}:{alias}': snapshot['before'][f'{image}:{alias}'] for image, alias in allowed}
            def build(row, tag):
                # Recheck both immutable references immediately before BuildKit's sole source-tag push.
                if any(registry.manifest(image, tag)[0] is not None for image in (HUB, GHCR)):
                    raise ValueError('Immutable source appeared before build')
                metadata = args.output_dir / 'build-metadata.json'
                run_command(['docker', 'buildx', 'build', '--platform', ','.join(PLATFORMS),
                             '--push', '--provenance=mode=max', '--sbom=true',
                             '--build-arg', 'ROUTEROS_VERSION=' + version,
                             '--build-arg', 'ROUTEROS_SHA256=' + row['checksumSHA256'],
                             '--build-arg', 'SOURCE_REVISION=' + sha,
                             '--build-arg', 'WRAPPER_VERSION=current-chr',
                             '--build-arg', 'SOURCE_URL=https://github.com/' + REPOSITORY,
                             '--metadata-file', str(metadata), '--tag', HUB + ':' + tag, '.'], 2400, stage='build')
                return json.loads(metadata.read_text())['containerimage.digest']
            def runtime(image):
                run_command(['docker', 'pull', '--platform', 'linux/amd64', image], 600, stage='pull')
                evidence = args.output_dir / 'private-runtime'
                try:
                    run_command(['python3', 'tests/docker-integration.py', '--run', '--image', image,
                                 '--output-dir', str(evidence)], 960, stage='runtime')
                finally:
                    path = evidence / 'report.json'
                    if path.exists():
                        try:
                            raw = json.loads(path.read_text())
                            if not isinstance(raw, dict):
                                raise ValueError('Expected harness report object')
                        except (OSError, ValueError):
                            # A partial report must not mask the subprocess exit/timeout.
                            report['runtime'] = {'status': 'failed', 'failure': {
                                'stage': 'harness', 'reason': 'invalid_report'}}
                        else:
                            # Harness diagnostics are already allowlisted; never copy free-form errors.
                            report['runtime'] = {k: raw[k] for k in ('status', 'image', 'image_id', 'image_labels',
                                'checks', 'shutdowns', 'cleanup_errors', 'harness_sha256', 'duration_seconds',
                                'dhcp_diagnostics', 'health_diagnostics', 'network_diagnostics') if k in raw}
                            if raw.get('status') != 'passed':
                                report['runtime']['failure'] = runtime_failure(raw)
                return report['runtime']
            stage = 'registry_login'
            with MatrixCopy(allowed) as copier:
                stage = 'publication'
                publish_version(registry, copier.copy_image, build, runtime, row, sha,
                                before, approve, harness_hash, report)
            stage = 'preservation_check'
            check_preserved(registry, snapshot, report)
            # Latest is untouched by individual version jobs, including reruns after partial latest copy.
        else:
            stage = 'aggregation'
            reports = [json.loads(p.read_text()) for p in args.reports.glob('*/report.json')]
            allowed = {(image, 'latest') for image in (HUB, GHCR)}
            with MatrixCopy(allowed) as copier:
                aggregate(registry, copier.copy_image, data, reports, sha, run_id,
                          harness_hash, snapshot, approve, report)
        return 0
    except Exception as error:
        report.update(failed_stage=report['status'], status='failed', error_type=type(error).__name__)
        report['failure'] = failure_details(error, report.get('stage', stage) if stage == 'publication' else stage)
        return 1
    finally:
        (args.output_dir / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({k: report[k] for k in ('status', 'command', 'error_type') if k in report}))


if __name__ == '__main__':
    raise SystemExit(main())
