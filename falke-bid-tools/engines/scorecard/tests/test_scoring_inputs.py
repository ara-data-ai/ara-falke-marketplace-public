"""Unit tests for the two REQUIRED scoring xlsx inputs (scoring_inputs.py).

Covers: valid parse of both files (incl. the shipped synthetic sample
examples), weights-don't-sum-100, blank/duplicate short labels, column mismatch
vs the framework, out-of-range / non-numeric / blank scores, duplicate firms,
and the missing-sheet stop. All synthetic files are built with the shared
conftest builders (template layout), no client data.
"""
from __future__ import annotations

import os

import pytest

from scorecard.scoring_inputs import (build_scores_from_inputs,
                                      parse_category_scores,
                                      parse_scoring_framework)
from .conftest import (SAMPLE_FRAMEWORK_XLSX, SAMPLE_SCORES_XLSX,
                       SIMPLE_FRAMEWORK_ROWS, write_framework_xlsx,
                       write_scores_xlsx)

FALKE_LABELS = ["Pricing", "Scope", "Condo Exp", "CO Risk",
                "Reputation", "Financial", "Controls", "Docs"]
FALKE_WEIGHTS = [25, 15, 15, 15, 10, 10, 5, 5]


# ---------------------------------------------------------------------------
# valid parses
# ---------------------------------------------------------------------------
def test_parse_framework_valid(tmp_path):
    p = write_framework_xlsx(str(tmp_path / "fw.xlsx"))
    fw = parse_scoring_framework(p)
    assert [f["short_label"] for f in fw] == ["Pricing", "Scope", "Docs"]
    assert [f["weight"] for f in fw] == [50, 30, 20]
    assert [f["key"] for f in fw] == ["pricing", "scope", "docs"]
    assert fw[0]["category"] == "Market-aligned pricing"
    assert fw[0]["description"] == "Closeness to baseline."


def test_parse_scores_valid_order_insensitive(tmp_path):
    fw = parse_scoring_framework(
        write_framework_xlsx(str(tmp_path / "fw.xlsx")))
    # columns supplied in a DIFFERENT order than the framework — must still map
    p = write_scores_xlsx(str(tmp_path / "cs.xlsx"), ["Docs", "Pricing", "Scope"],
                          [("Alpha Builders", [7, 9, 8]),
                           ("Beta Corp", [5, 4, 6])])
    cs = parse_category_scores(p, fw)
    assert cs["Alpha Builders"] == {"Docs": 7, "Pricing": 9, "Scope": 8}
    assert cs["Beta Corp"]["Pricing"] == 4


@pytest.mark.skipif(
    not (os.path.exists(SAMPLE_FRAMEWORK_XLSX) and os.path.exists(SAMPLE_SCORES_XLSX)),
    reason="sample scoring xlsx absent (gitignored binaries; regenerate with "
           "examples/_make_sample_scoring_inputs.py)")
def test_shipped_sample_examples_parse_to_falke_framework_and_gold_scores():
    fw = parse_scoring_framework(SAMPLE_FRAMEWORK_XLSX)
    assert [f["short_label"] for f in fw] == FALKE_LABELS
    assert [f["weight"] for f in fw] == FALKE_WEIGHTS
    cs = parse_category_scores(SAMPLE_SCORES_XLSX, fw)
    assert set(cs) == {"Acme", "Borealis", "Cascade", "Dorne", "Crest",
                       "Fjord", "Granite"}
    # spot-check two published sample Section-E values
    assert cs["Acme"]["Pricing"] == 9
    assert cs["Granite"]["CO Risk"] == 2


def test_build_scores_from_inputs_full_coverage(tmp_path):
    fw = parse_scoring_framework(
        write_framework_xlsx(str(tmp_path / "fw.xlsx")))
    bs = build_scores_from_inputs(
        "Alpha", fw, {"Pricing": 9, "Scope": 8, "Docs": 7}, run_id="t")
    wa = bs.weighted_average_x10({f["key"]: f["weight"] / 100.0 for f in fw})
    assert wa["coverage"] == pytest.approx(1.0)
    # 9*.5 + 8*.3 + 7*.2 = 8.3 -> 83.0 on the /100 scale
    assert wa["wavg"] == pytest.approx(83.0)
    assert bs.categories["pricing"].source == "category_scores_xlsx"


# ---------------------------------------------------------------------------
# framework validation errors
# ---------------------------------------------------------------------------
def test_framework_weights_must_sum_100(tmp_path):
    rows = [("A cat", "A", 50, ""), ("B cat", "B", 30, "")]  # sums to 80
    p = write_framework_xlsx(str(tmp_path / "fw.xlsx"), rows)
    with pytest.raises(ValueError, match="sum to 100"):
        parse_scoring_framework(p)


def test_framework_excel_percent_formatting_hint(tmp_path):
    # Excel percent-formatted cells store fractions -> sum 1.0, hint expected
    rows = [("A cat", "A", 0.5, ""), ("B cat", "B", 0.3, ""),
            ("C cat", "C", 0.2, "")]
    p = write_framework_xlsx(str(tmp_path / "fw.xlsx"), rows)
    with pytest.raises(ValueError, match="Excel-percent"):
        parse_scoring_framework(p)


def test_framework_blank_short_label_stops(tmp_path):
    rows = [("A cat", "A", 50, ""), ("B cat", None, 50, "")]
    p = write_framework_xlsx(str(tmp_path / "fw.xlsx"), rows)
    with pytest.raises(ValueError, match="Short Label.*blank"):
        parse_scoring_framework(p)


def test_framework_duplicate_short_label_stops(tmp_path):
    rows = [("A cat", "Pricing", 50, ""), ("B cat", "pricing", 50, "")]
    p = write_framework_xlsx(str(tmp_path / "fw.xlsx"), rows)
    with pytest.raises(ValueError, match="duplicate Short Label"):
        parse_scoring_framework(p)


def test_framework_non_numeric_weight_stops(tmp_path):
    rows = [("A cat", "A", "half", ""), ("B cat", "B", 50, "")]
    p = write_framework_xlsx(str(tmp_path / "fw.xlsx"), rows)
    with pytest.raises(ValueError, match="must be numeric"):
        parse_scoring_framework(p)


def test_framework_missing_sheet_stops(tmp_path):
    import openpyxl
    p = str(tmp_path / "wrong.xlsx")
    wb = openpyxl.Workbook()
    wb.active.title = "NotTheFramework"
    wb.save(p)
    with pytest.raises(ValueError, match="Scoring_Framework"):
        parse_scoring_framework(p)


def test_framework_empty_stops(tmp_path):
    p = write_framework_xlsx(str(tmp_path / "fw.xlsx"), rows=[])
    with pytest.raises(ValueError, match="No framework rows"):
        parse_scoring_framework(p)


# ---------------------------------------------------------------------------
# category-scores validation errors
# ---------------------------------------------------------------------------
@pytest.fixture
def simple_fw(tmp_path):
    return parse_scoring_framework(
        write_framework_xlsx(str(tmp_path / "fw.xlsx"),
                             SIMPLE_FRAMEWORK_ROWS))


def test_scores_column_mismatch_names_missing_and_extra(tmp_path, simple_fw):
    # 'Docs' missing, 'Schedule' unexpected
    p = write_scores_xlsx(str(tmp_path / "cs.xlsx"),
                          ["Pricing", "Scope", "Schedule"],
                          [("Alpha", [9, 8, 7])])
    with pytest.raises(ValueError) as ei:
        parse_category_scores(p, simple_fw)
    msg = str(ei.value)
    assert "Missing column(s): Docs" in msg
    assert "Unexpected column(s): Schedule" in msg


def test_scores_out_of_range_stops(tmp_path, simple_fw):
    p = write_scores_xlsx(str(tmp_path / "cs.xlsx"),
                          ["Pricing", "Scope", "Docs"],
                          [("Alpha", [11, 8, 7])])
    with pytest.raises(ValueError, match="within 1–10"):
        parse_category_scores(p, simple_fw)


def test_scores_non_numeric_stops(tmp_path, simple_fw):
    p = write_scores_xlsx(str(tmp_path / "cs.xlsx"),
                          ["Pricing", "Scope", "Docs"],
                          [("Alpha", [9, "good", 7])])
    with pytest.raises(ValueError, match="must be numeric"):
        parse_category_scores(p, simple_fw)


def test_scores_blank_cell_is_not_yet_scored_not_an_error(tmp_path, simple_fw):
    """P1-2 §1.1: a blank means NOT YET SCORED. Always. It used to raise, which
    is what made the provisional pathway dead (F3) — the engine could always do
    this; the parser rejected the input that would reach it.

    It arrives as None and is NEVER omitted: an omitted key would make "blank"
    and "column absent" indistinguishable, so coverage would be computed off a
    shape rather than off a fact."""
    p = write_scores_xlsx(str(tmp_path / "cs.xlsx"),
                          ["Pricing", "Scope", "Docs"],
                          [("Alpha", [9, None, 7])])
    scores = parse_category_scores(p, simple_fw)
    assert scores["Alpha"] == {"Pricing": 9, "Scope": None, "Docs": 7}
    assert "Scope" in scores["Alpha"]      # present-and-None, never dropped


def test_scores_all_blank_hard_stops(tmp_path, simple_fw):
    """P1-2 §1.2 — THE one hard stop, and it is degenerate: a grid with zero
    scored cells anywhere is not a partial evaluation record, it is the blank
    template. The tool refuses to render NOTHING; it never refuses to render
    LITTLE. And the leveled matrix already IS the zero-coverage artifact."""
    p = write_scores_xlsx(str(tmp_path / "cs.xlsx"),
                          ["Pricing", "Scope", "Docs"],
                          [("Alpha", [None, None, None]),
                           ("Beta", [None, None, None])])
    with pytest.raises(ValueError, match="No category scores were supplied"):
        parse_category_scores(p, simple_fw)


def test_one_scored_cell_renders(tmp_path, simple_fw):
    """§4.4 boundary: 1 of 6 cells → honest, useless, and LEGIBLY useless. The
    tool does not rule on whether the evaluation is far enough along."""
    p = write_scores_xlsx(str(tmp_path / "cs.xlsx"),
                          ["Pricing", "Scope", "Docs"],
                          [("Alpha", [9, None, None]),
                           ("Beta", [None, None, None])])
    scores = parse_category_scores(p, simple_fw)
    assert scores["Alpha"]["Pricing"] == 9
    assert scores["Beta"] == {"Pricing": None, "Scope": None, "Docs": None}


def test_scores_duplicate_firm_stops(tmp_path, simple_fw):
    p = write_scores_xlsx(str(tmp_path / "cs.xlsx"),
                          ["Pricing", "Scope", "Docs"],
                          [("Alpha Builders", [9, 8, 7]),
                           ("ALPHA  Builders", [5, 5, 5])])
    with pytest.raises(ValueError, match="duplicate firm"):
        parse_category_scores(p, simple_fw)


def test_scores_no_rows_stops(tmp_path, simple_fw):
    p = write_scores_xlsx(str(tmp_path / "cs.xlsx"),
                          ["Pricing", "Scope", "Docs"], [])
    with pytest.raises(ValueError, match="No bidder rows"):
        parse_category_scores(p, simple_fw)
