"""HMAC-SHA256 request signing from Integration Guide V1.3, sections 4 and 8.

Signature = hex(HMAC-SHA256(key=shared secret, message=timestamp + "." + body))
The body is the exact compact JSON posted, with no extra whitespace.
The hex digest is lowercase. The timestamp is Unix seconds at send time,
not the SCADA readingAt value.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Mapping, Sequence

from scada_uploader.schema import FIELD_ORDER, NUMERIC_FIELDS

HEADER_DEVICE = "X-SCADA-Device-Id"
HEADER_TIMESTAMP = "X-SCADA-Timestamp"
HEADER_SIGNATURE = "X-SCADA-Signature"
CONTENT_TYPE = "application/json"


def unix_timestamp(now_seconds: float) -> str:
    """Header value. Must be within ±300 seconds of the server clock."""
    return str(int(now_seconds))


def render_payload(
    reading_at: str,
    number_literals: Mapping[str, str],
    omit_fields: Sequence[str] = (),
) -> str:
    """Compact one-object body. Literals are already valid JSON numbers."""
    omit = set(omit_fields)
    required = [name for name in NUMERIC_FIELDS if name not in omit]
    missing = [name for name in required if name not in number_literals]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    parts = [json.dumps("readingAt") + ":" + json.dumps(reading_at)]
    for name in required:
        literal = number_literals[name]
        if not _is_json_number(literal):
            raise ValueError(f"unsafe number literal for {name}")
        parts.append(json.dumps(name) + ":" + literal)
    return "{" + ",".join(parts) + "}"


def payload_without_fields(payload: str, omit_fields: Sequence[str]) -> str:
    """Drop omitted keys from a stored compact object, keeping number literals."""
    omit = set(omit_fields)
    if not omit:
        return payload
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if not isinstance(data, dict) or not isinstance(data.get("readingAt"), str):
        return payload
    literals = {}
    for name in NUMERIC_FIELDS:
        if name in omit or name not in data:
            continue
        literals[name] = _compact_number_literal(payload, name)
    return render_payload(data["readingAt"], literals, omit_fields=omit_fields)


def assemble_body(object_payloads: Sequence[str], mode: str) -> str:
    """Section 5 option A is one object. Option B is a JSON array of objects."""
    if mode == "single":
        if len(object_payloads) != 1:
            raise ValueError("single payload mode sends one reading per request")
        return object_payloads[0]
    if mode == "batch":
        return "[" + ",".join(object_payloads) + "]"
    raise ValueError(f"unsupported payload mode: {mode}")


def signing_message(timestamp: str, body: str) -> bytes:
    return f"{timestamp}.{body}".encode("utf-8")


def sign_body(secret: str, timestamp: str, body: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        signing_message(timestamp, body),
        hashlib.sha256,
    )
    return digest.hexdigest()


def auth_headers(device_id: str, timestamp: str, signature: str) -> dict[str, str]:
    return {
        HEADER_DEVICE: device_id,
        HEADER_TIMESTAMP: timestamp,
        HEADER_SIGNATURE: signature,
        "Content-Type": CONTENT_TYPE,
    }


def _compact_number_literal(payload: str, name: str) -> str:
    key = json.dumps(name) + ":"
    start = payload.find(key)
    if start < 0:
        raise ValueError(f"missing {name}")
    start += len(key)
    end = start
    while end < len(payload) and payload[end] not in ",}":
        end += 1
    return payload[start:end]


def _is_json_number(literal: str) -> bool:
    if not literal or any(ch.isspace() for ch in literal):
        return False
    try:
        parsed = json.loads(literal)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, (int, float)) and not isinstance(parsed, bool)
