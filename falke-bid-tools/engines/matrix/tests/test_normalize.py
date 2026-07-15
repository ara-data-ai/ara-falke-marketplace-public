"""
FALKE Matrix Pipeline — Normalization Rule Engine Unit Tests
============================================================
One test class per priority rule.  All bid fixtures are constructed as real
Pydantic model instances from src.models — no mocking.

Run from the 03_Matrix/ directory:
    python3 -m pytest tests/test_normalize.py -v
"""

from __future__ import annotations

from decimal import Decimal

import pytest

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
from src.firm_config import (
    DEFAULT_KNOWN_FIRMS_PATH,
    Firm,
    KnownFirmsConfig,
    Reclassification,
    load_known_firms,
)
from src.normalize import (
    SUBTOTAL_SUM_STATES,
    _apply_reclass_moves,
    build_normalized_view,
    compute_cross_bid_stats,
    normalize_bid,
)
from src.normalized_models import CellState, NormalizedBid, ReclassRecommendation


def _synthetic_firm_config() -> KnownFirmsConfig:
    """A synthetic, ACTIVE known-firm config (no real names) carrying the two
    reclass quirks the mechanism is tested against. Injected via
    ``normalize_bid(doc, known_firms=...)`` so the tests exercise the identical
    name-agnostic reclass code path a real overlay would drive."""
    return KnownFirmsConfig(firms=[Firm(
        firm_id="acme",
        match=["acme"],
        reclassifications=[
            Reclassification(
                rule_id="ACME_FLOORING_LABOR",
                from_division="DIV 13 00 00", to_division="DIV 09 00 00",
                when_description_contains_all=["flooring", "labor"]),
            Reclassification(
                rule_id="ACME_DUMPSTER",
                from_division="DIV 11 00 00", to_division="DIV 01 00 00",
                when_description_contains_all=["dumpster"]),
        ],
    )])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _minimal_footer(
    construction_cost_subtotal: Decimal | None = None,
    gc_fee: Decimal | None = None,
    grand_total: Decimal | None = None,
    confidence: GrandTotalConfidence = GrandTotalConfidence.LOW,
) -> BidFooter:
    """Build a minimal BidFooter for test fixtures."""
    return BidFooter(
        construction_cost_subtotal=construction_cost_subtotal,
        gc_fee=gc_fee,
        grand_total=grand_total,
        grand_total_confidence=confidence,
    )


def _minimal_doc(
    contractor_name: str = "Test Contractor",
    form_type: FormType = FormType.FALKE_STANDARD,
    divisions: list[DivisionBid] | None = None,
    footer: BidFooter | None = None,
    input_type: InputType = InputType.DIGITAL_NATIVE,
    confidence: ExtractionConfidence = ExtractionConfidence.HIGH,
) -> BidDocument:
    """Build a minimal BidDocument for test fixtures."""
    return BidDocument(
        contractor_name=contractor_name,
        form_type=form_type,
        bid_document_input_type=input_type,
        divisions=divisions or [],
        footer=footer or _minimal_footer(),
        qualifications=BidQualifications(),
        extraction_confidence=confidence,
    )


def _div(
    csi_code: str,
    division_name: str,
    line_items: list[LineItem] | None = None,
    subtotal: Decimal | None = None,
    cost_structure: CostStructure = CostStructure.ITEMIZED,
    classification_source: ClassificationSource = ClassificationSource.CONTRACTOR_NATIVE,
    contractor_native_code: str | None = None,
) -> DivisionBid:
    """Build a minimal DivisionBid."""
    return DivisionBid(
        csi_code=csi_code,
        division_name=division_name,
        line_items=line_items or [],
        division_subtotal=subtotal,
        cost_structure=cost_structure,
        classification_source=classification_source,
        contractor_native_code=contractor_native_code,
    )


# ---------------------------------------------------------------------------
# Rule 1 — Cell-state semantics
# ---------------------------------------------------------------------------

class TestRule1CellStateSemantics:
    """All six CellState values and their board-display strings."""

    def test_amount_state_and_display(self):
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 09 00 00", "Finishes",
                line_items=[LineItem(description="Flooring", amount=Decimal("42000"))],
                subtotal=Decimal("42000"),
            )]
        )
        bid = normalize_bid(doc)
        div = bid.divisions[0]
        cell = div.line_item_cells["Flooring"]
        assert cell.state == CellState.AMOUNT
        assert cell.amount == Decimal("42000")
        assert cell.display == "$42,000"

    def test_explicit_zero_state_and_display(self):
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 01 00 00", "General Requirements",
                line_items=[LineItem(
                    description="Signage",
                    amount=Decimal("0"),
                    is_explicit_zero=True,
                )],
            )]
        )
        bid = normalize_bid(doc)
        cell = bid.divisions[0].line_item_cells["Signage"]
        assert cell.state == CellState.EXPLICIT_ZERO
        assert cell.amount == Decimal("0")
        assert cell.display == "$0"

    def test_null_blank_state_and_display(self):
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 04 00 00", "Masonry",
                line_items=[LineItem(description="Tuckpointing")],  # amount=None
            )]
        )
        bid = normalize_bid(doc)
        cell = bid.divisions[0].line_item_cells["Tuckpointing"]
        assert cell.state == CellState.NULL_BLANK
        assert cell.amount is None
        assert cell.display == "-"

    def test_excluded_state_and_display(self):
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 22 00 00", "Plumbing",
                line_items=[LineItem(
                    description="Domestic Water",
                    is_excluded=True,
                )],
            )]
        )
        bid = normalize_bid(doc)
        cell = bid.divisions[0].line_item_cells["Domestic Water"]
        assert cell.state == CellState.EXCLUDED
        assert cell.display == "EXCL"
        assert bid.explicit_exclusion_count == 1

    def test_by_owner_others_state_and_display(self):
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 12 00 00", "Furnishings",
                line_items=[LineItem(
                    description="Furniture",
                    is_by_owner_others=True,
                    amount=Decimal("25000"),
                )],
            )]
        )
        bid = normalize_bid(doc)
        cell = bid.divisions[0].line_item_cells["Furniture"]
        assert cell.state == CellState.BY_OWNER_OTHERS
        assert cell.display == "BY OTHERS"
        # Amount should be None in the cell (excluded from leveled total)
        assert cell.amount is None

    def test_allowance_state_and_display(self):
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 26 00 00", "Electrical",
                line_items=[LineItem(
                    description="Lighting Package",
                    amount=Decimal("50000"),
                    is_allowance=True,
                    allowance_basis="fixture schedule incomplete",
                )],
            )]
        )
        bid = normalize_bid(doc)
        cell = bid.divisions[0].line_item_cells["Lighting Package"]
        assert cell.state == CellState.ALLOWANCE
        assert cell.amount == Decimal("50000")
        assert cell.display == "ALLOW $50,000"

    def test_by_owner_others_takes_priority_over_allowance(self):
        """BY_OWNER_OTHERS wins even when is_allowance is also set."""
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 11 00 00", "Equipment",
                line_items=[LineItem(
                    description="Commercial Equipment",
                    is_by_owner_others=True,
                    is_allowance=True,
                    amount=Decimal("10000"),
                )],
            )]
        )
        bid = normalize_bid(doc)
        cell = bid.divisions[0].line_item_cells["Commercial Equipment"]
        assert cell.state == CellState.BY_OWNER_OTHERS

    def test_excluded_not_shown_as_zero(self):
        """EXCLUDED must never render as $0."""
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 23 00 00", "HVAC",
                line_items=[LineItem(description="HVAC Equipment", is_excluded=True)],
            )]
        )
        bid = normalize_bid(doc)
        cell = bid.divisions[0].line_item_cells["HVAC Equipment"]
        assert cell.state == CellState.EXCLUDED
        assert cell.display != "$0"
        assert cell.display == "EXCL"


# ---------------------------------------------------------------------------
# leveled_total — BY_OWNER_OTHERS deduction
# ---------------------------------------------------------------------------

class TestLeveledTotal:
    """leveled_total = grand_total − sum(BY_OWNER_OTHERS amounts); allowances are NOT deducted."""

    def test_by_owner_others_deducted_from_leveled_total(self):
        """$200,000 grand_total with $25,000 BY_OWNER_OTHERS → leveled_total = $175,000."""
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 01 00 00", "General Requirements",
                line_items=[
                    LineItem(description="Superintendent", amount=Decimal("175000")),
                    LineItem(
                        description="Fire Watch by Others",
                        amount=Decimal("25000"),
                        is_by_owner_others=True,
                        by_others_verbatim="By Owner",
                        is_explicit_zero=False,
                    ),
                ],
                subtotal=Decimal("200000"),
            )],
            footer=_minimal_footer(grand_total=Decimal("200000"), confidence=GrandTotalConfidence.MEDIUM),
        )
        bid = normalize_bid(doc)
        assert bid.footer.leveled_total == Decimal("175000"), (
            f"Expected leveled_total=175000, got {bid.footer.leveled_total}"
        )

    def test_allowance_not_deducted_from_leveled_total(self):
        """Allowance items are contractual — NOT deducted from leveled_total."""
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 26 00 00", "Electrical",
                line_items=[
                    LineItem(description="Branch Wiring", amount=Decimal("150000")),
                    LineItem(
                        description="Lighting Package",
                        amount=Decimal("50000"),
                        is_allowance=True,
                        allowance_basis="fixture schedule incomplete",
                    ),
                ],
                subtotal=Decimal("200000"),
            )],
            footer=_minimal_footer(grand_total=Decimal("200000"), confidence=GrandTotalConfidence.MEDIUM),
        )
        bid = normalize_bid(doc)
        assert bid.footer.leveled_total == Decimal("200000"), (
            f"Allowance must NOT be deducted from leveled_total; got {bid.footer.leveled_total}"
        )


# ---------------------------------------------------------------------------
# Rule 2 — csi_1995_2digit code-format remapping (signature-detected)
# ---------------------------------------------------------------------------

def _legacy_div(
    code: str,
    name: str,
    line_items: list[LineItem] | None = None,
    subtotal: Decimal | None = None,
    cost_structure: CostStructure = CostStructure.LUMP_SUM,
) -> DivisionBid:
    """A division carrying a bare legacy 2-digit code as its csi_code (the
    signature detector reads csi_code when no contractor_native_code is set)."""
    return DivisionBid(
        csi_code=code,
        division_name=name,
        cost_structure=cost_structure,
        division_subtotal=subtotal,
        classification_source=ClassificationSource.CONTRACTOR_NATIVE,
        contractor_native_code=None,
        line_items=line_items or [],
    )


def _legacy_bid(divisions: list[DivisionBid], name: str = "Generic Legacy Co") -> BidDocument:
    """A bid using the legacy 2-digit format, with NO known-firm name match —
    so the remap is driven purely by code signature (name-independent)."""
    return _minimal_doc(
        contractor_name=name,
        form_type=FormType.CONTRACTOR_OWN,
        divisions=divisions,
        confidence=ExtractionConfidence.MEDIUM,
    )


class TestRule2CodeFormatRemap:
    """Legacy 2-digit codes are detected by signature and losslessly remapped."""

    def test_legacy_code_13_remaps_to_div21(self):
        """Legacy code 13 (Special Construction) → DIV 21 (Fire Suppression), NOT DIV 13."""
        # Need ≥3 legacy codes + a 15/16/17 discriminator to trigger detection.
        doc = _legacy_bid([
            _legacy_div("13", "Special Construction", subtotal=Decimal("35000")),
            _legacy_div("03", "Concrete", subtotal=Decimal("100000")),
            _legacy_div("15", "Mechanical", subtotal=Decimal("80000"),
                        cost_structure=CostStructure.ITEMIZED,
                        line_items=[LineItem(description="HVAC ductwork", amount=Decimal("80000"))]),
        ])
        bid = normalize_bid(doc)
        csi_codes = [d.csi_code for d in bid.divisions]
        assert "DIV 21 00 00" in csi_codes, (
            f"Expected DIV 21 00 00 in {csi_codes}; legacy 13 must NOT become DIV 13"
        )
        assert "DIV 13 00 00" not in csi_codes

    def test_legacy_code_15_lumpsum_emits_split_unmatched(self):
        """Legacy 15 lump-sum (no routable sub-lines) → DIV 22 holding + RED CODE_SPLIT_UNMATCHED."""
        doc = _legacy_bid([
            _legacy_div("15", "Mechanical", subtotal=Decimal("120000")),
            _legacy_div("03", "Concrete", subtotal=Decimal("100000")),
            _legacy_div("16", "Electrical", subtotal=Decimal("90000"),
                        cost_structure=CostStructure.ITEMIZED,
                        line_items=[LineItem(description="Branch wiring", amount=Decimal("90000"))]),
        ])
        bid = normalize_bid(doc)

        warning_text = " ".join(bid.normalization_warnings)
        assert "15" in warning_text and "manual review" in warning_text.lower()

        csi_codes = [d.csi_code for d in bid.divisions]
        assert "DIV 22 00 00" in csi_codes  # first split target (holding)

        flag_types = [f.flag_type for f in bid.summary_flags]
        assert "CODE_SPLIT_UNMATCHED" in flag_types
        flag = next(f for f in bid.summary_flags if f.flag_type == "CODE_SPLIT_UNMATCHED")
        assert flag.severity == "critical"

    def test_legacy_code_16_fire_alarm_splits_to_div26_and_div28(self):
        """Legacy 16 with a Fire Alarm sub-line → split DIV 26 / DIV 28 (fire-alarm first)."""
        doc = _legacy_bid([
            _legacy_div("16", "Electrical", cost_structure=CostStructure.ITEMIZED,
                        line_items=[
                            LineItem(description="Service & Branch Wiring", amount=Decimal("160000")),
                            LineItem(description="Fire Alarm System", amount=Decimal("40000")),
                        ]),
            _legacy_div("03", "Concrete", subtotal=Decimal("100000")),
            _legacy_div("15", "Mechanical", cost_structure=CostStructure.ITEMIZED,
                        line_items=[LineItem(description="HVAC ductwork", amount=Decimal("80000"))]),
        ])
        bid = normalize_bid(doc)
        csi_codes = [d.csi_code for d in bid.divisions]
        assert "DIV 26 00 00" in csi_codes, f"Missing DIV 26 in {csi_codes}"
        assert "DIV 28 00 00" in csi_codes, f"Missing DIV 28 in {csi_codes}"

        div28 = next(d for d in bid.divisions if d.csi_code == "DIV 28 00 00")
        div28_labels = list(div28.line_item_cells.keys())
        assert any("alarm" in lbl.lower() for lbl in div28_labels), (
            f"Fire Alarm sub-line not found in DIV 28 cells: {div28_labels}"
        )

    def test_remap_sets_pipeline_remapped_and_emits_yellow_flag(self):
        """A straight legacy remap → PIPELINE_REMAPPED + YELLOW CODE_FORMAT_REMAPPED."""
        doc = _legacy_bid([
            _legacy_div("07", "Thermal and Moisture Protection", subtotal=Decimal("50000")),
            _legacy_div("03", "Concrete", subtotal=Decimal("100000")),
            _legacy_div("15", "Mechanical", cost_structure=CostStructure.ITEMIZED,
                        line_items=[LineItem(description="HVAC ductwork", amount=Decimal("80000"))]),
        ])
        bid = normalize_bid(doc)
        div07 = next((d for d in bid.divisions if d.csi_code == "DIV 07 00 00"), None)
        assert div07 is not None, "DIV 07 00 00 not found after remap"

        flag_types = [f.flag_type for f in bid.summary_flags]
        assert "CODE_FORMAT_REMAPPED" in flag_types
        remapped = [f for f in bid.summary_flags if f.flag_type == "CODE_FORMAT_REMAPPED"]
        assert all(f.severity == "warning" for f in remapped)

    def test_canonical_contractor_not_remapped(self):
        """A bidder using canonical DIV XX 00 00 codes is accepted as-is (no remap, no flag)."""
        div = DivisionBid(
            csi_code="DIV 07 00 00",
            division_name="Thermal & Moisture Protection",
            cost_structure=CostStructure.LUMP_SUM,
            division_subtotal=Decimal("50000"),
            classification_source=ClassificationSource.CONTRACTOR_NATIVE,
            contractor_native_code=None,
        )
        doc = _minimal_doc(
            contractor_name="Seabridge Construction",
            form_type=FormType.FALKE_STANDARD,
            divisions=[div],
        )
        bid = normalize_bid(doc)
        assert bid.divisions[0].csi_code == "DIV 07 00 00"
        flag_types = [f.flag_type for f in bid.summary_flags]
        assert "CODE_FORMAT_REMAPPED" not in flag_types
        assert "UNRECOGNIZED_CODE_FORMAT" not in flag_types

    def test_csi_remap_is_signature_driven_no_firm_match(self):
        """The csi_1995_2digit remap is signature-detected and name-independent:
        a legacy-format bid whose contractor matches NO firm in the shipped
        library is STILL losslessly remapped. (The remap never depended on a
        firm-name match — so it also holds on any machine without a local
        overlay.)"""
        shipped_cfg = load_known_firms(DEFAULT_KNOWN_FIRMS_PATH)
        # this contractor matches no firm in the shipped library, whatever it carries
        assert shipped_cfg.match("Generic Legacy Co").firm is None
        doc = _legacy_bid([
            _legacy_div("13", "Special Construction", subtotal=Decimal("35000")),
            _legacy_div("03", "Concrete", subtotal=Decimal("100000")),
            _legacy_div("15", "Mechanical", cost_structure=CostStructure.ITEMIZED,
                        line_items=[LineItem(description="HVAC ductwork", amount=Decimal("80000"))]),
        ])
        bid = normalize_bid(doc, known_firms=shipped_cfg)
        csi_codes = [d.csi_code for d in bid.divisions]
        assert "DIV 21 00 00" in csi_codes    # legacy 13 remapped by signature
        assert "DIV 13 00 00" not in csi_codes
        assert "CODE_FORMAT_REMAPPED" in [f.flag_type for f in bid.summary_flags]

    def test_would_be_reclass_firm_degrades_safe_when_unmatched(self):
        """DEGRADED-SAFE: a bid carrying a classic known-firm quirk (a legacy-
        format bid with a DIV 11 dumpster line — the 'move dumpster to DIV 01'
        move) from a contractor that matches NO firm in the loaded library is
        mirrored AS-SUBMITTED: NO firm-specific reclassification is applied (no
        destructive dollar move), because no firm matches. The name-independent
        csi_1995_2digit signature remap STILL fires. This is exactly the
        public-scaffold (no overlay) behavior for a would-be-known-firm bid,
        and equally the unknown-firm behavior on the private build — leveled
        correctly, dumpster not silently moved."""
        library = load_known_firms(DEFAULT_KNOWN_FIRMS_PATH)
        # This contractor matches no firm in the shipped library on EITHER
        # head (scaffold: match set empty; private: no term is a substring).
        assert library.match("Some Restoration Contractor").firm is None
        doc = _legacy_bid([
            _legacy_div("13", "Special Construction", subtotal=Decimal("35000")),
            _legacy_div("15", "Mechanical", cost_structure=CostStructure.ITEMIZED,
                        line_items=[LineItem(description="HVAC ductwork", amount=Decimal("80000"))]),
            _legacy_div("11", "Equipment", cost_structure=CostStructure.ITEMIZED,
                        line_items=[LineItem(description="Dumpsters", amount=Decimal("6500"))]),
        ])
        bid = normalize_bid(doc, known_firms=library)
        # Degraded-safe: the dumpster is NOT reclassified — no move recommendation.
        assert bid.reclass_recommendations == []
        # Signature remap still fires name-independently (legacy 13 -> DIV 21).
        csi_codes = [d.csi_code for d in bid.divisions]
        assert "DIV 21 00 00" in csi_codes
        assert "CODE_FORMAT_REMAPPED" in [f.flag_type for f in bid.summary_flags]


# ---------------------------------------------------------------------------
# Rule 3 — known-firm misclassification reclassifications
# ---------------------------------------------------------------------------

class TestRule3FirmReclassifications:
    """Option C: known-firm reclass is ANNOTATE-ONLY on the mirror (dollars stay
    as submitted + a recommendation is recorded); the move is applied only in the
    leveled view via build_normalized_view. Driven by a SYNTHETIC injected firm
    config (no real names); the code path is name-agnostic."""

    CFG = _synthetic_firm_config()

    def _firm_doc(self, divisions: list[DivisionBid]) -> BidDocument:
        return _minimal_doc(
            contractor_name="Acme Restoration LLC",
            form_type=FormType.FALKE_STANDARD,
            divisions=divisions,
        )

    def test_flooring_labor_stays_on_mirror_moves_in_leveled(self):
        """A matched firm's 'Flooring (Labor)' stays in DIV 13 on the mirror, moves to DIV 09 leveled."""
        divisions = [
            _div(
                "DIV 13 00 00", "Special Construction",
                line_items=[
                    LineItem(description="Flooring (Labor)", amount=Decimal("18000")),
                    LineItem(description="Special Construction (other)", amount=Decimal("5000")),
                ],
                subtotal=Decimal("23000"),
            ),
            _div(
                "DIV 09 00 00", "Finishes",
                line_items=[
                    LineItem(description="Tile", amount=Decimal("30000")),
                ],
                subtotal=Decimal("30000"),
            ),
        ]
        doc = self._firm_doc(divisions)
        bid = normalize_bid(doc, known_firms=self.CFG)

        # MIRROR: Flooring (Labor) stays in DIV 13 (as-submitted, foots to bid).
        div13 = next(d for d in bid.divisions if d.csi_code == "DIV 13 00 00")
        assert any("flooring" in lbl.lower() and "labor" in lbl.lower()
                   for lbl in div13.line_item_cells)
        assert div13.subtotal_cell.amount == Decimal("23000")
        # Not present in DIV 09 on the mirror.
        div09 = next((d for d in bid.divisions if d.csi_code == "DIV 09 00 00"), None)
        assert div09 is not None
        assert not any("flooring" in lbl.lower() and "labor" in lbl.lower()
                       for lbl in div09.line_item_cells)

        # A recommendation is recorded (DIV 13 → DIV 09).
        recs = [r for r in bid.reclass_recommendations
                if "flooring" in r.line_item_desc.lower()]
        assert recs and recs[0].from_division == "DIV 13 00 00"
        assert recs[0].to_division == "DIV 09 00 00"

        # Warning reflects recommendation framing.
        warnings_joined = " ".join(bid.normalization_warnings).lower()
        assert "flooring" in warnings_joined and "div 13" in warnings_joined

        # LEVELED view: the move IS applied (DIV 09 gains flooring; DIV 13 loses it).
        leveled = build_normalized_view(bid, doc)
        lev09 = next(d for d in leveled.divisions if d.csi_code == "DIV 09 00 00")
        lev13 = next(d for d in leveled.divisions if d.csi_code == "DIV 13 00 00")
        assert any("flooring" in lbl.lower() and "labor" in lbl.lower()
                   for lbl in lev09.line_item_cells)
        assert not any("flooring" in lbl.lower() and "labor" in lbl.lower()
                       for lbl in lev13.line_item_cells)
        assert lev13.subtotal_cell.amount == Decimal("5000")

    def test_dumpsters_stay_on_mirror_move_in_leveled(self):
        """A matched firm's 'Dumpster' stays in DIV 11 on the mirror, moves to DIV 01 leveled."""
        divisions = [
            _div(
                "DIV 11 00 00", "Equipment",
                line_items=[
                    LineItem(description="Dumpsters", amount=Decimal("6500")),
                    LineItem(description="Commercial Equipment", amount=Decimal("15000")),
                ],
                subtotal=Decimal("21500"),
            ),
            _div(
                "DIV 01 00 00", "General Requirements",
                line_items=[
                    LineItem(description="Project Management", amount=Decimal("40000")),
                ],
                subtotal=Decimal("40000"),
            ),
        ]
        doc = self._firm_doc(divisions)
        bid = normalize_bid(doc, known_firms=self.CFG)

        # MIRROR: Dumpsters stay in DIV 11.
        div11 = next(d for d in bid.divisions if d.csi_code == "DIV 11 00 00")
        assert any("dumpster" in lbl.lower() for lbl in div11.line_item_cells)
        assert div11.subtotal_cell.amount == Decimal("21500")
        div01 = next(d for d in bid.divisions if d.csi_code == "DIV 01 00 00")
        assert not any("dumpster" in lbl.lower() for lbl in div01.line_item_cells)

        recs = [r for r in bid.reclass_recommendations
                if "dumpster" in r.line_item_desc.lower()]
        assert recs and recs[0].from_division == "DIV 11 00 00"
        assert recs[0].to_division == "DIV 01 00 00"

        warnings_joined = " ".join(bid.normalization_warnings).lower()
        assert "dumpster" in warnings_joined and "div 11" in warnings_joined

        # LEVELED: Dumpsters land in DIV 01; DIV 11 loses them.
        leveled = build_normalized_view(bid, doc)
        lev01 = next(d for d in leveled.divisions if d.csi_code == "DIV 01 00 00")
        lev11 = next(d for d in leveled.divisions if d.csi_code == "DIV 11 00 00")
        assert any("dumpster" in lbl.lower() for lbl in lev01.line_item_cells)
        assert not any("dumpster" in lbl.lower() for lbl in lev11.line_item_cells)
        assert lev11.subtotal_cell.amount == Decimal("15000")

    def test_non_matching_contractor_not_reclassified(self):
        """A contractor that does NOT match the firm config's terms is NOT
        reclassified (even with the firm config present)."""
        div = _div(
            "DIV 13 00 00", "Special Construction",
            line_items=[LineItem(description="Flooring (Labor)", amount=Decimal("18000"))],
            subtotal=Decimal("18000"),
        )
        doc = _minimal_doc(
            contractor_name="Seabridge Construction",
            divisions=[div],
        )
        bid = normalize_bid(doc, known_firms=self.CFG)

        # Flooring (Labor) stays in DIV 13, no recommendation recorded.
        div13 = next((d for d in bid.divisions if d.csi_code == "DIV 13 00 00"), None)
        assert div13 is not None
        assert "Flooring (Labor)" in div13.line_item_cells
        assert bid.reclass_recommendations == []

    def test_firm_description_matching_is_case_insensitive(self):
        """A matched firm's rule keyword matching is case-insensitive — recommendation recorded."""
        div = _div(
            "DIV 13 00 00", "Special Construction",
            line_items=[LineItem(description="FLOORING (LABOR)", amount=Decimal("18000"))],
            subtotal=Decimal("18000"),
        )
        doc = _minimal_doc(
            contractor_name="ACME RESTORATION",
            divisions=[div],
        )
        bid = normalize_bid(doc, known_firms=self.CFG)
        # Mirror keeps it in place; recommendation targets DIV 09.
        recs = [r for r in bid.reclass_recommendations]
        assert recs and recs[0].to_division == "DIV 09 00 00"
        # Leveled view places it in DIV 09.
        leveled = build_normalized_view(bid, doc)
        lev09 = next((d for d in leveled.divisions if d.csi_code == "DIV 09 00 00"), None)
        assert lev09 is not None


# ---------------------------------------------------------------------------
# GOLD-DEV-10 — reclass touched-set semantics + the shared subtotal state table
# ---------------------------------------------------------------------------

class TestReclassTouchedSetSemantics:
    """GOLD-DEV-10 fix (Marvin ruling, 2026-07-15): _apply_reclass_moves
    rebuilds ONLY the touched from/to divisions; every other division passes
    through byte-identical (stated subtotal, cost structure, REM-1 $0s
    intact). Shapes (a)/(b)/(c) from the finding are pinned here so the
    golden set does not have to carry all three."""

    CFG = _synthetic_firm_config()

    def _reclass_doc(self, extra_divisions: list[DivisionBid]) -> BidDocument:
        """ACME doc with the dumpster reclass armed (DIV 11 → DIV 01) plus
        untouched divisions carrying the GOLD-DEV-10 danger shapes."""
        return _minimal_doc(
            contractor_name="Acme Restoration LLC",
            divisions=[
                _div("DIV 01 00 00", "General Requirements",
                     line_items=[LineItem(description="Project Management",
                                          amount=Decimal("40000"))],
                     subtotal=Decimal("40000")),
                _div("DIV 11 00 00", "Equipment",
                     line_items=[LineItem(description="Dumpsters",
                                          amount=Decimal("6500"))],
                     subtotal=Decimal("6500")),
                *extra_divisions,
            ],
        )

    def test_shape_a_untouched_stated_subtotal_and_r20_survive(self):
        """Shape (a): an untouched ITEMIZED division's STATED subtotal (508,000
        over lines summing 500,000) survives the leveled view — the R20 math
        error must NOT self-heal for a reclass-matched bidder."""
        doc = self._reclass_doc([
            _div("DIV 03 00 00", "Concrete",
                 line_items=[LineItem(description="Garage slab repairs",
                                      amount=Decimal("500000"))],
                 subtotal=Decimal("508000")),
        ])
        bid = normalize_bid(doc, known_firms=self.CFG)
        leveled = build_normalized_view(bid, doc)
        div03 = next(d for d in leveled.divisions if d.csi_code == "DIV 03 00 00")
        assert div03.subtotal_cell.amount == Decimal("508000")
        assert "ARITHMETIC_DISCREPANCY" in div03.subtotal_cell.flags

    def test_shape_b_untouched_allowance_division_not_understated(self):
        """Shape (b): an untouched allowance-bearing ITEMIZED division keeps
        its stated 400,000 (360,000 + 40,000 ALLOW) — no understated 360,000,
        no false discrepancy."""
        doc = self._reclass_doc([
            _div("DIV 07 00 00", "Thermal & Moisture Protection",
                 line_items=[
                     LineItem(description="Deck waterproofing",
                              amount=Decimal("360000")),
                     LineItem(description="Coating material allowance",
                              amount=Decimal("40000"), is_allowance=True),
                 ],
                 subtotal=Decimal("400000")),
        ])
        bid = normalize_bid(doc, known_firms=self.CFG)
        leveled = build_normalized_view(bid, doc)
        div07 = next(d for d in leveled.divisions if d.csi_code == "DIV 07 00 00")
        assert div07.subtotal_cell.amount == Decimal("400000")
        assert "ARITHMETIC_DISCREPANCY" not in div07.subtotal_cell.flags

    def test_shape_c_untouched_stated_zero_stays_explicit_zero(self):
        """Shape (c): an untouched stated-$0 division (REM-1 EXPLICIT_ZERO
        over Excluded lines) keeps its verified-$0 story on the leveled view —
        never NULL_BLANK / scope-gap."""
        doc = self._reclass_doc([
            _div("DIV 09 00 00", "Finishes",
                 line_items=[
                     LineItem(description="Corridor painting", is_excluded=True),
                     LineItem(description="Lobby flooring", is_excluded=True),
                 ],
                 subtotal=Decimal("0")),
        ])
        bid = normalize_bid(doc, known_firms=self.CFG)
        leveled = build_normalized_view(bid, doc)
        div09 = next(d for d in leveled.divisions if d.csi_code == "DIV 09 00 00")
        assert div09.subtotal_cell.state == CellState.EXPLICIT_ZERO
        assert div09.subtotal_cell.amount == Decimal("0")

    def test_touched_pair_rederives_with_full_inclusion_table(self):
        """The touched TO division re-derives from its post-move lines using
        the SAME inclusion table as _resolve_subtotal_cell: allowance and
        not-comparable amounts IN, by-owner/excluded OUT."""
        divisions = [
            _div("DIV 11 00 00", "Equipment",
                 line_items=[LineItem(description="Dumpsters",
                                      amount=Decimal("18000"))],
                 subtotal=Decimal("18000")),
            _div("DIV 01 00 00", "General Requirements",
                 line_items=[
                     LineItem(description="Supervision", amount=Decimal("30000")),
                     LineItem(description="Temp facilities allowance",
                              amount=Decimal("5000"), is_allowance=True),
                     LineItem(description="Owner option package",
                              amount=Decimal("2000"), is_not_comparable=True),
                     LineItem(description="Owner-carried permits",
                              is_by_owner_others=True),
                     LineItem(description="Declined scope", is_excluded=True),
                 ],
                 subtotal=Decimal("37000")),
        ]
        recs = [ReclassRecommendation(
            line_item_desc="Dumpsters",
            from_division="DIV 11 00 00", to_division="DIV 01 00 00",
            to_division_name="General Requirements",
            amount=Decimal("18000"), rule_id="ACME_DUMPSTER",
        )]
        moved = _apply_reclass_moves(divisions, recs)
        by_code = {d.csi_code: d for d in moved}
        # TO: 30,000 + 5,000 ALLOW + 2,000 NC + 18,000 moved = 55,000.
        assert by_code["DIV 01 00 00"].division_subtotal == Decimal("55000")
        # FROM: fully vacated → None (NULL_BLANK downstream).
        assert by_code["DIV 11 00 00"].division_subtotal is None

    def test_untouched_division_passes_through_byte_identical(self):
        """Untouched divisions are the ORIGINAL DivisionBid objects — not
        copies with re-derived fields."""
        untouched = _div("DIV 03 00 00", "Concrete",
                         line_items=[LineItem(description="Slab repairs",
                                              amount=Decimal("500000"))],
                         subtotal=Decimal("508000"))
        divisions = [
            _div("DIV 11 00 00", "Equipment",
                 line_items=[LineItem(description="Dumpsters",
                                      amount=Decimal("6500"))],
                 subtotal=Decimal("6500")),
            untouched,
        ]
        recs = [ReclassRecommendation(
            line_item_desc="Dumpsters",
            from_division="DIV 11 00 00", to_division="DIV 01 00 00",
            to_division_name="General Requirements",
            amount=Decimal("6500"), rule_id="ACME_DUMPSTER",
        )]
        moved = _apply_reclass_moves(divisions, recs)
        by_code = {d.csi_code: d for d in moved}
        assert by_code["DIV 03 00 00"] is untouched
        assert by_code["DIV 03 00 00"].division_subtotal == Decimal("508000")

    def test_vacated_by_reclass_property_carried_on_view(self):
        """Floyd W2-4: the vacated set is derived ONCE on the normalized view
        (from → to), for both audit.py and write_matrix.py to consume."""
        doc = self._reclass_doc([])
        bid = normalize_bid(doc, known_firms=self.CFG)
        assert bid.vacated_by_reclass == {"DIV 11 00 00": "DIV 01 00 00"}

    def test_from_division_net_negative_remainder_survives(self):
        """C-W4-3 regression (Floyd probe B3, W-D ruling 5.1): a from-division
        retaining a NET-NEGATIVE remainder (a credit line) after the move
        keeps its negative subtotal — the sign-aware gate (`!= 0`) must never
        silently blank a credit (R33)."""
        divisions = [
            _div("DIV 11 00 00", "Equipment",
                 line_items=[
                     LineItem(description="Dumpsters", amount=Decimal("20000")),
                     LineItem(description="Equipment credit",
                              amount=Decimal("-5000")),
                 ],
                 subtotal=Decimal("15000")),
            _div("DIV 01 00 00", "General Requirements",
                 line_items=[LineItem(description="Supervision",
                                      amount=Decimal("300000"))],
                 subtotal=Decimal("300000")),
        ]
        recs = [ReclassRecommendation(
            line_item_desc="Dumpsters",
            from_division="DIV 11 00 00", to_division="DIV 01 00 00",
            to_division_name="General Requirements",
            amount=Decimal("20000"), rule_id="ACME_DUMPSTER",
        )]
        moved = _apply_reclass_moves(divisions, recs)
        by_code = {d.csi_code: d for d in moved}
        # FROM keeps the −5,000 credit (never blanked to None).
        assert by_code["DIV 11 00 00"].division_subtotal == Decimal("-5000")
        # TO gains the moved 20,000; division-sum is conserved (315,000).
        assert by_code["DIV 01 00 00"].division_subtotal == Decimal("320000")
        total = sum(d.division_subtotal for d in moved
                    if d.division_subtotal is not None)
        assert total == Decimal("315000")

    def test_derived_subtotal_sign_aware_gate(self):
        """W-D ruling 5.1 on _resolve_subtotal_cell: derived ≠ 0 → AMOUNT
        (negative included); derived 0 → NULL_BLANK (REM-1 unchanged)."""
        neg_doc = _minimal_doc(
            contractor_name="Credit Deriver",
            divisions=[_div("DIV 03 00 00", "Concrete",
                            line_items=[
                                LineItem(description="Base work",
                                         amount=Decimal("10000")),
                                LineItem(description="Scope credit",
                                         amount=Decimal("-15000")),
                            ],
                            subtotal=None)],
        )
        bid = normalize_bid(neg_doc)
        cell = bid.divisions[0].subtotal_cell
        assert cell.state == CellState.AMOUNT
        assert cell.amount == Decimal("-5000")
        assert "SUBTOTAL_DERIVED" in cell.flags

        zero_doc = _minimal_doc(
            contractor_name="Zero Deriver",
            divisions=[_div("DIV 03 00 00", "Concrete",
                            line_items=[
                                LineItem(description="Base work",
                                         amount=Decimal("10000")),
                                LineItem(description="Full credit",
                                         amount=Decimal("-10000")),
                            ],
                            subtotal=None)],
        )
        bid = normalize_bid(zero_doc)
        assert bid.divisions[0].subtotal_cell.state == CellState.NULL_BLANK


class TestSubtotalStateContract:
    """Contract test binding every consumer of the state→amount inclusion
    table (Marvin GOLD-DEV-10 ruling (2); Floyd W2-3). Sites:
      1. normalize._resolve_subtotal_cell        (shared constant)
      2. normalize._apply_reclass_moves          (shared constant)
      3. reconcile._AMOUNT_BEARING_STATES        (deliberately INDEPENDENT
         copy — Floyd C-2 reviewer independence; value-equivalence asserted
         here, the implementation is NOT collapsed onto the constant)
    """

    def test_shared_table_is_the_ruling_table(self):
        assert set(SUBTOTAL_SUM_STATES) == {
            CellState.AMOUNT,
            CellState.EXPLICIT_ZERO,
            CellState.ALLOWANCE,
            CellState.NOT_COMPARABLE,
        }

    def test_resolve_subtotal_cell_follows_the_table(self):
        """Derived (no stated subtotal) ITEMIZED division with one line per
        state sums exactly the table states: 100 + 0 + 40 + 25 = 165."""
        doc = _minimal_doc(divisions=[
            _div("DIV 03 00 00", "Concrete", line_items=[
                LineItem(description="Amount line", amount=Decimal("100")),
                LineItem(description="Zero line", amount=Decimal("0"),
                         is_explicit_zero=True),
                LineItem(description="Allowance line", amount=Decimal("40"),
                         is_allowance=True),
                LineItem(description="NC line", amount=Decimal("25"),
                         is_not_comparable=True),
                LineItem(description="By-owner line", is_by_owner_others=True),
                LineItem(description="Excluded line", is_excluded=True),
            ]),
        ])
        bid = normalize_bid(doc)
        div = next(d for d in bid.divisions if d.csi_code == "DIV 03 00 00")
        assert div.subtotal_cell.amount == Decimal("165")

    def test_reconcile_fourth_site_value_equivalence(self):
        """Floyd W2-3: reconcile's independent table is VALUE-equivalent —
        identical membership on every state a subtotal cell can carry.
        NOT_COMPARABLE is line-level only: _resolve_subtotal_cell never emits
        it on a subtotal cell (an NC-composed subtotal resolves AMOUNT; the
        NC fencing lives in div_status/benchmarks, ENC-2)."""
        from src.reconcile import _AMOUNT_BEARING_STATES, _expected_subtotal
        assert set(_AMOUNT_BEARING_STATES) == (
            set(SUBTOTAL_SUM_STATES) - {CellState.NOT_COMPARABLE}
        )
        # Behavioral proof of the NOT_COMPARABLE carve-out: an all-NC division
        # with a stated subtotal resolves to state AMOUNT, and reconcile's
        # independent scalar agrees with the written amount for it.
        doc = _minimal_doc(divisions=[
            _div("DIV 09 00 00", "Finishes",
                 line_items=[LineItem(description="Owner option package",
                                      amount=Decimal("145000"),
                                      is_not_comparable=True)],
                 subtotal=Decimal("145000")),
        ])
        bid = normalize_bid(doc)
        cell = next(d for d in bid.divisions
                    if d.csi_code == "DIV 09 00 00").subtotal_cell
        assert cell.state == CellState.AMOUNT
        assert _expected_subtotal(cell.state, cell.amount) == Decimal("145000")
        # And the non-bearing states contribute 0 in reconcile's view.
        assert _expected_subtotal(CellState.NULL_BLANK, None) == Decimal("0")
        assert _expected_subtotal(CellState.EXCLUDED, None) == Decimal("0")
        assert _expected_subtotal(
            CellState.BY_OWNER_OTHERS, None) == Decimal("0")


# ---------------------------------------------------------------------------
# Rule 4 — Allowance treatment
# ---------------------------------------------------------------------------

class TestRule4AllowanceTreatment:
    """Allowance items are flagged, totalled, and excluded from hard-cost comparison."""

    def test_allowance_cell_state(self):
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 26 00 00", "Electrical",
                line_items=[
                    LineItem(description="Lighting Package", amount=Decimal("50000"), is_allowance=True),
                    LineItem(description="Branch Wiring", amount=Decimal("80000")),
                ],
            )]
        )
        bid = normalize_bid(doc)
        cell = bid.divisions[0].line_item_cells["Lighting Package"]
        assert cell.state == CellState.ALLOWANCE
        assert cell.display == "ALLOW $50,000"

    def test_total_allowance_value_summed(self):
        doc = _minimal_doc(
            divisions=[
                _div(
                    "DIV 26 00 00", "Electrical",
                    line_items=[
                        LineItem(description="Lighting Package", amount=Decimal("50000"), is_allowance=True),
                    ],
                ),
                _div(
                    "DIV 22 00 00", "Plumbing",
                    line_items=[
                        LineItem(description="Fixture Allowance", amount=Decimal("15000"), is_allowance=True),
                        LineItem(description="Pipe", amount=Decimal("30000")),
                    ],
                ),
            ]
        )
        bid = normalize_bid(doc)
        assert bid.total_allowance_value == Decimal("65000")
        assert bid.allowance_count == 2

    def test_allowance_summary_flag_emitted(self):
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 26 00 00", "Electrical",
                line_items=[
                    LineItem(description="Lighting Package", amount=Decimal("50000"), is_allowance=True),
                ],
            )]
        )
        bid = normalize_bid(doc)
        flag_types = [f.flag_type for f in bid.summary_flags]
        assert "ALLOWANCE_PRESENT" in flag_types

        flag = next(f for f in bid.summary_flags if f.flag_type == "ALLOWANCE_PRESENT")
        assert "50,000" in flag.message
        assert flag.severity == "warning"

    def test_allowance_not_in_hard_cost_leveled_total(self):
        """
        Allowance amounts are included in division subtotals (in contract) but
        should be separable from hard costs.  The normalized bid tracks allowance
        value separately; the leveled_total in footer is built from grand_total
        minus BY_OWNER_OTHERS (not allowances), consistent with how the Excel
        writer handles it.
        """
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 26 00 00", "Electrical",
                line_items=[
                    LineItem(description="Lighting Package", amount=Decimal("50000"), is_allowance=True),
                    LineItem(description="Branch Wiring", amount=Decimal("100000")),
                ],
                cost_structure=CostStructure.ITEMIZED,
            )],
            footer=_minimal_footer(
                construction_cost_subtotal=Decimal("150000"),
                grand_total=Decimal("150000"),
                confidence=GrandTotalConfidence.LOW,
            ),
        )
        bid = normalize_bid(doc)
        # total_allowance_value tracks the allowance separately
        assert bid.total_allowance_value == Decimal("50000")
        # Hard costs can be derived: grand_total - total_allowance_value = $100,000
        hard_cost = bid.footer.grand_total.amount - bid.total_allowance_value
        assert hard_cost == Decimal("100000")

    def test_no_allowance_flag_when_none_present(self):
        doc = _minimal_doc(
            divisions=[_div(
                "DIV 09 00 00", "Finishes",
                line_items=[LineItem(description="Tile", amount=Decimal("30000"))],
            )]
        )
        bid = normalize_bid(doc)
        flag_types = [f.flag_type for f in bid.summary_flags]
        assert "ALLOWANCE_PRESENT" not in flag_types
        assert bid.allowance_count == 0
        assert bid.total_allowance_value == Decimal("0")


# ---------------------------------------------------------------------------
# Rule 5 — GC Fee % normalization
# ---------------------------------------------------------------------------

class TestRule5GcFeePct:
    """GC fee % is computed per-bid and cross-bid outliers are flagged."""

    def _make_bid_with_gc_fee(
        self,
        name: str,
        subtotal: Decimal,
        gc_fee: Decimal | None,
        grand_total: Decimal | None = None,
    ) -> NormalizedBid:
        footer = _minimal_footer(
            construction_cost_subtotal=subtotal,
            gc_fee=gc_fee,
            grand_total=grand_total or (subtotal + gc_fee if gc_fee else subtotal),
            confidence=GrandTotalConfidence.LOW,
        )
        doc = _minimal_doc(contractor_name=name, footer=footer)
        return normalize_bid(doc)

    def test_gc_fee_pct_computed_per_bid(self):
        bid = self._make_bid_with_gc_fee(
            "Contractor A",
            subtotal=Decimal("1000000"),
            gc_fee=Decimal("100000"),
        )
        assert bid.footer.gc_fee_pct is not None
        assert bid.footer.gc_fee_pct == Decimal("10.00")

    def test_gc_fee_pct_none_when_gc_fee_missing(self):
        bid = self._make_bid_with_gc_fee(
            "Contractor B",
            subtotal=Decimal("1000000"),
            gc_fee=None,
        )
        assert bid.footer.gc_fee_pct is None

    def test_gc_fee_outlier_high_flagged_cross_bid(self):
        """
        A contractor with GC fee % more than 2 std devs above the mean
        should receive a GC_FEE_OUTLIER flag after compute_cross_bid_stats().

        Fixture: 5 bids at 8–12% GC fee and 1 outlier at 30%.
        With that distribution: mean ≈ 13.3%, stddev ≈ 8.3%, outlier deviation ≈ 2.01 std devs.
        The 3-identical-plus-outlier pattern always yields only 1.5 std devs (not enough),
        so we need variance in the baseline to get a meaningful outlier detection.
        """
        bid_a = self._make_bid_with_gc_fee("A", Decimal("1000000"), Decimal("80000"))   # 8%
        bid_b = self._make_bid_with_gc_fee("B", Decimal("1000000"), Decimal("90000"))   # 9%
        bid_c = self._make_bid_with_gc_fee("C", Decimal("1000000"), Decimal("100000"))  # 10%
        bid_d = self._make_bid_with_gc_fee("D", Decimal("1000000"), Decimal("110000"))  # 11%
        bid_e = self._make_bid_with_gc_fee("E", Decimal("1000000"), Decimal("120000"))  # 12%
        bid_f = self._make_bid_with_gc_fee("F", Decimal("1000000"), Decimal("300000"))  # 30% — outlier

        bids = [bid_a, bid_b, bid_c, bid_d, bid_e, bid_f]
        updated = compute_cross_bid_stats(bids)

        bid_f_updated = next(b for b in updated if b.contractor_name == "F")
        flag_types = [f.flag_type for f in bid_f_updated.summary_flags]
        assert "GC_FEE_OUTLIER" in flag_types, (
            f"Expected GC_FEE_OUTLIER for 30% fee bid. Flags: {bid_f_updated.summary_flags}"
        )

        flag = next(f for f in bid_f_updated.summary_flags if f.flag_type == "GC_FEE_OUTLIER")
        assert "above" in flag.message.lower()

    def test_gc_fee_missing_flagged_cross_bid(self):
        """A contractor with no separate GC fee gets a GC_FEE_MISSING flag."""
        bid_a = self._make_bid_with_gc_fee("A", Decimal("1000000"), Decimal("100000"))
        bid_b = self._make_bid_with_gc_fee("B", Decimal("1000000"), Decimal("100000"))
        bid_c = self._make_bid_with_gc_fee("C", Decimal("1000000"), None)  # no gc_fee

        bids = [bid_a, bid_b, bid_c]
        updated = compute_cross_bid_stats(bids)

        bid_c_updated = next(b for b in updated if b.contractor_name == "C")
        flag_types = [f.flag_type for f in bid_c_updated.summary_flags]
        assert "GC_FEE_MISSING" in flag_types

        flag = next(f for f in bid_c_updated.summary_flags if f.flag_type == "GC_FEE_MISSING")
        assert "not separately stated" in flag.message.lower()

    def test_normal_gc_fee_not_flagged(self):
        """Bids with similar GC fee % should not receive GC_FEE_OUTLIER flags."""
        bid_a = self._make_bid_with_gc_fee("A", Decimal("1000000"), Decimal("100000"))  # 10%
        bid_b = self._make_bid_with_gc_fee("B", Decimal("1000000"), Decimal("105000"))  # 10.5%
        bid_c = self._make_bid_with_gc_fee("C", Decimal("1000000"), Decimal("95000"))   # 9.5%

        bids = [bid_a, bid_b, bid_c]
        updated = compute_cross_bid_stats(bids)

        for bid in updated:
            flag_types = [f.flag_type for f in bid.summary_flags]
            assert "GC_FEE_OUTLIER" not in flag_types, (
                f"{bid.contractor_name} has unexpected GC_FEE_OUTLIER: {bid.summary_flags}"
            )


# ---------------------------------------------------------------------------
# Rule 6 — Image-scan confidence validation
# ---------------------------------------------------------------------------

class TestRule6ImageScanConfidence:
    """IMAGE_SCAN documents with non-LOW confidence receive a warning."""

    def test_image_scan_high_confidence_emits_warning(self):
        doc = _minimal_doc(
            input_type=InputType.IMAGE_SCAN,
            confidence=ExtractionConfidence.HIGH,
        )
        bid = normalize_bid(doc)
        warnings_joined = " ".join(bid.normalization_warnings)
        assert "image_scan" in warnings_joined.lower() or "IMAGE_SCAN" in warnings_joined, (
            f"Expected IMAGE_SCAN confidence warning. Got: {bid.normalization_warnings}"
        )
        assert "HIGH" in warnings_joined

    def test_image_scan_medium_confidence_emits_warning(self):
        doc = _minimal_doc(
            input_type=InputType.IMAGE_SCAN,
            confidence=ExtractionConfidence.MEDIUM,
        )
        bid = normalize_bid(doc)
        warnings_joined = " ".join(bid.normalization_warnings)
        assert "IMAGE_SCAN" in warnings_joined
        assert "MEDIUM" in warnings_joined

    def test_image_scan_low_confidence_no_warning(self):
        """LOW confidence on an IMAGE_SCAN document is correct — no warning needed."""
        doc = _minimal_doc(
            input_type=InputType.IMAGE_SCAN,
            confidence=ExtractionConfidence.LOW,
        )
        bid = normalize_bid(doc)
        # No image-scan-specific warning (only extraction warnings would be pass-through)
        image_warnings = [
            w for w in bid.normalization_warnings
            if "IMAGE_SCAN" in w and "consider upgrading" in w.lower()
        ]
        assert len(image_warnings) == 0

    def test_digital_native_no_image_warning(self):
        """DIGITAL_NATIVE documents never receive the IMAGE_SCAN confidence warning."""
        doc = _minimal_doc(
            input_type=InputType.DIGITAL_NATIVE,
            confidence=ExtractionConfidence.HIGH,
        )
        bid = normalize_bid(doc)
        image_warnings = [w for w in bid.normalization_warnings if "IMAGE_SCAN" in w]
        assert len(image_warnings) == 0


# ---------------------------------------------------------------------------
# Integration: import-smoke test
# ---------------------------------------------------------------------------

class TestImportSmoke:
    """Verify the module graph is importable and the entry points exist."""

    def test_normalize_bid_importable_and_callable(self):
        from src.normalize import normalize_bid as nb
        from src.normalized_models import NormalizedBid as NB
        assert callable(nb)
        assert NB is not None

    def test_compute_cross_bid_stats_importable(self):
        from src.normalize import compute_cross_bid_stats as ccbs
        assert callable(ccbs)

    def test_canon_importable_standalone(self):
        from src.canon import (
            CANONICAL_DIVISIONS,
            CSI_1995_2DIGIT_MAP,
            SCOPE_GAP_MEDIAN_THRESHOLD,
        )
        assert len(CANONICAL_DIVISIONS) == 20
        assert isinstance(CSI_1995_2DIGIT_MAP, dict)
        # Firm reclass rules no longer live in canon — they're in known_firms.yaml.
        from src.canon import detect_csi_1995_2digit
        assert callable(detect_csi_1995_2digit)
        assert SCOPE_GAP_MEDIAN_THRESHOLD == Decimal("20000")

    def test_no_circular_imports(self):
        """Importing all modules in order must not raise ImportError."""
        import importlib
        for mod in ["src.canon", "src.models", "src.normalized_models", "src.normalize"]:
            importlib.import_module(mod)
