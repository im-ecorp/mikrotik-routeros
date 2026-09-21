"""Recovery-only read budget: fresh tag HEADs, byte-verified immutable content."""
import base64
import binascii
import copy
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
import urllib.error
import urllib.request

try:
    from scripts.chr_matrix import MatrixRegistry
    from scripts.image_release import GHCR, HUB, SafeRedirect, decode_manifest, http_reason
except ModuleNotFoundError:
    from chr_matrix import MatrixRegistry
    from image_release import GHCR, HUB, SafeRedirect, decode_manifest, http_reason

# Bump when the shared-content wire format changes; consumers refuse other values.
CONTENT_SCHEMA = 1


class ReadRedirect(SafeRedirect):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if request.get_method() not in ('GET', 'HEAD'):
            raise ValueError('Unapproved read method')
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        # urllib otherwise turns redirected HEAD into a quota-consuming GET.
        if redirected is not None:
            redirected.method = request.get_method()
        return redirected


def fetch(url, headers, *, method='GET'):
    if method not in ('GET', 'HEAD'):
        raise ValueError('Unapproved read method')
    try:
        opener = urllib.request.build_opener(ReadRedirect())
        with opener.open(urllib.request.Request(url, headers=headers, method=method), timeout=60) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        # Include a sentinel byte so oversized diagnostics are rejected, not truncated into a match.
        return error.code, error.read(4097), error.headers


def quota_diagnostic(body, headers):
    """Untrusted headers/body become fixed codes and bounded integers, never excerpts."""
    result = {'classification': 'unknown'}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str) or len(value) > 64:
            continue
        name = name.lower()
        if name in ('ratelimit-limit', 'ratelimit-remaining'):
            match = re.fullmatch(r'([0-9]{1,9})(?:;\s*w=([0-9]{1,9}))?', value.strip())
            if match:
                field = 'limit' if name == 'ratelimit-limit' else 'remaining'
                result[field] = int(match[1])
                if match[2] and int(match[2]) > 0:
                    result[field + '_window_seconds'] = int(match[2])
        elif name == 'retry-after' and re.fullmatch(r'[0-9]{1,9}', value.strip()):
            result['retry_after_seconds'] = int(value.strip())
        elif name == 'retry-after':
            try:
                date = parsedate_to_datetime(value)
                epoch = int(date.timestamp()) if date.tzinfo is not None else -1
                if 0 <= epoch <= 253402300799:
                    result['retry_after_epoch'] = epoch
            except (TypeError, ValueError, OverflowError):
                pass
    if isinstance(body, bytes) and len(body) <= 4096:
        messages = [body.decode('utf-8', errors='replace').strip()]
        try:
            data = json.loads(body)
            if isinstance(data, dict) and isinstance(data.get('errors'), list):
                messages.extend(error.get('message') for error in data['errors']
                                if isinstance(error, dict) and error.get('code') == 'TOOMANYREQUESTS')
        except ValueError:
            pass
        pull_messages = {
            'You have reached your pull rate limit. You may increase the limit by authenticating and upgrading: https://www.docker.com/increase-rate-limits',
            'You have reached your unauthenticated pull rate limit. https://www.docker.com/increase-rate-limit',
            'You have reached your pull rate limit. You may increase the limit by authenticating and upgrading: https://www.docker.com/increase-rate-limit',
        }
        if any(isinstance(message, str) and message in pull_messages for message in messages):
            result['classification'] = 'pull_quota'
        elif messages[0] == 'Too Many Requests':
            result['classification'] = 'abuse'
    if result.get('remaining') == 0 and result.get('remaining_window_seconds') == 21600:
        result['classification'] = 'pull_quota'
    return result


class RecoveryRegistry(MatrixRegistry):
    def __init__(self, credentials=None, *, fetch=fetch):
        super().__init__(credentials, fetch=fetch)
        self.manifests = {}
        self.content = {}
        self._read_method = 'GET'
        self._body = None

    def read(self, url, headers, operation, image):
        method = 'GET' if operation == 'token' else self._read_method
        try:
            status, body, response_headers = self.fetch(url, headers, method=method)
        except TimeoutError:
            raise self.error(operation, 'timeout', image) from None
        except (OSError, ValueError):
            raise self.error(operation, 'transport_error', image) from None
        if status == 429:
            error = self.error(operation, 'rate_limited', image, status)
            error.diagnostic['quota'] = quota_diagnostic(body, response_headers)
            # Stop immediately, including token responses. No 429 retries or probe GET.
            raise error
        if operation == 'manifest' and method == 'GET' and status == 200:
            # Retain the exact transported bytes; the digest is proven against these.
            self._body = body
        return status, body, response_headers

    def request(self, image, path, *, method='GET'):
        if (method not in ('GET', 'HEAD') or
                not re.fullmatch(r'(?:manifests/(?:sha256:[0-9a-f]{64}|[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})|blobs/sha256:[0-9a-f]{64})', path) or
                (method == 'HEAD' and not path.startswith('manifests/'))):
            raise self.error('manifest', 'invalid_reference', image)
        previous = self._read_method
        self._read_method = method
        try:
            return super().request(image, path)
        finally:
            self._read_method = previous

    def manifest(self, image, tag):
        if not re.fullmatch(r'(?:sha256:[0-9a-f]{64}|[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})', tag):
            raise self.error('manifest', 'invalid_reference', image)
        immutable = tag.startswith('sha256:')
        expected = tag if immutable else None
        if not immutable:
            status, _, headers = self.request(image, 'manifests/' + tag, method='HEAD')
            if status == 404:
                # A HEAD alone cannot prove MANIFEST_UNKNOWN. Never cache absence.
                absent = super().manifest(image, tag)
                if absent != (None, None):
                    raise self.error('manifest', 'digest_mismatch', image)
                return absent
            if status != 200:
                raise self.error('manifest', http_reason(status), image, status)
            expected = headers.get('Docker-Content-Digest')
            if not isinstance(expected, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', expected):
                raise self.error('manifest', 'digest_mismatch', image, status)
        key = (image, expected)
        if key not in self.manifests:
            self._body = None
            actual, manifest = super().manifest(image, tag)
            raw, self._body = self._body, None
            # Cache only bytes this process saw and re-hashed to the resolved digest.
            if (actual != expected or not isinstance(raw, bytes) or
                    'sha256:' + hashlib.sha256(raw).hexdigest() != expected):
                raise self.error('manifest', 'digest_mismatch', image)
            self.manifests[key] = manifest
            self.content[key] = raw
        return expected, copy.deepcopy(self.manifests[key])

    def resolve(self, image, tag):
        """Tag -> digest from a fresh HEAD alone, for read-only drift comparison.

        A 404 here means 'not observed', NOT proven absence: only a GET returns the
        MANIFEST_UNKNOWN body that distinguishes a missing manifest from any other
        404. Absence is what authorizes a write, so a caller that may write to this
        reference must use manifest() instead and pay for the proof.
        """
        if not re.fullmatch(r'(?:sha256:[0-9a-f]{64}|[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})', tag):
            raise self.error('manifest', 'invalid_reference', image)
        if tag.startswith('sha256:'):
            return tag
        status, _, headers = self.request(image, 'manifests/' + tag, method='HEAD')
        if status == 404:
            return None
        if status != 200:
            raise self.error('manifest', http_reason(status), image, status)
        digest = headers.get('Docker-Content-Digest')
        if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise self.error('manifest', 'digest_mismatch', image, status)
        return digest

    def export_content(self):
        """Digest-keyed immutable bytes only; tag bindings are deliberately never shared."""
        return {'schema': CONTENT_SCHEMA,
                'content': {f'{image}|{digest}': base64.b64encode(raw).decode()
                            for (image, digest), raw in sorted(self.content.items())}}

    def import_content(self, data):
        """Trust nothing: accept bytes only when they hash to the digest that keys them."""
        if not isinstance(data, dict) or data.get('schema') != CONTENT_SCHEMA:
            raise ValueError('Unsupported shared registry content schema')
        entries = data.get('content')
        if not isinstance(entries, dict):
            raise ValueError('Shared registry content is missing')
        for key, encoded in entries.items():
            if not isinstance(key, str) or not isinstance(encoded, str):
                raise ValueError('Shared registry content is malformed')
            image, separator, digest = key.partition('|')
            if (not separator or image not in (HUB, GHCR) or
                    not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)):
                raise ValueError('Shared registry content is outside the approved scope')
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError('Shared registry content is not valid base64') from None
            if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('Shared registry content does not match its digest')
            # Reuse the production decoder so a shared blob cannot relax manifest schema rules.
            actual, manifest = decode_manifest(200, raw, digest)
            if actual != digest or manifest is None:
                raise ValueError('Shared registry content is not a valid manifest')
            self.manifests[image, digest] = manifest
            self.content[image, digest] = raw
        return len(entries)
