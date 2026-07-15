"""
FALKE Matrix Pipeline — Normalization Rule Engine
==================================================
Applies bid-leveling domain logic to a raw BidDocument and produces a
NormalizedBid that the Excel writer can consume without further interpretation.

Pipeline position:
    BidDocument  →  normalize_bid()  →  NormalizedBid
    list[NormalizedBid]  →  compute_cross_bid_stats()  →  list[NormalizedBid]

The 6 priority rules:
  Rule 1  — Cell-state semantics: $0 vs NULL vs EXCL vs BY-OTHERS
  Rule 2  — Code-format remapping (csi_1995_2digit, detected by code signature)
  Rule 3  — Known-firm reclassifications (config-driven, opt-in per matched firm)
  Rule 4  — Allowance treatment
  Rule 5  — GC Fee % normalization (Phase 1 per-bid; Phase 2 cross-bid)
  Rule 6  — Image-scan confidence validation

Each rule is implemented as a small, named helper.  normalize_bid() is the
pure-function composition of all per-bid rules.  compute_cross_bid_stats()
handles Rule 5 Phase 2 and implicit-gap counting post-cross-bid.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Optional

from src.canon import (
    CANONICAL_DIVISIONS,
    SCOPE_GAP_MEDIAN_THRESHOLD,
    UNMAPPED_DIVISION,
    GC_FEE_OUTLIER_STDDEV,
    compute_field_medians,
    detect_csi_1995_2digit,
    get_canonical_division,
    resolve_legacy_code,
    route_split_subline,
)
from src.firm_config import Firm, KnownFirmsConfig, Reclassification, load_known_firms
from src.models import (
    BidDocument,
    ClassificationSource,
    CostStructure,
    DivisionBid,
    ExtractionConfidence,
    FormType,
    GrandTotalConfidence,
    InputType,
    LineItem,
)
from src.normalized_models import (
    BidSummaryFlag,
    CellState,
    CellValue,
    NormalizedBid,
    NormalizedDivision,
    NormalizedFooter,
    ReclassRecommendation,
)


# ---------------------------------------------------------------------------
# Rule 1 — Cell-state resolution
# ---------------------------------------------------------------------------

def _resolve_cell_state(item: LineItem) -> tuple[CellState, Optional[Decimal], str]:
    """
    Resolve the CellState, amount, and display string for a single LineItem.

    Returns:
        (state, amount, display)

    Priority order (highest wins):
      1. BY_OWNER_OTHERS
      2. EXCLUDED
      3. NOT_COMPARABLE
      4. ALLOWANCE
      5. EXPLICIT_ZERO
      6. NULL_BLANK (amount is None, not explicit zero)
      7. AMOUNT (amount is set, positive)
    """
    if item.is_by_owner_others:
        # ENC-1 (Marvin v0.3.0 diff): carry the bidder's verbatim token
        # ("Not Applicable", "By Owner", "NIC — By Others", …) as the display
        # so the leveled sheet never tells the board a wrong story.
        verbatim = (item.by_others_verbatim or "").strip()
        return CellState.BY_OWNER_OTHERS, None, verbatim or "BY OTHERS"

    if item.is_excluded:
        return CellState.EXCLUDED, None, "EXCL"

    if item.is_not_comparable:
        # ENC-2: amount KEPT (never silently altered, R33) but fenced out of
        # every benchmark (R7/A5) via the dedicated state.
        if item.amount is not None:
            return (CellState.NOT_COMPARABLE, item.amount,
                    f"{_fmt(item.amount)} (Not Comparable)")
        return CellState.NOT_COMPARABLE, None, "Not Comparable"

    if item.is_allowance:
        amt = item.amount or Decimal("0")
        display = f"ALLOW {_fmt(amt)}" if amt else "ALLOW"
        return CellState.ALLOWANCE, amt, display

    if item.is_explicit_zero:
        return CellState.EXPLICIT_ZERO, Decimal("0"), "$0"

    if item.amount is None:
        return CellState.NULL_BLANK, None, "-"

    return CellState.AMOUNT, item.amount, _fmt(item.amount)


# The state→amount inclusion table for a division's OWN arithmetic: which line
# CellStates contribute a real amount to a division subtotal sum. This is the
# SINGLE SHARED TABLE for both consumers — _resolve_subtotal_cell (Rule 1) and
# the reclass touched-division re-derivation in _apply_reclass_moves (§3) — so
# the two sites can never drift (Marvin GOLD-DEV-10 ruling (2); contract-tested
# in tests/test_normalize.py TestSubtotalStateContract). NOT_COMPARABLE is IN:
# the bidder's own arithmetic keeps an NC amount — only cross-bid benchmarks
# fence it out (ENC-2). BY_OWNER_OTHERS / EXCLUDED / NULL_BLANK are OUT.
SUBTOTAL_SUM_STATES: tuple[CellState, ...] = (
    CellState.AMOUNT,
    CellState.EXPLICIT_ZERO,
    CellState.ALLOWANCE,
    CellState.NOT_COMPARABLE,
)


def _resolve_subtotal_cell(
    division: DivisionBid,
    line_cells: dict[str, CellValue],
    warnings: list[str],
) -> CellValue:
    """
    Build the division subtotal CellValue.

    For LUMP_SUM: use division_subtotal directly (no arithmetic derivation).
    For ITEMIZED / PARTIAL_ITEMIZED: sum AMOUNT + EXPLICIT_ZERO + ALLOWANCE +
      NOT_COMPARABLE cells (the bidder's OWN arithmetic keeps a not-comparable
      amount; only cross-bid benchmarks fence it out, ENC-2) and compare to
      stated division_subtotal; flag discrepancy.
    BY_OWNER_OTHERS and EXCLUDED cells are excluded from the sum.

    REM-1 (Marvin's ruling, R6/R7): a STATED subtotal of exactly $0 is never a
    valid price — it resolves to EXPLICIT_ZERO (error unless approved), so it
    can never enter a benchmark median as a pre-blessed AMOUNT(0). A DERIVED
    sum of 0 still resolves to NULL_BLANK (the `> 0` guard).
    """
    flags: list[str] = []

    if division.cost_structure == CostStructure.LUMP_SUM:
        if division.division_subtotal is not None:
            if division.division_subtotal == Decimal("0"):
                return CellValue(
                    state=CellState.EXPLICIT_ZERO,
                    amount=Decimal("0"),
                    display="$0",
                    flags=flags,
                )
            return CellValue(
                state=CellState.AMOUNT,
                amount=division.division_subtotal,
                display=_fmt(division.division_subtotal),
                flags=flags,
            )
        else:
            return CellValue(state=CellState.NULL_BLANK, display="-", flags=flags)

    # Derive from line items for ITEMIZED / PARTIAL_ITEMIZED
    computed_sum = Decimal("0")
    for cell in line_cells.values():
        if cell.state in SUBTOTAL_SUM_STATES:
            computed_sum += cell.amount or Decimal("0")

    # Validate against stated subtotal
    if division.division_subtotal is not None:
        delta = abs(computed_sum - division.division_subtotal)
        if delta > Decimal("1"):
            flags.append("ARITHMETIC_DISCREPANCY")
            warnings.append(
                f"{division.csi_code} ({division.division_name}): computed subtotal "
                f"{_fmt(computed_sum)} differs from stated {_fmt(division.division_subtotal)} "
                f"by {_fmt(delta)}"
            )
        # Prefer the stated subtotal for display (extractor is the authority)
        amount = division.division_subtotal
        if amount == Decimal("0"):
            # REM-1: stated $0 → EXPLICIT_ZERO, never AMOUNT(0).
            return CellValue(
                state=CellState.EXPLICIT_ZERO,
                amount=Decimal("0"),
                display="$0",
                flags=flags,
            )
    else:
        # W-D ruling 5 (credit semantics, C-W4-3): the derivation gate is
        # SIGN-AWARE — any non-zero derived sum (a net credit included) is a
        # real AMOUNT; only a derived ZERO resolves to NULL_BLANK (REM-1:
        # derived zero is nothing; a STATED $0 stays EXPLICIT_ZERO above).
        amount = computed_sum if computed_sum != Decimal("0") else None
        if amount is not None:
            # REM-2: mark the subtotal as engine-DERIVED (no stated subtotal
            # on the form) so the leveled sheet can disclose it on-cell.
            flags.append("SUBTOTAL_DERIVED")

    if amount is None:
        return CellValue(state=CellState.NULL_BLANK, display="-", flags=flags)

    return CellValue(
        state=CellState.AMOUNT,
        amount=amount,
        display=_fmt(amount),
        flags=flags,
    )


# ---------------------------------------------------------------------------
# Rule 2 — Code-format remapping (csi_1995_2digit profile, signature-detected)
# ---------------------------------------------------------------------------

def _bid_division_codes(doc: BidDocument) -> list[str]:
    """The division code token per division as the SIGNATURE detector sees it
    (the contractor's native code if present, else the csi_code field)."""
    return [d.contractor_native_code or d.csi_code for d in doc.divisions]


def _all_canonical(codes: list[str]) -> bool:
    """True iff every division code is a canonical `DIV XX 00 00` (lossless-accept).

    An empty code list counts as canonical (nothing to remap or flag)."""
    from src.canon import classify_code_token
    return all(classify_code_token(c) == "CANONICAL" for c in codes)


def _apply_code_format_remap(
    division: DivisionBid,
    warnings: list[str],
    summary_flags: list[BidSummaryFlag],
) -> list[DivisionBid]:
    """Remap one legacy-format DivisionBid to canonical divisions (Marvin §3).

    Returns a list because a split code (15/16) may produce two divisions.
    Emits YELLOW CODE_FORMAT_REMAPPED per remapped line (lossless translation).
    Sets classification_source=PIPELINE_REMAPPED + contractor_native_code.
    """
    native_code = division.contractor_native_code or division.csi_code
    canonical = resolve_legacy_code(native_code)

    if canonical is None:
        # Should not happen once detection passed; conservative pass-through.
        warnings.append(
            f"Legacy code '{native_code}' not in csi_1995_2digit profile — "
            f"passed through unchanged"
        )
        return [division]

    # Footer sentinels are handled at the bid level (_build_normalized_footer).
    if isinstance(canonical, str) and canonical.startswith("_") \
            and canonical != UNMAPPED_DIVISION:
        return [division]

    if canonical == UNMAPPED_DIVISION:
        # Legacy 14 Conveying etc. — no canonical target; hold + YELLOW (Marvin §3 n2).
        summary_flags.append(BidSummaryFlag(
            flag_type="CODE_FORMAT_REMAPPED",
            severity="warning",
            division_csi=None,
            value=native_code,
            message=(
                f"Bidder submitted on a legacy cost-code system; legacy code "
                f"'{native_code}' has no canonical division in this taxonomy — "
                f"placed in a labeled UNMAPPED holding row. Verify scope."
            ),
        ))
        remapped = division.model_copy(update={
            "csi_code": UNMAPPED_DIVISION,
            "division_name": "UNMAPPED (legacy code, no canonical target)",
            "classification_source": ClassificationSource.PIPELINE_REMAPPED,
            "contractor_native_code": native_code,
        })
        return [remapped]

    if isinstance(canonical, str):
        # Straight remap.
        summary_flags.append(BidSummaryFlag(
            flag_type="CODE_FORMAT_REMAPPED",
            severity="warning",
            division_csi=canonical,
            value=native_code,
            message=(
                f"Bidder submitted on a legacy cost-code system; their codes were "
                f"translated to the standard divisions. Original code "
                f"'{native_code}' → '{canonical}'. No dollar amounts were changed."
            ),
        ))
        remapped = division.model_copy(update={
            "csi_code": canonical,
            "classification_source": ClassificationSource.PIPELINE_REMAPPED,
            "contractor_native_code": native_code,
            "division_name": canonical,  # normalization resolves the canonical name
        })
        return [remapped]

    # Split remap (list) — code 15 (Mechanical) or 16 (Electrical).
    assert isinstance(canonical, list)
    return _apply_split_remap(division, canonical, native_code, warnings, summary_flags)


def _apply_split_remap(
    division: DivisionBid,
    canonical_codes: list[str],
    native_code: str,
    warnings: list[str],
    summary_flags: list[BidSummaryFlag],
) -> list[DivisionBid]:
    """Split a Mechanical/Electrical legacy division into its two trades.

    Per-sub-line keyword routing via canon.route_split_subline (the ONE table).
    A sub-line matching no target → CODE_SPLIT_UNMATCHED (RED), held in the first
    canonical target. A lump-sum (no routable sub-lines) is itself unroutable →
    CODE_SPLIT_UNMATCHED held in the first target.
    """
    first_code = canonical_codes[0]
    buckets: dict[str, list[LineItem]] = {code: [] for code in canonical_codes}

    routable_items = (
        list(division.line_items)
        if division.line_items and division.cost_structure != CostStructure.LUMP_SUM
        else []
    )

    if not routable_items:
        # No sub-line detail to route — the whole lump is an unverified split.
        _emit_split_unmatched(
            summary_flags, warnings, native_code, division.division_name, first_code
        )
        primary = division.model_copy(update={
            "csi_code": first_code,
            "classification_source": ClassificationSource.PIPELINE_REMAPPED,
            "contractor_native_code": native_code,
            "cost_structure": CostStructure.LUMP_SUM,
        })
        secondary = [
            DivisionBid(
                csi_code=code, division_name=code,
                cost_structure=CostStructure.LUMP_SUM,
                classification_source=ClassificationSource.PIPELINE_REMAPPED,
                contractor_native_code=native_code,
                line_items=[], division_subtotal=None,
            )
            for code in canonical_codes[1:]
        ]
        return [primary] + secondary

    # Route each sub-line through the ONE keyword table.
    for item in routable_items:
        target = route_split_subline(native_code, item.description)
        if target is None:
            _emit_split_unmatched(
                summary_flags, warnings, native_code, item.description, first_code
            )
            buckets[first_code].append(item)
        else:
            buckets[target].append(item)

    result: list[DivisionBid] = []
    for code in canonical_codes:
        items = buckets[code]
        subtotal = (
            sum((i.amount or Decimal("0")) for i in items
                if not i.is_by_owner_others and not i.is_excluded)
            if items else None
        )
        result.append(DivisionBid(
            csi_code=code,
            division_name=code,
            cost_structure=CostStructure.ITEMIZED if items else CostStructure.LUMP_SUM,
            classification_source=ClassificationSource.PIPELINE_REMAPPED,
            contractor_native_code=native_code,
            line_items=items,
            division_subtotal=subtotal if subtotal else None,
        ))
    return result


def _emit_split_unmatched(
    summary_flags: list[BidSummaryFlag],
    warnings: list[str],
    native_code: str,
    description: str,
    held_in: str,
) -> None:
    """Emit a RED CODE_SPLIT_UNMATCHED flag for an unroutable split sub-line (§5)."""
    warnings.append(
        f"Legacy code {native_code} sub-line '{description}' could not be routed "
        f"to a trade — placed in {held_in} pending manual review."
    )
    summary_flags.append(BidSummaryFlag(
        flag_type="CODE_SPLIT_UNMATCHED",
        severity="critical",
        division_csi=held_in,
        line_item_desc=description,
        value=native_code,
        message=(
            f"A line (Mechanical or Electrical) could not be confidently assigned "
            f"between its two trade divisions ('{description}'). It was placed in "
            f"{held_in} pending review. Verify the trade split before comparing "
            f"this division."
        ),
    ))


# ---------------------------------------------------------------------------
# Rule 3 — Known-firm reclassifications (config-driven; opt-in per matched firm)
# ---------------------------------------------------------------------------

def _div_short(csi_code: str) -> str:
    """Render the bare `DIV NN` form for marker/flag readability (drop ` 00 00`).

    `DIV 01 00 00` → `DIV 01`. Pass through anything that doesn't match the
    canonical pattern (e.g. an UNMAPPED sentinel) unchanged.
    """
    parts = csi_code.split()
    if len(parts) >= 2 and parts[0] == "DIV":
        return f"DIV {parts[1]}"
    return csi_code


def _detect_known_firm_reclassifications(
    divisions: list[DivisionBid],
    firm: Firm,
    warnings: list[str],
    summary_flags: list[BidSummaryFlag],
) -> list[ReclassRecommendation]:
    """Detect a matched firm's reclass rules WITHOUT moving dollars (Option C §1).

    ANNOTATE-ONLY: the dollars stay where the contractor submitted them on the
    mirror. For each fired rule this records a ReclassRecommendation and emits a
    reframed YELLOW KNOWN_FIRM_RECLASSIFIED flag (§5) keyed to the FROM division
    (where the dollars actually sit on the mirror). The dollar move happens only
    in build_normalized_view (§3). Only ever called for an unambiguous match.
    """
    def _matches(rule: Reclassification, description: str) -> bool:
        desc = description.lower()
        return all(kw.lower() in desc for kw in rule.when_description_contains_all)

    div_items: dict[str, list[LineItem]] = {
        div.csi_code: list(div.line_items) for div in divisions
    }

    recommendations: list[ReclassRecommendation] = []
    for rule in firm.reclassifications:
        from_code, to_code = rule.from_division, rule.to_division
        if from_code not in div_items:
            continue

        moving = [
            item for item in div_items[from_code] if _matches(rule, item.description)
        ]
        if not moving:
            continue

        canonical = get_canonical_division(to_code)
        to_name = canonical["division_name"] if canonical else to_code
        from_canonical = get_canonical_division(from_code)
        from_name = from_canonical["division_name"] if from_canonical else from_code
        from_short, to_short = _div_short(from_code), _div_short(to_code)

        for item in moving:
            warnings.append(
                f"{firm.firm_id}: '{item.description}' recommended reclass "
                f"{from_code} → {to_code} (rule {rule.rule_id}); shown in place on "
                f"Bid_Form, applied in Leveled_Normalized"
            )
            recommendations.append(ReclassRecommendation(
                line_item_desc=item.description,
                from_division=from_code,
                to_division=to_code,
                to_division_name=to_name,
                amount=item.amount,
                rule_id=rule.rule_id,
            ))
            summary_flags.append(BidSummaryFlag(
                flag_type="KNOWN_FIRM_RECLASSIFIED",
                severity="warning",
                division_csi=from_code,
                line_item_desc=item.description,
                value=f"{from_code} → {to_code}",
                message=(
                    f"Bidder is known to file '{item.description}' under division "
                    f"{from_short} ({from_name}); the estimator recommends normalizing "
                    f"it to {to_short} ({to_name}). It is shown IN PLACE on the Bid_Form "
                    f"(so the Bid_Form matches the submitted bid) and is applied in the "
                    f"Leveled_Normalized view used for apples-to-apples comparison."
                ),
            ))

    return recommendations


def _apply_reclass_moves(
    divisions: list[DivisionBid],
    recommendations: list[ReclassRecommendation],
) -> list[DivisionBid]:
    """Apply recommended reclass moves to a DivisionBid list (leveled view, §3).

    Relocates each recommended line item from its `from_division` into its
    `to_division`, re-derives ONLY the subtotals of divisions a move actually
    TOUCHED (the union of from/to across FIRED recommendations — Marvin
    GOLD-DEV-10 ruling (1)), and creates the target division if it does not yet
    exist. Every UNTOUCHED division passes through byte-identical: its stated
    subtotal, cost structure, and REM-1 stated-$0 (EXPLICIT_ZERO) survive, so a
    reclass match can never silently heal an R20 math error, understate an
    allowance-bearing subtotal, or blank a verified $0 elsewhere in the bid.
    Touched-division re-derivation uses the SAME state→amount inclusion table
    as _resolve_subtotal_cell (SUBTOTAL_SUM_STATES — ruling (2)). Grand totals
    are unchanged (the move is between divisions). This is the destructive half
    that §1 split out of normalization — it runs ONLY for the leveled view.
    """
    if not recommendations:
        return list(divisions)

    # Map each recommended (from_division, line_item_desc) to its target.
    move_targets: dict[tuple[str, str], str] = {
        (rec.from_division, rec.line_item_desc): rec.to_division
        for rec in recommendations
    }

    div_items: dict[str, list[LineItem]] = {}
    div_meta: dict[str, DivisionBid] = {}
    for div in divisions:
        div_items[div.csi_code] = list(div.line_items)
        div_meta[div.csi_code] = div

    # Divisions a move actually FIRED on (from + to). Only these are rebuilt.
    touched: set[str] = set()

    for (from_code, desc), to_code in move_targets.items():
        if from_code not in div_items:
            continue
        staying: list[LineItem] = []
        moving: list[LineItem] = []
        for item in div_items[from_code]:
            (moving if item.description == desc else staying).append(item)
        if not moving:
            continue
        div_items[from_code] = staying
        div_items.setdefault(to_code, []).extend(moving)
        touched.add(from_code)
        touched.add(to_code)
        if to_code not in div_meta:
            canonical = get_canonical_division(to_code)
            div_meta[to_code] = DivisionBid(
                csi_code=to_code,
                division_name=canonical["division_name"] if canonical else to_code,
                classification_source=ClassificationSource.PIPELINE_REMAPPED,
                contractor_native_code=from_code,
                line_items=[],
                division_subtotal=None,
            )

    def _rederive_touched_subtotal(
        div: DivisionBid, items: list[LineItem]
    ) -> Optional[Decimal]:
        """Re-derive a TOUCHED division's subtotal from its post-move lines.

        Uses the SAME state→amount inclusion table as _resolve_subtotal_cell
        (SUBTOTAL_SUM_STATES): allowance and not-comparable amounts count;
        by-owner and excluded lines do not. A LUMP_SUM target keeps its stated
        total as the base (its dollars were never itemized) and ADDS the
        moved-in line amounts. A derived sum of 0 with no lump base resolves
        to None (NULL_BLANK downstream — REM-1's derived-zero rule).

        W-D ruling 5 / Floyd C-W4-3: the gate is SIGN-AWARE (`!= 0`, not
        `> 0`) — a from-division retaining a NET-NEGATIVE remainder (a credit
        line) keeps its negative subtotal instead of being silently blanked
        (R33: never silently alter a bid). The vacated-division rendering
        never eats it: div_status classifies the negative AMOUNT as "priced",
        so the writer's kind=="missing" vacated branch cannot fire.
        """
        line_sum = Decimal("0")
        for item in items:
            state, amount, _display = _resolve_cell_state(item)
            if state in SUBTOTAL_SUM_STATES:
                line_sum += amount or Decimal("0")
        if div.cost_structure == CostStructure.LUMP_SUM:
            if div.division_subtotal is None:
                return line_sum if line_sum != Decimal("0") else None
            return div.division_subtotal + line_sum
        return line_sum if line_sum != Decimal("0") else None

    result: list[DivisionBid] = []
    for div in divisions:
        if div.csi_code not in touched:
            # Byte-identical pass-through (ruling (1)) — the original
            # DivisionBid, stated subtotal and EXPLICIT_ZERO semantics intact.
            result.append(div)
            continue
        items = div_items.get(div.csi_code, [])
        result.append(div.model_copy(update={
            "line_items": items,
            "division_subtotal": _rederive_touched_subtotal(div, items),
        }))

    existing_codes = {d.csi_code for d in divisions}
    for code, meta in div_meta.items():
        if code not in existing_codes:
            items = div_items.get(code, [])
            result.append(meta.model_copy(update={
                "line_items": items,
                "division_subtotal": _rederive_touched_subtotal(meta, items),
                "classification_source": ClassificationSource.PIPELINE_REMAPPED,
                "contractor_native_code": meta.contractor_native_code or code,
            }))

    return result


# ---------------------------------------------------------------------------
# Rule 4 — Allowance treatment
# ---------------------------------------------------------------------------

def _apply_allowance_treatment(
    normalized_bid: NormalizedBid,
) -> NormalizedBid:
    """
    After divisions are built, total up all allowances and emit the
    BidSummaryFlag.  Returns the bid with total_allowance_value, allowance_count,
    and the flag set.

    The division subtotals already INCLUDE allowance amounts (they are in the
    contract) — but the leveled_total in the footer will exclude them from
    hard-cost comparison.
    """
    total = Decimal("0")
    count = 0

    for div in normalized_bid.divisions:
        for cell in div.line_item_cells.values():
            if cell.state == CellState.ALLOWANCE and cell.amount:
                total += cell.amount
                count += 1

    updated_flags = list(normalized_bid.summary_flags)
    if count > 0:
        updated_flags.append(BidSummaryFlag(
            flag_type="ALLOWANCE_PRESENT",
            message=(
                f"{count} item(s) totalling {_fmt(total)} are priced as allowances "
                f"— final cost may vary"
            ),
            severity="warning",
        ))

    return normalized_bid.model_copy(update={
        "total_allowance_value": total,
        "allowance_count": count,
        "summary_flags": updated_flags,
    })


# ---------------------------------------------------------------------------
# Rule 5 — GC Fee % normalization
# ---------------------------------------------------------------------------

def _compute_gc_fee_pct(footer: "NormalizedFooter") -> Optional[Decimal]:
    """
    Phase 1 (per-bid): Compute gc_fee_pct = gc_fee / construction_subtotal * 100.
    Returns None when either value is not set or is zero.
    """
    gc_fee_cell = footer.gc_fee
    subtotal_cell = footer.construction_subtotal

    if (
        gc_fee_cell.state != CellState.AMOUNT
        or subtotal_cell.state != CellState.AMOUNT
        or gc_fee_cell.amount is None
        or subtotal_cell.amount is None
        or subtotal_cell.amount == Decimal("0")
    ):
        return None

    return (gc_fee_cell.amount / subtotal_cell.amount * Decimal("100")).quantize(
        Decimal("0.01")
    )


def compute_cross_bid_stats(bids: list[NormalizedBid]) -> list[NormalizedBid]:
    """
    Rule 5 Phase 2 (cross-bid): Compute field mean and standard deviation of
    gc_fee_pct across all bidders where it is computable.  Flag outliers.
    Also populates implicit_gap_count on each bid using field medians.

    Returns the updated list of NormalizedBid objects.  Input list is not mutated.

    Small-n behavior (Marvin §6, N3):
      * n = 1 — NO cross-bid flags at all (no scope gap, no GC outlier, no
        variance): a single bid is not a comparison. Intra-bid flags are
        untouched. The "single bid" notice is rendered by the writer.
      * n = 2 — pairwise comparison is valid (scope gaps, spreads) but the
        stddev GC-fee outlier is suppressed (degenerate with two points).
    """
    n_bids = len(bids)
    if n_bids <= 1:
        # No cross-bid statistic is meaningful with a single bidder — return as-is.
        return list(bids)

    # Collect computable gc_fee_pct values
    pct_values: list[Decimal] = []
    for bid in bids:
        pct = bid.footer.gc_fee_pct
        if pct is not None:
            pct_values.append(pct)

    # Field mean/stddev for the GC-fee outlier — only meaningful at n >= 3
    # (the stddev outlier is degenerate at n=2; suppress it per §6).
    field_mean: Optional[float] = None
    field_stddev: Optional[float] = None
    if n_bids >= 3 and len(pct_values) >= 2:
        pct_floats = [float(p) for p in pct_values]
        field_mean = statistics.mean(pct_floats)
        field_stddev = statistics.stdev(pct_floats)

    # Compute field medians for implicit gap detection
    division_subtotals_by_bid: list[dict[str, Optional[Decimal]]] = []
    for bid in bids:
        bid_subtotals: dict[str, Optional[Decimal]] = {}
        for div in bid.divisions:
            subtotal = div.subtotal_cell.amount if div.subtotal_cell.state == CellState.AMOUNT else None
            bid_subtotals[div.csi_code] = subtotal
        division_subtotals_by_bid.append(bid_subtotals)

    field_medians = compute_field_medians(division_subtotals_by_bid)

    updated_bids: list[NormalizedBid] = []
    for bid in bids:
        new_flags = list(bid.summary_flags)
        new_warnings = list(bid.normalization_warnings)
        pct = bid.footer.gc_fee_pct

        # GC fee outlier flagging
        if pct is None:
            new_flags.append(BidSummaryFlag(
                flag_type="GC_FEE_MISSING",
                message="GC Fee not separately stated — may be baked into division costs",
                severity="warning",
            ))
        elif field_mean is not None and field_stddev is not None and field_stddev > 0:
            pct_float = float(pct)
            deviation = abs(pct_float - field_mean) / field_stddev
            if deviation > GC_FEE_OUTLIER_STDDEV:
                if pct_float > field_mean:
                    new_flags.append(BidSummaryFlag(
                        flag_type="GC_FEE_OUTLIER",
                        message=(
                            f"GC Fee of {pct_float:.1f}% is above the field average "
                            f"({field_mean:.1f}%) — verify scope includes overhead"
                        ),
                        severity="warning",
                    ))
                else:
                    new_flags.append(BidSummaryFlag(
                        flag_type="GC_FEE_OUTLIER",
                        message=(
                            f"GC Fee of {pct_float:.1f}% is below the field average "
                            f"({field_mean:.1f}%) — verify overhead is not baked in"
                        ),
                        severity="warning",
                    ))

        # Phantom-gap fix (Option C §6): a division emptied by a reclassification
        # for THIS bidder must never raise SCOPE_GAP_IMPLICIT — the scope did not
        # vanish, it moved into another division. Suppression is keyed to the
        # `from_division` of this bid's own reclass recommendations (surgical: a
        # different bidder who genuinely left the division blank still flags).
        vacated_by_reclass = {
            rec.from_division for rec in bid.reclass_recommendations
        }

        # Implicit gap counting using field medians
        implicit_gap_count = 0
        updated_divisions: list["NormalizedDivision"] = []
        for div in bid.divisions:
            updated_cells = dict(div.line_item_cells)
            if div.csi_code in vacated_by_reclass:
                # Reclass-driven blank — not a scope gap. Leave the division as-is.
                updated_divisions.append(div)
                continue
            if div.subtotal_cell.state == CellState.NULL_BLANK:
                median = field_medians.get(div.csi_code, Decimal("0"))
                if median > SCOPE_GAP_MEDIAN_THRESHOLD:
                    implicit_gap_count += 1
                    # Flag the subtotal cell
                    flagged_cell = div.subtotal_cell.model_copy(update={
                        "flags": list(div.subtotal_cell.flags) + ["SCOPE_GAP_IMPLICIT"],
                    })
                    updated_div = div.model_copy(update={"subtotal_cell": flagged_cell})
                    updated_divisions.append(updated_div)
                    continue

            # Also check individual null cells within the division
            cell_gap_found = False
            for label, cell in updated_cells.items():
                if cell.state == CellState.NULL_BLANK:
                    median = field_medians.get(div.csi_code, Decimal("0"))
                    if median > SCOPE_GAP_MEDIAN_THRESHOLD and not cell_gap_found:
                        implicit_gap_count += 1
                        cell_gap_found = True
                        updated_cells[label] = cell.model_copy(update={
                            "flags": list(cell.flags) + ["SCOPE_GAP_IMPLICIT"],
                        })

            if updated_cells != div.line_item_cells:
                updated_divisions.append(div.model_copy(update={"line_item_cells": updated_cells}))
            else:
                updated_divisions.append(div)

        updated_bids.append(bid.model_copy(update={
            "summary_flags": new_flags,
            "normalization_warnings": new_warnings,
            "implicit_gap_count": implicit_gap_count,
            "divisions": updated_divisions,
        }))

    return updated_bids


# ---------------------------------------------------------------------------
# Rule 6 — Image-scan confidence validation
# ---------------------------------------------------------------------------

def _validate_image_confidence(
    doc: BidDocument,
    warnings: list[str],
) -> None:
    """
    Rule 6: Warn when an IMAGE_SCAN document was extracted with non-LOW
    confidence.  Does not change routing or extraction — enforces appropriate
    caution marking.
    """
    if (
        doc.bid_document_input_type == InputType.IMAGE_SCAN
        and doc.extraction_confidence != ExtractionConfidence.LOW
    ):
        warnings.append(
            f"IMAGE_SCAN document extracted with confidence {doc.extraction_confidence.value} "
            f"— consider upgrading to LOW given OCR uncertainty"
        )


# ---------------------------------------------------------------------------
# Division normalization helpers
# ---------------------------------------------------------------------------

def _normalize_division(
    division: DivisionBid,
    warnings: list[str],
) -> "NormalizedDivision":
    """
    Build a NormalizedDivision from a (post-remapping) DivisionBid.
    Applies Rule 1 cell-state resolution to all line items.
    """
    from src.normalized_models import NormalizedDivision
    from src.canon import get_canonical_division

    # Resolve canonical division name if available
    canonical = get_canonical_division(division.csi_code)
    division_name = canonical["division_name"] if canonical else division.division_name

    line_item_cells: dict[str, CellValue] = {}
    explicit_exclusion_count = 0

    for item in division.line_items:
        state, amount, display = _resolve_cell_state(item)
        flags: list[str] = []

        if state == CellState.EXCLUDED:
            explicit_exclusion_count += 1

        # Determine reclassification status
        is_reclassified = (
            division.classification_source == ClassificationSource.PIPELINE_REMAPPED
        )
        reclassified_from = division.contractor_native_code if is_reclassified else None

        cell = CellValue(
            state=state,
            amount=amount,
            display=display,
            is_reclassified=is_reclassified,
            reclassified_from=reclassified_from,
            flags=flags,
        )
        # Use description as key; handle duplicates by appending index
        key = item.description
        if key in line_item_cells:
            idx = sum(1 for k in line_item_cells if k.startswith(key))
            key = f"{key} ({idx})"
        line_item_cells[key] = cell

    subtotal_cell = _resolve_subtotal_cell(division, line_item_cells, warnings)

    return NormalizedDivision(
        csi_code=division.csi_code,
        division_name=division_name,
        line_item_cells=line_item_cells,
        subtotal_cell=subtotal_cell,
        cost_structure=division.cost_structure,
    )


def _build_normalized_footer(
    doc: BidDocument,
    warnings: list[str],
    divisions: list[DivisionBid],
) -> NormalizedFooter:
    """
    Build the NormalizedFooter from BidFooter.
    Applies Rule 1 cell-state logic to all footer fields.
    Computes gc_fee_pct (Rule 5 Phase 1).
    Computes leveled_total (grand_total minus BY_OWNER_OTHERS amounts).

    Parameters
    ----------
    doc:
        The raw BidDocument (provides footer values and confidence).
    warnings:
        Accumulating list of normalization warnings (mutated in place).
    divisions:
        Post-remapping DivisionBid list used to accumulate the
        BY_OWNER_OTHERS deduction for leveled_total.
    """
    from src.normalized_models import NormalizedAlternate, NormalizedFooter

    f = doc.footer

    # M7: surface alternates as their own structured list (never folded into base).
    alternates = [
        NormalizedAlternate(
            description=alt.description,
            amount=alt.amount,
            display=_fmt(alt.amount) if alt.amount is not None else "-",
        )
        for alt in f.alternates
    ]

    def _money_cell(amount: Optional[Decimal]) -> CellValue:
        if amount is None:
            return CellValue(state=CellState.NULL_BLANK, display="-")
        return CellValue(state=CellState.AMOUNT, amount=amount, display=_fmt(amount))

    construction_subtotal_cell = _money_cell(f.construction_cost_subtotal)
    gc_fee_cell = _money_cell(f.gc_fee)
    gl_cell = _money_cell(f.general_liability_insurance)
    br_cell = _money_cell(f.builders_risk_insurance)
    ohp_cell = _money_cell(f.overhead_and_profit)
    other_fees_cell = _money_cell(f.other_fees_subtotal)
    grand_total_cell = _money_cell(f.grand_total)
    bond_cell = _money_cell(f.bond)

    footer = NormalizedFooter(
        construction_subtotal=construction_subtotal_cell,
        general_liability_insurance=gl_cell,
        builders_risk_insurance=br_cell,
        gc_fee=gc_fee_cell,
        overhead_and_profit=ohp_cell,
        other_fees_subtotal=other_fees_cell,
        grand_total=grand_total_cell,
        bond=bond_cell,
        gc_fee_pct=None,  # computed next
        grand_total_confidence=f.grand_total_confidence,
        confidence_flags=list(f.confidence_flags),
        leveled_total=None,  # computed next
        alternates=alternates,
    )

    # Rule 5 Phase 1: compute gc_fee_pct
    gc_fee_pct = _compute_gc_fee_pct(footer)
    footer = footer.model_copy(update={"gc_fee_pct": gc_fee_pct})

    # Compute leveled_total: grand_total minus sum of BY_OWNER_OTHERS line-item amounts.
    # Allowances are retained — they are contractual.
    # BY_OWNER_OTHERS items are excluded — they are not the contractor's direct cost.
    if f.grand_total is not None:
        by_owner_deduction: Decimal = Decimal("0")
        for div in divisions:
            for item in div.line_items:
                if item.is_by_owner_others and item.amount is not None:
                    by_owner_deduction += item.amount
        footer = footer.model_copy(update={"leveled_total": f.grand_total - by_owner_deduction})

    return footer


def _build_qualifications_text(doc: BidDocument) -> str:
    """Concatenate all qualification fields into a single plain-text string."""
    q = doc.qualifications
    parts: list[str] = []
    if q.notes:
        parts.append(f"Notes:\n{q.notes}")
    if q.qualifications:
        parts.append(f"Qualifications:\n{q.qualifications}")
    if q.exclusions:
        parts.append(f"Exclusions:\n{q.exclusions}")
    if q.assumptions:
        parts.append(f"Assumptions:\n{q.assumptions}")
    if q.terms:
        parts.append(f"Terms:\n{q.terms}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Primary entry point
# ---------------------------------------------------------------------------

_KNOWN_FIRMS_CACHE: Optional[KnownFirmsConfig] = None


def _known_firms() -> KnownFirmsConfig:
    """Lazily load + cache the shipped known_firms.yaml (validated)."""
    global _KNOWN_FIRMS_CACHE
    if _KNOWN_FIRMS_CACHE is None:
        _KNOWN_FIRMS_CACHE = load_known_firms()
    return _KNOWN_FIRMS_CACHE


def normalize_bid(
    doc: BidDocument,
    known_firms: Optional[KnownFirmsConfig] = None,
) -> NormalizedBid:
    """
    Apply all per-bid normalization rules to a BidDocument and return a
    NormalizedBid ready for the Excel writer.

    Firm/format decision order (Marvin §2.3 / §1):
      A. Known-firm match (config, name-based, C3 collision-safe).
         - ambiguous (>1 match) → RED KNOWN_FIRM_AMBIGUOUS, NO reclass/profile.
         - exactly one match     → apply its reclass rules (destructive, opt-in).
      B. Code-format profile (signature-detected, name-independent, lossless):
         - csi_1995_2digit detected → remap (YELLOW CODE_FORMAT_REMAPPED).
         - all-canonical            → accept CONTRACTOR_NATIVE (no remap, no flag).
         - anything else (mixed/unknown) → RED UNRECOGNIZED_CODE_FORMAT, no remap.

    Then the original rules: 6 (image confidence), 1 (cell-state), 4 (allowance),
    5 Phase 1 (GC fee %). Rule 5 Phase 2 (cross-bid) is compute_cross_bid_stats().

    known_firms may be injected (tests); defaults to the shipped library.
    """
    normalization_warnings: list[str] = list(doc.extraction_warnings)
    summary_flags: list[BidSummaryFlag] = []

    # Rule 6: image-scan confidence check
    _validate_image_confidence(doc, normalization_warnings)

    # Start with the raw divisions
    divisions: list[DivisionBid] = list(doc.divisions)

    # --- A. Known-firm match (C3) ---
    cfg = known_firms if known_firms is not None else _known_firms()
    match = cfg.match(doc.contractor_name)
    if match.ambiguous:
        normalization_warnings.append(
            f"Contractor '{doc.contractor_name}' matched >1 known-firm profile "
            f"({', '.join(match.matched_firm_ids)}) — no firm-specific corrections applied."
        )
        summary_flags.append(BidSummaryFlag(
            flag_type="KNOWN_FIRM_AMBIGUOUS",
            severity="critical",
            division_csi=None,
            value=", ".join(match.matched_firm_ids),
            message=(
                f"Bidder name '{doc.contractor_name}' matched more than one known-firm "
                f"profile, so no firm-specific corrections were applied. An estimator "
                f"must confirm which firm this is before relying on the comparison."
            ),
        ))
    reclass_recommendations: list[ReclassRecommendation] = []
    if match.firm is not None:
        # Option C §1: reclassification is ANNOTATE-ONLY on the mirror. Detect the
        # matched lines + recommended targets WITHOUT moving dollars; the move runs
        # only in build_normalized_view (the leveled view).
        if match.firm.reclassifications:
            reclass_recommendations = _detect_known_firm_reclassifications(
                divisions, match.firm, normalization_warnings, summary_flags
            )

    # --- B. Code-format profile (signature-detected, name-independent) ---
    codes = _bid_division_codes(doc)
    if detect_csi_1995_2digit(codes):
        remapped_divisions: list[DivisionBid] = []
        for div in divisions:
            remapped_divisions.extend(
                _apply_code_format_remap(div, normalization_warnings, summary_flags)
            )
        divisions = remapped_divisions
    elif not _all_canonical(codes):
        # Mixed / unknown / too-thin-legacy → no remap; flag RED (no-silent-mislevel).
        normalization_warnings.append(
            f"Contractor '{doc.contractor_name}' used a cost-code format the engine "
            f"does not recognize — divisions placed as-extracted, verify before leveling."
        )
        summary_flags.append(BidSummaryFlag(
            flag_type="UNRECOGNIZED_CODE_FORMAT",
            severity="critical",
            division_csi=None,
            message=(
                f"Bidder '{doc.contractor_name}' used a cost-code format this tool does "
                f"not recognize. Their dollars were placed as-extracted and must be "
                f"verified by an estimator before this bid is compared. Do not rely on "
                f"this column until reviewed."
            ),
        ))
    # else: all-canonical → accept CONTRACTOR_NATIVE, no remap, no flag (§1.3).

    # Rule 1: Normalize each division (cell-state resolution)
    normalized_divs: list["NormalizedDivision"] = []
    explicit_exclusion_count = 0
    for div in divisions:
        norm_div = _normalize_division(div, normalization_warnings)
        # Count exclusions
        for cell in norm_div.line_item_cells.values():
            if cell.state == CellState.EXCLUDED:
                explicit_exclusion_count += 1
        normalized_divs.append(norm_div)

    # Build footer (includes Rule 5 Phase 1)
    footer = _build_normalized_footer(doc, normalization_warnings, divisions)

    # Build qualifications text
    qualifications_text = _build_qualifications_text(doc)

    # Assemble NormalizedBid (without cross-bid stats yet)
    bid = NormalizedBid(
        contractor_name=doc.contractor_name,
        project_name=doc.project_name,
        form_type=doc.form_type,
        bid_document_input_type=doc.bid_document_input_type,
        extraction_confidence=doc.extraction_confidence,
        divisions=normalized_divs,
        footer=footer,
        qualifications_text=qualifications_text,
        total_allowance_value=Decimal("0"),  # set by Rule 4 next
        allowance_count=0,
        explicit_exclusion_count=explicit_exclusion_count,
        implicit_gap_count=0,  # set by compute_cross_bid_stats()
        extraction_warnings=list(doc.extraction_warnings),
        normalization_warnings=normalization_warnings,
        summary_flags=summary_flags,  # remap/reclass/unrecognized/ambiguous/unmatched
        reclass_recommendations=reclass_recommendations,
    )

    # Rule 4: Allowance treatment (post-division assembly)
    bid = _apply_allowance_treatment(bid)

    return bid


# ---------------------------------------------------------------------------
# Leveled / normalized view builder (Option C §3)
# ---------------------------------------------------------------------------

def build_normalized_view(mirror: NormalizedBid, doc: BidDocument) -> NormalizedBid:
    """Build the leveled (moved-dollar) view from a faithful-mirror bid (Option C §3).

    Takes the as-submitted mirror NormalizedBid plus its source BidDocument and
    returns a second NormalizedBid in which the known-firm reclassifications ARE
    applied (dollars moved between divisions). Grand totals are identical to the
    mirror (the move never changes a bidder's total). All non-reclass
    normalization (code-format remap, cell-state, allowance) is reproduced from
    the same source so the leveled view is the fully-normalized one.

    Named `build_normalized_view` (not `leveled_*`) to avoid colliding with the
    footer's hard-cost ``leveled_total`` field (spec §9).
    """
    if not mirror.reclass_recommendations:
        # No reclass to apply — the leveled view equals the mirror.
        return mirror.model_copy(deep=True)

    warnings: list[str] = list(doc.extraction_warnings)
    summary_flags = list(mirror.summary_flags)

    # Re-derive the as-submitted/remapped DivisionBid list, then apply the moves.
    divisions: list[DivisionBid] = list(doc.divisions)
    codes = _bid_division_codes(doc)
    if detect_csi_1995_2digit(codes):
        remapped: list[DivisionBid] = []
        throwaway_flags: list[BidSummaryFlag] = []
        for div in divisions:
            remapped.extend(
                _apply_code_format_remap(div, warnings, throwaway_flags)
            )
        divisions = remapped
    divisions = _apply_reclass_moves(divisions, mirror.reclass_recommendations)

    normalized_divs: list[NormalizedDivision] = []
    explicit_exclusion_count = 0
    for div in divisions:
        norm_div = _normalize_division(div, warnings)
        for cell in norm_div.line_item_cells.values():
            if cell.state == CellState.EXCLUDED:
                explicit_exclusion_count += 1
        normalized_divs.append(norm_div)

    footer = _build_normalized_footer(doc, warnings, divisions)

    # Allowance totals are unchanged by an inter-division move, so the mirror's
    # allowance accounting (and its ALLOWANCE_PRESENT flag) carry over as-is — do
    # NOT re-run _apply_allowance_treatment or the flag would be duplicated.
    leveled = mirror.model_copy(update={
        "divisions": normalized_divs,
        "footer": footer,
        "explicit_exclusion_count": explicit_exclusion_count,
        "implicit_gap_count": 0,  # set by compute_cross_bid_stats on the leveled set
        "summary_flags": summary_flags,
    })
    return leveled


# ---------------------------------------------------------------------------
# Formatting helper
# ---------------------------------------------------------------------------

def _fmt(amount: Decimal) -> str:
    """Format a Decimal as a board-display dollar string: '$1,234,567'."""
    # Round to nearest dollar for display
    rounded = int(amount.quantize(Decimal("1")))
    return f"${rounded:,}"
