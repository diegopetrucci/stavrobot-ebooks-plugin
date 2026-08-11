"""Focused security and lifecycle tests for shared candidate request tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

import pytest

import candidate_token
from candidate_token import (
    CandidateTokenError,
    MAX_FOREIGN_BOOK_ID,
    MAX_TOKEN_LENGTH,
    TOKEN_TTL_SECONDS,
    issue_candidate_token,
    verify_candidate_token,
)


_API_KEY = 'test-api-key-SECRET'
_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _segments(token: str) -> tuple[str, str, str]:
    version, payload, signature = token.split('.')
    return version, payload, signature


def _decode_payload(token: str) -> dict[str, Any]:
    _, payload, _ = _segments(token)
    raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
    decoded = json.loads(raw.decode('utf-8'))
    assert isinstance(decoded, dict)
    return decoded


def _signed_token(claims: dict[str, Any]) -> str:
    payload = candidate_token._encode_base64url(  # noqa: SLF001
        candidate_token._canonical_json(claims)  # noqa: SLF001
    )
    signature = hmac.new(
        candidate_token._signing_key(_API_KEY),  # noqa: SLF001
        payload.encode('ascii'),
        hashlib.sha256,
    ).digest()
    return f'v1.{payload}.{candidate_token._encode_base64url(signature)}'  # noqa: SLF001


class TestCandidateTokenIssuance:
    def test_deterministic_issue_and_round_trip(self) -> None:
        token = issue_candidate_token(_API_KEY, '  Pride and Prejudice  ', 379647, _NOW)
        same = issue_candidate_token(_API_KEY, 'Pride and Prejudice', 379647, _NOW)

        assert token == same
        assert token.startswith('v1.')
        assert verify_candidate_token(token, _API_KEY, _NOW) == {
            'version': 1,
            'issued_at': int(_NOW.timestamp()),
            'expires_at': int(_NOW.timestamp()) + TOKEN_TTL_SECONDS,
            'term': 'Pride and Prejudice',
            'foreignBookId': 379647,
        }

    def test_term_and_foreign_id_are_bound(self) -> None:
        token = issue_candidate_token(_API_KEY, 'Pride and Prejudice', 379647, _NOW)
        assert issue_candidate_token(_API_KEY, 'Emma', 379647, _NOW) != token
        assert issue_candidate_token(_API_KEY, 'Pride and Prejudice', 123, _NOW) != token
        assert verify_candidate_token(token, _API_KEY, _NOW)['foreignBookId'] == 379647

    @pytest.mark.parametrize('foreign_book_id', [0, -1, True, False, '379647', 379647.0, None])
    def test_issue_requires_exact_positive_integer_foreign_book_id(
        self, foreign_book_id: Any
    ) -> None:
        with pytest.raises(CandidateTokenError):
            issue_candidate_token(_API_KEY, 'Book', foreign_book_id, _NOW)

    def test_issue_rejects_foreign_book_id_above_shared_bound(self) -> None:
        with pytest.raises(CandidateTokenError):
            issue_candidate_token(
                _API_KEY,
                'Book',
                MAX_FOREIGN_BOOK_ID + 1,
                _NOW,
            )

    def test_maximum_foreign_book_id_and_term_round_trip_within_token_bound(self) -> None:
        term = '\U00010348' * 200
        token = issue_candidate_token(
            _API_KEY,
            term,
            MAX_FOREIGN_BOOK_ID,
            _NOW,
        )

        assert len(token.encode('utf-8')) <= MAX_TOKEN_LENGTH
        claims = verify_candidate_token(token, _API_KEY, _NOW)
        assert claims['term'] == term
        assert claims['foreignBookId'] == MAX_FOREIGN_BOOK_ID

    def test_issue_never_emits_a_payload_segment_its_verifier_rejects(self) -> None:
        with pytest.raises(CandidateTokenError):
            issue_candidate_token(
                _API_KEY,
                '\x00' * 200,
                MAX_FOREIGN_BOOK_ID,
                float.fromhex('0x1.fffffffffffffp+1023'),
            )

    @pytest.mark.parametrize('term', ['', '   ', 'x' * 201, 42, None])
    def test_issue_requires_bounded_lookup_term(self, term: Any) -> None:
        with pytest.raises(CandidateTokenError):
            issue_candidate_token(_API_KEY, term, 1, _NOW)

    def test_datetime_and_callable_clocks_are_injectable(self) -> None:
        aware = issue_candidate_token(_API_KEY, 'Book', 1, _NOW)
        naive = issue_candidate_token(_API_KEY, 'Book', 1, _NOW.replace(tzinfo=None))
        callable_clock = issue_candidate_token(
            _API_KEY, 'Book', 1, lambda: _NOW.timestamp()
        )
        assert aware == naive == callable_clock


class TestCandidateTokenVerification:
    def test_tampered_payload_fails(self) -> None:
        token = issue_candidate_token(_API_KEY, 'Book', 1, _NOW)
        version, payload, signature = _segments(token)
        decoded = _decode_payload(token)
        decoded['foreignBookId'] = 2
        altered_payload = base64.urlsafe_b64encode(
            json.dumps(decoded, separators=(',', ':'), sort_keys=True).encode()
        ).rstrip(b'=').decode()

        with pytest.raises(CandidateTokenError):
            verify_candidate_token(f'{version}.{altered_payload}.{signature}', _API_KEY, _NOW)

    def test_tampered_signature_fails(self) -> None:
        token = issue_candidate_token(_API_KEY, 'Book', 1, _NOW)
        version, payload, signature = _segments(token)
        replacement = ('A' if signature[0] != 'A' else 'B') + signature[1:]

        with pytest.raises(CandidateTokenError):
            verify_candidate_token(f'{version}.{payload}.{replacement}', _API_KEY, _NOW)

    def test_wrong_version_fails(self) -> None:
        token = issue_candidate_token(_API_KEY, 'Book', 1, _NOW)
        _, payload, signature = _segments(token)

        with pytest.raises(CandidateTokenError, match='version'):
            verify_candidate_token(f'v99.{payload}.{signature}', _API_KEY, _NOW)

    def test_verify_rejects_validly_signed_foreign_book_id_above_shared_bound(self) -> None:
        issued_at = int(_NOW.timestamp())
        token = _signed_token(
            {
                'version': 1,
                'issued_at': issued_at,
                'expires_at': issued_at + TOKEN_TTL_SECONDS,
                'term': 'Book',
                'foreignBookId': MAX_FOREIGN_BOOK_ID + 1,
            }
        )

        with pytest.raises(CandidateTokenError):
            verify_candidate_token(token, _API_KEY, _NOW)

    def test_expiry_is_24_hours_and_exact_expiry_is_rejected(self) -> None:
        token = issue_candidate_token(_API_KEY, 'Book', 1, _NOW)

        assert verify_candidate_token(
            token, _API_KEY, _NOW.timestamp() + TOKEN_TTL_SECONDS - 1
        )['foreignBookId'] == 1
        with pytest.raises(CandidateTokenError, match='expired'):
            verify_candidate_token(
                token, _API_KEY, _NOW.timestamp() + TOKEN_TTL_SECONDS
            )

    def test_future_issued_token_is_rejected(self) -> None:
        token = issue_candidate_token(_API_KEY, 'Book', 1, _NOW)

        with pytest.raises(CandidateTokenError, match='not yet valid'):
            verify_candidate_token(token, _API_KEY, _NOW.timestamp() - 1)

    @pytest.mark.parametrize(
        'token',
        [
            '',
            'not-a-token',
            'v1..signature',
            'v1.payload.signature.extra',
            'v1.!!!.signature',
            'v1.payload.!!!',
            'v2.payload.signature',
        ],
    )
    def test_malformed_tokens_are_rejected(self, token: str) -> None:
        with pytest.raises(CandidateTokenError):
            verify_candidate_token(token, _API_KEY, _NOW)

    def test_oversized_token_is_rejected_without_decoding(self) -> None:
        token = 'v1.' + ('A' * 5000) + '.sig'

        with pytest.raises(CandidateTokenError):
            verify_candidate_token(token, _API_KEY, _NOW)

    def test_wrong_key_fails_and_token_has_no_secret(self) -> None:
        token = issue_candidate_token(_API_KEY, 'Book', 1, _NOW)

        assert _API_KEY not in token
        with pytest.raises(CandidateTokenError):
            verify_candidate_token(token, 'other-api-key', _NOW)

    def test_payload_contains_only_bounded_claims_and_no_upstream_values(self) -> None:
        token = issue_candidate_token(_API_KEY, 'Book', 1, _NOW)
        payload = _decode_payload(token)

        assert set(payload) == {
            'version',
            'issued_at',
            'expires_at',
            'term',
            'foreignBookId',
        }
        assert _API_KEY not in json.dumps(payload)
        assert 'https://' not in json.dumps(payload)
        assert '/api/' not in json.dumps(payload)
        assert 'Bookshelf Sandbox' not in json.dumps(payload)
        assert 'edition-private' not in json.dumps(payload)

    def test_noncanonical_payload_with_invalid_signature_is_rejected(self) -> None:
        token = issue_candidate_token(_API_KEY, 'Book', 1, _NOW)
        version, payload, signature = _segments(token)
        decoded = _decode_payload(token)
        noncanonical = base64.urlsafe_b64encode(
            json.dumps(decoded, indent=2).encode()
        ).rstrip(b'=').decode()

        with pytest.raises(CandidateTokenError):
            verify_candidate_token(f'{version}.{noncanonical}.{signature}', _API_KEY, _NOW)
