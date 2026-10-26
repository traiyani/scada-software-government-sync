"""Local checks against the section 5 constraints.

Out-of-range values are kept as measured. They are not rewritten so a row
can pass. Section 6 returns HTTP 400 when a posted value is outside the
constraints (the example is methane below 95.0).
"""

from __future__ import annotations

import json
from decimal import Decimal

from scada_uploader.schema import BOUNDS, FIELD_ORDER, NUMERIC_FIELDS


def validation_errors(
    values: dict[str, Decimal],
    *,
    check_ranges: bool = True,
    omit_fields: tuple[str, ...] | list[str] = (),
) -> list[str]:
    errors: list[str] = []
    omit = set(omit_fields)
    for name in NUMERIC_FIELDS:
        if name in omit:
            continue
        if name not in values:
            errors.append(f"{name} is required")
            continue
        number = values[name]
        if not number.is_finite():
            errors.append(f"{name} is not a finite number")
            continue
        if not check_ranges:
            continue
        lower, upper = BOUNDS.get(name, (None, None))
        if lower is not None and upper is not None and (number < lower or number > upper):
            errors.append(f"{name} must be >= {lower} and <= {upper} (got {number})")
        elif lower is not None and number < lower:
            errors.append(f"{name} must be >= {lower} (got {number})")
        elif upper is not None and number > upper:
            errors.append(f"{name} must be <= {upper} (got {number})")
    return errors


def errors_in_payload(
    payload: str,
    *,
    check_ranges: bool = True,
    omit_fields: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Re-check a stored object before it is placed on the wire."""
    omit = set(omit_fields)
    try:
        data = json.loads(payload, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError:
        return ["payload is not valid JSON"]
    if not isinstance(data, dict):
        return ["payload is not a single telemetry object"]
    missing = [name for name in FIELD_ORDER if name not in omit and name not in data]
    if missing:
        return ["payload missing " + ", ".join(missing)]
    if not isinstance(data["readingAt"], str) or not data["readingAt"]:
        return ["readingAt is empty"]
    numbers: dict[str, Decimal] = {}
    for name in NUMERIC_FIELDS:
        if name in omit:
            continue
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, Decimal):
            return [f"{name} is not a JSON number"]
        numbers[name] = value
    return validation_errors(numbers, check_ranges=check_ranges, omit_fields=omit_fields)
