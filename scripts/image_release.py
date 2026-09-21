#!/usr/bin/env python3
"""Fail-closed registry and publication policy; no credentials in reports."""


import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import base64
import re
import urllib.error
import urllib.parse
import urllib.request


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise ValueError('Refusing non-HTTPS redirect')
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(request.full_url).netloc:
            redirected.remove_header('Authorization')
        return redirected


def fetch(url, headers):
    try:
        opener = urllib.request.build_opener(SafeRedirect())
        with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


class Registry:
    def __init__(self, credentials=None, *, fetch=fetch):
        self.credentials = credentials if credentials is not None else os.environ
        self.fetch = fetch
        self.tokens = {}

    def request(self, image, path):
        if image == HUB:
            host, endpoint, service, repository, prefix = (
                'registry-1.docker.io', 'https://auth.docker.io/token', 'registry.docker.io', HUB, 'HUB')
        elif image == GHCR:
            host, endpoint, service, repository, prefix = (
                'ghcr.io', 'https://ghcr.io/token', 'ghcr.io', GHCR.removeprefix('ghcr.io/'), 'GH')
        else:
            raise ValueError('Unapproved registry repository')
        if image not in self.tokens:
            user, secret = self.credentials[prefix + '_USER'], self.credentials[prefix + '_TOKEN']
            if not user or not secret:
                raise ValueError('Registry credentials are missing')
            basic = base64.b64encode(f'{user}:{secret}'.encode()).decode()
            query = urllib.parse.urlencode({'service': service, 'scope': f'repository:{repository}:pull'})
            status, body, _ = self.fetch(endpoint + '?' + query, {'Authorization': 'Basic ' + basic})
            if status != 200:
                raise ValueError(f'Registry authentication failed (HTTP {status})')
            data = json.loads(body)
            token = data.get('token') or data.get('access_token')
            if not isinstance(token, str) or not token:
                raise ValueError('Registry token missing')
            self.tokens[image] = token
        return self.fetch(f'https://{host}/v2/{repository}/{path}', {
            'Authorization': 'Bearer ' + self.tokens[image],
            'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'})

    def manifest(self, image, tag):
        if not re.fullmatch(r'(?:sha256:[0-9a-f]{64}|[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})', tag):
            raise ValueError('Invalid manifest reference')
        status, raw, headers = self.request(image, 'manifests/' + tag)
        return decode_manifest(status, raw, headers.get('Docker-Content-Digest'))

    def config(self, image, digest):
        actual, manifest = self.manifest(image, digest)
        if actual != digest:
            raise ValueError('Child manifest digest mismatch')
        config_digest = manifest['config']['digest']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', config_digest):
            raise ValueError('Invalid config digest')
        status, raw, _ = self.request(image, 'blobs/' + config_digest)
        if status != 200 or 'sha256:' + hashlib.sha256(raw).hexdigest() != config_digest:
            raise ValueError('Config blob digest mismatch')
        return json.loads(raw)


class Skopeo:
    """Credentials exist only in a private temporary directory and stdin."""
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='chr-registry-auth-')
        self.authfile = str(Path(self.directory.name) / 'auth.json')
        fd = os.open(self.authfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write('{}')
        try:
            for host, prefix in (('docker.io', 'HUB'), ('ghcr.io', 'GH')):
                user, secret = os.environ[prefix + '_USER'], os.environ[prefix + '_TOKEN']
                if not user or not secret:
                    raise ValueError('Registry credentials are missing')
                subprocess.run(['skopeo', 'login', '--authfile', self.authfile,
                                '--username', user, '--password-stdin', host],
                               input=secret.encode(), capture_output=True, check=True, timeout=60)
        except BaseException:
            self.directory.cleanup()
            raise
        return self

    def __exit__(self, *args):
        self.directory.cleanup()

    def copy(self, image, tag):
        allowed = {(registry, alias) for registry in (HUB, GHCR) for alias in (SEED, 'v' + SEED)}
        allowed.add((GHCR, 'sha-' + SOURCE_SHA))
        if (image, tag) not in allowed:
            raise ValueError('Destination is outside the approved recovery scope')
        subprocess.run(['skopeo', 'copy', '--all', '--preserve-digests',
                        '--authfile', self.authfile,
                        f'docker://{HUB}@{DESIRED_DIGEST}', f'docker://{image}:{tag}'],
                       capture_output=True, check=True, timeout=1800)


def manifest_absent(status, body):
    try:
        errors = json.loads(body)['errors']
        return status == 404 and bool(errors) and all(e['code'] == 'MANIFEST_UNKNOWN' for e in errors)
    except (ValueError, KeyError, TypeError):
        return False


def decode_manifest(status, raw, digest):
    if manifest_absent(status, raw):
        return None, None
    actual = 'sha256:' + hashlib.sha256(raw).hexdigest()
    if status != 200 or digest != actual:
        raise ValueError(f'Uncertain registry response or digest mismatch (HTTP {status})')
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get('schemaVersion') != 2:
        raise ValueError('Invalid image manifest')
    return actual, manifest


REPOSITORY = 'im-ecorp/mikrotik-routeros'
SOURCE_SHA = 'f6108c7672800618fd78e407ef79fcd15f69eef8'
SEED = '7.21.4'
DESIRED_DIGEST = 'sha256:9dc63b8d19af9bb3e343fd946c487189cdf912fc822f3f829455b616b91d8425'
OLD_DIGEST = 'sha256:026123ea23088ceb1338b6d7d0d62ad06fd160c9394996e00c5a3e31c20e7449'
LATEST_DIGEST = 'sha256:be880f3daf8926d6f51f31694bf7e1085ebd1113d1da8c6778afe1bd51b49569'
HUB = 'hossein3piol/mikrotik-routeros'
GHCR = 'ghcr.io/im-ecorp/mikrotik-routeros'
LEGACY_TAG = '7.21.4-r1.0.0'  # Read-only preservation check, never a destination.


def require_recovery_target(digest, *, immutable):
    allowed = (None, DESIRED_DIGEST) if immutable else (OLD_DIGEST, DESIRED_DIGEST)
    if digest not in allowed:
        raise ValueError('Recovery target differs from the authorized old or desired digest')


def platforms(manifest):
    entries = manifest.get('manifests', [])
    actual = sorted(f"{m['platform']['os']}/{m['platform']['architecture']}"
                    for m in entries if m['platform']['os'] != 'unknown')
    if actual != ['linux/amd64', 'linux/arm64']:
        raise ValueError('Image must contain exactly linux/amd64 and linux/arm64')
    return actual


def require_image(registry, image, tag, expected, *, labels=False):
    digest, manifest = registry.manifest(image, tag)
    if digest != expected:
        raise ValueError(f'Read-back digest mismatch: {image}:{tag}')
    actual = platforms(manifest)
    if labels:
        expected_labels = {'org.opencontainers.image.version': '1.0.0',
                           'org.opencontainers.image.revision': SOURCE_SHA,
                           'org.opencontainers.image.source': 'https://github.com/' + REPOSITORY,
                           'io.mikrotik-routeros.seed.version': SEED}
        for entry in manifest['manifests']:
            if entry['platform']['os'] == 'unknown':
                continue  # BuildKit attestations are preserved by --all.
            config = registry.config(image, entry['digest'])
            if any(config.get(key) != entry['platform'][key] for key in ('os', 'architecture')):
                raise ValueError('Platform config contradicts index')
            found = config.get('config', {}).get('Labels', {})
            if any(found.get(key) != value for key, value in expected_labels.items()):
                raise ValueError('Source image labels do not match the authorized tested source')
    return {'image': f'{image}:{tag}', 'digest': digest, 'platforms': actual}


def recover(registry, copy, ci_gate, report=None):
    """Only the five approved destinations; already-correct tags are read-only."""
    report = report if report is not None else {}
    report.update(status='preflight', source_sha=SOURCE_SHA, seed_version=SEED,
                  source=f'{HUB}@{DESIRED_DIGEST}', images=[], before={}, after={},
                  runtime_tested=['linux/amd64'], build_only=['linux/arm64'])
    report['ci'] = ci_gate(SOURCE_SHA)
    require_image(registry, HUB, DESIRED_DIGEST, DESIRED_DIGEST, labels=True)
    preserved = [(HUB, 'latest', LATEST_DIGEST), (GHCR, 'latest', LATEST_DIGEST),
                 (HUB, LEGACY_TAG, DESIRED_DIGEST), (HUB, 'sha-' + SOURCE_SHA, DESIRED_DIGEST)]
    for image, tag, expected in preserved:
        digest, _ = registry.manifest(image, tag)
        if digest != expected:
            raise ValueError(f'Preserved reference changed: {image}:{tag}')
        report['before'][f'{image}:{tag}'] = digest
    targets = [(image, tag, False) for image in (HUB, GHCR) for tag in (SEED, 'v' + SEED)]
    targets.append((GHCR, 'sha-' + SOURCE_SHA, True))
    for image, tag, immutable in targets:
        digest, _ = registry.manifest(image, tag)
        require_recovery_target(digest, immutable=immutable)
        report['before'][f'{image}:{tag}'] = digest
    report['status'] = 'copying'
    for image, tag, immutable in targets:
        # Recheck immediately before each write; registry tags are not CAS.
        digest, _ = registry.manifest(image, tag)
        require_recovery_target(digest, immutable=immutable)
        if digest != DESIRED_DIGEST:
            copy(image, tag)
        report['images'].append(require_image(registry, image, tag, DESIRED_DIGEST))
    # Full final readback detects drift after an earlier individual copy.
    for image, tag, _ in targets:
        record = require_image(registry, image, tag, DESIRED_DIGEST)
        report['after'][record['image']] = record['digest']
    for image, tag, expected in preserved:
        digest, _ = registry.manifest(image, tag)
        report['after'][f'{image}:{tag}'] = digest
        if digest != expected:
            raise ValueError(f'Preserved reference changed: {image}:{tag}')
    report['status'] = 'success'
    return report


def require_publish_target(digest, *, immutable, approve):
    if digest is not None and (immutable or approve is not True):
        raise ValueError('Existing tag requires alias overwrite approval; SHA tags are immutable')


def validate_inputs(release, seed, ref, sha, tested_seed):
    semver = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    if not re.fullmatch(semver, release) or len(release) > 32:
        raise ValueError('release_version must be canonical X.Y.Z without v')
    if not re.fullmatch(semver, seed) or seed != tested_seed:
        raise ValueError('routeros_version must match the runtime-tested Dockerfile seed')
    if ref != 'refs/heads/main' or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('release requires an exact main commit SHA')
    return [seed, f'v{seed}', f'sha-{sha}']


def require_run(runs, sha):
    matching = [r for r in runs if r['head_sha'] == sha and
                r['head_branch'] == 'main' and r['event'] == 'push']
    if not matching:
        raise ValueError('No main push CI run for the exact release SHA')
    latest = max(matching, key=lambda r: (r['id'], r.get('run_attempt', 1)))
    if latest['status'] != 'completed' or latest['conclusion'] != 'success':
        raise ValueError('Latest exact-SHA main push CI run must finish successfully')
    return latest

def require_job(jobs, name):
    matching = [j for j in jobs if j['name'] == name]
    if len(matching) != 1 or matching[0]['status'] != 'completed' or matching[0]['conclusion'] != 'success':
        raise ValueError(f'Required CI job {name} must complete successfully, not skip')



def require_ci(sha):
    repository = os.environ['GITHUB_REPOSITORY']
    records = []
    headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
               'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28'}
    for workflow in ('validate.yml', 'runtime-integration.yml'):
        query = urllib.parse.urlencode({'head_sha': sha, 'branch': 'main',
                                         'event': 'push', 'per_page': 100})
        url = f'https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?{query}'
        runs = []
        while url:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                runs.extend(json.load(response)['workflow_runs'])
                links = response.headers.get('Link', '')
            url = next((part.split('>')[0].strip('< ') for part in links.split(',')
                        if 'rel="next"' in part), None)
        run = require_run(runs, sha)
        url = f"https://api.github.com/repos/{repository}/actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs?per_page=100"
        jobs = []
        while url:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                jobs.extend(json.load(response)['jobs'])
                links = response.headers.get('Link', '')
            url = next((part.split('>')[0].strip('< ') for part in links.split(',')
                        if 'rel="next"' in part), None)
        require_job(jobs, 'validate' if workflow == 'validate.yml' else 'integration')
        records.append({'workflow': workflow, 'run_id': run['id'], 'run_attempt': run.get('run_attempt', 1), 'url': run['html_url']})
    return records


def publish_preflight(registry, tags, approve):
    before = {}
    for image in (HUB, GHCR):
        digest, _ = registry.manifest(image, 'latest')
        if digest is None:
            raise ValueError('Cannot establish latest preservation baseline')
        before[f'{image}:latest'] = digest
        for tag in tags:
            digest, _ = registry.manifest(image, tag)
            require_publish_target(digest, immutable=tag.startswith('sha-'), approve=approve)
            before[f'{image}:{tag}'] = digest
    return before


def publish_readback(registry, tags, expected, before):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', expected):
        raise ValueError('Build output digest is missing or invalid')
    records = [require_image(registry, image, tag, expected)
               for image in (HUB, GHCR) for tag in tags]
    latest = {}
    for image in (HUB, GHCR):
        digest, _ = registry.manifest(image, 'latest')
        latest[f'{image}:latest'] = digest
        if digest is None or digest != before[f'{image}:latest']:
            raise ValueError('latest changed during publication')
    return {'status': 'success', 'images': records, 'before': before, 'latest_after': latest}


def write_report(path, report):
    text = json.dumps(report, indent=2) + '\n'
    Path(path).write_text(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('## CHR image manifest\n```json\n' + text + '```\n')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('recover', 'preflight', 'readback'))
    args = parser.parse_args(argv)
    if (os.environ.get('GITHUB_REF') != 'refs/heads/main' or
            os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or
            os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'):
        raise ValueError('Only manual dispatch on the authorized repository main is allowed')
    approval = os.environ.get('APPROVE_VERSION_OVERWRITE', 'false')
    if approval not in ('true', 'false'):
        raise ValueError('Approval must be an explicit boolean')
    approve = approval == 'true'
    registry = Registry()
    run_url = (f"https://github.com/{REPOSITORY}/actions/runs/" + os.environ.get('GITHUB_RUN_ID', 'unknown'))
    if args.command == 'recover':
        if not approve:
            raise ValueError('Recovery requires explicit version alias overwrite approval')
        report = {'run_url': run_url, 'executor_sha': os.environ.get('GITHUB_SHA')}
        try:
            with Skopeo() as skopeo:
                recover(registry, skopeo.copy, require_ci, report)
        except Exception as error:
            # Do not print HTTP bodies, tokens, subprocess output, or credential-bearing URLs.
            report.update(status='failed', error_type=type(error).__name__)
            raise
        finally:
            write_report('recovery-manifest.json', report)
        return
    tested_seed = re.search(r'^ARG ROUTEROS_VERSION=(\S+)$', Path('Dockerfile').read_text(), re.M).group(1)
    tags = validate_inputs(os.environ['RELEASE_VERSION'], os.environ['ROUTEROS_VERSION'],
                           os.environ['SOURCE_REF'], os.environ['SOURCE_SHA'], tested_seed)
    if os.environ['SOURCE_SHA'] != os.environ['GITHUB_SHA']:
        raise ValueError('Publisher source must equal the dispatch SHA')
    snapshot = Path('release-preflight.json')
    if args.command == 'preflight':
        before = publish_preflight(registry, tags, approve)
        snapshot.write_text(json.dumps(before, indent=2) + '\n')
    else:
        report = publish_readback(registry, tags, os.environ['EXPECTED_DIGEST'], json.loads(snapshot.read_text()))
        report.update(wrapper_version=os.environ['RELEASE_VERSION'], seed_version=tested_seed,
                      source_sha=os.environ['SOURCE_SHA'], source_url='https://github.com/' + REPOSITORY,
                      run_url=run_url, runtime_tested=['linux/amd64'], build_only=['linux/arm64'])
        write_report('release-manifest.json', report)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit(f'Image operation failed closed ({type(error).__name__}); review manifest and registry state') from None
