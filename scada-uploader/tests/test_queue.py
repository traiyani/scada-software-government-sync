import io
import json
import logging
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from scada_uploader import store
from scada_uploader.cli import main
from scada_uploader.config import live_send_allowed, load_config
from scada_uploader.schema import API_ACCEPTED, NEEDS_ATTENTION, PENDING, UNCERTAIN
from scada_uploader.signing import sign_body
from scada_uploader.transport import PostResult
from scada_uploader.uploader import run_cycle
from tests.helpers import make_config, valid_row, write_csv

CLOCK = lambda: 1780656091  # noqa: E731 — fixed clock, not the SCADA reading time


class QueueTests(unittest.TestCase):
    def test_zero_methane_is_held_and_not_sent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            sample = Path(__file__).resolve().parents[1] / "samples" / "SAMPLE_ONLY_two_zero_rows.csv"
            target = cfg.watch_folder / sample.name
            target.write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")
            calls = []
            conn = store.connect(cfg.database_path)
            try:
                summary = run_cycle(
                    conn,
                    cfg,
                    send=True,
                    transport=lambda *args: calls.append(args),
                    clock=CLOCK,
                    sleep=lambda _seconds: None,
                )
                rows = list(conn.execute("SELECT * FROM readings ORDER BY reading_at"))
            finally:
                conn.close()
        self.assertEqual(calls, [])
        self.assertEqual(summary.inserted, 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == NEEDS_ATTENTION for row in rows))
        self.assertIn("methane_mole_percent must be >= 95.0", rows[0]["error"])
        self.assertIn('"methane_mole_percent":0', rows[0]["payload_json"])
        self.assertEqual(rows[0]["attempt_count"], 0)
        self.assertIsNone(rows[0]["batch_id"])

    def test_api_accepted_row_is_not_resent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            path = cfg.watch_folder / "day.csv"
            write_csv(path, [valid_row()])
            calls = []

            def transport(url, body, headers, timeout):
                calls.append(body)
                self.assertEqual(
                    headers["X-SCADA-Signature"],
                    sign_body(cfg.shared_secret, headers["X-SCADA-Timestamp"], body),
                )
                self.assertIn("2026-10-23T13:45:02+05:30", body)
                self.assertNotEqual(headers["X-SCADA-Timestamp"], "2026-10-23T13:45:02+05:30")
                self.assertNotRegex(body, r"\s")
                return PostResult("accepted", 202, None, "batch-accepted-1")

            conn = store.connect(cfg.database_path)
            try:
                first = run_cycle(conn, cfg, send=True, transport=transport, clock=CLOCK, sleep=lambda _s: None)
                stored = store.get_by_reading_at(conn, "2026-10-23T13:45:02+05:30")
                self.assertEqual(stored["status"], API_ACCEPTED)
                self.assertEqual(stored["batch_id"], "batch-accepted-1")
                self.assertEqual(stored["http_status"], 202)
                self.assertEqual(stored["attempt_count"], 1)
                # Same file again, and a conflicting payload for that timestamp.
                write_csv(path, [valid_row(methane_mole_percent="97.1")])
                second = run_cycle(conn, cfg, send=True, transport=transport, clock=CLOCK, sleep=lambda _s: None)
                stored = store.get_by_reading_at(conn, "2026-10-23T13:45:02+05:30")
            finally:
                conn.close()
        self.assertEqual(first.accepted_rows, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(stored["status"], API_ACCEPTED)
        self.assertIn('"methane_mole_percent":96.8', stored["payload_json"])
        self.assertEqual(stored["source_filename"], "day.csv")

    def test_dry_run_does_not_call_transport_or_increment_attempts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            write_csv(cfg.watch_folder / "day.csv", [valid_row()])

            def transport(*_args):
                raise AssertionError("dry run must not open a request")

            conn = store.connect(cfg.database_path)
            try:
                summary = run_cycle(conn, cfg, send=False, transport=transport, clock=CLOCK, sleep=lambda _s: None)
                stored = store.get_by_reading_at(conn, "2026-10-23T13:45:02+05:30")
            finally:
                conn.close()
        self.assertTrue(summary.dry_run)
        self.assertEqual(len(summary.previews), 1)
        preview = summary.previews[0]
        self.assertEqual(preview.timestamp, "1780656091")
        self.assertEqual(preview.body, stored["payload_json"])
        self.assertEqual(stored["status"], PENDING)
        self.assertEqual(stored["attempt_count"], 0)

    def test_timeout_is_uncertain_and_not_retried(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root, max_attempts_per_run=3)
            write_csv(cfg.watch_folder / "day.csv", [valid_row()])
            calls = []

            def transport(*_args):
                calls.append(1)
                return PostResult("timeout", None, "timed out after connecting", None)

            conn = store.connect(cfg.database_path)
            try:
                run_cycle(conn, cfg, send=True, transport=transport, clock=CLOCK, sleep=lambda _s: None)
                stored = store.get_by_reading_at(conn, "2026-10-23T13:45:02+05:30")
                self.assertEqual(stored["status"], UNCERTAIN)
                run_cycle(conn, cfg, send=True, transport=transport, clock=CLOCK, sleep=lambda _s: None)
                stored = store.get_by_reading_at(conn, "2026-10-23T13:45:02+05:30")
            finally:
                conn.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(stored["status"], UNCERTAIN)
        self.assertEqual(stored["attempt_count"], 1)

    def test_connection_failure_stays_pending_and_retries_are_capped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root, max_attempts_per_run=3)
            write_csv(cfg.watch_folder / "day.csv", [valid_row()])
            calls = []

            def transport(*_args):
                calls.append(1)
                return PostResult("connection", None, "connection refused", None)

            conn = store.connect(cfg.database_path)
            try:
                run_cycle(conn, cfg, send=True, transport=transport, clock=CLOCK, sleep=lambda _s: None)
                stored = store.get_by_reading_at(conn, "2026-10-23T13:45:02+05:30")
                self.assertEqual(stored["status"], PENDING)
                self.assertEqual(stored["attempt_count"], 3)
                run_cycle(conn, cfg, send=True, transport=transport, clock=CLOCK, sleep=lambda _s: None)
            finally:
                conn.close()
        self.assertEqual(len(calls), 6)

    def test_batch_mode_signs_one_array_and_stores_one_batch_id(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root, payload_mode="batch", batch_size=20)
            write_csv(
                cfg.watch_folder / "day.csv",
                [valid_row(time_text="13:45:02"), valid_row(time_text="14:45:02")],
            )
            bodies = []

            def transport(_url, body, headers, _timeout):
                bodies.append(body)
                self.assertEqual(
                    headers["X-SCADA-Signature"],
                    sign_body(cfg.shared_secret, headers["X-SCADA-Timestamp"], body),
                )
                return PostResult("accepted", 202, None, "batch-array-1")

            conn = store.connect(cfg.database_path)
            try:
                summary = run_cycle(conn, cfg, send=True, transport=transport, clock=CLOCK, sleep=lambda _s: None)
                rows = list(conn.execute("SELECT status, batch_id FROM readings ORDER BY reading_at"))
            finally:
                conn.close()
        self.assertEqual(summary.requests, 1)
        self.assertEqual(len(bodies), 1)
        parsed = json.loads(bodies[0])
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 2)
        self.assertTrue(all(row["status"] == API_ACCEPTED and row["batch_id"] == "batch-array-1" for row in rows))

    def test_needs_attention_can_be_corrected_but_uncertain_cannot(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            path = cfg.watch_folder / "day.csv"
            write_csv(path, [valid_row(methane_mole_percent="0")])
            conn = store.connect(cfg.database_path)
            try:
                run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=lambda _s: None)
                self.assertEqual(
                    store.get_by_reading_at(conn, "2026-10-23T13:45:02+05:30")["status"],
                    NEEDS_ATTENTION,
                )
                write_csv(path, [valid_row(methane_mole_percent="96.8")])
                summary = run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=lambda _s: None)
                corrected = store.get_by_reading_at(conn, "2026-10-23T13:45:02+05:30")
                self.assertEqual(summary.corrected, 1)
                self.assertEqual(corrected["status"], PENDING)
                conn.execute(
                    "UPDATE readings SET status = ? WHERE reading_at = ?",
                    (UNCERTAIN, corrected["reading_at"]),
                )
                conn.commit()
                write_csv(path, [valid_row(methane_mole_percent="97")])
                run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=lambda _s: None)
                kept = store.get_by_reading_at(conn, "2026-10-23T13:45:02+05:30")
            finally:
                conn.close()
        self.assertEqual(kept["status"], UNCERTAIN)
        self.assertIn('"methane_mole_percent":96.8', kept["payload_json"])

    def test_cli_dry_run_prints_queue_state_without_the_secret(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = "unit-test-secret-value"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "watch_folder": "incoming",
                        "device_id": "TEST_DEVICE",
                        "shared_secret": secret,
                        "telemetry_url": "http://127.0.0.1:9/api/v1/public/scada/telemetry",
                        "timezone": "Asia/Kolkata",
                        "payload_mode": "single",
                        "file_stable_seconds": 0,
                        "retry_backoff_seconds": 0,
                        "database_path": "queue.sqlite",
                        "log_path": "uploader.log",
                        "lock_path": "uploader.lock",
                    }
                ),
                encoding="utf-8",
            )
            incoming = root / "incoming"
            incoming.mkdir()
            write_csv(incoming / "day.csv", [valid_row()])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--config", str(config_path)])
            output = buffer.getvalue()
            log_text = (root / "uploader.log").read_text(encoding="utf-8")
            shown = repr(load_config(config_path))
            logger = logging.getLogger("scada_uploader")
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
        self.assertEqual(code, 0)
        self.assertIn("Dry run: no HTTP request was sent.", output)
        self.assertIn("api_accepted 0 (API queued, not final storage)", output)
        self.assertIn("X-SCADA-Timestamp: ", output)
        self.assertNotIn(secret, output)
        self.assertNotIn(secret, log_text)
        self.assertIn("[redacted]", shown)
        self.assertNotIn(secret, shown)

    def test_config_example_has_placeholders_and_env_overrides_secret(self):
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        data = json.loads(example.read_text(encoding="utf-8"))
        self.assertEqual(data["shared_secret"], "YOUR_SHARED_SECRET")
        self.assertEqual(data["device_id"], "YOUR_DEVICE_ID")
        self.assertIn("YOUR_PORTAL_HOST", data["telemetry_url"])
        self.assertIsNotNone(live_send_allowed(data["telemetry_url"]))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_file = root / "secret.txt"
            secret_file.write_text("file-secret-value\n", encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "watch_folder": "incoming",
                        "device_id": "TEST_DEVICE",
                        "shared_secret": "inline-secret-value",
                        "shared_secret_file": str(secret_file),
                        "telemetry_url": "https://example.invalid/api/v1/public/scada/telemetry",
                    }
                ),
                encoding="utf-8",
            )
            from_file = load_config(config_path, environ={})
            self.assertEqual(from_file.shared_secret, "file-secret-value")
            from_env = load_config(config_path, environ={"SCADA_SHARED_SECRET": "env-secret-value"})
            self.assertEqual(from_env.shared_secret, "env-secret-value")
            self.assertNotIn("file-secret-value", repr(from_file))
            logger = logging.getLogger("scada_uploader")
            previous = logger.handlers[:]
            logger.handlers.clear()
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            from scada_uploader.cli import _RedactFilter

            handler.addFilter(_RedactFilter("file-secret-value"))
            logger.addHandler(handler)
            try:
                logger.info("failed with file-secret-value in the text")
            finally:
                logger.handlers.clear()
                logger.handlers.extend(previous)
            self.assertNotIn("file-secret-value", stream.getvalue())
            self.assertIn("[redacted]", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
