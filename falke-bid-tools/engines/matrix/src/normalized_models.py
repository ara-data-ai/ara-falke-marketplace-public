"""
FALKE Matrix Pipeline — Normalization Layer Output Models
=========================================================
These Pydantic models represent the output contract of the normalization
rule engine.  They are designed to be consumed directly by the Excel writer
without further interpretation.

Pipeline position:
    BidDocument  →  [Normalization Rule Engine (normalize.py)]
    →  NormalizedBid (this schema)  →  [Excel Writer]

Every cell in the matrix is represented as a CellValue, which carries both
a CellState (semantic classification) and a display string (board-ready).
The Excel writer must use display, not raw amounts, for cell text rendering.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# Re-export enums from models that the Excel writer will need
from src.models import (
    CostStructure,
    ExtractionConfidence,
    FormType,
    GrandTotalConfidence,
    InputType,
)


# ---------------------------------------------------------------------------
# Cell-level semantics
# ---------------------------------------------------------------------------

class CellState(str, Enum):
    """
    Semantic state of a single matrix cell.  The Excel writer must render
    each state distinctly — never treat EXCLUDED as $0 or NULL_BLANK as zero.
    """
    AMOUNT = "AMOUNT"
    """Contractor priced this item — display the dollar amount."""

    EXPLICIT_ZERO = "EXPLICIT_ZERO"
    """Contractor explicitly entered $0 — item is in scope at no cost."""

    NULL_BLANK = "NULL_BLANK"
    """
    Cell was blank in the source document.  Potential scope gap.
    Displayed as '-'.  When field-median > $20K, SCOPE_GAP_IMPLICIT is
    added to flags.
    """

    EXCLUDED = "EXCLUDED"
    """
    Contractor explicitly excluded this item from scope.
    Display as 'EXCL'.  Requires a plug number before the total is used
    for cross-bid comparison.
    """

    BY_OWNER_OTHERS = "BY_OWNER_OTHERS"
    """
    Item is marked 'By Others' / 'By Owner'.  Display as 'BY OTHERS'.
    Must be excluded from the contractor's leveled construction total.
    """

    ALLOWANCE = "ALLOWANCE"
    """
    Item is an allowance (estimate, not firm price).  Display as 'ALLOW $X'.
    Included in division subtotal but NOT in the hard-cost leveled total.
    Flagged separately in the board summary.
    """

    NOT_COMPARABLE = "NOT_COMPARABLE"
    """
    Item classified 'Not Comparable' (Falke §2 R3 vocabulary; ENC-2). The
    amount is kept as submitted and stays in the bidder's OWN subtotal
    arithmetic, but is EXCLUDED from every cross-bid benchmark median
    (R7/A5) and receives no variance paint.
    """


class CellValue(BaseModel):
    """A single leveled matrix cell — the atom of the normalized output."""

    state: CellState

    amount: Optional[Decimal] = None
    """
    Set when state is AMOUNT, EXPLICIT_ZERO, or ALLOWANCE.
    None for NULL_BLANK, EXCLUDED, BY_OWNER_OTHERS.
    """

    display: str
    """
    Board-display string, ready for Excel rendering:
      AMOUNT          → '$120,000'
      EXPLICIT_ZERO   → '$0'
      NULL_BLANK      → '-'
      EXCLUDED        → 'EXCL'
      BY_OWNER_OTHERS → 'BY OTHERS'
      ALLOWANCE       → 'ALLOW $50,000'
    """

    is_reclassified: bool = False
    """True when this cell's division was PIPELINE_REMAPPED from a wrong division."""

    reclassified_from: Optional[str] = None
    """Original CSI division code if is_reclassified=True."""

    flags: list[str] = Field(default_factory=list)
    """
    Machine-readable flags on this cell.  Examples:
      'SCOPE_GAP_IMPLICIT'      — NULL_BLANK in a division where field-median > $20K
      'ARITHMETIC_DISCREPANCY'  — line items do not sum to stated subtotal
    """


# ---------------------------------------------------------------------------
# Division-level aggregation
# ---------------------------------------------------------------------------

class NormalizedDivision(BaseModel):
    """One CSI division's normalized bid data for a single contractor."""

    csi_code: str
    """Canonical Falke division code: DIV XX 00 00."""

    division_name: str

    line_item_cells: dict[str, CellValue] = Field(default_factory=dict)
    """
    Keyed by canonical sub-line label (from canon.CANONICAL_DIVISIONS).
    May also include contractor-native labels when no canonical mapping exists.
    """

    subtotal_cell: CellValue
    """Division-level rolled-up cell.  Displayed in the subtotal row of the matrix."""

    cost_structure: CostStructure
    """Pricing structure from the source DivisionBid."""


# ---------------------------------------------------------------------------
# Footer / bid-level aggregation
# ---------------------------------------------------------------------------

class NormalizedAlternate(BaseModel):
    """One add/deduct bid alternate, kept separate from the base comparison (M7)."""

    description: str
    amount: Optional[Decimal] = None
    display: str


class NormalizedFooter(BaseModel):
    """
    The fee, insurance, and total section of the normalized bid.
    Parallels BidFooter but with fully resolved CellValues and computed fields.
    """

    construction_subtotal: CellValue
    """Sum of all division subtotals (including BY_OWNER_OTHERS, excluding nothing)."""

    general_liability_insurance: CellValue
    builders_risk_insurance: CellValue
    gc_fee: CellValue
    overhead_and_profit: CellValue
    other_fees_subtotal: CellValue
    grand_total: CellValue
    bond: CellValue

    gc_fee_pct: Optional[Decimal] = None
    """
    Computed: gc_fee / construction_subtotal * 100.
    None when either value is missing or zero (Rule 5 Phase 1).
    """

    grand_total_confidence: GrandTotalConfidence
    confidence_flags: list[str] = Field(default_factory=list)

    leveled_total: Optional[Decimal] = None
    """
    Grand total minus the sum of BY_OWNER_OTHERS line-item amounts.
    Allowances are retained in this total — they are contractual.
    BY_OWNER_OTHERS items are excluded — they are not the contractor's direct cost.
    None when grand_total is not set.
    """

    alternates: list[NormalizedAlternate] = Field(default_factory=list)
    """
    Bid alternates (add/deduct options), surfaced for their OWN section in the
    matrix — never folded into the base/leveled total (M7, instrument-separation).
    """


# ---------------------------------------------------------------------------
# Grand-total composition — the SINGLE SOURCE OF TRUTH
# ---------------------------------------------------------------------------
#
# The grand total is composed of the construction subtotal plus the footer
# fee/insurance components a contractor states. Firms differ in WHICH rows they
# populate: some split insurance into GL + Builders Risk; others (e.g. a
# recurring firm) fold insurance into `other_fees_subtotal` and break out `overhead_and_profit`
# separately. Conversely, some firms repeat their fee total in
# `other_fees_subtotal` as a MEMO line that duplicates GL/BR/GC already counted —
# adding it would double-count.
#
# `grand_total_component_amounts()` resolves this once, for everyone. It composes
# STRUCTURE-FIRST from the fee rows — NOT by back-solving from the stated grand
# total (which can be wrong): `other_fees_subtotal` is treated as an additive
# insurance/fee line ONLY when it is not already explained by a roll-up of
# GL/BR/GC/O&P already counted AND the insurance rows are absent (genuinely
# distinct from the fees already counted). The stated grand total is then a
# CHECK, not the decider — when structure-based composition does NOT tie to it,
# audit.py raises FOOTER_DISCREPANCY (RED) rather than silently composing to the
# wrong number. audit.py (FOOTER_DISCREPANCY), write_matrix.py (the rendered
# footer rows), and reconcile.py (Stage 6b read-back) all consume THIS function
# so the three stay structurally identical.

# $1 tolerance, matching FOOTER_DISCREPANCY / Stage-6b tie-out.
_GRAND_TOTAL_TOLERANCE = Decimal("1")


def grand_total_component_amounts(footer: "NormalizedFooter") -> dict[str, Decimal]:
    """Return the ADDITIVE footer components that compose the grand total.

    Keys are stable footer identifiers; values are the numeric contribution of
    each component (0 when absent). The sum of the returned values equals the
    stated grand total within $1 for a well-formed bid. `other_fees_subtotal` is
    included only when it is a genuine additive line (insurance/fee not otherwise
    stated) rather than a memo duplicating GL/BR/GC already counted — decided
    STRUCTURE-FIRST from the fee rows themselves, never by back-solving from the
    contractor's stated grand total (which can itself be wrong). The stated total
    is a CHECK applied downstream by audit.py's FOOTER_DISCREPANCY, not the
    decider here.
    """
    def _amt(cell: CellValue) -> Decimal:
        return cell.amount if (cell.state == CellState.AMOUNT and cell.amount is not None) else Decimal("0")

    construction = _amt(footer.construction_subtotal)
    gl = _amt(footer.general_liability_insurance)
    br = _amt(footer.builders_risk_insurance)
    gc = _amt(footer.gc_fee)
    ohp = _amt(footer.overhead_and_profit)
    other_fees = _amt(footer.other_fees_subtotal)
    bond = _amt(footer.bond)

    base: dict[str, Decimal] = {
        "CONSTRUCTION_SUBTOTAL": construction,
        "GL_INSURANCE": gl,
        "BUILDERS_RISK": br,
        "GC_FEE": gc,
        "OVERHEAD_PROFIT": ohp,
        "BOND": bond,
    }

    # Decide whether other_fees_subtotal is additive, STRUCTURE-FIRST — never by
    # back-solving from the contractor's stated grand total. The stated total can
    # itself be wrong (dual-total columns, OCR garble); composing to match it
    # would silently mirror a wrong number with no flag. So we classify on the
    # structure of the fee rows, and let the stated total be a CHECK (audit.py's
    # FOOTER_DISCREPANCY), not the decider.
    #
    #   MEMO (exclude): other_fees reconciles (within $1) to a roll-up of fee
    #   rows ALREADY counted in `base` — i.e. it equals (GL+BR+GC), or GC alone,
    #   or (GL+BR+GC+O&P). That is a contractor "Total Fees/Markup" subtotal line;
    #   adding it would double-count.
    #
    #   ADDITIVE (include): other_fees is NOT explained by those already-counted
    #   rows AND the insurance rows it would represent are absent — the
    #   insurance-folded pattern, where GL and BR are both blank, so insurance
    #   can only live in other_fees.
    #
    # When neither rule fires (other_fees is unexplained but GL/BR are present),
    # we keep it OUT (conservative): it is most likely a memo we can't tie to a
    # specific roll-up, and the FOOTER_DISCREPANCY check will surface the gap
    # rather than us silently inflating the total.
    if other_fees > Decimal("0"):
        memo_rollups = (gl + br + gc, gc, gl + br + gc + ohp)
        is_memo = any(
            abs(other_fees - rollup) <= _GRAND_TOTAL_TOLERANCE
            for rollup in memo_rollups
        )
        insurance_absent = gl == Decimal("0") and br == Decimal("0")
        if not is_memo and insurance_absent:
            base["OTHER_FEES"] = other_fees

    return base


def grand_total_component_sum(footer: "NormalizedFooter") -> Decimal:
    """Sum of the additive grand-total components (see component-amounts above)."""
    return sum(grand_total_component_amounts(footer).values(), Decimal("0"))


# The full set of footer keys that CAN compose the grand total, in footer order.
# This is the single source of truth for "which rows roll up into the grand
# total" — write_matrix.py (rendered rows + Fees Subtotal) and reconcile.py
# (Stage 6b re-sum) BOTH derive their component sets from this constant so they
# can never drift from grand_total_component_amounts() above. OTHER_FEES is
# listed because its ROW always exists; whether it carries an additive amount for
# a given bid is decided per-bid by grand_total_component_amounts() (memo → 0).
# BOND is an additive component of the grand total (Marvin's ruling: bond-inside-
# the-grand-total is the common bonded-bid presentation), so it belongs here.
#
# Known limitation (bond "on top"): a bid that quotes bond OUTSIDE its stated
# grand total will produce a Stage-6b tie-out RED equal to the bond amount — a
# conservative, loud flag for human verification; leveling/ranking is unaffected
# because it keys on the contractor's stated grand_total. Bond add-back/decompose
# is a deliberately separate future enhancement.
GRAND_TOTAL_COMPONENT_KEYS: tuple[str, ...] = (
    "CONSTRUCTION_SUBTOTAL",
    "GL_INSURANCE",
    "BUILDERS_RISK",
    "GC_FEE",
    "OVERHEAD_PROFIT",
    "OTHER_FEES",
    "BOND",
)


# ---------------------------------------------------------------------------
# Summary flags
# ---------------------------------------------------------------------------

class BidSummaryFlag(BaseModel):
    """
    A board-memo-ready flag on a normalized bid.  Consumed by the board
    summary generator AND by audit_bids(), which promotes flags whose
    flag_type matches an AuditCode into first-class AuditItems on the AUDIT
    sheet (the C4 contract — remap/reclass/ambiguous flags must reach the
    board-facing sheet and feed the cell-coloring pass).
    """

    flag_type: str
    """
    Machine-readable type key.  Examples:
      'SCOPE_GAP_IMPLICIT', 'GC_FEE_OUTLIER', 'ALLOWANCE_PRESENT',
      'CODE_FORMAT_REMAPPED', 'KNOWN_FIRM_RECLASSIFIED',
      'UNRECOGNIZED_CODE_FORMAT', 'KNOWN_FIRM_AMBIGUOUS', 'CODE_SPLIT_UNMATCHED'
    """

    message: str
    """Human-readable, board-memo-ready description of the flag."""

    severity: str
    """'info' | 'warning' | 'critical'"""

    division_csi: Optional[str] = None
    """
    Canonical CSI code this flag attaches to (DIV XX 00 00), or None for a
    bid-level flag.  audit_bids() uses this to place the promoted AuditItem on
    the right division row and to drive _apply_audit_fills cell coloring (C4).
    """

    line_item_desc: Optional[str] = None
    """The specific line-item description involved, when the flag is line-scoped."""

    value: Optional[str] = None
    """The concrete value (e.g. native code, '15') surfaced on the AUDIT row."""


# ---------------------------------------------------------------------------
# Reclassification recommendation (Option C — annotate-only on the mirror)
# ---------------------------------------------------------------------------

class ReclassRecommendation(BaseModel):
    """One known-firm reclassification, recorded as a RECOMMENDATION (Option C §1.3).

    Detection-only artifact: it identifies a matched line item and the division
    Marvin recommends normalizing it to, WITHOUT moving the dollars on the
    faithful-mirror Bid_Form. It is the single source for the in-place marker
    (§2), the AUDIT reframe (§5), and the leveled-view move (§3).
    """

    line_item_desc: str
    """The matched line item's description (the cell key on the mirror)."""

    from_division: str
    """Canonical code the contractor filed it under (e.g. DIV 11 00 00)."""

    to_division: str
    """Canonical code Marvin recommends (e.g. DIV 01 00 00)."""

    to_division_name: str
    """Display name of the target (e.g. General Requirements)."""

    amount: Optional[Decimal] = None
    """The line amount (for the marker text); None if not separately priced."""

    rule_id: str
    """Provenance (e.g. EXAMPLE_DUMPSTER)."""


# ---------------------------------------------------------------------------
# Top-level normalized bid
# ---------------------------------------------------------------------------

class NormalizedBid(BaseModel):
    """
    Top-level normalization artifact: one contractor's complete bid after
    all rule-engine transformations.  The Excel writer consumes this directly.
    """

    contractor_name: str
    project_name: Optional[str] = None

    form_type: FormType
    bid_document_input_type: InputType
    extraction_confidence: ExtractionConfidence

    divisions: list[NormalizedDivision] = Field(default_factory=list)
    footer: NormalizedFooter

    qualifications_text: str
    """
    Concatenation of notes + qualifications + exclusions + assumptions + terms
    from BidQualifications.  Plain text, newline-separated sections.
    """

    # --- Allowance accounting ---
    total_allowance_value: Decimal = Decimal("0")
    """Sum of all line items where is_allowance=True."""

    allowance_count: int = 0
    """Number of allowance line items across all divisions."""

    # --- Scope gap accounting ---
    explicit_exclusion_count: int = 0
    """Number of line items where is_excluded=True."""

    implicit_gap_count: int = 0
    """
    Count of NULL_BLANK cells in divisions where field-median > $20K.
    Set after cross-bid median computation; 0 before that step.
    """

    # --- Pass-through and generated warnings ---
    extraction_warnings: list[str] = Field(default_factory=list)
    """Warnings generated by the extraction layer — passed through unchanged."""

    normalization_warnings: list[str] = Field(default_factory=list)
    """Warnings generated by the normalization rule engine."""

    summary_flags: list[BidSummaryFlag] = Field(default_factory=list)
    """Structured flags consumed by the board-memo generator."""

    reclass_recommendations: list[ReclassRecommendation] = Field(default_factory=list)
    """
    Known-firm reclassification RECOMMENDATIONS (Option C §1.3). On the
    faithful-mirror Bid_Form these are annotations only — the dollars stay
    as-submitted. The leveled-view builder (build_normalized_view) applies them.
    """

    @property
    def vacated_by_reclass(self) -> dict[str, str]:
        """Divisions this bid's reclass recommendations vacate, keyed
        ``from_division`` → ``to_division``.

        The SINGLE derivation for the phantom-gap suppressions (Floyd W2-4 —
        carried on the normalized view, never recomputed per consumer):
        audit.py skips SCOPE_GAP_IMPLICIT on these divisions (§6), and
        write_matrix.py's leveled R5 branch renders them blank with a
        reclass comment instead of a false "no pricing submitted" red
        (GOLD-DEV-8). First recommendation wins the TO label when a division
        feeds more than one target.
        """
        out: dict[str, str] = {}
        for rec in self.reclass_recommendations:
            out.setdefault(rec.from_division, rec.to_division)
        return out
