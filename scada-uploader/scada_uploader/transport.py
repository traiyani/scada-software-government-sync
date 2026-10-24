"""HTTP POST of an already-signed body.

Uses http.client rather than urllib.request. On Python 3.9, urllib applies
str.title() to header names, so the guide's X-SCADA-Device-Id would be sent
as X-Scada-Device-Id. http.client writes the names unchanged. Both modules
are in the standard library.

Redirects are not followed.
"""

from __future__ import annotations

import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

import http.client


@dataclass(frozen=True)
class PostResult:
    # accepted: HTTP 202, queued by the API
    # bad_request: HTTP 400, validation rejection
    # unauthorized: HTTP 401, signature or identity rejected; one try per run
    # transient: HTTP 429 or 5xx, retried inside this run
    # connection: request was not sent (refused, DNS, timeout before send)
    # timeout: request may have left the machine; not retried automatically
    # uncertain: HTTP 2xx other than 202; not retried automatically
    # other: unexpected status, left pending for a later run
    outcome: str
    http_status: int | None
    message: str | None
    batch_id: str | None


def post_telemetry(url: str, body: str, headers: dict[str, str], timeout: float) -> PostResult:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        return PostResult("other", None, "telemetry_url must be http or https", None)
    host = parts.hostname
    if not host:
        return PostResult("other", None, "telemetry_url has no host", None)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = path + "?" + parts.query
    payload = body.encode("utf-8")
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        try:
            connection.connect()
        except (TimeoutError, socket.timeout) as exc:
            return PostResult("connection", None, f"timed out before the request was sent: {exc}", None)
        except (ConnectionRefusedError, socket.gaierror, ConnectionAbortedError, ssl.SSLError, OSError) as exc:
            return PostResult("connection", None, f"connection failed before the request was sent: {exc}", None)

        try:
            connection.request("POST", path, body=payload, headers=dict(headers))
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (TimeoutError, socket.timeout) as exc:
            return PostResult("timeout", None, f"timed out after connecting; delivery uncertain: {exc}", None)
        except (
            http.client.HTTPException,
            ConnectionResetError,
            BrokenPipeError,
            ssl.SSLError,
            OSError,
        ) as exc:
            return PostResult("timeout", None, f"connection lost after send; delivery uncertain: {exc}", None)
    finally:
        connection.close()

    text = raw.decode("utf-8", errors="replace")
    return interpret_response(status, text)


def interpret_response(status: int, text: str) -> PostResult:
    message = _response_message(text)
    if status == 202:
        batch_id = None
        note = None
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = None
            note = "202 Accepted but response was not JSON"
        if isinstance(data, dict):
            raw_id = data.get("batchId")
            if isinstance(raw_id, str) and raw_id:
                batch_id = raw_id
            else:
                note = "202 Accepted but response had no batchId"
        if note and message:
            note = note + "; " + message
        return PostResult("accepted", status, note, batch_id)
    if status == 400:
        return PostResult("bad_request", status, message or "HTTP 400", None)
    if status == 401:
        return PostResult("unauthorized", status, message or "HTTP 401", None)
    if status == 408:
        return PostResult("timeout", status, message or "HTTP 408", None)
    if status == 429 or status >= 500:
        return PostResult("transient", status, message or f"HTTP {status}", None)
    if 200 <= status < 300:
        # Section 6 defines success as 202 Accepted (queued), not a generic 2xx.
        # Leave the row uncertain so a later run does not post it again.
        return PostResult(
            "uncertain",
            status,
            f"HTTP {status} is not 202 Accepted; not retrying automatically",
            None,
        )
    return PostResult("other", status, message or f"HTTP {status}", None)


def redact_text(text: str | None, secret: str) -> str | None:
    if text is None or not secret:
        return text
    if secret not in text:
        return text
    return text.replace(secret, "[redacted]")


def _response_message(text: str) -> str | None:
    clipped = " ".join(text.split())
    if len(clipped) > 500:
        clipped = clipped[:500] + "..."
    if not clipped:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return clipped
    if isinstance(data, dict):
        detail = data.get("message") or data.get("error")
        if detail:
            detail_text = " ".join(str(detail).split())
            if len(detail_text) > 500:
                detail_text = detail_text[:500] + "..."
            return detail_text
    return clipped
