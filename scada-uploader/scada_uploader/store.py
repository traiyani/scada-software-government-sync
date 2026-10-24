"""SQLite queue stored next to the app. No separate database server."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from scada_uploader.schema import API_ACCEPTED, NEEDS_ATTENTION, PENDING, UNCERTAIN

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_at TEXT NOT NULL UNIQUE,
    source_filename TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'api_accepted', 'needs_attention', 'uncertain')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    http_status INTEGER,
    error TEXT,
    batch_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_status ON readings(status);
CREATE TABLE IF NOT EXISTS file_snapshots (
    filename TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('dry_run', 'send')),
    files_seen INTEGER,
    files_unchanged INTEGER,
    files_skipped INTEGER,
    file_errors INTEGER,
    inserted INTEGER,
    requests INTEGER,
    accepted_rows INTEGER,
    uncertain_rows INTEGER,
    exit_code INTEGER
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def upsert_reading(
    conn: sqlite3.Connection,
    *,
    reading_at: str,
    source_filename: str,
    payload_json: str,
    status: str,
    error: str | None,
) -> str:
    """Insert a new measurement or refresh a row that was never accepted.

    api_accepted and uncertain rows are left alone so they are not resent.
    A needs_attention row can be replaced when a later export corrects it.
    """
    existing = conn.execute(
        "SELECT status, payload_json, error, http_status FROM readings WHERE reading_at = ?",
        (reading_at,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO readings (
                reading_at, source_filename, payload_json, status,
                attempt_count, last_attempt_at, http_status, error, batch_id
            ) VALUES (?, ?, ?, ?, 0, NULL, NULL, ?, NULL)
            """,
            (reading_at, source_filename, payload_json, status, error),
        )
        conn.commit()
        return "inserted"

    same_payload = existing["payload_json"] == payload_json
    same = same_payload and (existing["error"] or None) == error
    rejected = existing["http_status"] == 400
    if existing["status"] == NEEDS_ATTENTION and not same and not (rejected and same_payload):
        conn.execute(
            """
            UPDATE readings
            SET source_filename = ?, payload_json = ?, status = ?, error = ?,
                attempt_count = 0, last_attempt_at = NULL, http_status = NULL, batch_id = NULL
            WHERE reading_at = ? AND status = ?
            """,
            (source_filename, payload_json, status, error, reading_at, NEEDS_ATTENTION),
        )
        conn.commit()
        return "corrected"
    if same_payload:
        return "duplicate"
    return "unchanged"


def pending_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM readings WHERE status = ? ORDER BY reading_at, id",
            (PENDING,),
        )
    )


def held_rows_not_rejected(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM readings
            WHERE status = ? AND COALESCE(http_status, 0) != 400
            ORDER BY reading_at, id
            """,
            (NEEDS_ATTENTION,),
        )
    )


def record_attempt(
    conn: sqlite3.Connection,
    ids: Iterable[int],
    *,
    status: str,
    when: str,
    http_status: int | None,
    error: str | None,
    batch_id: str | None,
) -> None:
    id_list = list(ids)
    if not id_list:
        return
    conn.executemany(
        """
        UPDATE readings
        SET status = ?,
            attempt_count = attempt_count + 1,
            last_attempt_at = ?,
            http_status = ?,
            error = ?,
            batch_id = COALESCE(?, batch_id)
        WHERE id = ?
        """,
        [(status, when, http_status, error, batch_id, row_id) for row_id in id_list],
    )
    conn.commit()


def requeue_uncertain(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        UPDATE readings
        SET status = ?, error = ?
        WHERE status = ?
        """,
        (PENDING, "requeued by operator", UNCERTAIN),
    )
    conn.commit()
    return cursor.rowcount


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {PENDING: 0, API_ACCEPTED: 0, NEEDS_ATTENTION: 0, UNCERTAIN: 0}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM readings GROUP BY status"):
        counts[row["status"]] = row["n"]
    return counts


def file_snapshot(conn: sqlite3.Connection, filename: str) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT size, mtime_ns FROM file_snapshots WHERE filename = ?",
        (filename,),
    ).fetchone()
    if row is None:
        return None
    return row["size"], row["mtime_ns"]


def record_file_snapshot(conn: sqlite3.Connection, filename: str, snapshot: tuple[int, int]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO file_snapshots (filename, size, mtime_ns) VALUES (?, ?, ?)",
        (filename, snapshot[0], snapshot[1]),
    )
    conn.commit()


def latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def start_run(conn: sqlite3.Connection, *, started_at: str, mode: str) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (started_at, mode) VALUES (?, ?)",
        (started_at, mode),
    )
    conn.commit()
    return cursor.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: str,
    exit_code: int,
    files_seen: int,
    files_unchanged: int,
    files_skipped: int,
    file_errors: int,
    inserted: int,
    requests: int,
    accepted_rows: int,
    uncertain_rows: int,
) -> None:
    conn.execute(
        """
        UPDATE runs
        SET finished_at = ?, exit_code = ?, files_seen = ?, files_unchanged = ?,
            files_skipped = ?, file_errors = ?, inserted = ?, requests = ?,
            accepted_rows = ?, uncertain_rows = ?
        WHERE id = ?
        """,
        (
            finished_at,
            exit_code,
            files_seen,
            files_unchanged,
            files_skipped,
            file_errors,
            inserted,
            requests,
            accepted_rows,
            uncertain_rows,
            run_id,
        ),
    )
    conn.commit()


def get_by_reading_at(conn: sqlite3.Connection, reading_at: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM readings WHERE reading_at = ?",
        (reading_at,),
    ).fetchone()
