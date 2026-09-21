"""Offline guardrails for the fixed original-dispatch recovery."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import chr_matrix as matrix
from scripts import chr_diagnostics as original

ROOT = Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def module(self):
        self.assertTrue((ROOT / 'scripts/chr_recovery.py').exists(), 'bounded recovery entry point missing')
        from scripts import chr_recovery
        return chr_recovery

    def test_identity_requires_main_manual_distinct_executor_and_approval(self):
        recovery = self.module()
        env = {'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '1',
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_REPOSITORY': matrix.REPOSITORY, 'APPROVE_VERSION_OVERWRITE': 'true'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(recovery.identity(), {'executor_sha': 'b' * 40, 'run_id': '999',
                'run_attempt': '1', 'original_run_id': original.ORIGINAL_RUN_ID,
                'snapshot_sha256': original.SNAPSHOT_SHA256})
            for key, value in [('GITHUB_SHA', original.ORIGINAL_SOURCE_SHA),
                               ('GITHUB_SHA', 'SECRET'), ('GITHUB_RUN_ID', original.ORIGINAL_RUN_ID),
                               ('GITHUB_RUN_ATTEMPT', '0'), ('GITHUB_RUN_ID', '1x'),
                               ('GITHUB_REF', 'refs/heads/other'), ('GITHUB_EVENT_NAME', 'push'),
                               ('GITHUB_REPOSITORY', 'foreign/repo'), ('APPROVE_VERSION_OVERWRITE', 'false')]:
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                    recovery.identity()
        self.assertEqual(recovery.FAILED_VERSIONS,
                         ('6.49.21', '6.49.22', '7.23.5', '7.24.2', '7.24.3', '7.24.4', '7.25beta5'))

    def fixture(self):
        from test_chr_matrix import PublicationTests
        fixture = PublicationTests(); fixture.setUp()
        fixture.sha = original.ORIGINAL_SOURCE_SHA
        registry = fixture.registry()
        for image in (matrix.HUB, matrix.GHCR):
            registry.tags[image, 'latest'] = 'sha256:' + 'f' * 64
        snapshot = matrix.create_snapshot(registry, fixture.data, fixture.sha, original.ORIGINAL_RUN_ID, True)
        reports = []
        for n, row in enumerate(fixture.data['versions']):
            if row['version'] in self.module().FAILED_VERSIONS:
                continue
            digest = 'sha256:' + f'{n:064x}'
            registry.rows[digest] = row
            for image in (matrix.HUB, matrix.GHCR):
                for tag in (row['version'], 'v' + row['version'], matrix.source_tag(fixture.sha, row['version'])):
                    registry.tags[image, tag] = digest
            runtime = fixture.runtime(row, digest); runtime['image'] = matrix.HUB + '@' + digest
            reports.append({'status': 'success', 'version': row['version'], 'source_sha': fixture.sha,
                            'run_id': original.ORIGINAL_RUN_ID, 'digest': digest, 'platforms': matrix.PLATFORMS,
                            'runtime_tested': ['linux/amd64'], 'build_only': ['linux/arm64'], 'runtime': runtime})
        return fixture, registry, snapshot, reports

    def test_preflight_reuses_ten_and_rejects_alias_or_original_drift(self):
        recovery = self.module()
        self.assertTrue(hasattr(recovery, 'preflight'), 'full read-only recovery preflight missing')
        fixture, registry, snapshot, reports = self.fixture()
        recovery.preflight(registry, fixture.data, snapshot, reports, 'b' * 64, {})
        self.assertFalse(registry.copies)
        # A new immutable original-source seed is reusable, but unknown alias drift is not.
        row = next(r for r in fixture.data['versions'] if r['version'] == '7.24.4')
        digest = 'sha256:' + 'd' * 64
        registry.rows[digest] = row
        registry.tags[matrix.HUB, matrix.source_tag(fixture.sha, row['version'])] = digest
        registry.tags[matrix.HUB, row['version']] = digest
        recovery.preflight(registry, fixture.data, snapshot, reports, 'b' * 64, {})
        for image, tag in [(matrix.HUB, '7.24.4'), (matrix.GHCR, '6.49.18'),
                           (matrix.HUB, 'latest'), (matrix.HUB, matrix.PRESERVED_TAGS[0])]:
            saved = registry.tags.get((image, tag))
            registry.tags[image, tag] = 'sha256:' + 'e' * 64
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                recovery.preflight(registry, fixture.data, snapshot, reports, 'b' * 64, {})
            if saved is None: registry.tags.pop((image, tag))
            else: registry.tags[image, tag] = saved
        for bad in [reports[:-1], reports + reports[:1], [dict(r, source_sha='c' * 40) for r in reports]]:
            with self.assertRaises(ValueError):
                recovery.preflight(registry, fixture.data, snapshot, bad, 'b' * 64, {})
        self.assertFalse(registry.copies)

    def test_source_and_snapshot_are_original_not_executor(self):
        recovery = self.module()
        self.assertTrue(hasattr(recovery, 'load_inputs'), 'original source/evidence verifier missing')
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source'
            # Synthetic Git fixture also works in shallow CI checkouts. Real pins are
            # checked separately against the retained original evidence before dispatch.
            source.mkdir(); (source / 'tests').mkdir()
            for path in ('Dockerfile', 'tests/chr-smoke.py', 'tests/docker-integration.py'):
                (source / path).write_text('fixture\n')
            (source / '.gitignore').write_text('.env\n')
            def git(*args):
                return subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True).stdout.decode().strip()
            git('init', '--quiet'); git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet', '-m', 'fixture')
            with patch.object(recovery, 'ORIGINAL_SOURCE_SHA', git('rev-parse', 'HEAD')), \
                    patch.object(recovery, 'HARNESS_SHA256', hashlib.sha256(b'fixture\n').hexdigest()):
                self.assertEqual(recovery.source_identity(source), recovery.HARNESS_SHA256)
                for path in ('Dockerfile', 'tests/chr-smoke.py', '.env'):
                    target = source / path; before = target.read_bytes() if target.exists() else None
                    target.write_text('SECRET-ALTERED-SOURCE')
                    with self.subTest(path=path), self.assertRaises(ValueError):
                        recovery.source_identity(source)
                    if before is None: target.unlink()
                    else: target.write_bytes(before)
            with self.assertRaises(ValueError):
                recovery.source_identity(ROOT)
            snapshot = root / 'snapshot.json'; snapshot.write_text('{}')
            with self.assertRaises(ValueError):
                recovery.load_inputs(source, snapshot, root / 'originals', {})

    def test_nested_original_checkout_import_does_not_write_bytecode(self):
        import re
        import subprocess
        import sys
        recovery = self.module()
        workflow = (ROOT / '.github/workflows/chr-matrix-recovery.yml').read_text()
        workflow_env = workflow.split('\nenv:\n', 1)[1].split('\njobs:', 1)[0]
        self.assertRegex(workflow_env, r"(?m)^  PYTHONDONTWRITEBYTECODE: '1'$")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'executor/original-source'
            (source / 'tests').mkdir(parents=True)
            (source / 'tests/helper.py').write_text('VALUE = 1\n')
            harness = source / 'tests/docker-integration.py'
            harness.write_text('import helper\nassert helper.VALUE == 1\n')
            def git(*args):
                return subprocess.run(['git', '-C', str(source), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '--quiet'); git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '--quiet', '-m', 'fixture')
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE=re.search(
                r"PYTHONDONTWRITEBYTECODE: '([^']+)'", workflow_env).group(1))
            with patch.object(recovery, 'ORIGINAL_SOURCE_SHA', git('rev-parse', 'HEAD')), \
                    patch.object(recovery, 'HARNESS_SHA256', hashlib.sha256(harness.read_bytes()).hexdigest()):
                subprocess.run([sys.executable, str(harness)], env=env, check=True, capture_output=True)
                self.assertEqual(recovery.source_identity(source), recovery.HARNESS_SHA256)
                self.assertFalse(list(source.rglob('*.pyc')))
                # Negative control demonstrates why nested checkout imports need the flag.
                env.pop('PYTHONDONTWRITEBYTECODE')
                env.pop('PYTHONPYCACHEPREFIX', None)
                subprocess.run([sys.executable, str(harness)], env=env, check=True, capture_output=True)
                self.assertTrue(list(source.rglob('*.pyc')))
                with self.assertRaises(ValueError): recovery.source_identity(source)

    def test_original_artifact_bytes_are_pinned_before_reuse(self):
        recovery = self.module()
        self.assertTrue(hasattr(recovery, 'load_originals'), 'original report byte pins missing')
        fixture, _, _, reports = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); pins = {}
            for report in reports:
                version = report['version']; path = root / ('chr-matrix-version-' + version) / 'report.json'
                path.parent.mkdir(); path.write_text(json.dumps(report))
                pins[version] = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(recovery, 'ORIGINAL_REPORT_HASHES', pins):
                self.assertEqual(recovery.load_originals(root), [next(r for r in reports if r['version'] == v) for v in pins])
                path.write_text(path.read_text() + ' ')
                with self.assertRaises(ValueError): recovery.load_originals(root)
            self.assertEqual(set(recovery.ORIGINAL_REPORT_HASHES), {r['version'] for r in reports})

    def invoke(self, command='variant', version='7.24.4', failure=None, existing=False,
               runtime_text=None, timed_out=True):
        recovery = self.module()
        self.assertTrue(hasattr(recovery, 'main'), 'recovery orchestration missing')
        fixture, registry, snapshot, originals = self.fixture()
        row = next(r for r in fixture.data['versions'] if r['version'] == version)
        digest = 'sha256:' + 'd' * 64
        if existing:
            registry.rows[digest] = row
            registry.tags[matrix.HUB, matrix.source_tag(fixture.sha, version)] = digest
        commands = []
        class Copier:
            def __init__(self, allowed): self.allowed = allowed
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def copy_image(self, source, digest, image, tag):
                if (image, tag) not in self.allowed: raise AssertionError('outside scope')
                registry.copy(source, digest, image, tag)
        env = {'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '1',
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_REPOSITORY': matrix.REPOSITORY, 'APPROVE_VERSION_OVERWRITE': 'true',
               'CHR_VERSION': version}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source'; source.mkdir()
            def run(argv, timeout, *, stage):
                commands.append((argv, stage))
                if stage == 'build':
                    registry.rows[digest] = row
                    registry.tags[matrix.HUB, matrix.source_tag(fixture.sha, version)] = digest
                    Path(argv[argv.index('--metadata-file') + 1]).write_text(json.dumps({'containerimage.digest': digest}))
                if stage == 'runtime':
                    result = fixture.runtime(row, digest); result['image'] = matrix.HUB + '@' + digest
                    if failure:
                        result.update(status='failed', error='SECRET-RAW-TRANSCRIPT', transcript='SECRET-RAW-TRANSCRIPT')
                    evidence = Path(argv[argv.index('--output-dir') + 1]); evidence.mkdir()
                    (evidence / 'report.json').write_text(json.dumps(result) if runtime_text is None else runtime_text)
                if stage == failure:
                    raise matrix.CommandFailure(stage, None if timed_out else 37, timed_out=timed_out)
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(recovery, 'load_inputs', return_value=(fixture.data, snapshot, originals, 'b' * 64)), \
                    patch.object(recovery, 'source_identity', return_value='b' * 64), \
                    patch.object(recovery, 'RecoveryRegistry', return_value=registry), \
                    patch.object(matrix, 'MatrixCopy', Copier), patch.object(matrix, 'run_command', run):
                argv = [command, '--source-tree', str(source), '--output-dir', str(root / 'out')]
                result = recovery.main(argv)
                text = (root / 'out/report.json').read_text()
                # Same attempt cannot destructively overwrite evidence.
                self.assertEqual(recovery.main(argv), 1)
                self.assertEqual((root / 'out/report.json').read_text(), text)
            self.assertNotIn('SECRET-RAW-TRANSCRIPT', text)
            return result, json.loads(text), commands, registry, str(source)

    def test_variant_builds_original_tree_and_reports_distinct_provenance(self):
        result, report, commands, registry, source = self.invoke()
        self.assertEqual(result, 0)
        self.assertEqual(report['run_id'], original.ORIGINAL_RUN_ID)
        self.assertEqual(report['source_sha'], original.ORIGINAL_SOURCE_SHA)
        self.assertEqual(report['recovery']['executor_sha'], 'b' * 40)
        self.assertEqual(report['recovery']['run_id'], '999')
        build = next(argv for argv, stage in commands if stage == 'build')
        self.assertEqual(build[-1], source)
        self.assertEqual(build[build.index('--file') + 1], source + '/Dockerfile')
        self.assertIn('SOURCE_REVISION=' + original.ORIGINAL_SOURCE_SHA, build)
        runtime = next(argv for argv, stage in commands if stage == 'runtime')
        self.assertEqual(runtime[1], source + '/tests/docker-integration.py')
        self.assertEqual([registry.tags[i, 'latest'] for i in (matrix.HUB, matrix.GHCR)], ['sha256:' + 'f' * 64] * 2)
        self.assertEqual(len(report['original_readbacks']), 60)

    def test_valid_seed_reused_without_build_and_failure_cannot_promote(self):
        code, report, commands, registry, _ = self.invoke(existing=True)
        self.assertEqual(code, 0)
        self.assertNotIn('build', [stage for _, stage in commands])
        for stage in ('build', 'pull', 'runtime'):
            code, report, commands, registry, _ = self.invoke(failure=stage)
            self.assertEqual(code, 1)
            self.assertEqual(report['failure'], {'stage': stage, 'reason': 'timeout', 'exit_code': None, 'timed_out': True})
            self.assertFalse(registry.copies)
        code, report, commands, registry, _ = self.invoke(version='6.49.18')
        self.assertEqual(code, 1)
        self.assertFalse(commands)
        self.assertFalse(registry.copies)

    def test_partial_runtime_report_preserves_primary_failure(self):
        for text in ('[]', '{SECRET-RAW-TRANSCRIPT'):
            code, report, _, registry, _ = self.invoke(failure='runtime', runtime_text=text)
            self.assertEqual(code, 1)
            self.assertEqual(report['failure']['reason'], 'timeout')
            self.assertEqual(report['runtime']['failure']['reason'], 'invalid_report')
            self.assertFalse(registry.copies)
        code, report, _, registry, _ = self.invoke(failure='runtime', timed_out=False)
        self.assertEqual(report['failure'], {'stage': 'runtime', 'reason': 'nonzero_exit',
                                             'exit_code': 37, 'timed_out': False})
        self.assertEqual(report['runtime']['failure']['reason'], 'unclassified_failure')

    def attempt_fixture(self, root, attempt='2'):
        recovery = self.module()
        provenance = {'executor_sha': 'b' * 40, 'run_id': '999', 'run_attempt': attempt,
                      'original_run_id': original.ORIGINAL_RUN_ID, 'snapshot_sha256': original.SNAPSHOT_SHA256}
        jobs = []
        for index, version in enumerate(recovery.FAILED_VERSIONS, 1):
            path = root / f'chr-recovery-version-{version}-1' / 'report.json'
            path.parent.mkdir(); path.write_text(json.dumps({'version': version, 'status': 'success',
                'recovery': dict(provenance, run_attempt='1')}))
            jobs.append({'id': index, 'run_id': 999, 'run_attempt': 1,
                'run_url': f'https://api.github.com/repos/{matrix.REPOSITORY}/actions/runs/999',
                'head_sha': provenance['executor_sha'], 'name': f'variant ({version})',
                'status': 'completed', 'conclusion': 'success'})
        return provenance, jobs

    def jobs_response(self, jobs):
        import subprocess
        return subprocess.CompletedProcess([], 0, json.dumps([{'total_count': len(jobs), 'jobs': jobs}]), '')

    def test_missing_newer_report_cannot_reuse_older_success(self):
        recovery = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); provenance, jobs = self.attempt_fixture(root)
            # Runner/upload died in attempt 2: no newer artifact exists at all.
            jobs.append(dict(jobs[0], id=100, run_attempt=2, conclusion='failure'))
            with patch('subprocess.run', return_value=self.jobs_response(jobs)), self.assertRaises(ValueError):
                recovery.load_recovered(root, provenance)

    def test_successful_newer_job_requires_its_exact_uploaded_report(self):
        recovery = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); provenance, jobs = self.attempt_fixture(root)
            jobs.append(dict(jobs[0], id=100, run_attempt=2))
            with patch('subprocess.run', return_value=self.jobs_response(jobs)), self.assertRaises(ValueError):
                recovery.load_recovered(root, provenance)

    def test_latest_execution_blocks_failure_cancel_timeout_and_upload_failure(self):
        recovery = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); provenance, jobs = self.attempt_fixture(root, attempt='3')
            for conclusion in ('failure', 'cancelled', 'timed_out', 'skipped', None):
                newer = dict(jobs[0], id=100, run_attempt=2, conclusion=conclusion)
                for uploaded in (False, True):
                    with self.subTest(conclusion=conclusion, uploaded=uploaded):
                        reports = root / ('uploaded' if uploaded else 'missing')
                        reports.mkdir(exist_ok=True)
                        if uploaded:
                            artifact = reports / 'chr-recovery-version-6.49.21-2'
                            artifact.mkdir(exist_ok=True)
                            (artifact / 'report.json').write_text(json.dumps({'status': 'success',
                                'version': '6.49.21', 'recovery': dict(provenance, run_attempt='2')}))
                        # A later aggregate-only attempt must not hide attempt 2.
                        aggregate = dict(jobs[0], id=101, name='aggregate', run_attempt=3,
                                         status='in_progress', conclusion=None)
                        with patch('subprocess.run', return_value=self.jobs_response(jobs + [newer, aggregate])), \
                                self.assertRaisesRegex(ValueError, 'Latest variant execution'):
                            recovery.load_recovered(reports if uploaded else root, provenance)

    def test_aggregate_only_rerun_keeps_versions_not_rerun(self):
        recovery = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); provenance, jobs = self.attempt_fixture(root, attempt='3')
            jobs.append(dict(jobs[0], id=100, name='aggregate', run_attempt=3,
                             status='in_progress', conclusion=None))
            with patch('subprocess.run', return_value=self.jobs_response(jobs)) as api:
                recovered = recovery.load_recovered(root, provenance)
            self.assertEqual({r['recovery']['run_attempt'] for r in recovered}, {'1'})
            self.assertEqual(len(recovered), 7)
            self.assertEqual(api.call_args.args[0], ['gh', 'api', '--hostname', 'github.com',
                '--paginate', '--slurp', f'repos/{matrix.REPOSITORY}/actions/runs/999/jobs?filter=all&per_page=100'])
            self.assertTrue(api.call_args.kwargs['check'])
            self.assertEqual(api.call_args.kwargs['timeout'], 120)

    def test_failed_jobs_rerun_replaces_only_reexecuted_versions(self):
        recovery = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); provenance, jobs = self.attempt_fixture(root, attempt='3')
            version = recovery.FAILED_VERSIONS[0]
            jobs[0]['conclusion'] = 'failure'
            jobs.append(dict(jobs[0], id=100, run_attempt=2, conclusion='success'))
            artifact = root / f'chr-recovery-version-{version}-2'; artifact.mkdir()
            (artifact / 'report.json').write_text(json.dumps({'status': 'success', 'version': version,
                'recovery': dict(provenance, run_attempt='2')}))
            with patch('subprocess.run', return_value=self.jobs_response(jobs)):
                recovered = recovery.load_recovered(root, provenance)
            self.assertEqual({r['version']: r['recovery']['run_attempt'] for r in recovered},
                             {v: '2' if v == version else '1' for v in recovery.FAILED_VERSIONS})
            # Another rerun succeeds, but a different unresolved failure remains.
            jobs[1]['conclusion'] = 'failure'
            with patch('subprocess.run', return_value=self.jobs_response(jobs)), self.assertRaises(ValueError):
                recovery.load_recovered(root, provenance)

    def test_jobs_evidence_is_complete_and_bound_to_run_source_and_attempt(self):
        recovery = self.module()
        with tempfile.TemporaryDirectory() as directory:
            provenance, jobs = self.attempt_fixture(Path(directory))
            for changes in ({'run_id': 998}, {'run_url': 'https://api.github.com/repos/foreign/repo/actions/runs/999'},
                            {'head_sha': 'c' * 40}, {'run_attempt': 3}, {'run_attempt': 0},
                            {'run_attempt': '1'}, {'name': 'variant (foreign)'}, {'status': 'in_progress'}):
                bad = [dict(jobs[0], **changes)] + jobs[1:]
                with self.subTest(changes=changes), \
                        patch('subprocess.run', return_value=self.jobs_response(bad)), self.assertRaises(ValueError):
                    recovery.latest_variant_attempts(provenance)
            for bad in (jobs[:-1], jobs + jobs[:1], jobs + [dict(jobs[0], id=100)]):
                with patch('subprocess.run', return_value=self.jobs_response(bad)), self.assertRaises(ValueError):
                    recovery.latest_variant_attempts(provenance)
            result = self.jobs_response(jobs)
            result.stdout = json.dumps([{'total_count': len(jobs), 'jobs': jobs[:3]},
                                       {'total_count': len(jobs), 'jobs': jobs[3:]}])
            with patch('subprocess.run', return_value=result):
                self.assertEqual(recovery.latest_variant_attempts(provenance), {v: '1' for v in recovery.FAILED_VERSIONS})
            result.stdout = json.dumps([{'total_count': len(jobs) + 1, 'jobs': jobs}])
            with patch('subprocess.run', return_value=result), self.assertRaises(ValueError):
                recovery.latest_variant_attempts(provenance)

    def test_api_failure_or_missing_report_blocks_cli_before_copier(self):
        import subprocess
        recovery = self.module()
        fixture, registry, snapshot, originals = self.fixture()
        env = {'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '2',
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_REPOSITORY': matrix.REPOSITORY, 'APPROVE_VERSION_OVERWRITE': 'true'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); provenance, jobs = self.attempt_fixture(root)
            jobs.append(dict(jobs[0], id=100, run_attempt=2))
            errors = [None, subprocess.CalledProcessError(1, ['gh'], stderr='SECRET'),
                      subprocess.TimeoutExpired(['gh'], 120), FileNotFoundError('SECRET')]
            for index, error in enumerate(errors):
                output = root / f'out-{index}'
                with patch.dict(os.environ, env, clear=True), \
                        patch.object(recovery, 'load_inputs', return_value=(fixture.data, snapshot, originals, 'b' * 64)), \
                        patch.object(recovery, 'RecoveryRegistry', return_value=registry), \
                        patch('subprocess.run', return_value=self.jobs_response(jobs), side_effect=error), \
                        patch.object(matrix, 'MatrixCopy') as copier:
                    self.assertEqual(recovery.main(['aggregate', '--reports', str(root), '--output-dir', str(output)]), 1)
                    copier.assert_not_called()
                report = (output / 'report.json').read_text()
                self.assertNotIn('SECRET', report)
                self.assertEqual(json.loads(report)['status'], 'failed')
                self.assertFalse(registry.copies)

    def test_recovery_artifacts_select_newest_attempt_without_hiding_failure(self):
        recovery = self.module()
        self.assertTrue(hasattr(recovery, 'load_recovered'), 'attempt-aware recovery aggregation missing')
        provenance = {'executor_sha': 'b' * 40, 'run_id': '999', 'run_attempt': '2',
                      'original_run_id': original.ORIGINAL_RUN_ID, 'snapshot_sha256': original.SNAPSHOT_SHA256}
        attempts = {v: '1' for v in recovery.FAILED_VERSIONS}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(recovery, 'latest_variant_attempts', return_value=attempts):
            root = Path(directory)
            def put(version, attempt, **changes):
                report = {'version': version, 'status': 'success',
                          'recovery': dict(provenance, run_attempt=str(attempt))}
                report.update(changes)
                path = root / f'chr-recovery-version-{version}-{attempt}' / 'report.json'
                path.parent.mkdir(exist_ok=True); path.write_text(json.dumps(report)); return path
            for version in recovery.FAILED_VERSIONS: put(version, 1)
            self.assertEqual(len(recovery.load_recovered(root, provenance)), 7)
            path = put('7.24.4', 2, status='failed')
            attempts['7.24.4'] = '2'
            self.assertEqual(next(r for r in recovery.load_recovered(root, provenance) if r['version'] == '7.24.4')['status'], 'failed')
            for bad in (dict(provenance, run_id='foreign'), dict(provenance, executor_sha='c' * 40),
                        dict(provenance, run_attempt='3')):
                put('7.24.4', 2, recovery=bad)
                with self.assertRaises(ValueError): recovery.load_recovered(root, provenance)
            path.unlink(); path.parent.rmdir()
            put('6.49.18', 1)
            with self.assertRaises(ValueError): recovery.load_recovered(root, provenance)

    def test_aggregate_requires_complete_original_and_recovery_qualification(self):
        recovery = self.module()
        self.assertTrue(hasattr(recovery, 'recover_aggregate'), 'gated latest recovery missing')
        fixture, registry, snapshot, originals = self.fixture()
        recovered = []
        for n, row in enumerate(fixture.data['versions'], 100):
            if row['version'] not in recovery.FAILED_VERSIONS: continue
            digest = 'sha256:' + f'{n:064x}'
            registry.rows[digest] = row
            for image in (matrix.HUB, matrix.GHCR):
                for tag in (row['version'], 'v' + row['version'], matrix.source_tag(fixture.sha, row['version'])):
                    registry.tags[image, tag] = digest
            runtime = fixture.runtime(row, digest); runtime['image'] = matrix.HUB + '@' + digest
            recovered.append({'status': 'success', 'version': row['version'], 'source_sha': fixture.sha,
                'run_id': original.ORIGINAL_RUN_ID, 'digest': digest, 'platforms': matrix.PLATFORMS,
                'runtime_tested': ['linux/amd64'], 'build_only': ['linux/arm64'], 'runtime': runtime})
        class Copier:
            def __init__(self, allowed):
                self.assert_allowed = allowed == {(image, 'latest') for image in (matrix.HUB, matrix.GHCR)}
                if not self.assert_allowed: raise AssertionError('scope')
            def __enter__(self): return self
            def __exit__(self, *args): pass
            copy_image = staticmethod(registry.copy)
        with patch.object(matrix, 'MatrixCopy', Copier):
            for bad in (recovered[:-1], recovered + recovered[:1], [dict(r, status='failed') for r in recovered]):
                with self.assertRaises(ValueError):
                    recovery.recover_aggregate(registry, fixture.data, originals, bad, snapshot, 'b' * 64, {})
                self.assertFalse(registry.copies)
            report = {}
            recovery.recover_aggregate(registry, fixture.data, originals, recovered, snapshot, 'b' * 64, report)
        self.assertEqual(report['count'], 17)
        self.assertEqual(report['latest_version'], '7.24.4')
        self.assertEqual(len(registry.copies), 2)

    def test_cli_preflight_is_read_only_and_registry_failure_is_sanitized(self):
        code, report, commands, registry, _ = self.invoke(command='preflight')
        self.assertEqual(code, 0)
        self.assertFalse(commands)
        self.assertFalse(registry.copies)
        recovery = self.module()
        from scripts.image_release import RegistryError
        fixture, registry, snapshot, originals = self.fixture()
        env = {'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '1',
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_REPOSITORY': matrix.REPOSITORY, 'APPROVE_VERSION_OVERWRITE': 'true'}
        error = RegistryError('token', 'forbidden', matrix.HUB, 403)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True), \
                patch.object(recovery, 'load_inputs', return_value=(fixture.data, snapshot, originals, 'b' * 64)), \
                patch.object(recovery, 'RecoveryRegistry', return_value=registry), \
                patch.object(registry, 'manifest', side_effect=error), \
                patch.object(matrix, 'MatrixCopy', side_effect=AssertionError('no login')):
            output = Path(directory) / 'out'
            self.assertEqual(recovery.main(['preflight', '--output-dir', str(output)]), 1)
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['failure'], {'stage': 'preservation_check', **error.diagnostic})

    def test_workflow_bounds_source_executor_artifact_and_publication_scope(self):
        path = ROOT / '.github/workflows/chr-matrix-recovery.yml'
        self.assertTrue(path.exists(), 'manual recovery workflow missing')
        text = path.read_text()
        for required in ('workflow_dispatch:', "github.ref == 'refs/heads/main'", 'chr-image-publication',
                         'run-id: 35607084875', 'name: chr-matrix-snapshot',
                         'ref: 858d85fd17ff720df88a65b44ec65ecf198dd8dd', 'path: original-source',
                         'ref: ${{ github.sha }}', 'persist-credentials: false',
                         'fail-fast: false', 'max-parallel: 3', 'if: always()',
                         'python3 scripts/chr_recovery.py preflight', 'python3 scripts/chr_recovery.py variant',
                         'python3 scripts/chr_recovery.py aggregate',
                         'name: chr-recovery-version-${{ matrix.version }}-${{ github.run_attempt }}',
                         'pattern: chr-recovery-version-*', 'merge-multiple: false',
                         'path: recovery-output/registry-content.json',
                         'pattern: chr-recovery-content-*',
                         '--shared-content shared-content/registry-content.json'):
            self.assertIn(required, text)
        for forbidden in ('overwrite: true', 'chr_matrix.py prepare', 'chr_matrix.py variant',
                          'chr_matrix.py aggregate', 'contents: write', 'actions: write'):
            self.assertNotIn(forbidden, text)
        aggregate = text.split('\n  aggregate:', 1)[1]
        self.assertIn("if: always() && needs.preflight.result == 'success' && needs.variant.result == 'success'", aggregate)
        for version in self.module().FAILED_VERSIONS: self.assertIn("'" + version + "'", text)
        # Only the producer writes shared content; consumers must degrade, not fail.
        self.assertEqual(text.count('--shared-content'), 2)
        self.assertEqual(text.count('continue-on-error: true'), 2)

    def test_evidence_steps_run_the_way_actions_runs_them(self):
        """`shell: python` executes a temp file, so the workspace is not on sys.path.

        This step is the job's last line of defence and runs with `if: always()`, so a
        failure here marks a successful recovery as failed and blocks aggregation.
        Run each step exactly as Actions does: a file outside the repo, cwd at the root.
        """
        import subprocess
        import sys
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/chr-matrix-recovery.yml').read_text())
        steps = [(job, step) for job, spec in workflow['jobs'].items()
                 for step in spec['steps'] if step.get('shell') == 'python']
        self.assertEqual(len(steps), 3, 'every python evidence step must be covered')
        for job, step in steps:
            with self.subTest(job=job), tempfile.TemporaryDirectory() as directory:
                outside = Path(directory) / 'step.py'
                outside.write_text(step['run'])
                workspace = Path(directory) / 'workspace'
                workspace.mkdir()
                # A report already exists, so the step must fall through without writing.
                evidence = workspace / 'recovery-output'
                evidence.mkdir()
                (evidence / 'report.json').write_text('{"status": "success"}')
                for name in ('scripts', 'config'):
                    (workspace / name).symlink_to(ROOT / name)
                env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
                env.update(CHR_VERSION='7.24.4', GITHUB_SHA='b' * 40, GITHUB_RUN_ID='999',
                           GITHUB_RUN_ATTEMPT='1', GITHUB_REF='refs/heads/main',
                           GITHUB_EVENT_NAME='workflow_dispatch',
                           GITHUB_REPOSITORY=matrix.REPOSITORY,
                           APPROVE_VERSION_OVERWRITE='true')
                result = subprocess.run([sys.executable, str(outside)], cwd=workspace,
                                        env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, f'{job}: {result.stderr}')
                self.assertEqual((evidence / 'report.json').read_text(), '{"status": "success"}',
                                 f'{job}: an existing report must never be overwritten')

    def test_missing_or_poisoned_shared_content_never_decides_correctness(self):
        recovery = self.module()
        fixture, registry, _, _ = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A missing artifact degrades to extra reads instead of failing the job.
            absent = recovery.import_shared_content(registry, root / 'nope.json')
            self.assertEqual(absent, {'imported': False, 'reason': 'unavailable', 'entries': 0})
            self.assertEqual(recovery.import_shared_content(registry, None)['reason'], 'not_requested')
            # An oversized artifact is refused outright rather than parsed.
            huge = root / 'huge.json'
            huge.write_bytes(b'{}' + b' ' * (32 * 1024 * 1024))
            with self.assertRaises(ValueError):
                recovery.import_shared_content(registry, huge)


if __name__ == '__main__':
    unittest.main()
