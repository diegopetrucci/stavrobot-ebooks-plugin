"""Signed candidate request tokens shared by search and request tools.

A token carries only the bounded lookup term and the exact Bookshelf
``foreignBookId`` needed to revalidate a candidate.  It is deliberately
opaque to callers: the API key is used only to derive the signing key and is
never serialized into the token.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import time
from typing import Any, Mapping

TOKEN_VERSION = 1
TOKEN_TTL_SECONDS = 24 * 60 * 60
MAX_LOOKUP_TERM_LENGTH = 200
MAX_FOREIGN_BOOK_ID = (1 << 63) - 1
MAX_TOKEN_LENGTH = 4096

# HMAC(api-key, domain) gives this token family an independent signing key.  A
# different plugin or token purpose cannot accidentally reuse the API key as a
# signing key without opting into this exact domain string.
_SIGNING_DOMAIN = b"stavrobot-ebooks:candidate-request-token:v1"
_TOKEN_PREFIX = "v1"
_MAX_PAYLOAD_SEGMENT_LENGTH = 2048
_MAX_SIGNATURE_SEGMENT_LENGTH = 128
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CLAIM_KEYS = frozenset(
    {"version", "issued_at", "expires_at", "term", "foreignBookId"}
)


class CandidateTokenError(ValueError):
    """Raised when a candidate request token is invalid or expired."""


def _error(message: str = "invalid candidate request token") -> CandidateTokenError:
    # Keep failures fixed and secret-free.  In particular, never interpolate
    # the token, API key, or upstream resource into an exception.
    return CandidateTokenError(message)


def _signing_key(api_key: str) -> bytes:
    if not isinstance(api_key, str) or not api_key.strip():
        raise _error("invalid candidate token signing key")
    return hmac.new(
        api_key.strip().encode("utf-8"), _SIGNING_DOMAIN, hashlib.sha256
    ).digest()


def _epoch_seconds(value: Any) -> int:
    """Convert an injectable clock value to non-negative whole epoch seconds."""
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        value = parsed.timestamp()
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("invalid candidate token clock")

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _error("invalid candidate token clock") from exc
    if not math.isfinite(number) or number < 0:
        raise _error("invalid candidate token clock")
    return int(number)


def _now_seconds(now: Any | None) -> int:
    if now is None:
        value: Any = time.time()
    elif callable(now):
        value = now()
    else:
        value = now
    return _epoch_seconds(value)


def _validate_term(term: Any) -> str:
    if not isinstance(term, str):
        raise _error("invalid candidate token term")
    bounded = term.strip()
    if not bounded or len(bounded) > MAX_LOOKUP_TERM_LENGTH:
        raise _error("invalid candidate token term")
    return bounded


def validate_foreign_book_id(foreign_book_id: Any) -> int:
    """Require an exact positive integer within Bookshelf's bounded ID domain."""
    # Do not coerce strings or booleans. Search normalizes Bookshelf's decimal
    # string representation before calling this shared validator, while token
    # issue and verification both require the canonical integer claim.
    if (
        isinstance(foreign_book_id, bool)
        or not isinstance(foreign_book_id, int)
        or not 1 <= foreign_book_id <= MAX_FOREIGN_BOOK_ID
    ):
        raise _error("invalid candidate token foreignBookId")
    return foreign_book_id


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _error() from exc


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: Any, *, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _error()
    # Padding is intentionally forbidden so every byte sequence has one
    # canonical token representation.
    if _B64URL_RE.fullmatch(value) is None:
        raise _error()
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise _error() from exc
    if _encode_base64url(decoded) != value:
        raise _error()
    return decoded


def issue_candidate_token(
    api_key: str,
    term: str,
    foreign_book_id: int,
    now: Any | None = None,
    *,
    clock: Any | None = None,
) -> str:
    """Issue a versioned 24-hour token for one exact search candidate.

    ``term`` is normalized in the same way as ``search_books`` (trimmed and
    bounded to 200 characters).  ``foreign_book_id`` is intentionally strict:
    a numeric string, zero, negative value, or boolean is not accepted.
    ``now`` may be an epoch value or timezone-aware/naive ``datetime`` for
    deterministic tests; omitted values use the current epoch clock.
    """
    if clock is not None:
        if now is not None:
            raise _error("candidate token clock specified twice")
        now = clock
    signing_key = _signing_key(api_key)
    bounded_term = _validate_term(term)
    book_id = validate_foreign_book_id(foreign_book_id)
    issued_at = _now_seconds(now)
    expires_at = issued_at + TOKEN_TTL_SECONDS
    claims = {
        "version": TOKEN_VERSION,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "term": bounded_term,
        "foreignBookId": book_id,
    }
    payload = _encode_base64url(_canonical_json(claims))
    if len(payload) > _MAX_PAYLOAD_SEGMENT_LENGTH:
        raise _error()
    signature = hmac.new(
        signing_key, payload.encode("ascii"), hashlib.sha256
    ).digest()
    token = f"{_TOKEN_PREFIX}.{payload}.{_encode_base64url(signature)}"
    if len(token.encode("utf-8")) > MAX_TOKEN_LENGTH:
        raise _error()
    return token


def _parse_claims(payload: bytes) -> dict[str, Any]:
    if len(payload) > 2048:
        raise _error()
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error() from exc
    if not isinstance(decoded, dict) or set(decoded) != _CLAIM_KEYS:
        raise _error()

    version = decoded.get("version")
    issued_at = decoded.get("issued_at")
    expires_at = decoded.get("expires_at")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != TOKEN_VERSION
        or isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
        or issued_at < 0
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at < 0
        or expires_at - issued_at != TOKEN_TTL_SECONDS
    ):
        raise _error("unsupported candidate token version")

    term = _validate_term(decoded.get("term"))
    book_id = validate_foreign_book_id(decoded.get("foreignBookId"))
    # Return a fresh bounded dictionary rather than retaining any decoded
    # object or upstream data supplied by a caller.
    return {
        "version": version,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "term": term,
        "foreignBookId": book_id,
    }


def verify_candidate_token(
    token: str,
    api_key: str,
    now: Any | None = None,
    *,
    clock: Any | None = None,
) -> dict[str, Any]:
    """Verify signature, schema, version, and 24-hour expiry of ``token``.

    Returns only the five bounded claims needed by the future request tool.
    All malformed, tampered, wrong-key, wrong-version, and expired values use
    ``CandidateTokenError`` with secret-free messages.
    """
    if clock is not None:
        if now is not None:
            raise _error("candidate token clock specified twice")
        now = clock
    if not isinstance(token, str):
        raise _error()
    try:
        token_length = len(token.encode("utf-8"))
    except UnicodeError as exc:
        raise _error() from exc
    if token_length > MAX_TOKEN_LENGTH:
        raise _error()
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        raise _error("unsupported candidate token version")

    payload_segment, signature_segment = parts[1], parts[2]
    payload = _decode_base64url(
        payload_segment,
        maximum=_MAX_PAYLOAD_SEGMENT_LENGTH,
    )
    signature = _decode_base64url(
        signature_segment,
        maximum=_MAX_SIGNATURE_SEGMENT_LENGTH,
    )
    expected_length = hashlib.sha256().digest_size
    if len(signature) != expected_length:
        raise _error()

    signing_key = _signing_key(api_key)
    expected_signature = hmac.new(
        signing_key, payload_segment.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected_signature, signature):
        raise _error()

    claims = _parse_claims(payload)
    # Require canonical JSON in addition to authenticating the bytes.  This
    # avoids multiple valid encodings for the same signed claims.
    canonical_segment = _encode_base64url(_canonical_json(claims))
    if not hmac.compare_digest(canonical_segment, payload_segment):
        raise _error()

    current = _now_seconds(now)
    if current < claims["issued_at"]:
        raise _error("candidate request token is not yet valid")
    if current >= claims["expires_at"]:
        raise _error("candidate request token has expired")
    return claims


# Short aliases keep the shared module convenient for sibling tools while the
# explicit names above document the public contract.
def issue_request_token(
    api_key: str,
    lookup_term: str,
    foreign_book_id: int,
    now: Any | None = None,
    *,
    clock: Any | None = None,
) -> str:
    return issue_candidate_token(
        api_key,
        lookup_term,
        foreign_book_id,
        now,
        clock=clock,
    )


def verify_request_token(
    token: str,
    api_key: str,
    now: Any | None = None,
    *,
    clock: Any | None = None,
) -> dict[str, Any]:
    return verify_candidate_token(token, api_key, now, clock=clock)


issue_token = issue_candidate_token
verify_token = verify_candidate_token
create_candidate_token = issue_candidate_token
