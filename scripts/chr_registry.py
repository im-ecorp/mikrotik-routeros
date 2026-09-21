"""Recovery-only read budget: fresh tag HEADs, byte-verified immutable content."""
import copy
from email.utils import parsedate_to_datetime
import json
import re
import urllib.error
import urllib.request

try:
    from scripts.chr_matrix import MatrixRegistry
    from scripts.image_release import SafeRedirect, http_reason
except ModuleNotFoundError:
    from chr_matrix import MatrixRegistry
    from image_release import SafeRedirect, http_reason


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
        self._read_method = 'GET'

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
            actual, manifest = super().manifest(image, tag)
            if actual != expected:
                raise self.error('manifest', 'digest_mismatch', image)
            self.manifests[key] = manifest
        return expected, copy.deepcopy(self.manifests[key])
