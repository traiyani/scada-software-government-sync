import unittest
from datetime import timedelta, timezone
from pathlib import Path

from scada_uploader.mapping import format_reading_at, map_row, parse_measurement_time, parse_number
from scada_uploader.reader import is_stable_pair, read_export_text, wait_until_stable
from scada_uploader.schema import FIELD_ORDER, NEEDS_ATTENTION, PENDING, SCADA_COLUMNS
from scada_uploader.validate import validation_errors
from tests.helpers import HEADER, valid_row

IST = timezone(timedelta(hours=5, minutes=30))
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")
TIME_FORMATS = ("%H:%M:%S", "%H:%M:%S.%f", "%H:%M")


def _mapped(row):
    cells = dict(zip(HEADER, row))
    headers = {name.casefold(): name for name in HEADER}
    return map_row(
        cells,
        line_number=2,
        source_name="unit.csv",
        tzinfo=IST,
        date_formats=DATE_FORMATS,
        time_formats=TIME_FORMATS,
        original_headers=headers,
    )


class MappingTests(unittest.TestCase):
    def test_columns_become_guide_fields_with_kolkata_offset(self):
        mapped = _mapped(valid_row())
        self.assertEqual(mapped.status, PENDING)
        self.assertIsNone(mapped.error)
        self.assertEqual(mapped.reading_at, "2026-10-23T13:45:02+05:30")
        self.assertIn('"readingAt":"2026-10-23T13:45:02+05:30"', mapped.payload_json)
        positions = [mapped.payload_json.index(f'"{name}"') for name in FIELD_ORDER]
        self.assertEqual(positions, sorted(positions))
        for _scada, field in SCADA_COLUMNS:
            self.assertIn(f'"{field}":', mapped.payload_json)
        self.assertNotRegex(mapped.payload_json, r"\s")

    def test_day_first_date(self):
        moment = parse_measurement_time("23-10-2026", "13:45:02", DATE_FORMATS, TIME_FORMATS)
        self.assertEqual(format_reading_at(moment, IST), "2026-10-23T13:45:02+05:30")

    def test_original_zero_and_decimal_text_are_preserved(self):
        number, literal = parse_number("0")
        self.assertEqual(literal, "0")
        self.assertEqual(number, 0)
        _number, thousands = parse_number("12,548,750.75")
        self.assertEqual(thousands, "12548750.75")
        mapped = _mapped(valid_row(methane_mole_percent="0", h2s_mg_m3="0"))
        self.assertEqual(mapped.status, NEEDS_ATTENTION)
        self.assertIn('"methane_mole_percent":0', mapped.payload_json)
        self.assertIn('"h2s_mg_m3":0', mapped.payload_json)
        self.assertNotIn('"methane_mole_percent":95', mapped.payload_json)

    def test_methane_bounds_are_inclusive_and_other_zeros_are_allowed(self):
        from decimal import Decimal

        self.assertEqual(validation_errors({"methane_mole_percent": Decimal("95.0"), **_rest("96.8")}), [])
        # Rebuild with explicit methane edges through the public mapper.
        self.assertEqual(_mapped(valid_row(methane_mole_percent="95")).status, PENDING)
        self.assertEqual(_mapped(valid_row(methane_mole_percent="100")).status, PENDING)
        low = _mapped(valid_row(methane_mole_percent="94.9"))
        high = _mapped(valid_row(methane_mole_percent="100.1"))
        self.assertEqual(low.status, NEEDS_ATTENTION)
        self.assertIn("methane_mole_percent must be >= 95.0 and <= 100.0 (got 94.9)", low.error)
        self.assertIn("got 100.1", high.error)
        self.assertEqual(_mapped(valid_row(moisture_mg_m3="5")).status, PENDING)
        wet = _mapped(valid_row(moisture_mg_m3="5.1"))
        self.assertEqual(wet.status, NEEDS_ATTENTION)
        self.assertIn('"moisture_mg_m3":5.1', wet.payload_json)
        zeros = valid_row(**{field: "0" for _name, field in SCADA_COLUMNS})
        zeros[HEADER.index("CH4")] = "96.8"
        held = _mapped(zeros)
        self.assertEqual(held.status, PENDING, held.error)
        self.assertIn('"fresh_slurry_flow_scm_hr":0', held.payload_json)

    def test_sample_file_has_two_zero_methane_rows(self):
        sample = Path(__file__).resolve().parents[1] / "samples" / "SAMPLE_ONLY_two_zero_rows.csv"
        parsed = read_export_text(
            sample.read_text(encoding="utf-8"),
            source_name=sample.name,
            tzinfo=IST,
            date_formats=DATE_FORMATS,
            time_formats=TIME_FORMATS,
        )
        self.assertIsNone(parsed.error)
        self.assertEqual(len(parsed.rows), 2)
        self.assertEqual(
            [row.reading_at for row in parsed.rows],
            ["2026-10-23T13:45:02+05:30", "2026-10-23T13:46:02+05:30"],
        )
        for row in parsed.rows:
            self.assertEqual(row.status, NEEDS_ATTENTION)
            self.assertEqual(row.error, "methane_mole_percent must be >= 95.0 and <= 100.0 (got 0)")
            self.assertIn('"methane_mole_percent":0', row.payload_json)

    def test_unstable_file_is_detected_without_a_real_wait(self):
        self.assertTrue(is_stable_pair((10, 5), (10, 5)))
        self.assertFalse(is_stable_pair((10, 5), (11, 5)))
        self.assertFalse(is_stable_pair((0, 1), (0, 1)))

        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "growing.csv"
            path.write_text("Date,Time\n", encoding="utf-8")

            def grow(_seconds):
                path.write_text("Date,Time\n2026-10-23,13:45:02\n", encoding="utf-8")

            self.assertFalse(wait_until_stable(path, 1, grow))


def _rest(methane: str):
    from decimal import Decimal

    values = {}
    for (_name, field), literal in zip(SCADA_COLUMNS, [
        "3.2", methane, "1.9", "7.4", "3.6", "2.8", "0.3",
        "25.4", "120.5", "110.2", "450.75", "1", "1", "1", "1",
    ]):
        values[field] = Decimal(literal)
    return {key: value for key, value in values.items() if key != "methane_mole_percent"}


if __name__ == "__main__":
    unittest.main()
