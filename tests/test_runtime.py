"""
Tests for shared/runtime.py.

Coverage:
- load_config: missing file, invalid JSON, non-object, missing required keys,
  bad URL, bad timeout, defaults applied, key never in error messages.
- bookshelf_get: 401/403 → authentication_failed, 5xx → upstream_error (retryable),
  404 → not_found, timeout → timeout (retryable), URLError → unreachable,
  invalid JSON response → invalid_response.
- API key redaction: key value must not appear in any BookshelfError message.
- API key not in URL: request URL contains only the path, not the key.
- run_tool: success path, ToolError path.
- param validators: require_string, optional_bool, optional_int, optional_string.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

import runtime
from runtime import (
    BookshelfError,
    ToolError,
    load_config,
    optional_bool,
    optional_int,
    optional_string,
    require_string,
    run_tool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOOD_CONFIG: dict[str, Any] = {
    'bookshelf_url': 'http://localhost:8787',
    'bookshelf_api_key': 'test-api-key-abc123',
}


def _make_config_file(tmp_path: Path, data: dict[str, Any]) -> None:
    (tmp_path / 'config.json').write_text(json.dumps(data), encoding='utf-8')


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            with pytest.raises(ToolError, match='config.json is missing'):
                load_config()

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        (tmp_path / 'config.json').write_text('{not valid json', encoding='utf-8')
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            with pytest.raises(ToolError, match='valid JSON'):
                load_config()

    def test_non_object_raises(self, tmp_path: Path) -> None:
        (tmp_path / 'config.json').write_text('["list"]', encoding='utf-8')
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            with pytest.raises(ToolError, match='JSON object'):
                load_config()

    def test_missing_bookshelf_url_raises(self, tmp_path: Path) -> None:
        _make_config_file(tmp_path, {'bookshelf_api_key': 'key'})
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            with pytest.raises(ToolError, match='bookshelf_url'):
                load_config()

    def test_invalid_url_scheme_raises(self, tmp_path: Path) -> None:
        _make_config_file(tmp_path, {'bookshelf_url': 'ftp://bad', 'bookshelf_api_key': 'key'})
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            with pytest.raises(ToolError, match='HTTP or HTTPS'):
                load_config()

    def test_missing_api_key_raises(self, tmp_path: Path) -> None:
        _make_config_file(tmp_path, {'bookshelf_url': 'http://localhost:8787'})
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            with pytest.raises(ToolError, match='bookshelf_api_key'):
                load_config()

    @pytest.mark.parametrize(
        ('timeout', 'expected'),
        [
            (15, 15.0),
            (15.5, 15.5),
            ('15', 15.0),
            ('0.5', 0.5),
            ('120', 120.0),
            (' 15.5 ', 15.5),
            (0.5, 0.5),
            (120, 120.0),
        ],
    )
    def test_timeout_numbers_and_plain_decimal_strings_load(
        self,
        tmp_path: Path,
        timeout: int | float | str,
        expected: float,
    ) -> None:
        data = {**_GOOD_CONFIG, 'http_timeout_seconds': timeout}
        _make_config_file(tmp_path, data)
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            config = load_config()
        assert config['http_timeout_seconds'] == expected
        assert isinstance(config['http_timeout_seconds'], float)

    @pytest.mark.parametrize(
        'timeout',
        [
            True,
            False,
            0,
            -1,
            120.0001,
            121,
            float('nan'),
            float('inf'),
            float('-inf'),
            '',
            '   ',
            '0',
            '0.0',
            '-1',
            '120.0001',
            '121',
            '.5',
            '15.',
            '01',
            '+15',
            '1e1',
            'NaN',
            'Infinity',
            'arbitrary text',
        ],
    )
    def test_invalid_timeout_is_rejected_without_secret_exposure(
        self,
        tmp_path: Path,
        timeout: Any,
    ) -> None:
        data = {**_GOOD_CONFIG, 'http_timeout_seconds': timeout}
        _make_config_file(tmp_path, data)
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            with pytest.raises(ToolError, match='http_timeout_seconds') as exc_info:
                load_config()
        assert _GOOD_CONFIG['bookshelf_api_key'] not in str(exc_info.value)

    def test_defaults_applied(self, tmp_path: Path) -> None:
        _make_config_file(tmp_path, _GOOD_CONFIG)
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            cfg = load_config()
        assert cfg['root_folder_name'] == 'Bookshelf Sandbox'
        assert cfg['quality_profile_name'] == 'eBook'
        assert cfg['metadata_profile_name'] == 'Standard'
        assert cfg['http_timeout_seconds'] == 15.0

    def test_load_config_stores_one_internal_deadline(self, tmp_path: Path) -> None:
        # The internal deadline is generated at load time, never accepted from
        # the user-controlled config file.
        data = {**_GOOD_CONFIG, runtime._DEADLINE_CONFIG_KEY: 9999.0}
        _make_config_file(tmp_path, data)
        with (
            patch.object(runtime, 'PLUGIN_ROOT', tmp_path),
            patch.object(runtime.time, 'monotonic', return_value=100.0) as monotonic,
        ):
            cfg = load_config()

        assert cfg[runtime._DEADLINE_CONFIG_KEY] == 125.0
        monotonic.assert_called_once_with()

    def test_trailing_slash_stripped_from_url(self, tmp_path: Path) -> None:
        data = {**_GOOD_CONFIG, 'bookshelf_url': 'http://localhost:8787/'}
        _make_config_file(tmp_path, data)
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            cfg = load_config()
        assert cfg['bookshelf_url'] == 'http://localhost:8787'

    def test_overrides_accepted(self, tmp_path: Path) -> None:
        data = {
            **_GOOD_CONFIG,
            'root_folder_name': 'My Books',
            'quality_profile_name': 'Spoken',
            'metadata_profile_name': 'None',
            'http_timeout_seconds': 30,
        }
        _make_config_file(tmp_path, data)
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            cfg = load_config()
        assert cfg['root_folder_name'] == 'My Books'
        assert cfg['quality_profile_name'] == 'Spoken'
        assert cfg['metadata_profile_name'] == 'None'
        assert cfg['http_timeout_seconds'] == 30.0

    def test_api_key_not_in_missing_url_error(self, tmp_path: Path) -> None:
        """Error messages must not contain the API key value."""
        data = {'bookshelf_url': '', 'bookshelf_api_key': 'SECRET-KEY-XYZ'}
        _make_config_file(tmp_path, data)
        with patch.object(runtime, 'PLUGIN_ROOT', tmp_path):
            with pytest.raises(ToolError) as exc_info:
                load_config()
        assert 'SECRET-KEY-XYZ' not in str(exc_info.value)


# ---------------------------------------------------------------------------
# bookshelf_get – HTTP error mapping
# ---------------------------------------------------------------------------


def _make_http_error(code: int, body: bytes = b'{}') -> HTTPError:
    return HTTPError(
        url='http://localhost:8787/api/v1/system/status',
        code=code,
        msg=f'HTTP {code}',
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


def _make_raw_response(body: bytes) -> MagicMock:
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read = MagicMock(return_value=body)
    return mock


def _make_ok_response(data: Any) -> MagicMock:
    return _make_raw_response(json.dumps(data).encode('utf-8'))


_TEST_CONFIG: dict[str, Any] = {
    'bookshelf_url': 'http://localhost:8787',
    'bookshelf_api_key': 'test-api-key-SECRET',
    'http_timeout_seconds': 5.0,
}


class TestBookshelfGet:
    def test_success_returns_parsed_json(self) -> None:
        payload = {'version': '0.4.20.129'}
        with patch('runtime.urlopen', return_value=_make_ok_response(payload)):
            result = runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert result == payload

    def test_deadline_decreases_and_is_shared_by_get_and_mutation(self) -> None:
        config = {
            **_TEST_CONFIG,
            'http_timeout_seconds': 60.0,
            runtime._DEADLINE_CONFIG_KEY: 125.0,
        }
        timeouts: list[float] = []

        def capture(_: Any, timeout: float) -> Any:
            timeouts.append(timeout)
            return _make_ok_response({})

        with (
            patch.object(runtime.time, 'monotonic', side_effect=[100.0, 104.0, 120.0]),
            patch('runtime.urlopen', side_effect=capture),
        ):
            runtime.bookshelf_get('/api/v1/system/status', config=config)
            runtime.bookshelf_json_mutation(
                'POST', '/api/v1/book', {}, config=config
            )
            runtime.bookshelf_get('/api/v1/system/status', config=config)

        assert timeouts == [25.0, 21.0, 5.0]

    def test_configured_timeout_caps_remaining_deadline(self) -> None:
        config = {
            **_TEST_CONFIG,
            'http_timeout_seconds': 5.0,
            runtime._DEADLINE_CONFIG_KEY: 125.0,
        }
        captured: list[float] = []

        def capture(_: Any, timeout: float) -> Any:
            captured.append(timeout)
            return _make_ok_response({})

        with (
            patch.object(runtime.time, 'monotonic', return_value=100.0),
            patch('runtime.urlopen', side_effect=capture),
        ):
            runtime.bookshelf_get('/api/v1/system/status', config=config)

        assert captured == [5.0]

    def test_direct_config_without_deadline_keeps_configured_timeout(self) -> None:
        captured: list[float] = []

        def capture(_: Any, timeout: float) -> Any:
            captured.append(timeout)
            return _make_ok_response({})

        with (
            patch.object(runtime.time, 'monotonic', side_effect=AssertionError),
            patch('runtime.urlopen', side_effect=capture),
        ):
            runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)

        assert captured == [_TEST_CONFIG['http_timeout_seconds']]

    @pytest.mark.parametrize('helper', ['get', 'mutation'])
    def test_exhausted_deadline_fails_before_urlopen(
        self, helper: str
    ) -> None:
        config = {**_TEST_CONFIG, runtime._DEADLINE_CONFIG_KEY: 100.0}
        with (
            patch.object(runtime.time, 'monotonic', return_value=100.0),
            patch('runtime.urlopen') as urlopen,
        ):
            with pytest.raises(BookshelfError) as exc_info:
                if helper == 'get':
                    runtime.bookshelf_get('/api/v1/system/status', config=config)
                else:
                    runtime.bookshelf_json_mutation(
                        'POST', '/api/v1/book', {}, config=config
                    )

        assert exc_info.value.code == 'timeout'
        assert exc_info.value.retryable is True
        assert str(exc_info.value) == (
            'Bookshelf did not respond within the configured timeout'
        )
        urlopen.assert_not_called()

    def test_success_list(self) -> None:
        payload = [{'id': 1, 'name': 'eBook'}]
        with patch('runtime.urlopen', return_value=_make_ok_response(payload)):
            result = runtime.bookshelf_get('/api/v1/qualityprofile', config=_TEST_CONFIG)
        assert result == payload

    def test_success_read_uses_limit_plus_one(self) -> None:
        response = _make_ok_response({'ok': True})
        with patch('runtime.urlopen', return_value=response):
            runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)

        response.read.assert_called_once_with(runtime._MAX_RESPONSE_BYTES + 1)

    def test_oversized_response_is_rejected_without_body_exposure(self) -> None:
        response = _make_raw_response(b'SECRET-BODY')
        with (
            patch.object(runtime, '_MAX_RESPONSE_BYTES', 8),
            patch('runtime.urlopen', return_value=response),
        ):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)

        assert exc_info.value.code == 'response_too_large'
        assert 'SECRET-BODY' not in str(exc_info.value)
        response.read.assert_called_once_with(9)

    def test_401_raises_authentication_failed(self) -> None:
        with patch('runtime.urlopen', side_effect=_make_http_error(401)):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert exc_info.value.code == 'authentication_failed'
        assert not exc_info.value.retryable

    def test_403_raises_authentication_failed(self) -> None:
        with patch('runtime.urlopen', side_effect=_make_http_error(403)):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert exc_info.value.code == 'authentication_failed'

    def test_404_raises_fixed_not_found_without_user_controlled_path(self) -> None:
        attacker_path = (
            '/api/v1/search?term=attacker%2Fquery%3Ftoken%3DUSER-SECRET%250A'
        )
        with patch('runtime.urlopen', side_effect=_make_http_error(404)):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get(attacker_path, config=_TEST_CONFIG)

        message = str(exc_info.value)
        assert exc_info.value.code == 'not_found'
        assert not exc_info.value.retryable
        assert message == 'Bookshelf did not find the requested resource'
        assert attacker_path not in message
        assert 'attacker' not in message
        assert 'USER-SECRET' not in message
        assert '%2F' not in message

    def test_500_raises_upstream_error_retryable(self) -> None:
        with patch('runtime.urlopen', side_effect=_make_http_error(500)):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert exc_info.value.code == 'upstream_error'
        assert exc_info.value.retryable

    def test_503_raises_upstream_error_retryable(self) -> None:
        with patch('runtime.urlopen', side_effect=_make_http_error(503)):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert exc_info.value.code == 'upstream_error'
        assert exc_info.value.retryable

    def test_timeout_via_url_error_raises_timeout_retryable(self) -> None:
        # Python 3.11/3.12 style: TimeoutError wrapped in URLError
        timeout_exc = URLError(TimeoutError('timed out'))
        with patch('runtime.urlopen', side_effect=timeout_exc):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert exc_info.value.code == 'timeout'
        assert exc_info.value.retryable

    def test_timeout_direct_raises_timeout_retryable(self) -> None:
        # Python 3.13+ style: TimeoutError raised directly from urlopen
        with patch('runtime.urlopen', side_effect=TimeoutError('timed out')):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert exc_info.value.code == 'timeout'
        assert exc_info.value.retryable

    def test_connection_refused_raises_unreachable(self) -> None:
        conn_exc = URLError(ConnectionRefusedError('refused'))
        with patch('runtime.urlopen', side_effect=conn_exc):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert exc_info.value.code == 'unreachable'
        assert exc_info.value.retryable

    def test_invalid_json_response_raises(self) -> None:
        mock = MagicMock()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        mock.read = MagicMock(return_value=b'not json {')
        with patch('runtime.urlopen', return_value=mock):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert exc_info.value.code == 'invalid_response'

    # --- redaction ---

    def test_api_key_not_in_401_error_message(self) -> None:
        with patch('runtime.urlopen', side_effect=_make_http_error(401)):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        assert 'test-api-key-SECRET' not in str(exc_info.value)

    def test_api_key_not_in_url(self) -> None:
        """Verify the request URL does not contain the API key."""
        captured: list[Any] = []

        def capture_request(req: Any, timeout: float) -> Any:
            captured.append(req)
            raise URLError('stop after capture')

        with patch('runtime.urlopen', side_effect=capture_request):
            with pytest.raises(BookshelfError):
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)

        assert captured, 'urlopen was not called'
        req = captured[0]
        assert 'test-api-key-SECRET' not in req.full_url
        # Key must be in header only
        assert req.get_header('X-api-key') == 'test-api-key-SECRET'

    def test_upstream_body_not_in_http_error_message(self) -> None:
        """Raw upstream body (which may contain tracker URLs) must not appear in errors."""
        sensitive_body = b'{"downloadUrl": "https://tracker.private/announce?passkey=SECRETTOKEN"}'
        error = _make_http_error(500, sensitive_body)
        error.read = MagicMock(return_value=sensitive_body)
        with (
            patch.object(runtime, '_MAX_RESPONSE_BYTES', 8),
            patch('runtime.urlopen', side_effect=error),
        ):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_get('/api/v1/system/status', config=_TEST_CONFIG)
        error_msg = str(exc_info.value)
        assert 'SECRETTOKEN' not in error_msg
        assert 'tracker.private' not in error_msg
        error.read.assert_called_once_with(9)

    def test_bookshelf_error_as_dict_shape(self) -> None:
        err = BookshelfError('authentication_failed', 'bad key', retryable=False)
        d = err.as_dict()
        assert d['ok'] is False
        assert d['error']['code'] == 'authentication_failed'
        assert d['error']['message'] == 'bad key'
        assert d['error']['retryable'] is False


# ---------------------------------------------------------------------------
# bookshelf_json_mutation
# ---------------------------------------------------------------------------


class TestBookshelfJsonMutation:
    def test_empty_response_returns_none_and_uses_limit_plus_one(self) -> None:
        response = _make_raw_response(b'')
        with (
            patch.object(runtime, '_MAX_RESPONSE_BYTES', 8),
            patch('runtime.urlopen', return_value=response),
        ):
            result = runtime.bookshelf_json_mutation(
                'POST',
                '/api/v1/book',
                {},
                config=_TEST_CONFIG,
            )

        assert result is None
        response.read.assert_called_once_with(9)

    def test_oversized_response_is_rejected_without_body_exposure(self) -> None:
        response = _make_raw_response(b'SECRET-BODY')
        with (
            patch.object(runtime, '_MAX_RESPONSE_BYTES', 8),
            patch('runtime.urlopen', return_value=response),
        ):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_json_mutation(
                    'POST',
                    '/api/v1/book',
                    {},
                    config=_TEST_CONFIG,
                )

        assert exc_info.value.code == 'response_too_large'
        assert 'SECRET-BODY' not in str(exc_info.value)
        response.read.assert_called_once_with(9)

    def test_http_error_discard_is_bounded_and_redacted(self) -> None:
        sensitive_body = b'https://tracker.invalid/passkey=SECRET'
        error = _make_http_error(500, sensitive_body)
        error.read = MagicMock(return_value=sensitive_body)
        with (
            patch.object(runtime, '_MAX_RESPONSE_BYTES', 8),
            patch('runtime.urlopen', side_effect=error),
        ):
            with pytest.raises(BookshelfError) as exc_info:
                runtime.bookshelf_json_mutation(
                    'POST',
                    '/api/v1/book',
                    {},
                    config=_TEST_CONFIG,
                )

        assert exc_info.value.code == 'upstream_error'
        assert 'SECRET' not in str(exc_info.value)
        assert 'tracker.invalid' not in str(exc_info.value)
        error.read.assert_called_once_with(9)


# ---------------------------------------------------------------------------
# run_tool
# ---------------------------------------------------------------------------


class TestRunTool:
    def test_success_writes_json_to_stdout(self, capsys: pytest.CaptureFixture) -> None:
        with patch('sys.stdin', io.StringIO('{}')):
            exit_code = run_tool(lambda _: {'ok': True, 'value': 42})
        assert exit_code == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {'ok': True, 'value': 42}

    def test_tool_error_writes_to_stderr_and_exits_1(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        def failing(_: Any) -> Any:
            raise ToolError('something went wrong')

        with patch('sys.stdin', io.StringIO('{}')):
            exit_code = run_tool(failing)
        assert exit_code == 1
        captured = capsys.readouterr()
        assert 'something went wrong' in captured.err

    def test_invalid_stdin_json_exits_1(self, capsys: pytest.CaptureFixture) -> None:
        with patch('sys.stdin', io.StringIO('{invalid')):
            exit_code = run_tool(lambda _: {})
        assert exit_code == 1

    def test_non_object_stdin_exits_1(self, capsys: pytest.CaptureFixture) -> None:
        with patch('sys.stdin', io.StringIO('[1,2,3]')):
            exit_code = run_tool(lambda _: {})
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Param validators
# ---------------------------------------------------------------------------


class TestParamValidators:
    def test_require_string_present(self) -> None:
        assert require_string({'q': 'hello'}, 'q') == 'hello'

    def test_require_string_missing_raises(self) -> None:
        with pytest.raises(ToolError, match='q is required'):
            require_string({}, 'q')

    def test_require_string_empty_raises(self) -> None:
        with pytest.raises(ToolError):
            require_string({'q': '   '}, 'q')

    def test_optional_string_absent(self) -> None:
        assert optional_string({}, 'q') is None

    def test_optional_string_present(self) -> None:
        assert optional_string({'q': 'val'}, 'q') == 'val'

    def test_optional_string_empty_raises(self) -> None:
        with pytest.raises(ToolError):
            optional_string({'q': ''}, 'q')

    def test_optional_bool_absent(self) -> None:
        assert optional_bool({}, 'flag') is None

    def test_optional_bool_true(self) -> None:
        assert optional_bool({'flag': True}, 'flag') is True

    def test_optional_bool_false(self) -> None:
        assert optional_bool({'flag': False}, 'flag') is False

    def test_optional_bool_non_bool_raises(self) -> None:
        with pytest.raises(ToolError, match='boolean'):
            optional_bool({'flag': 'true'}, 'flag')

    def test_optional_int_absent(self) -> None:
        assert optional_int({}, 'n') is None

    def test_optional_int_present(self) -> None:
        assert optional_int({'n': 5}, 'n') == 5

    def test_optional_int_bool_raises(self) -> None:
        with pytest.raises(ToolError, match='integer'):
            optional_int({'n': True}, 'n')

    def test_optional_int_float_raises(self) -> None:
        with pytest.raises(ToolError, match='integer'):
            optional_int({'n': 3.14}, 'n')
