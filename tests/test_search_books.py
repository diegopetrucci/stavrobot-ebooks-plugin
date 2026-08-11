"""Tests for search_books/run.py with no live Bookshelf calls."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from candidate_token import (
    CandidateTokenError,
    MAX_FOREIGN_BOOK_ID,
    verify_candidate_token,
)
from runtime import BookshelfError, ToolError


_RUN_PATH = Path(__file__).parent.parent / 'search_books' / 'run.py'
_SPEC = importlib.util.spec_from_file_location('search_books_run', _RUN_PATH)
assert _SPEC is not None and _SPEC.loader is not None
search_books_run = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = search_books_run
_SPEC.loader.exec_module(search_books_run)


_BASE_CONFIG: dict[str, Any] = {
    'bookshelf_url': 'http://localhost:8787',
    'bookshelf_api_key': 'REDACTED-IN-OUTPUT',
    'http_timeout_seconds': 5.0,
}


def _book(
    foreign_book_id: Any,
    title: Any,
    **fields: Any,
) -> dict[str, Any]:
    return {
        'foreignBookId': foreign_book_id,
        'title': title,
        **fields,
    }


def _record(book: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        'foreignId': str(book.get('foreignBookId'))
        if book is not None
        else 'search-resource-id'
    }
    if book is not None:
        record['book'] = book
    record.update(fields)
    return record


# Sanitized fields based on an enriched live SearchResource shape. Fields such
# as images, links, and editions must never be returned by the compact schema.
_SEARCH_FIXTURE: list[dict[str, Any]] = [
    _record(
        _book(
            379647,
            'Pride and Prejudice',
            author={'authorName': 'Jane Austen'},
            releaseDate='1813-01-28',
            disambiguation='Penguin Classics edition',
            seriesTitle='Classic novels',
            images=[{'url': 'https://images.example.invalid/cover.jpg'}],
            editions=[
                {
                    'foreignEditionId': 'edition-private',
                    'title': 'raw edition',
                    'links': [{'url': 'https://metadata.example.invalid/private'}],
                }
            ],
        )
    )
]


def _handle(params: dict[str, Any], response: Any) -> dict[str, Any]:
    with (
        patch('search_books_run.load_config', return_value=_BASE_CONFIG),
        patch('search_books_run.bookshelf_get', return_value=response),
    ):
        return search_books_run.handle(params)


class TestSearchBooksNormalization:
    def test_normalizes_enriched_search_fixture_without_raw_fields(self) -> None:
        with (
            patch('search_books_run.load_config', return_value=_BASE_CONFIG),
            patch('search_books_run.bookshelf_get', return_value=_SEARCH_FIXTURE) as get,
        ):
            result = search_books_run.handle({'query': '  Pride and Prejudice  '})

        assert result['ok'] is True
        candidate = result['results'][0]
        assert candidate['candidate_id'] == '379647'
        assert candidate['title'] == 'Pride and Prejudice'
        assert candidate['author'] == 'Jane Austen'
        assert candidate['year'] == 1813
        assert candidate['disambiguation'] == 'Penguin Classics edition'
        assert candidate['series_title'] == 'Classic novels'
        assert isinstance(candidate['request_token'], str)
        assert verify_candidate_token(
            candidate['request_token'], _BASE_CONFIG['bookshelf_api_key']
        )['foreignBookId'] == 379647
        get.assert_called_once_with(
            '/api/v1/search?term=Pride%20and%20Prejudice', config=_BASE_CONFIG
        )

        rendered = json.dumps(result)
        assert 'REDACTED-IN-OUTPUT' not in rendered
        assert 'images.example.invalid' not in rendered
        assert 'metadata.example.invalid' not in rendered
        assert 'edition-private' not in rendered
        assert 'raw edition' not in rendered

    def test_public_display_fields_are_printable_bounded_and_token_term_is_unchanged(
        self,
    ) -> None:
        query = 'Book / edition?x=1&note=%0A'
        record = _record(
            _book(
                11,
                f" \x00{'T' * 250}\n ",
                author={'authorName': f"\t{'A' * 250}\x7f"},
                disambiguation=f"\n{'D' * 250}\x01",
                seriesTitle=f"\r{'S' * 250}\v",
            )
        )
        with (
            patch('search_books_run.load_config', return_value=_BASE_CONFIG),
            patch('search_books_run.bookshelf_get', return_value=[record]) as get,
        ):
            result = search_books_run.handle({'query': f'  {query}  '})

        candidate = result['results'][0]
        expected_display = {
            'title': 'T' * 200,
            'author': 'A' * 200,
            'disambiguation': 'D' * 200,
            'series_title': 'S' * 200,
        }
        assert {
            field: candidate[field]
            for field in expected_display
        } == expected_display
        assert all(
            len(candidate[field]) == search_books_run._MAX_DISPLAY_TEXT
            and all(character.isprintable() for character in candidate[field])
            for field in expected_display
        )
        claims = verify_candidate_token(
            candidate['request_token'],
            _BASE_CONFIG['bookshelf_api_key'],
        )
        assert claims['term'] == query
        assert claims['foreignBookId'] == 11
        get.assert_called_once_with(
            '/api/v1/search?term=Book%20%2F%20edition%3Fx%3D1%26note%3D%250A',
            config=_BASE_CONFIG,
        )

    def test_author_object_name_is_preferred_over_author_title(self) -> None:
        record = _record(
            _book(
                1,
                'A Title',
                author={'authorName': 'Preferred Author'},
                authorTitle='Other, Person A Title',
            )
        )

        result = _handle({'query': 'A Title'}, [record])

        assert result['results'][0]['author'] == 'Preferred Author'

    def test_author_title_strips_exact_case_insensitive_title_then_reorders_pair(self) -> None:
        record = _record(
            _book(
                2,
                'The Long Title',
                authorTitle='Doe, Jane THE LONG TITLE',
            )
        )

        result = _handle({'query': 'The Long Title'}, [record])

        assert result['results'][0]['author'] == 'Jane Doe'

    def test_author_title_does_not_subtract_only_some_title_words(self) -> None:
        record = _record(
            _book(
                3,
                'The Long Title',
                authorTitle='Jane Long Title',
            )
        )

        result = _handle({'query': 'The Long Title'}, [record])

        assert result['results'][0]['author'] == 'Jane Long Title'

    def test_author_title_with_multiple_commas_is_retained_verbatim_after_title_removal(self) -> None:
        record = _record(
            _book(
                4,
                'A Title',
                authorTitle='Doe, Jane, Jr. A Title',
            )
        )

        result = _handle({'query': 'A Title'}, [record])

        assert result['results'][0]['author'] == 'Doe, Jane, Jr.'

    def test_author_is_null_when_no_author_value_remains(self) -> None:
        record = _record(_book(5, 'A Title', authorTitle='A Title'))

        result = _handle({'query': 'A Title'}, [record])

        assert result['results'][0]['author'] is None

    def test_optional_fields_and_invalid_release_date_are_conservatively_normalized(self) -> None:
        record = _record(
            _book(
                6,
                'No Extras',
                authorTitle='Author Name',
                releaseDate='not a date',
                disambiguation='   ',
                seriesTitle=None,
                editions=[{'releaseDate': 'also not a date'}],
            )
        )

        result = _handle({'query': 'No Extras'}, [record])

        candidate = result['results'][0]
        assert candidate['candidate_id'] == '6'
        assert candidate['title'] == 'No Extras'
        assert candidate['author'] == 'Author Name'
        assert candidate['year'] is None
        assert set(candidate) == {
            'candidate_id',
            'request_token',
            'title',
            'author',
            'year',
        }

    def test_year_can_be_read_from_nested_edition_without_returning_edition(self) -> None:
        record = _record(
            _book(
                7,
                'Edition Date',
                author={'authorName': 'Author'},
                editions=[{'releaseDate': '2001-05-02', 'isbn13': 'private'}],
            )
        )

        result = _handle({'query': 'Edition Date'}, [record])

        assert result['results'][0]['year'] == 2001
        assert 'isbn13' not in json.dumps(result)

    def test_author_only_and_malformed_nested_records_are_filtered(self) -> None:
        records = [
            {'foreignId': 'author-only', 'author': {'authorName': 'Only Author'}},
            _record(_book('379647x', 'Malformed ID')),
            _record(_book('0379647', 'Leading zero ID')),
            _record(_book(' 379647', 'Whitespace ID')),
            _record(_book(0, 'Zero ID')),
            _record(_book(True, 'Boolean ID')),
            _record(_book(8, None)),
            _record(_book(9, 'Usable')),
        ]

        result = _handle({'query': 'books'}, records)

        assert result['ok'] is True
        assert [candidate['candidate_id'] for candidate in result['results']] == ['9']

    def test_decimal_string_foreign_id_from_book_resource_is_normalized(self) -> None:
        result = _handle(
            {'query': 'book'},
            [_record(_book('379647', 'String ID'))],
        )

        assert result['results'][0]['candidate_id'] == '379647'
        assert verify_candidate_token(
            result['results'][0]['request_token'], _BASE_CONFIG['bookshelf_api_key']
        )['foreignBookId'] == 379647

    def test_missing_outer_foreign_id_is_filtered_before_token_issuance(self) -> None:
        with patch('search_books_run.issue_candidate_token') as issue:
            result = _handle(
                {'query': 'book'},
                [{'book': _book('9', 'Missing outer ID')}],
            )

        assert result['error']['code'] == 'invalid_response'
        issue.assert_not_called()

    @pytest.mark.parametrize(
        'outer_foreign_id',
        [None, '', 'not-an-id', '09', '9 ', True, 9.0],
    )
    def test_malformed_outer_foreign_id_is_filtered_before_token_issuance(
        self,
        outer_foreign_id: Any,
    ) -> None:
        with patch('search_books_run.issue_candidate_token') as issue:
            result = _handle(
                {'query': 'book'},
                [_record(_book('9', 'Malformed outer ID'), foreignId=outer_foreign_id)],
            )

        assert result['error']['code'] == 'invalid_response'
        issue.assert_not_called()

    def test_mismatched_outer_and_nested_foreign_ids_are_filtered_before_token_issuance(
        self,
    ) -> None:
        with patch('search_books_run.issue_candidate_token') as issue:
            result = _handle(
                {'query': 'book'},
                [_record(_book('9', 'Mismatched ID'), foreignId='10')],
            )

        assert result['error']['code'] == 'invalid_response'
        issue.assert_not_called()

    def test_foreign_book_id_above_shared_bound_is_filtered(self) -> None:
        oversized = str(MAX_FOREIGN_BOOK_ID + 1)
        with patch('search_books_run.issue_candidate_token') as issue:
            result = _handle(
                {'query': 'book'},
                [_record(_book(oversized, 'Oversized ID'), foreignId=oversized)],
            )

        assert result['error']['code'] == 'invalid_response'
        issue.assert_not_called()

    def test_maximum_foreign_book_id_is_selectable_and_token_round_trips(self) -> None:
        maximum = str(MAX_FOREIGN_BOOK_ID)
        result = _handle(
            {'query': 'book'},
            [_record(_book(maximum, 'Maximum ID'), foreignId=maximum)],
        )

        candidate = result['results'][0]
        assert candidate['candidate_id'] == maximum
        assert verify_candidate_token(
            candidate['request_token'], _BASE_CONFIG['bookshelf_api_key']
        )['foreignBookId'] == MAX_FOREIGN_BOOK_ID

    def test_limit_is_applied_after_malformed_records_are_skipped(self) -> None:
        records = [
            {'foreignId': 'author-only'},
            _record(_book('work-7', 'Malformed ID')),
            _record(_book(7, 'First')),
            _record(_book(8, 'Second')),
        ]

        result = _handle({'query': 'books', 'limit': 1}, records)

        assert [candidate['candidate_id'] for candidate in result['results']] == ['7']


class TestSearchBooksParameters:
    @pytest.mark.parametrize('limit', [0, -1, 11])
    def test_limit_bounds_are_rejected(self, limit: int) -> None:
        with pytest.raises(ToolError, match='limit must be between 1 and 10'):
            search_books_run.handle({'query': 'book', 'limit': limit})

    @pytest.mark.parametrize('limit', [True, '5', 1.5])
    def test_limit_must_be_an_integer(self, limit: Any) -> None:
        with pytest.raises(ToolError, match='limit must be an integer'):
            search_books_run.handle({'query': 'book', 'limit': limit})

    def test_default_limit_is_five(self) -> None:
        records = [
            _record(_book(index, f'Book {index}'))
            for index in range(1, 7)
        ]

        result = _handle({'query': 'book'}, records)

        assert len(result['results']) == 5
        assert [candidate['candidate_id'] for candidate in result['results']] == [
            '1',
            '2',
            '3',
            '4',
            '5',
        ]

    def test_query_is_trimmed_and_bounded_after_trimming(self) -> None:
        with pytest.raises(ToolError, match='at most 200 characters'):
            search_books_run.handle({'query': f"  {'x' * 201}  "})

    @pytest.mark.parametrize('query', ['', '   ', 42])
    def test_query_must_be_a_nonempty_string(self, query: Any) -> None:
        with pytest.raises(ToolError, match='query is required'):
            search_books_run.handle({'query': query})

    def test_unknown_parameters_are_rejected(self) -> None:
        with pytest.raises(ToolError, match='unknown parameters: route'):
            search_books_run.handle({'query': 'book', 'route': '/api/v1/book'})

    def test_user_query_is_percent_encoded_before_the_runtime_call(self) -> None:
        query = 'book & term=/api/v1/book?x=1'
        with (
            patch('search_books_run.load_config', return_value=_BASE_CONFIG),
            patch('search_books_run.bookshelf_get', return_value=_SEARCH_FIXTURE) as get,
        ):
            search_books_run.handle({'query': query})

        path = get.call_args.args[0]
        assert path == (
            '/api/v1/search?term=book%20%26%20term%3D%2Fapi%2Fv1%2Fbook%3Fx%3D1'
        )
        assert query not in path


class TestSearchBooksResponses:
    def test_empty_search_is_reported_as_ambiguous_with_exact_note(self) -> None:
        result = _handle({'query': 'missing title'}, [])

        assert result == {
            'ok': True,
            'state': 'no_results_or_metadata_unavailable',
            'results': [],
            'note': (
                'Bookshelf returned no candidates. Metadata search may be temporarily unavailable; '
                'try again in a few minutes.'
            ),
        }

    @pytest.mark.parametrize(
        ('error', 'expected_code', 'retryable'),
        [
            (BookshelfError('authentication_failed', 'bad API key', retryable=False), 'authentication_failed', False),
            (BookshelfError('timeout', 'timed out', retryable=True), 'timeout', True),
            (BookshelfError('upstream_error', 'HTTP 500', retryable=True), 'upstream_error', True),
        ],
    )
    def test_runtime_errors_are_returned_as_sanitized_error_codes(
        self,
        error: BookshelfError,
        expected_code: str,
        retryable: bool,
    ) -> None:
        with (
            patch('search_books_run.load_config', return_value=_BASE_CONFIG),
            patch('search_books_run.bookshelf_get', side_effect=error),
        ):
            result = search_books_run.handle({'query': 'book'})

        assert result['ok'] is False
        assert result['error']['code'] == expected_code
        assert result['error']['retryable'] is retryable
        assert 'REDACTED-IN-OUTPUT' not in json.dumps(result)

    def test_non_list_response_is_sanitized(self) -> None:
        result = _handle({'query': 'book'}, {'unexpected': 'raw payload'})

        assert result == {
            'ok': False,
            'error': {
                'code': 'invalid_response',
                'message': 'Bookshelf search returned an invalid response',
                'retryable': False,
            },
        }

    def test_valid_author_only_results_preserve_ambiguous_empty_state(self) -> None:
        result = _handle(
            {'query': 'Jane Austen'},
            [
                {
                    'foreignId': 'author-1265',
                    'author': {
                        'foreignAuthorId': 'author-1265',
                        'authorName': 'Jane Austen',
                    },
                    'book': None,
                }
            ],
        )

        assert result == {
            'ok': True,
            'state': 'no_results_or_metadata_unavailable',
            'results': [],
            'note': (
                'Bookshelf returned no candidates. Metadata search may be temporarily unavailable; '
                'try again in a few minutes.'
            ),
        }

    def test_nonempty_malformed_only_response_remains_invalid(self) -> None:
        result = _handle(
            {'query': 'book'},
            [
                None,
                {},
                {'foreignId': 'not-an-id', 'author': {}},
                _record(_book('9', 'Mismatched'), foreignId='10'),
            ],
        )

        assert result['ok'] is False
        assert result['error']['code'] == 'invalid_response'

    def test_token_wrong_key_is_not_accepted(self) -> None:
        result = _handle({'query': 'book'}, [_record(_book(10, 'Book'))])
        token = result['results'][0]['request_token']

        with pytest.raises(CandidateTokenError):
            verify_candidate_token(token, 'different-key')
