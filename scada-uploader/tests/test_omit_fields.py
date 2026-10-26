import csv
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from scada_uploader import store
from scada_uploader.config import load_config
from scada_uploader.schema import API_ACCEPTED, NEEDS_ATTENTION, NUMERIC_FIELDS, PENDING, UNCERTAIN
from scada_uploader.uploader import run_cycle
from tests.helpers import HEADER, make_config, valid_row, write_csv

CLOCK = lambda: 1780656091  # noqa: E731

SULPHUR = "totalSulphurIncludingH2s_mg_m3"
HUGE = "2026-10-23T13:45:02+05:30"
EMPTY = "2026-10-23T14:00:02+05:30"
REJECTED = "2026-10-23T14:15:02+05:30"
ACCEPTED = "2026-10-23T14:30:02+05:30"
UNSURE = "2026-10-23T14:45:02+05:30"
METHANE = "2026-10-23T15:00:02+05:30"


def dry_run(conn, cfg):
    return run_cycle(conn, cfg, send=False, transport=None, clock=CLOCK, sleep=lambda _s: None)


def status_of(conn, reading_at):
    return store.get_by_reading_at(conn, reading_at)["status"]


def previewed(summary):
    return [json.loads(preview.body)["readingAt"] for preview in summary.previews]


class OmitFieldsConfigTests(unittest.TestCase):
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

    def test_missing_key_defaults_to_empty(self):
        self.assertEqual(self._load({}).omit_fields, ())
        self.assertEqual(self._load({"omit_fields": []}).omit_fields, ())

    def test_non_array_or_non_string_is_a_config_error(self):
        for value in ["totalSulphurIncludingH2s_mg_m3", {"field": SULPHUR}, 1, None, [SULPHUR, 1]]:
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as caught:
                    self._load({"omit_fields": value})
                self.assertIn("omit_fields must be a JSON array of field name strings", str(caught.exception))

    def test_unknown_name_lists_valid_fields(self):
        with self.assertRaises(SystemExit) as caught:
            self._load({"omit_fields": ["not_a_real_field"]})
        message = str(caught.exception)
        self.assertIn("unknown field 'not_a_real_field'", message)
        self.assertIn("valid names are:", message)
        for name in NUMERIC_FIELDS:
            self.assertIn(name, message)
        self.assertNotIn("readingAt", message.split("valid names are:", 1)[1])

    def test_reading_at_cannot_be_omitted(self):
        with self.assertRaises(SystemExit) as caught:
            self._load({"omit_fields": ["readingAt"]})
        message = str(caught.exception)
        self.assertIn("unknown field 'readingAt'", message)
        self.assertIn("valid names are:", message)


class OmitFieldsRunTests(unittest.TestCase):
    def test_omit_drops_field_and_huge_value_is_pending(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root, omit_fields=(SULPHUR,))
            write_csv(
                cfg.watch_folder / "day.csv",
                [
                    valid_row(time_text="13:45:02", totalSulphurIncludingH2s_mg_m3="99999"),
                    valid_row(time_text="15:00:02", methane_mole_percent="0"),
                ],
            )
            conn = store.connect(cfg.database_path)
            try:
                with self.assertLogs("scada_uploader", level="WARNING") as logged:
                    summary = dry_run(conn, cfg)
                huge = store.get_by_reading_at(conn, HUGE)
                methane = store.get_by_reading_at(conn, METHANE)
            finally:
                conn.close()
        self.assertEqual(huge["status"], PENDING)
        self.assertIsNone(huge["error"])
        self.assertNotIn(SULPHUR, json.loads(huge["payload_json"]))
        self.assertNotIn(f'"{SULPHUR}"', summary.previews[0].body)
        self.assertEqual(previewed(summary), [HUGE])
        self.assertEqual(methane["status"], NEEDS_ATTENTION)
        self.assertIn("methane_mole_percent must be >= 95.0", methane["error"])
        self.assertTrue(
            any(
                "omitted fields: totalSulphurIncludingH2s_mg_m3 (not sent; portal may still require them)"
                in line
                for line in logged.output
            )
        )

    def test_omitted_column_may_be_missing_empty_or_invalid(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root, omit_fields=(SULPHUR,))
            header = [name for name in HEADER if name != "H2S TOT"]
            rows = [
                [cell for index, cell in enumerate(valid_row(time_text="13:45:02")) if HEADER[index] != "H2S TOT"],
                valid_row(time_text="14:00:02", totalSulphurIncludingH2s_mg_m3=""),
                valid_row(time_text="14:15:02", totalSulphurIncludingH2s_mg_m3="not-a-number"),
            ]
            path = cfg.watch_folder / "day.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerow(rows[0])
            with (cfg.watch_folder / "more.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(HEADER)
                writer.writerow(rows[1])
                writer.writerow(rows[2])
            conn = store.connect(cfg.database_path)
            try:
                summary = dry_run(conn, cfg)
                missing = store.get_by_reading_at(conn, HUGE)
                empty = store.get_by_reading_at(conn, EMPTY)
                invalid = store.get_by_reading_at(conn, REJECTED)
            finally:
                conn.close()
        self.assertEqual(missing["status"], PENDING)
        self.assertEqual(empty["status"], PENDING)
        self.assertEqual(invalid["status"], PENDING)
        for row in (missing, empty, invalid):
            self.assertNotIn(SULPHUR, json.loads(row["payload_json"]))
        self.assertEqual(len(summary.previews), 3)

    def test_toggling_releases_only_omitted_local_holds_not_http_400(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_off = make_config(root)
            cfg_on = replace(cfg_off, omit_fields=(SULPHUR,))
            path = cfg_off.watch_folder / "day.csv"
            write_csv(
                path,
                [
                    valid_row(time_text="13:45:02", totalSulphurIncludingH2s_mg_m3="99999"),
                    valid_row(time_text="14:00:02", totalSulphurIncludingH2s_mg_m3=""),
                    valid_row(time_text="14:15:02", totalSulphurIncludingH2s_mg_m3="88888"),
                    valid_row(time_text="14:30:02", totalSulphurIncludingH2s_mg_m3="77777"),
                    valid_row(time_text="14:45:02", totalSulphurIncludingH2s_mg_m3="66666"),
                    valid_row(time_text="15:00:02", methane_mole_percent="0"),
                ],
            )
            conn = store.connect(cfg_off.database_path)
            try:
                first = dry_run(conn, cfg_off)
                self.assertEqual(first.previews, [])
                self.assertEqual(status_of(conn, HUGE), NEEDS_ATTENTION)
                self.assertEqual(status_of(conn, EMPTY), NEEDS_ATTENTION)
                self.assertEqual(status_of(conn, METHANE), NEEDS_ATTENTION)
                conn.execute(
                    "UPDATE readings SET http_status = 400, error = 'HTTP 400 from portal' WHERE reading_at = ?",
                    (REJECTED,),
                )
                conn.execute("UPDATE readings SET status = ? WHERE reading_at = ?", (API_ACCEPTED, ACCEPTED))
                conn.execute("UPDATE readings SET status = ? WHERE reading_at = ?", (UNCERTAIN, UNSURE))
                conn.commit()

                on = dry_run(conn, cfg_on)
                self.assertEqual(on.files_unchanged, 1)
                self.assertEqual(on.released, 2)
                self.assertEqual(sorted(previewed(on)), [HUGE, EMPTY])
                self.assertEqual(status_of(conn, HUGE), PENDING)
                self.assertEqual(status_of(conn, EMPTY), PENDING)
                self.assertNotIn(SULPHUR, json.loads(on.previews[0].body))
                self.assertNotIn(SULPHUR, json.loads(on.previews[1].body))
                self.assertEqual(status_of(conn, METHANE), NEEDS_ATTENTION)
                self.assertIn("methane_mole_percent must be >= 95.0", store.get_by_reading_at(conn, METHANE)["error"])
                self.assertEqual(status_of(conn, REJECTED), NEEDS_ATTENTION)
                self.assertEqual(store.get_by_reading_at(conn, REJECTED)["http_status"], 400)
                self.assertEqual(status_of(conn, ACCEPTED), API_ACCEPTED)
                self.assertEqual(status_of(conn, UNSURE), UNCERTAIN)
                self.assertIn(SULPHUR, json.loads(store.get_by_reading_at(conn, HUGE)["payload_json"]))

                off_again = dry_run(conn, cfg_off)
                huge = store.get_by_reading_at(conn, HUGE)
                empty = store.get_by_reading_at(conn, EMPTY)
                accepted = status_of(conn, ACCEPTED)
                unsure = status_of(conn, UNSURE)
                rejected = store.get_by_reading_at(conn, REJECTED)
            finally:
                conn.close()
        self.assertEqual(off_again.previews, [])
        self.assertEqual(huge["status"], NEEDS_ATTENTION)
        self.assertIn(f"{SULPHUR} must be <= 10.0", huge["error"])
        self.assertEqual(empty["status"], NEEDS_ATTENTION)
        self.assertEqual(accepted, API_ACCEPTED)
        self.assertEqual(unsure, UNCERTAIN)
        self.assertEqual(rejected["status"], NEEDS_ATTENTION)
        self.assertEqual(rejected["http_status"], 400)


if __name__ == "__main__":
    unittest.main()
