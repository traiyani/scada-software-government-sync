"""Shared fixtures. Uses a stand-in secret, never the plant key."""

from __future__ import annotations

import csv
from datetime import timedelta, timezone
from pathlib import Path

from scada_uploader.config import Config
from scada_uploader.schema import SCADA_COLUMNS

HEADER = ["Date", "Time", *[name for name, _field in SCADA_COLUMNS]]

VALID_NUMBERS = [
    "3.2",
    "96.8",
    "1.9",
    "7.4",
    "3.6",
    "2.8",
    "0.3",
    "25.4",
    "120.5",
    "110.2",
    "450.75",
    "12548750.75",
    "11548750.11",
    "15548750.09",
    "15548750.03",
]


def make_config(root: Path, **overrides) -> Config:
    incoming = root / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    values = dict(
        watch_folder=incoming,
        device_id="TEST_DEVICE",
        shared_secret="unit-test-secret",
        telemetry_url="http://127.0.0.1:9/api/v1/public/scada/telemetry",
        timezone_name="Asia/Kolkata",
        tz=timezone(timedelta(hours=5, minutes=30)),
        payload_mode="single",
        batch_size=20,
        max_attempts_per_run=3,
        request_timeout_seconds=2,
        retry_backoff_seconds=0,
        file_stable_seconds=0,
        file_open_retries=1,
        file_open_retry_seconds=0,
        database_path=root / "queue.sqlite",
        log_path=root / "uploader.log",
        lock_path=root / "uploader.lock",
        date_formats=("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y"),
        time_formats=("%H:%M:%S", "%H:%M:%S.%f", "%H:%M"),
    )
    values.update(overrides)
    return Config(**values)


def read_logs(root: Path, stem: str = "uploader") -> str:
    return "".join(path.read_text(encoding="utf-8") for path in sorted(root.glob(f"{stem}-*.log")))


def write_csv(path: Path, data_rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(data_rows)


def valid_row(date: str = "2026-10-23", time_text: str = "13:45:02", **replacements: str) -> list[str]:
    values = dict(zip((field for _name, field in SCADA_COLUMNS), VALID_NUMBERS))
    values.update(replacements)
    ordered = [values[field] for _name, field in SCADA_COLUMNS]
    return [date, time_text, *ordered]
