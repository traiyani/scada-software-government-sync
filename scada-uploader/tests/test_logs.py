import io
import json
import logging
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scada_uploader.cli import _configure_logging, dated_log_path, main
from scada_uploader.locking import acquire
from tests.helpers import valid_row, write_csv

IST = timezone(timedelta(hours=5, minutes=30))
# 00:10 on 24 Oct in Kolkata, still 23 Oct in UTC.
JUST_AFTER_IST_MIDNIGHT = datetime(2026, 10, 23, 18, 40, tzinfo=timezone.utc).timestamp()


def close_logging():
    logger = logging.getLogger("scada_uploader")
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)


def write_config(root: Path, **extra) -> Path:
    config_path = root / "config.json"
    values = {
        "watch_folder": "incoming",
        "device_id": "TEST_DEVICE",
        "shared_secret": "unit-test-secret-value",
        "telemetry_url": "http://127.0.0.1:9/api/v1/public/scada/telemetry",
        "timezone": "Asia/Kolkata",
        "file_stable_seconds": 0,
        "database_path": "queue.sqlite",
        "log_path": "uploader.log",
        "lock_path": "uploader.lock",
        **extra,
    }
    config_path.write_text(json.dumps(values), encoding="utf-8")
    (root / "incoming").mkdir(exist_ok=True)
    return config_path


def run_main(argv):
    try:
        with redirect_stdout(io.StringIO()):
            return main(argv)
    finally:
        close_logging()


class DatedLogTests(unittest.TestCase):
    def test_name_keeps_folder_stem_and_suffix_and_uses_local_date(self):
        base = Path("C:/SCADA/uploader/scada_uploader.log")
        self.assertEqual(
            dated_log_path(base, IST, JUST_AFTER_IST_MIDNIGHT),
            Path("C:/SCADA/uploader/scada_uploader-2026-10-24.log"),
        )
        self.assertEqual(
            dated_log_path(base, timezone.utc, JUST_AFTER_IST_MIDNIGHT).name,
            "scada_uploader-2026-10-23.log",
        )
        self.assertEqual(
            dated_log_path(Path("D:/logs/plant.txt"), IST, JUST_AFTER_IST_MIDNIGHT),
            Path("D:/logs/plant-2026-10-24.txt"),
        )

    def test_runs_on_one_day_append_to_the_same_dated_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            write_csv(root / "incoming" / "day.csv", [valid_row()])
            with mock.patch("time.time", return_value=JUST_AFTER_IST_MIDNIGHT):
                self.assertEqual(run_main(["--config", str(config_path)]), 0)
                first = (root / "uploader-2026-10-24.log").read_text(encoding="utf-8")
                self.assertEqual(run_main(["--config", str(config_path)]), 0)
            logs = sorted(path.name for path in root.glob("*.log"))
            text = (root / "uploader-2026-10-24.log").read_text(encoding="utf-8")
        self.assertEqual(logs, ["uploader-2026-10-24.log"])
        self.assertTrue(text.startswith(first))
        self.assertEqual(text.count("Dry run: no HTTP request was sent."), 2)
        self.assertIn("Previous run: none recorded.", first)
        self.assertNotIn("unit-test-secret-value", text)

    def test_custom_log_path_in_subfolder_and_early_lock_message(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root, log_path="logs/plant.txt")
            lock = acquire(root / "uploader.lock")
            try:
                with mock.patch("time.time", return_value=JUST_AFTER_IST_MIDNIGHT):
                    code = run_main(["--config", str(config_path)])
            finally:
                lock.close()
            text = (root / "logs" / "plant-2026-10-24.txt").read_text(encoding="utf-8")
            plain = (root / "logs" / "plant.txt").exists()
        self.assertEqual(code, 0)
        self.assertIn("exiting because the lock is held", text)
        self.assertFalse(plain)

    def test_secret_is_redacted_in_the_dated_file(self):
        with TemporaryDirectory() as tmp:
            path = dated_log_path(Path(tmp) / "scada_uploader.log", IST, JUST_AFTER_IST_MIDNIGHT)
            _configure_logging(path, "file-secret-value")
            try:
                logging.getLogger("scada_uploader").info("failed with file-secret-value in the text")
            finally:
                close_logging()
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("file-secret-value", text)
        self.assertIn("[redacted]", text)


if __name__ == "__main__":
    unittest.main()
