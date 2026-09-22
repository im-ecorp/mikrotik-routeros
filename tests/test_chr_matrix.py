"""Current website release scope is deliberately separate from default-seed CI."""
import copy
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
    def test_reviewed_manifest_has_exact_website_scope(self):
        path = ROOT / 'config/chr-versions.json'
        self.assertTrue(path.exists(), 'reviewed website manifest is missing')
        data = json.loads(path.read_text())
        expected = ({f'6.49.{n}' for n in range(17, 23)} | {'7.21.5', '7.24', '7.25beta5'} |
                    {f'7.23.{n}' for n in range(4, 8)} | {f'7.24.{n}' for n in range(1, 5)})
        self.assertEqual(len(data['versions']), 17)
        self.assertEqual({v['version'] for v in data['versions']}, expected)
        self.assertEqual(data['latest'], '7.24.4')
        for row in data['versions']:
            self.assertEqual(row['architecture'], 'x86')
            self.assertEqual(row['url'], f"https://download.mikrotik.com/routeros/{row['version']}/chr-{row['version']}.vdi.zip")
            self.assertRegex(row['checksumSHA256'], r'^[a-f0-9]{64}$')
            self.assertTrue(row['channels'])


class SeedChecksumTests(unittest.TestCase):
    def test_download_verified_before_extraction(self):
        text = (ROOT / 'Dockerfile').read_text()
        self.assertIn('ARG ROUTEROS_SHA256=', text)
        self.assertLess(text.index('sha256sum -c'), text.index('unzip /routeros/image.zip'))
        self.assertIn('io.mikrotik-routeros.seed.sha256=$ROUTEROS_SHA256', text)
        self.assertIn('ARG ROUTEROS_VERSION=7.21.5\n', text)


class MatrixPolicyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'scripts/chr_matrix.py').exists(), 'matrix policy is missing')
        from scripts import chr_matrix
        self.m = chr_matrix
        self.data = self.m.load_manifest(ROOT / 'config/chr-versions.json')
        self.sha = 'a' * 40

    def test_manifest_rejects_duplicates_and_unreviewed_versions(self):
        for mutate in (lambda d: d['versions'].append(d['versions'][0]),
                       lambda d: d['versions'][0].update(version='7.24rc4'),
                       lambda d: d['versions'][0].update(url='https://evil.invalid/seed.zip'),
                       lambda d: d['versions'][0].update(checksumSHA256='bad')):
            data = copy.deepcopy(self.data)
            mutate(data)
            with self.assertRaises(ValueError):
                self.m.validate_manifest(data)

    def test_runtime_blocked_excludes_versions_without_losing_the_record(self):
        """Blocked versions stay in the manifest as reviewed data; only the gate skips them."""
        data = copy.deepcopy(self.data)
        self.assertEqual(len(data['versions']), 17)
        self.assertEqual(sorted(data['runtimeBlocked']['versions']), ['6.49.21', '6.49.22'])
        self.assertTrue(data['runtimeBlocked']['reason'].strip())
        qualifying = {r['version'] for r in self.m.qualifying(data)}
        self.assertEqual(len(qualifying), 15)
        self.assertNotIn('6.49.21', qualifying)
        self.assertNotIn('6.49.22', qualifying)
        # The promotion target must always remain qualifying.
        self.assertIn(data['latest'], qualifying)

    def test_absent_runtime_blocked_keeps_every_version_qualifying(self):
        """The pinned original manifest has no such key and must be unaffected."""
        pinned = copy.deepcopy(self.data)
        pinned.pop('runtimeBlocked')
        validated = self.m.validate_manifest(pinned)
        self.assertEqual(len(self.m.qualifying(validated)), 17)

    def test_runtime_blocked_cannot_hide_latest_or_fail_open(self):
        for broken in ([], None, 'x', {}, {'versions': '6.49.21'},
                       {'versions': ['6.49.21'], 'reason': '   '},
                       {'versions': ['9.9.9'], 'reason': 'unknown version'},
                       {'versions': ['6.49.21', '6.49.21'], 'reason': 'duplicate'},
                       {'versions': ['7.24.4'], 'reason': 'blocking the promotion target'}):
            data = copy.deepcopy(self.data)
            data['runtimeBlocked'] = broken
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                self.m.validate_manifest(data)

    def test_source_tag_includes_full_sha_and_exact_seed(self):
        self.assertEqual(self.m.source_tag(self.sha, '7.24'), 'sha-' + self.sha + '-chr-7.24')
        self.assertNotEqual(self.m.source_tag(self.sha, '7.24'), self.m.source_tag(self.sha, '7.25beta5'))

    def test_target_drift_and_overwrite_approval(self):
        old, desired, other = ('sha256:' + c * 64 for c in 'abc')
        self.m.require_target(desired, old, desired, False)
        self.m.require_target(old, old, desired, True)
        self.m.require_target(None, None, desired, False)
        for current, baseline, approval in [(old, old, False), (other, old, True), (None, old, True)]:
            with self.assertRaises(ValueError):
                self.m.require_target(current, baseline, desired, approval)


class QualificationTests(MatrixPolicyTests):
    def runtime(self, row, digest):
        return {'status': 'passed', 'image': 'repo@' + digest, 'cleanup_errors': [],
                'harness_sha256': 'b' * 64,
                'image_labels': self.m.expected_labels(row, self.sha),
                'checks': [{'fresh_guest_matches_seed': True, 'seed_version': row['version']}],
                'shutdowns': [{'label': name} for name in ('restart', 'recreate', 'final')]}

    def test_runtime_gate_binds_seed_source_digest_and_harness(self):
        self.assertTrue(hasattr(self.m, 'require_runtime'), 'runtime evidence gate missing')
        row = self.data['versions'][0]
        digest = 'sha256:' + 'd' * 64
        report = self.runtime(row, digest)
        self.m.require_runtime(report, row, self.sha, 'repo@' + digest, 'b' * 64)
        for field, value in [('status', 'failed'), ('image', 'repo:mutable'),
                             ('cleanup_errors', ['failure']), ('harness_sha256', 'c' * 64),
                             ('checks', []), ('shutdowns', [])]:
            bad = copy.deepcopy(report); bad[field] = value
            with self.assertRaises(ValueError):
                self.m.require_runtime(bad, row, self.sha, 'repo@' + digest, 'b' * 64)

    def test_exact_aggregate_rejects_missing_duplicate_failed_or_foreign(self):
        self.assertTrue(hasattr(self.m, 'require_reports'), 'aggregate gate missing')
        reports = [{'status': 'success', 'version': r['version'], 'source_sha': self.sha,
                    'run_id': '123', 'digest': 'sha256:' + 'd' * 64,
                    'platforms': ['linux/amd64', 'linux/arm64'],
                    'runtime_tested': ['linux/amd64'], 'build_only': ['linux/arm64'],
                    'runtime': self.runtime(r, 'sha256:' + 'd' * 64)} for r in self.m.qualifying(self.data)]
        self.m.require_reports(self.data, reports, self.sha, '123', 'b' * 64, source_image='repo')
        for bad in [reports[:-1], reports + reports[:1]]:
            with self.assertRaises(ValueError):
                self.m.require_reports(self.data, bad, self.sha, '123', 'b' * 64, source_image='repo')
        for field, value in [('status', 'failed'), ('source_sha', 'c' * 40), ('run_id', '124'),
                             ('platforms', ['linux/amd64']), ('runtime_tested', []), ('digest', 'invalid')]:
            bad = copy.deepcopy(reports); bad[0][field] = value
            with self.assertRaises(ValueError):
                self.m.require_reports(self.data, bad, self.sha, '123', 'b' * 64, source_image='repo')


class PublicationTests(QualificationTests):
    def registry(self):
        outer = self
        class Fake:
            def __init__(self):
                self.tags = {}; self.rows = {}; self.copies = []; self.content = {}
            def export_content(self):
                from scripts.chr_registry import CONTENT_SCHEMA
                return {'schema': CONTENT_SCHEMA, 'content': {}}
            def import_content(self, data):
                from scripts.chr_registry import CONTENT_SCHEMA
                if not isinstance(data, dict) or data.get('schema') != CONTENT_SCHEMA:
                    raise ValueError('Unsupported shared registry content schema')
                return len(data.get('content') or {})
            def resolve(self, image, tag):
                # HEAD-only binding: same answer, without the absence proof.
                return tag if tag.startswith('sha256:') and tag in self.rows else self.tags.get((image, tag))
            def manifest(self, image, tag):
                digest = tag if tag.startswith('sha256:') and tag in self.rows else self.tags.get((image, tag))
                return digest, ({'manifests': [{'digest': digest + '/' + arch, 'platform': {'os': 'linux', 'architecture': arch}}
                                              for arch in ('amd64', 'arm64')]} if digest else None)
            def config(self, image, digest):
                parent, arch = digest.split('/')
                return {'os': 'linux', 'architecture': arch,
                        'config': {'Labels': outer.m.expected_labels(self.rows[parent], outer.sha)}}
            def copy(self, source, digest, image, tag):
                self.copies.append((image, tag)); self.tags[image, tag] = digest
        return Fake()

    def test_failed_runtime_keeps_aliases_and_success_reuses_source(self):
        self.assertTrue(hasattr(self.m, 'publish_version'), 'version orchestrator missing')
        registry = self.registry(); row = self.data['versions'][0]; digest = 'sha256:' + 'd' * 64
        tag = self.m.source_tag(self.sha, row['version'])
        before = {f'{image}:{alias}': None for image in (self.m.HUB, self.m.GHCR)
                  for alias in (row['version'], 'v' + row['version'], tag)}
        def build(row, tag):
            registry.rows[digest] = row; registry.tags[self.m.HUB, tag] = digest
            return digest
        def failed(image):
            raise RuntimeError('runtime failed')
        with self.assertRaises(RuntimeError):
            self.m.publish_version(registry, registry.copy, build, failed, row, self.sha,
                                   before, False, 'b' * 64, {})
        self.assertFalse(registry.copies)
        def no_build(*args):
            self.fail('Existing immutable source must not rebuild')
        def runtime(image):
            result = self.runtime(row, digest); result['image'] = image; return result
        report = {}
        self.m.publish_version(registry, registry.copy, no_build, runtime, row, self.sha,
                               before, False, 'b' * 64, report)
        self.assertEqual(report['status'], 'success')
        for image in (self.m.HUB, self.m.GHCR):
            for alias in (row['version'], 'v' + row['version'], tag):
                self.assertEqual(registry.tags[image, alias], digest)
        count = len(registry.copies)
        self.m.publish_version(registry, registry.copy, no_build, runtime, row, self.sha,
                               before, False, 'b' * 64, {})
        self.assertEqual(len(registry.copies), count)


class LatestTests(PublicationTests):
    def test_latest_waits_for_all_reports_and_preserves_snapshot(self):
        self.assertTrue(hasattr(self.m, 'aggregate'), 'latest aggregate missing')
        registry = self.registry(); reports = []; before = {}
        for n, row in enumerate(self.m.qualifying(self.data)):
            digest = 'sha256:' + f'{n:064x}'
            registry.rows[digest] = row
            for image in (self.m.HUB, self.m.GHCR):
                for tag in (row['version'], 'v' + row['version'], self.m.source_tag(self.sha, row['version'])):
                    registry.tags[image, tag] = digest
            runtime = self.runtime(row, digest); runtime['image'] = self.m.HUB + '@' + digest
            reports.append({'status': 'success', 'version': row['version'], 'source_sha': self.sha,
                            'run_id': '123', 'digest': digest, 'platforms': self.m.PLATFORMS,
                            'runtime_tested': ['linux/amd64'], 'build_only': ['linux/arm64'], 'runtime': runtime})
        old = 'sha256:' + 'f' * 64
        for image in (self.m.HUB, self.m.GHCR):
            registry.tags[image, 'latest'] = old; before[f'{image}:latest'] = old
        snapshot = {'before': before, 'preserved': {}}
        with self.assertRaises(ValueError):
            self.m.aggregate(registry, registry.copy, self.data, reports[:-1], self.sha, '123',
                             'b' * 64, snapshot, True, {})
        self.assertFalse(registry.copies)
        with self.assertRaises(ValueError):
            self.m.aggregate(registry, registry.copy, self.data, reports, self.sha, '123',
                             'b' * 64, snapshot, False, {})
        result = {}
        self.m.aggregate(registry, registry.copy, self.data, reports, self.sha, '123',
                         'b' * 64, snapshot, True, result)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['latest_before'], before)
        desired = next(r['digest'] for r in reports if r['version'] == '7.24.4')
        self.assertEqual([registry.tags[i, 'latest'] for i in (self.m.HUB, self.m.GHCR)], [desired, desired])

    def test_partial_copy_recovery_rejects_drift_without_rebuilding(self):
        registry = self.registry(); row = self.data['versions'][0]; digest = 'sha256:' + 'd' * 64
        tag = self.m.source_tag(self.sha, row['version']); registry.rows[digest] = row
        registry.tags[self.m.HUB, tag] = digest
        before = {f'{i}:{t}': None for i in (self.m.HUB, self.m.GHCR)
                  for t in (row['version'], 'v' + row['version'], tag)}
        def build(*args):
            self.fail('Recovery must not rebuild')
        def runtime(image):
            result = self.runtime(row, digest); result['image'] = image; return result
        def partial(source, digest, image, target):
            if image == self.m.GHCR:
                raise RuntimeError('simulated package permission failure')
            registry.copy(source, digest, image, target)
        with self.assertRaises(RuntimeError):
            self.m.publish_version(registry, partial, build, runtime, row, self.sha,
                                   before, False, 'b' * 64, {})
        self.assertEqual(registry.tags[self.m.HUB, row['version']], digest)
        registry.tags[self.m.GHCR, row['version']] = 'sha256:' + 'e' * 64
        count = len(registry.copies)
        with self.assertRaises(ValueError):
            self.m.publish_version(registry, registry.copy, build, runtime, row, self.sha,
                                   before, True, 'b' * 64, {})
        self.assertEqual(len(registry.copies), count)
        del registry.tags[self.m.GHCR, row['version']]
        self.m.publish_version(registry, registry.copy, build, runtime, row, self.sha,
                               before, False, 'b' * 64, {})
        self.assertEqual(registry.tags[self.m.GHCR, row['version']], digest)

    def test_wrong_platform_config_or_labels_fail_readback(self):
        from unittest.mock import patch
        registry = self.registry(); row = self.data['versions'][0]; digest = 'sha256:' + 'd' * 64
        registry.rows[digest] = row
        with patch.object(registry, 'config', return_value={'os': 'linux', 'architecture': 'arm64'}):
            with self.assertRaises(ValueError):
                self.m.verify_image(registry, self.m.HUB, digest, digest, row, self.sha)
        with patch.object(self.m, 'expected_labels', return_value={'wrong': 'label'}):
            # Construct a real inconsistent config, not a fake that shares expected_labels.
            with patch.object(registry, 'config', return_value={'os': 'linux', 'architecture': 'amd64', 'config': {'Labels': {}}}):
                with self.assertRaises(ValueError):
                    self.m.verify_image(registry, self.m.HUB, digest, digest, row, self.sha)

    def test_cli_variant_builds_only_candidate_and_pulls_exact_digest(self):
        import hashlib
        import os
        import tempfile
        from unittest.mock import patch
        registry = self.registry(); row = self.data['versions'][0]; digest = 'sha256:' + 'd' * 64
        for image in (self.m.HUB, self.m.GHCR):
            registry.tags[image, 'latest'] = 'sha256:' + 'f' * 64
        snapshot = self.m.create_snapshot(registry, self.data, self.sha, '123', True)
        commands = []
        class Copier:
            def __init__(self, allowed): self.allowed = allowed
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def copy_image(self, source, digest, image, tag):
                if (image, tag) not in self.allowed: raise AssertionError('out of scope')
                registry.copy(source, digest, image, tag)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); snapshot_path = root / 'snapshot.json'
            snapshot_path.write_text(json.dumps(snapshot))
            def command(argv, timeout, *, stage):
                commands.append(argv)
                if argv[:3] == ['docker', 'buildx', 'build']:
                    registry.rows[digest] = row
                    registry.tags[self.m.HUB, self.m.source_tag(self.sha, row['version'])] = digest
                    Path(argv[argv.index('--metadata-file') + 1]).write_text(json.dumps({'containerimage.digest': digest}))
                elif argv[:2] == ['python3', 'tests/docker-integration.py']:
                    result = self.runtime(row, digest)
                    result['image'] = argv[argv.index('--image') + 1]
                    result['harness_sha256'] = hashlib.sha256((ROOT / 'tests/docker-integration.py').read_bytes()).hexdigest()
                    evidence = Path(argv[argv.index('--output-dir') + 1]); evidence.mkdir()
                    (evidence / 'report.json').write_text(json.dumps(result))
            env = {'GITHUB_SHA': self.sha, 'GITHUB_RUN_ID': '123', 'GITHUB_REF': 'refs/heads/main',
                   'GITHUB_REPOSITORY': self.m.REPOSITORY, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                   'APPROVE_VERSION_OVERWRITE': 'true', 'CHR_VERSION': row['version']}
            with patch.dict(os.environ, env), patch.object(self.m, 'MatrixRegistry', return_value=registry), \
                    patch.object(self.m, 'MatrixCopy', Copier), patch.object(self.m, 'run_command', command):
                self.assertEqual(self.m.main(['variant', '--snapshot', str(snapshot_path), '--output-dir', str(root / 'output')]), 0)
            report = json.loads((root / 'output/report.json').read_text())
            self.assertEqual(report['status'], 'success')
            build = commands[0]
            self.assertEqual(build[build.index('--platform') + 1], 'linux/amd64,linux/arm64')
            self.assertEqual(build.count('--tag'), 1)
            self.assertEqual(build[build.index('--tag') + 1], self.m.HUB + ':' + self.m.source_tag(self.sha, row['version']))
            self.assertIn('ROUTEROS_SHA256=' + row['checksumSHA256'], build)
            self.assertEqual(commands[1], ['docker', 'pull', '--platform', 'linux/amd64', self.m.HUB + '@' + digest])
            self.assertEqual(registry.tags[self.m.HUB, 'latest'], 'sha256:' + 'f' * 64)

    def test_registry_refreshes_expired_token_once(self):
        self.assertTrue(hasattr(self.m, 'MatrixRegistry'), 'long-build token refresh missing')
        from unittest.mock import patch
        registry = self.m.MatrixRegistry(credentials={})
        registry.tokens[self.m.HUB] = 'expired'
        with patch.object(self.m.Registry, 'request', side_effect=[(401, b'', {}), (200, b'ok', {})]) as request:
            self.assertEqual(registry.request(self.m.HUB, 'manifests/latest')[0], 200)
            self.assertEqual(request.call_count, 2)
            self.assertNotIn(self.m.HUB, registry.tokens)
        with patch.object(self.m.Registry, 'request', return_value=(401, b'', {})) as request:
            self.assertEqual(registry.request(self.m.HUB, 'manifests/latest')[0], 401)
            self.assertEqual(request.call_count, 2)

    def test_config_cache_reuses_only_verified_immutable_blobs(self):
        from unittest.mock import patch
        registry = self.m.MatrixRegistry(credentials={})
        with patch.object(self.m.Registry, 'config', return_value={'verified': True}) as config:
            first = registry.config(self.m.HUB, 'sha256:' + 'a' * 64)
            self.assertEqual(first, registry.config(self.m.HUB, 'sha256:' + 'a' * 64))
            self.assertEqual(config.call_count, 1)
            registry.config(self.m.GHCR, 'sha256:' + 'a' * 64)
            self.assertEqual(config.call_count, 2)

    def test_snapshot_scope_and_approval_are_bound(self):
        registry = self.registry()
        for image in (self.m.HUB, self.m.GHCR):
            registry.tags[image, 'latest'] = 'sha256:' + 'f' * 64
        snapshot = self.m.create_snapshot(registry, self.data, self.sha, '123', True)
        self.m.require_snapshot(snapshot, self.data, self.sha, '123', True)
        for field, value in [('run_id', '124'), ('approve_version_overwrite', False),
                             ('before', {}), ('preserved', {})]:
            bad = copy.deepcopy(snapshot); bad[field] = value
            with self.assertRaises(ValueError):
                self.m.require_snapshot(bad, self.data, self.sha, '123', True)

    def test_cli_fails_closed_with_sanitized_artifact(self):
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, GITHUB_REF='refs/heads/unsafe', GH_TOKEN='never-print-this')
            result = subprocess.run(['python3', str(ROOT / 'scripts/chr_matrix.py'), 'variant',
                                     '--output-dir', directory], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            path = Path(directory) / 'report.json'
            self.assertTrue(path.exists(), 'fail-closed CLI report missing')
            self.assertEqual(json.loads(path.read_text())['status'], 'failed')
            self.assertNotIn('never-print-this', result.stdout + result.stderr + path.read_text())

    def test_workflow_has_bounded_manual_matrix_and_always_artifacts(self):
        path = ROOT / '.github/workflows/current-chr-matrix.yml'
        self.assertTrue(path.exists(), 'manual matrix workflow missing')
        text = path.read_text()
        for required in ('workflow_dispatch:', 'fail-fast: false', 'max-parallel: 3',
                         "github.ref == 'refs/heads/main'", 'if: always()',
                         'approve_version_overwrite:', 'chr-image-publication',
                         'python3 scripts/chr_matrix.py variant', 'python3 scripts/chr_matrix.py aggregate'):
            self.assertIn(required, text)


class ObservabilityTests(unittest.TestCase):
    def variant_failure(self, stage, *, timed_out=False, runtime_fields=None, runtime_report_text=None):
        import contextlib
        import hashlib
        import io
        import os
        import subprocess
        import tempfile
        from unittest.mock import patch
        fixture = PublicationTests(); fixture.setUp()
        m = fixture.m; registry = fixture.registry()
        row = fixture.data['versions'][0]; digest = 'sha256:' + 'd' * 64
        for image in (m.HUB, m.GHCR):
            registry.tags[image, 'latest'] = 'sha256:' + 'f' * 64
        snapshot = m.create_snapshot(registry, fixture.data, fixture.sha, '123', False)
        secret = 'RAW-AUTH-TRANSCRIPT-DISK-STDOUT-DO-NOT-UPLOAD'
        def command(argv, **kwargs):
            current = ('build' if argv[:3] == ['docker', 'buildx', 'build'] else
                       'pull' if argv[:2] == ['docker', 'pull'] else
                       'runtime' if argv[0] == 'python3' else 'copy')
            kwargs['stdout'].write(secret.encode())
            kwargs['stderr'].write(secret.encode())
            if current == 'build' and stage != 'build':
                registry.rows[digest] = row
                registry.tags[m.HUB, m.source_tag(fixture.sha, row['version'])] = digest
                Path(argv[argv.index('--metadata-file') + 1]).write_text(json.dumps({'containerimage.digest': digest}))
            if current == 'runtime':
                result = fixture.runtime(row, digest)
                result.update(image=argv[argv.index('--image') + 1],
                              harness_sha256=hashlib.sha256((ROOT / 'tests/docker-integration.py').read_bytes()).hexdigest())
                if stage == 'runtime':
                    result.update(status='failed', error='RuntimeError: ' + secret,
                                  authentication=secret, transcript=secret, disk=secret, stdout=secret)
                result.update(runtime_fields or {})
                evidence = Path(argv[argv.index('--output-dir') + 1]); evidence.mkdir()
                (evidence / 'report.json').write_text(json.dumps(result) if runtime_report_text is None else runtime_report_text)
            if current == stage:
                if timed_out:
                    raise subprocess.TimeoutExpired(argv + [secret], kwargs['timeout'], output=secret, stderr=secret)
                return subprocess.CompletedProcess(argv, 37)
            return subprocess.CompletedProcess(argv, 0)
        def enter(copier):
            copier.authfile = secret
            return copier
        env = {'GITHUB_SHA': fixture.sha, 'GITHUB_RUN_ID': '123', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_REPOSITORY': m.REPOSITORY, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'APPROVE_VERSION_OVERWRITE': 'false', 'CHR_VERSION': row['version']}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); snapshot_path = root / 'snapshot.json'
            snapshot_path.write_text(json.dumps(snapshot))
            captured = io.StringIO()
            with patch.dict(os.environ, env), patch.object(m, 'MatrixRegistry', return_value=registry), \
                    patch.object(m.MatrixCopy, '__enter__', enter), patch.object(m.MatrixCopy, '__exit__', return_value=False), \
                    patch.object(m.subprocess, 'run', command), contextlib.redirect_stdout(captured), \
                    contextlib.redirect_stderr(captured):
                self.assertEqual(m.main(['variant', '--snapshot', str(snapshot_path),
                                         '--output-dir', str(root / 'output')]), 1)
            text = (root / 'output/report.json').read_text()
            self.assertNotIn(secret, text + captured.getvalue())
            self.assertFalse(registry.copies)
            return json.loads(text)

    def test_invalid_runtime_report_does_not_hide_process_failure(self):
        for text in ('{RAW-AUTH-TRANSCRIPT-DISK-STDOUT-DO-NOT-UPLOAD', '[]'):
            report = self.variant_failure('runtime', timed_out=True, runtime_report_text=text)
            self.assertEqual(report.get('failure'), {'stage': 'runtime', 'reason': 'timeout',
                                                    'exit_code': None, 'timed_out': True})
            self.assertEqual(report['runtime'].get('failure'), {'stage': 'harness', 'reason': 'invalid_report'})

    def test_runtime_failure_reasons_use_exact_fixed_mapping(self):
        cases = [
            ('RuntimeError: Persisted identity differs', 'persistence', 'identity_mismatch'),
            ('RuntimeError: Unexpected password reset on persisted guest', 'authentication', 'persisted_password_reset'),
            ('RuntimeError: Timed out: Docker health healthy', 'health', 'healthy_timeout'),
            ('RuntimeError: Recovered DHCP lease differs from Docker address', 'dhcp', 'recovery_address_mismatch'),
            ('RuntimeError: Guest HTTP response not successful', 'protocols', 'http_response_failed'),
            ('RuntimeError: Fresh guest version differs from image seed version label', 'seed', 'version_mismatch'),
            ('RuntimeError: Serial timeout at login completion; authentication is not logged', 'authentication', 'serial_timeout'),
            ('RuntimeError: Guest shutdown was not clean; see shutdown evidence', 'shutdown', 'unclean_shutdown'),
            ('RuntimeError: Isolation requires zero replies and a counted firewall drop', 'isolation', 'drop_check_failed'),
            ('RuntimeError: Cold disk backup differs from independent readback', 'backup', 'readback_mismatch'),
            ('RuntimeError: Timed out: Docker health unhealthy', 'health', 'unhealthy_timeout'),
            ('RuntimeError: DHCP disable was not confirmed', 'dhcp', 'disable_unconfirmed'),
            ('RuntimeError: DHCP enable was not confirmed', 'dhcp', 'enable_unconfirmed'),
            ('RuntimeError: Guest did not obtain Docker IP via DHCP', 'dhcp', 'initial_address_mismatch'),
            ('RuntimeError: Guest SSH banner missing', 'protocols', 'ssh_banner_missing'),
            ('RuntimeError: Guest UDP DNS record differs', 'protocols', 'dns_record_mismatch'),
            ('RuntimeError: Image seed version label is missing or empty', 'seed', 'label_missing'),
            ('RuntimeError: Restart changed guest version', 'persistence', 'restart_version_mismatch'),
            ('RuntimeError: Recreation changed guest version', 'persistence', 'recreate_version_mismatch'),
            ('RuntimeError: Missing guest-origin shutdown event', 'shutdown', 'guest_event_missing'),
            ('RuntimeError: Container still running after stop', 'shutdown', 'container_still_running'),
            ('RuntimeError: Stable disk missing', 'backup', 'disk_missing'),
            ('RuntimeError: Cold copy requires a stopped guest', 'backup', 'guest_not_stopped'),
            ('RuntimeError: Missing scoped firewall counter', 'isolation', 'counter_missing'),
            ('RuntimeError: Bridge isolation options differ', 'isolation', 'bridge_options_mismatch'),
            ('RuntimeError: Expected IPv4 non-internal bridge for Docker port publishing', 'isolation', 'bridge_type_mismatch'),
            ('RuntimeError: Integration exceeded 840 seconds or was interrupted', 'harness', 'interrupted'),
            ('RuntimeError: Timed out: serial socket', 'serial', 'socket_timeout'),
            ('RuntimeError: Fresh-password flow not exercised', 'authentication', 'fresh_password_flow_missing'),
        ]
        for label in ('login', 'password', 'repeat fresh password', 'fresh password completion'):
            cases.append((f'RuntimeError: Serial timeout at {label}; authentication is not logged',
                          'authentication', 'serial_timeout'))
        for label in ('login', 'password', 'login completion', 'repeat fresh password', 'fresh password completion'):
            cases.append((f'RuntimeError: QEMU serial closed at {label}', 'authentication', 'serial_closed'))
        for label in ('tagged command completion', 'tagged command prompt', 'IT_IDENTITY', 'IT_IDENTITY prompt',
                      'IT_VERSION', 'IT_VERSION prompt', 'IT_ADDRESS', 'IT_ADDRESS prompt', 'IT_DHCP',
                      'IT_DHCP prompt', 'IT_ISOLATION', 'IT_ISOLATION prompt'):
            cases.extend([(f'RuntimeError: Serial timeout at {label}; authentication is not logged', 'serial', 'readback_timeout'),
                          (f'RuntimeError: QEMU serial closed at {label}', 'serial', 'readback_closed')])
        for port in ('80/tcp', '22/tcp', '53/udp'):
            for message, reason in [('Missing published port', 'published_port_missing'),
                                    ('Port is not exclusively loopback bound', 'non_loopback_binding'),
                                    ('Missing host port', 'host_port_missing')]:
                cases.append((f'RuntimeError: {message}: {port}', 'protocols', reason))
        for error, stage, reason in cases:
            with self.subTest(reason=reason):
                report = self.variant_failure('runtime', runtime_fields={'error': error})
                self.assertEqual(report['runtime'].get('failure'), {'stage': stage, 'reason': reason})
                self.assertNotIn('error', report['runtime'])
        for error in (cases[0][0] + ' RAW-AUTH-TRANSCRIPT-DISK-STDOUT-DO-NOT-UPLOAD',
                      'RAW-AUTH-TRANSCRIPT-DISK-STDOUT-DO-NOT-UPLOAD', None, {'untrusted': 'error'}):
            report = self.variant_failure('runtime', runtime_fields={'error': error,
                         'failure': {'stage': 'RAW-AUTH-TRANSCRIPT-DISK-STDOUT-DO-NOT-UPLOAD'}})
            self.assertEqual(report['runtime'].get('failure'), {'stage': 'harness', 'reason': 'unclassified_failure'})

    def test_runtime_retains_allowlisted_diagnostics_without_raw_error(self):
        fields = {
            'dhcp_diagnostics': [{'phase': 'recovery', 'disabled': False, 'status': 'searching...', 'address': None}],
            'health_diagnostics': [{'expected': 'healthy', 'docker_health': 'unhealthy',
                                    'qmp_status': 'running', 'probe_exit_code': 1, 'health_exit_codes': [1]}],
            'network_diagnostics': {'Ports': {'80/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '32768'}]},
                                    'PortBindings': {}, 'Networks': {}},
        }
        report = self.variant_failure('runtime', runtime_fields=fields)
        for key, value in fields.items():
            self.assertEqual(report['runtime'].get(key), value)
        for key in ('error', 'authentication', 'transcript', 'disk', 'stdout'):
            self.assertNotIn(key, report['runtime'])

    def test_subprocess_timeout_is_safe_in_uploaded_report(self):
        for stage in ('build', 'pull', 'runtime', 'copy'):
            with self.subTest(stage=stage):
                report = self.variant_failure(stage, timed_out=True)
                self.assertEqual(report.get('failure'), {'stage': stage, 'reason': 'timeout',
                                                        'exit_code': None, 'timed_out': True})

    def test_subprocess_exit_failure_is_safe_in_uploaded_report(self):
        for stage in ('build', 'pull', 'runtime', 'copy'):
            with self.subTest(stage=stage):
                report = self.variant_failure(stage)
                self.assertEqual(report.get('failure'), {'stage': stage, 'reason': 'nonzero_exit',
                                                        'exit_code': 37, 'timed_out': False})


if __name__ == '__main__':
    unittest.main()
