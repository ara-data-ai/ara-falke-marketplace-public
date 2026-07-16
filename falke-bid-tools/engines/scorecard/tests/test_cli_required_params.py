"""CLI exit-code contract for missing required PARAMETERS.

The hard-stop validation (config.RunInputs.validate) is unit-tested in
test_config.py. This file locks the *process-level* contract a scripted caller
relies on: when a required PARAMETER is missing, cli.main() must (a) print the
friendly "[STOP] ..." message and (b) return exit 2 — an input-gate stop — so an
automated runner can detect the stop instead of seeing exit 0.

A missing band value is rejected in load_config() BEFORE the matrix is opened
(the explicit --sf-basis below skips the Row-10 detection that would open it).
The matrix path is nevertheless a file that EXISTS — an empty stub built in
tmp_path — because P1-1's exit-contract v2 added an unreadable-matrix pre-flight
(exit 1, environment/nothing-written) that fires before any parameter gate. The
pre-flight only os.path.isfile()s; it never opens. So a zero-byte stub is
sufficient, and it exists in EVERY tree.

That last clause is the whole point. This test first used a nonexistent path
(which the pre-flight then caught, asserting the wrong code), and was then
repointed at tests/fixtures/create_matrix_2bidders.xlsx — a real workbook that
exists on a developer machine and NOT in the shipped tree, because *.xlsx is
gitignored by design and release.sh stages `git archive` of tracked files only.
Result: 267 green tests over a release that aborted at [6/8]. The fix is not to
find a better file; it is to stop depending on a file the release does not
carry. See tests/test_release_stage_contract.py, which pins the class.

The SF basis is NO LONGER a load_config hard-stop — it is a CLI
suggest-and-confirm gate (the matrix Row-10 GSF is read and offered as the
default), so its process-level contract lives in test_cli_sf_gate.py.
"""
from scorecard.cli import main

from .conftest import write_framework_xlsx, write_scores_xlsx

def _stub_matrix(tmp_path):
    """A file that EXISTS so the exit-1 pre-flight (os.path.isfile) passes
    through to the parameter gates. Never opened: --sf-basis is explicit, so
    Row-10 detection is skipped and the band stop fires in load_config first.

    Built in tmp_path so it exists in the SHIPPED tree too — tests/fixtures/
    *.xlsx is gitignored by design and is absent from release.sh's stage.
    """
    m = tmp_path / "unused-matrix.xlsx"
    m.write_bytes(b"")
    return str(m)


def _argv(tmp_path, **omit):
    """Full, otherwise-valid argv; pass omit=name->True to drop a flag.

    Includes an EXPLICIT --sf-basis (so the SF gate passes without touching the
    matrix), --baseline-confirmed, and VALID scoring xlsx inputs (so the
    scoring-inputs gate passes without touching the matrix), so the only stop
    this triggers is the MISSING-BAND stop in load_config — which fires BEFORE
    the matrix is opened (the exit-1 pre-flight only stats it).
    """
    fw = write_framework_xlsx(str(tmp_path / "framework.xlsx"))
    cs = write_scores_xlsx(str(tmp_path / "scores.xlsx"),
                           ["Pricing", "Scope", "Docs"],
                           [("Any Firm", [7, 7, 7])])
    args = ["--matrix", _stub_matrix(tmp_path), "--project-name", "TEST CLI",
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
