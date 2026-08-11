"""
Tests for books_status/run.py (the handle() function).

All tests mock load_config and bookshelf_get so no network calls occur.
Covers:
- Happy path: all checks pass → status ok
- Reachability failure → status unavailable (no further checks)
- Auth (401/403) failure → status unavailable
- Root folder not found → status degraded
- Root folder name matches multiple records → configuration_error
- Quality profile not found → status degraded
- Metadata profile not found → status degraded
- metadata_check=False → no metadata_lookup key in checks
- metadata_check=True with results → has_results
- metadata_check=True with empty list → no_results_or_unavailable (ambiguous)
- metadata_check=True with upstream error → degraded
- Invalid response shape for folder list
- API key not in any output
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# conftest.py has already added books_status/ and shared/ to sys.path
import run as books_status_run
from runtime import BookshelfError

_BASE_CONFIG: dict[str, Any] = {
    'bookshelf_url': 'http://localhost:8787',
    'bookshelf_api_key': 'REDACTED-IN-OUTPUT',
    'root_folder_name': 'Bookshelf Sandbox',
    'quality_profile_name': 'eBook',
    'metadata_profile_name': 'Standard',
    'http_timeout_seconds': 5.0,
}

_STATUS_DATA = {'version': '0.4.20.129', 'appName': 'Readarr'}
_ROOTFOLDER_DATA = [{'id': 1, 'name': 'Bookshelf Sandbox', 'path': '/books'}]
_QUALITY_DATA = [{'id': 1, 'name': 'eBook'}, {'id': 2, 'name': 'Spoken'}]
_METADATA_DATA = [{'id': 1, 'name': 'Standard'}, {'id': 2, 'name': 'None'}]
_LOOKUP_DATA = [{'id': 20, 'title': 'Pride and Prejudice'}]


def _side_effects(*responses: Any) -> Any:
    """Return a side_effect list for bookshelf_get calls in order."""
    return list(responses)


class TestBooksStatusHappyPath:
    def test_all_ok(self) -> None:
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, _ROOTFOLDER_DATA, _QUALITY_DATA, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({})

        assert result['ok'] is True
        assert result['status'] == 'ok'
        checks = result['checks']
        assert checks['reachable']['ok'] is True
        assert checks['reachable']['version'] == '0.4.20.129'
        assert checks['root_folder']['ok'] is True
        assert checks['root_folder']['id'] == 1
        assert checks['root_folder']['path'] == '/books'
        assert checks['quality_profile']['ok'] is True
        assert checks['quality_profile']['id'] == 1
        assert checks['metadata_profile']['ok'] is True
        assert checks['metadata_profile']['id'] == 1

    def test_no_metadata_lookup_by_default(self) -> None:
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, _ROOTFOLDER_DATA, _QUALITY_DATA, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({})

        assert 'metadata_lookup' not in result['checks']

    def test_api_key_not_in_output(self) -> None:
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, _ROOTFOLDER_DATA, _QUALITY_DATA, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({})

        import json
        output_str = json.dumps(result)
        assert 'REDACTED-IN-OUTPUT' not in output_str


class TestBooksStatusUnavailable:
    def test_connection_refused_returns_unavailable(self) -> None:
        err = BookshelfError('unreachable', 'not reachable', retryable=True)
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch('run.bookshelf_get', side_effect=[err]),
        ):
            result = books_status_run.handle({})

        assert result['ok'] is True
        assert result['status'] == 'unavailable'
        assert result['checks']['reachable']['ok'] is False
        # Should stop after reachability failure — no other checks
        assert 'root_folder' not in result['checks']
        assert 'quality_profile' not in result['checks']

    def test_auth_failure_returns_unavailable(self) -> None:
        err = BookshelfError('authentication_failed', 'bad key', retryable=False)
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch('run.bookshelf_get', side_effect=[err]),
        ):
            result = books_status_run.handle({})

        assert result['status'] == 'unavailable'
        assert result['checks']['reachable']['error']['code'] == 'authentication_failed'


class TestBooksStatusDegraded:
    def test_root_folder_not_found_is_degraded(self) -> None:
        config = {**_BASE_CONFIG, 'root_folder_name': 'No Such Folder'}
        with (
            patch('run.load_config', return_value=config),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, _ROOTFOLDER_DATA, _QUALITY_DATA, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({})

        assert result['status'] == 'degraded'
        rf = result['checks']['root_folder']
        assert rf['ok'] is False
        assert rf['error']['code'] == 'configuration_error'
        assert 'No Such Folder' in rf['error']['message']

    def test_root_folder_multiple_matches_is_configuration_error(self) -> None:
        duplicate_folders = [
            {'id': 1, 'name': 'Bookshelf Sandbox', 'path': '/books1'},
            {'id': 2, 'name': 'Bookshelf Sandbox', 'path': '/books2'},
        ]
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, duplicate_folders, _QUALITY_DATA, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({})

        assert result['status'] == 'degraded'
        rf = result['checks']['root_folder']
        assert rf['ok'] is False
        assert rf['error']['code'] == 'configuration_error'
        assert '2' in rf['error']['message']

    def test_quality_profile_not_found_is_degraded(self) -> None:
        config = {**_BASE_CONFIG, 'quality_profile_name': 'NoSuchProfile'}
        with (
            patch('run.load_config', return_value=config),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, _ROOTFOLDER_DATA, _QUALITY_DATA, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({})

        assert result['status'] == 'degraded'
        qp = result['checks']['quality_profile']
        assert qp['ok'] is False
        assert qp['error']['code'] == 'configuration_error'

    def test_metadata_profile_not_found_is_degraded(self) -> None:
        config = {**_BASE_CONFIG, 'metadata_profile_name': 'Unknown'}
        with (
            patch('run.load_config', return_value=config),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, _ROOTFOLDER_DATA, _QUALITY_DATA, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({})

        assert result['status'] == 'degraded'
        mp = result['checks']['metadata_profile']
        assert mp['ok'] is False
        assert mp['error']['code'] == 'configuration_error'

    def test_root_folder_invalid_response_shape_is_degraded(self) -> None:
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, {'not': 'a list'}, _QUALITY_DATA, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({})

        assert result['status'] == 'degraded'
        assert result['checks']['root_folder']['ok'] is False

    def test_bookshelf_error_on_quality_profile_is_degraded(self) -> None:
        err = BookshelfError('upstream_error', 'HTTP 503', retryable=True)
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, _ROOTFOLDER_DATA, err, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({})

        assert result['status'] == 'degraded'
        assert result['checks']['quality_profile']['ok'] is False
        assert result['checks']['quality_profile']['error']['retryable'] is True


class TestBooksStatusMetadataCheck:
    def test_metadata_check_false_skipped(self) -> None:
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[_STATUS_DATA, _ROOTFOLDER_DATA, _QUALITY_DATA, _METADATA_DATA],
            ),
        ):
            result = books_status_run.handle({'metadata_check': False})

        assert 'metadata_lookup' not in result['checks']

    def test_metadata_check_true_with_results(self) -> None:
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[
                    _STATUS_DATA,
                    _ROOTFOLDER_DATA,
                    _QUALITY_DATA,
                    _METADATA_DATA,
                    _LOOKUP_DATA,
                ],
            ),
        ):
            result = books_status_run.handle({'metadata_check': True})

        ml = result['checks']['metadata_lookup']
        assert ml['ok'] is True
        assert ml['result'] == 'has_results'
        assert '1' in ml['heuristic']

    def test_metadata_check_true_empty_list_ambiguous(self) -> None:
        """200 OK + [] is ambiguous and must be reported honestly."""
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[
                    _STATUS_DATA,
                    _ROOTFOLDER_DATA,
                    _QUALITY_DATA,
                    _METADATA_DATA,
                    [],  # empty lookup result
                ],
            ),
        ):
            result = books_status_run.handle({'metadata_check': True})

        ml = result['checks']['metadata_lookup']
        assert ml['ok'] is True
        assert ml['result'] == 'no_results_or_unavailable'
        assert 'ambiguous' in ml['heuristic']
        # Empty result does not degrade overall status
        assert result['status'] == 'ok'

    def test_metadata_check_error_degrades_status(self) -> None:
        err = BookshelfError('upstream_error', 'HTTP 500', retryable=True)
        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
            patch(
                'run.bookshelf_get',
                side_effect=[
                    _STATUS_DATA,
                    _ROOTFOLDER_DATA,
                    _QUALITY_DATA,
                    _METADATA_DATA,
                    err,
                ],
            ),
        ):
            result = books_status_run.handle({'metadata_check': True})

        assert result['status'] == 'degraded'
        assert result['checks']['metadata_lookup']['ok'] is False

    def test_metadata_check_invalid_bool_raises(self) -> None:
        """metadata_check must be a boolean, not a string."""
        from runtime import ToolError

        with (
            patch('run.load_config', return_value=_BASE_CONFIG),
        ):
            with pytest.raises(ToolError, match='boolean'):
                books_status_run.handle({'metadata_check': 'yes'})
