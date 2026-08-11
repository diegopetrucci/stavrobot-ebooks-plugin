"""Repository contracts derived from the local Stavrobot plugin runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_TOOLS = frozenset(
    {
        'books_status',
        'check_book_request',
        'request_book',
        'search_books',
    }
)
_RUNNER_PARAMETER_TYPES = frozenset(
    {'string', 'number', 'integer', 'boolean', 'file'}
)
_RUNNER_PARAMETER_FIELDS = frozenset({'type', 'description'})
_RUNNER_CONFIG_FIELDS = frozenset({'description', 'required', 'default'})


def _load_json_object(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as handle:
        value = json.load(handle)
    assert isinstance(value, dict), f'{path} must contain a JSON object'
    return value


def test_root_manifest_and_config_example_match() -> None:
    manifest = _load_json_object(_PLUGIN_ROOT / 'manifest.json')
    example = _load_json_object(_PLUGIN_ROOT / 'config.example.json')

    assert 'entrypoint' not in manifest
    assert isinstance(manifest.get('name'), str)
    assert isinstance(manifest.get('description'), str)

    config = manifest.get('config')
    assert isinstance(config, dict)
    assert set(example) == set(config)

    for key, raw_schema in config.items():
        assert isinstance(raw_schema, dict), f'config schema for {key} must be flat'
        assert set(raw_schema) <= _RUNNER_CONFIG_FIELDS
        assert isinstance(raw_schema.get('description'), str)
        assert isinstance(raw_schema.get('required'), bool)
        if 'default' in raw_schema:
            assert example[key] == raw_schema['default']


def test_all_tool_manifests_match_runner_contract() -> None:
    manifest_paths = {
        path.parent.name: path
        for path in _PLUGIN_ROOT.glob('*/manifest.json')
    }
    assert set(manifest_paths) == _EXPECTED_TOOLS

    for tool_directory, path in sorted(manifest_paths.items()):
        manifest = _load_json_object(path)
        assert manifest.get('name') == tool_directory
        assert isinstance(manifest.get('description'), str)

        entrypoint = manifest.get('entrypoint')
        assert isinstance(entrypoint, str)
        assert (path.parent / entrypoint).is_file()

        parameters = manifest.get('parameters')
        assert isinstance(parameters, dict)
        for name, raw_schema in parameters.items():
            assert isinstance(name, str)
            assert isinstance(raw_schema, dict)
            assert set(raw_schema) == _RUNNER_PARAMETER_FIELDS
            assert raw_schema['type'] in _RUNNER_PARAMETER_TYPES
            assert isinstance(raw_schema['description'], str)
