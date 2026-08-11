"""Contract tests for request_book using local GET/mutation fakes only."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

import runtime
from candidate_token import issue_candidate_token
from runtime import BookshelfError, ToolError


_REQUEST_BOOK_DIR = Path(__file__).parent.parent / 'request_book'
_RUN_PATH = _REQUEST_BOOK_DIR / 'run.py'
_MANIFEST_PATH = _REQUEST_BOOK_DIR / 'manifest.json'
_SPEC = importlib.util.spec_from_file_location('request_book_run', _RUN_PATH)
assert _SPEC is not None and _SPEC.loader is not None
request_book_run = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = request_book_run
_SPEC.loader.exec_module(request_book_run)


_CONFIG: dict[str, Any] = {
    'bookshelf_url': 'http://bookshelf.invalid',
    'bookshelf_api_key': 'REDACTED-IN-OUTPUT',
    'root_folder_name': 'Bookshelf Sandbox',
    'quality_profile_name': 'eBook',
    'metadata_profile_name': 'Standard',
    'http_timeout_seconds': 5.0,
}
_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
_FOREIGN_ID = 379647
_TOKEN = issue_candidate_token(
    _CONFIG['bookshelf_api_key'],
    'Pride and Prejudice',
    _FOREIGN_ID,
    _NOW,
)


def _book(
    *,
    foreign_id: Any = str(_FOREIGN_ID),
    book_id: int = 0,
    title: str = 'Pride and Prejudice',
    author_id: int = 0,
    monitored: bool = False,
) -> dict[str, Any]:
    return {
        'id': book_id,
        'foreignBookId': foreign_id,
        'foreignEditionId': 'edition-379647',
        'title': title,
        'authorTitle': 'Austen, Jane Pride and Prejudice',
        'seriesTitle': None,
        'disambiguation': 'Penguin Classics',
        'overview': 'A bounded overview that must remain in the POST fixture.',
        'titleSlug': 'pride-and-prejudice',
        'monitored': monitored,
        'anyEditionOk': False,
        'releaseDate': '1813-01-28T00:00:00Z',
        'pageCount': 432,
        'genres': ['Classics'],
        'ratings': {'value': 4.5},
        'images': [{'coverType': 'cover', 'remoteUrl': 'https://images.invalid/cover'}],
        'links': [{'url': 'https://metadata.invalid/book'}],
        'author': {
            'id': author_id,
            'authorName': 'Jane Austen',
            'foreignAuthorId': 'author-1265',
            'authorNameLastFirst': 'Austen, Jane',
            'titleSlug': 'jane-austen',
            'folder': 'Jane Austen',
            'images': [],
            'links': [],
            'tags': [],
        },
        'editions': [
            {
                'id': 0,
                'foreignEditionId': 'edition-379647',
                'title': 'Pride and Prejudice',
                'monitored': True,
                'pageCount': 432,
                'releaseDate': '1813-01-28T00:00:00Z',
                'links': [{'url': 'https://metadata.invalid/edition'}],
            }
        ],
    }


def _search_record(book: dict[str, Any], *, outer_id: Any = str(_FOREIGN_ID)) -> dict[str, Any]:
    return {'id': 1, 'foreignId': outer_id, 'book': book}


def _config_get(path: str, *, config: dict[str, Any]) -> Any:
    if path == '/api/v1/rootfolder':
        return [{'id': 1, 'name': 'Bookshelf Sandbox', 'path': '/books'}]
    if path == '/api/v1/qualityprofile':
        return [{'id': 2, 'name': 'eBook'}]
    if path == '/api/v1/metadataprofile':
        return [{'id': 3, 'name': 'Standard'}]
    if path.startswith('/api/v1/search?term='):
        return [_search_record(_book())]
    raise AssertionError(f'unexpected GET path: {path}')


def _token_at_now() -> str:
    return issue_candidate_token(
        _CONFIG['bookshelf_api_key'],
        'Pride and Prejudice',
        _FOREIGN_ID,
        _NOW,
    )


def _call(
    params: dict[str, Any],
    *,
    books: Any = None,
    mutation: Any = None,
) -> tuple[dict[str, Any], MagicMock, MagicMock]:
    get = MagicMock()
    mutation_call = MagicMock()

    def fake_get(path: str, *, config: dict[str, Any]) -> Any:
        if path == '/api/v1/book':
            return [] if books is None else books
        return _config_get(path, config=config)

    get.side_effect = fake_get
    mutation_call.return_value = (
        {'id': 99, 'foreignBookId': str(_FOREIGN_ID)}
        if mutation is None
        else mutation
    )
    with (
        patch.object(request_book_run, 'load_config', return_value=_CONFIG),
        patch.object(request_book_run, 'bookshelf_get', get),
        patch.object(request_book_run, 'bookshelf_mutation', mutation_call),
        patch.object(request_book_run, 'verify_request_token', return_value={
            'version': 1,
            'issued_at': int(_NOW.timestamp()),
            'expires_at': int(_NOW.timestamp()) + 86400,
            'term': 'Pride and Prejudice',
            'foreignBookId': _FOREIGN_ID,
        }),
    ):
        result = request_book_run.handle(params)
    return result, get, mutation_call


class TestRequestBookValidation:
    @pytest.mark.parametrize(
        'params',
        [
            {},
            {'request_token': ''},
            {'request_token': _TOKEN, 'candidate_id': str(_FOREIGN_ID)},
        ],
    )
    def test_invalid_params_fail_before_any_api_call(self, params: dict[str, Any]) -> None:
        get = MagicMock()
        mutation = MagicMock()
        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(request_book_run, 'bookshelf_get', get),
            patch.object(request_book_run, 'bookshelf_mutation', mutation),
        ):
            with pytest.raises(ToolError):
                request_book_run.handle(params)
        get.assert_not_called()
        mutation.assert_not_called()

    @pytest.mark.parametrize('value', ['true', 1, None])
    def test_search_now_must_be_boolean(self, value: Any) -> None:
        with patch.object(request_book_run, 'load_config', return_value=_CONFIG):
            with pytest.raises(ToolError, match='boolean'):
                request_book_run.handle({'request_token': _TOKEN, 'search_now': value})

    @pytest.mark.parametrize('token', ['tampered', _TOKEN[:-1] + 'x'])
    def test_invalid_token_has_zero_api_calls(self, token: str) -> None:
        get = MagicMock()
        mutation = MagicMock()
        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(request_book_run, 'bookshelf_get', get),
            patch.object(request_book_run, 'bookshelf_mutation', mutation),
        ):
            result = request_book_run.handle({'request_token': token})
        assert result['error']['code'] == 'invalid_request_token'
        get.assert_not_called()
        mutation.assert_not_called()

    def test_genuinely_expired_token_has_zero_api_calls(self) -> None:
        expired_token = issue_candidate_token(
            _CONFIG['bookshelf_api_key'],
            'Pride and Prejudice',
            _FOREIGN_ID,
            0,
        )
        get = MagicMock()
        mutation = MagicMock()
        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(request_book_run, 'bookshelf_get', get),
            patch.object(request_book_run, 'bookshelf_mutation', mutation),
        ):
            result = request_book_run.handle({'request_token': expired_token})

        assert result['error']['code'] == 'invalid_request_token'
        get.assert_not_called()
        mutation.assert_not_called()


class TestRequestBookIdempotency:
    def test_existing_monitored_exact_match_has_no_mutation(self) -> None:
        result, get, mutation = _call(
            {'request_token': _TOKEN},
            books=[{
                'id': 42,
                'foreignBookId': str(_FOREIGN_ID),
                'monitored': True,
                'title': 'Pride and Prejudice',
                'author': {'authorName': 'Jane Austen'},
            }],
        )

        assert result == {
            'ok': True,
            'request_id': 42,
            'status': 'requested',
            'terminal': False,
            'suggested_check_after_seconds': 300,
            'title': 'Pride and Prejudice',
            'author': 'Jane Austen',
        }
        mutation.assert_not_called()
        assert get.call_args_list == [
            (("/api/v1/book",), {'config': _CONFIG}),
        ]

    def test_existing_unmonitored_monitors_and_starts_verified_command(self) -> None:
        result, _, mutation = _call(
            {'request_token': _TOKEN},
            books=[{
                'id': 43,
                'foreignBookId': str(_FOREIGN_ID),
                'monitored': False,
                'title': 'Pride and Prejudice',
                'author': {'authorName': 'Jane Austen'},
            }],
        )

        assert result['status'] == 'searching'
        assert result['request_id'] == 43
        assert mutation.call_args_list == [
            (('PUT', '/api/v1/book/monitor', {
                'bookIds': [43],
                'monitored': True,
            }), {'config': _CONFIG}),
            (('POST', '/api/v1/command', {
                'name': 'BookSearch',
                'bookIds': [43],
            }), {'config': _CONFIG}),
        ]

    def test_duplicate_exact_existing_records_prevent_mutation(self) -> None:
        duplicate = {
            'foreignBookId': str(_FOREIGN_ID),
            'monitored': True,
            'title': 'Pride and Prejudice',
            'author': {'authorName': 'Jane Austen'},
        }
        result, _, mutation = _call(
            {'request_token': _TOKEN},
            books=[
                {'id': 45, **duplicate},
                {'id': 46, **duplicate},
            ],
        )

        assert result['error']['code'] == 'duplicate_book'
        mutation.assert_not_called()

    def test_search_now_false_does_not_start_command(self) -> None:
        result, _, mutation = _call(
            {'request_token': _TOKEN, 'search_now': False},
            books=[{
                'id': 44,
                'foreignBookId': str(_FOREIGN_ID),
                'monitored': False,
                'title': 'Pride and Prejudice',
                'author': {'authorName': 'Jane Austen'},
            }],
        )

        assert result['status'] == 'requested'
        mutation.assert_called_once_with(
            'PUT',
            '/api/v1/book/monitor',
            {'bookIds': [44], 'monitored': True},
            config=_CONFIG,
        )


class TestRequestBookAdd:
    def test_missing_book_posts_exact_ui_transformed_resource(self) -> None:
        result, get, mutation = _call({'request_token': _TOKEN}, books=[])

        assert result['request_id'] == 99
        assert result['status'] == 'searching'
        assert result['title'] == 'Pride and Prejudice'
        assert result['author'] == 'Jane Austen'
        mutation.assert_called_once()
        method, path, payload = mutation.call_args.args
        assert (method, path) == ('POST', '/api/v1/book')
        assert mutation.call_args.kwargs == {'config': _CONFIG}
        assert payload['monitored'] is True
        assert payload['addOptions'] == {'searchForNewBook': True}
        assert payload['author']['monitored'] is True
        assert payload['author']['monitorNewItems'] == 'none'
        assert payload['author']['qualityProfileId'] == 2
        assert payload['author']['metadataProfileId'] == 3
        assert payload['author']['rootFolderPath'] == '/books'
        assert payload['author']['tags'] == []
        assert payload['author']['addOptions'] == {
            'searchForMissingBooks': False,
            'booksToMonitor': [str(_FOREIGN_ID)],
        }
        assert len(payload['author']['addOptions']['booksToMonitor']) == 1
        assert 'monitor' not in payload['author']['addOptions']
        # Source fields remain in the POST resource, while output is compact.
        assert payload['editions'][0]['foreignEditionId'] == 'edition-379647'
        assert 'metadata.invalid' not in json.dumps(result)
        assert get.call_args_list[0][0][0] == '/api/v1/book'

    def test_existing_author_is_not_reconfigured_by_get_new_book(self) -> None:
        fresh = _book(author_id=77)
        # Use an existing-author nested resource for this call.
        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(
                request_book_run,
                'bookshelf_get',
                side_effect=lambda path, *, config: (
                    []
                    if path == '/api/v1/book'
                    else [_search_record(fresh)]
                    if path.startswith('/api/v1/search?term=')
                    else _config_get(path, config=config)
                ),
            ),
            patch.object(
                request_book_run,
                'bookshelf_mutation',
                return_value={
                    'id': 100,
                    'foreignBookId': str(_FOREIGN_ID),
                },
            ) as post,
            patch.object(request_book_run, 'verify_request_token', return_value={
                'term': 'Pride and Prejudice', 'foreignBookId': _FOREIGN_ID,
            }),
        ):
            result = request_book_run.handle({'request_token': _TOKEN, 'search_now': False})
        payload = post.call_args.args[2]
        assert result['request_id'] == 100
        assert payload['author']['id'] == 77
        assert payload['author'].get('qualityProfileId') != 2
        assert payload['author'].get('metadataProfileId') != 3
        assert payload['author'].get('rootFolderPath') != '/books'
        assert payload['addOptions'] == {'searchForNewBook': False}
        assert 'booksToMonitor' not in payload['author'].get('addOptions', {})

    @pytest.mark.parametrize(
        'endpoint,records',
        [
            ('/api/v1/rootfolder', []),
            ('/api/v1/qualityprofile', []),
            ('/api/v1/metadataprofile', []),
            (
                '/api/v1/rootfolder',
                [
                    {'id': 1, 'name': 'Bookshelf Sandbox', 'path': '/one'},
                    {'id': 2, 'name': 'Bookshelf Sandbox', 'path': '/two'},
                ],
            ),
        ],
    )
    def test_name_resolution_requires_exactly_one_record(
        self,
        endpoint: str,
        records: list[dict[str, Any]],
    ) -> None:
        def fake_get(path: str, *, config: dict[str, Any]) -> Any:
            if path == '/api/v1/book':
                return []
            if path == endpoint:
                return records
            return _config_get(path, config=config)

        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(request_book_run, 'bookshelf_get', side_effect=fake_get),
            patch.object(request_book_run, 'bookshelf_mutation') as mutation,
            patch.object(request_book_run, 'verify_request_token', return_value={
                'term': 'Book', 'foreignBookId': _FOREIGN_ID,
            }),
        ):
            result = request_book_run.handle({'request_token': _TOKEN})
        assert result['error']['code'] == 'configuration_error'
        mutation.assert_not_called()

    def test_search_requires_one_exact_outer_and_nested_id(self) -> None:
        def fake_get(path: str, *, config: dict[str, Any]) -> Any:
            if path == '/api/v1/book':
                return []
            if path.startswith('/api/v1/search?term='):
                return [_search_record(_book(), outer_id='999')]
            return _config_get(path, config=config)

        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(request_book_run, 'bookshelf_get', side_effect=fake_get),
            patch.object(request_book_run, 'bookshelf_mutation') as mutation,
            patch.object(request_book_run, 'verify_request_token', return_value={
                'term': 'Book', 'foreignBookId': _FOREIGN_ID,
            }),
        ):
            result = request_book_run.handle({'request_token': _TOKEN})
        assert result['error']['code'] == 'candidate_not_found'
        mutation.assert_not_called()

    def test_token_bound_search_term_is_encoded_as_query_data(self) -> None:
        claims = {
            'term': 'Book / edition?x=1',
            'foreignBookId': _FOREIGN_ID,
        }
        paths: list[str] = []

        def fake_get(path: str, *, config: dict[str, Any]) -> Any:
            paths.append(path)
            if path == '/api/v1/book':
                return []
            if path.startswith('/api/v1/search?term='):
                return []
            return _config_get(path, config=config)

        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(request_book_run, 'bookshelf_get', side_effect=fake_get),
            patch.object(request_book_run, 'bookshelf_mutation') as mutation,
            patch.object(request_book_run, 'verify_request_token', return_value=claims),
        ):
            request_book_run.handle({'request_token': _TOKEN})

        assert paths[-1] == '/api/v1/search?term=Book%20%2F%20edition%3Fx%3D1'
        mutation.assert_not_called()

    @pytest.mark.parametrize(
        'post_response',
        [
            777,
            {'id': 777},
            {'id': 777, 'foreignBookId': '999'},
        ],
        ids=['bare-id', 'missing-foreign-id', 'mismatched-foreign-id'],
    )
    def test_unproven_post_response_id_uses_exact_id_convergence(
        self,
        post_response: Any,
    ) -> None:
        book_get_calls = 0

        def fake_get(path: str, *, config: dict[str, Any]) -> Any:
            nonlocal book_get_calls
            if path == '/api/v1/book':
                book_get_calls += 1
                if book_get_calls == 1:
                    return []
                return [{
                    'id': 88,
                    'foreignBookId': str(_FOREIGN_ID),
                    'monitored': True,
                    'title': 'Pride and Prejudice',
                    'author': {'authorName': 'Jane Austen'},
                }]
            return _config_get(path, config=config)

        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(request_book_run, 'bookshelf_get', side_effect=fake_get) as get,
            patch.object(
                request_book_run,
                'bookshelf_mutation',
                return_value=post_response,
            ) as mutation,
            patch.object(request_book_run, 'verify_request_token', return_value={
                'term': 'Pride and Prejudice', 'foreignBookId': _FOREIGN_ID,
            }),
        ):
            result = request_book_run.handle({'request_token': _TOKEN})

        assert result['ok'] is True
        assert result['request_id'] == 88
        assert result['request_id'] != 777
        assert [call.args[0] for call in get.call_args_list].count('/api/v1/book') == 2
        mutation.assert_called_once()

    def test_timeout_after_post_rechecks_exact_foreign_id(self) -> None:
        calls = 0

        def fake_get(path: str, *, config: dict[str, Any]) -> Any:
            nonlocal calls
            if path == '/api/v1/book':
                calls += 1
                if calls == 1:
                    return []
                return [{
                    'id': 88,
                    'foreignBookId': str(_FOREIGN_ID),
                    'monitored': True,
                    'title': 'Pride and Prejudice',
                    'author': {'authorName': 'Jane Austen'},
                }]
            return _config_get(path, config=config)

        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(request_book_run, 'bookshelf_get', side_effect=fake_get),
            patch.object(
                request_book_run,
                'bookshelf_mutation',
                side_effect=BookshelfError('timeout', 'private raw body', retryable=True),
            ) as mutation,
            patch.object(request_book_run, 'verify_request_token', return_value={
                'term': 'Pride and Prejudice', 'foreignBookId': _FOREIGN_ID,
            }),
        ):
            result = request_book_run.handle({'request_token': _TOKEN})

        assert result['ok'] is True
        assert result['request_id'] == 88
        assert result['status'] == 'requested'
        assert 'private raw body' not in json.dumps(result)
        mutation.assert_called_once()

    def test_mutation_error_is_sanitized(self) -> None:
        get = MagicMock(side_effect=lambda path, *, config: (
            [] if path == '/api/v1/book' else _config_get(path, config=config)
        ))
        mutation = MagicMock(side_effect=BookshelfError(
            'upstream_error',
            'https://private-tracker.invalid/passkey=SECRET',
            retryable=True,
        ))
        with (
            patch.object(request_book_run, 'load_config', return_value=_CONFIG),
            patch.object(request_book_run, 'bookshelf_get', get),
            patch.object(request_book_run, 'bookshelf_mutation', mutation),
            patch.object(request_book_run, 'verify_request_token', return_value={
                'term': 'Pride and Prejudice', 'foreignBookId': _FOREIGN_ID,
            }),
        ):
            result = request_book_run.handle({'request_token': _TOKEN})
        assert result['ok'] is False
        assert result['error']['code'] == 'upstream_error'
        assert result['error']['retryable'] is True
        assert 'private-tracker' not in json.dumps(result)


class TestRequestBookDeadlineSimulation:
    def test_multi_call_budget_and_retry_converge_after_partial_mutation(self) -> None:
        # Model six sequential calls inside one 25-second invocation.  The
        # mutation starts with time remaining, changes upstream state, and then
        # times out; the convergence read is refused at the exhausted deadline.
        # A retry gets a fresh deadline and finds the exact existing record.
        first_config = {
            **_CONFIG,
            'http_timeout_seconds': 30.0,
            runtime._DEADLINE_CONFIG_KEY: 25.0,
        }
        retry_config = {
            **first_config,
            runtime._DEADLINE_CONFIG_KEY: 50.0,
        }
        existing = {
            'id': 88,
            'foreignBookId': str(_FOREIGN_ID),
            'monitored': True,
            'title': 'Pride and Prejudice',
            'author': {'authorName': 'Jane Austen'},
        }
        now = [0.0]
        state = {'existing': False}
        attempts: list[tuple[str, str, float]] = []
        accepted: list[tuple[str, str, float]] = []
        mutation_calls: list[tuple[str, str]] = []

        def fake_get(path: str, *, config: dict[str, Any]) -> Any:
            attempts.append(('GET', path, now[0]))
            timeout = runtime._request_timeout(config)
            accepted.append(('GET', path, timeout))
            now[0] += 4.0
            if path == '/api/v1/book':
                return [existing] if state['existing'] else []
            return _config_get(path, config=config)

        def fake_mutation(
            method: str,
            path: str,
            body: Any,
            *,
            config: dict[str, Any],
        ) -> Any:
            attempts.append(('MUTATION', path, now[0]))
            timeout = runtime._request_timeout(config)
            accepted.append(('MUTATION', path, timeout))
            mutation_calls.append((method, path))
            # The request began before the deadline and may have committed
            # upstream even though its response timed out.
            now[0] += timeout
            state['existing'] = True
            raise BookshelfError(
                'timeout',
                'private tracker SECRET',
                retryable=True,
            )

        claims = {'term': 'Pride and Prejudice', 'foreignBookId': _FOREIGN_ID}
        with (
            patch.object(
                request_book_run,
                'load_config',
                side_effect=[first_config, retry_config],
            ),
            patch.object(request_book_run, 'bookshelf_get', side_effect=fake_get),
            patch.object(
                request_book_run,
                'bookshelf_mutation',
                side_effect=fake_mutation,
            ),
            patch.object(request_book_run, 'verify_request_token', return_value=claims),
            patch.object(runtime.time, 'monotonic', side_effect=lambda: now[0]),
        ):
            first = request_book_run.handle({'request_token': _TOKEN})
            retry = request_book_run.handle({'request_token': _TOKEN})

        assert first['error']['code'] == 'timeout'
        assert first['error']['retryable'] is True
        assert 'private tracker' not in json.dumps(first)
        assert retry['ok'] is True
        assert retry['request_id'] == 88
        assert len(mutation_calls) == 1
        assert [entry[2] for entry in accepted[:6]] == [25.0, 21.0, 17.0, 13.0, 9.0, 5.0]
        # The convergence read was attempted exactly at the exhausted deadline
        # and therefore never reached the mocked upstream call.  The next
        # accepted call is the retry's fresh 25-second budget.
        assert attempts[6] == ('GET', '/api/v1/book', 25.0)
        assert accepted[6] == ('GET', '/api/v1/book', 25.0)
        assert now[0] == 29.0


class TestRequestBookManifest:
    def test_description_contains_complete_single_recurring_cron_guidance(self) -> None:
        manifest = json.loads(_MANIFEST_PATH.read_text(encoding='utf-8'))
        description = manifest['description']
        lowered = description.casefold()

        assert lowered.count('one recurring manage_cron') == 1
        assert 'ebooks/check_book_request' in description
        assert 'request_id' in description
        assert 'send_telegram_message' in description
        assert 'terminal' in lowered
        assert '48 hours' in lowered
        assert 'delete that cron entry' in lowered


class TestMutationHelper:
    def _response(self, payload: Any) -> MagicMock:
        response = MagicMock()
        response.__enter__ = lambda value: value
        response.__exit__ = MagicMock(return_value=False)
        response.read.return_value = json.dumps(payload).encode('utf-8')
        return response

    def test_allowlist_header_only_key_and_json_encoding(self) -> None:
        captured: list[Any] = []

        def capture(request: Any, timeout: float) -> Any:
            captured.append((request, timeout))
            return self._response({'id': 7})

        config = {**_CONFIG, 'bookshelf_api_key': 'secret-api-key'}
        with patch('runtime.urlopen', side_effect=capture):
            result = request_book_run.bookshelf_mutation(
                'POST',
                '/api/v1/command',
                {'name': 'BookSearch', 'bookIds': [7]},
                config=config,
            )

        assert result == {'id': 7}
        request, timeout = captured[0]
        assert timeout == 5.0
        assert request.full_url == 'http://bookshelf.invalid/api/v1/command'
        assert request.get_header('X-api-key') == 'secret-api-key'
        assert request.get_header('Content-type') == 'application/json'
        assert json.loads(request.data.decode('utf-8')) == {
            'name': 'BookSearch',
            'bookIds': [7],
        }
        assert 'secret-api-key' not in request.full_url
        assert 'secret-api-key' not in request.data.decode('utf-8')

    @pytest.mark.parametrize(
        'body',
        [
            {'name': 'RefreshBook', 'bookIds': [7]},
            {'name': 'BookSearch', 'bookIds': [7], 'extra': True},
            {'name': 'BookSearch', 'bookIds': []},
            {'name': 'BookSearch', 'bookIds': [7, 8]},
            {'name': 'BookSearch', 'bookIds': [0]},
            {'name': 'BookSearch', 'bookIds': [-1]},
            {'name': 'BookSearch', 'bookIds': [True]},
            {'name': 'BookSearch', 'bookIds': ['7']},
            {'name': 'BookSearch', 'bookIds': [7.0]},
            {'name': 'BookSearch', 'bookIds': [1 << 31]},
            {'name': 'BookSearch'},
        ],
    )
    def test_command_body_rejects_rpc_bypasses(self, body: dict[str, Any]) -> None:
        with patch('runtime.urlopen') as urlopen:
            with pytest.raises(ToolError, match='body is not allowed'):
                request_book_run.bookshelf_mutation(
                    'POST',
                    '/api/v1/command',
                    body,
                    config=_CONFIG,
                )
        urlopen.assert_not_called()

    @pytest.mark.parametrize(
        'body',
        [
            {'bookIds': [7], 'monitored': False},
            {'bookIds': [7], 'monitored': 'true'},
            {'bookIds': [7], 'monitored': True, 'extra': True},
            {'bookIds': [], 'monitored': True},
            {'bookIds': [7, 8], 'monitored': True},
            {'bookIds': [0], 'monitored': True},
            {'bookIds': [True], 'monitored': True},
            {'bookIds': ['7'], 'monitored': True},
            {'monitored': True},
        ],
    )
    def test_monitor_body_rejects_shape_bypasses(self, body: dict[str, Any]) -> None:
        with patch('runtime.urlopen') as urlopen:
            with pytest.raises(ToolError, match='body is not allowed'):
                request_book_run.bookshelf_mutation(
                    'PUT',
                    '/api/v1/book/monitor',
                    body,
                    config=_CONFIG,
                )
        urlopen.assert_not_called()

    @pytest.mark.parametrize(
        ('method', 'path'),
        [
            ('POST', '/api/v1/author'),
            ('DELETE', '/api/v1/book'),
            ('POST', 'https://attacker.invalid/api/v1/book'),
            ('PUT', '/api/v1/book/monitor?bookIds=7'),
        ],
    )
    def test_mutation_allowlist_rejects_unowned_routes(self, method: str, path: str) -> None:
        with pytest.raises(ToolError, match='not allowed'):
            request_book_run.bookshelf_mutation(
                method,
                path,
                {},
                config=_CONFIG,
            )

    def test_http_error_body_is_drained_and_redacted(self) -> None:
        body = b'{"private":"https://tracker.invalid/passkey=SECRET"}'
        error = HTTPError(
            url='http://bookshelf.invalid/api/v1/book',
            code=500,
            msg='HTTP 500',
            hdrs=None,
            fp=io.BytesIO(body),
        )
        with patch('runtime.urlopen', side_effect=error):
            with pytest.raises(BookshelfError) as raised:
                request_book_run.bookshelf_mutation(
                    'POST', '/api/v1/book', {}, config=_CONFIG
                )
        assert raised.value.code == 'upstream_error'
        assert 'SECRET' not in str(raised.value)

    def test_timeout_maps_to_retryable(self) -> None:
        with patch('runtime.urlopen', side_effect=URLError(TimeoutError('secret'))):
            with pytest.raises(BookshelfError) as raised:
                request_book_run.bookshelf_mutation(
                    'PUT',
                    '/api/v1/book/monitor',
                    {'bookIds': [7], 'monitored': True},
                    config=_CONFIG,
                )
        assert raised.value.code == 'timeout'
        assert raised.value.retryable is True
