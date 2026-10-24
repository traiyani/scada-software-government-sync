"""Scan the watch folder, queue rows, and optionally POST them.

Network I/O happens only when the caller passes send=True. That flag is set
from the command line. Configuration cannot turn it on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from scada_uploader import store
from scada_uploader.reader import list_csv_files, read_csv_file, wait_until_stable
from scada_uploader.schema import API_ACCEPTED, NEEDS_ATTENTION, PENDING, UNCERTAIN
from scada_uploader.signing import assemble_body, auth_headers, sign_body, unix_timestamp
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
    inserted: int = 0
    duplicates: int = 0
    corrected: int = 0
    unchanged: int = 0
    requests: int = 0
    accepted_rows: int = 0
    uncertain_rows: int = 0
    requeued: int = 0
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
) -> RunSummary:
    summary = RunSummary(dry_run=not send)
    if requeue:
        summary.requeued = store.requeue_uncertain(conn)
        log.info("requeued %s uncertain row(s)", summary.requeued)

    _ingest(conn, cfg, summary, sleep)
    _deliver(conn, cfg, summary, send=send, transport=transport, clock=clock, sleep=sleep)
    summary.counts = store.status_counts(conn)
    return summary


def _ingest(conn, cfg, summary: RunSummary, sleep) -> None:
    files = list_csv_files(cfg.watch_folder)
    for path in files:
        summary.files_seen += 1
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
            outcome = store.upsert_reading(
                conn,
                reading_at=row.reading_at,
                source_filename=path.name,
                payload_json=row.payload_json,
                status=row.status,
                error=row.error,
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


def _deliver(conn, cfg, summary: RunSummary, *, send: bool, transport, clock, sleep) -> None:
    ready = []
    for row in store.pending_rows(conn):
        problems = errors_in_payload(row["payload_json"])
        if problems:
            conn.execute(
                "UPDATE readings SET status = ?, error = ? WHERE id = ? AND status = ?",
                (NEEDS_ATTENTION, "; ".join(problems), row["id"], PENDING),
            )
            conn.commit()
            log.info("held reading %s: %s", row["reading_at"], "; ".join(problems))
            continue
        ready.append(row)

    size = 1 if cfg.payload_mode == "single" else cfg.batch_size
    for group in _chunked(ready, size):
        payloads = [row["payload_json"] for row in group]
        body = assemble_body(payloads, cfg.payload_mode)
        ids = [row["id"] for row in group]
        if not send:
            timestamp = unix_timestamp(clock())
            signature = sign_body(cfg.shared_secret, timestamp, body)
            summary.previews.append(
                Preview(body, timestamp, signature, cfg.device_id, ids, cfg.telemetry_url)
            )
            continue
        _post_group(conn, cfg, summary, body, ids, transport, clock, sleep)


def _post_group(conn, cfg, summary: RunSummary, body: str, ids: list[int], transport, clock, sleep) -> None:
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


def attention_samples(conn, limit: int = 10) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT reading_at, error FROM readings
        WHERE status = ? ORDER BY reading_at LIMIT ?
        """,
        (NEEDS_ATTENTION, limit),
    )
    return [(row["reading_at"], row["error"] or "") for row in rows]
