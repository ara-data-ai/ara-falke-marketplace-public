"""Regenerate the create-matrix compatibility fixtures in this directory.

The fixtures are REAL output of the falke-bid-tools create-matrix engine
(the canonical producer of bid-comparison matrices), written by its own
writer + normalize pipeline from FULLY SYNTHETIC bid data — fictional firms,
fictional dollars, no client data (Floyd fixture rule). They back
tests/test_create_matrix_compat.py, which proves the scorecard parser reads
producer-generated workbooks at a variable number of bidders.

Run (needs the sibling matrix engine at engines/matrix in this plugin):
    /usr/bin/python3 tests/fixtures/_make_create_matrix_fixtures.py

Regenerate ONLY when the producer's format intentionally changes; commit the
resulting .xlsx files so the scorecard suite never imports the producer.
"""
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
# .../engines/scorecard/tests/fixtures -> the sibling matrix engine at engines/matrix
ENGINE = HERE.parents[2] / "matrix"
sys.path.insert(0, str(ENGINE))

from src.audit import audit_bids  # noqa: E402
from src.models import (  # noqa: E402
    BidDocument, BidFooter, BidQualifications, ClassificationSource,
    CostStructure, DivisionBid, ExtractionConfidence, FormType,
    GrandTotalConfidence, InputType, LineItem)
from src.normalize import (  # noqa: E402
    build_normalized_view, compute_cross_bid_stats, normalize_bid)
from src.run_config import RunInputs  # noqa: E402
from src.write_matrix import write_matrix  # noqa: E402

# Fully synthetic firms + per-division dollar seeds (no client data).
FIRMS = [
    "Alpine Restoration Group",
    "Bayside Builders LLC",
    "Cypress Construction Co.",
    "Driftwood Contractors",
]

DIVS = [
    ("DIV 01 00 00", "General Requirements", Decimal("250000")),
    ("DIV 03 00 00", "Concrete", Decimal("400000")),
    ("DIV 07 00 00", "Thermal & Moisture Protection", Decimal("300000")),
    ("DIV 09 00 00", "Finishes", Decimal("150000")),
]

GSF = 12_000.0


def _div(code, name, desc, amount):
    return DivisionBid(
        csi_code=code, division_name=name,
        cost_structure=CostStructure.ITEMIZED,
        division_subtotal=amount,
        classification_source=ClassificationSource.CONTRACTOR_NATIVE,
        contractor_native_code=None,
        line_items=[LineItem(description=desc, amount=amount)],
    )


def _doc(i):
    factor = Decimal(1) + Decimal(i) * Decimal("0.08")
    divisions = [
        _div(code, name, f"{name} work", (base * factor).quantize(Decimal("1")))
        for code, name, base in DIVS
    ]
    total = sum(d.division_subtotal for d in divisions)
    return BidDocument(
        contractor_name=FIRMS[i],
        form_type=FormType.FALKE_STANDARD,
        bid_document_input_type=InputType.DIGITAL_NATIVE,
        divisions=divisions,
        footer=BidFooter(
            construction_cost_subtotal=total, gc_fee=None, grand_total=total,
            alternates=[], grand_total_confidence=GrandTotalConfidence.LOW,
        ),
        qualifications=BidQualifications(),
        extraction_confidence=ExtractionConfidence.HIGH,
    )


def build(n: int, out_dir: Path = HERE) -> Path:
    """Build an n-bidder synthetic workbook with the CURRENT in-tree producer.

    out_dir defaults to this fixtures directory (regenerating the local pins);
    the live cross-engine compat gate (tests/test_producer_live_compat.py)
    calls it with a tmp dir so every test run exercises the producer that
    exists NOW — the fixture-freshness gap that let v0.4.0 ship (P0-2)."""
    docs = [_doc(i) for i in range(n)]
    mirrors = [normalize_bid(d) for d in docs]
    leveled = compute_cross_bid_stats(
        [build_normalized_view(m, d) for m, d in zip(mirrors, docs)])
    items = audit_bids(leveled)
    run = RunInputs(
        project_name="Harborview Synthetic Test Tower",
        project_address="1 Test Quay, Test City FL 00000",
        gross_sf=GSF,
        sf_basis_label="GSF",
        sf_source="explicit",
    )
    out = Path(out_dir) / f"create_matrix_{n}bidders.xlsx"
    write_matrix(mirrors, out, run, audit_items=items, leveled_bids=leveled)
    print(f"[fixtures] wrote {out}")
    return out


if __name__ == "__main__":
    for n in (2, 4):
        build(n)
