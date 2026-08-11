"""
shared/runtime.py – Shared utilities for the ebooks Stavrobot plugin.

Security invariants (enforced by construction):
- bookshelf_api_key is ONLY sent as the X-Api-Key request header; never in URLs
  or error messages.
- Raw upstream response bodies are never passed through to callers. Error paths
  read only a bounded prefix and discard it; callers receive structured codes.
- ToolError messages must be secret-free before they are written to stderr.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_DURABLE_RESOURCE_ID = (1 << 31) - 1
_MAX_HTTP_TIMEOUT_SECONDS = 120
_HTTP_TIMEOUT_ERROR = (
    'config http_timeout_seconds must be greater than 0 and at most 120'
)
_BOOKSHELF_TIMEOUT_MESSAGE = 'Bookshelf did not respond within the configured timeout'
_TOOL_DEADLINE_SECONDS = 25.0
_DEADLINE_CONFIG_KEY = '_deadline_monotonic'
_PLAIN_DECIMAL_RE = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?')


def monotonic() -> float:
    """Read monotonic time through a small injectable seam for tests."""
    return time.monotonic()


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Fatal tool failure.  Message must be free of secrets."""


class BookshelfError(Exception):
    """Bookshelf API call failure with structured, secret-free error info."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {
            'ok': False,
            'error': {
                'code': self.code,
                'message': str(self),
                'retryable': self.retryable,
            },
        }


# ---------------------------------------------------------------------------
# Tool entry-point wrapper
# ---------------------------------------------------------------------------


def run_tool(handler: Callable[[dict[str, Any]], Any]) -> int:
    """Read params from stdin, call handler, write result JSON to stdout.

    Returns the exit code (0 on success, 1 on ToolError).
    """
    try:
        params = read_params()
        result = handler(params)
        json.dump(result, sys.stdout, separators=(',', ':'), ensure_ascii=False)
        sys.stdout.write('\n')
        return 0
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def read_params() -> dict[str, Any]:
    """Decode JSON object from stdin."""
    try:
        params = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ToolError('stdin must be valid JSON') from exc
    if not isinstance(params, dict):
        raise ToolError('stdin JSON must be an object')
    return params


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _parse_http_timeout(value: Any) -> float:
    """Normalize a bounded JSON number or settings-page decimal string."""
    if isinstance(value, bool):
        raise ToolError(_HTTP_TIMEOUT_ERROR)

    if isinstance(value, str):
        text = value.strip()
        if _PLAIN_DECIMAL_RE.fullmatch(text) is None:
            raise ToolError(_HTTP_TIMEOUT_ERROR)
        try:
            exact_timeout = Decimal(text)
        except InvalidOperation as exc:
            raise ToolError(_HTTP_TIMEOUT_ERROR) from exc
        if not Decimal(0) < exact_timeout <= Decimal(_MAX_HTTP_TIMEOUT_SECONDS):
            raise ToolError(_HTTP_TIMEOUT_ERROR)
        timeout = float(exact_timeout)
    elif isinstance(value, int):
        if not 0 < value <= _MAX_HTTP_TIMEOUT_SECONDS:
            raise ToolError(_HTTP_TIMEOUT_ERROR)
        timeout = float(value)
    elif isinstance(value, float):
        timeout = value
    else:
        raise ToolError(_HTTP_TIMEOUT_ERROR)

    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or timeout > _MAX_HTTP_TIMEOUT_SECONDS
    ):
        raise ToolError(_HTTP_TIMEOUT_ERROR)
    return timeout


def load_config() -> dict[str, Any]:
    """Load and validate config.json from the plugin root.

    Returns a clean config dict.  The API key value is never included in any
    raised ToolError message.
    """
    deadline = monotonic() + _TOOL_DEADLINE_SECONDS
    config_path = PLUGIN_ROOT / 'config.json'
    try:
        with config_path.open('r', encoding='utf-8') as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise ToolError(
            'config.json is missing; set bookshelf_url and bookshelf_api_key'
        ) from exc
    except json.JSONDecodeError as exc:
        raise ToolError('config.json must contain valid JSON') from exc

    if not isinstance(raw, dict):
        raise ToolError('config.json must be a JSON object')

    # bookshelf_url (required)
    url = raw.get('bookshelf_url')
    if not isinstance(url, str) or not url.strip():
        raise ToolError('config bookshelf_url is missing or empty')
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ToolError('config bookshelf_url must be an HTTP or HTTPS URL')

    # bookshelf_api_key (required) – never mention its value in errors
    key = raw.get('bookshelf_api_key')
    if not isinstance(key, str) or not key.strip():
        raise ToolError('config bookshelf_api_key is missing or empty')

    # root_folder_name (optional, default 'Bookshelf Sandbox')
    root_folder_name = raw.get('root_folder_name', 'Bookshelf Sandbox')
    if not isinstance(root_folder_name, str) or not root_folder_name.strip():
        raise ToolError('config root_folder_name must be a non-empty string')

    # quality_profile_name (optional, default 'eBook')
    quality_profile_name = raw.get('quality_profile_name', 'eBook')
    if not isinstance(quality_profile_name, str) or not quality_profile_name.strip():
        raise ToolError('config quality_profile_name must be a non-empty string')

    # metadata_profile_name (optional, default 'Standard')
    metadata_profile_name = raw.get('metadata_profile_name', 'Standard')
    if not isinstance(metadata_profile_name, str) or not metadata_profile_name.strip():
        raise ToolError('config metadata_profile_name must be a non-empty string')

    # http_timeout_seconds (optional, default 15). Stavrobot's settings page
    # serializes config fields as strings, so accept its canonical decimal form.
    timeout = _parse_http_timeout(raw.get('http_timeout_seconds', 15))

    return {
        'bookshelf_url': url.rstrip('/'),
        'bookshelf_api_key': key.strip(),
        'root_folder_name': root_folder_name.strip(),
        'quality_profile_name': quality_profile_name.strip(),
        'metadata_profile_name': metadata_profile_name.strip(),
        'http_timeout_seconds': timeout,
        # Internal runtime state only.  It is deliberately not read from
        # config.json, so callers cannot extend the tool-wide budget.
        _DEADLINE_CONFIG_KEY: deadline,
    }


# ---------------------------------------------------------------------------
# Bookshelf HTTP helpers
# ---------------------------------------------------------------------------


def _request_timeout(config: dict[str, Any]) -> float:
    """Return the per-call timeout capped by this invocation's deadline.

    Direct unit-test configs may omit the internal deadline key; in that case
    they retain the historical configured per-request timeout and do not gain
    a hidden dependency on wall-clock time.
    """
    configured_timeout = config.get('http_timeout_seconds', 15)
    deadline = config.get(_DEADLINE_CONFIG_KEY)
    if deadline is None:
        return configured_timeout

    # Only load_config creates this value.  Treat a malformed direct-test
    # fixture as an old-style config rather than leaking an internal exception.
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return configured_timeout

    remaining = deadline - monotonic()
    if remaining <= 0:
        raise BookshelfError(
            'timeout',
            _BOOKSHELF_TIMEOUT_MESSAGE,
            retryable=True,
        )
    return min(configured_timeout, remaining)


def _read_bounded_response(response: Any) -> bytes:
    """Read at most one byte beyond the limit so oversize is detectable."""
    body = response.read(_MAX_RESPONSE_BYTES + 1)
    if not isinstance(body, bytes):
        raise BookshelfError(
            'invalid_response',
            'Bookshelf returned a non-JSON response',
            retryable=False,
        )
    if len(body) > _MAX_RESPONSE_BYTES:
        raise BookshelfError(
            'response_too_large',
            'Bookshelf response exceeded the maximum allowed size',
            retryable=False,
        )
    return body


def _discard_bounded_error_body(exc: HTTPError) -> None:
    """Discard only a bounded prefix of an upstream HTTP error body."""
    try:
        exc.read(_MAX_RESPONSE_BYTES + 1)
    except Exception:
        # The body is never used for error mapping, and a secondary read error
        # must not replace the fixed, sanitized HTTP status error below.
        pass


def bookshelf_get(path: str, *, config: dict[str, Any]) -> Any:
    """Perform an authenticated GET against the Bookshelf API.

    Security invariants:
    - API key is sent exclusively as the X-Api-Key header (never in the URL).
    - Response body is limited to _MAX_RESPONSE_BYTES.
    - On HTTP error only a bounded prefix is read and discarded; callers receive
      a structured BookshelfError with a secret-free message.

    Returns the parsed JSON value (dict or list) on success.
    Raises BookshelfError on any failure.
    """
    timeout = _request_timeout(config)
    url = config['bookshelf_url'] + path
    request = Request(
        url,
        headers={'X-Api-Key': config['bookshelf_api_key']},
        method='GET',
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            body = _read_bounded_response(response)
    except HTTPError as exc:
        _discard_bounded_error_body(exc)
        if exc.code in {401, 403}:
            raise BookshelfError(
                'authentication_failed',
                'Bookshelf rejected the API key; check bookshelf_api_key in config',
                retryable=False,
            ) from exc
        if exc.code == 404:
            raise BookshelfError(
                'not_found',
                'Bookshelf did not find the requested resource',
                retryable=False,
            ) from exc
        retryable = exc.code >= 500
        raise BookshelfError(
            'upstream_error',
            f'Bookshelf returned HTTP {exc.code}',
            retryable=retryable,
        ) from exc
    except TimeoutError as exc:
        # Python 3.13+ may raise TimeoutError directly rather than wrapping in URLError
        raise BookshelfError(
            'timeout',
            'Bookshelf did not respond within the configured timeout',
            retryable=True,
        ) from exc
    except URLError as exc:
        # Python 3.11/3.12: socket.timeout (a TimeoutError / OSError subclass) is
        # wrapped in URLError.  Python 3.13+ raises TimeoutError directly (handled
        # above), but keep this check for defence in depth.
        if isinstance(exc.reason, OSError) and isinstance(exc.reason, TimeoutError):
            raise BookshelfError(
                'timeout',
                'Bookshelf did not respond within the configured timeout',
                retryable=True,
            ) from exc
        raise BookshelfError(
            'unreachable',
            'Bookshelf is not reachable; check bookshelf_url in config',
            retryable=True,
        ) from exc

    try:
        return json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BookshelfError(
            'invalid_response',
            'Bookshelf returned a non-JSON response',
            retryable=False,
        ) from exc


# ---------------------------------------------------------------------------
# Plugin-owned JSON mutations
# ---------------------------------------------------------------------------


# Keep this list deliberately small.  The route is part of the helper's
# contract, rather than an arbitrary URL supplied by a tool caller.
_ALLOWED_JSON_MUTATIONS = frozenset(
    {
        ('POST', '/api/v1/book'),
        ('PUT', '/api/v1/book/monitor'),
        ('POST', '/api/v1/command'),
    }
)
ALLOWED_JSON_MUTATIONS = _ALLOWED_JSON_MUTATIONS


def _is_positive_durable_resource_id(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= _MAX_DURABLE_RESOURCE_ID
    )


def _is_single_durable_id_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 1
        and _is_positive_durable_resource_id(value[0])
    )


def _validate_json_mutation_body(method: str, path: str, body: Any) -> None:
    """Restrict fixed RPC-like routes to the plugin's exact safe bodies."""
    if not isinstance(body, dict):
        raise ToolError('Bookshelf mutation body is not allowed')

    if (method, path) == ('POST', '/api/v1/command'):
        if (
            set(body) != {'name', 'bookIds'}
            or body.get('name') != 'BookSearch'
            or not _is_single_durable_id_list(body.get('bookIds'))
        ):
            raise ToolError('Bookshelf mutation body is not allowed')

    if (method, path) == ('PUT', '/api/v1/book/monitor'):
        if (
            set(body) != {'bookIds', 'monitored'}
            or body.get('monitored') is not True
            or not _is_single_durable_id_list(body.get('bookIds'))
        ):
            raise ToolError('Bookshelf mutation body is not allowed')


def bookshelf_json_mutation(
    method: str,
    path: str,
    body: Any,
    *,
    config: dict[str, Any],
) -> Any:
    """Perform one authenticated JSON mutation on a plugin-owned route.

    The caller can select only one of the fixed method/path pairs above.  The
    base URL and authentication headers are always derived here from the
    validated config; callers cannot provide either a URL or headers.  The
    response is bounded and parsed, while only a bounded prefix of upstream
    error bodies is read and discarded just like :func:`bookshelf_get`.
    """
    if not isinstance(method, str) or not isinstance(path, str):
        raise ToolError('Bookshelf mutation route is not allowed')
    normalized_method = method.upper()
    if (normalized_method, path) not in _ALLOWED_JSON_MUTATIONS:
        raise ToolError('Bookshelf mutation route is not allowed')
    _validate_json_mutation_body(normalized_method, path, body)

    try:
        encoded_body = json.dumps(
            body,
            ensure_ascii=False,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ToolError('Bookshelf mutation body must be valid JSON') from exc

    url = config.get('bookshelf_url')
    api_key = config.get('bookshelf_api_key')
    if not isinstance(url, str) or not url.strip():
        raise ToolError('config bookshelf_url is missing or empty')
    if not isinstance(api_key, str) or not api_key.strip():
        raise ToolError('config bookshelf_api_key is missing or empty')

    timeout = _request_timeout(config)
    request = Request(
        url.rstrip('/') + path,
        data=encoded_body,
        headers={
            'X-Api-Key': api_key,
            'Content-Type': 'application/json',
        },
        method=normalized_method,
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            # The shared bounded reader detects oversize with one extra byte.
            # No response text is included in a caller-facing error.
            response_body = _read_bounded_response(response)
    except HTTPError as exc:
        _discard_bounded_error_body(exc)
        if exc.code in {401, 403}:
            raise BookshelfError(
                'authentication_failed',
                'Bookshelf rejected the API key; check bookshelf_api_key in config',
                retryable=False,
            ) from exc
        if exc.code == 404:
            raise BookshelfError(
                'not_found',
                'Bookshelf mutation route was not found',
                retryable=False,
            ) from exc
        retryable = exc.code >= 500
        raise BookshelfError(
            'upstream_error',
            f'Bookshelf returned HTTP {exc.code}',
            retryable=retryable,
        ) from exc
    except TimeoutError as exc:
        raise BookshelfError(
            'timeout',
            'Bookshelf did not respond within the configured timeout',
            retryable=True,
        ) from exc
    except URLError as exc:
        if isinstance(exc.reason, OSError) and isinstance(exc.reason, TimeoutError):
            raise BookshelfError(
                'timeout',
                'Bookshelf did not respond within the configured timeout',
                retryable=True,
            ) from exc
        raise BookshelfError(
            'unreachable',
            'Bookshelf is not reachable; check bookshelf_url in config',
            retryable=True,
        ) from exc

    if not response_body:
        return None

    try:
        return json.loads(response_body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BookshelfError(
            'invalid_response',
            'Bookshelf returned a non-JSON response',
            retryable=False,
        ) from exc


# These aliases keep the helper discoverable beside bookshelf_get while
# retaining one implementation and one allowlist.
bookshelf_mutation = bookshelf_json_mutation
bookshelf_mutate = bookshelf_json_mutation


# ---------------------------------------------------------------------------
# Parameter validators (for tool params arriving via stdin)
# ---------------------------------------------------------------------------


def require_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f'{key} is required')
    return value.strip()


def optional_string(params: dict[str, Any], key: str) -> str | None:
    if key not in params:
        return None
    value = params[key]
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f'{key} must be a non-empty string')
    return value.strip()


def optional_bool(params: dict[str, Any], key: str) -> bool | None:
    if key not in params:
        return None
    value = params[key]
    if not isinstance(value, bool):
        raise ToolError(f'{key} must be a boolean')
    return value


def optional_int(params: dict[str, Any], key: str) -> int | None:
    if key not in params:
        return None
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f'{key} must be an integer')
    return value
