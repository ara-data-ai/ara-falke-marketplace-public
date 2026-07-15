"""
FALKE Matrix Pipeline — Extraction & Normalization Audit Engine
===============================================================
Validates normalized bid data and produces a flat list of AuditItems,
each representing a single check result at GREEN / YELLOW / RED status.

Pipeline position:
    list[NormalizedBid]  →  audit_bids()  →  list[AuditItem]
    list[AuditItem]      →  write_matrix() AUDIT sheet

All checks are defined in the brief; the order of items in the returned list
is deterministic but unsorted — the writer sorts by status then contractor.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel

from src import falke_rules
from src.canon import SCOPE_GAP_MEDIAN_THRESHOLD
from src.models import CostStructure, InputType
from src.normalized_models import (
    CellState,
    NormalizedBid,
    grand_total_component_sum,
)

# ---------------------------------------------------------------------------
# Canonical 20 divisions (same sequence as write_matrix.DIVISION_ROWS)
# ---------------------------------------------------------------------------

CANONICAL_20: list[str] = [
    "DIV 01 00 00",
    "DIV 02 00 00",
    "DIV 03 00 00",
    "DIV 04 00 00",
    "DIV 05 00 00",
    "DIV 06 00 00",
    "DIV 07 00 00",
    "DIV 08 00 00",
    "DIV 09 00 00",
    "DIV 10 00 00",
    "DIV 11 00 00",
    "DIV 12 00 00",
    "DIV 13 00 00",
    "DIV 21 00 00",
    "DIV 22 00 00",
    "DIV 23 00 00",
    "DIV 25 00 00",
    "DIV 26 00 00",
    "DIV 27 00 00",
    "DIV 28 00 00",
]


# ---------------------------------------------------------------------------
# AuditItem models
# ---------------------------------------------------------------------------

class AuditStatus(str, Enum):
    GREEN  = "GREEN"   # verified / passed
    YELLOW = "YELLOW"  # flagged for review
    RED    = "RED"     # rejected / critical — requires resolution before use


class AuditCode(str, Enum):
    # GREEN codes
    ARITHMETIC_VERIFIED     = "ARITHMETIC_VERIFIED"   # subtotal reconciles to line items
    SCOPE_COMPLETE          = "SCOPE_COMPLETE"         # all 20 divisions priced
    GC_FEE_NORMAL           = "GC_FEE_NORMAL"          # GC % within normal range

    # YELLOW codes
    SCOPE_GAP_IMPLICIT      = "SCOPE_GAP_IMPLICIT"    # division present, no price, no exclusion
    ALLOWANCE_PRESENT       = "ALLOWANCE_PRESENT"     # item priced as allowance (not firm)
    GC_FEE_OUTLIER          = "GC_FEE_OUTLIER"        # GC % > 2 std deviations from field mean
    GC_FEE_MISSING          = "GC_FEE_MISSING"        # GC fee not separately stated
    INSURANCE_NOT_STATED    = "INSURANCE_NOT_STATED"  # GL/BR baked into construction cost
    IMAGE_OCR_UNCERTAINTY   = "IMAGE_OCR_UNCERTAINTY" # value from image-scanned PDF — verify visually
    BY_OWNER_DEDUCTED       = "BY_OWNER_DEDUCTED"     # item excluded from leveled total
    CODE_FORMAT_REMAPPED    = "CODE_FORMAT_REMAPPED"   # legacy 2-digit code losslessly translated (§5)
    KNOWN_FIRM_RECLASSIFIED = "KNOWN_FIRM_RECLASSIFIED"  # known firm's habitual misfile corrected (§5)
    LUMP_SUM_DIVISION       = "LUMP_SUM_DIVISION"     # no sub-line detail available — lump sum only
    CROSS_BID_HIGH_VARIANCE = "CROSS_BID_HIGH_VARIANCE"  # division spread > 100% of field median

    # RED codes
    ARITHMETIC_DISCREPANCY  = "ARITHMETIC_DISCREPANCY"  # line item sum ≠ division subtotal by > $1
    FOOTER_DISCREPANCY      = "FOOTER_DISCREPANCY"      # construction subtotal + fees ≠ grand total by > $1
    MISSING_GRAND_TOTAL     = "MISSING_GRAND_TOTAL"     # grand total not extracted
    EXPLICIT_EXCLUSION      = "EXPLICIT_EXCLUSION"      # item explicitly excluded — plug needed
    UNRECOGNIZED_CODE_FORMAT = "UNRECOGNIZED_CODE_FORMAT"  # codes the engine can't confidently map (§5)
    KNOWN_FIRM_AMBIGUOUS    = "KNOWN_FIRM_AMBIGUOUS"   # name matched >1 known-firm profile (§5, C3)
    CODE_SPLIT_UNMATCHED    = "CODE_SPLIT_UNMATCHED"   # Mech/Elec line couldn't be split to a trade (§5)
    POST_WRITE_TIEOUT_FAILURE = "POST_WRITE_TIEOUT_FAILURE"  # written .xlsx cell ≠ blessed value (Stage 6b)
    INPUT_EXCLUDED          = "INPUT_EXCLUDED"          # input bid dropped (parse/validation/intake/normalize) — NOT in the matrix (F1, exit 4)
    SUBTOTAL_COMPOSITION_DISCREPANCY = "SUBTOTAL_COMPOSITION_DISCREPANCY"  # Σ displayed division subtotals ≠ stated CCS (ENC-3)
    NEGATIVE_UNCLASSIFIED   = "NEGATIVE_UNCLASSIFIED"   # net-negative division subtotal without credit classification (R22)


class AuditItem(BaseModel):
    contractor_name: str
    division_csi: Optional[str] = None   # e.g. "DIV 01 00 00" — None for bid-level checks
    line_item_desc: Optional[str] = None # specific line item description, or None for division-level
    status: AuditStatus
    code: AuditCode
    message: str                         # plain-English, board-memo readable
    value: Optional[str] = None          # the actual value involved (e.g. "$17,432 discrepancy")
    view: Literal["mirror", "leveled", "both"] = "both"
    """Which sheet this finding belongs to (Option C §4).

    Intra-bid signals are valid on both sheets (``both``). Cross-bid signals
    (SCOPE_GAP_IMPLICIT, CROSS_BID_HIGH_VARIANCE, GC_FEE_OUTLIER, GC_FEE_NORMAL)
    are only honest on the leveled buckets (``leveled``) and are suppressed on
    the mirror. Drives the AUDIT sheet's ``View`` column and the per-sheet
    cell-fill slice.
    """


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt(amount: Decimal) -> str:
    """Format a Decimal as a board-display dollar string: '$1,234,567'."""
    rounded = int(amount.quantize(Decimal("1")))
    return f"${rounded:,}"


def _fmt_pct(pct: Decimal) -> str:
    return f"{float(pct):.1f}%"


# ---------------------------------------------------------------------------
# C4 — promote normalizer summary_flags to first-class AuditItems
# ---------------------------------------------------------------------------

# Flags whose flag_type is one of these are promoted onto the AUDIT sheet so the
# board sees every remap / reclass / unrecognized-format / ambiguous / unmatched
# finding (Floyd C4). The severity carried on the flag maps to the audit status.
_PROMOTED_FLAG_CODES: dict[str, AuditCode] = {
    "CODE_FORMAT_REMAPPED":     AuditCode.CODE_FORMAT_REMAPPED,
    "KNOWN_FIRM_RECLASSIFIED":  AuditCode.KNOWN_FIRM_RECLASSIFIED,
    "UNRECOGNIZED_CODE_FORMAT": AuditCode.UNRECOGNIZED_CODE_FORMAT,
    "KNOWN_FIRM_AMBIGUOUS":     AuditCode.KNOWN_FIRM_AMBIGUOUS,
    "CODE_SPLIT_UNMATCHED":     AuditCode.CODE_SPLIT_UNMATCHED,
}

_SEVERITY_TO_STATUS: dict[str, AuditStatus] = {
    "critical": AuditStatus.RED,
    "warning":  AuditStatus.YELLOW,
    "info":     AuditStatus.GREEN,
}


def _promote_summary_flags(bid: NormalizedBid) -> list[AuditItem]:
    """Translate a bid's promoted summary_flags into AuditItems (C4).

    These reach the AUDIT sheet AND feed _apply_audit_fills cell coloring,
    closing the gap where audit_bids never read summary_flags.
    """
    out: list[AuditItem] = []
    for flag in bid.summary_flags:
        code = _PROMOTED_FLAG_CODES.get(flag.flag_type)
        if code is None:
            continue
        out.append(AuditItem(
            contractor_name=bid.contractor_name,
            division_csi=flag.division_csi,
            line_item_desc=flag.line_item_desc,
            status=_SEVERITY_TO_STATUS.get(flag.severity, AuditStatus.YELLOW),
            code=code,
            message=flag.message,
            value=flag.value,
        ))
    return out


# ---------------------------------------------------------------------------
# Field-median computation
# ---------------------------------------------------------------------------

def _compute_division_medians(bids: list[NormalizedBid]) -> dict[str, Decimal]:
    """
    Compute per-division field medians across all bids using the ONE M-2
    median-membership rule (falke_rules.median_membership): a subtotal enters
    iff div_status kind == "priced" AND amount > 0 AND not R20-failed. This
    is the SAME set the leveled benchmark block computes over, so the
    register's scope-gap value strings cite the number the board sees
    (closes GOLD-DEV-1 and GOLD-DEV-4 — NC-composed and R20-failed subtotals
    are fenced here exactly as on the sheet).
    """
    amounts_by_div: dict[str, list[Decimal]] = {code: [] for code in CANONICAL_20}

    for bid in bids:
        for code in CANONICAL_20:
            member = falke_rules.median_membership(bid, code)
            if member is not None:
                amounts_by_div[code].append(Decimal(str(member)))

    medians: dict[str, Decimal] = {}
    for code, vals in amounts_by_div.items():
        if vals:
            sorted_vals = sorted(vals)
            n = len(sorted_vals)
            if n % 2 == 1:
                medians[code] = sorted_vals[n // 2]
            else:
                medians[code] = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
        else:
            medians[code] = Decimal("0")

    return medians


# ---------------------------------------------------------------------------
# audit_bids — main entry point
# ---------------------------------------------------------------------------

def audit_bids(bids: list[NormalizedBid]) -> list[AuditItem]:
    """
    Run all audit checks for every bid and return a flat list of AuditItems.

    Under Option C the input ``bids`` are the LEVELED (fully-normalized) views, so
    cross-bid statistics (SCOPE_GAP_IMPLICIT, CROSS_BID_HIGH_VARIANCE,
    GC_FEE_OUTLIER) compute on the normalized buckets (§4). Those cross-bid items
    are tagged ``view="leveled"`` and suppressed on the mirror. Most intra-bid
    items are ``view="both"``, EXCEPT the division-subtotal-bearing intra-bid codes
    (ARITHMETIC_VERIFIED, ARITHMETIC_DISCREPANCY, LUMP_SUM_DIVISION) on a division a
    reclass TOUCHED for that bidder (in any ``from_division``/``to_division``):
    those carry the leveled subtotal, which differs from the as-submitted Bid_Form
    cell, so they are re-tagged ``view="leveled"`` to keep the mirror honest
    (Floyd/Marvin §8 — re-tag, don't duplicate). A division a reclass vacated for a
    given bidder is skipped for SCOPE_GAP_IMPLICIT (the phantom-gap fix, §6).

    Checks performed:
      Per-bid, per-division:
        1. ARITHMETIC_DISCREPANCY / ARITHMETIC_VERIFIED
        2. SCOPE_GAP_IMPLICIT
        3. EXPLICIT_EXCLUSION
        4. ALLOWANCE_PRESENT
        5. BY_OWNER_DEDUCTED
        6. LUMP_SUM_DIVISION
        7. (code-format remap / known-firm reclass / unrecognized-format /
            ambiguous-firm / split-unmatched) — promoted from the normalizer's
            summary_flags via _promote_summary_flags (C4), not inferred here
        8. IMAGE_OCR_UNCERTAINTY
        9. CROSS_BID_HIGH_VARIANCE  (cross-bid, emitted per contractor per division)

      Per-bid footer:
        10. FOOTER_DISCREPANCY / ARITHMETIC_VERIFIED (grand total)
        11. MISSING_GRAND_TOTAL
        12. GC_FEE_MISSING / GC_FEE_OUTLIER / GC_FEE_NORMAL
        13. INSURANCE_NOT_STATED

      Bid-level summary:
        14. SCOPE_COMPLETE
    """
    items: list[AuditItem] = []

    # N3 (Marvin §6): cross-bid statistics are only meaningful with ≥2 bidders;
    # the stddev GC-fee outlier needs ≥3 (degenerate at n=2). A single bid is not
    # a comparison — emit NO cross-bid flags (scope gap, variance, GC outlier).
    n_bids = len(bids)
    cross_bid_enabled = n_bids >= 2

    # Pre-compute cross-bid medians (needed for checks 2 and 9)
    division_medians = _compute_division_medians(bids)

    # Pre-compute cross-bid division subtotals for variance check (check 9).
    # Same M-2 membership as the medians above — the spread and the median it
    # compares against are computed over ONE set (never two dialects).
    division_amounts_by_code: dict[str, list[tuple[str, Decimal]]] = {
        code: [] for code in CANONICAL_20
    }
    for bid in bids:
        for code in CANONICAL_20:
            member = falke_rules.median_membership(bid, code)
            if member is not None:
                division_amounts_by_code[code].append(
                    (bid.contractor_name, Decimal(str(member)))
                )

    # Pre-compute high-variance divisions (check 9)
    high_variance_divs: set[str] = set()
    for code, entries in division_amounts_by_code.items():
        if len(entries) < 2:
            continue
        amounts = [e[1] for e in entries]
        spread = max(amounts) - min(amounts)
        median = division_medians.get(code, Decimal("0"))
        if median > Decimal("0") and spread > median:
            high_variance_divs.add(code)

    # Pre-compute GC fee field mean and stddev (check 12)
    gc_pct_values: list[float] = []
    for bid in bids:
        pct = bid.footer.gc_fee_pct
        if pct is not None:
            gc_pct_values.append(float(pct))

    gc_field_mean: Optional[float] = None
    gc_field_stddev: Optional[float] = None
    # Stddev GC-fee outlier only at n >= 3 (degenerate at n=2; absent at n=1) — §6.
    if n_bids >= 3 and len(gc_pct_values) >= 2:
        gc_field_mean = statistics.mean(gc_pct_values)
        gc_field_stddev = statistics.stdev(gc_pct_values)

    # -----------------------------------------------------------------------
    # Per-bid checks
    # -----------------------------------------------------------------------
    for bid in bids:
        name = bid.contractor_name
        is_image_scan = bid.bid_document_input_type == InputType.IMAGE_SCAN

        # C4: promote the normalizer's remap/reclass/unrecognized/ambiguous/
        # unmatched flags onto the AUDIT sheet (they were never surfaced before).
        items.extend(_promote_summary_flags(bid))

        # Phantom-gap fix (§6): divisions this bidder's reclass emptied must not
        # raise SCOPE_GAP_IMPLICIT — keyed to the `from_division` per bidder.
        # Single derivation carried on the normalized view (Floyd W2-4);
        # write_matrix's leveled R5 suppression consumes the same property.
        vacated_by_reclass = bid.vacated_by_reclass

        # Mirror-honesty fix (Floyd/Marvin §8): a division-subtotal-bearing
        # intra-bid code (ARITHMETIC_VERIFIED/DISCREPANCY, LUMP_SUM_DIVISION) on a
        # division a reclass TOUCHED for THIS bidder carries the LEVELED subtotal,
        # which differs from the as-submitted Bid_Form cell. Tagging it "both"
        # would surface a leveled value on the mirror — a visible contradiction on
        # the exact cross-reference a board performs. Re-tag those rows "leveled"
        # (don't duplicate — that would trip Stage 6b row-count parity). Untouched
        # divisions stay "both": their value is identical on both sheets.
        reclass_touched_divisions: set[str] = set()
        for rec in bid.reclass_recommendations:
            reclass_touched_divisions.add(rec.from_division)
            reclass_touched_divisions.add(rec.to_division)

        def _intra_bid_view(csi: str) -> Literal["both", "leveled"]:
            return "leveled" if csi in reclass_touched_divisions else "both"

        # Build a lookup: csi_code → NormalizedDivision for this bid
        div_by_code: dict[str, "NormalizedDivision"] = {}
        for div in bid.divisions:
            # Last one wins if duplicates (shouldn't happen post-normalization)
            div_by_code[div.csi_code] = div

        # Track which of the canonical 20 are fully priced (for check 14)
        priced_divisions: set[str] = set()

        # -------------------------------------------------------------------
        # Check all 20 canonical divisions
        # -------------------------------------------------------------------
        for csi_code in CANONICAL_20:
            div = div_by_code.get(csi_code)
            median_val = division_medians.get(csi_code, Decimal("0"))
            median_str = _fmt(median_val) if median_val else "$0"

            if div is None:
                # Division entirely absent from bid — treat as scope gap if median is meaningful.
                # Cross-bid signal: only with ≥2 bidders (no field to compare at n=1, §6).
                # §6: a division fully drained by THIS bidder's reclass is not a gap.
                # M-1 (W-D): the AUDIT scope-gap row is an ARA materiality
                # diagnostic — thresholded at field median > $20,000 (ONE
                # constant, canon.SCOPE_GAP_MEDIAN_THRESHOLD). The R5 red on
                # the leveled sheet stays threshold-free (Falke program rule).
                if (
                    cross_bid_enabled
                    and median_val > SCOPE_GAP_MEDIAN_THRESHOLD
                    and csi_code not in vacated_by_reclass
                ):
                    items.append(AuditItem(
                        contractor_name=name,
                        division_csi=csi_code,
                        status=AuditStatus.YELLOW,
                        code=AuditCode.SCOPE_GAP_IMPLICIT,
                        view="leveled",
                        message=(
                            f"Division not present in bid — potential scope gap. "
                            f"Field median: {median_str}."
                        ),
                        value=median_str,
                    ))
                continue

            subtotal_state = div.subtotal_cell.state
            subtotal_amount = div.subtotal_cell.amount

            # ENC-6 (W-D): exclusion / by-owner instrumentation keys on the
            # SAME state classification the leveled sheet renders
            # (falke_rules.div_status), not on a subtotal CellState normalize
            # never produces (the old dead EXCLUDED branch — GOLD-DEV-3).
            kind, _kind_amt = falke_rules.div_status(bid, csi_code)

            # GOLD-DEV-7 rule (3): a present-but-blank division whose line
            # states are ALL classified (BY_OWNER / EXCLUDED / NC) raises NO
            # scope-gap row — the classification rows carry the story.
            # (Ordering constraint satisfied: rule 3 lands WITH the ENC-6
            # rows below, in this one change.)
            lines_all_classified = bool(div.line_item_cells) and all(
                c.state in (CellState.BY_OWNER_OTHERS, CellState.EXCLUDED,
                            CellState.NOT_COMPARABLE)
                for c in div.line_item_cells.values()
            )

            # Check 2: SCOPE_GAP_IMPLICIT — cross-bid signal (needs ≥2 bidders, §6).
            # §6 phantom-gap fix: skip a division this bidder's reclass vacated.
            # GOLD-DEV-7 noise fix rule (2) (Marvin ruling 2026-07-15): the
            # present-but-blank branch carries the SAME median guard as the
            # absent branch above — now the M-1 materiality threshold
            # (> $20,000, ONE constant in canon.py; the R5 red on the leveled
            # sheet stays threshold-free per the Falke program).
            if (
                cross_bid_enabled
                and subtotal_state == CellState.NULL_BLANK
                and median_val > SCOPE_GAP_MEDIAN_THRESHOLD
                and csi_code not in vacated_by_reclass
                and not lines_all_classified
            ):
                items.append(AuditItem(
                    contractor_name=name,
                    division_csi=csi_code,
                    status=AuditStatus.YELLOW,
                    code=AuditCode.SCOPE_GAP_IMPLICIT,
                    view="leveled",
                    message=(
                        f"Division present but no price entered and no explicit exclusion. "
                        f"Field median: {median_str}."
                    ),
                    value=median_str,
                ))

            # ENC-6 (1): fully-excluded division → ONE division-level RED row
            # (the scope-gap add-back discipline reaching the register).
            # Dedup: division-level row only, NO per-line rows (below).
            if kind == "excluded":
                if median_val > Decimal("0"):
                    impact = (
                        f"field median {median_str}; a plug/add-back of this "
                        f"magnitude is needed before this bidder's total is "
                        f"compared as complete scope."
                    )
                else:
                    impact = ("no field benchmark available — price the "
                              "exclusion before comparison.")
                items.append(AuditItem(
                    contractor_name=name,
                    division_csi=csi_code,
                    status=AuditStatus.RED,
                    code=AuditCode.EXPLICIT_EXCLUSION,
                    view="both",
                    message=(
                        f"Division explicitly excluded by the bidder — shown "
                        f"red 'Excluded' on the leveled sheet (R28: red until "
                        f"user-approved). Estimated cost impact: {impact}"
                    ),
                    value="Excluded",
                ))

            # ENC-6 (3): fully by-owner division → ONE division-level YELLOW
            # row carrying the VERBATIM token; it REPLACES the per-line rows
            # for this division (dedup below).
            elif kind == "by_owner":
                token = falke_rules.by_owner_token(bid, csi_code)
                items.append(AuditItem(
                    contractor_name=name,
                    division_csi=csi_code,
                    status=AuditStatus.YELLOW,
                    code=AuditCode.BY_OWNER_DEDUCTED,
                    view="both",
                    message=(
                        f"Division classified '{token}' by the bidder "
                        f"(approved classification, R3/R8) — excluded from "
                        f"benchmarks. Verify the owner/other party actually "
                        f"carries this scope."
                    ),
                    value=token,
                ))

            # Division is priced
            if subtotal_state == CellState.AMOUNT and subtotal_amount:
                priced_divisions.add(csi_code)

            # W-D ruling 5.3: a net-negative division subtotal is preserved
            # and rendered (accounting-negative) but is an ERROR until
            # classified a deductive alternate / approved credit (R22 —
            # no classification pathway exists yet, Q10). Fenced from every
            # median by M-2 membership; the bidder's own arithmetic keeps it.
            if (
                subtotal_state == CellState.AMOUNT
                and subtotal_amount is not None
                and subtotal_amount < Decimal("0")
            ):
                items.append(AuditItem(
                    contractor_name=name,
                    division_csi=csi_code,
                    status=AuditStatus.RED,
                    code=AuditCode.NEGATIVE_UNCLASSIFIED,
                    view="both",
                    message=(
                        f"Net negative division subtotal "
                        f"({_fmt(subtotal_amount)}) — negative values are "
                        f"errors unless marked as a deductive alternate or "
                        f"approved credit (R22). Verify the credit's "
                        f"classification before leveling."
                    ),
                    value=_fmt(subtotal_amount),
                ))

            # Check 1: ARITHMETIC_DISCREPANCY / ARITHMETIC_VERIFIED
            # Only applicable to ITEMIZED / PARTIAL_ITEMIZED divisions
            if div.cost_structure != CostStructure.LUMP_SUM and div.line_item_cells:
                computed_sum = Decimal("0")
                for cell in div.line_item_cells.values():
                    if cell.state in (CellState.AMOUNT, CellState.EXPLICIT_ZERO, CellState.ALLOWANCE):
                        computed_sum += cell.amount or Decimal("0")

                if subtotal_amount is not None:
                    delta = abs(computed_sum - subtotal_amount)
                    if delta > Decimal("1"):
                        items.append(AuditItem(
                            contractor_name=name,
                            division_csi=csi_code,
                            status=AuditStatus.RED,
                            code=AuditCode.ARITHMETIC_DISCREPANCY,
                            view=_intra_bid_view(csi_code),
                            message=(
                                f"Line item sum ({_fmt(computed_sum)}) differs from "
                                f"stated subtotal ({_fmt(subtotal_amount)}) by {_fmt(delta)}."
                            ),
                            value=_fmt(delta),
                        ))
                    else:
                        items.append(AuditItem(
                            contractor_name=name,
                            division_csi=csi_code,
                            status=AuditStatus.GREEN,
                            code=AuditCode.ARITHMETIC_VERIFIED,
                            view=_intra_bid_view(csi_code),
                            message=(
                                f"Line items reconcile to stated subtotal "
                                f"({_fmt(subtotal_amount)}) within $1."
                            ),
                            value=_fmt(subtotal_amount),
                        ))

            # Check 6: LUMP_SUM_DIVISION
            # GOLD-DEV-7 noise fix rule (1) (Marvin ruling 2026-07-15, harness
            # D1): fires only when the division actually carries a subtotal
            # amount (a stated $0 counts — it is a submitted figure) OR line
            # items. A zero-item, no-subtotal shell (an empty split secondary,
            # a reclass-vacated division) has no "single total" to verify —
            # it emits nothing.
            if div.cost_structure == CostStructure.LUMP_SUM and (
                subtotal_amount is not None or div.line_item_cells
            ):
                items.append(AuditItem(
                    contractor_name=name,
                    division_csi=csi_code,
                    status=AuditStatus.YELLOW,
                    code=AuditCode.LUMP_SUM_DIVISION,
                    view=_intra_bid_view(csi_code),
                    message=(
                        "No line-item detail available — priced as a single total. "
                        "Verify scope inclusion during bid clarification."
                    ),
                    value=_fmt(subtotal_amount) if subtotal_amount else None,
                ))

            # Check 7 (CODE_FORMAT_REMAPPED / KNOWN_FIRM_RECLASSIFIED) is now
            # emitted from the normalizer's summary_flags via _promote_summary_flags
            # (C4) — it carries the precise native→canonical / from→to semantics,
            # so the old inference-from-is_reclassified check is removed.

            # Check 8: IMAGE_OCR_UNCERTAINTY
            if is_image_scan and subtotal_amount and subtotal_amount > Decimal("0"):
                items.append(AuditItem(
                    contractor_name=name,
                    division_csi=csi_code,
                    status=AuditStatus.YELLOW,
                    code=AuditCode.IMAGE_OCR_UNCERTAINTY,
                    view=_intra_bid_view(csi_code),
                    message=(
                        "Value extracted via OCR from a scanned document — "
                        "verify against original PDF."
                    ),
                    value=_fmt(subtotal_amount),
                ))

            # Check 9: CROSS_BID_HIGH_VARIANCE
            if csi_code in high_variance_divs:
                amounts_for_div = division_amounts_by_code.get(csi_code, [])
                if amounts_for_div:
                    all_amounts = [e[1] for e in amounts_for_div]
                    spread = max(all_amounts) - min(all_amounts)
                    med = division_medians.get(csi_code, Decimal("0"))
                    items.append(AuditItem(
                        contractor_name=name,
                        division_csi=csi_code,
                        status=AuditStatus.YELLOW,
                        code=AuditCode.CROSS_BID_HIGH_VARIANCE,
                        view="leveled",
                        message=(
                            f"High price variance across bidders (spread {_fmt(spread)}, "
                            f"median {_fmt(med)}) — scope interpretation likely differs. "
                            f"Review before award."
                        ),
                        value=_fmt(spread),
                    ))

            # -------------------------------------------------------------------
            # Per-line-item checks within this division
            # -------------------------------------------------------------------
            for desc, cell in div.line_item_cells.items():
                # Check 4: ALLOWANCE_PRESENT
                if cell.state == CellState.ALLOWANCE:
                    # Try to get allowance_basis — not stored in CellValue; use display
                    items.append(AuditItem(
                        contractor_name=name,
                        division_csi=csi_code,
                        line_item_desc=desc,
                        status=AuditStatus.YELLOW,
                        code=AuditCode.ALLOWANCE_PRESENT,
                        message=(
                            f"Item priced as an allowance — final cost may vary. "
                            f"Allowance amount: {cell.display}."
                        ),
                        value=cell.display,
                    ))

                # Check 5: BY_OWNER_DEDUCTED — per-line rows remain ONLY for
                # by-owner lines inside otherwise-priced divisions; a fully
                # by-owner division's division-level row REPLACES them
                # (ENC-6 rule 3 dedup).
                if cell.state == CellState.BY_OWNER_OTHERS and kind != "by_owner":
                    items.append(AuditItem(
                        contractor_name=name,
                        division_csi=csi_code,
                        line_item_desc=desc,
                        status=AuditStatus.YELLOW,
                        code=AuditCode.BY_OWNER_DEDUCTED,
                        message=(
                            "Deducted from leveled total — amount is owner/others scope."
                        ),
                        value="BY OTHERS",
                    ))

                # ENC-6 (2): EXCLUDED line inside an otherwise-priced division
                # → one RED row per excluded line. Dedup: a fully-excluded
                # division emits ONE division-level row instead (above).
                if cell.state == CellState.EXCLUDED and kind != "excluded":
                    items.append(AuditItem(
                        contractor_name=name,
                        division_csi=csi_code,
                        line_item_desc=desc,
                        status=AuditStatus.RED,
                        code=AuditCode.EXPLICIT_EXCLUSION,
                        view="both",
                        message=(
                            "Line item explicitly excluded within an "
                            "otherwise-priced division — confirm the division "
                            "subtotal excludes it and price the gap before "
                            "comparison."
                        ),
                        value="Excluded",
                    ))

        # -------------------------------------------------------------------
        # Per-bid footer checks
        # -------------------------------------------------------------------
        footer = bid.footer

        # ENC-3 (S2-1, W-D): CCS composition check — Σ displayed division
        # subtotals vs the bidder's STATED Construction Cost Subtotal at the
        # Falke tolerance max($5, 0.5%). The writer paints the same failure
        # RED on the leveled CCS cell ([FALKE R21] comment); this row is the
        # register anchor the REM-2 on-cell disclosure points at.
        cs_cell = footer.construction_subtotal
        stated_ccs = (
            float(cs_cell.amount)
            if (cs_cell.state == CellState.AMOUNT and cs_cell.amount is not None)
            else None
        )
        comp_fail = falke_rules.composition_check(bid, CANONICAL_20, stated_ccs)
        if comp_fail is not None:
            displayed_sum, comp_delta = comp_fail
            delta_dec = Decimal(str(comp_delta))
            items.append(AuditItem(
                contractor_name=name,
                status=AuditStatus.RED,
                code=AuditCode.SUBTOTAL_COMPOSITION_DISCREPANCY,
                view="leveled",
                message=(
                    f"Stated Construction Cost Subtotal "
                    f"({_fmt(Decimal(str(stated_ccs)))}) does not compose from "
                    f"the displayed division subtotals "
                    f"(sum {_fmt(Decimal(str(displayed_sum)))}) — delta "
                    f"{_fmt(delta_dec)} exceeds max($5, 0.5%) (ENC-3, "
                    f"R21-class). Dollars may be carried outside the division "
                    f"grid — verify where before comparing this bidder's "
                    f"totals."
                ),
                value=_fmt(delta_dec),
            ))

        # Check 11: MISSING_GRAND_TOTAL
        if footer.grand_total.amount is None:
            items.append(AuditItem(
                contractor_name=name,
                status=AuditStatus.RED,
                code=AuditCode.MISSING_GRAND_TOTAL,
                message="Grand total was not extracted from this bid — cannot perform cost comparison.",
                value=None,
            ))
        else:
            # Check 10: FOOTER_DISCREPANCY / ARITHMETIC_VERIFIED
            # The grand total is composed of the construction subtotal plus the
            # additive footer fee/insurance components the contractor stated.
            # grand_total_component_sum() is the SINGLE SOURCE OF TRUTH for that
            # composition (it handles firms that fold insurance/OH&P into
            # overhead_and_profit / other_fees_subtotal — e.g. a recurring firm —
            # rather than splitting GL/BR, and ignores a memo other_fees line that duplicates
            # fees already counted). reconcile.py Stage 6b and write_matrix.py's
            # rendered footer consume the same helper so all three stay aligned.
            grand_total = footer.grand_total.amount or Decimal("0")
            computed_total = grand_total_component_sum(footer)
            delta = abs(computed_total - grand_total)

            if delta > Decimal("1"):
                items.append(AuditItem(
                    contractor_name=name,
                    status=AuditStatus.RED,
                    code=AuditCode.FOOTER_DISCREPANCY,
                    message=(
                        f"Construction subtotal + fees ({_fmt(computed_total)}) does not "
                        f"reconcile to grand total ({_fmt(grand_total)}) — "
                        f"discrepancy: {_fmt(delta)}."
                    ),
                    value=_fmt(delta),
                ))
            else:
                items.append(AuditItem(
                    contractor_name=name,
                    status=AuditStatus.GREEN,
                    code=AuditCode.ARITHMETIC_VERIFIED,
                    message=(
                        f"Footer reconciles: construction subtotal + fees = grand total "
                        f"({_fmt(grand_total)}) within $1."
                    ),
                    value=_fmt(grand_total),
                ))

        # Check 12: GC_FEE_MISSING / GC_FEE_OUTLIER / GC_FEE_NORMAL
        pct = footer.gc_fee_pct
        if pct is None:
            items.append(AuditItem(
                contractor_name=name,
                status=AuditStatus.YELLOW,
                code=AuditCode.GC_FEE_MISSING,
                message=(
                    "GC fee not separately stated — may be baked into construction cost. "
                    "Verify before comparing bids."
                ),
                value=None,
            ))
        elif (
            gc_field_mean is not None
            and gc_field_stddev is not None
            and gc_field_stddev > 0
        ):
            pct_float = float(pct)
            deviation = abs(pct_float - gc_field_mean) / gc_field_stddev
            if deviation > 2.0:
                items.append(AuditItem(
                    contractor_name=name,
                    status=AuditStatus.YELLOW,
                    code=AuditCode.GC_FEE_OUTLIER,
                    view="leveled",
                    message=(
                        f"GC fee of {_fmt_pct(pct)} is more than 2 std deviations from "
                        f"field mean ({gc_field_mean:.1f}%) — verify scope and overhead treatment."
                    ),
                    value=_fmt_pct(pct),
                ))
            else:
                items.append(AuditItem(
                    contractor_name=name,
                    status=AuditStatus.GREEN,
                    code=AuditCode.GC_FEE_NORMAL,
                    view="leveled",
                    message=(
                        f"GC fee of {_fmt_pct(pct)} is within normal range "
                        f"(field mean {gc_field_mean:.1f}%)."
                    ),
                    value=_fmt_pct(pct),
                ))
        else:
            # Only one bid has a GC fee pct — can't compute stddev, treat as normal
            items.append(AuditItem(
                contractor_name=name,
                status=AuditStatus.GREEN,
                code=AuditCode.GC_FEE_NORMAL,
                view="leveled",
                message=f"GC fee of {_fmt_pct(pct)} (only one comparable bid; field std dev unavailable).",
                value=_fmt_pct(pct),
            ))

        # Check 13: INSURANCE_NOT_STATED
        gl_is_zero = (
            footer.general_liability_insurance.amount is None
            or footer.general_liability_insurance.amount == Decimal("0")
        )
        br_is_zero = (
            footer.builders_risk_insurance.amount is None
            or footer.builders_risk_insurance.amount == Decimal("0")
        )
        if gl_is_zero and br_is_zero:
            items.append(AuditItem(
                contractor_name=name,
                status=AuditStatus.YELLOW,
                code=AuditCode.INSURANCE_NOT_STATED,
                message=(
                    "Neither GL nor Builders Risk insurance is separately stated — "
                    "likely baked into construction cost. Verify before cost comparison."
                ),
                value=None,
            ))

        # Check 14: SCOPE_COMPLETE (bid-level summary)
        # A division is "priced" if it has a non-zero AMOUNT subtotal.
        # Divisions with NULL_BLANK or EXCLUDED subtotals are NOT complete.
        all_priced = True
        for csi_code in CANONICAL_20:
            div = div_by_code.get(csi_code)
            if div is None:
                all_priced = False
                break
            if div.subtotal_cell.state not in (CellState.AMOUNT, CellState.EXPLICIT_ZERO):
                all_priced = False
                break

        if all_priced:
            items.append(AuditItem(
                contractor_name=name,
                status=AuditStatus.GREEN,
                code=AuditCode.SCOPE_COMPLETE,
                message="All 20 canonical divisions are priced — no scope gaps detected.",
                value=None,
            ))

    return items
