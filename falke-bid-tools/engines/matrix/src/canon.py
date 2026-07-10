"""
FALKE Matrix Pipeline — Canonical Reference Tables
===================================================
This module is the single source of truth for:
  - The 20 canonical Falke/CSI divisions (DIV 01 through DIV 28, skipping
    14–20 and 24) used in the leveled bid matrix.
  - The `csi_1995_2digit` legacy-code FORMAT PROFILE: the CSI-1995 2-digit →
    canonical 6-digit mapping table, the bid-level signature detector, and the
    ONE split-keyword routing table (firm-agnostic; selected by code signature,
    not by firm name — Marvin §1/§2/§3, Floyd C1/C5).
  - Shared threshold constants consumed by the normalization layer.

Firm-specific quirks (firm-specific reclassifications) live in
`config/known_firms.yaml`, loaded by `firm_config.py` — NOT here.

Importable standalone — no imports from normalize.py or normalized_models.py.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional, Union

# ---------------------------------------------------------------------------
# 1. Canonical Falke divisions
# ---------------------------------------------------------------------------

CANONICAL_DIVISIONS: list[dict] = [
    {
        "csi_code": "DIV 01 00 00",
        "division_name": "General Requirements",
        "sub_lines": [
            "Project Management & Supervision",
            "Site Safety & Security",
            "Temporary Facilities",
            "Waste Management / Dumpsters",
            "General Conditions (other)",
        ],
    },
    {
        "csi_code": "DIV 02 00 00",
        "division_name": "Existing Conditions / Site Construction",
        "sub_lines": [
            "Demolition",
            "Selective Demolition",
            "Site Preparation",
            "Hazardous Material Abatement",
        ],
    },
    {
        "csi_code": "DIV 03 00 00",
        "division_name": "Concrete",
        "sub_lines": [
            "Cast-in-Place Concrete",
            "Concrete Repair",
            "Flatwork & Slabs",
        ],
    },
    {
        "csi_code": "DIV 04 00 00",
        "division_name": "Masonry",
        "sub_lines": [
            "Unit Masonry",
            "Masonry Restoration",
            "Tuckpointing",
        ],
    },
    {
        "csi_code": "DIV 05 00 00",
        "division_name": "Metals",
        "sub_lines": [
            "Structural Steel",
            "Metal Fabrications",
            "Railings & Handrails",
        ],
    },
    {
        "csi_code": "DIV 06 00 00",
        "division_name": "Wood, Plastics & Composites",
        "sub_lines": [
            "Rough Carpentry",
            "Finish Carpentry & Millwork",
        ],
    },
    {
        "csi_code": "DIV 07 00 00",
        "division_name": "Thermal & Moisture Protection",
        "sub_lines": [
            "Waterproofing & Dampproofing",
            "Insulation",
            "Roofing",
            "Sheet Metal & Flashing",
            "Joint Sealants & Caulking",
        ],
    },
    {
        "csi_code": "DIV 08 00 00",
        "division_name": "Openings",
        "sub_lines": [
            "Doors & Frames",
            "Windows",
            "Hardware",
            "Glazing",
        ],
    },
    {
        "csi_code": "DIV 09 00 00",
        "division_name": "Finishes",
        "sub_lines": [
            "Drywall & Plaster",
            "Tile",
            "Flooring",
            "Flooring (Labor)",
            "Painting & Coatings",
            "Acoustic Ceilings",
        ],
    },
    {
        "csi_code": "DIV 10 00 00",
        "division_name": "Specialties",
        "sub_lines": [
            "Signage",
            "Toilet Accessories",
            "Louvers & Vents",
        ],
    },
    {
        "csi_code": "DIV 11 00 00",
        "division_name": "Equipment",
        "sub_lines": [
            "Commercial Equipment",
            "Residential Equipment",
        ],
    },
    {
        "csi_code": "DIV 12 00 00",
        "division_name": "Furnishings",
        "sub_lines": [
            "Window Treatments",
            "Furniture",
        ],
    },
    {
        "csi_code": "DIV 13 00 00",
        "division_name": "Special Construction",
        "sub_lines": [
            "Special Construction (other)",
        ],
    },
    {
        "csi_code": "DIV 21 00 00",
        "division_name": "Fire Suppression",
        "sub_lines": [
            "Sprinkler System",
            "Fire Suppression (other)",
        ],
    },
    {
        "csi_code": "DIV 22 00 00",
        "division_name": "Plumbing",
        "sub_lines": [
            "Domestic Water",
            "Sanitary Waste & Vent",
            "Plumbing Fixtures",
            "Plumbing (other)",
        ],
    },
    {
        "csi_code": "DIV 23 00 00",
        "division_name": "HVAC",
        "sub_lines": [
            "HVAC Equipment",
            "Ductwork & Distribution",
            "Controls & BAS",
            "HVAC (other)",
        ],
    },
    {
        "csi_code": "DIV 26 00 00",
        "division_name": "Electrical",
        "sub_lines": [
            "Service & Distribution",
            "Branch Wiring & Devices",
            "Lighting",
            "Lighting Package",
            "Electrical (other)",
        ],
    },
    {
        "csi_code": "DIV 27 00 00",
        "division_name": "Communications",
        "sub_lines": [
            "Structured Cabling",
            "Data & AV",
        ],
    },
    {
        "csi_code": "DIV 28 00 00",
        "division_name": "Electronic Safety & Security",
        "sub_lines": [
            "Fire Alarm",
            "Access Control",
            "Security (other)",
        ],
    },
    {
        "csi_code": "DIV 25 00 00",
        "division_name": "Integrated Automation",
        "sub_lines": [],
    },
]

# Quick lookup: csi_code → division dict
_CANONICAL_DIVISION_BY_CODE: dict[str, dict] = {
    d["csi_code"]: d for d in CANONICAL_DIVISIONS
}


def get_canonical_division(csi_code: str) -> dict | None:
    """Return the canonical division dict for a CSI code, or None if not found."""
    return _CANONICAL_DIVISION_BY_CODE.get(csi_code)


# ---------------------------------------------------------------------------
# 2. The `csi_1995_2digit` legacy code-format profile (Marvin §3)
# ---------------------------------------------------------------------------
# CSI-1995 2-digit division → canonical 6-digit MasterFormat target. Firm-
# agnostic: selected by CODE SIGNATURE (§1), not by firm name. Values are:
#   - a single canonical CSI code string (straight remap), OR
#   - a list of canonical CSI codes (split remap — requires sub-line analysis), OR
#   - a footer sentinel string starting with "_" (routes to a fee/footer row), OR
#   - the UNMAPPED sentinel (no canonical target in this taxonomy).
#
# Two deliberate non-obvious mappings (Marvin §3, do NOT "fix" to identity):
#   13 → DIV 21 (legacy Special Construction = fire-suppression bucket)
#   15/16 → split targets (Mechanical→Plumbing|HVAC, Electrical→Electrical|FireAlarm)

UNMAPPED_DIVISION = "_UNMAPPED"
"""Sentinel target for a legacy code with no canonical division (e.g. 14 Conveying).
The amount is placed in a clearly-labeled UNMAPPED holding row, never dropped."""

CSI_1995_2DIGIT_MAP: dict[str, Union[str, list[str]]] = {
    "01": "DIV 01 00 00",                        # General Requirements
    "02": "DIV 02 00 00",                        # Site Construction
    "03": "DIV 03 00 00",                        # Concrete
    "04": "DIV 04 00 00",                        # Masonry
    "05": "DIV 05 00 00",                        # Metals
    "06": "DIV 06 00 00",                        # Wood and Plastics
    "07": "DIV 07 00 00",                        # Thermal and Moisture Protection
    "08": "DIV 08 00 00",                        # Openings
    "09": "DIV 09 00 00",                        # Finishes
    "10": "DIV 10 00 00",                        # Specialties
    "11": "DIV 11 00 00",                        # Equipment
    "12": "DIV 12 00 00",                        # Furnishings
    "13": "DIV 21 00 00",                        # Special Construction → Fire Suppression (NOT DIV 13)
    "14": UNMAPPED_DIVISION,                     # Conveying Systems — no canonical target (Marvin §3 note 2)
    "15": ["DIV 22 00 00", "DIV 23 00 00"],      # Mechanical — split: Plumbing | HVAC
    "16": ["DIV 26 00 00", "DIV 28 00 00"],      # Electrical — split: Electrical | Fire Alarm
    "17-050": "DIV 01 00 00",                    # General Conditions footer → rolls into DIV 01
    "17-040": "_GC_FEE_FOOTER",                  # OH&P → gc_fee footer row
    "17-020": "_GL_INSURANCE_FOOTER",            # Insurance → general_liability_insurance footer row
}

# Legacy codes that require sub-line splitting (cannot place amount wholesale).
SPLIT_CODES: set[str] = {"15", "16"}

# The ONE split-keyword routing table (Marvin §2, Floyd C5). Supersedes both the
# old per-firm split-routing constant AND the inline literals that lived in
# normalize.py. For each split code the targets are evaluated IN ORDER: the
# more-specific target FIRST (HVAC before Plumbing; Fire Alarm before Electrical)
# so e.g. "fire alarm wiring" lands in DIV 28, not DIV 26. A sub-line matching no
# target's keywords is CODE_SPLIT_UNMATCHED (RED) — both 15 and 16 share this
# logic; no code is special (the old code-16 always-route bug is removed).
SPLIT_ROUTING: dict[str, list[tuple[str, list[str]]]] = {
    "15": [
        ("DIV 23 00 00", ["hvac", "mechanical", "ductwork", "duct", "air handling",
                          "air handler", "heating", "cooling", "ventilation",
                          "vav", "rtu", "chiller", "condenser", "fan coil"]),
        ("DIV 22 00 00", ["plumbing", "domestic water", "sanitary", "waste", "vent",
                          "fixture", "water heater", "backflow", "riser", "drain", "pump"]),
    ],
    "16": [
        ("DIV 28 00 00", ["fire alarm", "alarm", "detection", "smoke detector",
                          "notification", "facp", "annunciator"]),
        ("DIV 26 00 00", ["electrical", "wiring", "lighting", "light", "service",
                          "distribution", "branch", "panel", "conduit", "feeder",
                          "switchgear", "receptacle", "device"]),
    ],
}

# Named code-format profiles. Today there is one; the value is the mapping table.
CODE_FORMAT_PROFILES: dict[str, dict[str, Union[str, list[str]]]] = {
    "csi_1995_2digit": CSI_1995_2DIGIT_MAP,
}


def resolve_legacy_code(
    code: str, profile: str = "csi_1995_2digit"
) -> Union[str, list[str], None]:
    """Return the canonical CSI code(s) for a legacy 2-digit code under a profile.

    Returns:
      - str: single canonical code OR a footer/UNMAPPED sentinel
      - list[str]: split targets (amount must not be placed wholesale)
      - None: code not in the profile's mapping table
    """
    return CODE_FORMAT_PROFILES.get(profile, {}).get(code)


def is_split_code(code: str, profile: str = "csi_1995_2digit") -> bool:
    """True if this legacy code splits across multiple canonical divisions."""
    return isinstance(resolve_legacy_code(code, profile), list)


def route_split_subline(
    split_code: str, description: str
) -> Optional[str]:
    """Route ONE split-code sub-line to a canonical target by keyword (Marvin §2).

    Targets are tested in declared order (more-specific first). Returns the
    canonical CSI code on a match, or None when the description matches no
    target — the caller treats None as CODE_SPLIT_UNMATCHED (RED).
    """
    desc_lower = description.lower()
    for target_code, keywords in SPLIT_ROUTING.get(split_code, []):
        if any(kw in desc_lower for kw in keywords):
            return target_code
    return None


# ---------------------------------------------------------------------------
# 2b. Code-signature classification + bid-level detection (Marvin §1, Floyd C1)
# ---------------------------------------------------------------------------

# A canonical code is exactly `DIV XX 00 00` where XX is a canonical division.
_CANONICAL_CODE_RE = re.compile(r"^DIV\s+(\d{2})\s+00\s+00$")
_CANONICAL_TWO_DIGITS = frozenset(
    _CANONICAL_CODE_RE.match(d["csi_code"]).group(1) for d in CANONICAL_DIVISIONS
)

# Legacy vocabulary = the CSI-1995 2-digit keys (bare numbers + 17-0xx footers).
_LEGACY_BARE = frozenset(k for k in CSI_1995_2DIGIT_MAP if "-" not in k)  # '01'..'16'
_LEGACY_DISCRIMINATORS = frozenset({"15", "16", "17"})  # only exist in CSI-1995
_LEGACY_FOOTER_RE = re.compile(r"^17-\d{3}$")            # 17-040, 17-050, 17-020


def classify_code_token(code: Optional[str]) -> str:
    """Classify a division code token as CANONICAL | LEGACY_2DIGIT | UNKNOWN (§1.1).

    - CANONICAL: full `DIV XX 00 00` form whose XX is a canonical division.
    - LEGACY_2DIGIT: a bare 1–2 digit CSI-1995 number, a bare `17`, or a `17-0xx`
      footer. A bare 2-digit number that is ALSO a canonical division number is
      LEGACY_2DIGIT (CANONICAL requires the full DIV form — Marvin §1.1).
    - UNKNOWN: anything else (other vocabulary, malformed, free text).
    """
    if not code:
        return "UNKNOWN"
    token = code.strip()
    m = _CANONICAL_CODE_RE.match(token)
    if m and m.group(1) in _CANONICAL_TWO_DIGITS:
        return "CANONICAL"
    norm = token.lstrip("0").zfill(2) if token.isdigit() else token
    if token.isdigit() and norm in _LEGACY_BARE:
        return "LEGACY_2DIGIT"
    if token == "17" or _LEGACY_FOOTER_RE.match(token):
        return "LEGACY_2DIGIT"
    return "UNKNOWN"


def detect_csi_1995_2digit(codes: list[str]) -> bool:
    """Bid-level, all-or-nothing detector for the csi_1995_2digit format (§1.2).

    True iff: n_canonical == 0 AND n_unknown == 0 AND n_legacy_2digit >= 3 AND
    at least one legacy discriminator (15/16/17/17-0xx) is present. Conservative
    by design: any canonical code, any unknown token, or too few legacy codes →
    False → the caller falls to RED UNRECOGNIZED_CODE_FORMAT (no partial remap).
    """
    n_canonical = n_legacy = n_unknown = 0
    has_discriminator = False
    for code in codes:
        cls = classify_code_token(code)
        if cls == "CANONICAL":
            n_canonical += 1
        elif cls == "LEGACY_2DIGIT":
            n_legacy += 1
            token = (code or "").strip()
            norm = token.lstrip("0").zfill(2) if token.isdigit() else token
            if norm in _LEGACY_DISCRIMINATORS or token == "17" \
                    or _LEGACY_FOOTER_RE.match(token):
                has_discriminator = True
        else:
            n_unknown += 1
    return (
        n_canonical == 0
        and n_unknown == 0
        and n_legacy >= 3
        and has_discriminator
    )


# ---------------------------------------------------------------------------
# 3. Firm-specific reclassification rules
# ---------------------------------------------------------------------------
# REMOVED from engine source: firm-specific reclass rules (the old hardcoded
# per-firm reclassifications) now live in config/known_firms.yaml and are loaded
# by firm_config.py — no firm names in engine source (Marvin §4, Floyd C4/M4).


# ---------------------------------------------------------------------------
# 4. Threshold constants
# ---------------------------------------------------------------------------

SCOPE_GAP_MEDIAN_THRESHOLD: Decimal = Decimal("20000")
"""
Minimum field-median value for a canonical division before a NULL_BLANK cell
is promoted to a SCOPE_GAP_IMPLICIT flag.  A blank cell in a division where
all other bidders priced > $20,000 is a meaningful potential scope gap.
"""

GC_FEE_OUTLIER_STDDEV: int = 2
"""
Number of standard deviations from the field mean beyond which a GC fee
percentage is flagged as an outlier (Rule 5 Phase 2).
"""


# ---------------------------------------------------------------------------
# 5. Field-median computation helpers
# ---------------------------------------------------------------------------

def compute_field_medians(
    division_subtotals_by_bid: list[dict[str, "Decimal | None"]],
) -> dict[str, "Decimal"]:
    """
    Compute the field median subtotal for each canonical CSI division across
    all bids.  Used to determine whether a NULL_BLANK cell represents a
    meaningful scope gap.

    Args:
        division_subtotals_by_bid: list of dicts, one per bid, mapping
            csi_code → subtotal Decimal (or None if not priced).

    Returns:
        dict mapping csi_code → median Decimal (0 if no bids priced the division).
    """
    # Collect all values per division
    values_by_code: dict[str, list[Decimal]] = {}
    for bid_subtotals in division_subtotals_by_bid:
        for code, amount in bid_subtotals.items():
            if amount is not None and amount > Decimal("0"):
                values_by_code.setdefault(code, []).append(amount)

    medians: dict[str, Decimal] = {}
    for code, values in values_by_code.items():
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        if n == 0:
            medians[code] = Decimal("0")
        elif n % 2 == 1:
            medians[code] = sorted_vals[n // 2]
        else:
            mid = n // 2
            medians[code] = (sorted_vals[mid - 1] + sorted_vals[mid]) / Decimal("2")

    return medians
