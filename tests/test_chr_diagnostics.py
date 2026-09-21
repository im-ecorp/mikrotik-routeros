"""Offline diagnostics tests: secrets never enter artifacts or console."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import chr_matrix as matrix
from scripts import image_release as release

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'PRIVATE-TOKEN-URL-BODY-DO-NOT-EMIT'
CREDS = {'HUB_USER': 'user', 'HUB_TOKEN': SECRET, 'GH_USER': 'user', 'GH_TOKEN': SECRET}


class RegistryDiagnosticsTests(unittest.TestCase):
    def test_registry_errors_keep_allowlisted_operation_status_and_attempt(self):
        for status, reason in ((401, 'unauthorized'), (403, 'forbidden'), (429, 'rate_limited'), (500, 'http_error')):
            registry = matrix.MatrixRegistry(CREDS, fetch=lambda *a: (status, SECRET.encode(), {}))
            with self.subTest(status=status), self.assertRaises(ValueError) as caught:
                registry.manifest(release.HUB, 'latest')
            self.assertEqual(getattr(caught.exception, 'diagnostic', None), {
                'operation': 'token', 'reason': reason, 'status': status, 'registry': 'docker.io', 'attempt': 1})
            self.assertNotIn(SECRET, str(caught.exception))

    def test_registry_response_failures_never_copy_private_data(self):
        raw = b'{"schemaVersion":2}'
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        cases = [
            ([(200, SECRET.encode(), {})], 'token', 'invalid_json', 200, 1),
            ([(200, b'[]', {})], 'token', 'token_missing', 200, 1),
            ([(200, b'{}', {})], 'token', 'token_missing', 200, 1),
            ([(200, b'{"token":"t"}', {}), (403, SECRET.encode(), {})], 'manifest', 'forbidden', 403, 1),
            ([(200, b'{"token":"t"}', {}), (429, SECRET.encode(), {})], 'manifest', 'rate_limited', 429, 1),
            ([(200, b'{"token":"t"}', {}), (200, raw, {})], 'manifest', 'digest_mismatch', 200, 1),
            ([(200, b'{"token":"t"}', {}), (401, b'', {}),
              (200, b'{"token":"t2"}', {}), (401, SECRET.encode(), {})], 'manifest', 'unauthorized', 401, 2),
            ([(200, b'{"token":"t"}', {}), (401, b'', {}),
              (403, SECRET.encode(), {})], 'token', 'forbidden', 403, 2),
        ]
        for replies, operation, reason, status, attempt in cases:
            fetch = unittest.mock.Mock(side_effect=replies)
            registry = matrix.MatrixRegistry(CREDS, fetch=fetch)
            with self.subTest(reason=reason, attempt=attempt), self.assertRaises(ValueError) as caught:
                registry.manifest(release.GHCR, 'latest')
            self.assertEqual(getattr(caught.exception, 'diagnostic', None), {
                'operation': operation, 'reason': reason, 'status': status, 'registry': 'ghcr.io', 'attempt': attempt})
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertEqual(fetch.call_count, len(replies))

    def test_missing_credentials_and_transport_failures_are_safe(self):
        import urllib.error
        for error, reason in ((TimeoutError(SECRET), 'timeout'),
                              (urllib.error.URLError(SECRET), 'transport_error')):
            registry = matrix.MatrixRegistry(CREDS, fetch=unittest.mock.Mock(side_effect=error))
            with self.assertRaises(ValueError) as caught:
                registry.manifest(release.HUB, 'latest')
            self.assertEqual(getattr(caught.exception, 'diagnostic', {}).get('reason'), reason)
            self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            matrix.MatrixRegistry({}).manifest(release.HUB, 'latest')
        self.assertEqual(caught.exception.diagnostic['reason'], 'credentials_missing')

    def test_config_digest_failure_carries_config_operation(self):
        raw = b'{"schemaVersion":2,"config":{"digest":"sha256:' + b'a' * 64 + b'"}}'
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        fetch = unittest.mock.Mock(side_effect=[(200, b'{"token":"t"}', {}),
            (200, raw, {'Docker-Content-Digest': digest}), (200, SECRET.encode(), {})])
        with self.assertRaises(ValueError) as caught:
            matrix.MatrixRegistry(CREDS, fetch=fetch).config(release.HUB, digest)
        self.assertEqual(getattr(caught.exception, 'diagnostic', None), {
            'operation': 'config', 'reason': 'digest_mismatch', 'status': 200, 'registry': 'docker.io', 'attempt': 1})


class EarlyFailureTests(unittest.TestCase):
    def test_policy_failure_codes_are_exact_and_never_emit_exception_strings(self):
        for message, reason in (
                ('Snapshot must be the original dispatch scope and approval', 'snapshot_identity_mismatch'),
                ('Snapshot target set is incomplete', 'snapshot_targets_mismatch'),
                ('Snapshot preservation scope is incomplete', 'snapshot_preservation_mismatch'),
                ('Missing old latest baseline', 'latest_baseline_missing'),
                ('Invalid baseline digest', 'baseline_digest_invalid'),
                ('Out-of-scope reference changed', 'preserved_reference_changed'),
                ('Source or destination digest mismatch', 'image_digest_mismatch'),
                ('Target drifted from the original dispatch snapshot', 'target_drift'),
                ('Existing alias requires explicit overwrite approval', 'overwrite_not_approved')):
            self.assertEqual(matrix.failure_details(ValueError(message), 'snapshot_validation')['reason'], reason)
            self.assertEqual(matrix.failure_details(ValueError(message + SECRET), 'snapshot_validation')['reason'], 'policy_rejected')

    def test_publication_reports_alias_preflight_before_registry_error(self):
        from test_chr_matrix import PublicationTests
        fixture = PublicationTests(); fixture.setUp()
        row = fixture.data['versions'][0]; registry = fixture.registry()
        digest = 'sha256:' + 'd' * 64
        tag = matrix.source_tag(fixture.sha, row['version'])
        registry.tags[matrix.HUB, tag] = digest; registry.rows[digest] = row
        before = {f'{i}:{t}': None for i in (matrix.HUB, matrix.GHCR) for t in (tag, row['version'], 'v' + row['version'])}
        original = registry.manifest
        def manifest(image, ref):
            if ref == row['version']:
                raise release.RegistryError('manifest', 'rate_limited', image, 429)
            return original(image, ref)
        def runtime(image):
            report = fixture.runtime(row, digest); report['image'] = image; return report
        report = {}
        with patch.object(registry, 'manifest', manifest), self.assertRaises(ValueError):
            matrix.publish_version(registry, registry.copy, lambda *a: self.fail('no rebuild'), runtime,
                                   row, fixture.sha, before, True, 'b' * 64, report)
        self.assertEqual(report.get('stage'), 'alias_preflight')
        self.assertFalse(registry.copies)

    def test_matrix_early_registry_failure_is_retained_in_uploaded_report(self):
        data = matrix.load_manifest(ROOT / 'config/chr-versions.json')
        baseline = unittest.mock.Mock()
        baseline.manifest.return_value = ('sha256:' + 'f' * 64, {})
        snapshot = matrix.create_snapshot(baseline, data, 'a' * 40, '123', True)
        env = {'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '123', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_REPOSITORY': release.REPOSITORY, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'APPROVE_VERSION_OVERWRITE': 'true', 'CHR_VERSION': '7.25beta5'}
        registry = matrix.MatrixRegistry(CREDS, fetch=lambda *a: (429, SECRET.encode(), {}))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env):
            root = Path(directory); path = root / 'snapshot.json'; path.write_text(json.dumps(snapshot))
            captured = io.StringIO()
            with patch.object(matrix, 'MatrixRegistry', return_value=registry), \
                    patch.object(matrix, 'MatrixCopy', side_effect=AssertionError('must not login')), \
                    patch.object(matrix.subprocess, 'run', side_effect=AssertionError('must not run')), \
                    contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                result = matrix.main(['variant', '--snapshot', str(path), '--output-dir', str(root / 'out')])
            text = (root / 'out/report.json').read_text(); report = json.loads(text)
            self.assertEqual(result, 1)
            self.assertEqual(report['version'], '7.25beta5')
            self.assertEqual(report['failure'], {'stage': 'preservation_check', 'operation': 'token',
                'reason': 'rate_limited', 'status': 429, 'registry': 'docker.io', 'attempt': 1})
            self.assertEqual(report['stage_details'], {'reference_index': 1, 'reference_count': 8})
            self.assertNotIn(SECRET, text + captured.getvalue())

    def test_early_snapshot_failure_retains_valid_version_and_safe_stage(self):
        env = {'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '123', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_REPOSITORY': release.REPOSITORY, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'APPROVE_VERSION_OVERWRITE': 'true', 'CHR_VERSION': '7.25beta5'}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env):
            root = Path(directory)
            (root / 'snapshot.json').write_text('{"secret":"' + SECRET + '"}')
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                result = matrix.main(['variant', '--snapshot', str(root / 'snapshot.json'), '--output-dir', str(root / 'out')])
            text = (root / 'out/report.json').read_text()
            report = json.loads(text)
            self.assertEqual(result, 1)
            self.assertEqual(report.get('version'), '7.25beta5')
            self.assertEqual(report.get('failure'), {'stage': 'snapshot_validation', 'reason': 'missing_field'})
            self.assertNotIn(SECRET, text + captured.getvalue())


class CommandDiagnosticsTests(unittest.TestCase):
    def test_output_is_classified_into_fixed_codes_only(self):
        import subprocess
        cases = [(b'permission_denied: write_package ' + SECRET.encode(), 'registry_permission_denied'),
                 (b'toomanyrequests: ' + SECRET.encode(), 'registry_rate_limited'),
                 (b'no space left on device ' + SECRET.encode(), 'storage_full'),
                 (b'SHA256 checksum did NOT match ' + SECRET.encode(), 'checksum_mismatch'),
                 (b'TLS handshake timeout ' + SECRET.encode(), 'transport_timeout')]
        for stage in ('build', 'copy'):
            for output, reason in cases:
                def run(argv, **kwargs):
                    kwargs['stdout'].write(output)
                    return subprocess.CompletedProcess(argv, 1)
                with self.subTest(stage=stage, reason=reason), patch.object(matrix.subprocess, 'run', run):
                    with self.assertRaises(matrix.CommandFailure) as caught:
                        matrix.run_command(['unused', SECRET], 1, stage=stage)
                    self.assertEqual(caught.exception.failure.get('output_reason'), reason)
                    self.assertNotIn(SECRET, json.dumps(caught.exception.failure) + str(caught.exception))

    def test_classifier_is_bounded_and_unknown_output_is_not_retained(self):
        import subprocess
        def run(argv, **kwargs):
            kwargs['stdout'].write(b'x' * 200000 + SECRET.encode())
            return subprocess.CompletedProcess(argv, 1)
        with patch.object(matrix.subprocess, 'run', run):
            with self.assertRaises(matrix.CommandFailure) as caught:
                matrix.run_command(['unused'], 1, stage='build')
        self.assertEqual(caught.exception.failure, {'stage': 'build', 'reason': 'nonzero_exit', 'exit_code': 1, 'timed_out': False})


class ReadOnlyDiagnosticTests(unittest.TestCase):
    def module(self):
        self.assertTrue((ROOT / 'scripts/chr_diagnostics.py').exists(), 'read-only diagnostic entry point missing')
        from scripts import chr_diagnostics
        return chr_diagnostics

    def fixture(self, d):
        data = matrix.load_manifest(ROOT / 'config/chr-versions.json')
        registry = unittest.mock.Mock()
        registry.manifest.return_value = ('sha256:' + 'f' * 64, {})
        return matrix.create_snapshot(registry, data, d.ORIGINAL_SOURCE_SHA, d.ORIGINAL_RUN_ID, True)

    def invoke(self, d, snapshot, *, env_override=None, bad_hash=False, registry_error=None):
        env = {'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '999', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REPOSITORY': release.REPOSITORY,
               'ORIGINAL_SOURCE_SHA': d.ORIGINAL_SOURCE_SHA, 'ORIGINAL_RUN_ID': d.ORIGINAL_RUN_ID}
        env.update(env_override or {})
        raw = json.dumps(snapshot).encode()
        registry = unittest.mock.Mock()
        registry.manifest.return_value = ('sha256:' + 'f' * 64, {})
        if registry_error:
            registry.manifest.side_effect = registry_error
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'snapshot.json'; path.write_bytes(raw)
            captured = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch.object(d, 'MatrixRegistry', return_value=registry), \
                    patch.object(d, 'SNAPSHOT_SHA256', '0' * 64 if bad_hash else hashlib.sha256(raw).hexdigest()), \
                    patch.object(matrix, 'create_snapshot', side_effect=AssertionError('no rebaseline')), \
                    patch.object(matrix, 'MatrixCopy', side_effect=AssertionError('no copier')), \
                    patch.object(matrix.subprocess, 'run', side_effect=AssertionError('no processes')), \
                    contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                result = d.main(['--snapshot', str(path), '--output-dir', str(root / 'out')])
            self.assertEqual(path.read_bytes(), raw)
            text = (root / 'out/report.json').read_text()
            self.assertNotIn(SECRET, text + captured.getvalue())
            return result, json.loads(text), registry

    def test_pins_original_identity_and_reads_only_preserved_references(self):
        d = self.module()
        self.assertEqual(d.ORIGINAL_RUN_ID, '35607084875')
        self.assertEqual(d.ORIGINAL_SOURCE_SHA, '858d85fd17ff720df88a65b44ec65ecf198dd8dd')
        self.assertEqual(d.SNAPSHOT_SHA256, '4deddff21e16cc5a22d5d1d5c134253395c3e2feb88d1086f699c3c16091772d')
        code, report, registry = self.invoke(d, self.fixture(d))
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'success')
        self.assertEqual(report['source_sha'], d.ORIGINAL_SOURCE_SHA)
        self.assertEqual(report['executor_sha'], 'b' * 40)
        self.assertEqual(registry.manifest.call_count, 8)
        self.assertEqual({c.args for c in registry.manifest.call_args_list},
                         {(i, t) for i in (matrix.HUB, matrix.GHCR) for t in matrix.PRESERVED_TAGS})

    def test_all_identity_snapshot_and_dispatch_boundaries_fail_before_network(self):
        d = self.module()
        for env in ({'ORIGINAL_RUN_ID': '999'}, {'ORIGINAL_SOURCE_SHA': 'c' * 40},
                    {'GITHUB_REF': 'refs/heads/feature'}, {'GITHUB_EVENT_NAME': 'push'},
                    {'GITHUB_REPOSITORY': 'other/repo'}, {'GITHUB_SHA': SECRET}):
            code, report, registry = self.invoke(d, self.fixture(d), env_override=env)
            self.assertEqual(code, 1)
            registry.manifest.assert_not_called()
        for field, value in [('run_id', '999'), ('source_sha', 'c' * 40),
                             ('approve_version_overwrite', False), ('before', {}), ('preserved', {})]:
            snapshot = self.fixture(d); snapshot[field] = value
            code, _, registry = self.invoke(d, snapshot)
            self.assertEqual(code, 1)
            registry.manifest.assert_not_called()
        code, report, registry = self.invoke(d, self.fixture(d), bad_hash=True)
        self.assertEqual(code, 1)
        self.assertEqual(report['failure']['stage'], 'snapshot_read')
        registry.manifest.assert_not_called()

    def test_preserved_registry_failure_is_structured_in_final_artifact(self):
        d = self.module()
        error = release.RegistryError('token', 'forbidden', release.HUB, 403, 1)
        code, report, registry = self.invoke(d, self.fixture(d), registry_error=error)
        self.assertEqual(code, 1)
        self.assertEqual(report['failure'], {'stage': 'preservation_check', **error.diagnostic})
        self.assertEqual(report['stage_details'], {'reference_index': 1, 'reference_count': 8})
        self.assertEqual(registry.manifest.call_count, 1)

    def test_workflow_has_no_publication_surface_or_variable_original_identity(self):
        path = ROOT / '.github/workflows/chr-matrix-diagnostics.yml'
        self.assertTrue(path.exists(), 'manual read-only workflow missing')
        text = path.read_text()
        for required in ('workflow_dispatch:', "github.ref == 'refs/heads/main'", 'contents: read',
                         'actions: read', 'packages: read', 'run-id: 35607084875',
                         'name: chr-matrix-snapshot', 'github-token: ${{ github.token }}',
                         'ORIGINAL_SOURCE_SHA: 858d85fd17ff720df88a65b44ec65ecf198dd8dd',
                         "ORIGINAL_RUN_ID: '35607084875'", 'persist-credentials: false',
                         'secrets.DOCKERHUB_USERNAME', 'secrets.DOCKERHUB_TOKEN',
                         'GH_USER: ${{ github.actor }}', 'GH_TOKEN: ${{ github.token }}',
                         'python3 scripts/chr_diagnostics.py', 'if: always()', 'timeout-minutes: 20'):
            self.assertIn(required, text)
        for forbidden in ('packages: write', 'contents: write', 'actions: write', 'docker/', 'skopeo',
                          'chr_matrix.py prepare', 'chr_matrix.py variant', 'chr_matrix.py aggregate', 'inputs:', 'overwrite: true'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()
