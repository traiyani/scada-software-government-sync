import csv
import io
import json
import logging
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from scada_uploader import store
from scada_uploader.cli import main
from scada_uploader.locking import acquire
from scada_uploader.transport import PostResult
from scada_uploader.uploader import run_cycle
from tests.helpers import make_config, valid_row, write_csv

CLOCK = lambda: 1780656091  # noqa: E731
LOW = "2026-10-23T13:45:02+05:30"
SECRET = "unit-test-secret-value"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def captured(fn):
    logger = logging.getLogger("scada_uploader")
    handler = _Capture()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        result = fn()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return result, handler.lines


def write_config(root: Path) -> Path:
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "watch_folder": "incoming",
                "device_id": "TEST_DEVICE",
                "shared_secret": SECRET,
                "telemetry_url": "http://127.0.0.1:9/api/v1/public/scada/telemetry",
                "timezone": "Asia/Kolkata",
                "file_stable_seconds": 0,
                "database_path": "queue.sqlite",
                "log_path": "uploader.log",
                "lock_path": "uploader.lock",
            }
        ),
        encoding="utf-8",
    )
    (root / "incoming").mkdir(exist_ok=True)
    return config_path


def run_main(argv) -> tuple[int, str]:
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            code = main(argv)
    finally:
        logger = logging.getLogger("scada_uploader")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
    return code, buffer.getvalue()


class HoldLoggingTests(unittest.TestCase):
    def test_local_hold_is_logged_once_and_counted_as_new(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            path = cfg.watch_folder / "day.csv"
            write_csv(path, [valid_row(methane_mole_percent="0")])
            conn = store.connect(cfg.database_path)
            try:
                dry = lambda: run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=lambda _s: None)  # noqa: E731
                first, first_lines = captured(dry)
                second, second_lines = captured(dry)
                with path.open("a", encoding="utf-8", newline="") as handle:
                    csv.writer(handle).writerow(valid_row(time_text="14:00:02", oxygen_mole_percent="0.9"))
                third, third_lines = captured(dry)
            finally:
                conn.close()
        held = lambda lines: [line for line in lines if line.startswith("held reading")]  # noqa: E731
        self.assertEqual(held(first_lines), [f"held reading {LOW}: methane_mole_percent must be >= 95.0 and <= 100.0 (got 0)"])
        self.assertEqual(first.new_attention, 1)
        self.assertEqual(held(second_lines), [])
        self.assertEqual(second.new_attention, 0)
        self.assertEqual(len(held(third_lines)), 1)
        self.assertIn("2026-10-23T14:00:02+05:30", held(third_lines)[0])
        self.assertEqual(third.duplicates, 1)
        self.assertEqual(third.new_attention, 1)

    def test_portal_400_logs_the_redacted_clipped_message_per_row(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = replace(make_config(root, payload_mode="batch"), validate_ranges=False, shared_secret=SECRET)
            write_csv(
                cfg.watch_folder / "day.csv",
                [valid_row(methane_mole_percent="0"), valid_row(time_text="14:00:02", methane_mole_percent="0")],
            )
            message = "methane_mole_percent must be >= 95.0 " + SECRET + " " + "x" * 600

            def transport(*_args):
                return PostResult("bad_request", 400, message, None)

            conn = store.connect(cfg.database_path)
            try:
                summary, lines = captured(
                    lambda: run_cycle(conn, cfg, send=True, transport=transport, clock=CLOCK, sleep=lambda _s: None)
                )
                statuses = [row["status"] for row in conn.execute("SELECT status FROM readings")]
            finally:
                conn.close()
        rejected = [line for line in lines if line.startswith("rejected by portal")]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(rejected[0].startswith(f"rejected by portal {LOW} http=400: methane_mole_percent must be >= 95.0"))
        self.assertTrue(all(SECRET not in line and "[redacted]" in line for line in rejected))
        self.assertTrue(rejected[0].endswith("..."))
        self.assertLess(len(rejected[0].split(": ", 1)[1]), 310)
        self.assertTrue(any(line.startswith("request rows=") and "http=400" in line for line in lines))
        self.assertEqual(summary.new_attention, 2)
        self.assertEqual(statuses, ["needs_attention", "needs_attention"])


class AttentionCliTests(unittest.TestCase):
    def test_summary_line_shows_total_and_new(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            write_csv(root / "incoming" / "day.csv", [valid_row(methane_mole_percent="0"), valid_row(time_text="14:00:02")])
            _code, first = run_main(["--config", str(config_path)])
            _code, second = run_main(["--config", str(config_path)])
        tail = " (full list: scada-uploader.exe --config config.json --list-attention)."
        self.assertIn("Needs attention: 1 total, 1 new this run" + tail, first)
        self.assertIn("Needs attention: 1 total, 0 new this run" + tail, second)
        self.assertNotIn("Needs attention (not sent)", first)

    def test_list_attention_prints_all_rows_read_only_even_with_lock_held(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            write_csv(
                root / "incoming" / "day.csv",
                [valid_row(methane_mole_percent="0"), valid_row(time_text="14:00:02", oxygen_mole_percent="0.9")],
            )
            run_main(["--config", str(config_path)])
            conn = store.connect(root / "queue.sqlite")
            conn.execute(
                "UPDATE readings SET http_status = 400, error = ? WHERE reading_at = ?",
                ("portal says no " + SECRET, "2026-10-23T14:00:02+05:30"),
            )
            conn.commit()
            conn.close()
            write_csv(root / "incoming" / "later.csv", [valid_row(time_text="15:00:02", methane_mole_percent="1")])
            logs_before = sorted(path.name for path in root.glob("*.log"))
            size_before = sum(path.stat().st_size for path in root.glob("*.log"))
            lock = acquire(root / "uploader.lock")
            try:
                code, output = run_main(["--config", str(config_path), "--list-attention"])
            finally:
                lock.close()
            conn = store.connect(root / "queue.sqlite")
            count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            conn.close()
            logs_after = sorted(path.name for path in root.glob("*.log"))
            size_after = sum(path.stat().st_size for path in root.glob("*.log"))
        lines = output.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(
            lines[0],
            f"{LOW} | local | day.csv | methane_mole_percent must be >= 95.0 and <= 100.0 (got 0)",
        )
        self.assertEqual(lines[1], "2026-10-23T14:00:02+05:30 | http=400 | day.csv | portal says no [redacted]")
        self.assertEqual(lines[-1], "Needs attention: 2 total.")
        self.assertEqual(len(lines), 3)
        self.assertEqual(count, 2)
        self.assertEqual((logs_before, size_before), (logs_after, size_after))

    def test_list_attention_without_database_and_with_conflicting_flags(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            code, output = run_main(["--config", str(config_path), "--list-attention"])
            exists = (root / "queue.sqlite").exists()
            buffer = io.StringIO()
            from contextlib import redirect_stderr

            with redirect_stderr(buffer):
                with_send, _ = run_main(["--config", str(config_path), "--list-attention", "--send"])
                with_requeue, _ = run_main(["--config", str(config_path), "--list-attention", "--requeue-uncertain"])
                with_attention, _ = run_main(
                    ["--config", str(config_path), "--list-attention", "--requeue-attention"]
                )
                missing_flag, _ = run_main(
                    ["--config", str(config_path), "--attention-contains", "totalSulphur"]
                )
        self.assertEqual(code, 0)
        self.assertIn("Needs attention: 0 total.", output)
        self.assertFalse(exists)
        self.assertEqual(with_send, 2)
        self.assertEqual(with_requeue, 2)
        self.assertEqual(with_attention, 2)
        self.assertEqual(missing_flag, 2)
        self.assertIn("cannot be combined", buffer.getvalue())
        self.assertIn("requires --requeue-attention", buffer.getvalue())

    def test_requeue_attention_all_and_filtered(self):
        later = "2026-10-23T14:00:02+05:30"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            write_csv(
                root / "incoming" / "day.csv",
                [valid_row(), valid_row(time_text="14:00:02")],
            )
            run_main(["--config", str(config_path)])
            conn = store.connect(root / "queue.sqlite")
            try:
                conn.execute(
                    """
                    UPDATE readings SET status = 'needs_attention', http_status = 400,
                    error = 'totalSulphurIncludingH2s_mg_m3 must be <= 10.0'
                    WHERE reading_at = ?
                    """,
                    (LOW,),
                )
                conn.execute(
                    """
                    UPDATE readings SET status = 'needs_attention', http_status = 400,
                    error = 'oxygen_mole_percent must be <= 0.5'
                    WHERE reading_at = ?
                    """,
                    (later,),
                )
                conn.commit()
            finally:
                conn.close()
            filtered, out_filtered = run_main(
                [
                    "--config",
                    str(config_path),
                    "--requeue-attention",
                    "--attention-contains",
                    "totalSulphurIncludingH2s",
                ]
            )
            conn = store.connect(root / "queue.sqlite")
            try:
                statuses = {
                    row["reading_at"]: row["status"]
                    for row in conn.execute("SELECT reading_at, status FROM readings")
                }
            finally:
                conn.close()
            all_rows, out_all = run_main(["--config", str(config_path), "--requeue-attention"])
        self.assertEqual(filtered, 0)
        self.assertIn("Requeued needs_attention rows: 1.", out_filtered)
        self.assertEqual(statuses[LOW], "pending")
        self.assertEqual(statuses[later], "needs_attention")
        self.assertEqual(all_rows, 0)
        self.assertIn("Requeued needs_attention rows: 1.", out_all)


if __name__ == "__main__":
    unittest.main()
