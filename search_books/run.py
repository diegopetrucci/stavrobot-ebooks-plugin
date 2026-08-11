#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///

"""Read-only Bookshelf metadata search for explicitly selected ebook requests."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
from candidate_token import (  # noqa: E402
    CandidateTokenError,
    issue_candidate_token,
    validate_foreign_book_id,
)
from runtime import (  # noqa: E402
    BookshelfError,
    ToolError,
    bookshelf_get,
    load_config,
    optional_int,
    require_string,
    run_tool,
)

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 10
_MAX_QUERY_LENGTH = 200
_MAX_DISPLAY_TEXT = 200
_KNOWN_PARAMS = frozenset({'query', 'limit'})
_EMPTY_RESULTS_NOTE = (
    'Bookshelf returned no candidates. Metadata search may be temporarily unavailable; '
    'try again in a few minutes.'
)


def _clean_text(value: Any) -> str | None:
    """Return a non-empty trimmed string, without coercing arbitrary values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _clean_display_text(value: Any) -> str | None:
    """Return short printable text suitable for Telegram/tool output."""
    if not isinstance(value, str):
        return None
    cleaned = ''.join(character for character in value.strip() if character.isprintable())
    cleaned = cleaned[:_MAX_DISPLAY_TEXT].strip()
    return cleaned or None


def _positive_foreign_book_id(value: Any) -> int | None:
    """Normalize Bookshelf's numeric ID representation to an exact integer.

    BookResource serializes ``foreignBookId`` as a decimal string, while
    callers and token claims use a positive integer.  Accept only canonical
    decimal strings (no signs, whitespace, decimal points, or leading zeroes)
    and actual positive integers; never coerce arbitrary values.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value):
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


def _year_from_release_date(value: Any) -> int | None:
    """Extract a four-digit release year from a supported releaseDate shape."""
    if not isinstance(value, str):
        return None
    match = re.match(r'^(\d{4})(?:-|$)', value.strip())
    return int(match.group(1)) if match else None


def _year_from_book(book: Mapping[str, Any]) -> int | None:
    """Read a book year without exposing edition objects.

    Enriched search resources normally put the date on the nested book, but
    compatible Bookshelf builds may expose it only on one of the nested
    editions.  The first usable date is sufficient for the compact display
    schema.
    """
    for key in ('releaseDate', 'release_date'):
        year = _year_from_release_date(book.get(key))
        if year is not None:
            return year

    editions = book.get('editions')
    if not isinstance(editions, list):
        return None
    for edition in editions:
        if not isinstance(edition, Mapping):
            continue
        for key in ('releaseDate', 'release_date'):
            year = _year_from_release_date(edition.get(key))
            if year is not None:
                return year
    return None


def _without_trailing_title(author_title: str, title: str) -> str:
    """Remove only a complete, whitespace-delimited trailing title.

    Some compatible resources concatenate author and title in
    ``authorTitle``. Matching the full title avoids guessing from individual
    title words.
    """
    match = re.search(re.escape(title) + r'$', author_title, flags=re.IGNORECASE)
    if match is None:
        return author_title
    if match.start() == 0:
        return ''
    if not author_title[match.start() - 1].isspace():
        return author_title
    return author_title[:match.start()].rstrip()


def _author_from_book(book: Mapping[str, Any], title: str) -> str | None:
    """Normalize the known Bookshelf nested-book author representations."""
    author = book.get('author')
    if isinstance(author, Mapping):
        author_name = _clean_display_text(author.get('authorName'))
        if author_name is not None:
            return author_name

    author_title = _clean_text(book.get('authorTitle'))
    if author_title is None:
        return None

    remaining = _without_trailing_title(author_title, title).strip()
    if not remaining:
        return None

    # Reorder only a single unambiguous "last, first" pair. Names with
    # suffixes or multiple commas are retained exactly as supplied.
    if remaining.count(',') != 1:
        return _clean_display_text(remaining)
    last, first = (part.strip() for part in remaining.split(',', 1))
    if not last or not first:
        return _clean_display_text(remaining)
    return _clean_display_text(f'{first} {last}')


def _normalize_candidate(
    record: Any,
    *,
    lookup_term: str,
    api_key: str,
) -> dict[str, Any] | None:
    """Normalize one enriched SearchResource nested book.

    ``/api/v1/search`` also returns author-only resources.  A candidate is
    selectable only when the record contains a nested book with a title and an
    exact positive-integer ``foreignBookId``.  No part of the nested resource
    is retained beyond the bounded public candidate fields below.
    """
    if not isinstance(record, Mapping):
        return None
    book = record.get('book')
    if not isinstance(book, Mapping):
        return None

    outer_foreign_id = _positive_foreign_book_id(record.get('foreignId'))
    foreign_book_id = _positive_foreign_book_id(book.get('foreignBookId'))
    raw_title = _clean_text(book.get('title'))
    title = _clean_display_text(raw_title)
    if (
        outer_foreign_id is None
        or foreign_book_id is None
        or outer_foreign_id != foreign_book_id
        or raw_title is None
        or title is None
    ):
        return None

    candidate: dict[str, Any] = {
        # This is display/debug identity only. request_book must verify the
        # signed request_token rather than trusting candidate_id.
        'candidate_id': str(foreign_book_id),
        'request_token': issue_candidate_token(
            api_key,
            lookup_term,
            foreign_book_id,
        ),
        'title': title,
        'author': _author_from_book(book, raw_title),
        'year': _year_from_book(book),
    }

    disambiguation = _clean_display_text(book.get('disambiguation'))
    if disambiguation is not None:
        candidate['disambiguation'] = disambiguation

    series_title = _clean_display_text(book.get('seriesTitle'))
    if series_title is not None:
        candidate['series_title'] = series_title

    return candidate


def _is_valid_author_only_record(record: Any) -> bool:
    """Return whether one result is a structurally valid author-only resource."""
    if not isinstance(record, Mapping) or record.get('book') is not None:
        return False
    # Author identifiers share SearchResource's string field but are not book
    # IDs, so only book candidates apply the numeric foreign-book-ID contract.
    if _clean_text(record.get('foreignId')) is None:
        return False
    author = record.get('author')
    return isinstance(author, Mapping) and _clean_text(author.get('authorName')) is not None


def _empty_result() -> dict[str, Any]:
    return {
        'ok': True,
        'state': 'no_results_or_metadata_unavailable',
        'results': [],
        'note': _EMPTY_RESULTS_NOTE,
    }


def _validated_params(params: Any) -> tuple[str, int]:
    """Validate direct handler calls as well as runner-filtered stdin params."""
    if not isinstance(params, dict):
        raise ToolError('parameters must be an object')

    unknown = set(params) - _KNOWN_PARAMS
    if unknown:
        raise ToolError(f"unknown parameters: {', '.join(sorted(unknown))}")

    query = require_string(params, 'query')
    if len(query) > _MAX_QUERY_LENGTH:
        raise ToolError(f'query must be at most {_MAX_QUERY_LENGTH} characters')

    limit = optional_int(params, 'limit')
    if limit is None:
        return query, _DEFAULT_LIMIT
    if not 1 <= limit <= _MAX_LIMIT:
        raise ToolError(f'limit must be between 1 and {_MAX_LIMIT}')
    return query, limit


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        'ok': False,
        'error': {'code': code, 'message': message, 'retryable': retryable},
    }


def handle(params: dict[str, Any]) -> dict[str, Any]:
    """Search Bookshelf without selecting, adding, or monitoring a book."""
    query, limit = _validated_params(params)

    # urlencode keeps query input confined to the term value; it cannot alter
    # the Bookshelf host, request headers, or API route.
    search_path = '/api/v1/search?' + urlencode(
        {'term': query}, quote_via=quote
    )

    config = load_config()
    try:
        raw_results = bookshelf_get(search_path, config=config)
    except BookshelfError as exc:
        return exc.as_dict()

    if not isinstance(raw_results, list):
        return _failure(
            'invalid_response',
            'Bookshelf search returned an invalid response',
        )
    if not raw_results:
        return _empty_result()

    results: list[dict[str, Any]] = []
    saw_valid_author_only = False
    for record in raw_results:
        candidate = _normalize_candidate(
            record,
            lookup_term=query,
            api_key=config['bookshelf_api_key'],
        )
        if candidate is not None:
            results.append(candidate)
        elif _is_valid_author_only_record(record):
            saw_valid_author_only = True
        if len(results) == limit:
            break

    if not results and saw_valid_author_only:
        return _empty_result()
    if not results:
        return _failure(
            'invalid_response',
            'Bookshelf search returned no usable nested book records',
        )
    return {'ok': True, 'results': results}


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
