#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///

"""
books_status – Read-only Bookshelf health probe.

Checks:
  reachable        GET /api/v1/system/status
  root_folder      GET /api/v1/rootfolder      resolved by configured name
  quality_profile  GET /api/v1/qualityprofile  resolved by configured name
  metadata_profile GET /api/v1/metadataprofile resolved by configured name

Each profile check requires exactly one matching record; 0 or >1 is
reported as configuration_error.

Optional param metadata_check=true adds a book/lookup probe.  A 200 OK with
an empty list is ambiguous (metadata-service failure vs. no results) and is
reported honestly as no_results_or_unavailable.

No mutations are performed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
from runtime import (  # noqa: E402
    BookshelfError,
    bookshelf_get,
    load_config,
    optional_bool,
    run_tool,
)

_LOOKUP_PROBE_TERM = 'pride prejudice'


def _ok(**kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = {'ok': True}
    result.update(kwargs)
    return result


def _fail(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {'ok': False, 'error': {'code': code, 'message': message, 'retryable': retryable}}


def _resolve_by_name(
    items: list[Any],
    name: str,
    label: str,
    endpoint: str,
) -> dict[str, Any]:
    """Return a check dict for a list resolved by name.

    Requires exactly one match; 0 or >1 → configuration_error.
    On success returns ok=True with id and name of the matched record.
    """
    if not isinstance(items, list):
        return _fail('invalid_response', f'Expected a list from {endpoint}')
    matches = [i for i in items if isinstance(i, dict) and i.get('name') == name]
    if len(matches) == 1:
        return _ok(id=matches[0].get('id'), name=matches[0].get('name'))
    if len(matches) == 0:
        return _fail(
            'configuration_error',
            f"No {label} named '{name}' found in Bookshelf",
        )
    return _fail(
        'configuration_error',
        f"Found {len(matches)} {label}s named '{name}' in Bookshelf; expected exactly 1",
    )


def handle(params: dict[str, Any]) -> dict[str, Any]:
    metadata_check: bool = optional_bool(params, 'metadata_check') or False

    config = load_config()
    checks: dict[str, Any] = {}
    overall = 'ok'

    # --- reachability + authentication ---
    try:
        status_data = bookshelf_get('/api/v1/system/status', config=config)
        version = (
            status_data.get('version', 'unknown')
            if isinstance(status_data, dict)
            else 'unknown'
        )
        checks['reachable'] = _ok(version=version)
    except BookshelfError as exc:
        checks['reachable'] = exc.as_dict()
        # Unreachable or auth failure: no further checks are meaningful
        return {'ok': True, 'status': 'unavailable', 'checks': checks}

    # --- root folder ---
    try:
        folders = bookshelf_get('/api/v1/rootfolder', config=config)
        check = _resolve_by_name(
            folders, config['root_folder_name'], 'root folder', '/api/v1/rootfolder'
        )
        if check['ok']:
            # Include path for diagnostics
            matches = [
                f for f in folders
                if isinstance(f, dict) and f.get('name') == config['root_folder_name']
            ]
            check['path'] = matches[0].get('path') if matches else None
        checks['root_folder'] = check
        if not check['ok']:
            overall = 'degraded'
    except BookshelfError as exc:
        checks['root_folder'] = exc.as_dict()
        overall = 'degraded'

    # --- quality profile ---
    try:
        profiles = bookshelf_get('/api/v1/qualityprofile', config=config)
        check = _resolve_by_name(
            profiles,
            config['quality_profile_name'],
            'quality profile',
            '/api/v1/qualityprofile',
        )
        checks['quality_profile'] = check
        if not check['ok']:
            overall = 'degraded'
    except BookshelfError as exc:
        checks['quality_profile'] = exc.as_dict()
        overall = 'degraded'

    # --- metadata profile ---
    try:
        profiles = bookshelf_get('/api/v1/metadataprofile', config=config)
        check = _resolve_by_name(
            profiles,
            config['metadata_profile_name'],
            'metadata profile',
            '/api/v1/metadataprofile',
        )
        checks['metadata_profile'] = check
        if not check['ok']:
            overall = 'degraded'
    except BookshelfError as exc:
        checks['metadata_profile'] = exc.as_dict()
        overall = 'degraded'

    # --- optional metadata lookup probe ---
    if metadata_check:
        path = f'/api/v1/book/lookup?term={quote(_LOOKUP_PROBE_TERM)}'
        try:
            results = bookshelf_get(path, config=config)
            if not isinstance(results, list):
                checks['metadata_lookup'] = _fail(
                    'invalid_response', 'book/lookup did not return a list'
                )
                overall = 'degraded'
            elif results:
                count = len(results)
                checks['metadata_lookup'] = {
                    'ok': True,
                    'heuristic': f'lookup returned {count} result(s)',
                    'result': 'has_results',
                }
            else:
                # 200 OK + empty list is ambiguous — see AUDIOBOOK-STACK-DIAGNOSIS.md
                checks['metadata_lookup'] = {
                    'ok': True,
                    'heuristic': (
                        'lookup returned 0 results (200 OK + empty list is ambiguous'
                        ' — may indicate metadata service failure)'
                    ),
                    'result': 'no_results_or_unavailable',
                }
        except BookshelfError as exc:
            checks['metadata_lookup'] = exc.as_dict()
            overall = 'degraded'

    return {'ok': True, 'status': overall, 'checks': checks}


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
