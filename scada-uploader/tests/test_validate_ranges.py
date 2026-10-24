import csv
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from scada_uploader import store
from scada_uploader.config import load_config
from scada_uploader.schema import API_ACCEPTED, NEEDS_ATTENTION, PENDING, UNCERTAIN
from scada_uploader.uploader import run_cycle
from tests.helpers import make_config, valid_row, write_csv

CLOCK = lambda: 1780656091  # noqa: E731

LOW = "2026-10-23T13:45:02+05:30"
BROKEN = "2026-10-23T14:00:02+05:30"
REJECTED = "2026-10-23T14:15:02+05:30"
ACCEPTED = "2026-10-23T14:30:02+05:30"
UNSURE = "2026-10-23T14:45:02+05:30"


def dry_run(conn, cfg):
    return run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=lambda _s: None)


def status_of(conn, reading_at):
    return store.get_by_reading_at(conn, reading_at)["status"]


def previewed(summary):
    return [json.loads(preview.body)["readingAt"] for preview in summary.previews]


class ValidateRangesConfigTests(unittest.TestCase):
    def _load(self, extra):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            values = {
                "device_id": "TEST_DEVICE",
                "shared_secret": "unit-test-secret",
                "telemetry_url": "https://example.invalid/api/v1/public/scada/telemetry",
                **extra,
            }
            config_path.write_text(json.dumps(values), encoding="utf-8")
            return load_config(config_path, environ={})

    def test_missing_flag_defaults_to_true(self):
        self.assertIs(self._load({}).validate_ranges, True)
        self.assertIs(self._load({"validate_ranges": False}).validate_ranges, False)

    def test_non_boolean_flag_is_a_config_error(self):
        for value in ["false", "true", 0, 1, None]:
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as caught:
                    self._load({"validate_ranges": value})
                self.assertIn("validate_ranges must be true or false", str(caught.exception))


class ValidateRangesRunTests(unittest.TestCase):
    def test_flag_off_sends_out_of_range_but_holds_unparseable(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root, validate_ranges=False)
            write_csv(
                cfg.watch_folder / "day.csv",
                [
                    valid_row(time_text="13:45:02", methane_mole_percent="0"),
                    valid_row(time_text="14:00:02", methane_mole_percent="abc"),
                ],
            )
            conn = store.connect(cfg.database_path)
            try:
                summary = dry_run(conn, cfg)
                low = store.get_by_reading_at(conn, LOW)
                broken = store.get_by_reading_at(conn, BROKEN)
            finally:
                conn.close()
        self.assertEqual(low["status"], PENDING)
        self.assertIsNone(low["error"])
        self.assertEqual(previewed(summary), [LOW])
        self.assertIn('"methane_mole_percent":0', summary.previews[0].body)
        self.assertEqual(broken["status"], NEEDS_ATTENTION)
        self.assertIn("not a number", broken["error"])

    def test_toggling_releases_only_range_holds_and_back_on_holds_again(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_on = make_config(root)
            cfg_off = replace(cfg_on, validate_ranges=False)
            path = cfg_on.watch_folder / "day.csv"
            write_csv(
                path,
                [
                    valid_row(time_text="13:45:02", methane_mole_percent="0"),
                    valid_row(time_text="14:00:02", methane_mole_percent=""),
                    valid_row(time_text="14:15:02", oxygen_mole_percent="0.9"),
                    valid_row(time_text="14:30:02", methane_mole_percent="90"),
                    valid_row(time_text="14:45:02", methane_mole_percent="91"),
                ],
            )
            conn = store.connect(cfg_on.database_path)
            try:
                first = dry_run(conn, cfg_on)
                self.assertEqual(first.previews, [])
                conn.execute(
                    "UPDATE readings SET http_status = 400, error = 'HTTP 400 from portal' WHERE reading_at = ?",
                    (REJECTED,),
                )
                conn.execute("UPDATE readings SET status = ? WHERE reading_at = ?", (API_ACCEPTED, ACCEPTED))
                conn.execute("UPDATE readings SET status = ? WHERE reading_at = ?", (UNCERTAIN, UNSURE))
                conn.commit()

                off = dry_run(conn, cfg_off)
                self.assertEqual(off.files_unchanged, 1)
                self.assertEqual(off.released, 1)
                self.assertEqual(previewed(off), [LOW])
                self.assertEqual(status_of(conn, LOW), PENDING)
                self.assertEqual(status_of(conn, BROKEN), NEEDS_ATTENTION)
                self.assertEqual(status_of(conn, REJECTED), NEEDS_ATTENTION)
                self.assertEqual(status_of(conn, ACCEPTED), API_ACCEPTED)
                self.assertEqual(status_of(conn, UNSURE), UNCERTAIN)

                with path.open("a", encoding="utf-8", newline="") as handle:
                    csv.writer(handle).writerow(valid_row(time_text="15:00:02", methane_mole_percent="1"))
                reread = dry_run(conn, cfg_off)
                self.assertEqual(reread.files_unchanged, 0)
                self.assertEqual(reread.unchanged, 0)
                self.assertEqual(reread.corrected, 0)
                self.assertEqual(status_of(conn, REJECTED), NEEDS_ATTENTION)
                self.assertEqual(store.get_by_reading_at(conn, REJECTED)["http_status"], 400)
                self.assertEqual(status_of(conn, "2026-10-23T15:00:02+05:30"), PENDING)

                back_on = dry_run(conn, cfg_on)
                low = store.get_by_reading_at(conn, LOW)
                late = store.get_by_reading_at(conn, "2026-10-23T15:00:02+05:30")
                accepted = status_of(conn, ACCEPTED)
                unsure = status_of(conn, UNSURE)
            finally:
                conn.close()
        self.assertEqual(back_on.previews, [])
        self.assertEqual(back_on.released, 0)
        self.assertEqual(low["status"], NEEDS_ATTENTION)
        self.assertIn("methane_mole_percent must be >= 95.0", low["error"])
        self.assertEqual(late["status"], NEEDS_ATTENTION)
        self.assertEqual(accepted, API_ACCEPTED)
        self.assertEqual(unsure, UNCERTAIN)


if __name__ == "__main__":
    unittest.main()
