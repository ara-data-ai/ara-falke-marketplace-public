"""CLI exit-code contract for missing required PARAMETERS.

The hard-stop validation (config.RunInputs.validate) is unit-tested in
test_config.py. This file locks the *process-level* contract a scripted caller
relies on: when a required PARAMETER is missing, cli.main() must (a) print the
friendly "[STOP] ..." message and (b) return a NON-ZERO exit code (2 — a
usage/parameter stop, distinct from the audit-FAIL path which returns 1), so an
automated runner can detect the stop instead of seeing exit 0.

A missing band value is rejected in load_config() BEFORE the matrix is opened,
so this test needs no real xlsx; a nonexistent matrix path never gets read. The
SF basis is NO LONGER a load_config hard-stop — it is a CLI suggest-and-confirm
gate (the matrix Row-10 GSF is read and offered as the default), so its
process-level contract lives in test_cli_sf_gate.py.
"""
from scorecard.cli import main

from .conftest import write_framework_xlsx, write_scores_xlsx

NONEXISTENT_MATRIX = "/tmp/__scorecard_no_such_matrix__.xlsx"


def _argv(tmp_path, **omit):
    """Full, otherwise-valid argv; pass omit=name->True to drop a flag.

    Includes an EXPLICIT --sf-basis (so the SF gate passes without touching the
    matrix), --baseline-confirmed, and VALID scoring xlsx inputs (so the
    scoring-inputs gate passes without touching the matrix), so the only stop
    this triggers is the MISSING-BAND stop in load_config — which fires BEFORE
    the matrix is opened.
    """
    fw = write_framework_xlsx(str(tmp_path / "framework.xlsx"))
    cs = write_scores_xlsx(str(tmp_path / "scores.xlsx"),
                           ["Pricing", "Scope", "Docs"],
                           [("Any Firm", [7, 7, 7])])
    args = ["--matrix", NONEXISTENT_MATRIX, "--project-name", "TEST CLI",
            "--sf-basis", "16000", "--baseline-confirmed",
            "--scoring-framework", fw, "--category-scores", cs]
    if not omit.get("band_low"):
        args += ["--band-low", "3.35"]
    if not omit.get("band_high"):
        args += ["--band-high", "3.55"]
    if not omit.get("mid"):
        args += ["--mid", "3.4"]
    return args


def test_missing_band_exits_nonzero_and_prints_stop(tmp_path, capsys):
    rc = main(_argv(tmp_path, band_low=True))
    assert rc == 2
    err = capsys.readouterr().err
    assert "[STOP]" in err
    assert "band_low" in err
