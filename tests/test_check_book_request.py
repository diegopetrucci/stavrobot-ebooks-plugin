"""Contract tests for check_book_request with sanitized local fakes only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest

from runtime import BookshelfError, ToolError


_RUN_PATH = Path(__file__).parent.parent / 'check_book_request' / 'run.py'
_SPEC = importlib.util.spec_from_file_location('check_book_request_run', _RUN_PATH)
assert _SPEC is not None and _SPEC.loader is not None
check_book_request_run = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check_book_request_run
_SPEC.loader.exec_module(check_book_request_run)


_CONFIG: dict[str, Any] = {
    'bookshelf_url': 'http://bookshelf.invalid',
    'bookshelf_api_key': 'REDACTED-IN-OUTPUT',
    'http_timeout_seconds': 5.0,
}
_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace('+00:00', 'Z')


def _book(
    book_id: int,
    *,
    file_count: int = 0,
    last_search: datetime | None = None,
    monitored: bool = True,
) -> dict[str, Any]:
    return {
        'id': book_id,
        'title': 'Sanitized ebook title',
        'monitored': monitored,
        'statistics': {
            'bookFileCount': file_count,
            'bookCount': 1,
            'sizeOnDisk': 1_234_567 if file_count else 0,
        },
        **({'lastSearchTime': _iso(last_search)} if last_search is not None else {}),
    }


def _event(
    book_id: int,
    event_type: str,
    *,
    when: datetime | None = None,
    download_id: str | None = None,
    data: dict[str, Any] | None = None,
    quality: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        'bookId': book_id,
        'eventType': event_type,
    }
    if when is not None:
        record['date'] = _iso(when)
    if download_id is not None:
        record['downloadId'] = download_id
    if data is not None:
        record['data'] = data
    if quality is not None:
        record['quality'] = {'quality': {'name': quality}}
    return record


def _fake_handle(
    request_id: int,
    *,
    book: dict[str, Any] | None = None,
    queue: Any = None,
    history: Any = None,
    commands: Any = None,
    queue_error: BaseException | None = None,
    commands_error: BaseException | None = None,
) -> tuple[dict[str, Any], list[str]]:
    responses: dict[str, Any] = {
        'book': book if book is not None else _book(request_id),
        'queue': [] if queue is None else queue,
        'history': {'records': []} if history is None else history,
        'command': [] if commands is None else commands,
    }
    calls: list[str] = []

    def fake_get(path: str, *, config: dict[str, Any]) -> Any:
        calls.append(path)
        if path.startswith('/api/v1/book/'):
            return responses['book']
        if path.startswith('/api/v1/queue'):
            if queue_error is not None:
                raise queue_error
            return responses['queue']
        if path.startswith('/api/v1/history'):
            return responses['history']
        if path == '/api/v1/command':
            if commands_error is not None:
                raise commands_error
            return responses['command']
        raise AssertionError(f'unexpected path: {path}')

    with (
        patch.object(check_book_request_run, 'load_config', return_value=_CONFIG),
        patch.object(check_book_request_run, 'bookshelf_get', side_effect=fake_get),
        patch.object(check_book_request_run, '_utcnow', return_value=_NOW),
    ):
        return check_book_request_run.handle({'request_id': request_id}), calls


class TestCheckBookRequestStates:
    def test_book_20_imported_with_matching_grab_and_import_history(self) -> None:
        book = _book(20, file_count=1)
        book['bookFiles'] = [
            {
                'quality': {'quality': {'name': 'EPUB'}},
                'size': 1_234_567,
                'path': '/private/library/must-not-be-returned/book.epub',
            }
        ]
        history = {
            'page': 1,
            'pageSize': 100,
            'records': [
                # The sibling record proves that title/author or a raw data
                # URL cannot accidentally identify this request.
                _event(
                    21,
                    'bookFileImported',
                    download_id='sibling-download',
                    data={'downloadUrl': 'https://private-tracker.invalid/private/sibling'},
                ),
                _event(
                    20,
                    'grabbed',
                    when=_NOW - timedelta(minutes=30),
                    download_id='download-20',
                    data={'downloadUrl': 'https://private-tracker.invalid/private/grab'},
                ),
                _event(
                    20,
                    'bookFileImported',
                    when=_NOW - timedelta(minutes=5),
                    download_id='download-20',
                    data={'downloadUrl': 'https://private-tracker.invalid/private/import'},
                    quality='EPUB',
                ),
            ],
        }

        result, calls = _fake_handle(20, book=book, history=history)

        assert result['ok'] is True
        assert result['request_id'] == 20
        assert result['state'] == 'imported'
        assert result['terminal'] is True
        assert result['suggested_check_after_seconds'] == 0
        assert result['detail']['quality'] == 'EPUB'
        assert result['detail']['size'] == 1_234_567
        rendered = json.dumps(result)
        assert 'private-tracker' not in rendered.casefold()
        assert 'downloadUrl' not in rendered
        assert 'download-20' not in rendered
        assert '/private/' not in rendered

        assert calls[0] == '/api/v1/book/20'
        queue_query = parse_qs(urlsplit(calls[1]).query)
        assert queue_query['page'] == ['1']
        assert queue_query['pageSize'] == ['100']
        history_query = parse_qs(urlsplit(calls[2]).query)
        assert history_query['bookId'] == ['20']
        assert history_query['pageSize'] == ['100']
        assert calls[3] == '/api/v1/command'

    def test_book_20_imported_with_download_id_and_no_grab_is_terminal(self) -> None:
        """A current history page need not retain Book 20's old grabbed event."""
        history = {
            'records': [
                _event(
                    20,
                    'bookFileImported',
                    when=_NOW - timedelta(minutes=5),
                    download_id='download-20',
                    data={'downloadUrl': 'https://private-tracker.invalid/private/import'},
                    quality='EPUB',
                )
            ]
        }

        result, _ = _fake_handle(20, book=_book(20, file_count=1), history=history)

        assert result['ok'] is True
        assert result['state'] == 'imported'
        assert result['terminal'] is True
        assert result['suggested_check_after_seconds'] == 0
        rendered = json.dumps(result)
        assert 'private-tracker' not in rendered.casefold()
        assert 'downloadUrl' not in rendered
        assert 'download-20' not in rendered

    def test_file_count_without_book_scoped_import_is_not_terminal(self) -> None:
        result, _ = _fake_handle(20, book=_book(20, file_count=1))

        assert result['state'] == 'requested'
        assert result['terminal'] is False
        assert result['suggested_check_after_seconds'] == 1800

    def test_book_54_old_grab_is_grabbed_stalled(self) -> None:
        history = {
            'records': [
                _event(
                    54,
                    'grabbed',
                    when=_NOW - timedelta(hours=3),
                    download_id='download-54',
                    data={
                        'downloadUrl': 'https://private-tracker.invalid/private/release',
                        'downloadClient': 'qBittorrent',
                        'indexer': 'private-indexer',
                    },
                ),
                _event(55, 'bookFileImported', download_id='other-book'),
            ]
        }

        result, _ = _fake_handle(54, book=_book(54), history=history)

        assert result['state'] == 'grabbed_stalled'
        assert result['terminal'] is False
        assert result['suggested_check_after_seconds'] == 1800
        assert 'manual attention' in result['detail']['message']
        rendered = json.dumps(result)
        assert 'private-tracker' not in rendered.casefold()
        assert 'downloadUrl' not in rendered
        assert 'qBittorrent' not in rendered
        assert 'private-indexer' not in rendered

    @pytest.mark.parametrize(
        ('status', 'expected_state'),
        [('downloading', 'downloading'), ('queued', 'queued')],
    )
    def test_active_queue_maps_state_and_safe_progress_fields(
        self, status: str, expected_state: str
    ) -> None:
        queue = [
            {'bookId': 999, 'status': 'downloading', 'progress': 99},
            {
                'bookId': 20,
                'status': status,
                'progress': 42.5,
                'timeleft': '01:02:03',
                'trackedDownloadStatus': 'warning',
                'downloadId': 'must-not-be-returned',
                'outputPath': '/private/downloads/secret.epub',
            },
        ]

        result, _ = _fake_handle(20, book=_book(20), queue=queue)

        assert result['state'] == expected_state
        assert result['terminal'] is False
        assert result['suggested_check_after_seconds'] == 900
        assert result['detail']['progress'] == 42.5
        assert result['detail']['eta'] == '01:02:03'
        assert result['detail']['trackedDownloadStatus'] == 'warning'
        rendered = json.dumps(result)
        assert 'must-not-be-returned' not in rendered
        assert '/private/' not in rendered

    def test_old_failure_does_not_outrank_active_downloading_record(self) -> None:
        history = {
            'records': [
                _event(
                    20,
                    'downloadFailed',
                    when=_NOW - timedelta(hours=3),
                    data={'downloadUrl': 'https://private-tracker.invalid/old-failure'},
                )
            ]
        }
        queue = [{'bookId': 20, 'status': 'downloading', 'progress': 12}]

        result, _ = _fake_handle(20, history=history, queue=queue)

        assert result['state'] == 'downloading'
        assert result['terminal'] is False
        assert result['suggested_check_after_seconds'] == 900

    def test_old_failure_is_superseded_by_a_later_grab(self) -> None:
        history = {
            'records': [
                _event(20, 'importFailed', when=_NOW - timedelta(hours=2)),
                _event(20, 'grabbed', when=_NOW - timedelta(minutes=30)),
            ]
        }

        result, _ = _fake_handle(20, history=history)

        assert result['state'] == 'searching'
        assert result['terminal'] is False
        assert result['suggested_check_after_seconds'] == 300

    def test_current_failure_without_queue_is_terminal(self) -> None:
        history = {
            'records': [
                _event(20, 'downloadFailed', when=_NOW - timedelta(minutes=5))
            ]
        }

        result, _ = _fake_handle(20, history=history)

        assert result['state'] == 'failed'
        assert result['terminal'] is True
        assert result['suggested_check_after_seconds'] == 0

    def test_imported_state_outranks_failure(self) -> None:
        history = {
            'records': [
                _event(20, 'bookFileImported', when=_NOW - timedelta(minutes=10)),
                _event(20, 'downloadFailed', when=_NOW - timedelta(minutes=5)),
            ]
        }

        result, _ = _fake_handle(20, book=_book(20, file_count=1), history=history)

        assert result['state'] == 'imported'
        assert result['terminal'] is True
        assert result['suggested_check_after_seconds'] == 0

    @pytest.mark.parametrize(
        ('failure_date', 'grab_date', 'expected_state'),
        [
            (_NOW - timedelta(minutes=30), _NOW - timedelta(minutes=30), 'searching'),
            (None, _NOW - timedelta(minutes=30), 'searching'),
            (_NOW - timedelta(minutes=5), None, 'requested'),
            ('not-a-timestamp', _NOW - timedelta(minutes=30), 'searching'),
            (_NOW - timedelta(minutes=5), 'not-a-timestamp', 'requested'),
        ],
    )
    def test_unverifiable_failure_ordering_is_non_terminal(
        self,
        failure_date: datetime | str | None,
        grab_date: datetime | str | None,
        expected_state: str,
    ) -> None:
        failure = _event(20, 'downloadFailed')
        grab = _event(20, 'grabbed')
        if failure_date is not None:
            failure['date'] = _iso(failure_date) if isinstance(failure_date, datetime) else failure_date
        if grab_date is not None:
            grab['date'] = _iso(grab_date) if isinstance(grab_date, datetime) else grab_date

        result, _ = _fake_handle(20, history={'records': [failure, grab]})

        assert result['state'] == expected_state
        assert result['terminal'] is False

    def test_unverifiable_queue_read_does_not_guess_terminal_failure(self) -> None:
        history = {
            'records': [_event(20, 'downloadFailed', when=_NOW - timedelta(minutes=5))]
        }

        result, _ = _fake_handle(
            20,
            history=history,
            queue_error=BookshelfError(
                'upstream_error',
                'private queue response https://private-tracker.invalid/queue',
                retryable=True,
            ),
        )

        assert result == {
            'ok': False,
            'error': {
                'code': 'upstream_error',
                'message': 'Bookshelf queue status could not be verified.',
                'retryable': True,
            },
        }

    def test_unverifiable_command_read_returns_structured_upstream_error(self) -> None:
        result, _ = _fake_handle(
            20,
            commands_error=BookshelfError(
                'upstream_error',
                'private command response https://private-tracker.invalid/command',
                retryable=True,
            ),
        )

        assert result == {
            'ok': False,
            'error': {
                'code': 'upstream_error',
                'message': 'Bookshelf command status could not be verified.',
                'retryable': True,
            },
        }

    def test_recent_book_search_command_maps_searching(self) -> None:
        commands = [
            {'name': 'BookSearch', 'status': 'started', 'body': {'bookIds': [20]}},
            {'name': 'BookSearch', 'status': 'started', 'body': {'bookIds': [999]}},
        ]
        # Commands without a timestamp are intentionally not called recent.
        commands[0]['queued'] = _iso(_NOW - timedelta(minutes=10))

        result, _ = _fake_handle(20, book=_book(20), commands=commands)

        assert result['state'] == 'searching'
        assert result['terminal'] is False
        assert result['suggested_check_after_seconds'] == 300
        assert 'searching' in result['detail']['message'].casefold()

    def test_recent_grab_without_queue_maps_searching(self) -> None:
        history = {
            'records': [
                _event(20, 'grabbed', when=_NOW - timedelta(minutes=30), download_id='d20')
            ]
        }

        result, _ = _fake_handle(20, book=_book(20), history=history)

        assert result['state'] == 'searching'
        assert result['suggested_check_after_seconds'] == 300

    def test_old_search_without_activity_maps_requested_honestly(self) -> None:
        result, _ = _fake_handle(
            20,
            book=_book(20, last_search=_NOW - timedelta(hours=4)),
        )

        assert result['state'] == 'requested'
        assert result['terminal'] is False
        assert result['suggested_check_after_seconds'] == 1800
        assert 'no release was grabbed' in result['detail']['message']
        assert 'no matching releases or metadata issues' in result['detail']['message']

    def test_explicit_import_failure_maps_failed(self) -> None:
        history = {
            'records': [
                _event(
                    20,
                    'bookImportIncomplete',
                    data={'downloadUrl': 'https://private-tracker.invalid/private/failure'},
                )
            ]
        }

        result, _ = _fake_handle(20, book=_book(20), history=history)

        assert result['state'] == 'failed'
        assert result['terminal'] is True
        assert result['suggested_check_after_seconds'] == 0
        assert 'failure' in result['detail']['message']
        rendered = json.dumps(result)
        assert 'private-tracker' not in rendered.casefold()
        assert 'downloadUrl' not in rendered


class TestCheckBookRequestValidation:
    @pytest.mark.parametrize('params', [{}, {'request_id': None}])
    def test_missing_request_id_rejected_before_network(self, params: dict[str, Any]) -> None:
        with pytest.raises(ToolError, match='request_id is required'):
            check_book_request_run.handle(params)

    @pytest.mark.parametrize('request_id', [True, False, '20', 20.0, 0, -1])
    def test_request_id_must_be_positive_integer(self, request_id: Any) -> None:
        with pytest.raises(ToolError):
            check_book_request_run.handle({'request_id': request_id})

    def test_unknown_parameters_rejected_for_direct_calls(self) -> None:
        with pytest.raises(ToolError, match='unknown parameters: route'):
            check_book_request_run.handle({'request_id': 20, 'route': '/api/v1/book/20'})

    def test_book_not_found_is_a_sanitized_request_error(self) -> None:
        def not_found(path: str, *, config: dict[str, Any]) -> Any:
            raise BookshelfError(
                'not_found',
                'private response https://private-tracker.invalid/private',
                retryable=False,
            )

        with (
            patch.object(check_book_request_run, 'load_config', return_value=_CONFIG),
            patch.object(check_book_request_run, 'bookshelf_get', side_effect=not_found),
        ):
            result = check_book_request_run.handle({'request_id': 404})

        assert result == {
            'ok': False,
            'error': {
                'code': 'request_not_found',
                'message': 'Bookshelf book request was not found',
                'retryable': False,
            },
        }
        rendered = json.dumps(result)
        assert 'private-tracker' not in rendered.casefold()
        assert 'private' not in rendered
