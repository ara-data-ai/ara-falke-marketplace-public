"""Scorecard run-pack SCHEMA — the single source of truth for every label,
sheet name, enum, and stamp property in the pack (P1-4; Marvin's run-pack tab
ratification 2026-07-16).

WHY THIS MODULE EXISTS, AND WHY IT IS STDLIB-ONLY
-------------------------------------------------
The pack is a PRODUCER -> CONSUMER artifact: the matrix engine writes it, the
scorecard engine reads it. Floyd's verdict (c) ruled that schema dependency
acceptable *because both engines ship in one plugin at one version*. This
module is that one version's schema, and it is imported by BOTH engines:

  * engines/scorecard/scorecard/run_pack.py   (the parser — imports normally)
  * engines/matrix/src/scorecard_pack.py      (the emitter — imports by path)

It therefore depends on NOTHING but the standard library: no openpyxl, no
sibling scorecard modules. Anything that needs a third-party library belongs in
the emitter or the parser, never here. Duplicating a label across the two
engines instead of importing it from here is the drift that P0-1 existed to
kill — don't.

WHAT THIS MODULE IS NOT
-----------------------
It is not enforcement. The stamp is data; the enforcement is the LIVE
cross-engine test (tests/test_producer_live_compat.py), which emits a pack with
the in-tree matrix engine and consumes it through this scorecard. That is the
P0-2 lesson: a schema shared in source but never executed across the seam is a
contract with a dead counterparty.

THE BINDING DESIGN RULES (Marvin §2) THAT SHAPE EVERYTHING BELOW
---------------------------------------------------------------
R1  No cell in the pack may satisfy a gate. There is NO confirmation field in
    this schema — not `sf_confirmed`, not `baseline_confirmed`, not
    `audit: skip`. A field that does not exist cannot be auto-satisfied, cannot
    be hand-added by a helpful operator (R5 rejects unknown keys), and cannot
    be built in later by a maintainer chasing convenience. If you are reading
    this because you are about to add one: that is the single most important
    line in the ruling. Don't.
R2  Label-addressed, never row-index-addressed. Every scalar is found by its
    label in column A; every table by a header-row scan; every block ends on a
    fully-blank row. No parser in the pack path may hard-code a row number.
R3  "Locked" in xlsx is advisory UI, never integrity. The parser re-derives
    every producer-filled field from the matrix (or from the authoritative doc
    properties) and compares. It never trusts the lock.
R5  Unknown keys are rejected loudly (exit 2), never ignored. This is what
    makes R1 enforceable.
R6  One home per fact. The band lives on Baseline. Firms live on Scores.
    Aliases live on Settings. Nowhere else.
R7  The pack is an award-file artifact. Everything in it is a durable fact
    about the EVALUATION. Nothing in it is a fact about the SESSION — no
    machine paths, no save preferences, no recipients, no --sheet selection
    (Marvin §7.2; a `sheet` cell would be a gate bypass by data).
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# Pack format version — the pack's OWN schema version, independent of the
# matrix's PRODUCER_FORMAT_VERSION (Marvin §3.1).
# ---------------------------------------------------------------------------
PACK_FORMAT_VERSION = "1.0"

# The scorecard's accepted (min, max) inclusive range, as (major, minor) pairs.
# I1: a pack outside this range hard-stops (exit 2) — schema mismatch means the
# parser cannot be trusted to read the file. TRIPWIRE: any change to the label
# set, the sheet names, or the block structure below must bump
# PACK_FORMAT_VERSION and revisit this range in the SAME commit.
SUPPORTED_PACK_FORMAT = ((1, 0), (1, 0))

# ---------------------------------------------------------------------------
# Sheet names (exact — part of the contract, Marvin §3)
# ---------------------------------------------------------------------------
SHEET_SETTINGS = "Settings"
SHEET_BASELINE = "Baseline"
SHEET_FRAMEWORK = "Framework"
SHEET_SCORES = "Scores"

# The pack's tab list is FOUR (Marvin §11). The qualification register and the
# alternates/allowances register (P2-7) are NOT ride-alongs; when they are
# greenlit, R2's label addressing means a fifth tab is additive.
PACK_SHEETS: Tuple[str, ...] = (
    SHEET_SETTINGS, SHEET_BASELINE, SHEET_FRAMEWORK, SHEET_SCORES)

# ---------------------------------------------------------------------------
# The pack's own stamp — workbook CUSTOM DOCUMENT PROPERTIES (Marvin §8.1).
# Invisible; never visible geometry. Mirrors the matrix's _stamp_workbook
# pattern exactly. These properties are AUTHORITATIVE; the human-readable
# Settings rows that echo them are courtesy for the archived-artifact reader
# (R7). Where a fact is not re-derivable from the matrix (the standing-framework
# reference), the property is the ONLY integrity anchor — which is why the
# standing hash is stamped and not merely printed.
# ---------------------------------------------------------------------------
PACK_STAMP_FORMAT_PROP = "falke_bid_tools.pack_format_version"
PACK_STAMP_PRODUCER_PROP = "falke_bid_tools.producer"
PACK_STAMP_MATRIX_FORMAT_PROP = "falke_bid_tools.matrix_format_version"
PACK_STAMP_MATRIX_RUN_ID_PROP = "falke_bid_tools.matrix_run_id"
PACK_STAMP_STANDING_VERSION_PROP = "falke_bid_tools.standing_framework_version"
PACK_STAMP_STANDING_DATE_PROP = "falke_bid_tools.standing_framework_effective_date"
PACK_STAMP_STANDING_HASH_PROP = "falke_bid_tools.standing_framework_hash"

# ---------------------------------------------------------------------------
# Settings tab — the identity-and-binding tab (Marvin §3.1)
# ---------------------------------------------------------------------------
S_PACK_FORMAT_VERSION = "Pack Format Version"
S_PRODUCER = "Producer"
S_MATRIX_FORMAT_VERSION = "Matrix Format Version"
S_MATRIX_RUN_ID = "Matrix Run ID"
S_MATRIX_FILE_NAME = "Matrix File Name"
S_EMITTED_AT = "Emitted At"
S_PROJECT_NAME = "Project Name"
S_PROJECT_ADDRESS = "Project Address"
S_SF_BASIS_VALUE = "SF Basis (Value)"
S_SF_BASIS_LABEL = "SF Basis (Label)"
S_STANDING_VERSION = "Standing Framework Version"
S_STANDING_EFFECTIVE = "Standing Framework Effective Date"
S_STANDING_HASH = "Standing Framework Hash"
S_BID_OPENING_DATE = "Bid Opening Date"
S_ADDENDA_THROUGH = "Addenda Through"

# Producer-filled, locked. Order is the emitted row order.
SETTINGS_PRODUCER_FIELDS: Tuple[str, ...] = (
    S_PACK_FORMAT_VERSION,
    S_PRODUCER,
    S_MATRIX_FORMAT_VERSION,
    S_MATRIX_RUN_ID,
    S_MATRIX_FILE_NAME,
    S_EMITTED_AT,
    S_PROJECT_NAME,
    S_PROJECT_ADDRESS,
    S_SF_BASIS_VALUE,
    S_SF_BASIS_LABEL,
    S_STANDING_VERSION,
    S_STANDING_EFFECTIVE,
    S_STANDING_HASH,
)

# Operator-entry. `Bid Opening Date` is REQUIRED — it is the clock reference for
# the W3 coherence check and the producer cannot know it. `Addenda Through` is
# the P1-9 seam: carried and recorded now, enforced later.
SETTINGS_OPERATOR_FIELDS: Tuple[str, ...] = (
    S_BID_OPENING_DATE,
    S_ADDENDA_THROUGH,
)

SETTINGS_SCALAR_FIELDS: Tuple[str, ...] = (
    SETTINGS_PRODUCER_FIELDS + SETTINGS_OPERATOR_FIELDS)

SETTINGS_REQUIRED_OPERATOR_FIELDS: Tuple[str, ...] = (S_BID_OPENING_DATE,)

# ---- Settings table blocks (label row, then a header row, then data) --------
T_DISPLAY_ALIASES = "Display Aliases"
T_MATRIX_EXCLUSIONS = "Matrix Exclusions"
T_ADDITIONAL_EXCLUSIONS = "Additional Exclusions"

ALIAS_HEADERS: Tuple[str, str] = ("Matrix Name", "Display Name")
EXCLUSION_HEADERS: Tuple[str, str] = ("Firm", "Reason")

SETTINGS_TABLE_BLOCKS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (T_DISPLAY_ALIASES, ALIAS_HEADERS),
    (T_MATRIX_EXCLUSIONS, EXCLUSION_HEADERS),
    (T_ADDITIONAL_EXCLUSIONS, EXCLUSION_HEADERS),
)

# ---------------------------------------------------------------------------
# Baseline tab — the cost yardstick and its provenance (Marvin §3.2)
# ---------------------------------------------------------------------------
# Semantics are UNCHANGED from baseline-template.xlsx (band + trade lines).
# What is new is the P1-6 provenance block, shaped now and sitting in the
# label-addressed header block ABOVE the trade lines so P1-6 adds rows without
# shifting an index (§6.1). Entirely operator-entered: the producer knows
# nothing about Falke's estimate and must never appear to.
#
# There is NO `Baseline Confirmed` field. Deliberately (R1, §5).
B_BAND_LOW = "Band Low ($M)"
B_BAND_HIGH = "Band High ($M)"
B_BAND_MID = "Band Mid ($M)"
B_PROVENANCE = "Provenance"
B_ESTIMATOR = "Estimator of Record"
B_BASIS_DATE = "Basis Date"
B_BASIS_DOCUMENTS = "Basis Documents"
B_MI_SIRS_DERIVED = "MI/SIRS-Derived Project?"
B_MI_SIRS_CONFLICT = "MI/SIRS Performed by Estimator or Any Bidder?"

BASELINE_BAND_FIELDS: Tuple[str, str, str] = (B_BAND_LOW, B_BAND_HIGH, B_BAND_MID)

# P1-6 SEAM. These fields are EMITTED, PARSED, and RECORDED in scorecard_run.json
# at P1-4. `Provenance` + `Estimator of Record` are REQUIRED now (two cells) so
# the award file carries the estimator of record from the first pack run forward
# and P1-6 is purely additive. What P1-6 adds LATER — and what P1-4 must NOT
# build: declaration-keyed document language, the fingerprint-contradiction gate
# (Q6.4), the circularity rule (Q6.3), the provenance-consistency and
# anchored-bidder audit checks (Q5.4-5), and SB 4-D / HB 913 attestation
# enforcement. A required-but-not-yet-rendered declaration is not harmful;
# silence is P0-5's designed neutral state (§6.2).
BASELINE_PROVENANCE_FIELDS: Tuple[str, ...] = (
    B_PROVENANCE,
    B_ESTIMATOR,
    B_BASIS_DATE,
    B_BASIS_DOCUMENTS,
    B_MI_SIRS_DERIVED,
    B_MI_SIRS_CONFLICT,
)

BASELINE_SCALAR_FIELDS: Tuple[str, ...] = (
    BASELINE_BAND_FIELDS + BASELINE_PROVENANCE_FIELDS)

BASELINE_REQUIRED_FIELDS: Tuple[str, ...] = (
    B_BAND_LOW, B_BAND_HIGH, B_BAND_MID, B_PROVENANCE, B_ESTIMATOR)

# Enum for B_PROVENANCE. The tool does not know the fact, so it must not assert
# it and must not guess it (§4.2 / F1).
PROVENANCE_INDEPENDENT = "independent"
PROVENANCE_BID_INFORMED = "bid-informed"
PROVENANCE_BID_DERIVED = "bid-derived"
PROVENANCE_VALUES: Tuple[str, ...] = (
    PROVENANCE_INDEPENDENT, PROVENANCE_BID_INFORMED, PROVENANCE_BID_DERIVED)

BASELINE_TRADE_HEADERS: Tuple[str, ...] = (
    "Scope", "Basis", "Cost ($)", "Value", "Kind")

# ---------------------------------------------------------------------------
# Framework tab — the evaluation PLAN (Marvin §3.3)
# ---------------------------------------------------------------------------
F_BASIS = "Framework Basis"
F_LOCK_DATE = "Framework Lock Date"
F_RULING_NOTE = "Ruling Note"

FRAMEWORK_SCALAR_FIELDS: Tuple[str, ...] = (F_BASIS, F_LOCK_DATE, F_RULING_NOTE)

# The declaration (§4.2). Provenance of the PLAN is a declared input, not an
# inferred fact — the same move F1 forced for baseline provenance, for the same
# reason. Note the design property: declaring `standing` costs the operator
# nothing and requires no dates. THE HONEST PATH IS THE LAZY PATH. That is
# deliberate — a control that makes integrity the cheapest option is the only
# kind that survives a deadline. Do not add friction to the `standing` path.
BASIS_STANDING = "standing"
BASIS_PROJECT_SPECIFIC = "project-specific"
BASIS_REVISED_POST_OPENING = "revised-post-opening"
FRAMEWORK_BASIS_VALUES: Tuple[str, ...] = (
    BASIS_STANDING, BASIS_PROJECT_SPECIFIC, BASIS_REVISED_POST_OPENING)

# A Ruling Note is REQUIRED for any basis other than `standing` (W7 -> exit 2:
# an input-gate stop, not an audit finding — a required cell is empty).
FRAMEWORK_BASES_REQUIRING_NOTE: Tuple[str, ...] = (
    BASIS_PROJECT_SPECIFIC, BASIS_REVISED_POST_OPENING)

FRAMEWORK_HEADERS: Tuple[str, ...] = (
    "Category", "Short Label", "Weight (%)", "What it captures")

# ---------------------------------------------------------------------------
# Scores tab — the evaluation RECORD (Marvin §3.4)
# ---------------------------------------------------------------------------
SC_SCORING_COMPLETED_DATE = "Scoring Completed Date"
SCORES_SCALAR_FIELDS: Tuple[str, ...] = (SC_SCORING_COMPLETED_DATE,)
SCORES_REQUIRED_FIELDS: Tuple[str, ...] = (SC_SCORING_COMPLETED_DATE,)
SCORES_FIRM_HEADER = "Firm"

# ---------------------------------------------------------------------------
# Falke's STANDING framework file (Marvin §4.3 / §10.2) — THE SEAM
# ---------------------------------------------------------------------------
# THIS ARTIFACT DOES NOT EXIST TODAY. Falke has no standing evaluation
# framework on file, so the drift check degrades to W8 (WARN, always) and the
# card claims nothing about policy drift. Read §10.2 before touching this:
#
#   * It must be a FALKE-owned, firm-level file (`standing-framework.xlsx`),
#     versioned and dated, stored beside their project files.
#   * It must NOT be the engine's `weights` config block (slated for deletion
#     in P2-2, keyed on eight hard-coded slugs, and decisively: it is a VENDOR
#     artifact — an owner's-rep firm's evaluation policy cannot live in its
#     software vendor's source tree; that is a fiduciary objection, not a
#     technical one).
#   * It must NOT be DEFAULT_FRAMEWORK_ROWS below. Measuring drift from ARA's
#     shipped default would be the tool asserting a fact it does not know —
#     F1's error with a different subject.
#
# When Falke adopts one, it slots in HERE with no redesign: the emitter already
# takes an optional path (--standing-framework), the hash is already computed
# and stamped, and the W1/W2 BLOCKER tiers already exist and are already
# tested. The only thing that changes is that W8 stops firing.
STANDING_SHEET = "Standing_Framework"
SF_VERSION = "Version"
SF_EFFECTIVE_DATE = "Effective Date"
STANDING_SCALAR_FIELDS: Tuple[str, ...] = (SF_VERSION, SF_EFFECTIVE_DATE)

# The literal Marvin specifies for `Standing Framework Version` when no standing
# framework file was supplied (§3.1). The card's W8 language keys on this: it
# must not claim a standing framework that does not exist (§4.5).
STANDING_NONE = "none (shipped default)"

# ---------------------------------------------------------------------------
# ARA's shipped DEFAULT framework — starting CONTENT, explicitly NOT a standing
# framework (§10.2). This is a verbatim copy of the rows in the shipped
# templates/scoring-framework-template.xlsx; tests/test_run_pack.py asserts the
# two agree so they cannot drift. The pack pre-fills the Framework tab from it
# when Falke has no standing file, and Settings then reads
# `Standing Framework Version = none (shipped default)` — an honest statement
# that no reference existed, never a claim that this one is Falke's policy.
# ---------------------------------------------------------------------------
DEFAULT_FRAMEWORK_ROWS: Tuple[Tuple[str, str, float, str], ...] = (
    ("Market-aligned pricing", "Pricing", 25,
     "Closeness to takeoff baseline and South Florida $/SF realism (reduces "
     "CO drift probability)."),
    ("Scope completeness / clarity", "Scope", 15,
     "Quality of inclusions/exclusions/allowances; fewer silent omissions "
     "score higher."),
    ("Condo-specific execution experience", "Condo Exp", 15,
     "Occupied high-rise/common-area execution (phasing, protection, "
     "logistics, resident sensitivity)."),
    ("Change order exposure risk", "CO Risk", 15,
     "Likelihood of drift based on under-baseline pricing, allowance "
     "structure, and coordination triggers."),
    ("Reputation & longevity", "Reputation", 10,
     "Public footprint and credibility signals (track record, references)."),
    ("Financial strength / stability", "Financial", 10,
     "Implied resilience (cash flow, ability to carry subs) given price "
     "posture and scale."),
    ("Project controls & infrastructure", "Controls", 5,
     "RFI/submittal/change control discipline; tools and reporting cadence."),
    ("Documentation quality / professionalism", "Docs", 5,
     "Completeness/accuracy of forms; clarity and consistency of submission."),
)

# ---------------------------------------------------------------------------
# Shared normalization + the semantic hash
# ---------------------------------------------------------------------------


def norm_label(value) -> str:
    """Case/space-insensitive label key ('Band  Low ($M)' -> 'band low ($m)').

    Every label lookup in the pack path goes through this (R2). Kept here, in
    the stdlib-only schema module, so the emitter and the parser normalize
    IDENTICALLY — a label that round-trips through two different normalizers is
    a label that eventually fails to round-trip at all.
    """
    return re.sub(r"\s+", " ", str(value if value is not None else "").strip().lower())


def _slug(value) -> str:
    """Letters+digits only, lowercased — the key the semantic hash sorts on."""
    return re.sub(r"[^a-z0-9]+", "", str(value if value is not None else "").lower())


def framework_semantic_hash(rows: Iterable[Sequence]) -> str:
    """SHA-256 over the normalized, label-sorted (short_label, weight) pairs.

    ``rows`` is any iterable of (short_label, weight) pairs — or of longer
    sequences whose first two elements are those, so a caller can pass
    DEFAULT_FRAMEWORK_ROWS or parsed framework tuples unchanged.

    THE HASH IS SEMANTIC, NOT BYTE-LEVEL, and that is load-bearing (§4.3).
    Descriptions, row order, and formatting MUST NOT trip it — otherwise Falke
    rewording a "What it captures" cell fires a fiduciary alarm, and an alarm
    that cries wolf is worse than no alarm. Categories and weights are the plan;
    everything else is prose.
    """
    items: List[Tuple[str, str]] = []
    for row in rows:
        short_label, weight = row[0], row[1]
        # %.6g keeps 25, 25.0 and 25.000001-from-a-float-cell the same string
        # without pretending to a precision the weights column does not carry.
        items.append((_slug(short_label), f"{float(weight):.6g}"))
    payload = "|".join(f"{label}={weight}" for label, weight in sorted(items))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_pack_version(value) -> Tuple[int, int]:
    """'1.0' -> (1, 0). Raises ValueError on anything else."""
    text = str(value if value is not None else "").strip()
    match = re.match(r"^(\d+)\.(\d+)", text)
    if not match:
        raise ValueError(f"Unparseable pack format version {value!r}.")
    return int(match.group(1)), int(match.group(2))


def pack_filename(project_name: str) -> str:
    """'<Project> - Scorecard Inputs.xlsx', with path-hostile characters
    replaced so the name survives every filesystem the operator uses."""
    safe = re.sub(r'[<>:"/\\|?*]', "-", str(project_name or "Project")).strip()
    safe = re.sub(r"\s+", " ", safe) or "Project"
    return f"{safe} - Scorecard Inputs.xlsx"
