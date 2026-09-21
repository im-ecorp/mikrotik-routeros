"""Offline policy and execution tests: never use live registries."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("image_release", ROOT / "scripts/image_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class FakeRegistry:
    def __init__(self):
        self.refs = {(image, tag): release.OLD_DIGEST
                     for image in (release.HUB, release.GHCR)
                     for tag in (release.SEED, "v" + release.SEED)}
        self.refs.update({(image, "latest"): release.LATEST_DIGEST
                          for image in (release.HUB, release.GHCR)})
        self.refs.update({(release.HUB, tag): release.DESIRED_DIGEST
                          for tag in (release.DESIRED_DIGEST, release.LEGACY_TAG,
                                      "sha-" + release.SOURCE_SHA)})
        self.index = {"schemaVersion": 2, "manifests": [
            {"digest": "sha256:" + char * 64,
             "platform": {"os": "linux", "architecture": arch}}
            for char, arch in (("a", "amd64"), ("b", "arm64"))]}
        self.labels = {"org.opencontainers.image.version": "1.0.0",
                       "org.opencontainers.image.revision": release.SOURCE_SHA,
                       "org.opencontainers.image.source": "https://github.com/" + release.REPOSITORY,
                       "io.mikrotik-routeros.seed.version": release.SEED}

    def manifest(self, image, tag):
        return self.refs.get((image, tag)), self.index

    def config(self, image, digest):
        arch = "amd64" if digest == "sha256:" + "a" * 64 else "arm64"
        return {"os": "linux", "architecture": arch, "config": {"Labels": self.labels}}


class RecoveryTests(unittest.TestCase):
    def test_exact_ci_queries_latest_attempt_and_paginated_jobs(self):
        import io
        import json
        import os
        from unittest.mock import patch
        calls = []
        def urlopen(request, timeout):
            url = request.full_url
            calls.append(url)
            if '/workflows/' in url:
                self.assertIn('head_sha=' + release.SOURCE_SHA, url)
                self.assertIn('branch=main', url)
                self.assertIn('event=push', url)
                payload = {"workflow_runs": [{"id": 1, "run_attempt": 2,
                    "head_sha": release.SOURCE_SHA, "head_branch": "main", "event": "push",
                    "status": "completed", "conclusion": "success", "html_url": "https://github.com/run/1"}]}
            else:
                self.assertIn('/attempts/2/jobs', url)
                payload = {"jobs": [{"name": name, "status": "completed", "conclusion": "success"}
                                    for name in ("validate", "integration")]}
            next_page = 'page=2' not in url
            response = io.BytesIO(json.dumps({key: [] for key in payload} if next_page else payload).encode())
            response.headers = {'Link': f'<{url}&page=2>; rel="next"'} if next_page else {}
            return response
        with patch.dict(os.environ, GITHUB_REPOSITORY=release.REPOSITORY, GH_TOKEN='test-token'), \
                patch.object(release.urllib.request, "urlopen", urlopen):
            evidence = release.require_ci(release.SOURCE_SHA)
        self.assertEqual(len(evidence), 2)
        self.assertEqual(len(calls), 8)

    def test_redirect_never_forwards_credentials_to_another_host(self):
        request = release.urllib.request.Request('https://registry-1.docker.io/blob',
                                                headers={'Authorization': 'Bearer test-token'})
        handler = release.SafeRedirect()
        result = handler.redirect_request(request, None, 307, 'redirect', {}, 'https://cdn.example/blob')
        self.assertIsNone(result.get_header('Authorization'))
        with self.assertRaises(ValueError):
            handler.redirect_request(request, None, 307, 'redirect', {}, 'http://cdn.example/blob')

    def test_cli_recovery_writes_success_and_partial_failure_evidence(self):
        import json
        import os
        import tempfile
        from unittest.mock import patch
        for fail in (False, True):
            registry = FakeRegistry()
            class Transport:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def copy(self, image, tag):
                    if fail: raise RuntimeError('private transport details')
                    registry.refs[image, tag] = release.DESIRED_DIGEST
            env = {"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
                   "GITHUB_REPOSITORY": release.REPOSITORY, "APPROVE_VERSION_OVERWRITE": "true",
                   "GITHUB_SHA": "d" * 40, "GITHUB_STEP_SUMMARY": ""}
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env), \
                    patch.object(release, 'Registry', return_value=registry), \
                    patch.object(release, 'Skopeo', Transport), \
                    patch.object(release, 'require_ci', return_value=['proof']):
                original = Path.cwd()
                try:
                    os.chdir(directory)
                    if fail:
                        with self.assertRaises(RuntimeError): release.main(['recover'])
                    else:
                        release.main(['recover'])
                    report = json.loads(Path('recovery-manifest.json').read_text())
                    self.assertEqual(report['status'], 'failed' if fail else 'success')
                    self.assertEqual(report['executor_sha'], 'd' * 40)
                    self.assertEqual(report['source_sha'], release.SOURCE_SHA)
                    self.assertNotIn('private transport details', json.dumps(report))
                finally:
                    os.chdir(original)

    def test_publish_snapshot_and_readback_all_six_tags(self):
        preflight = getattr(release, "publish_preflight", None)
        self.assertIsNotNone(preflight)
        registry = FakeRegistry()
        sha = "c" * 40
        tags = [release.SEED, "v" + release.SEED, "sha-" + sha]
        with self.assertRaises(ValueError): preflight(registry, tags, False)
        before = preflight(registry, tags, True)
        for image in (release.HUB, release.GHCR):
            for tag in tags: registry.refs[image, tag] = release.DESIRED_DIGEST
        report = release.publish_readback(registry, tags, release.DESIRED_DIGEST, before)
        self.assertEqual(len(report["images"]), 6)
        with self.assertRaises(ValueError): preflight(registry, tags, True)
        registry.refs[release.HUB, "latest"] = release.OLD_DIGEST
        with self.assertRaises(ValueError):
            release.publish_readback(registry, tags, release.DESIRED_DIGEST, before)

    def test_recovery_cli_rejects_non_main_and_missing_approval(self):
        main = getattr(release, "main", None)
        self.assertIsNotNone(main)
        import os
        from unittest.mock import patch
        for ref, approval, event in [("refs/heads/feature", "true", "workflow_dispatch"),
                                     ("refs/heads/main", "false", "workflow_dispatch"),
                                     ("refs/heads/main", "true", "push")]:
            env = {"GITHUB_REF": ref, "GITHUB_EVENT_NAME": event,
                   "GITHUB_REPOSITORY": release.REPOSITORY,
                   "APPROVE_VERSION_OVERWRITE": approval}
            with patch.dict(os.environ, env), self.subTest(env=env), self.assertRaises(ValueError):
                main(["recover"])

    def test_registry_transport_checks_authenticated_manifest_and_config(self):
        import hashlib
        import json
        transport = getattr(release, "Registry", None)
        self.assertIsNotNone(transport, "authenticated registry read transport must exist")
        blob = json.dumps({"os": "linux", "architecture": "amd64", "config": {}}).encode()
        blob_digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        raw = json.dumps({"schemaVersion": 2, "config": {"digest": blob_digest}}).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        calls = []
        def fetch(url, headers):
            calls.append((url, headers))
            if '/token?' in url: return 200, b'{"token":"temporary"}', {}
            if '/blobs/' in url: return 200, blob, {}
            return 200, raw, {"Docker-Content-Digest": digest}
        registry = transport({"HUB_USER": "user", "HUB_TOKEN": "secret",
                              "GH_USER": "user", "GH_TOKEN": "secret"}, fetch=fetch)
        self.assertEqual(registry.manifest(release.HUB, release.SEED)[0], digest)
        self.assertEqual(registry.config(release.HUB, digest)["architecture"], "amd64")
        self.assertEqual(calls[-1][1]["Authorization"], "Bearer temporary")
        with self.assertRaises(ValueError): registry.manifest("unapproved/image", "latest")

    def test_skopeo_copy_uses_digest_and_secure_temporary_auth(self):
        import os
        from unittest.mock import patch
        transport = getattr(release, "Skopeo", None)
        self.assertIsNotNone(transport, "recovery must use skopeo preserve-digests")
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            self.assertNotIn("secret-value", " ".join(command))
            self.assertFalse(kwargs.get("shell", False))
            if command[1] == "login":
                self.assertEqual(kwargs["input"], b"secret-value")
                self.assertIn("--password-stdin", command)
                auth = Path(command[command.index("--authfile") + 1])
                self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
            return type("Result", (), {"stdout": b'{}'})()
        credentials = {"HUB_USER": "user", "HUB_TOKEN": "secret-value",
                       "GH_USER": "user", "GH_TOKEN": "secret-value"}
        with patch.dict(os.environ, credentials), patch.object(release.subprocess, "run", run):
            with transport() as skopeo:
                auth = skopeo.authfile
                skopeo.copy(release.GHCR, release.SEED)
                with self.assertRaises(ValueError): skopeo.copy(release.GHCR, "latest")
        self.assertFalse(Path(auth).exists())
        command = calls[-1][0]
        self.assertEqual(command[:4], ["skopeo", "copy", "--all", "--preserve-digests"])
        self.assertEqual(command[-2:], ["docker://" + release.HUB + "@" + release.DESIRED_DIGEST,
                                       "docker://" + release.GHCR + ":" + release.SEED])

    def test_recovery_executes_only_authorized_copies_and_readbacks(self):
        recover = getattr(release, "recover", None)
        self.assertIsNotNone(recover, "bounded recovery executor must exist")
        registry = FakeRegistry()
        ci = []
        copies = []
        def copy(image, tag):
            copies.append((image, tag))
            registry.refs[image, tag] = release.DESIRED_DIGEST
        report = recover(registry, copy, lambda sha: ci.append(sha) or ["ci-proof"])
        self.assertEqual(ci, [release.SOURCE_SHA])
        expected = {(image, tag) for image in (release.HUB, release.GHCR)
                    for tag in (release.SEED, "v" + release.SEED)}
        expected.add((release.GHCR, "sha-" + release.SOURCE_SHA))
        self.assertEqual(set(copies), expected)
        self.assertEqual(len(copies), 5)
        self.assertEqual(len(report["images"]), 5)
        self.assertEqual(report["status"], "success")
        copies.clear()
        recover(registry, copy, lambda sha: [])
        self.assertEqual(copies, [], "retry should only read already-correct references")

    def test_recovery_aborts_before_writes_on_preflight_failure(self):
        recover = getattr(release, "recover", None)
        self.assertIsNotNone(recover)
        for bad in ("alias", "latest", "labels", "platform", "source", "sha", "ci"):
            registry = FakeRegistry()
            if bad == "alias": registry.refs[release.HUB, release.SEED] = None
            if bad == "latest": registry.refs[release.GHCR, "latest"] = release.OLD_DIGEST
            if bad == "source": registry.refs[release.HUB, release.DESIRED_DIGEST] = release.OLD_DIGEST
            if bad == "sha": registry.refs[release.GHCR, "sha-" + release.SOURCE_SHA] = release.OLD_DIGEST
            if bad == "labels": registry.labels["org.opencontainers.image.revision"] = "b" * 40
            if bad == "platform": registry.index["manifests"].pop()
            def ci(sha):
                if bad == "ci": raise ValueError("CI failed")
                return []
            copies = []
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                recover(registry, lambda *args: copies.append(args), ci)
            self.assertEqual(copies, [])

    def test_recovery_rejects_failed_readback_and_latest_drift(self):
        recover = getattr(release, "recover", None)
        self.assertIsNotNone(recover)
        for drift in (False, True):
            registry = FakeRegistry()
            def copy(image, tag):
                if drift:
                    registry.refs[image, tag] = release.DESIRED_DIGEST
                    registry.refs[release.HUB, "latest"] = release.OLD_DIGEST
            with self.subTest(drift=drift), self.assertRaises(ValueError):
                recover(registry, copy, lambda sha: [])

    def test_registry_readback_rejects_uncertainty_and_checks_bytes(self):
        import hashlib
        import json
        decode = getattr(release, "decode_manifest", None)
        self.assertIsNotNone(decode, "registry reads must validate digest and JSON")
        raw = json.dumps({"schemaVersion": 2, "manifests": []}).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.assertEqual(decode(200, raw, digest), (digest, json.loads(raw)))
        self.assertEqual(decode(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', None), (None, None))
        for status, body, header in [(401, raw, digest), (403, raw, digest),
                (429, raw, digest), (500, raw, digest), (404, b'not found', None),
                (404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}', None),
                (200, raw, None), (200, raw, "sha256:" + "b" * 64),
                (200, b'{}', digest)]:
            with self.subTest(status=status, header=header), self.assertRaises(ValueError):
                decode(status, body, header)

    def test_recovery_target_policy_is_bounded_and_idempotent(self):
        policy = getattr(release, "require_recovery_target", None)
        self.assertIsNotNone(policy, "recovery must check exact known target digests")
        for value in (release.OLD_DIGEST, release.DESIRED_DIGEST):
            policy(value, immutable=False)
        for value in (None, release.DESIRED_DIGEST):
            policy(value, immutable=True)
        for value, immutable in [(None, False), (release.OLD_DIGEST, True),
                                 ("sha256:" + "a" * 64, False), ("", True)]:
            with self.subTest(value=value, immutable=immutable), self.assertRaises(ValueError):
                policy(value, immutable=immutable)
