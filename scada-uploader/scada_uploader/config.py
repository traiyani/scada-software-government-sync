"""Load local configuration. The shared secret is never included in repr()."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from scada_uploader.mapping import resolve_timezone

DEFAULT_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d.%m.%Y",
)
DEFAULT_TIME_FORMATS = (
    "%H:%M:%S",
    "%H:%M:%S.%f",
    "%H:%M",
)


@dataclass(frozen=True)
class Config:
    watch_folder: Path
    device_id: str
    shared_secret: str
    telemetry_url: str
    timezone_name: str
    tz: object
    payload_mode: str
    batch_size: int
    max_attempts_per_run: int
    request_timeout_seconds: float
    retry_backoff_seconds: float
    file_stable_seconds: float
    file_open_retries: int
    file_open_retry_seconds: float
    database_path: Path
    log_path: Path
    lock_path: Path
    date_formats: tuple[str, ...]
    time_formats: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "Config("
            f"device_id={self.device_id!r}, "
            f"telemetry_url={self.telemetry_url!r}, "
            f"watch_folder={str(self.watch_folder)!r}, "
            f"payload_mode={self.payload_mode!r}, "
            "shared_secret='[redacted]')"
        )


def load_config(path: Path, environ: dict[str, str] | None = None) -> Config:
    path = path.resolve()
    environ = os.environ if environ is None else environ
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Config file is not valid JSON: {path} (line {exc.lineno}, column {exc.colno})"
        ) from None
    if not isinstance(raw, dict):
        raise SystemExit("Config file must be a JSON object")

    base = path.parent
    device_id = str(raw.get("device_id") or "").strip()
    secret = _load_secret(raw, base, environ)
    url = str(raw.get("telemetry_url") or "").strip()
    if not device_id:
        raise SystemExit("Config device_id is empty")
    if not secret:
        raise SystemExit("Shared secret is missing. Set shared_secret, shared_secret_file, or SCADA_SHARED_SECRET.")
    if not url.startswith(("https://", "http://")):
        raise SystemExit("telemetry_url must start with https:// or http://")

    mode = str(raw.get("payload_mode") or "single").strip().lower()
    if mode not in ("single", "batch"):
        raise SystemExit("payload_mode must be 'single' or 'batch'")
    batch_size = _positive_int(raw.get("batch_size", 20), "batch_size")
    attempts = _positive_int(raw.get("max_attempts_per_run", 3), "max_attempts_per_run")
    timeout = _positive_float(raw.get("request_timeout_seconds", 30), "request_timeout_seconds")
    backoff = _non_negative_float(raw.get("retry_backoff_seconds", 1), "retry_backoff_seconds")
    stable = _non_negative_float(raw.get("file_stable_seconds", 1), "file_stable_seconds")
    open_retries = _positive_int(raw.get("file_open_retries", 3), "file_open_retries")
    open_delay = _non_negative_float(raw.get("file_open_retry_seconds", 0.5), "file_open_retry_seconds")

    timezone_name = str(raw.get("timezone") or "Asia/Kolkata").strip()
    offset_text = str(raw.get("utc_offset") or "+05:30").strip()
    try:
        tzinfo = resolve_timezone(timezone_name, offset_text)
    except Exception as exc:
        raise SystemExit(f"Could not resolve timezone {timezone_name!r}: {exc}") from exc

    database_path = _resolve(base, str(raw.get("database_path") or "scada_uploader.sqlite"))
    log_path = _resolve(base, str(raw.get("log_path") or "scada_uploader.log"))
    lock_path = _resolve(base, str(raw.get("lock_path") or "scada_uploader.lock"))
    watch_folder = _resolve(base, str(raw.get("watch_folder") or "incoming"))
    date_formats = tuple(raw.get("date_formats") or DEFAULT_DATE_FORMATS)
    time_formats = tuple(raw.get("time_formats") or DEFAULT_TIME_FORMATS)

    return Config(
        watch_folder=watch_folder,
        device_id=device_id,
        shared_secret=secret,
        telemetry_url=url,
        timezone_name=timezone_name,
        tz=tzinfo,
        payload_mode=mode,
        batch_size=batch_size,
        max_attempts_per_run=attempts,
        request_timeout_seconds=timeout,
        retry_backoff_seconds=backoff,
        file_stable_seconds=stable,
        file_open_retries=open_retries,
        file_open_retry_seconds=open_delay,
        database_path=database_path,
        log_path=log_path,
        lock_path=lock_path,
        date_formats=date_formats,
        time_formats=time_formats,
    )


def live_send_allowed(url: str) -> str | None:
    """Return an error string when the URL is still an example placeholder."""
    lowered = url.lower()
    if "your_portal_host" in lowered or "example.invalid" in lowered:
        return "Refusing to send: telemetry_url is still a placeholder."
    return None


def _load_secret(raw: dict, base: Path, environ: dict[str, str]) -> str:
    from_env = str(environ.get("SCADA_SHARED_SECRET") or "")
    if from_env.strip():
        return from_env.strip()
    file_value = str(raw.get("shared_secret_file") or "").strip()
    if file_value:
        secret_path = _resolve(base, file_value)
        try:
            text = secret_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError as exc:
            raise SystemExit(f"Shared secret file not found: {secret_path}") from exc
        return text.strip()
    return str(raw.get("shared_secret") or "").strip()


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _positive_int(value, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if number < 1:
        raise SystemExit(f"{name} must be >= 1")
    return number


def _positive_float(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{name} must be a number") from exc
    if number <= 0:
        raise SystemExit(f"{name} must be > 0")
    return number


def _non_negative_float(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{name} must be a number") from exc
    if number < 0:
        raise SystemExit(f"{name} must be >= 0")
    return number
