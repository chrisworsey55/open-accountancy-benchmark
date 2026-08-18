"""Fail-closed redaction for untrusted provider and persisted result content."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence


class SanitizationError(ValueError):
    """Untrusted content cannot be represented safely in a persisted artifact."""


_SECRET_KEY_MARKERS = (
    "apikey",
    "apitoken",
    "authorization",
    "credential",
    "password",
    "secret",
    "accesstoken",
    "refreshtoken",
    "privatekey",
    "clientsecret",
    "cookie",
    "certificate",
    "pem",
)
_PEM_BLOCK = re.compile(
    r"-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)[^-]*-----.*?-----END [^-]*-----",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_TOKEN = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_KEY_VALUE = re.compile(
    r"\b(?:token|id[ _-]?token|api[ _-]?key|private[ _-]?key|password|authorization|"
    r"client[ _-]?secret|access[ _-]?token|refresh[ _-]?token|cookie|"
    r"certificate|pem)\b\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_SECRET_TOKEN = re.compile(
    r"\b\S*(?:api[ _-]?key|private[ _-]?key|password|authorization|"
    r"client[ _-]?secret|access[ _-]?token|refresh[ _-]?token|cookie|"
    r"certificate|pem)\S*\b",
    re.IGNORECASE,
)


def _serialized_json(value: str) -> object | None:
    """Parse a complete JSON container only; prose is never treated as JSON."""

    stripped = value.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping | list) else None


def is_secret_key(value: str) -> bool:
    """Recognise normalized credential-shaped mapping keys, without values."""

    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", value).lower()
        if character.isalnum()
    )
    return normalized == "token" or any(
        marker in normalized for marker in _SECRET_KEY_MARKERS
    )


def sanitize_text(value: str) -> str:
    """Redact common credential values while leaving ordinary fictional evidence intact."""

    parsed = _serialized_json(value)
    if parsed is not None:
        try:
            return json.dumps(
                sanitize_for_persistence(parsed),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise SanitizationError(
                "serialized JSON cannot be sanitized safely"
            ) from error

    sanitized = _PEM_BLOCK.sub("[redacted]", value)
    sanitized = _BEARER_TOKEN.sub("Bearer [redacted]", sanitized)
    sanitized = _KEY_VALUE.sub("[redacted]", sanitized)
    return _SECRET_TOKEN.sub("[redacted]", sanitized)


def sanitize_for_persistence(value: object) -> object:
    """Recursively produce JSON-safe, secret-free content or fail closed."""

    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise SanitizationError("persisted mapping keys must be strings")
            sanitized[raw_key] = (
                "[redacted]"
                if is_secret_key(raw_key)
                else sanitize_for_persistence(item)
            )
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [sanitize_for_persistence(item) for item in value]
    raise SanitizationError(
        f"untrusted content of type {type(value).__name__} cannot be persisted"
    )


def canonical_sanitized_json(value: object) -> bytes:
    """Encode one fully sanitized JSON value deterministically."""

    try:
        return (
            json.dumps(
                sanitize_for_persistence(value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, SanitizationError):
            raise
        raise SanitizationError(
            "untrusted content cannot be serialized safely"
        ) from error


def sanitize_transcript_bytes(data: bytes) -> bytes:
    """Require JSONL transcript records to already be canonical and secret-free."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SanitizationError("transcript is not valid UTF-8") from error
    lines = text.splitlines()
    canonical: list[bytes] = []
    for line in lines:
        if not line:
            raise SanitizationError("transcript contains an empty record")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SanitizationError("transcript contains malformed JSON") from error
        if not isinstance(record, Mapping):
            raise SanitizationError("transcript records must be JSON objects")
        canonical.append(canonical_sanitized_json(record))
    return b"".join(canonical)
