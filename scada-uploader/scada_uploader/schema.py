"""Telemetry field names and bounds from Integration Guide V1.3, section 5.

The JSON examples in that section are the source for field spelling. The table
wraps a few names across lines and uses inconsistent quotes; the example
payloads use the names below.
"""

from __future__ import annotations

from decimal import Decimal

# Insertion order is the signed body order (section 5 example, section 8).
FIELD_ORDER = (
    "readingAt",
    "moisture_mg_m3",
    "methane_mole_percent",
    "h2s_mg_m3",
    "totalSulphurIncludingH2s_mg_m3",
    "co2N2O2_mole_percent",
    "co2_mole_percent",
    "oxygen_mole_percent",
    "fresh_slurry_flow_scm_hr",
    "raw_biogas_flow_scm_hr",
    "purified_biogas_flow_scm_hr",
    "dispatch_flow_kg_hr",
    "slurry_FlowTotalizer_scm",
    "raw_FlowTotalizer_scm",
    "purified_FlowTotalizer_scm",
    "dispatch_flowTotalizer_kg",
)

NUMERIC_FIELDS = FIELD_ORDER[1:]

# SCADA export column -> government field. Matching is case-insensitive.
SCADA_COLUMNS = (
    ("MOISTURE SENSOR", "moisture_mg_m3"),
    ("CH4", "methane_mole_percent"),
    ("H2S", "h2s_mg_m3"),
    ("H2S TOT", "totalSulphurIncludingH2s_mg_m3"),
    ("CO2 N2 O2", "co2N2O2_mole_percent"),
    ("CO2", "co2_mole_percent"),
    ("O2", "oxygen_mole_percent"),
    ("FRESH SLURRY ACT FLOW", "fresh_slurry_flow_scm_hr"),
    ("RAW BIOGAS ACT FLOW", "raw_biogas_flow_scm_hr"),
    ("PURIFIED BIOGAS ACT FLOW", "purified_biogas_flow_scm_hr"),
    ("DISPATCH BIOGAS ACT FLOW", "dispatch_flow_kg_hr"),
    ("FRESH SLURRY ACT TOT", "slurry_FlowTotalizer_scm"),
    ("RAW BIOGAS ACT TOT", "raw_FlowTotalizer_scm"),
    ("PURIFIED BIOGAS ACT TOT", "purified_FlowTotalizer_scm"),
    ("DISPATCH BIOGAS ACT TOT", "dispatch_flowTotalizer_kg"),
)

# Inclusive bounds. None means section 5 states no bound on that side.
# Only methane has a minimum. "Max: N" is treated as <= N.
BOUNDS: dict[str, tuple[Decimal | None, Decimal | None]] = {
    "moisture_mg_m3": (None, Decimal("5.0")),
    "methane_mole_percent": (Decimal("95.0"), Decimal("100.0")),
    "h2s_mg_m3": (None, Decimal("3.7")),
    "totalSulphurIncludingH2s_mg_m3": (None, Decimal("10.0")),
    "co2N2O2_mole_percent": (None, Decimal("5.0")),
    "co2_mole_percent": (None, Decimal("4.0")),
    "oxygen_mole_percent": (None, Decimal("0.5")),
}

PENDING = "pending"
API_ACCEPTED = "api_accepted"
NEEDS_ATTENTION = "needs_attention"
UNCERTAIN = "uncertain"
STATUSES = (PENDING, API_ACCEPTED, NEEDS_ATTENTION, UNCERTAIN)
