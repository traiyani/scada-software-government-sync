import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scada_uploader import store
from scada_uploader.uploader import run_cycle
from tests.helpers import make_config, valid_row, write_csv

CLOCK = lambda: 1780656091  # noqa: E731


class FileSkipTests(unittest.TestCase):
    def test_unchanged_file_is_skipped_without_sleep_or_parse(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root, file_stable_seconds=1)
            write_csv(cfg.watch_folder / "day.csv", [valid_row()])
            sleeps = []
            conn = store.connect(cfg.database_path)
            try:
                first = run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=sleeps.append)
                self.assertEqual(first.inserted, 1)
                self.assertEqual(first.files_unchanged, 0)
                self.assertEqual(sleeps, [1])
                sleeps.clear()
                with mock.patch("scada_uploader.uploader.read_csv_file") as reader:
                    second = run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=sleeps.append)
            finally:
                conn.close()
        reader.assert_not_called()
        self.assertEqual(sleeps, [])
        self.assertEqual(second.files_seen, 1)
        self.assertEqual(second.files_unchanged, 1)
        self.assertEqual(second.files_skipped, 0)
        self.assertEqual(second.inserted + second.duplicates, 0)

    def test_appended_row_is_ingested_on_next_run(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            path = cfg.watch_folder / "day.csv"
            write_csv(path, [valid_row(time_text="13:45:02")])
            conn = store.connect(cfg.database_path)
            try:
                run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=lambda _s: None)
                with path.open("a", encoding="utf-8", newline="") as handle:
                    csv.writer(handle).writerow(valid_row(time_text="14:00:02"))
                second = run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=lambda _s: None)
                added = store.get_by_reading_at(conn, "2026-10-23T14:00:02+05:30")
                recorded = store.file_snapshot(conn, "day.csv")
                stat = path.stat()
            finally:
                conn.close()
        self.assertEqual(second.files_unchanged, 0)
        self.assertEqual(second.inserted, 1)
        self.assertEqual(second.duplicates, 1)
        self.assertIsNotNone(added)
        self.assertEqual(recorded, (stat.st_size, stat.st_mtime_ns))

    def test_unstable_or_errored_file_is_not_recorded(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root, file_stable_seconds=1)
            growing = cfg.watch_folder / "growing.csv"
            broken = cfg.watch_folder / "broken.csv"
            write_csv(growing, [valid_row()])
            broken.write_text("Date,Time\n2026-10-23,13:45:02\n", encoding="utf-8")

            def sleep(_seconds):
                with growing.open("a", encoding="utf-8", newline="") as handle:
                    csv.writer(handle).writerow(valid_row(time_text="14:00:02"))

            conn = store.connect(cfg.database_path)
            try:
                first = run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=sleep)
                self.assertIsNone(store.file_snapshot(conn, "growing.csv"))
                self.assertIsNone(store.file_snapshot(conn, "broken.csv"))
                second = run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=lambda _s: None)
            finally:
                conn.close()
        self.assertEqual(first.files_skipped, 1)
        self.assertEqual(len(first.file_errors), 1)
        self.assertEqual(first.inserted, 0)
        self.assertEqual(second.files_unchanged, 0)
        self.assertEqual(second.inserted, 2)
        self.assertEqual(len(second.file_errors), 1)


if __name__ == "__main__":
    unittest.main()
