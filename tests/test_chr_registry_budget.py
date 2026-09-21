"""Offline HTTP budget: real recovery policy over digest-valid synthetic bytes."""
import copy
from collections import Counter
import hashlib
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from scripts import chr_matrix as matrix
from scripts import chr_recovery as recovery

CREDENTIALS = {'HUB_USER': 'fixture', 'HUB_TOKEN': 'fixture',
               'GH_USER': 'fixture', 'GH_TOKEN': 'fixture'}


def recovery_registry():
    # Baseline measurement is runnable before the optimization exists.
    return getattr(recovery, 'RecoveryRegistry', matrix.MatrixRegistry)


class Transport:
    def __init__(self):
        self.calls = []
        self.tags = {}
        self.content = {}
        self.overrides = {}

    def put(self, image, value, kind='manifests'):
        raw = json.dumps(value, sort_keys=True).encode()
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        self.content[image, kind, digest] = raw
        return digest

    def __call__(self, url, headers, *, method='GET'):
        parsed = urlsplit(url)
        image = matrix.HUB if parsed.hostname in ('registry-1.docker.io', 'auth.docker.io') else matrix.GHCR
        if parsed.path == '/token':
            self.calls.append((image, method, 'token', 'token'))
            return 200, b'{"token":"fixture"}', {}
        prefix = '/v2/' + image.removeprefix('ghcr.io/') + '/'
        if not parsed.path.startswith(prefix):
            raise AssertionError('unexpected fixture URL')
        kind, ref = parsed.path[len(prefix):].split('/', 1)
        self.calls.append((image, method, kind, ref))
        if (image, method, kind, ref) in self.overrides:
            return self.overrides[image, method, kind, ref]
        digest = ref if ref.startswith('sha256:') else self.tags.get((image, ref))
        raw = self.content.get((image, kind, digest))
        if raw is None:
            return 404, b'' if method == 'HEAD' else b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', {}
        return 200, b'' if method == 'HEAD' else raw, {'Docker-Content-Digest': digest}

    def counts(self):
        return dict(sorted(Counter(f'{"hub" if image == matrix.HUB else "ghcr"}:{method}:{kind}'
                                   for image, method, kind, _ in self.calls).items()))


def fixture():
    wire = Transport()
    data = matrix.load_manifest(Path(__file__).resolve().parents[1] / 'config/chr-versions.json')
    sha = recovery.ORIGINAL_SOURCE_SHA
    snapshot = {'source_sha': sha, 'run_id': recovery.ORIGINAL_RUN_ID, 'manifest': data,
                'approve_version_overwrite': True, 'before': {}, 'preserved': {}}
    reports = []
    digests = {}
    for image in (matrix.HUB, matrix.GHCR):
        old = wire.put(image, {'schemaVersion': 2, 'fixture': 'historical'})
        latest = wire.put(image, {'schemaVersion': 2, 'fixture': 'old-latest'})
        wire.tags[image, 'latest'] = latest
        snapshot['before'][f'{image}:latest'] = latest
        for tag in matrix.PRESERVED_TAGS:
            wire.tags[image, tag] = old
            snapshot['preserved'][f'{image}:{tag}'] = old
        for row in data['versions']:
            entries = []
            for arch in ('amd64', 'arm64'):
                config = wire.put(image, {'os': 'linux', 'architecture': arch,
                    'config': {'Labels': matrix.expected_labels(row, sha)}}, 'blobs')
                child = wire.put(image, {'schemaVersion': 2, 'config': {'digest': config}})
                entries.append({'digest': child, 'platform': {'os': 'linux', 'architecture': arch}})
            digest = wire.put(image, {'schemaVersion': 2, 'manifests': entries})
            digests[row['version']] = digest
            source = matrix.source_tag(sha, row['version'])
            original = row['version'] not in recovery.FAILED_VERSIONS
            for tag in (source, row['version'], 'v' + row['version']):
                snapshot['before'][f'{image}:{tag}'] = None
                if original or (image == matrix.HUB and tag == source):
                    wire.tags[image, tag] = digest
            if original and image == matrix.HUB:
                runtime = {'status': 'passed', 'image': matrix.HUB + '@' + digest,
                    'cleanup_errors': [], 'harness_sha256': recovery.HARNESS_SHA256,
                    'image_labels': matrix.expected_labels(row, sha),
                    'checks': [{'fresh_guest_matches_seed': True, 'seed_version': row['version']}],
                    'shutdowns': [{'label': label} for label in ('restart', 'recreate', 'final')]}
                reports.append({'status': 'success', 'version': row['version'], 'source_sha': sha,
                    'run_id': recovery.ORIGINAL_RUN_ID, 'digest': digest, 'platforms': matrix.PLATFORMS,
                    'runtime_tested': ['linux/amd64'], 'build_only': ['linux/arm64'], 'runtime': runtime})
    return wire, data, snapshot, reports, digests


def measure(cls):
    wire, data, snapshot, originals, digests = fixture()
    registry = cls(CREDENTIALS, fetch=wire)
    report = {}
    recovery.preflight(registry, data, snapshot, originals, recovery.HARNESS_SHA256, report)
    preflight = wire.counts()
    wire.calls.clear()
    row = data['versions'][0]
    for _ in range(10):
        matrix.verify_image(registry, matrix.HUB, row['version'], digests[row['version']], row,
                            recovery.ORIGINAL_SOURCE_SHA)
    return {'preflight': preflight, 'ten_warm_verify_image': wire.counts(),
            'original_readbacks': len(report['original_readbacks']),
            'reused_versions': len(report['reused_versions'])}


class BudgetTests(unittest.TestCase):
    def test_preflight_retains_readbacks_but_warm_verification_uses_no_get(self):
        measured = measure(recovery_registry())
        self.assertEqual(measured['original_readbacks'], 60)
        self.assertEqual(measured['reused_versions'], 10)
        self.assertEqual(measured['ten_warm_verify_image'], {'hub:HEAD:manifests': 10})
        self.assertEqual(measured['preflight'], {
            'hub:GET:token': 1, 'hub:GET:blobs': 34, 'hub:GET:manifests': 67,
            'hub:HEAD:manifests': 83, 'ghcr:GET:token': 1, 'ghcr:GET:blobs': 20,
            'ghcr:GET:manifests': 53, 'ghcr:HEAD:manifests': 76})


class CacheSafetyTests(unittest.TestCase):
    def setUp(self):
        self.wire, self.data, self.snapshot, self.originals, self.digests = fixture()
        self.registry = recovery_registry()(CREDENTIALS, fetch=self.wire)
        self.image = matrix.HUB
        self.tag = self.data['versions'][0]['version']
        self.digest = self.digests[self.tag]

    def test_fresh_head_detects_drift_even_to_cached_content(self):
        for row in self.data['versions'][:2]:
            self.registry.manifest(self.image, row['version'])
        self.wire.tags[self.image, self.tag] = self.digests[self.data['versions'][1]['version']]
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            matrix.verify_image(self.registry, self.image, self.tag, self.digest,
                                self.data['versions'][0], recovery.ORIGINAL_SOURCE_SHA)

    def test_head_get_race_or_corrupt_bytes_never_enters_cache(self):
        key = self.image, 'GET', 'manifests', self.tag
        raw = self.wire.content[self.image, 'manifests', self.digest]
        for response in ((200, raw + b' ', {'Docker-Content-Digest': self.digest}),
                         (200, raw, {'Docker-Content-Digest': 'sha256:' + 'f' * 64}),
                         (200, b'{"schemaVersion":2}', {'Docker-Content-Digest':
                             'sha256:' + hashlib.sha256(b'{"schemaVersion":2}').hexdigest()}),
                         (404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', {})):
            self.wire.overrides[key] = response
            with self.subTest(response=response), self.assertRaises(matrix.RegistryError):
                self.registry.manifest(self.image, self.tag)
            self.assertFalse(self.registry.manifests)
        del self.wire.overrides[key]
        self.assertEqual(self.registry.manifest(self.image, self.tag)[0], self.digest)

    def test_missing_header_is_not_content_and_cannot_seed_cache(self):
        key = self.image, 'HEAD', 'manifests', self.tag
        for headers in ({}, {'Docker-Content-Digest': 'SECRET'}, {'Docker-Content-Digest': None}):
            self.wire.overrides[key] = (200, b'', headers)
            with self.assertRaises(matrix.RegistryError):
                self.registry.manifest(self.image, self.tag)
        self.assertFalse(self.registry.manifests)
        self.assertFalse(any(method == 'GET' and kind == 'manifests' for _, method, kind, _ in self.wire.calls))

    def test_cache_is_repository_scoped_and_returned_content_is_detached(self):
        digest, manifest = self.registry.manifest(self.image, self.tag)
        manifest['manifests'].clear()
        self.assertEqual(len(self.registry.manifest(self.image, digest)[1]['manifests']), 2)
        self.registry.manifest(matrix.GHCR, digest)
        self.assertEqual(sum(kind == 'manifests' and method == 'GET'
                             for _, method, kind, _ in self.wire.calls), 2)

    def test_no_cached_absence_or_http_errors(self):
        for _ in range(2):
            self.assertEqual(self.registry.manifest(self.image, 'missing'), (None, None))
        self.assertEqual(sum(kind == 'manifests' for _, _, kind, _ in self.wire.calls), 4)
        key = self.image, 'HEAD', 'manifests', self.tag
        for status in (403, 429, 500):
            self.wire.overrides[key] = (status, b'SECRET', {})
            with self.assertRaises(matrix.RegistryError):
                self.registry.manifest(self.image, self.tag)
        del self.wire.overrides[key]
        self.assertEqual(self.registry.manifest(self.image, self.tag)[0], self.digest)

    def test_head_absence_get_presence_race_fails_closed(self):
        self.wire.overrides[self.image, 'HEAD', 'manifests', self.tag] = (404, b'', {})
        with self.assertRaises(matrix.RegistryError):
            self.registry.manifest(self.image, self.tag)
        self.assertFalse(self.registry.manifests)

    def test_digest_reference_cannot_accept_other_verified_content(self):
        raw = b'{"schemaVersion":2}'
        other = 'sha256:' + hashlib.sha256(raw).hexdigest()
        self.wire.overrides[self.image, 'GET', 'manifests', self.digest] = (200, raw, {'Docker-Content-Digest': other})
        with self.assertRaises(matrix.RegistryError):
            self.registry.manifest(self.image, self.digest)
        self.assertFalse(self.registry.manifests)

    def test_manifest_schema_and_config_byte_hash_remain_required(self):
        raw = b'{"schemaVersion":1}'
        bad = 'sha256:' + hashlib.sha256(raw).hexdigest()
        self.wire.overrides[self.image, 'GET', 'manifests', bad] = (200, raw, {'Docker-Content-Digest': bad})
        with self.assertRaises(matrix.RegistryError):
            self.registry.manifest(self.image, bad)
        index = json.loads(self.wire.content[self.image, 'manifests', self.digest])
        child = index['manifests'][0]['digest']
        config = json.loads(self.wire.content[self.image, 'manifests', child])['config']['digest']
        self.wire.overrides[self.image, 'GET', 'blobs', config] = (200, b'SECRET', {})
        with self.assertRaises(matrix.RegistryError):
            self.registry.config(self.image, child)
        self.assertFalse(self.registry.configs)
        del self.wire.overrides[self.image, 'GET', 'blobs', config]
        self.assertEqual(self.registry.config(self.image, child)['architecture'], 'amd64')

    def test_allowlists_apply_before_transport(self):
        for image, path, method in [('foreign/repo', 'manifests/latest', 'HEAD'),
                (self.image, 'manifests/latest', 'POST'), (self.image, 'manifests/../../token', 'GET'),
                (self.image, 'blobs/sha256:' + 'a' * 64, 'HEAD')]:
            with self.assertRaises(matrix.RegistryError):
                self.registry.request(image, path, method=method)
        self.assertFalse(self.wire.calls)

    def test_401_refresh_preserves_head_and_uses_get_token(self):
        original = self.wire.__call__
        def fetch(url, headers, *, method='GET'):
            result = original(url, headers, method=method)
            if method == 'HEAD' and len([c for c in self.wire.calls if c[2] == 'manifests']) == 1:
                return 401, b'', {}
            return result
        registry = recovery_registry()(CREDENTIALS, fetch=fetch)
        self.assertEqual(registry.manifest(self.image, self.tag)[0], self.digest)
        self.assertEqual([c[1:3] for c in self.wire.calls],
                         [('GET', 'token'), ('HEAD', 'manifests'), ('GET', 'token'),
                          ('HEAD', 'manifests'), ('GET', 'manifests')])


class TransportTests(unittest.TestCase):
    def test_redirect_preserves_method_strips_cross_host_auth_and_rejects_http(self):
        from scripts.chr_registry import ReadRedirect
        from urllib.request import Request
        for method in ('GET', 'HEAD'):
            request = Request('https://registry-1.docker.io/v2/path',
                              headers={'Authorization': 'Bearer SECRET'}, method=method)
            redirect = ReadRedirect()
            same = redirect.redirect_request(request, None, 307, '', {}, 'https://registry-1.docker.io/other')
            self.assertEqual(same.get_method(), method)
            self.assertEqual(same.get_header('Authorization'), 'Bearer SECRET')
            other = redirect.redirect_request(request, None, 302, '', {}, 'https://cdn.example.invalid/blob')
            self.assertEqual(other.get_method(), method)
            self.assertIsNone(other.get_header('Authorization'))
            with self.assertRaises(ValueError):
                redirect.redirect_request(request, None, 302, '', {}, 'http://cdn.example.invalid/blob')
        with self.assertRaises(ValueError):
            ReadRedirect().redirect_request(Request('https://example.invalid', method='POST'),
                                            None, 302, '', {}, 'https://example.invalid/other')

    def test_fetch_only_get_head_and_bounded_error_read(self):
        import io
        import urllib.error
        from unittest.mock import patch
        from scripts.chr_registry import fetch
        with patch('urllib.request.build_opener') as opener:
            with self.assertRaises(ValueError):
                fetch('https://example.invalid', {}, method='POST')
            opener.assert_not_called()
            opener.return_value.open.side_effect = urllib.error.HTTPError(
                'https://example.invalid', 429, 'SECRET', {}, io.BytesIO(b'x' * 5000))
            status, raw, _ = fetch('https://example.invalid', {}, method='HEAD')
            self.assertEqual(status, 429)
            self.assertEqual(len(raw), 4097)
            self.assertEqual(opener.return_value.open.call_args.args[0].get_method(), 'HEAD')


class DiagnosticTests(unittest.TestCase):
    def test_quota_parser_allowlists_and_date_retry_after(self):
        from scripts.chr_registry import quota_diagnostic
        parsed = quota_diagnostic(b'Too Many Requests', {'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT'})
        self.assertEqual(parsed, {'classification': 'abuse', 'retry_after_epoch': 1445412480})
        invalid = {'RateLimit-Limit': 'SECRET', 'ratelimit-remaining': '-1;w=21600',
                   'Retry-After': '999999999999999999999999', 'Authorization': 'SECRET'}
        self.assertEqual(quota_diagnostic(b'Too Many Requests SECRET', invalid), {'classification': 'unknown'})
        self.assertEqual(quota_diagnostic(b'Too Many Requests' + b' ' * 5000, {}), {'classification': 'unknown'})
        raw = json.dumps({'errors': [{'code': 'TOOMANYREQUESTS',
            'message': 'You have reached your unauthenticated pull rate limit. https://www.docker.com/increase-rate-limit',
            'detail': 'SECRET'}]}).encode()
        self.assertEqual(quota_diagnostic(raw, {}), {'classification': 'pull_quota'})

    def test_429_diagnostic_is_bounded_and_not_retried(self):
        wire, _, _, _, _ = fixture()
        body = b'You have reached your pull rate limit. You may increase the limit by authenticating and upgrading: https://www.docker.com/increase-rate-limits'
        wire.overrides[matrix.HUB, 'HEAD', 'manifests', 'latest'] = (429, body, {
            'ratelimit-limit': '200;w=21600', 'RateLimit-Remaining': '0;w=21600',
            'Retry-After': '21600', 'docker-ratelimit-source': 'SECRET-IP', 'Other': 'SECRET'})
        registry = recovery_registry()(CREDENTIALS, fetch=wire)
        with self.assertRaises(matrix.RegistryError) as caught:
            registry.manifest(matrix.HUB, 'latest')
        diagnostic = matrix.failure_details(caught.exception, 'preservation_check')
        self.assertEqual(diagnostic.get('quota'), {'classification': 'pull_quota',
            'limit': 200, 'limit_window_seconds': 21600, 'remaining': 0,
            'remaining_window_seconds': 21600, 'retry_after_seconds': 21600})
        self.assertEqual(diagnostic['status'], 429)
        self.assertEqual(diagnostic['attempt'], 1)
        self.assertNotIn('SECRET', json.dumps(diagnostic))
        self.assertEqual(wire.counts(), {'hub:GET:token': 1, 'hub:HEAD:manifests': 1})


if __name__ == '__main__':
    unittest.main()
