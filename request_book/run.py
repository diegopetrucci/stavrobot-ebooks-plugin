#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///

"""Idempotently request one explicitly selected Bookshelf book.

The only caller-visible identity accepted here is the signed request token
issued by ``search_books``.  Every mutation is routed through the shared
allowlisted JSON helper; this module never constructs a caller-controlled URL
or authentication header.
"""

from __future__ import annotations

import copy
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
from candidate_token import (  # noqa: E402
    CandidateTokenError,
    validate_foreign_book_id,
    verify_request_token,
)
from runtime import (  # noqa: E402
    BookshelfError,
    ToolError,
    bookshelf_get,
    bookshelf_json_mutation,
    load_config,
    optional_bool,
    require_string,
    run_tool,
)

# Keep the familiar name available to callers/tests while retaining the
# shared helper's fixed allowlist and URL/header ownership.
bookshelf_mutation = bookshelf_json_mutation

_KNOWN_PARAMS = frozenset({'request_token', 'search_now'})
_REQUEST_CHECK_SECONDS = 300
_MAX_DISPLAY_TEXT = 200
_MAX_DURABLE_BOOK_ID = (1 << 31) - 1
_CANONICAL_DECIMAL_RE = re.compile(r'^[1-9][0-9]*$')

_GENERIC_ERROR_MESSAGES = {
    'authentication_failed': 'Bookshelf rejected the API key',
    'not_found': 'Bookshelf did not find the requested resource',
    'timeout': 'Bookshelf did not respond within the configured timeout',
    'unreachable': 'Bookshelf is not reachable; check bookshelf_url in config',
    'upstream_error': 'Bookshelf returned an upstream error',
    'invalid_response': 'Bookshelf returned an invalid response',
    'response_too_large': 'Bookshelf returned an oversized response',
}


# ---------------------------------------------------------------------------
# Bounded/sanitized local helpers
# ---------------------------------------------------------------------------


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        'ok': False,
        'error': {
            'code': code,
            'message': message,
            'retryable': retryable,
        },
    }


def _bookshelf_failure(exc: BookshelfError) -> dict[str, Any]:
    """Map any upstream error to a fixed, secret-free public message."""
    code = exc.code if exc.code in _GENERIC_ERROR_MESSAGES else 'upstream_error'
    return _failure(
        code,
        _GENERIC_ERROR_MESSAGES[code],
        retryable=bool(exc.retryable),
    )


def _clean_display_text(value: Any) -> str | None:
    """Return short printable text suitable for Telegram/tool output."""
    if not isinstance(value, str):
        return None
    # Newlines and other controls are not useful in a compact status message.
    cleaned = ''.join(character for character in value.strip() if character.isprintable())
    cleaned = cleaned[:_MAX_DISPLAY_TEXT].strip()
    return cleaned or None


def _positive_foreign_book_id(value: Any) -> int | None:
    """Accept only the canonical IDs emitted by Bookshelf resources."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _CANONICAL_DECIMAL_RE.fullmatch(value):
        try:
            parsed = int(value)
        except ValueError:
            return None
    else:
        return None

    try:
        return validate_foreign_book_id(parsed)
    except CandidateTokenError:
        return None


def _positive_durable_book_id(value: Any) -> int | None:
    """Normalize the bounded integer ID returned by the Bookshelf API."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _CANONICAL_DECIMAL_RE.fullmatch(value):
        try:
            parsed = int(value)
        except ValueError:
            return None
    else:
        return None
    if not 1 <= parsed <= _MAX_DURABLE_BOOK_ID:
        return None
    return parsed


def _author_display(book: Mapping[str, Any]) -> str | None:
    author = book.get('author')
    if isinstance(author, Mapping):
        name = _clean_display_text(author.get('authorName'))
        if name is not None:
            return name
    return _clean_display_text(book.get('authorTitle'))


def _success(
    request_id: int,
    *,
    status: str,
    title: str | None,
    author: str | None,
    note: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        'ok': True,
        'request_id': request_id,
        'status': status,
        'terminal': False,
        'suggested_check_after_seconds': _REQUEST_CHECK_SECONDS,
        'title': title,
        'author': author,
    }
    if note is not None:
        result['note'] = note
    return result


# ---------------------------------------------------------------------------
# Bookshelf resource validation and transformation
# ---------------------------------------------------------------------------


def _find_existing(
    raw_books: Any,
    expected_foreign_book_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Find one exact existing record, or return a sanitized validation error."""
    if not isinstance(raw_books, list):
        return None, _failure(
            'invalid_response',
            'Bookshelf books response was not a list',
        )

    matches: list[dict[str, Any]] = []
    for raw_book in raw_books:
        if not isinstance(raw_book, Mapping):
            continue
        if _positive_foreign_book_id(raw_book.get('foreignBookId')) != expected_foreign_book_id:
            continue
        durable_id = _positive_durable_book_id(raw_book.get('id'))
        monitored = raw_book.get('monitored')
        if durable_id is None or not isinstance(monitored, bool):
            return None, _failure(
                'invalid_response',
                'Bookshelf returned an unusable matching book record',
            )
        matches.append(
            {
                'id': durable_id,
                'monitored': monitored,
                'title': _clean_display_text(raw_book.get('title')),
                'author': _author_display(raw_book),
            }
        )

    if len(matches) > 1:
        # Mutating one of multiple exact records could leave the request
        # ambiguous, so fail closed rather than creating another duplicate.
        return None, _failure(
            'duplicate_book',
            'Bookshelf contains multiple records for this selected book',
        )
    return (matches[0] if matches else None), None


def _resolve_named(
    raw_records: Any,
    *,
    name: str,
    label: str,
    endpoint: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw_records, list):
        return None, _failure(
            'invalid_response',
            f'Bookshelf {label} response was not a list',
        )

    matches = [
        record
        for record in raw_records
        if isinstance(record, Mapping) and record.get('name') == name
    ]
    if len(matches) == 0:
        return None, _failure(
            'configuration_error',
            f"No {label} named '{name}' found in Bookshelf",
        )
    if len(matches) > 1:
        return None, _failure(
            'configuration_error',
            f"Found {len(matches)} {label}s named '{name}'; expected exactly 1",
        )

    record = matches[0]
    if label == 'root folder':
        path = record.get('path')
        if not isinstance(path, str) or not path.strip():
            return None, _failure(
                'invalid_response',
                f'Bookshelf {endpoint} returned a root folder without a path',
            )
        return {'path': path}, None

    profile_id = _positive_durable_book_id(record.get('id'))
    if profile_id is None:
        return None, _failure(
            'invalid_response',
            f'Bookshelf {endpoint} returned a profile without a valid id',
        )
    return {'id': profile_id}, None


def _find_fresh_nested_book(
    raw_results: Any,
    expected_foreign_book_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Require one exact outer/nested SearchResource match."""
    if not isinstance(raw_results, list):
        return None, _failure(
            'invalid_response',
            'Bookshelf search response was not a list',
        )

    matches: list[dict[str, Any]] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, Mapping):
            continue
        nested_book = raw_result.get('book')
        if not isinstance(nested_book, Mapping):
            continue
        outer_id = _positive_foreign_book_id(raw_result.get('foreignId'))
        nested_id = _positive_foreign_book_id(nested_book.get('foreignBookId'))
        if outer_id != expected_foreign_book_id or nested_id != expected_foreign_book_id:
            continue
        try:
            matches.append(copy.deepcopy(dict(nested_book)))
        except (TypeError, ValueError):
            # A real HTTP JSON response is deepcopy-safe; treat a test/fake
            # supplying an exotic object as an invalid upstream shape.
            return None, _failure(
                'invalid_response',
                'Bookshelf search returned an unusable nested book record',
            )

    if not matches:
        return None, _failure(
            'candidate_not_found',
            'The selected book was not returned by the verified Bookshelf search',
        )
    if len(matches) > 1:
        return None, _failure(
            'candidate_ambiguous',
            'Bookshelf returned multiple exact records for the selected book',
        )
    return matches[0], None


def _transform_for_add(
    nested_book: dict[str, Any],
    *,
    root_folder_path: str,
    quality_profile_id: int,
    metadata_profile_id: int,
    search_now: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Apply the source-verified frontend getNewBook transformation."""
    title = nested_book.get('title')
    author = nested_book.get('author')
    editions = nested_book.get('editions')
    if not isinstance(title, str) or not title.strip():
        return None, _failure(
            'invalid_response',
            'Bookshelf search returned a book without a title',
        )
    if not isinstance(author, Mapping):
        return None, _failure(
            'invalid_response',
            'Bookshelf search returned a book without an author resource',
        )
    if not isinstance(editions, list) or not editions:
        return None, _failure(
            'invalid_response',
            'Bookshelf search returned a book without edition resources',
        )

    # getNewBook mutates a clone of the nested SearchResource book.  Keep the
    # fresh resource's complete edition/metadata fields and change only the
    # fields the verified UI changes.
    payload = copy.deepcopy(nested_book)
    payload_author = payload['author']
    payload['addOptions'] = {'searchForNewBook': search_now}
    payload['monitored'] = True

    # The UI configures an author only when the nested author is not already in
    # Bookshelf.  For a new author its selected settings are overridden and
    # only the requested foreign book is monitored.
    author_is_existing = 'id' in payload_author and payload_author.get('id') != 0
    if not author_is_existing:
        # BookMonitoredService.SetBookMonitoredStatus gives a non-empty
        # BooksToMonitor precedence over Monitor; mirror the UI by omitting
        # monitor and selecting exactly this book.
        payload_author['addOptions'] = {
            'searchForMissingBooks': False,
            'booksToMonitor': [payload.get('foreignBookId')],
        }
        payload_author['monitored'] = True
        payload_author['monitorNewItems'] = 'none'
        payload_author['qualityProfileId'] = quality_profile_id
        payload_author['metadataProfileId'] = metadata_profile_id
        payload_author['rootFolderPath'] = root_folder_path
        payload_author['tags'] = []

    return payload, None


def _response_book_id(
    response: Any,
    expected_foreign_book_id: int,
) -> int | None:
    """Trust a created ID only when its full resource proves exact identity."""
    if not isinstance(response, Mapping):
        return None
    if (
        _positive_foreign_book_id(response.get('foreignBookId'))
        != expected_foreign_book_id
    ):
        return None
    return _positive_durable_book_id(response.get('id'))


def _title_author_from_existing(existing: Mapping[str, Any]) -> tuple[str | None, str | None]:
    return (
        _clean_display_text(existing.get('title')),
        _clean_display_text(existing.get('author')),
    )


# ---------------------------------------------------------------------------
# Mutation convergence
# ---------------------------------------------------------------------------


def _get_existing_for_convergence(
    *,
    config: dict[str, Any],
    expected_foreign_book_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, BookshelfError | None]:
    try:
        raw_books = bookshelf_get('/api/v1/book', config=config)
    except BookshelfError as exc:
        return None, None, exc
    existing, validation_error = _find_existing(raw_books, expected_foreign_book_id)
    return existing, validation_error, None


def _converge_after_error(
    exc: BookshelfError,
    *,
    config: dict[str, Any],
    expected_foreign_book_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Re-read the exact foreign ID after an uncertain mutation outcome."""
    existing, validation_error, read_error = _get_existing_for_convergence(
        config=config,
        expected_foreign_book_id=expected_foreign_book_id,
    )
    if read_error is not None:
        # The original mutation error is the useful bounded result; do not
        # expose a second upstream message or pretend convergence was proven.
        return None, _bookshelf_failure(exc)
    if validation_error is not None:
        return None, validation_error
    if existing is not None and existing['monitored']:
        title, author = _title_author_from_existing(existing)
        return existing, _success(
            existing['id'],
            status='requested',
            title=title,
            author=author,
            note=(
                'Bookshelf accepted the request before the response completed; '
                'the exact book record was found on recheck.'
            ),
        )
    # A failed monitor mutation that did not converge must remain an error;
    # retrying the tool will perform the same exact-ID check before mutation.
    return None, _bookshelf_failure(exc)


def _start_bounded_book_search(
    *,
    config: dict[str, Any],
    book_id: int,
) -> bool:
    """Start only the source-verified BookSearch command; failure is best effort."""
    try:
        bookshelf_mutation(
            'POST',
            '/api/v1/command',
            {'name': 'BookSearch', 'bookIds': [book_id]},
            config=config,
        )
    except BookshelfError:
        return False
    return True


def _handle_existing(
    existing: dict[str, Any],
    *,
    config: dict[str, Any],
    expected_foreign_book_id: int,
    search_now: bool,
) -> dict[str, Any]:
    title, author = _title_author_from_existing(existing)
    if existing['monitored']:
        # An already-monitored exact match is intentionally mutation-free.
        return _success(
            existing['id'],
            status='requested',
            title=title,
            author=author,
        )

    try:
        bookshelf_mutation(
            'PUT',
            '/api/v1/book/monitor',
            {'bookIds': [existing['id']], 'monitored': True},
            config=config,
        )
    except BookshelfError as exc:
        _, result = _converge_after_error(
            exc,
            config=config,
            expected_foreign_book_id=expected_foreign_book_id,
        )
        if result is not None:
            return result
        return _bookshelf_failure(exc)

    if search_now and _start_bounded_book_search(config=config, book_id=existing['id']):
        return _success(
            existing['id'],
            status='searching',
            title=title,
            author=author,
        )
    if search_now:
        return _success(
            existing['id'],
            status='requested',
            title=title,
            author=author,
            note=(
                'The book is monitored, but Bookshelf could not start an immediate '
                'search; its next scheduled search can continue the request.'
            ),
        )
    return _success(
        existing['id'],
        status='requested',
        title=title,
        author=author,
    )


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


def _validated_params(params: Any) -> tuple[str, bool]:
    if not isinstance(params, dict):
        raise ToolError('parameters must be an object')
    unknown = set(params) - _KNOWN_PARAMS
    if unknown:
        raise ToolError(f"unknown parameters: {', '.join(sorted(unknown))}")
    request_token = require_string(params, 'request_token')
    search_now = optional_bool(params, 'search_now')
    return request_token, True if search_now is None else search_now


def handle(params: dict[str, Any]) -> dict[str, Any]:
    request_token, search_now = _validated_params(params)
    config = load_config()

    # This is deliberately before every Bookshelf GET and mutation.  A
    # malformed, tampered, wrong-key, or expired token has zero API calls.
    try:
        claims = verify_request_token(
            request_token,
            config['bookshelf_api_key'],
        )
    except CandidateTokenError:
        return _failure(
            'invalid_request_token',
            'request_token is invalid or expired',
        )

    expected_foreign_book_id = claims['foreignBookId']

    try:
        raw_books = bookshelf_get('/api/v1/book', config=config)
    except BookshelfError as exc:
        return _bookshelf_failure(exc)

    existing, validation_error = _find_existing(
        raw_books,
        expected_foreign_book_id,
    )
    if validation_error is not None:
        return validation_error
    if existing is not None:
        return _handle_existing(
            existing,
            config=config,
            expected_foreign_book_id=expected_foreign_book_id,
            search_now=search_now,
        )

    # Resolve every configured name from the live API; IDs and paths are never
    # guessed from config or hard-coded defaults in the request body.
    try:
        raw_roots = bookshelf_get('/api/v1/rootfolder', config=config)
        root, resolution_error = _resolve_named(
            raw_roots,
            name=config.get('root_folder_name', 'Bookshelf Sandbox'),
            label='root folder',
            endpoint='/api/v1/rootfolder',
        )
        if resolution_error is not None:
            return resolution_error

        raw_quality_profiles = bookshelf_get('/api/v1/qualityprofile', config=config)
        quality_profile, resolution_error = _resolve_named(
            raw_quality_profiles,
            name=config.get('quality_profile_name', 'eBook'),
            label='quality profile',
            endpoint='/api/v1/qualityprofile',
        )
        if resolution_error is not None:
            return resolution_error

        raw_metadata_profiles = bookshelf_get('/api/v1/metadataprofile', config=config)
        metadata_profile, resolution_error = _resolve_named(
            raw_metadata_profiles,
            name=config.get('metadata_profile_name', 'Standard'),
            label='metadata profile',
            endpoint='/api/v1/metadataprofile',
        )
        if resolution_error is not None:
            return resolution_error
    except BookshelfError as exc:
        return _bookshelf_failure(exc)

    # Match search_books' query encoding, including escaping slashes and any
    # other token-bound characters that must remain data rather than URL syntax.
    search_path = '/api/v1/search?term=' + quote(claims['term'], safe='')
    try:
        raw_results = bookshelf_get(search_path, config=config)
    except BookshelfError as exc:
        return _bookshelf_failure(exc)

    nested_book, candidate_error = _find_fresh_nested_book(
        raw_results,
        expected_foreign_book_id,
    )
    if candidate_error is not None:
        return candidate_error
    assert nested_book is not None
    assert root is not None and quality_profile is not None and metadata_profile is not None

    post_resource, transform_error = _transform_for_add(
        nested_book,
        root_folder_path=root['path'],
        quality_profile_id=quality_profile['id'],
        metadata_profile_id=metadata_profile['id'],
        search_now=search_now,
    )
    if transform_error is not None:
        return transform_error
    assert post_resource is not None

    title = _clean_display_text(nested_book.get('title'))
    author = _author_display(nested_book)
    try:
        response = bookshelf_mutation(
            'POST',
            '/api/v1/book',
            post_resource,
            config=config,
        )
    except BookshelfError as exc:
        _, result = _converge_after_error(
            exc,
            config=config,
            expected_foreign_book_id=expected_foreign_book_id,
        )
        if result is not None:
            return result
        return _bookshelf_failure(exc)

    request_id = _response_book_id(response, expected_foreign_book_id)
    if request_id is None:
        # A compatible Bookshelf response should be the created BookResource,
        # but a bounded recheck also handles an empty/legacy 201 response.
        existing, validation_error, read_error = _get_existing_for_convergence(
            config=config,
            expected_foreign_book_id=expected_foreign_book_id,
        )
        if read_error is not None:
            return _bookshelf_failure(read_error)
        if validation_error is not None:
            return validation_error
        if existing is None:
            return _failure(
                'invalid_response',
                'Bookshelf add response did not identify the requested book',
            )
        request_id = existing['id']

    return _success(
        request_id,
        status='searching' if search_now else 'requested',
        title=title,
        author=author,
    )


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
