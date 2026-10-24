import io
import json
import logging
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scada_uploader import store
from scada_uploader.cli import main
from scada_uploader.transport import PostResult
from tests.helpers import read_logs, valid_row, write_csv


def write_config(root: Path) -> Path:
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "watch_folder": "incoming",
                "device_id": "TEST_DEVICE",
                "shared_secret": "unit-test-secret",
                "telemetry_url": "http://127.0.0.1:9/api/v1/public/scada/telemetry",
                "timezone": "Asia/Kolkata",
                "file_stable_seconds": 0,
                "retry_backoff_seconds": 0,
                "max_attempts_per_run": 1,
                "database_path": "queue.sqlite",
                "log_path": "uploader.log",
                "lock_path": "uploader.lock",
            }
        ),
        encoding="utf-8",
    )
    (root / "incoming").mkdir()
    return config_path


def run_main(argv: list[str]) -> tuple[int, str]:
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


def all_runs(root: Path) -> list:
    conn = store.connect(root / "queue.sqlite")
    try:
        return list(conn.execute("SELECT * FROM runs ORDER BY id"))
    finally:
        conn.close()


class RunRecordTests(unittest.TestCase):
    def test_version_is_1_0_1(self):
        from scada_uploader import __version__

        self.assertEqual(__version__, "1.0.1")
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()) as output:
                main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "scada-uploader 1.0.1")

    def test_runs_are_recorded_and_previous_run_is_printed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            write_csv(root / "incoming" / "day.csv", [valid_row()])
            first_code, first_out = run_main(["--config", str(config_path)])
            with mock.patch(
                "scada_uploader.cli.post_telemetry",
                return_value=PostResult("connection", None, "connection refused", None),
            ):
                second_code, second_out = run_main(["--config", str(config_path), "--send"])
            runs = all_runs(root)
            log_text = read_logs(root)
        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 1)
        self.assertIn("Previous run: none recorded.", first_out)
        self.assertEqual(len(runs), 2)
        first, second = runs
        self.assertEqual(first["mode"], "dry_run")
        self.assertEqual(first["files_seen"], 1)
        self.assertEqual(first["files_unchanged"], 0)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(first["requests"], 0)
        self.assertEqual(first["exit_code"], 0)
        self.assertRegex(first["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+05:30$")
        self.assertRegex(first["finished_at"], r"\+05:30$")
        self.assertEqual(second["mode"], "send")
        self.assertEqual(second["files_unchanged"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["requests"], 1)
        self.assertEqual(second["accepted_rows"], 0)
        self.assertEqual(second["uncertain_rows"], 0)
        self.assertEqual(second["file_errors"], 0)
        self.assertEqual(second["exit_code"], 1)
        expected = (
            f"Previous run: started {first['started_at']}, finished {first['finished_at']}, dry_run, exit 0."
        )
        self.assertIn(expected, second_out)
        self.assertIn(expected, log_text)

    def test_interrupted_run_is_left_unfinished_and_reported(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            with mock.patch("scada_uploader.cli.run_cycle", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    run_main(["--config", str(config_path)])
            crashed = all_runs(root)
            code, output = run_main(["--config", str(config_path)])
            runs = all_runs(root)
        self.assertEqual(len(crashed), 1)
        self.assertIsNone(crashed[0]["finished_at"])
        self.assertIsNone(crashed[0]["exit_code"])
        self.assertEqual(code, 0)
        self.assertIn(
            f"Previous run: started {crashed[0]['started_at']}, dry_run, did not finish.",
            output,
        )
        self.assertEqual(len(runs), 2)
        self.assertIsNotNone(runs[1]["finished_at"])


if __name__ == "__main__":
    unittest.main()
