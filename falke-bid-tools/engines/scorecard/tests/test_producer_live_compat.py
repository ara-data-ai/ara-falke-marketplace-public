"""LIVE cross-engine compat gate (P0-2 — Floyd consolidated ruling, C-R1 ≡
Boris E3, merged): generate a workbook with the IN-TREE matrix engine (same
plugin, same version), parse it with THIS scorecard, and tie the numbers out
cell-vs-parsed against the workbook's own machine-key footer.

This is the test that would have failed the v0.4.0 release before it shipped:
the committed producer fixtures froze a producer that no longer existed, so
the suite stayed green while production broke ("fixture-based contract testing
without a regeneration trigger is a contract with a dead counterparty").
Because it is pytest-shaped, release.sh gates [2/8] and [6/8] run it for free.

Fully synthetic bids (fictional firms/dollars — Floyd fixture rule); the
workbooks are generated into a tmp dir per session, never committed. The
committed tests/fixtures/create_matrix_{2,4}bidders.xlsx remain as v0.3-era
BACK-COMPAT pins (test_create_matrix_compat.py); this module covers the
producer that exists NOW.
"""
from __future__ import annotations

import importlib.util
import os
import shutil

import openpyxl
import pytest

from scorecard.config import load_config
from scorecard.errors import MatrixStructureError, ProducerVersionError
from scorecard.matrix import (LEVELED_SHEET, MIRROR_SHEET, MatrixParser,
                              read_producer_stamp)
from scorecard.pipeline import audit_run, run_scorecard

HERE = os.path.dirname(os.path.abspath(__file__))
GENERATOR = os.path.join(HERE, "fixtures", "_make_create_matrix_fixtures.py")
MATRIX_ENGINE = os.path.abspath(os.path.join(HERE, "..", "..", "matrix"))

pytestmark = pytest.mark.skipif(
    not os.path.isdir(os.path.join(MATRIX_ENGINE, "src")),
    reason="in-tree matrix engine not present (engines/matrix)")

# synthetic ground truth (mirrors the generator's seeds):
# 4 priced CSI divisions, base dollars scaled by (1 + 0.08*i) per bidder.
_DIV_BASES = {
    "DIV 01 00 00": 250_000.0,
    "DIV 03 00 00": 400_000.0,
    "DIV 07 00 00": 300_000.0,
    "DIV 09 00 00": 150_000.0,
}
_FIRMS = ["Alpine Restoration Group", "Bayside Builders LLC",
          "Cypress Construction Co.", "Driftwood Contractors"]


def _expected_totals(n):
    return {
        _FIRMS[i]: round(sum(_DIV_BASES.values()) * (1 + 0.08 * i))
        for i in range(n)
    }


@pytest.fixture(scope="session")
def generated(tmp_path_factory):
    """Generate 2- and 4-bidder workbooks with the CURRENT producer, once."""
    out = tmp_path_factory.mktemp("live_producer")
    spec = importlib.util.spec_from_file_location("_mk_live", GENERATOR)
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    return {n: str(mk.build(n, out)) for n in (2, 4)}


def _cfg():
    return load_config(overrides={
        "sf_basis": 10_000, "band_low": 1.00, "band_high": 1.20,
        "modeled_mid_takeoff": 1.10})


def _machine_key_footer(path, sheet=MIRROR_SHEET):
    """INDEPENDENT read of the mirror's machine-key GRAND_TOTAL footer row:
    row located by col-A key, per-bidder values by the bidder name columns
    (names row 5, COST-SUBTOTALS column of each 2-col group)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb[sheet].iter_rows(values_only=True))
    gt_row = next(i for i, r in enumerate(rows)
                  if r and str(r[0] or "").strip() == "GRAND_TOTAL")
    names_row = next(r for r in rows if r and any(f in r for f in _FIRMS))
    out = {}
    for c, v in enumerate(names_row):
        if v in _FIRMS:
            out[v] = rows[gt_row][c]
    return out


# ---------------------------------------------------------------------------
# THE gate: current producer output -> default parse -> machine-key tie-out
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n", [2, 4])
def test_default_path_grand_totals_tie_out_to_machine_key_footer(generated, n):
    parsed = MatrixParser(_cfg().block("matrix")).parse(generated[n])

    # the DEFAULT sheet is the ruled leveled view — never an ordering accident
    assert parsed.sheet_name == LEVELED_SHEET
    assert parsed.sheet_mode == "leveled"

    expected = _expected_totals(n)
    got = {b.raw_name: b.grand_total for b in parsed.included_blocks}
    assert got == expected, got

    # cell-vs-parsed: the mirror's machine-key footer must carry the same
    # values (independent openpyxl read — no parser code involved)
    footer = _machine_key_footer(generated[n])
    assert footer == expected, footer


@pytest.mark.parametrize("n", [2, 4])
def test_division_subtotals_tie_out_cell_vs_parsed(generated, n):
    """Division-level tie-out on the DEFAULT (leveled) sheet: exactly the 20
    CSI division-subtotal rows are detected (the footer 'Fees Subtotal' is a
    markup row, never a 21st division — P0-3 ride-along), each bidder
    populates exactly the 4 priced divisions, and the priced cells re-read
    from the workbook equal the synthetic seeds."""
    parsed = MatrixParser(_cfg().block("matrix")).parse(generated[n])
    assert len(parsed.division_rows) == 20, \
        [lab for _r, lab in parsed.division_rows]
    assert not any("fees" in lab.lower() for _r, lab in parsed.division_rows)

    priced_short = {"general requirements", "concrete",
                    "thermal & moisture protection", "finishes"}
    for i, b in enumerate(parsed.included_blocks):
        assert b.populated_divisions == 4, (b.raw_name, b.populated_divisions)
        factor = 1 + 0.08 * i
        sub_col = b.cols.get("cost_subtotal") or b.cols.get("cost")
        seen = {}
        for (r, lab) in parsed.division_rows:
            v = parsed.grid.cell(r, sub_col)
            if isinstance(v, (int, float)) and v != 0:
                seen[lab.lower().replace(" subtotal", "")] = float(v)
        expected_cells = {
            name.lower(): round(base * factor)
            for code, base in _DIV_BASES.items()
            for name in [{"DIV 01 00 00": "General Requirements",
                          "DIV 03 00 00": "Concrete",
                          "DIV 07 00 00": "Thermal & Moisture Protection",
                          "DIV 09 00 00": "Finishes"}[code]]
        }
        assert set(seen) == set(expected_cells), (b.raw_name, seen)
        for k, v in expected_cells.items():
            assert abs(seen[k] - v) < 1.0, (b.raw_name, k, seen[k], v)


def test_mirror_path_reads_grand_totals_via_machine_key(generated):
    """P0-3 regression: the as-submitted mirror (explicit --sheet) reads its
    grand totals via the col-A GRAND_TOTAL machine key — the v0.4.0 unified
    legend can never be selected again (its prose row carries no bidder-column
    numerics and 'subtotal' has no word-boundary 'total')."""
    mc = dict(_cfg().block("matrix"))
    mc["sheet_name"] = MIRROR_SHEET
    parsed = MatrixParser(mc).parse(generated[4])
    assert parsed.sheet_mode == "mirror"
    assert parsed.grand_total_label.strip().upper() == "GRAND TOTAL"
    got = {b.raw_name: b.grand_total for b in parsed.included_blocks}
    assert got == _expected_totals(4)
    # the chosen row must be ABOVE the legend block (which contains the
    # 'subtotal rows is house formatting' prose line that broke v0.4.0)
    legend_rows = [r for r in range(1, parsed.grid.max_row + 1)
                   if isinstance(parsed.grid.cell(r, 3), str)
                   and "house formatting" in parsed.grid.cell(r, 3).lower()]
    assert legend_rows, "expected the v0.4.0 unified legend on the mirror"
    assert parsed.grand_total_row < min(legend_rows)


def test_producer_stamp_written_and_in_range(generated):
    wb = openpyxl.load_workbook(generated[4], read_only=True)
    stamp = read_producer_stamp(wb)
    assert stamp == {"producer": "falke-bid-tools/matrix",
                     "format_version": "0.4.0"}
    parsed = MatrixParser(_cfg().block("matrix")).parse(generated[4])
    assert parsed.producer_stamp == stamp
    assert any("inside supported range" in l for l in parsed.log)


def test_out_of_range_producer_stamp_hard_stops(generated, tmp_path):
    """A NEWER-format stamp than SUPPORTED_PRODUCER refuses to parse."""
    path = str(tmp_path / "future.xlsx")
    shutil.copy(generated[2], path)
    wb = openpyxl.load_workbook(path)
    for p in wb.custom_doc_props.props:
        if p.name == "falke_bid_tools.format_version":
            p.value = "9.9"
    wb.save(path)
    with pytest.raises(ProducerVersionError):
        MatrixParser(_cfg().block("matrix")).parse(path)


def test_missing_leveled_sheet_on_producer_workbook_hard_stops(generated,
                                                               tmp_path):
    """Marvin P0-7 hard rule 2: a producer workbook without the default
    leveled sheet exits with a hard stop NAMING the expected sheet — never a
    silent first-sheet fallback."""
    path = str(tmp_path / "no_leveled.xlsx")
    shutil.copy(generated[2], path)
    wb = openpyxl.load_workbook(path)
    del wb[LEVELED_SHEET]
    wb.save(path)
    with pytest.raises(MatrixStructureError) as ei:
        MatrixParser(_cfg().block("matrix")).parse(path)
    assert LEVELED_SHEET in str(ei.value)


def test_quarantine_banner_blocks_via_audit(generated, tmp_path):
    """Marvin P0-7 hard rule 5: a workbook carrying the producer's Stage-6b
    RED quarantine banner is flagged at parse and BLOCKED by audit C17."""
    path = str(tmp_path / "quarantined.xlsx")
    shutil.copy(generated[2], path)
    wb = openpyxl.load_workbook(path)
    for sheet in (MIRROR_SHEET, LEVELED_SHEET):
        wb[sheet].cell(row=1, column=1).value = (
            "⚠ AUTOMATED CHECK FAILED — DO NOT RELY ON THE FLAGGED FIGURES "
            "FOR AN AWARD DECISION.")
    wb.save(path)

    cfg = _cfg()
    result = run_scorecard(path, cfg, project_name="Quarantine Probe")
    assert result["parsed"].quarantine_flag is True
    ar, _paths = audit_run(result, cfg, str(tmp_path / "out"))
    c17 = next(c for c in ar.checks if c.name == "C17")
    assert c17.status == "fail" and c17.severity == "BLOCKER"
    assert ar.verdict == "FAIL"


def test_cross_sheet_gt_mismatch_is_audit_blocker(generated, tmp_path):
    """Marvin P0-7 hard rule 4: mirror-GT == leveled-GT is a producer
    invariant; the audit's independent cross-sheet re-read (C18) BLOCKS when
    it is broken."""
    path = str(tmp_path / "tampered.xlsx")
    shutil.copy(generated[2], path)
    wb = openpyxl.load_workbook(path)
    ws = wb[MIRROR_SHEET]
    gt_row = next(r for r in range(1, ws.max_row + 1)
                  if str(ws.cell(row=r, column=1).value or "").strip()
                  == "GRAND_TOTAL")
    # bump the first bidder's mirror GT (col C = first group's COST SUBTOTALS)
    for c in range(3, ws.max_column + 1):
        v = ws.cell(row=gt_row, column=c).value
        if isinstance(v, (int, float)):
            ws.cell(row=gt_row, column=c).value = v + 12_345
            break
    wb.save(path)

    cfg = _cfg()
    result = run_scorecard(path, cfg, project_name="Tamper Probe")
    ar, _paths = audit_run(result, cfg, str(tmp_path / "out"))
    c18 = next(c for c in ar.checks if c.name == "C18")
    assert c18.status == "fail" and c18.severity == "BLOCKER"
    assert ar.verdict == "FAIL"


def test_full_pipeline_and_audit_clean_on_live_producer_output(generated,
                                                               tmp_path):
    """End-to-end on the CURRENT producer's output, default path: correct
    totals/$/SF, ranking, the leveled disclosure recorded, and a self-audit
    with no blockers (C1/C2/C14/C17/C18 all pass live)."""
    cfg = _cfg()
    result = run_scorecard(generated[4], cfg,
                           project_name="Harborview Synthetic Test Tower")
    expected = _expected_totals(4)
    by_name = {b["name"]: b for b in result["bidders"]}
    for name, total in expected.items():
        assert by_name[name]["total"] == total
        assert by_name[name]["per_sf"] == round(total / 10_000)
    assert sorted(b["rank"] for b in result["bidders"]) == [1, 2, 3, 4]

    assert result["sheet"]["name"] == LEVELED_SHEET
    assert result["sheet"]["mode"] == "leveled"
    assert "Leveled/Normalized view" in result["sheet"]["disclosure"]

    ar, _paths = audit_run(result, cfg, str(tmp_path / "out"))
    assert ar.counts["blocker"] == 0, [
        c.verdict_line for c in ar.checks if c.status == "fail"]
    c18 = next(c for c in ar.checks if c.name == "C18")
    assert c18.status == "pass"
