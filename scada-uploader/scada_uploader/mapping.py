"""Map a SCADA CSV row onto the section 5 JSON object.

Date and Time are the measurement clock (readingAt), including backfill.
They are not the authentication timestamp.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scada_uploader.schema import (
    NEEDS_ATTENTION,
    PENDING,
    SCADA_COLUMNS,
)
from scada_uploader.signing import render_payload
from scada_uploader.validate import validation_errors

_JSON_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?")
_SCIENTIFIC = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?[eE][+-]?\d{1,3}")
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_OFFSET = re.compile(r"([+-])(\d{2}):?(\d{2})")

# Asia/Kolkata has no daylight-saving change. A fixed offset avoids the
# tzdata package on Windows SCADA PCs, where zoneinfo has no system database.
FIXED_OFFSETS = {
    "Asia/Kolkata": timedelta(hours=5, minutes=30),
    "Asia/Calcutta": timedelta(hours=5, minutes=30),
    "UTC": timedelta(0),
}


@dataclass(frozen=True)
class MappedRow:
    reading_at: str
    payload_json: str
    status: str
    error: str | None
    line_number: int


def normalize_header(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold()


def resolve_timezone(name: str, offset_text: str):
    """Return a tzinfo. Fixed offsets win for the names in FIXED_OFFSETS."""
    if name in FIXED_OFFSETS:
        return timezone(FIXED_OFFSETS[name])
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
        if offset_text:
            return timezone(parse_offset(offset_text))
        raise


def parse_offset(text: str) -> timedelta:
    match = _OFFSET.fullmatch(text.strip())
    if not match:
        raise ValueError(f"utc_offset must look like +05:30, got {text!r}")
    sign = 1 if match.group(1) == "+" else -1
    return timedelta(
        hours=sign * int(match.group(2)),
        minutes=sign * int(match.group(3)),
    )


def format_reading_at(moment: datetime, tzinfo) -> str:
    """ISO-8601 with a numeric offset, matching the section 5 example."""
    if moment.tzinfo is not None:
        raise ValueError("measurement time must be naive local SCADA time")
    aware = moment.replace(tzinfo=tzinfo)
    if aware.microsecond:
        text = aware.isoformat(timespec="microseconds")
        return _trim_fraction(text)
    return aware.isoformat(timespec="seconds")


def parse_measurement_time(
    date_text: str,
    time_text: str,
    date_formats: Iterable[str],
    time_formats: Iterable[str],
) -> datetime:
    date_text = (date_text or "").strip()
    time_text = (time_text or "").strip()
    if not date_text:
        raise ValueError("Date is empty")
    if time_text:
        for date_fmt in date_formats:
            for time_fmt in time_formats:
                try:
                    return datetime.strptime(
                        f"{date_text} {time_text}",
                        f"{date_fmt} {time_fmt}",
                    )
                except ValueError:
                    continue
    combined_formats = [f"{date_fmt} {time_fmt}" for date_fmt in date_formats for time_fmt in time_formats]
    combined_formats.extend(date_formats)
    candidates = [f"{date_text} {time_text}".strip()] if time_text else [date_text]
    # A Date cell sometimes already contains the clock time.
    candidates.append(date_text)
    for candidate in candidates:
        for fmt in combined_formats:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    raise ValueError(f"unrecognized Date/Time {date_text!r} {time_text!r}")


def parse_number(raw: str) -> tuple[Decimal, str]:
    """Keep the CSV numeric token. Do not pass it through binary float."""
    text = (raw or "").strip().replace(" ", "")
    if not text:
        raise ValueError("empty number")
    if _THOUSANDS.fullmatch(text):
        text = text.replace(",", "")
    if text.startswith("+"):
        text = text[1:]
    scientific = _SCIENTIFIC.fullmatch(text) is not None
    if not scientific and not _JSON_NUMBER.fullmatch(text):
        raise ValueError(f"not a number: {raw!r}")
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError(f"not a finite number: {raw!r}")
    if scientific:
        text = format(number, "f")
    return number, text


def map_row(
    cells: Mapping[str, str],
    *,
    line_number: int,
    source_name: str,
    tzinfo,
    date_formats: Iterable[str],
    time_formats: Iterable[str],
    original_headers: Mapping[str, str],
    omit_fields: Iterable[str] = (),
    check_ranges: bool = True,
) -> MappedRow:
    """original_headers maps a normalized header to the CSV header text."""
    omit = set(omit_fields)

    def cell(name: str) -> str | None:
        original = original_headers.get(normalize_header(name))
        if original is None:
            return None
        value = cells.get(original)
        return "" if value is None else str(value).strip()

    problems: list[str] = []
    try:
        moment = parse_measurement_time(
            cell("Date") or "",
            cell("Time") or "",
            date_formats,
            time_formats,
        )
        reading_at = format_reading_at(moment, tzinfo)
    except ValueError as exc:
        reading_at = f"unparsed:{source_name}:{line_number}"
        problems.append(str(exc))

    numbers: dict[str, Decimal] = {}
    literals: dict[str, str] = {}
    required = [(scada_name, field) for scada_name, field in SCADA_COLUMNS if field not in omit]
    for scada_name, field in required:
        raw = cell(scada_name)
        if raw is None:
            problems.append(f"missing column {scada_name}")
            continue
        try:
            number, literal = parse_number(raw)
        except ValueError as exc:
            problems.append(f"{field}: {exc}")
            continue
        numbers[field] = number
        literals[field] = literal

    if not problems:
        problems.extend(validation_errors(numbers, check_ranges=check_ranges, omit_fields=omit))

    if reading_at.startswith("unparsed:") or any(field not in literals for _name, field in required):
        payload = json.dumps(
            {"readingAt": None if reading_at.startswith("unparsed:") else reading_at, "sourceRow": dict(cells)},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return MappedRow(
            reading_at=reading_at,
            payload_json=payload,
            status=NEEDS_ATTENTION,
            error="; ".join(problems) or "row could not be mapped",
            line_number=line_number,
        )

    payload = render_payload(reading_at, literals, omit_fields=omit)
    if problems:
        return MappedRow(reading_at, payload, NEEDS_ATTENTION, "; ".join(problems), line_number)
    return MappedRow(reading_at, payload, PENDING, None, line_number)


def _trim_fraction(iso_text: str) -> str:
    split_at = max(iso_text.rfind("+"), iso_text.rfind("-"))
    # The date portion also contains hyphens; the offset sign is the later one
    # only when it sits after the 'T'. If rfind hits the date, keep the text.
    if split_at <= iso_text.find("T"):
        return iso_text
    main = iso_text[:split_at]
    offset = iso_text[split_at:]
    if "." in main:
        main = main.rstrip("0").rstrip(".")
    return main + offset
