"""Scan the watch folder, queue rows, and optionally POST them.

Network I/O happens only when the caller passes send=True. That flag is set
from the command line. Configuration cannot turn it on.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from scada_uploader import store
from scada_uploader.mapping import map_row, normalize_header
from scada_uploader.reader import list_csv_files, read_csv_file, snapshot, wait_until_stable
from scada_uploader.schema import API_ACCEPTED, NEEDS_ATTENTION, PENDING, UNCERTAIN
from scada_uploader.signing import assemble_body, auth_headers, payload_without_fields, sign_body, unix_timestamp
from scada_uploader.transport import redact_text
from scada_uploader.validate import errors_in_payload

log = logging.getLogger("scada_uploader")


@dataclass
class Preview:
    body: str
    timestamp: str
    signature: str
    device_id: str
    reading_ids: list[int]
    url: str


@dataclass
class RunSummary:
    dry_run: bool
    files_seen: int = 0
    files_skipped: int = 0
    files_unchanged: int = 0
    inserted: int = 0
    duplicates: int = 0
    corrected: int = 0
    unchanged: int = 0
    requests: int = 0
    accepted_rows: int = 0
    uncertain_rows: int = 0
    requeued: int = 0
    requeued_attention: int = 0
    released: int = 0
    new_attention: int = 0
    file_errors: list[str] = field(default_factory=list)
    previews: list[Preview] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def run_cycle(
    conn,
    cfg,
    *,
    send: bool,
    transport,
    clock,
    sleep,
    requeue: bool = False,
    requeue_attention: bool = False,
    attention_contains: str | None = None,
) -> RunSummary:
    summary = RunSummary(dry_run=not send)
    if cfg.omit_fields:
        log.warning(
            "omitted fields: %s (not sent; portal may still require them)",
            ", ".join(cfg.omit_fields),
        )
    if requeue:
        summary.requeued = store.requeue_uncertain(conn)
        log.info("requeued %s uncertain row(s)", summary.requeued)
    if requeue_attention:
        summary.requeued_attention = store.requeue_attention(conn, attention_contains)
        log.info(
            "requeued %s needs_attention row(s)%s",
            summary.requeued_attention,
            f" matching {attention_contains!r}" if attention_contains else "",
        )

    _ingest(conn, cfg, summary, sleep)
    _deliver(conn, cfg, summary, send=send, transport=transport, clock=clock, sleep=sleep)
    summary.counts = store.status_counts(conn)
    return summary


def _ingest(conn, cfg, summary: RunSummary, sleep) -> None:
    files = list_csv_files(cfg.watch_folder)
    for path in files:
        summary.files_seen += 1
        try:
            current = snapshot(path)
        except OSError:
            current = None
        if current is not None and current == store.file_snapshot(conn, path.name):
            summary.files_unchanged += 1
            continue
        if not wait_until_stable(path, cfg.file_stable_seconds, sleep):
            summary.files_skipped += 1
            log.info("skipped %s (empty or still being written)", path.name)
            continue
        parsed = read_csv_file(
            path,
            tzinfo=cfg.tz,
            date_formats=cfg.date_formats,
            time_formats=cfg.time_formats,
            retries=cfg.file_open_retries,
            retry_delay=cfg.file_open_retry_seconds,
            sleep=sleep,
            omit_fields=cfg.omit_fields,
            check_ranges=cfg.validate_ranges,
        )
        if parsed.skipped_unstable:
            summary.files_skipped += 1
            log.info("%s", parsed.error)
            continue
        if parsed.error and not parsed.rows:
            summary.file_errors.append(parsed.error)
            log.error("%s", parsed.error)
            continue
        for row in parsed.rows:
            status, error = row.status, row.error
            if status == NEEDS_ATTENTION and not errors_in_payload(
                row.payload_json, check_ranges=cfg.validate_ranges, omit_fields=cfg.omit_fields
            ):
                status, error = PENDING, None
            outcome = store.upsert_reading(
                conn,
                reading_at=row.reading_at,
                source_filename=path.name,
                payload_json=row.payload_json,
                status=status,
                error=error,
            )
            if outcome == "inserted":
                summary.inserted += 1
            elif outcome == "corrected":
                summary.corrected += 1
                log.info("corrected %s from %s", row.reading_at, path.name)
            elif outcome == "unchanged":
                summary.unchanged += 1
                log.warning(
                    "left %s unchanged; a stored reading with a different payload was not replaced",
                    row.reading_at,
                )
            else:
                summary.duplicates += 1
            if outcome in ("inserted", "corrected") and status == NEEDS_ATTENTION:
                summary.new_attention += 1
                log.info("held reading %s: %s", row.reading_at, error)
        if parsed.error is None and parsed.snapshot is not None:
            store.record_file_snapshot(conn, path.name, parsed.snapshot)


def _release_now_valid_holds(conn, cfg, summary: RunSummary) -> None:
    for row in store.held_rows_not_rejected(conn):
        wire, problems = _sendable_payload(row["payload_json"], cfg)
        if problems or wire is None:
            continue
        cursor = conn.execute(
            "UPDATE readings SET status = ?, error = NULL WHERE id = ? AND status = ?",
            (PENDING, row["id"], NEEDS_ATTENTION),
        )
        summary.released += cursor.rowcount
    conn.commit()
    if summary.released:
        log.info("released %s held row(s) that now pass local checks", summary.released)


def _sendable_payload(payload: str, cfg) -> tuple[str | None, list[str]]:
    problems = errors_in_payload(
        payload, check_ranges=cfg.validate_ranges, omit_fields=cfg.omit_fields
    )
    if not problems:
        return payload_without_fields(payload, cfg.omit_fields), []
    remapped = _remap_source_row(payload, cfg)
    if remapped is not None:
        return remapped, []
    return None, problems


def _remap_source_row(payload: str, cfg) -> str | None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    cells = data.get("sourceRow")
    if not isinstance(cells, dict):
        return None
    row_cells = {str(key): "" if value is None else str(value) for key, value in cells.items()}
    headers = {normalize_header(key): key for key in row_cells}
    mapped = map_row(
        row_cells,
        line_number=0,
        source_name="stored",
        tzinfo=cfg.tz,
        date_formats=cfg.date_formats,
        time_formats=cfg.time_formats,
        original_headers=headers,
        omit_fields=cfg.omit_fields,
        check_ranges=cfg.validate_ranges,
    )
    if mapped.status != PENDING:
        return None
    return mapped.payload_json


def _deliver(conn, cfg, summary: RunSummary, *, send: bool, transport, clock, sleep) -> None:
    _release_now_valid_holds(conn, cfg, summary)
    ready = []
    for row in store.pending_rows(conn):
        wire, problems = _sendable_payload(row["payload_json"], cfg)
        if problems or wire is None:
            conn.execute(
                "UPDATE readings SET status = ?, error = ? WHERE id = ? AND status = ?",
                (NEEDS_ATTENTION, "; ".join(problems), row["id"], PENDING),
            )
            conn.commit()
            summary.new_attention += 1
            log.info("held reading %s: %s", row["reading_at"], "; ".join(problems))
            continue
        ready.append((row, wire))

    size = 1 if cfg.payload_mode == "single" else cfg.batch_size
    for group in _chunked(ready, size):
        payloads = [wire for _row, wire in group]
        body = assemble_body(payloads, cfg.payload_mode)
        ids = [row["id"] for row, _wire in group]
        reading_ats = [row["reading_at"] for row, _wire in group]
        if not send:
            timestamp = unix_timestamp(clock())
            signature = sign_body(cfg.shared_secret, timestamp, body)
            summary.previews.append(
                Preview(body, timestamp, signature, cfg.device_id, ids, cfg.telemetry_url)
            )
            continue
        _post_group(conn, cfg, summary, body, ids, reading_ats, transport, clock, sleep)


def _post_group(
    conn, cfg, summary: RunSummary, body: str, ids: list[int], reading_ats: list[str], transport, clock, sleep
) -> None:
    summary.requests += 1
    attempts = cfg.max_attempts_per_run
    for attempt in range(attempts):
        timestamp = unix_timestamp(clock())
        signature = sign_body(cfg.shared_secret, timestamp, body)
        headers = auth_headers(cfg.device_id, timestamp, signature)
        if attempt == 0:
            summary.previews.append(
                Preview(body, timestamp, signature, cfg.device_id, list(ids), cfg.telemetry_url)
            )
        result = transport(cfg.telemetry_url, body, headers, cfg.request_timeout_seconds)
        message = redact_text(result.message, cfg.shared_secret)
        when = _attempt_time(clock, cfg.tz)
        retry = result.outcome in ("connection", "transient") and attempt + 1 < attempts
        status = PENDING if retry else _status_for(result.outcome)
        store.record_attempt(
            conn,
            ids,
            status=status,
            when=when,
            http_status=result.http_status,
            error=message,
            batch_id=result.batch_id if status == API_ACCEPTED else None,
        )
        log.info(
            "request rows=%s http=%s outcome=%s status=%s",
            ids,
            result.http_status,
            result.outcome,
            status,
        )
        if retry:
            sleep(min(cfg.retry_backoff_seconds * (2**attempt), 8))
            continue
        if status == API_ACCEPTED:
            summary.accepted_rows += len(ids)
        elif status == UNCERTAIN:
            summary.uncertain_rows += len(ids)
        elif status == NEEDS_ATTENTION:
            summary.new_attention += len(ids)
            reason = _clip(message or "no error message", 300)
            for reading_at in reading_ats:
                log.info("rejected by portal %s http=%s: %s", reading_at, result.http_status, reason)
        return


def _status_for(outcome: str) -> str:
    if outcome == "accepted":
        return API_ACCEPTED
    if outcome == "bad_request":
        return NEEDS_ATTENTION
    if outcome in ("timeout", "uncertain"):
        return UNCERTAIN
    return PENDING


def _attempt_time(clock, tzinfo) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(clock(), tzinfo).isoformat(timespec="seconds")


def _chunked(items, size: int):
    if size < 1:
        raise ValueError("batch size must be >= 1")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def attention_rows(conn) -> list:
    return list(
        conn.execute(
            """
            SELECT reading_at, http_status, source_filename, error FROM readings
            WHERE status = ? ORDER BY reading_at, id
            """,
            (NEEDS_ATTENTION,),
        )
    )
