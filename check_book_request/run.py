#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///

"""Read-only status inspection for a Bookshelf ebook request.

Bookshelf is the source of terminal state for this v1 ebook-only tool.  The
implementation deliberately keeps only a small, normalized subset of each
upstream response in memory.  In particular, history ``data`` is inspected for
correlation but is never copied into a result or an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
from runtime import (  # noqa: E402
    BookshelfError,
    ToolError,
    bookshelf_get,
    load_config,
    run_tool,
)

_REQUEST_PARAMS = frozenset({'request_id'})
_PAGE_SIZE = 100
_STALE_GRAB_AFTER = timedelta(hours=2)
_MAX_PUBLIC_TEXT = 96
_MAX_SIZE_BYTES = 10 * 1024 * 1024 * 1024 * 1024  # 10 TiB

# These are the names emitted by the current Readarr/Bookshelf history enum,
# plus names used by compatible builds.  Matching is case-insensitive and
# punctuation-insensitive, but never based on a title or author.
_IMPORTED_EVENT = 'bookfileimported'
_GRABBED_EVENT = 'grabbed'
_FAILURE_EVENTS = frozenset(
    {
        'downloadfailed',
        'importfailed',
        'bookfileimportfailed',
        'bookimportfailed',
        'bookimportincomplete',
        'downloadimportfailed',
    }
)
_EVENT_TYPE_BY_NUMBER = {
    1: _GRABBED_EVENT,
    3: _IMPORTED_EVENT,
    4: 'downloadfailed',
    7: 'bookimportincomplete',
}
_ACTIVE_QUEUE_STATUSES = frozenset(
    {
        'queued',
        'pending',
        'qualifying',
        'waiting',
        'downloading',
        'continuing',
        'importing',
        'paused',
        'completed',
        'stalled',
    }
)
_INACTIVE_QUEUE_STATUSES = frozenset(
    {'failed', 'removed', 'cancelled', 'canceled', 'ignored', 'completedfailed'}
)
_QUEUED_QUEUE_STATUSES = frozenset({'queued', 'pending', 'qualifying', 'waiting', 'paused'})
_ACTIVE_COMMAND_STATUSES = frozenset(
    {'queued', 'started', 'running', 'pending', 'inprogress', 'processing'}
)

_MISSING = object()


@dataclass(frozen=True)
class _HistoryEvent:
    book_id: int
    event_type: str
    download_id: str | None
    when: datetime | None
    quality: str | None


@dataclass(frozen=True)
class _QueueRecord:
    book_id: int
    state: str
    progress: float | None
    eta: str | None
    tracked_download_status: str | None


@dataclass(frozen=True)
class _SearchCommand:
    book_ids: frozenset[int]
    status: str
    when: datetime | None


@dataclass(frozen=True)
class _CallFailure:
    code: str
    retryable: bool
    message: str


# ---------------------------------------------------------------------------
# Safe result and input helpers
# ---------------------------------------------------------------------------


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    """Build an error with a fixed, bounded message."""
    return {
        'ok': False,
        'error': {'code': code, 'message': message, 'retryable': retryable},
    }


def _safe_bookshelf_error(exc: BaseException, *, not_found_code: str | None = None) -> _CallFailure:
    """Convert an upstream exception without retaining its message.

    ``BookshelfError`` messages are currently sanitized by shared/runtime.py,
    but this boundary intentionally does not depend on that implementation
    detail.  It also protects direct tests/fakes that raise a plain exception
    containing a tracker URL or another private upstream value.
    """
    if isinstance(exc, BookshelfError):
        code = exc.code
        retryable = exc.retryable
        if code == 'not_found' and not_found_code is not None:
            return _CallFailure(
                not_found_code,
                False,
                'Bookshelf book request was not found',
            )
        messages = {
            'authentication_failed': 'Bookshelf authentication failed.',
            'timeout': 'Bookshelf request timed out.',
            'unreachable': 'Bookshelf is unavailable.',
            'upstream_error': 'Bookshelf returned an upstream error.',
            'invalid_response': 'Bookshelf returned an invalid response.',
            'not_found': 'Bookshelf resource was not found.',
        }
        return _CallFailure(
            code if code in messages else 'upstream_error',
            retryable,
            messages.get(code, 'Bookshelf request failed.'),
        )
    return _CallFailure('upstream_error', True, 'Bookshelf request failed.')


def _validate_params(params: Any) -> int:
    """Validate direct handler calls as well as runner-filtered stdin input."""
    if not isinstance(params, dict):
        raise ToolError('parameters must be an object')

    unknown = set(params) - _REQUEST_PARAMS
    if unknown:
        raise ToolError(f"unknown parameters: {', '.join(sorted(unknown))}")

    if 'request_id' not in params or params['request_id'] is None:
        raise ToolError('request_id is required')
    request_id = params['request_id']
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise ToolError('request_id must be an integer')
    if request_id <= 0:
        raise ToolError('request_id must be a positive integer')
    return request_id


def _query_path(path: str, params: Mapping[str, Any]) -> str:
    """Encode every query value using the standard-library URL encoder."""
    return f'{path}?{urlencode(params)}'


def _safe_public_text(value: Any, *, max_length: int = _MAX_PUBLIC_TEXT) -> str | None:
    """Allow a small, non-URL label in a result.

    We do not expose upstream titles, paths, indexer names, or exception text
    from this tool.  This helper is only used for quality/status/ETA values
    that have a deliberately tiny public schema.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_length or any(ord(c) < 32 for c in value):
        return None
    lowered = value.casefold()
    if (
        '://' in lowered
        or lowered.startswith(('www.', 'magnet:'))
        or '/' in value
        or '\\' in value
        or '@' in value
        or any(
            sensitive in lowered
            for sensitive in (
                'private tracker',
                'tracker',
                'indexer',
                'downloadurl',
                'downloadid',
                'downloadclient',
                'qbittorrent',
            )
        )
    ):
        return None
    return value


def _safe_token(value: Any) -> str | None:
    """Normalize enum-like upstream labels without exposing arbitrary text."""
    text = _safe_public_text(value, max_length=48)
    if text is None:
        return None
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _+().:-]*', text):
        return None
    return text


def _as_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r'\d+', value.strip()):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except (ValueError, TypeError):
                return None
        else:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _bounded_size(value: Any) -> int | None:
    number = _as_number(value)
    if number is None or number < 0 or number > _MAX_SIZE_BYTES or not number.is_integer():
        return None
    return int(number)


def _parse_datetime(value: Any) -> datetime | None:
    """Parse the ISO/epoch date forms emitted by Bookshelf."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_recent(when: datetime | None, now: datetime) -> bool:
    if when is None:
        return False
    return now - when <= _STALE_GRAB_AFTER


# ---------------------------------------------------------------------------
# Bounded response normalization
# ---------------------------------------------------------------------------


def _records_from_payload(payload: Any) -> list[dict[str, Any]] | None:
    """Accept the known list and paging-envelope shapes, bounded to one page."""
    records: Any
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = None
        for key in ('records', 'items', 'commands', 'queue'):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                records = candidate
                break
        if records is None:
            return None
    else:
        return None
    return [record for record in records[:_PAGE_SIZE] if isinstance(record, dict)]


def _nested_data(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    data = record.get('data')
    return data if isinstance(data, Mapping) else None


def _first_value(record: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    data = _nested_data(record)
    if data is not None:
        for name in names:
            if name in data and data[name] is not None:
                return data[name]
    return None


def _book_id_from_record(record: Mapping[str, Any]) -> int | None:
    for source in (record, _nested_data(record)):
        if not isinstance(source, Mapping):
            continue
        for key in ('bookId', 'book_id'):
            book_id = _as_positive_int(source.get(key))
            if book_id is not None:
                return book_id
        nested_book = source.get('book')
        if isinstance(nested_book, Mapping):
            book_id = _as_positive_int(nested_book.get('id'))
            if book_id is not None:
                return book_id
    return None


def _download_id_from_record(record: Mapping[str, Any]) -> str | None:
    value = _first_value(record, 'downloadId', 'download_id')
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    if not text or len(text) > 256:
        return None
    return text


def _normalize_event_type(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _EVENT_TYPE_BY_NUMBER.get(value)
    if isinstance(value, Mapping):
        value = value.get('name') or value.get('value')
    if not isinstance(value, str):
        return None
    normalized = re.sub(r'[^a-z0-9]', '', value.casefold())
    return normalized or None


def _event_date(record: Mapping[str, Any]) -> datetime | None:
    return _parse_datetime(
        _first_value(record, 'date', 'Date', 'eventDate', 'created', 'time')
    )


def _quality_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        nested = value.get('quality')
        if nested is not None:
            result = _quality_name(nested)
            if result is not None:
                return result
        for key in ('name', 'qualityName'):
            result = _safe_token(value.get(key))
            if result is not None:
                return result
        return None
    return _safe_token(value)


def _event_quality(record: Mapping[str, Any]) -> str | None:
    return _quality_name(record.get('quality'))


def _parse_history(payload: Any, request_id: int) -> list[_HistoryEvent] | None:
    records = _records_from_payload(payload)
    if records is None:
        return None
    events: list[_HistoryEvent] = []
    for record in records:
        if _book_id_from_record(record) != request_id:
            continue
        event_type = _normalize_event_type(
            _first_value(record, 'eventType', 'EventType', 'event_type')
        )
        if event_type is None:
            continue
        events.append(
            _HistoryEvent(
                book_id=request_id,
                event_type=event_type,
                download_id=_download_id_from_record(record),
                when=_event_date(record),
                quality=_event_quality(record),
            )
        )
    return events


def _queue_progress(record: Mapping[str, Any]) -> float | None:
    value = _first_value(record, 'progress', 'progressPercent', 'percent')
    progress = _as_number(value)
    if progress is None:
        size = _as_number(record.get('size'))
        size_left = _as_number(record.get('sizeleft', record.get('sizeLeft')))
        if size is not None and size > 0 and size_left is not None:
            progress = (1.0 - size_left / size) * 100.0
    if progress is None:
        return None
    return round(max(0.0, min(100.0, progress)), 1)


def _queue_eta(record: Mapping[str, Any]) -> str | None:
    value = record.get('timeleft')
    if value is None:
        value = record.get('eta', record.get('estimatedCompletionTime'))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A numeric ETA is not useful to a caller without a documented unit.
        return None
    return _safe_public_text(value, max_length=64)


def _queue_status(record: Mapping[str, Any]) -> str | None:
    status = _safe_token(record.get('status'))
    if status is None:
        return None
    return re.sub(r'[^a-z0-9]', '', status.casefold())


def _parse_queue(payload: Any, request_id: int) -> list[_QueueRecord] | None:
    records = _records_from_payload(payload)
    if records is None:
        return None
    queue: list[_QueueRecord] = []
    for record in records:
        if _book_id_from_record(record) != request_id:
            continue
        status = _queue_status(record)
        if status is None or status in _INACTIVE_QUEUE_STATUSES:
            continue
        if status not in _ACTIVE_QUEUE_STATUSES:
            # Unknown queue labels are not enough to claim an active download.
            continue
        state = 'queued' if status in _QUEUED_QUEUE_STATUSES else 'downloading'
        queue.append(
            _QueueRecord(
                book_id=request_id,
                state=state,
                progress=_queue_progress(record),
                eta=_queue_eta(record),
                tracked_download_status=_safe_token(
                    record.get('trackedDownloadStatus', record.get('tracked_download_status'))
                ),
            )
        )
    return queue


def _command_book_ids(record: Mapping[str, Any]) -> frozenset[int]:
    body = record.get('body')
    if not isinstance(body, Mapping):
        # Some test doubles/API proxies serialize the command body as JSON.  It
        # is parsed only for book IDs; it is never retained or returned.
        if isinstance(body, str) and len(body) <= 4096:
            try:
                decoded = json.loads(body)
            except (TypeError, ValueError):
                decoded = None
            body = decoded if isinstance(decoded, Mapping) else None
    sources: list[Mapping[str, Any]] = [record]
    if isinstance(body, Mapping):
        sources.insert(0, body)
    found: set[int] = set()
    for source in sources:
        values = source.get('bookIds')
        if isinstance(values, list):
            for value in values:
                book_id = _as_positive_int(value)
                if book_id is not None:
                    found.add(book_id)
        for key in ('bookId', 'book_id'):
            book_id = _as_positive_int(source.get(key))
            if book_id is not None:
                found.add(book_id)
    return frozenset(found)


def _parse_commands(payload: Any, request_id: int) -> list[_SearchCommand] | None:
    records = _records_from_payload(payload)
    if records is None:
        return None
    commands: list[_SearchCommand] = []
    for record in records:
        name = _first_value(record, 'name', 'commandName', 'command_name')
        normalized_name = re.sub(r'[^a-z0-9]', '', name.casefold()) if isinstance(name, str) else ''
        if normalized_name != 'booksearch':
            continue
        book_ids = _command_book_ids(record)
        if request_id not in book_ids:
            continue
        status_value = record.get('status')
        status = (
            re.sub(r'[^a-z0-9]', '', status_value.casefold())
            if isinstance(status_value, str)
            else ''
        )
        if status not in _ACTIVE_COMMAND_STATUSES:
            continue
        when = _parse_datetime(record.get('started')) or _parse_datetime(record.get('queued'))
        commands.append(_SearchCommand(book_ids, status, when))
    return commands


# ---------------------------------------------------------------------------
# State details and mapping
# ---------------------------------------------------------------------------


def _book_file_count(book: Mapping[str, Any]) -> int:
    statistics = book.get('statistics')
    if not isinstance(statistics, Mapping):
        return 0
    count = _as_positive_int(statistics.get('bookFileCount'))
    return count or 0


def _book_file_details(book: Mapping[str, Any], imported: list[_HistoryEvent]) -> tuple[str | None, int | None]:
    quality: str | None = None
    size: int | None = None
    files = book.get('bookFiles')
    if isinstance(files, list):
        for file_record in files:
            if not isinstance(file_record, Mapping):
                continue
            quality = quality or _quality_name(file_record.get('quality'))
            size = size if size is not None else _bounded_size(
                file_record.get('size', file_record.get('sizeOnDisk'))
            )
            if quality is not None and size is not None:
                break
    if quality is None:
        for event in imported:
            quality = quality or event.quality
    if size is None:
        statistics = book.get('statistics')
        if isinstance(statistics, Mapping):
            size = _bounded_size(statistics.get('sizeOnDisk'))
    return quality, size


def _imported_detail(book: Mapping[str, Any], imported: list[_HistoryEvent]) -> dict[str, Any]:
    quality, size = _book_file_details(book, imported)
    return {
        'message': 'Book imported into Bookshelf.',
        'quality': quality,
        'size': size,
    }


def _queue_detail(record: _QueueRecord) -> dict[str, Any]:
    detail: dict[str, Any] = {
        'message': 'Book is queued for download.'
        if record.state == 'queued'
        else 'Book is downloading.'
    }
    if record.progress is not None:
        detail['progress'] = record.progress
    if record.eta is not None:
        detail['eta'] = record.eta
    if record.tracked_download_status is not None:
        detail['trackedDownloadStatus'] = record.tracked_download_status
    return detail


def _result(
    request_id: int,
    state: str,
    terminal: bool,
    interval: int,
    detail: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        'ok': True,
        'request_id': request_id,
        'state': state,
        'terminal': terminal,
        'suggested_check_after_seconds': interval,
        'detail': dict(detail),
    }


def _has_book_scoped_import(imported: list[_HistoryEvent]) -> bool:
    """Return whether history confirms an import for this exact book ID.

    ``_parse_history`` admits only records whose numeric ``bookId`` equals the
    requested ID.  A matching ``downloadId`` on a grab is useful supplementary
    evidence when both records are present, but a bounded/current history page
    may not contain an older grab.  That absence must not negate a confirmed
    book-scoped ``bookFileImported`` event.
    """
    return bool(imported)


def _latest_with_date(events: list[_HistoryEvent]) -> _HistoryEvent | None:
    dated = [event for event in events if event.when is not None]
    if not dated:
        return None
    return max(dated, key=lambda event: event.when or datetime.min.replace(tzinfo=timezone.utc))


def _failure_is_current(
    failures: list[_HistoryEvent], grabs: list[_HistoryEvent]
) -> bool:
    """Require provable ordering before treating a failure as terminal.

    Without a competing grab, an explicit book-scoped failure is sufficient.
    Once a grab is present, every relevant event needs a parsed timestamp and
    the newest failure must be strictly newer than the newest grab.  Equal,
    missing, or malformed dates are intentionally non-terminal: the bounded
    history page cannot establish which acquisition attempt is current.
    """
    if not failures:
        return False
    if not grabs:
        return True
    if any(event.when is None for event in (*failures, *grabs)):
        return False

    newest_failure = _latest_with_date(failures)
    newest_grab = _latest_with_date(grabs)
    if newest_failure is None or newest_grab is None:
        return False
    if newest_failure.when is None or newest_grab.when is None:
        return False
    return newest_failure.when > newest_grab.when


def _last_search_time(book: Mapping[str, Any]) -> datetime | None:
    return _parse_datetime(book.get('lastSearchTime'))


def _map_state(
    request_id: int,
    book: Mapping[str, Any],
    history: list[_HistoryEvent],
    queue: list[_QueueRecord],
    commands: list[_SearchCommand],
    *,
    queue_available: bool,
    commands_available: bool,
    now: datetime,
) -> dict[str, Any]:
    imports = [event for event in history if event.event_type == _IMPORTED_EVENT]
    grabs = [event for event in history if event.event_type == _GRABBED_EVENT]
    failures = [event for event in history if event.event_type in _FAILURE_EVENTS]

    # Precedence is intentional: a confirmed Bookshelf import wins over stale
    # failure/grab records, while an uncorroborated file count does not.
    if _book_file_count(book) >= 1 and _has_book_scoped_import(imports):
        return _result(
            request_id,
            'imported',
            True,
            0,
            _imported_detail(book, imports),
        )

    # An active queue record outranks terminal failure evidence.  Likewise, a
    # failed queue read cannot prove that no active record exists.
    active_queue = queue[0] if queue else None
    if active_queue is not None:
        return _result(
            request_id,
            active_queue.state,
            False,
            900,
            _queue_detail(active_queue),
        )

    # Absence of a queue is meaningful only when the queue call succeeded.
    if not queue_available:
        return _failure(
            'upstream_error',
            'Bookshelf queue status could not be verified.',
            retryable=True,
        )

    if _failure_is_current(failures, grabs):
        return _result(
            request_id,
            'failed',
            True,
            0,
            {
                'message': 'Bookshelf reported an acquisition failure; manual attention may be needed.'
            },
        )

    latest_grab = _latest_with_date(grabs)
    if latest_grab is not None:
        if now - (latest_grab.when or now) > _STALE_GRAB_AFTER:
            return _result(
                request_id,
                'grabbed_stalled',
                False,
                1800,
                {
                    'message': (
                        'A release was grabbed but has not reached the import queue; '
                        'manual attention may be needed.'
                    )
                },
            )
        return _result(
            request_id,
            'searching',
            False,
            300,
            {'message': 'A release was recently grabbed; waiting for download progress.'},
        )

    if commands:
        recent_command = any(_is_recent(command.when, now) for command in commands)
        if recent_command:
            return _result(
                request_id,
                'searching',
                False,
                300,
                {'message': 'Bookshelf is searching for this book.'},
            )

    # If no history or queue evidence exists, we need a successful command
    # read before safely saying that there is no active search.
    if not commands_available:
        return _failure(
            'upstream_error',
            'Bookshelf command status could not be verified.',
            retryable=True,
        )

    last_search = _last_search_time(book)
    if last_search is not None and now - last_search > _STALE_GRAB_AFTER:
        detail = {
            'message': (
                'Bookshelf searched this book previously, but no release was grabbed; '
                'there may be no matching releases or metadata issues.'
            )
        }
    else:
        detail = {'message': 'Book is monitored; no active search or download is visible.'}
    return _result(request_id, 'requested', False, 1800, detail)


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------


def _get(path: str, config: Mapping[str, Any]) -> tuple[Any, _CallFailure | None]:
    try:
        return bookshelf_get(path, config=dict(config)), None
    except Exception as exc:  # noqa: BLE001 - sanitize every fake/upstream failure
        return _MISSING, _safe_bookshelf_error(exc)


def handle(params: dict[str, Any]) -> dict[str, Any]:
    """Inspect one positive Bookshelf book ID without mutating any service."""
    request_id = _validate_params(params)
    config = load_config()

    book_payload, book_failure = _get(f'/api/v1/book/{request_id}', config)
    if book_failure is not None:
        if book_failure.code == 'not_found':
            return _failure(
                'request_not_found',
                'Bookshelf book request was not found',
            )
        return _failure(book_failure.code, book_failure.message, retryable=book_failure.retryable)
    if not isinstance(book_payload, Mapping):
        return _failure('invalid_response', 'Bookshelf book response was invalid.')
    response_id = _as_positive_int(book_payload.get('id'))
    if response_id is not None and response_id != request_id:
        return _failure('invalid_response', 'Bookshelf book response was invalid.')

    # Queue does not support a bookId filter in the Readarr API.  Fetch one
    # bounded page and filter by numeric bookId locally.  History does support
    # bookId, but is filtered locally too because some compatible builds have
    # ignored that query parameter.
    queue_path = _query_path(
        '/api/v1/queue',
        {
            'page': 1,
            'pageSize': _PAGE_SIZE,
            'sortKey': 'timeleft',
            'sortDirection': 'ascending',
            'includeUnknownAuthorItems': 'true',
        },
    )
    history_path = _query_path(
        '/api/v1/history',
        {
            'bookId': request_id,
            'page': 1,
            'pageSize': _PAGE_SIZE,
            'sortKey': 'date',
            'sortDirection': 'descending',
        },
    )

    queue_payload, queue_failure = _get(queue_path, config)
    history_payload, history_failure = _get(history_path, config)
    commands_payload, commands_failure = _get('/api/v1/command', config)

    if history_failure is not None:
        return _failure(
            history_failure.code,
            'Bookshelf history status could not be verified.',
            retryable=history_failure.retryable,
        )
    history = _parse_history(history_payload, request_id)
    if history is None:
        return _failure('invalid_response', 'Bookshelf history response was invalid.')

    queue_available = queue_failure is None
    queue = [] if queue_failure is not None else _parse_queue(queue_payload, request_id)
    if queue is None:
        queue_available = False
        queue = []

    commands_available = commands_failure is None
    commands = [] if commands_failure is not None else _parse_commands(commands_payload, request_id)
    if commands is None:
        commands_available = False
        commands = []

    return _map_state(
        request_id,
        book_payload,
        history,
        queue,
        commands,
        queue_available=queue_available,
        commands_available=commands_available,
        now=_utcnow(),
    )


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
