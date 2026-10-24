"""Command line entry. A real POST requires --send. The default is a dry run."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from scada_uploader.config import live_send_allowed, load_config
from scada_uploader.locking import acquire
from scada_uploader.transport import post_telemetry, redact_text
from scada_uploader.uploader import attention_samples, run_cycle

log = logging.getLogger("scada_uploader")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Queue SCADA CSV readings and, only with --send, POST them to the portal."
    )
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument(
        "--send",
        action="store_true",
        help="POST pending readings to telemetry_url. Omit this for a dry run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not open a network connection. This is already the default.",
    )
    parser.add_argument(
        "--requeue-uncertain",
        action="store_true",
        help="Move uncertain rows back to pending. Does not send unless --send is also set.",
    )
    parser.add_argument(
        "--watch-folder",
        default=None,
        help="Override the watch folder for this run.",
    )
    args = parser.parse_args(argv)
    if args.send and args.dry_run:
        print("Choose either --send or --dry-run, not both.", file=sys.stderr)
        return 2

    cfg = load_config(Path(args.config))
    if args.watch_folder:
        override = Path(args.watch_folder)
        if not override.is_absolute():
            override = (Path(args.config).resolve().parent / override).resolve()
        cfg = _replace_watch(cfg, override)

    printer = _Printer(cfg.shared_secret)
    _configure_logging(cfg.log_path, cfg.shared_secret)
    if not cfg.watch_folder.is_dir():
        printer(f"Watch folder does not exist: {cfg.watch_folder}")
        return 1

    if args.send:
        refusal = live_send_allowed(cfg.telemetry_url)
        if refusal:
            printer(refusal)
            return 2

    lock = acquire(cfg.lock_path)
    if lock is None:
        printer("Another uploader instance is already running; exiting.")
        log.info("exiting because the lock is held: %s", cfg.lock_path)
        return 0

    from scada_uploader import store

    conn = None
    try:
        conn = store.connect(cfg.database_path)
        summary = run_cycle(
            conn,
            cfg,
            send=args.send,
            transport=post_telemetry,
            clock=time.time,
            sleep=time.sleep,
            requeue=args.requeue_uncertain,
        )
        _print_summary(printer, conn, cfg, summary)
    finally:
        if conn is not None:
            conn.close()
        lock.close()

    if not args.send:
        return 0
    if summary.file_errors or summary.uncertain_rows:
        return 1
    if summary.requests and summary.accepted_rows == 0:
        return 1
    return 0


def _print_summary(printer, conn, cfg, summary) -> None:
    if summary.dry_run:
        printer("Dry run: no HTTP request was sent.")
    else:
        printer("Send mode: requests were made only for pending rows that passed local checks.")
    printer(
        f"Files seen: {summary.files_seen}; skipped (empty or still being written): {summary.files_skipped}."
    )
    printer(
        "Queue changes: "
        f"inserted {summary.inserted}, duplicates {summary.duplicates}, "
        f"corrected {summary.corrected}, left unchanged {summary.unchanged}."
    )
    if summary.requeued:
        printer(f"Requeued uncertain rows: {summary.requeued}.")
    for error in summary.file_errors:
        printer(redact_text(error, cfg.shared_secret) or error)
    counts = summary.counts
    printer(
        "Database: "
        f"pending {counts.get('pending', 0)}, "
        f"api_accepted {counts.get('api_accepted', 0)} (API queued, not final storage), "
        f"needs_attention {counts.get('needs_attention', 0)}, "
        f"uncertain {counts.get('uncertain', 0)}."
    )
    if summary.dry_run:
        if summary.previews:
            printer(f"Would send {len(summary.previews)} request(s) in {cfg.payload_mode} mode.")
            _print_previews(printer, summary.previews)
        else:
            printer("Nothing to send.")
    else:
        printer(
            f"Requests: {summary.requests}; rows API-accepted this run: {summary.accepted_rows}; "
            f"rows marked uncertain: {summary.uncertain_rows}."
        )
    held = attention_samples(conn)
    if held:
        printer("Needs attention (not sent):")
        for reading_at, error in held:
            printer(f"  {reading_at}: {redact_text(error, cfg.shared_secret) or error}")
    printer(f"Queue database: {cfg.database_path}")


def _print_previews(printer, previews) -> None:
    shown = previews[:3]
    for index, preview in enumerate(shown, start=1):
        printer(f"Request {index}: POST {preview.url}")
        printer(f"  X-SCADA-Device-Id: {preview.device_id}")
        printer(f"  X-SCADA-Timestamp: {preview.timestamp}")
        printer(f"  X-SCADA-Signature: {preview.signature}")
        printer(f"  body bytes: {len(preview.body.encode('utf-8'))}")
        text = preview.body
        if len(text) > 4000:
            text = text[:4000] + "...(truncated in this display; the signature covers the full body)"
        printer(f"  body: {text}")
    if len(previews) > len(shown):
        printer(f"  ... {len(previews) - len(shown)} more request(s) not shown.")


class _Printer:
    def __init__(self, secret: str):
        self.secret = secret

    def __call__(self, text: str = "") -> None:
        if self.secret and self.secret in text:
            text = text.replace(self.secret, "[redacted]")
        print(text)


def _configure_logging(path: Path, secret: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("scada_uploader")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(_RedactFilter(secret))
    logger.addHandler(handler)


class _RedactFilter(logging.Filter):
    def __init__(self, secret: str):
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secret:
            return True
        message = record.getMessage()
        if self.secret in message:
            record.msg = message.replace(self.secret, "[redacted]")
            record.args = ()
        return True


def _replace_watch(cfg, folder: Path):
    from dataclasses import replace

    return replace(cfg, watch_folder=folder)

