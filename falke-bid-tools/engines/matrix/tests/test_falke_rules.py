"""
FALKE Matrix — Falke leveling-rules tests (Leveled_Normalized, v0.3.0)
=======================================================================
Proves the encoded Falke program rules fire correctly on the written sheet:

  * A6 (Marvin's trap): a BLANK division never enters the benchmark median
    as 0.0 — benchmarks compute from classified cell STATE only.
  * R12/R13 with the INCLUSIVE ±20% boundary (Q1 decided default).
  * R16 red-first precedence (an R20 math error beats a yellow variance).
  * Q5/RISK-2: no cyan/yellow paint below 3 valid bids (benchmark still shows).
  * R6: an unapproved explicit zero paints RED.
  * R5: a blank division paints RED where peers priced it.

Run from the engine root:
    python3 -m pytest tests/test_falke_rules.py -v
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import openpyxl

from src.models import (
    BidDocument,
    BidFooter,
    BidQualifications,
    ClassificationSource,
    CostStructure,
    DivisionBid,
    ExtractionConfidence,
    FormType,
    GrandTotalConfidence,
    InputType,
    LineItem,
)
from src.normalize import compute_cross_bid_stats, normalize_bid
from src.run_config import RunInputs
from src.write_matrix import (
    LEVELED_CSUB_OFFSET,
    _lev_bench_col,
    _lev_col_start,
    write_matrix,
)

CYAN, YELLOW, RED = "00FFFF", "FFFF00", "FF0000"


def _div(code, name, subtotal=None, items=None,
         cost=CostStructure.LUMP_SUM):
    return DivisionBid(
        csi_code=code,
        division_name=name,
        cost_structure=cost,
        division_subtotal=subtotal,
        classification_source=ClassificationSource.CONTRACTOR_NATIVE,
        contractor_native_code=None,
        line_items=items or [],
    )


def _doc(name, divisions, grand_total):
    return BidDocument(
        contractor_name=name,
        form_type=FormType.FALKE_STANDARD,
        bid_document_input_type=InputType.DIGITAL_NATIVE,
        divisions=divisions,
        footer=BidFooter(
            construction_cost_subtotal=grand_total,
            grand_total=grand_total,
            grand_total_confidence=GrandTotalConfidence.LOW,
        ),
        qualifications=BidQualifications(),
        extraction_confidence=ExtractionConfidence.HIGH,
    )


def _concrete_bidder(name, amount):
    return _doc(name, [_div("DIV 03 00 00", "Concrete", Decimal(amount))],
                Decimal(amount))


def _run_inputs():
    return RunInputs(
        project_name="Rules Test",
        project_address="1 Test St",
        gross_sf=10_000.0,
        sf_basis_label="GSF",
        sf_source="explicit",
    )


def _write(tmp, docs):
    bids = compute_cross_bid_stats([normalize_bid(d) for d in docs])
    out = Path(tmp) / "m.xlsx"
    write_matrix(bids, out, _run_inputs())
    return openpyxl.load_workbook(out)["Leveled_Normalized"], len(docs)


def _hex(cell) -> str:
    rgb = cell.fill.fgColor.rgb
    return rgb[-6:].upper() if isinstance(rgb, str) else ""


def _row_of(ws, col, value):
    for r in range(1, ws.max_row + 1):
        if ws.cell(row=r, column=col).value == value:
            return r
    raise AssertionError(f"row with col{col}=={value!r} not found")


def _col_of(ws, n, name):
    for i in range(n):
        if ws.cell(row=5, column=_lev_col_start(i)).value == name:
            return _lev_col_start(i)
    raise AssertionError(f"bidder {name!r} not found on row 5")


class TestBenchmarkFromClassifiedStates:
    def test_blank_never_enters_the_median_as_zero(self):
        """Marvin's A6 trap: 3 priced bids (100k/110k/120k) + 1 BLANK bid.
        Benchmark must be the median of the THREE valid prices (110k) — if the
        blank leaked in as 0.0, the median would drop to 105k."""
        docs = [
            _concrete_bidder("Alpha", "100000"),
            _concrete_bidder("Bravo", "110000"),
            _concrete_bidder("Charlie", "120000"),
            # Delta: DIV 03 blank (NULL_BLANK), priced elsewhere so it has a GT.
            _doc("Delta", [
                _div("DIV 03 00 00", "Concrete", None,
                     items=[LineItem(description="Concrete", amount=None)],
                     cost=CostStructure.ITEMIZED),
                _div("DIV 01 00 00", "General Requirements", Decimal("50000")),
            ], Decimal("50000")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, docs)
            sub_row = _row_of(ws, 2, "CONCRETE SUBTOTAL")
            bench = ws.cell(row=sub_row, column=_lev_bench_col(n)).value
            n_valid = ws.cell(row=sub_row, column=_lev_bench_col(n) + 2).value
            assert bench == 110000.0, (
                f"median must be 110k over the 3 valid prices, got {bench} "
                f"(a blank entered the median as 0.0)"
            )
            assert n_valid == 3
            # R5: Delta's blank DIV 03 paints RED (peers priced it).
            delta_csub = ws.cell(
                row=sub_row, column=_col_of(ws, n, "Delta") + LEVELED_CSUB_OFFSET
            )
            assert _hex(delta_csub) == RED
            assert delta_csub.comment is not None
            assert "R5" in delta_csub.comment.text

    def test_zero_subtotal_paints_red_not_neutral(self):
        """R6 at line level: an unapproved explicit zero paints RED on the
        line's COST cell."""
        docs = [
            _doc("Alpha", [_div(
                "DIV 01 00 00", "General Requirements", Decimal("0"),
                items=[LineItem(description="Final Cleaning",
                                amount=Decimal("0"), is_explicit_zero=True)],
                cost=CostStructure.ITEMIZED,
            )], Decimal("0")),
            _doc("Bravo", [_div(
                "DIV 01 00 00", "General Requirements", Decimal("10000"),
                items=[LineItem(description="Final Cleaning",
                                amount=Decimal("10000"))],
                cost=CostStructure.ITEMIZED,
            )], Decimal("10000")),
            _doc("Charlie", [_div(
                "DIV 01 00 00", "General Requirements", Decimal("12000"),
                items=[LineItem(description="Final Cleaning",
                                amount=Decimal("12000"))],
                cost=CostStructure.ITEMIZED,
            )], Decimal("12000")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, docs)
            line_row = _row_of(ws, 2, "Final Cleaning")
            zero_cell = ws.cell(row=line_row, column=_col_of(ws, n, "Alpha"))
            assert zero_cell.value == 0.0
            assert _hex(zero_cell) == RED
            assert "R6" in zero_cell.comment.text


class TestVarianceBoundaries:
    def _field(self):
        # Median of (80k, 100k, 100k, 120k) = 100k. Low bidder sits EXACTLY at
        # ×0.80, high bidder EXACTLY at ×1.20 — the Q1 inclusive boundary.
        return [
            _concrete_bidder("Low Bidder", "80000"),
            _concrete_bidder("Mid One", "100000"),
            _concrete_bidder("Mid Two", "100000"),
            _concrete_bidder("High Bidder", "120000"),
        ]

    def test_inclusive_20pct_boundaries_cyan_and_yellow(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, self._field())
            sub_row = _row_of(ws, 2, "CONCRETE SUBTOTAL")

            low = ws.cell(row=sub_row,
                          column=_col_of(ws, n, "Low Bidder") + LEVELED_CSUB_OFFSET)
            high = ws.cell(row=sub_row,
                           column=_col_of(ws, n, "High Bidder") + LEVELED_CSUB_OFFSET)
            mid = ws.cell(row=sub_row,
                          column=_col_of(ws, n, "Mid One") + LEVELED_CSUB_OFFSET)
            assert _hex(low) == CYAN, "price at exactly ×0.80 must paint cyan (Q1)"
            assert _hex(high) == YELLOW, "price at exactly ×1.20 must paint yellow (Q1)"
            assert _hex(mid) == "A3EAF3", "within-range subtotal keeps the aqua band"
            assert "20% below" in low.comment.text     # R31 cyan language
            assert "20% above" in high.comment.text    # R31 yellow language

    def test_var_pct_written_in_separator_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, self._field())
            sub_row = _row_of(ws, 2, "CONCRETE SUBTOTAL")
            var_col = _col_of(ws, n, "Low Bidder") + 4  # VAR % offset
            assert abs(ws.cell(row=sub_row, column=var_col).value - (-0.2)) < 1e-9


class TestRedFirstPrecedence:
    def test_r20_math_error_beats_yellow_variance(self):
        """A bidder whose stated subtotal (200k) both fails R20 (items sum to
        100k) and sits ≥×1.20 over the benchmark must paint RED, not yellow
        (R16: Red > Cyan > Yellow)."""
        docs = [
            _doc("Wrongmath", [_div(
                "DIV 03 00 00", "Concrete", Decimal("200000"),
                items=[LineItem(description="Concrete work",
                                amount=Decimal("100000"))],
                cost=CostStructure.LUMP_SUM,  # stated subtotal wins the cell
            )], Decimal("200000")),
            _concrete_bidder("Peer One", "100000"),
            _concrete_bidder("Peer Two", "100000"),
            _concrete_bidder("Peer Three", "100000"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, docs)
            sub_row = _row_of(ws, 2, "CONCRETE SUBTOTAL")
            cell = ws.cell(row=sub_row,
                           column=_col_of(ws, n, "Wrongmath") + LEVELED_CSUB_OFFSET)
            assert _hex(cell) == RED, "R16: red must beat the yellow variance"
            assert "R20" in cell.comment.text


class TestMinimumBidGate:
    def test_no_variance_paint_below_three_valid_bids(self):
        """RISK-2/Q5: with 2 valid bids (100k vs 160k) both sit beyond ±20% of
        the 130k midpoint-median — but paint is SUPPRESSED below 3 valid bids.
        The benchmark still displays."""
        docs = [
            _concrete_bidder("Alpha", "100000"),
            _concrete_bidder("Bravo", "160000"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, docs)
            sub_row = _row_of(ws, 2, "CONCRETE SUBTOTAL")
            for name in ("Alpha", "Bravo"):
                cell = ws.cell(
                    row=sub_row, column=_col_of(ws, n, name) + LEVELED_CSUB_OFFSET
                )
                assert _hex(cell) == "A3EAF3", (
                    f"{name}: no cyan/yellow below 3 valid bids — got {_hex(cell)}"
                )
            assert ws.cell(row=sub_row, column=_lev_bench_col(n)).value == 130000.0
            # R29/Q2: <3 valid bids ⇒ Low confidence.
            assert ws.cell(row=sub_row, column=_lev_bench_col(n) + 3).value == "Low"


class TestExclusionAndSummary:
    def test_unapproved_exclusion_paints_red_and_summary_counts(self):
        docs = [
            _doc("Excluder", [
                _div("DIV 03 00 00", "Concrete", None,
                     items=[LineItem(description="Concrete work",
                                     is_excluded=True)],
                     cost=CostStructure.ITEMIZED),
                _div("DIV 01 00 00", "General Requirements", Decimal("50000")),
            ], Decimal("50000")),
            _concrete_bidder("Peer One", "100000"),
            _concrete_bidder("Peer Two", "110000"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, docs)
            sub_row = _row_of(ws, 2, "CONCRETE SUBTOTAL")
            cell = ws.cell(row=sub_row,
                           column=_col_of(ws, n, "Excluder") + LEVELED_CSUB_OFFSET)
            assert cell.value == "Excluded"
            assert _hex(cell) == RED
            assert "R28" in cell.comment.text
            # R32 summary block: Excluder carries ≥1 red flag.
            red_row = _row_of(ws, 2, "Red Flags")
            red_count = ws.cell(
                row=red_row,
                column=_col_of(ws, n, "Excluder") + LEVELED_CSUB_OFFSET,
            ).value
            assert red_count and red_count >= 1
            # Legend present.
            _row_of(ws, 2, "LEGEND — FALKE HIGHLIGHT VOCABULARY "
                           "(precedence: Red > Cyan > Yellow > Neutral)")


class TestRem1StatedZeroSubtotal:
    """Marvin's REM-1 (gold-standard diff, 2026-07-03): a stated $0 division
    subtotal is NEVER a valid benchmark price (R6/R7)."""

    def test_marvin_acceptance_stated_zero_with_excluded_lines(self):
        """Marvin's acceptance test verbatim: A=8,675; B=34,081.85; C=55,700;
        D stated-$0 subtotal over excluded lines ⇒ benchmark 34,081.85 over
        3 valid bids, D's cell reads "Excluded" + RED + R28 comment, and D's
        zero is absent from the prices (with it, the median would be the
        21,378.43 midpoint)."""
        docs = [
            _concrete_bidder("Alpha", "8675"),
            _concrete_bidder("Bravo", "34081.85"),
            _concrete_bidder("Charlie", "55700"),
            _doc("Delta", [
                _div("DIV 03 00 00", "Concrete", Decimal("0"),
                     items=[LineItem(description="Concrete work",
                                     is_excluded=True)],
                     cost=CostStructure.ITEMIZED),
                _div("DIV 01 00 00", "General Requirements", Decimal("50000")),
            ], Decimal("50000")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, docs)
            sub_row = _row_of(ws, 2, "CONCRETE SUBTOTAL")
            bench = ws.cell(row=sub_row, column=_lev_bench_col(n)).value
            n_valid = ws.cell(row=sub_row, column=_lev_bench_col(n) + 2).value
            assert bench == 34081.85, (
                f"benchmark must be 34,081.85 over the 3 valid prices, got "
                f"{bench} — a stated $0 subtotal entered the median (REM-1)"
            )
            assert n_valid == 3
            cell = ws.cell(row=sub_row,
                           column=_col_of(ws, n, "Delta") + LEVELED_CSUB_OFFSET)
            assert cell.value == "Excluded"
            assert _hex(cell) == RED
            assert "R28" in cell.comment.text

    def test_stated_zero_without_classified_lines_stays_r6_zero(self):
        """A stated-$0 LUMP_SUM subtotal with no classified lines (a recurring
        DIV 07 pattern) paints RED with the R6 zero language, writes 0.00,
        and still never enters the median."""
        docs = [
            _concrete_bidder("Alpha", "100000"),
            _concrete_bidder("Bravo", "110000"),
            _concrete_bidder("Charlie", "120000"),
            _doc("Delta", [
                _div("DIV 03 00 00", "Concrete", Decimal("0")),  # LUMP_SUM $0
                _div("DIV 01 00 00", "General Requirements", Decimal("50000")),
            ], Decimal("50000")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, docs)
            sub_row = _row_of(ws, 2, "CONCRETE SUBTOTAL")
            assert ws.cell(row=sub_row, column=_lev_bench_col(n)).value == 110000.0
            assert ws.cell(row=sub_row, column=_lev_bench_col(n) + 2).value == 3
            cell = ws.cell(row=sub_row,
                           column=_col_of(ws, n, "Delta") + LEVELED_CSUB_OFFSET)
            assert cell.value == 0.0
            assert _hex(cell) == RED
            assert "R6" in cell.comment.text


class TestEnc1VerbatimClassificationDisplay:
    def test_not_applicable_division_displays_verbatim_token(self):
        """ENC-1 (Marvin): a division classified 'Not Applicable' must display
        the verbatim token, not 'By Owner' — the owner does not carry it."""
        docs = [
            _concrete_bidder("Alpha", "100000"),
            _concrete_bidder("Bravo", "110000"),
            _doc("Delta", [
                _div("DIV 03 00 00", "Concrete", None,
                     items=[LineItem(description="Concrete work",
                                     is_by_owner_others=True,
                                     by_others_verbatim="Not Applicable")],
                     cost=CostStructure.ITEMIZED),
                _div("DIV 01 00 00", "General Requirements", Decimal("50000")),
            ], Decimal("50000")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, docs)
            sub_row = _row_of(ws, 2, "CONCRETE SUBTOTAL")
            cell = ws.cell(row=sub_row,
                           column=_col_of(ws, n, "Delta") + LEVELED_CSUB_OFFSET)
            assert cell.value == "Not Applicable"
            assert _hex(cell) != RED  # approved classification, no error
            assert "Approved classification: Not Applicable" in cell.comment.text


class TestEnc2NotComparable:
    def _field(self):
        # A/B/C price the same line; D's is classified Not Comparable at 500k.
        def bidder(name, amount, nc=False):
            item = LineItem(description="Site Fencing",
                            amount=Decimal(amount), is_not_comparable=nc)
            return _doc(name, [_div(
                "DIV 01 00 00", "General Requirements", Decimal(amount),
                items=[item], cost=CostStructure.ITEMIZED,
            )], Decimal(amount))
        return [
            bidder("Alpha", "100000"),
            bidder("Bravo", "110000"),
            bidder("Charlie", "120000"),
            bidder("Delta", "500000", nc=True),
        ]

    def test_line_level_amount_displayed_but_out_of_benchmark(self):
        """ENC-2: the Not-Comparable amount is displayed as submitted with no
        paint and the R7/R8 comment, and the line benchmark is the median of
        the OTHER three (110k — with the 500k in, it would be 115k)."""
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, self._field())
            line_row = _row_of(ws, 2, "Site Fencing")
            bench = ws.cell(row=line_row, column=_lev_bench_col(n)).value
            n_valid = ws.cell(row=line_row, column=_lev_bench_col(n) + 2).value
            assert bench == 110000.0, (
                f"line benchmark must exclude the Not-Comparable 500k, got {bench}"
            )
            assert n_valid == 3
            cell = ws.cell(row=line_row, column=_col_of(ws, n, "Delta"))
            assert cell.value == 500000.0          # displayed as submitted
            assert cell.fill.patternType is None   # no paint
            assert "Not Comparable — excluded from benchmark (R7/R8)" in \
                cell.comment.text

    def test_division_composed_of_nc_lines_out_of_subtotal_benchmark(self):
        """ENC-2 one level up: a division whose lines are ALL Not Comparable
        must not push its (derived) subtotal into the subtotal median."""
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, self._field())
            sub_row = _row_of(ws, 2, "GENERAL REQUIREMENTS SUBTOTAL")
            bench = ws.cell(row=sub_row, column=_lev_bench_col(n)).value
            n_valid = ws.cell(row=sub_row, column=_lev_bench_col(n) + 2).value
            assert bench == 110000.0
            assert n_valid == 3
            csub = ws.cell(row=sub_row,
                           column=_col_of(ws, n, "Delta") + LEVELED_CSUB_OFFSET)
            assert csub.value == 500000.0          # displayed, aqua band kept
            assert _hex(csub) == "A3EAF3"
            assert "Not Comparable — excluded from benchmark (R7/R8)" in \
                csub.comment.text


class TestRem2DerivedSubtotalComment:
    def test_derived_subtotal_carries_disclosure_comment(self):
        """REM-2 (Marvin): a subtotal the engine DERIVED from line items (no
        stated subtotal on the form — a recurring scissor-lift pattern) gets
        an on-cell disclosure, with composition numbers when the bidder's
        stated Construction Cost Subtotal does not carry it."""
        derived = BidDocument(
            contractor_name="Derived Co",
            form_type=FormType.FALKE_STANDARD,
            bid_document_input_type=InputType.DIGITAL_NATIVE,
            divisions=[
                _div("DIV 01 00 00", "General Requirements", Decimal("100000")),
                # DIV 11: no stated subtotal — the engine derives 18,500.
                _div("DIV 11 00 00", "Equipment", None,
                     items=[LineItem(description="Scissor Lift",
                                     amount=Decimal("18500"))],
                     cost=CostStructure.ITEMIZED),
            ],
            footer=BidFooter(
                construction_cost_subtotal=Decimal("100000"),  # excludes 18.5k
                grand_total=Decimal("100000"),
                grand_total_confidence=GrandTotalConfidence.LOW,
            ),
            qualifications=BidQualifications(),
            extraction_confidence=ExtractionConfidence.HIGH,
        )
        docs = [derived, _concrete_bidder("Peer One", "90000")]
        with tempfile.TemporaryDirectory() as tmp:
            ws, n = _write(tmp, docs)
            sub_row = _row_of(ws, 2, "EQUIPMENT SUBTOTAL")
            cell = ws.cell(row=sub_row,
                           column=_col_of(ws, n, "Derived Co") + LEVELED_CSUB_OFFSET)
            assert cell.value == 18500.0
            assert cell.comment is not None, "derived subtotal must be disclosed"
            text = cell.comment.text
            assert "Subtotal DERIVED from priced line items" in text
            assert "$100,000.00" in text and "$118,500.00" in text, (
                "composition numbers (stated CCS vs division-subtotal sum) "
                f"must appear; got: {text}"
            )
            # A STATED subtotal gets no derived-disclosure comment.
            stated_row = _row_of(ws, 2, "GENERAL REQUIREMENTS SUBTOTAL")
            stated_cell = ws.cell(
                row=stated_row,
                column=_col_of(ws, n, "Derived Co") + LEVELED_CSUB_OFFSET)
            assert (stated_cell.comment is None
                    or "DERIVED" not in stated_cell.comment.text)
