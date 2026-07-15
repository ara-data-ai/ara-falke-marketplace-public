"""Pipeline-level input gates + exit-code contract v2 (F1 + F2, Floyd R-2/R-3).

Covers the three v0.4 Tier-1 behaviors added to src/pipeline.py:

  * F1 / exit 4 — a run that delivers a matrix while EXCLUDING one or more
    inputs (JSON parse / Pydantic validation failures) exits 4, and every
    exclusion lands as a RED INPUT_EXCLUDED row on the AUDIT sheet (visible on
    the instrument, not just console prose). Standing gate criterion: no
    dropped inputs on exit 0.
  * --expect-bids N — caller-asserted bid count; mismatch hard-stops exit 2
    BEFORE anything is written.
  * F2 / duplicate contractor name — two interim files claiming the same
    (case-folded) contractor_name hard-stop exit 2 BEFORE anything is written,
    with an operator-actionable message naming both files. This kills both
    failure modes reproduced in the v04 review: false quarantine (different
    totals) and the silent verification hole (identical duplicates).

Clean runs are asserted unchanged (exit 0 == plain return, no gate output).

Run from the engine root (engines/matrix/):
    python3 -m pytest tests/test_pipeline_gates.py -v
"""
from __future__ import annotations

import json

import openpyxl
import pytest

from src.pipeline import run_pipeline


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _bid(name: str, subtotal: float = 100000.0) -> dict:
    """A minimal, schema-valid BidDocument dict (one lump-sum division)."""
    return {
        "contractor_name": name,
        "form_type": "FALKE_STANDARD",
        "divisions": [
            {
                "csi_code": "DIV 03 00 00",
                "division_name": "Concrete",
                "cost_structure": "LUMP_SUM",
                "division_subtotal": str(subtotal),
            }
        ],
        "footer": {
            "construction_cost_subtotal": str(subtotal),
            "grand_total": str(subtotal),
        },
        "qualifications": {},
        "extraction_confidence": "HIGH",
    }


def _write_run(tmp_path, bids: dict[str, dict]) -> tuple:
    """Write an interim dir of bid JSONs + a project config; return
    (interim_dir, out_path, config_path)."""
    interim = tmp_path / "interim"
    interim.mkdir()
    for fname, doc in bids.items():
        (interim / fname).write_text(json.dumps(doc), encoding="utf-8")
    config = tmp_path / "project.json"
    config.write_text(json.dumps({
        "project_name": "Gate Test Condo",
        "project_address": "1 Test Way",
        "sf_basis_label": "GSF",
        "gross_sf": 50000,
    }), encoding="utf-8")
    return interim, tmp_path / "out" / "matrix.xlsx", config


def _run(interim, out, config, **kw):
    out.parent.mkdir(exist_ok=True)
    return run_pipeline(
        interim_dir=interim, out_path=out, project_config=config,
        sf_confirmed=True, **kw,
    )


def _audit_rows(out_path) -> list[tuple]:
    wb = openpyxl.load_workbook(out_path)
    ws = wb["AUDIT"]
    rows = []
    for row in ws.iter_rows(min_row=5, values_only=True):
        if row[0] in ("RED", "YELLOW", "GREEN"):
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Clean runs are unchanged (exit-0 == plain return)
# ---------------------------------------------------------------------------

class TestCleanRunUnchanged:
    def test_clean_two_bid_run_returns_and_writes(self, tmp_path, capsys):
        interim, out, config = _write_run(tmp_path, {
            "alpha.json": _bid("Alpha Builders", 100000),
            "bravo.json": _bid("Bravo Construction", 120000),
        })
        _run(interim, out, config)  # must NOT raise SystemExit
        assert out.exists()
        text = capsys.readouterr().out
        assert "Tie-out OK" in text
        assert "EXCLUDED" not in text
        # No INPUT_EXCLUDED rows on a clean run
        assert not [r for r in _audit_rows(out) if r[2] == "INPUT_EXCLUDED"]

    def test_deliberate_skip_is_not_a_drop(self, tmp_path):
        """skip=true files are deliberate skips — never exit 4."""
        interim, out, config = _write_run(tmp_path, {
            "alpha.json": _bid("Alpha Builders"),
            "bravo.json": _bid("Bravo Construction"),
            "template.json": {**_bid("Template Co"), "skip": True},
        })
        _run(interim, out, config)  # returns clean
        assert out.exists()


# ---------------------------------------------------------------------------
# F1 — exit 4 + RED INPUT_EXCLUDED audit rows (no silent bid drops)
# ---------------------------------------------------------------------------

class TestExit4InputExcluded:
    def test_validation_failure_exits_4_with_red_audit_row(self, tmp_path, capsys):
        bad = _bid("Charlie Corp")
        del bad["footer"]  # schema-invalid: footer is required
        interim, out, config = _write_run(tmp_path, {
            "alpha.json": _bid("Alpha Builders", 100000),
            "bravo.json": _bid("Bravo Construction", 120000),
            "charlie.json": bad,
        })
        with pytest.raises(SystemExit) as exc:
            _run(interim, out, config)
        assert exc.value.code == 4
        # Delivered: the file exists, with the surviving bids tied out
        assert out.exists()
        # The exclusion is ON THE INSTRUMENT: a RED INPUT_EXCLUDED row naming
        # the file, not just console prose.
        excluded = [r for r in _audit_rows(out) if r[2] == "INPUT_EXCLUDED"]
        assert len(excluded) == 1
        assert excluded[0][0] == "RED"
        assert excluded[0][3] == "charlie.json"          # Contractor/identifier col
        assert "EXCLUDED" in excluded[0][7]              # Message col
        assert "DELIVERED, BUT 1 INPUT BID(S) EXCLUDED" in capsys.readouterr().out

    def test_json_parse_failure_exits_4(self, tmp_path):
        interim, out, config = _write_run(tmp_path, {
            "alpha.json": _bid("Alpha Builders"),
            "bravo.json": _bid("Bravo Construction"),
        })
        (interim / "garbage.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _run(interim, out, config)
        assert exc.value.code == 4
        excluded = [r for r in _audit_rows(out) if r[2] == "INPUT_EXCLUDED"]
        assert [r[3] for r in excluded] == ["garbage.json"]

    def test_stress_probe_15_bidders_4_invalid(self, tmp_path, capsys):
        """The exact v04-review stress probe: 15 inputs, 4 failing validation.
        Was: 11-bidder matrix, exit 0, console-only warnings. Now: exit 4 with
        4 RED INPUT_EXCLUDED rows."""
        bids = {
            f"bidder{i:02d}.json": _bid(f"Bidder {i:02d} LLC", 100000 + i * 7000)
            for i in range(11)
        }
        for i in range(4):
            invalid = _bid(f"Broken {i} Inc")
            del invalid["footer"]
            bids[f"broken{i}.json"] = invalid
        interim, out, config = _write_run(tmp_path, bids)
        with pytest.raises(SystemExit) as exc:
            _run(interim, out, config)
        assert exc.value.code == 4
        assert out.exists()
        excluded = [r for r in _audit_rows(out) if r[2] == "INPUT_EXCLUDED"]
        assert len(excluded) == 4
        assert all(r[0] == "RED" for r in excluded)
        assert {r[3] for r in excluded} == {f"broken{i}.json" for i in range(4)}
        assert "DELIVERED, BUT 4 INPUT BID(S) EXCLUDED" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --expect-bids N — caller-asserted count, hard stop before writing
# ---------------------------------------------------------------------------

class TestExpectBidsGate:
    def test_mismatch_stops_exit_2_before_writing(self, tmp_path, capsys):
        interim, out, config = _write_run(tmp_path, {
            "alpha.json": _bid("Alpha Builders"),
            "bravo.json": _bid("Bravo Construction"),
        })
        with pytest.raises(SystemExit) as exc:
            _run(interim, out, config, expect_bids=3)
        assert exc.value.code == 2
        assert not out.exists()  # BEFORE writing
        assert "--expect-bids 3" in capsys.readouterr().out

    def test_mismatch_from_validation_failure_stops_exit_2(self, tmp_path, capsys):
        """The defense-in-depth case: the skill layer expected 3 (its PDF
        count); one input failed validation → gate stops BEFORE the write, and
        the stop message names the failed file."""
        bad = _bid("Charlie Corp")
        del bad["footer"]
        interim, out, config = _write_run(tmp_path, {
            "alpha.json": _bid("Alpha Builders"),
            "bravo.json": _bid("Bravo Construction"),
            "charlie.json": bad,
        })
        with pytest.raises(SystemExit) as exc:
            _run(interim, out, config, expect_bids=3)
        assert exc.value.code == 2
        assert not out.exists()
        assert "charlie.json" in capsys.readouterr().out

    def test_match_runs_clean(self, tmp_path):
        interim, out, config = _write_run(tmp_path, {
            "alpha.json": _bid("Alpha Builders"),
            "bravo.json": _bid("Bravo Construction"),
        })
        _run(interim, out, config, expect_bids=2)  # returns clean
        assert out.exists()


# ---------------------------------------------------------------------------
# F2 — duplicate contractor-name hard stop (exit 2, before writing)
# ---------------------------------------------------------------------------

class TestDuplicateNameGate:
    def test_duplicate_different_totals_stops_exit_2(self, tmp_path, capsys):
        """The false-quarantine reproduction from the v04 review: same name,
        different grand totals. Was: exit 3 with a FALSE 'rendering defective'
        quarantine on a correct workbook. Now: exit 2 before writing."""
        interim, out, config = _write_run(tmp_path, {
            "sailfish_a.json": _bid("Sailfish Ridge Construction", 100000),
            "sailfish_b.json": _bid("Sailfish Ridge Construction", 140000),
            "other.json": _bid("Other Builders", 120000),
        })
        with pytest.raises(SystemExit) as exc:
            _run(interim, out, config)
        assert exc.value.code == 2
        assert not out.exists()
        text = capsys.readouterr().out
        # Operator message — Marvin's W-D M-4 ruling, verbatim anchors:
        # lead with the stale-JSON cause, name the exact field to edit, close
        # with the no-silent-drop guarantee. Both files named.
        assert ("Two bid files carry the same contractor name "
                "'Sailfish Ridge Construction'") in text
        assert "sailfish_a.json" in text and "sailfish_b.json" in text
        assert ("stale extraction JSON from an earlier run still in the "
                "interim folder — delete the stale file and re-run") in text
        assert ("rename the second file's `contractor_name` to "
                "\"Sailfish Ridge Construction - Alternate\"") in text
        assert "never merges two files or silently drops one" in text
        assert "No file was written" in text

    def test_identical_duplicate_stops_exit_2(self, tmp_path):
        """The silent-verification-hole variant: identical duplicates (the
        re-run / stale-JSON case) previously passed exit 0 with one column
        never verified. Now: exit 2."""
        interim, out, config = _write_run(tmp_path, {
            "sailfish_a.json": _bid("Sailfish Ridge Construction", 100000),
            "sailfish_b.json": _bid("Sailfish Ridge Construction", 100000),
        })
        with pytest.raises(SystemExit) as exc:
            _run(interim, out, config)
        assert exc.value.code == 2
        assert not out.exists()

    def test_duplicate_detection_is_case_and_whitespace_folded(self, tmp_path):
        interim, out, config = _write_run(tmp_path, {
            "a.json": _bid("SAILFISH  RIDGE construction"),
            "b.json": _bid("Sailfish Ridge Construction"),
        })
        with pytest.raises(SystemExit) as exc:
            _run(interim, out, config)
        assert exc.value.code == 2

    def test_distinct_names_pass(self, tmp_path):
        interim, out, config = _write_run(tmp_path, {
            "a.json": _bid("Sailfish Ridge Construction"),
            "b.json": _bid("Cedar Key Builders"),
        })
        _run(interim, out, config)  # returns clean
        assert out.exists()
