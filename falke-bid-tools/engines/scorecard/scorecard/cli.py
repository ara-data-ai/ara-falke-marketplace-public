"""Command-line entry point for the scorecard skill.

Usage:
  # 1. PREVIEW the cost baseline (echoes it + runs the bid-anchoring check,
  #    renders NOTHING). Review with the owner, then re-run to render.
  python -m scorecard.cli --preview-baseline \
      --matrix "/path/<project> bid matrix.xlsx" \
      --project-name "<Project · Scope>" \
      --sf-basis <SF> --band-low <low $M> --band-high <high $M> --mid <mid $M> \
      --baseline path/to/baseline.json

  # 2. RENDER the scorecard (only after the baseline is confirmed).
  python -m scorecard.cli --baseline-confirmed \
      --matrix "/path/<project> bid matrix.xlsx" \
      --project-name "<Project · Scope>" \
      --sf-basis <SF> --band-low <low $M> --band-high <high $M> --mid <mid $M> \
      --baseline path/to/baseline.json \
      --scoring-framework path/to/scoring-framework.xlsx \
      --category-scores path/to/category-scores.xlsx \
      --out-dir Outputs --refit

REQUIRED each run: --matrix, --project-name, the band (--band-low/--band-high/
--mid), a CONFIRMED SF basis (see the SF gate below), AND the two per-run
scoring xlsx inputs (see the scoring-inputs gate below). The band hard-stops
with MissingParameterError if omitted; --project-name has no default so a new
project can never silently inherit another project's name on a board deliverable.
See examples/sample_run.yaml for the synthetic validation values.

SCORING-INPUTS GATE (REQUIRED, NO FALLBACK): every render needs TWO Falke-filled
xlsx files — the SCORING FRAMEWORK (--scoring-framework, categories/weights/
descriptions; templates/scoring-framework-template.xlsx) and the DETAILED
CATEGORY SCORES (--category-scores, per-bidder 1–10 scores;
templates/category-scores-template.xlsx). They are the SINGLE SOURCE OF TRUTH
for weights and scores (superseding config `weights` and the old --overrides
qual-scores JSON), because every run has different bidders and may carry a
different framework. A render missing either file — or failing their
validation (weights sum 100; score columns match the framework short labels;
every score that IS present is 1–10; firms match the scored bidder field) —
HARD-STOPS (exit 2). --preview-baseline does NOT require them (it renders
nothing).

A BLANK score cell is NOT a validation failure: it means NOT YET SCORED, and the
run renders PROVISIONAL (exit 4 — unranked, no Overall, no named leader). The
only scoring-inputs hard stop about blanks is the degenerate one: a grid with
ZERO scored cells anywhere is the blank template, not a partial evaluation
record. See P1-2 / scoring_inputs.py.

SF-BASIS SUGGEST-AND-CONFIRM GATE (relaxed from the old hard refusal): the skill
now READS the matrix's own Row-10 'TOTAL GSF' and offers it as a SUGGESTED
default — but it NEVER silently renders with it. A render REQUIRES one of:
  * --sf-basis <value>  — explicit override; use this value, no prompt; or
  * --sf-confirmed       — accept the matrix Row-10 GSF as the SF basis.
A render with NEITHER hard-stops (exit 2) with a message naming the matrix SF:
  "[STOP] SF basis not confirmed — the matrix reports <N> SF; re-run with
   --sf-basis <value> to override, or --sf-confirmed to accept the matrix SF."
$/SF is always computed against whichever SF is confirmed. --preview-baseline
echoes the matrix-detected SF (the suggested default) and renders nothing, so the
owner can confirm or override before any card is built.

BASELINE-CONFIRMATION GATE (REQUIRED, mirrors the SF gate): the modeled cost
baseline is the yardstick the whole scorecard measures against, and it can show
signs of being bid-derived. A render run therefore HARD-STOPS (exit 2) unless
--baseline-confirmed is passed. The intended flow is: run --preview-baseline,
review the echo + fingerprint check with the owner, then re-run with
--baseline-confirmed. --preview-baseline renders nothing and ignores
--baseline-confirmed.

RUN PACK (--inputs, P1-4): create-matrix emits one
"<Project> - Scorecard Inputs.xlsx" beside the matrix, with the firms, the
project identity, and the SF suggestion already filled in. Pass it with
--inputs and it supplies the baseline, the framework, the scores, the aliases,
and the exclusions in ONE upload.

  * PRE-FILLED IS NOT PRE-CONFIRMED. --inputs does NOT imply --sf-confirmed and
    does NOT imply --baseline-confirmed. Both gates run exactly as they do
    today, per run, answered by a human. The pack carries DATA; the gates
    consume DECISIONS. The pack schema contains no confirmation field of any
    kind, and an unrecognized Settings key hard-stops (exit 2) — so hand-adding
    one is a stop, not a silence.
  * ONE CHANNEL PER RUN. --inputs is mutually exclusive with --baseline,
    --scoring-framework, --category-scores, --aliases, --exclude and
    --exclusions. Passing both = exit 2. No merge semantics, no precedence
    rules, not even "the flag wins": a pack plus an overriding flag produces a
    card whose Settings tab says one thing and whose baseline says another, and
    precedence rules between two input channels are precisely how the
    --overrides and band-override hazards happened. We have paid for that lesson
    twice.
  * The individual flags remain the ESCAPE HATCH, legitimately and indefinitely:
    legacy workbooks that predate the pack, archival re-renders from hand-built
    inputs, and ARA engineering/debugging.

EXIT CONTRACT v2 (P1-1) — the full table is in scorecard/exit_codes.py, which is
the single source both this CLI and the skill text read:

  0  clean — rendered; audit PASS or PASS-WITH-WARNINGS. Deliverable.
  1  environment / nothing to do — nothing written (e.g. a missing dependency).
  2  input-gate hard stop — nothing written. THE GATE IS WORKING, not an error.
  3  delivered WITH an audit blocker — artifacts exist and every page is
     stamped PRELIMINARY. Lead with the disclosure; never present it as final.
  4  delivered PROVISIONAL — incomplete evaluation record. (The trigger is
     P1-2's; the path and its watermark are built here.)

Precedence 3 > 4. Exit 3 replaces the old, overloaded exit 1 — which meant
"everything was written and you must not deliver it", the exact opposite of the
matrix engine's exit 1, and indistinguishable from a crash.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .baseline_parser import parse_baseline_xlsx
from .config import load_config
from .errors import ScorecardError
from .exit_codes import (EXIT_DELIVERED_PROVISIONAL, EXIT_DELIVERED_WITH_BLOCKER,
                         EXIT_ENVIRONMENT, resolve_exit_code,
                         resolve_watermark, watermark_headline)
from .modeling import refit_all
from .pipeline import audit_run, preview_baseline, render_summary, run_scorecard
from .render import build_context, render_html, render_pdf, write_html
from .run_pack import (PackError, apply_aliases_to_scores, bind_pack_to_matrix,
                       parse_pack, read_matrix_input_exclusions,
                       resolve_pack_aliases, resolve_pack_exclusions)
from .scoring_inputs import parse_category_scores, parse_scoring_framework

# Flags that supply the same facts the run pack supplies. --inputs is mutually
# exclusive with every one of them (Marvin §9.2): one channel per run.
#
# NOT in this list, deliberately: --sf-basis, --sf-confirmed and
# --baseline-confirmed. Those are not input channels — they are GATE DECISIONS,
# and per R1/§5 they always come from the command line, answered by a human, in
# every mode including --inputs. That distinction is the whole reason the pack
# is safe to ship.
#
# The BAND flags are in the list (Boris, 2026-07-16). They were missed on the
# first pass and it was a real defect: --inputs with --band-low 0.50 rendered a
# $0.50M card off a pack whose Baseline tab said $1.00M, at exit 0. Three
# reasons it has to be a hard stop rather than guidance:
#   * §9.2 — the band arriving from two channels with an implicit "the flag
#     wins" is exactly the precedence rule the ruling forbids ("No merge
#     semantics. No precedence rules. Not even 'the flag wins'"). It is how the
#     --overrides and band-override hazards happened; we have paid for it twice.
#   * R6 — one home per fact. The band lives on Baseline. Unambiguously a pack
#     fact.
#   * It defeats the baseline-confirmation gate: the owner confirms the pack's
#     band at preview, and then a flag substitutes a different band into the
#     render. That is the same failure class P0 just closed, re-opened through
#     a side door.
# They stay fully legitimate on the escape-hatch path (an individual --baseline
# xlsx), where they are the documented override — only the pack combination is
# barred.
PACK_CONFLICTING_FLAGS = (
    ("--baseline", "baseline"),
    ("--scoring-framework", "scoring_framework"),
    ("--category-scores", "category_scores"),
    ("--aliases", "aliases"),
    ("--exclude", "exclude"),
    ("--exclusions", "exclusions"),
    ("--band-low", "band_low"),
    ("--band-high", "band_high"),
    ("--mid", "mid"),
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Falke bid-comparison scorecard skill")
    ap.add_argument("--matrix", required=True, help="path to bid-comparison xlsx")
    ap.add_argument("--sheet", default=None,
                    help="EXPLICIT sheet to consume (overrides the default). "
                         "Default (Marvin P0-7 ruling): the Leveled_Normalized "
                         "view when present — the apples-to-apples board "
                         "comparison basis; a single-sheet legacy matrix uses "
                         "its only sheet; a producer workbook missing the "
                         "leveled view hard-stops (exit 2). Use --sheet "
                         "Bid_Form ONLY for reconciliation/verification, "
                         "dispute support, or debugging — a mirror run is an "
                         "internal artifact, never the board deliverable when "
                         "a leveled sheet exists. The consumed sheet + its "
                         "disclosure line are printed on the card and recorded "
                         "in scorecard_run.json.")
    ap.add_argument("--inputs", default=None,
                    help="THE SINGLE-UPLOAD PATH: the "
                         "'<Project> - Scorecard Inputs.xlsx' run pack that "
                         "create-matrix emitted beside this matrix (tabs: "
                         "Settings | Baseline | Framework | Scores). Supplies "
                         "the baseline, the framework, the scores, the display "
                         "aliases and the exclusions in one file, with the firm "
                         "names already filled in from the matrix — so nobody "
                         "re-types them. MUTUALLY EXCLUSIVE with --baseline / "
                         "--scoring-framework / --category-scores / --aliases / "
                         "--exclude / --exclusions (passing both = exit 2; one "
                         "channel per run). Does NOT confirm anything: the SF "
                         "gate and the baseline gate still require an explicit "
                         "human decision on this command line, every run.")
    ap.add_argument("--config", default=None, help="path to scorecard_config.yaml")
    # REQUIRED parameters (also can live in config run_inputs)
    ap.add_argument("--sf-basis", type=float, default=None,
                    help="$/SF area basis — EXPLICIT OVERRIDE. When supplied it "
                         "is used as-is (no prompt). When omitted, the matrix's "
                         "Row-10 GSF is the suggested default and a render "
                         "requires --sf-confirmed (see the SF gate).")
    ap.add_argument("--sf-confirmed", action="store_true",
                    help="accept the matrix's Row-10 'TOTAL GSF' as the SF basis. "
                         "Required to render when --sf-basis is NOT supplied; "
                         "ignored when --sf-basis IS supplied (the explicit value "
                         "wins) and by --preview-baseline.")
    ap.add_argument("--band-low", type=float, default=None, help="band low $M")
    ap.add_argument("--band-high", type=float, default=None, help="band high $M")
    ap.add_argument("--mid", type=float, default=None,
                    help="modeled mid (takeoff) $M")
    ap.add_argument("--variance-mid", type=float, default=None,
                    help="Section C variance reference $M (default=band center)")
    # PRESENTATION labels (NOT modeled). Defaults to generic region labels.
    ap.add_argument("--region", default=None,
                    help='short region label for the cost-band chip + Section A '
                         'title, e.g. "South FL" (default "South FL")')
    ap.add_argument("--region-full", default=None,
                    help='long region label for the Section A title, e.g. '
                         '"South Florida" (default: the --region value, or '
                         '"South Florida" when region is "South FL")')
    ap.add_argument("--baseline-year", type=int, default=None,
                    help="baseline/pricing year shown in the Section A title "
                         "(default: the current year)")
    ap.add_argument("--baseline", default=None,
                    help="JSON or xlsx baseline file of Section A baseline lines "
                         "(PARAMETER). xlsx files use the baseline-template.xlsx "
                         "format and may supply the band values internally.")
    ap.add_argument("--qual-notes", default=None,
                    help="JSON file of per-bidder qualitative notes")
    ap.add_argument("--scoring-framework", default=None,
                    help="REQUIRED for any render (NO fallback): Falke-filled "
                         "scoring-framework xlsx (templates/scoring-framework-"
                         "template.xlsx format; sheet Scoring_Framework — "
                         "Category | Short Label | Weight (%%) | What it "
                         "captures; weights must sum to 100). The single "
                         "source of truth for Section D categories/weights "
                         "and the Overall /100 weighting. A render without it "
                         "hard-stops (exit 2). Not needed by "
                         "--preview-baseline.")
    ap.add_argument("--category-scores", default=None,
                    help="REQUIRED for any render (NO fallback): Falke-filled "
                         "category-scores xlsx (templates/category-scores-"
                         "template.xlsx format; sheet Category_Scores — Firm | "
                         "one 1–10 column per framework Short Label; one row "
                         "per SCORED bidder; Overall /100 is COMPUTED, never "
                         "supplied). The single source of truth for Section E "
                         "scores. A render without it hard-stops (exit 2). "
                         "Not needed by --preview-baseline.")
    ap.add_argument("--overrides", default=None,
                    help="DEPRECATED / superseded: the old per-bidder category "
                         "qual-scores JSON. Weights and 1–10 scores now come "
                         "SOLELY from --scoring-framework / --category-scores; "
                         "supplying --overrides hard-stops (exit 2) so scores "
                         "can never arrive from two sources.")
    ap.add_argument("--exclude", default=None,
                    help="comma-separated bidder names to EXCLUDE from the scored "
                         "field per a human ruling (matched on normalized name), "
                         'e.g. --exclude "Harbor Builders Inc.,Borealis Builders '
                         'Solutions". Default = include all & flag (Marvin §1.4).')
    ap.add_argument("--exclusions", default=None,
                    help='JSON file with an exclusions list, either ["Name", ...] '
                         'or {"exclude": ["Name", ...]}. Merged with --exclude.')
    ap.add_argument("--aliases", default=None,
                    help='JSON file mapping raw/normalized firm name -> short '
                         'display name, e.g. {"Acme Restoration": "Acme"}. '
                         'Applied to the DISPLAYED bidder name; the raw matrix '
                         'name is retained in the run log for audit (Marvin §1.5).'
                         ' Merged over config["aliases"] (this file wins). '
                         'Default = no rename.')
    ap.add_argument("--project-name", required=True,
                    help="project title shown on the board scorecard (REQUIRED — "
                         "no default, so a new project can never silently inherit "
                         "another project's name on a board deliverable). "
                         'e.g. --project-name "Sample Condominium · Lobby Renovation"')
    ap.add_argument("--out-dir", default=".", help="output directory")
    ap.add_argument("--engine", default="chromium",
                    choices=["chromium", "auto", "weasyprint"],
                    help="PDF engine (default chromium — installed in Falke env; "
                         "weasyprint is an optional alternative)")
    ap.add_argument("--refit", action="store_true",
                    help="re-fit the Section C models (volatility + drift) "
                         "with scipy and print vs Darvish ranges (FIRST build "
                         "step). The Overall presentation curve is RETIRED "
                         "(P0-6) — Overall is the honest weighted average.")
    ap.add_argument("--html-only", action="store_true",
                    help="emit HTML, skip PDF (useful when no PDF engine)")
    ap.add_argument("--audit", dest="audit", action="store_true", default=True,
                    help="run the deterministic self-audit BEFORE the artifacts "
                         "are rendered (DEFAULT ON); writes audit_report.md + "
                         "audit.json. Running it first is what lets a blocked "
                         "run render its own PRELIMINARY watermark.")
    ap.add_argument("--no-audit", dest="audit", action="store_false",
                    help="skip the self-audit. PROHIBITED for board runs. The "
                         "artifacts are still produced, but every page is "
                         "stamped 'PRELIMINARY — not audited' so an unaudited "
                         "card can never be mistaken for a checked one. For "
                         "debugging only.")
    ap.add_argument("--preview-baseline", action="store_true",
                    help="ECHO the supplied cost baseline (trade lines, "
                         "subtotals, OH&P, band in $M AND $/SF) + run the "
                         "bid-anchoring fingerprint check, then EXIT 0 WITHOUT "
                         "rendering a scorecard. Review with the owner first.")
    ap.add_argument("--baseline-confirmed", action="store_true",
                    help="REQUIRED to render (mirrors --sf-basis): confirms the "
                         "owner reviewed the baseline via --preview-baseline. "
                         "Without it a render run HARD-STOPS (exit 2). Ignored "
                         "by --preview-baseline.")
    args = ap.parse_args(argv)

    # ---- EXIT 1 — ENVIRONMENT / NOTHING TO DO (P1-1, exit-contract v2).
    # An unreadable --matrix is the one input whose open was never guarded: it
    # raised a raw FileNotFoundError out of openpyxl, uncaught, as a traceback
    # at exit 1. That is the same shape as Floyd's C-2 (a good failure delivered
    # as a crash), and leaving it after an item whose subject IS the exit
    # contract would be the same miss twice.
    #
    # Exit 1, not 2, per Boris §D's table ("bad matrix path") and Floyd's
    # verdict (e) ("1 = environment/nothing-written"), which adopts that shape
    # exactly. It is deliberately NOT the exit-2 fiduciary-gate framing: a
    # typo'd path is not "the gate working", and telling the operator it was
    # would teach them to distrust the message that matters. Every OTHER input
    # file (--inputs, --baseline, --scoring-framework, --category-scores) is
    # already guarded and correctly reports exit 2 — verified.
    if not os.path.isfile(args.matrix):
        print(f"[STOP] Matrix workbook not found: {args.matrix!r}. Nothing was "
              f"read and nothing was written — check the path and re-run.",
              file=sys.stderr)
        return EXIT_ENVIRONMENT

    # ---- RUN-PACK MUTUAL EXCLUSION (Marvin §9.2). One channel per run: no
    # merge semantics, no precedence rules. The tempting case is a partial
    # correction ("good pack, but let me swap a corrected baseline") — rejected
    # on purpose. If the baseline changed, edit the pack's Baseline tab.
    if args.inputs:
        # `is not None`, not truthiness: the band flags are floats, and
        # `--band-low 0` is falsy. A conflict check that a zero slips through is
        # not a check. The predicate we want is "was the flag supplied", which
        # is exactly what argparse's None default encodes.
        #
        # No getattr default (Floyd F-5): a misspelled dest would silently
        # return None and produce a DEAD CHECK that no test would catch — the
        # exact failure mode the band miss already cost us once. Without the
        # default, a typo raises AttributeError on the first pack run instead.
        conflicts = [flag for flag, dest in PACK_CONFLICTING_FLAGS
                     if getattr(args, dest) is not None]
        if conflicts:
            print(f"[STOP] --inputs supplies the same facts as "
                  f"{', '.join(conflicts)} — pass one channel or the other, not "
                  f"both. There are deliberately no precedence rules between "
                  f"them: a pack plus an overriding flag produces a card whose "
                  f"Settings tab says one thing and whose inputs say another. "
                  f"If the pack's contents are wrong, edit the pack.",
                  file=sys.stderr)
            return 2

    # ---- RUN-PACK PARSE (the single-upload path). Structure, required fields,
    # and unknown-key rejection all land here as exit 2, BEFORE any gate — a
    # pack we cannot read is not a pack we should reason about.
    pack = None
    if args.inputs:
        try:
            pack = parse_pack(args.inputs)
        # ValueError, not PackError (Floyd C-2). PackError IS a ValueError, so
        # this is strictly wider and loses nothing. It has to be wider: the
        # three parsers extracted so the pack and the individual flags run the
        # SAME code — parse_framework_table / parse_scores_table /
        # parse_baseline_sheet — raise plain ValueError with good, actionable
        # messages, and catching only PackError let every one of them escape as
        # a traceback at exit 1. The individual-flag path below already catches
        # ValueError and was always right; the refactor moved the code and left
        # the guard behind. That is the whole defect: same parsers, two nets.
        #
        # It fires on the likeliest slips, on the tabs Marvin deliberately left
        # editable — weights that don't sum to 100, and a forgotten cell in the
        # pre-filled 8xN score grid.
        except ValueError as exc:
            print(f"[STOP] {exc}", file=sys.stderr)
            return 2

    # ---- XLSX BASELINE PRE-PARSE. When --baseline points to an xlsx/xlsm file,
    # parse it now (before the SF gate and config load) so xlsx-derived band
    # values are available as fallbacks in overrides_inputs. CLI flags always win.
    _xlsx_band_low = None
    _xlsx_band_high = None
    _xlsx_band_mid = None
    _baseline_lines = None
    _baseline_is_xlsx = (
        args.baseline is not None
        and os.path.splitext(args.baseline)[1].lower() in (".xlsx", ".xlsm")
    )
    if _baseline_is_xlsx:
        try:
            _xlsx_band_low, _xlsx_band_high, _xlsx_band_mid, _baseline_lines = (
                parse_baseline_xlsx(args.baseline)
            )
        except ValueError as exc:
            print(f"[STOP] {exc}", file=sys.stderr)
            return 2

    # ---- SCORING-INPUTS GATE (REQUIRED for any render; NO fallback). The
    # scoring framework and the detailed category scores are per-run Falke
    # inputs — every run has different bidders and may carry a different
    # framework, so there is NO default and nothing is reused from a previous
    # run. --preview-baseline (renders nothing) is exempt. The two files
    # supersede config weights and the old --overrides qual-scores JSON.
    framework = None
    category_scores = None
    if pack is not None:
        # The pack satisfies the scoring-inputs gate the same way the two files
        # do — by SUPPLYING the framework and the scores. It does not bypass it:
        # a pack whose Framework or Scores tab is unusable already exited 2 in
        # parse_pack, with the same [STOP] shape and the same actionable text.
        framework = pack.framework
        category_scores = pack.category_scores
        _baseline_lines = pack.baseline_lines
        _xlsx_band_low = pack.band_low
        _xlsx_band_high = pack.band_high
        _xlsx_band_mid = pack.band_mid
        # The pack's Baseline tab IS an xlsx baseline — same parser, same
        # semantics (baseline_parser.parse_baseline_sheet), just a different
        # workbook. Everything downstream treats it identically.
        _baseline_is_xlsx = True
    elif not args.preview_baseline:
        if not args.scoring_framework:
            print("[STOP] Scoring Framework not provided — the scorecard "
                  "CANNOT be produced without it. There is no default. Fill "
                  "out scoring-framework-template.xlsx and re-run with "
                  "--scoring-framework <path>.", file=sys.stderr)
            return 2
        if not args.category_scores:
            print("[STOP] Detailed Category Scores not provided — the "
                  "scorecard CANNOT be produced without them. There is no "
                  "default. Fill out category-scores-template.xlsx and re-run "
                  "with --category-scores <path>.", file=sys.stderr)
            return 2
        if args.overrides:
            print("[STOP] --overrides is superseded — category weights and "
                  "1–10 scores now come SOLELY from --scoring-framework / "
                  "--category-scores (the single source of truth). Move the "
                  "scores into the category-scores xlsx and drop --overrides.",
                  file=sys.stderr)
            return 2
        try:
            framework = parse_scoring_framework(args.scoring_framework)
            category_scores = parse_category_scores(
                args.category_scores, framework)
        except ValueError as exc:
            print(f"[STOP] {exc}", file=sys.stderr)
            return 2

    # ---- SF-BASIS SUGGEST-AND-CONFIRM GATE. Resolve the SF basis BEFORE config
    # so $/SF is always computed against a CONFIRMED value. Reads the matrix's
    # own Row-10 GSF as the suggested default; the gate then decides:
    #   * --sf-basis supplied  -> explicit override (used as-is);
    #   * --sf-confirmed (no --sf-basis) -> accept the matrix GSF;
    #   * preview mode          -> use explicit if given else the matrix GSF, and
    #                              surface the suggestion (renders nothing);
    #   * a RENDER with neither -> hard-stop (exit 2) naming the matrix SF.
    # The matrix GSF is detected with the SAME detector the full parse uses
    # (MatrixParser.detect_sf), so the suggested value matches what the audit
    # later sees.
    try:
        # validate=False: we only need the static matrix block to detect the
        # Row-10 GSF; sf_basis/band are not yet resolved here.
        cfg_probe = load_config(args.config, validate=False)
    except ScorecardError:
        cfg_probe = None
    matrix_gsf = None
    if args.sf_basis is None and cfg_probe is not None:
        try:
            from .matrix import MatrixParser
            _, matrix_gsf = MatrixParser(cfg_probe.block("matrix")).detect_sf(
                args.matrix)
        except ScorecardError as e:
            # a missing/unreadable matrix is reported the same way the parse path
            # would report it; the gate below still asks the user to act.
            print(f"[STOP] {e}", file=sys.stderr)
            return 2

    # ---- PACK <-> MATRIX BINDING (§8.3). Runs BEFORE the gates and before any
    # artifact: I3 (wrong building), I6 (roster mismatch) and I8 (an edited
    # producer field) are hard stops, and a hard stop after a render is just a
    # mess on disk. The confirmable tiers (I5 different-but-reconciling run,
    # I7 unstamped matrix) return log lines instead and are carried into the
    # audit (C22) and the run json.
    pack_log: list = []
    pack_exclude = None
    pack_aliases = None
    if pack is not None and cfg_probe is not None:
        try:
            from .matrix import MatrixParser
            probe = MatrixParser(cfg_probe.block("matrix")).parse(args.matrix)
        except ScorecardError as e:
            print(f"[STOP] {e}", file=sys.stderr)
            return 2
        roster = [b.raw_name for b in probe.blocks]
        try:
            pack_log = bind_pack_to_matrix(
                pack, probe,
                matrix_stamp=probe.producer_stamp,
                matrix_project_name=(probe.producer_stamp or {}).get(
                    "project_name", ""),
                matrix_project_address=(probe.producer_stamp or {}).get(
                    "project_address", ""),
                matrix_exclusions=read_matrix_input_exclusions(args.matrix),
            )
            pack_exclude = resolve_pack_exclusions(pack, roster)
            pack_aliases = resolve_pack_aliases(pack, roster)
            # The pack's Scores tab carries the matrix's raw names; everything
            # downstream keys off the displayed name. Re-key once, here.
            category_scores = apply_aliases_to_scores(
                category_scores, pack_aliases)
        # ValueError for the same reason as the parse guard above (Floyd C-2):
        # strictly wider, and PackError is a ValueError.
        except ValueError as exc:
            print(f"[STOP] {exc}", file=sys.stderr)
            return 2
        # I4 (bound, clean) is not a warning — only the tiers that ask the
        # operator to confirm something are. Crying wolf on the clean path is
        # how a control gets trained out of an operator.
        needs_confirm = bool((pack.binding or {}).get("confirmed_required"))
        for line in pack_log:
            print(f"[WARN] {line}" if needs_confirm else line,
                  file=sys.stderr if needs_confirm else sys.stdout)

        # SF echo vs matrix (§5.5). The pack's SF cell is an ADVISORY ECHO. If
        # it diverges from the matrix, one of them is wrong — surface it, do
        # NOT hard-stop and do NOT adopt it. Editing that cell is an override
        # PROPOSAL, semantically identical to --sf-basis, and it still requires
        # an explicit decision on this command line. A suggestion does not
        # become a confirmation by traveling through a spreadsheet.
        if (pack.sf_basis_value is not None and probe.gsf_value is not None
                and abs(pack.sf_basis_value - probe.gsf_value) > 0.5):
            print(f"[WARN] The run pack's SF echo ({pack.sf_basis_value:,.0f} "
                  f"SF) differs from the matrix ({probe.gsf_value:,.0f} SF). "
                  f"The pack's value is a PROPOSAL, not a confirmation — it is "
                  f"not used unless you pass it explicitly with --sf-basis "
                  f"{pack.sf_basis_value:g}. Review which denominator is right "
                  f"for this comparison before you decide.", file=sys.stderr)

    if args.sf_basis is not None:
        sf_basis, sf_source = args.sf_basis, "explicit"
    elif args.preview_baseline:
        # preview never blocks; show the matrix GSF as the suggested basis.
        sf_basis, sf_source = matrix_gsf, "matrix-confirmed"
    elif args.sf_confirmed:
        sf_basis, sf_source = matrix_gsf, "matrix-confirmed"
    else:
        # RENDER with neither an explicit basis nor confirmation -> suggest+stop.
        if matrix_gsf is None:
            print("[STOP] SF basis not confirmed and the matrix reports no "
                  "Row-10 'TOTAL GSF' to suggest — re-run with --sf-basis "
                  "<value> to set it explicitly.", file=sys.stderr)
        else:
            print(f"[STOP] SF basis not confirmed — the matrix reports "
                  f"{matrix_gsf:,.0f} SF; re-run with --sf-basis <value> to "
                  f"override, or --sf-confirmed to accept the matrix SF.",
                  file=sys.stderr)
        return 2

    # When confirming/previewing the matrix GSF but none was detected, there is
    # nothing to confirm — STOP rather than fall through to a None-basis config.
    if sf_basis is None:
        print("[STOP] --sf-confirmed given but the matrix reports no Row-10 "
              "'TOTAL GSF' to confirm — supply --sf-basis <value> explicitly.",
              file=sys.stderr)
        return 2

    # CLI flags win over xlsx-derived band values (explicit override).
    # When an xlsx baseline was supplied and a flag is absent (None), fall back
    # to the xlsx-derived value so Falke only needs to fill one file per job.
    overrides_inputs = {
        "sf_basis": sf_basis,
        "sf_source": sf_source,
        "band_low": args.band_low if args.band_low is not None else _xlsx_band_low,
        "band_high": args.band_high if args.band_high is not None else _xlsx_band_high,
        "modeled_mid_takeoff": args.mid if args.mid is not None else _xlsx_band_mid,
        "variance_mid": args.variance_mid,
        "region": args.region,
        "region_full": args.region_full,
        "pricing_year": args.baseline_year,
    }

    try:
        cfg = load_config(args.config, overrides=overrides_inputs)
    except ScorecardError as e:
        print(f"[STOP] {e}", file=sys.stderr)
        return 2

    # ---- PREVIEW MODE: echo the baseline + run the fingerprint check, then
    # EXIT 0 without rendering. The owner SEES the yardstick (incl. the
    # matrix-suggested SF basis) before any card is built. (--baseline-confirmed
    # and --sf-confirmed are both ignored here.)
    if args.preview_baseline:
        try:
            preview = preview_baseline(
                args.matrix, cfg,
                baseline_lines=_baseline_lines if _baseline_is_xlsx
                               else _load_json(args.baseline),
                sheet=args.sheet)
        except ScorecardError as e:
            print(f"[STOP] {e}", file=sys.stderr)
            return 2
        for line in preview["echo"]:
            print(line)
        print("\n(No scorecard rendered — review the baseline AND the SF basis "
              "with the owner, then re-run with --baseline-confirmed plus either "
              "--sf-basis <value> or --sf-confirmed to build the card.)")
        return 0

    # ---- BASELINE-CONFIRMATION GATE (REQUIRED to render; mirrors the SF gate).
    # The cost baseline is the yardstick the scorecard measures against and can
    # be bid-derived; it must be confirmed each run.
    if not args.baseline_confirmed:
        print("[STOP] Baseline not confirmed — run with --preview-baseline, "
              "review it with the owner, then re-run with --baseline-confirmed. "
              "(The cost baseline is the yardstick the scorecard measures "
              "against; it must be confirmed each run.)", file=sys.stderr)
        return 2

    if args.refit:
        print("=== CURVE RE-FIT (scipy.optimize.least_squares) ===")
        for rr in refit_all(cfg.run.variance_mid):
            print(f"\n[{rr.name}] params={rr.params}")
            print(f"  max|resid|={rr.max_abs_residual} mean|resid|={rr.mean_abs_residual}")
            print(f"  in_range={rr.in_range}")
            print(f"  note: {rr.notes}")
        print()

    baseline_lines = (_baseline_lines if _baseline_is_xlsx
                      else _load_json(args.baseline))
    qual_notes = _load_json(args.qual_notes)
    if pack is not None:
        # R6, one home per fact: with a pack, aliases and exclusions come from
        # the Settings tab and nowhere else. The mutual-exclusion check above
        # already refused the flags, so there is nothing to merge here.
        exclude = pack_exclude
        aliases = pack_aliases
    else:
        exclude = _parse_exclusions(args.exclude, _load_json(args.exclusions))
        aliases = _load_json(args.aliases)

    try:
        result = run_scorecard(
            args.matrix, cfg,
            baseline_lines=baseline_lines,
            qualitative_notes=qual_notes,
            exclude=exclude,
            aliases=aliases,
            project_name=args.project_name,
            framework=framework,
            category_scores=category_scores,
            sheet=args.sheet,
        )
    except ScorecardError as e:
        print(f"[STOP] {e}", file=sys.stderr)
        return 2

    # ---- Carry the pack's facts onto the result so the audit can judge them
    # (C19-C22) and the card can disclose the framework declaration (Section H).
    # `input_channel` is §9.3: record HOW the inputs arrived, every run.
    result["pack"] = pack
    result["input_channel"] = "pack" if pack is not None else "individual"
    result["log"].extend(pack_log)

    os.makedirs(args.out_dir, exist_ok=True)
    print("=== RUN LOG ===")
    for line in result["log"]:
        print("  " + line)

    # ---- SELF-AUDIT FIRST (P1-1; Floyd verdict (e) + C-R12) ----------------
    # The audit USED to run after the artifacts were written, which left a
    # board-plausible, clean-looking PDF on disk beside a FAIL verdict. Nothing
    # about the audit needed that order: audit_run reads only the pipeline
    # result and never the rendered HTML — the old ordering was a habit, not a
    # dependency.
    #
    # Running it first is what makes the watermark possible at all: the render
    # cannot disclose a verdict that does not exist yet. And the watermark is
    # not optional garnish — exit 3's contract REQUIRES the artifacts to exist
    # ("delivered WITH audit blocker"), so we are not permitted to withhold
    # them. If the artifact must exist and must not be trusted, the artifact
    # itself has to say so.
    # C12(d) asserts the artifacts carry the 'evaluation incomplete' mark, and
    # C12(c) that the summary names no leader — but the audit now runs BEFORE
    # both the watermark and the render. Neither needs the audit to exist:
    #
    #   * the COVERAGE half of the watermark is already on the result —
    #     run_scorecard set it (it depends only on full_coverage, which the
    #     pipeline knows). The FULL list is composed below, once the verdict
    #     exists.
    #   * build_summary_context is pure and does not need the watermark to
    #     decide whether it names a leader.
    #
    # This keeps C12 non-circular: it never judges a verdict it is part of.
    full_coverage = bool(result.get("full_coverage", True))
    summary_ctx = None
    try:
        from .summary import build_summary_context
        summary_ctx = build_summary_context(result)
    except Exception:
        # a summary that cannot be built is the summary render's problem to
        # report, not the audit's to crash on.
        summary_ctx = None

    ar = None
    if args.audit:
        ar, audit_paths = audit_run(result, cfg, args.out_dir, aliases=aliases,
                                    summary_context=summary_ctx)
        print(f"AUDIT -> {audit_paths['report_md']}")
        print(f"AUDIT -> {audit_paths['audit_json']}")
        print(f"\n=== SELF-AUDIT VERDICT: {ar.verdict} "
              f"({ar.counts['blocker']} blocker(s), {ar.counts['warn']} "
              f"warning(s), {ar.counts['info']} info) ===")

    audit_verdict = ar.verdict if ar is not None else None
    # the FULL list now the verdict exists (coverage reasons + audit reasons)
    watermark = resolve_watermark(audit_verdict=audit_verdict,
                                  full_coverage=full_coverage)
    result["watermark"] = watermark
    exit_code = resolve_exit_code(audit_verdict=audit_verdict,
                                  full_coverage=full_coverage)
    if watermark:
        print(f"\n[{watermark_headline(watermark)}] — every rendered artifact "
              f"carries this on every page.", file=sys.stderr)

    ctx = build_context(result, cfg, watermark=watermark)
    html = render_html(ctx)
    # ---- DISTINCT FILENAME for a non-deliverable artifact (Marvin P1-2 §2.3)
    # The PDF is the thing that TRAVELS. Two files named scorecard.pdf with
    # different scores in the same mailbox is exactly the version confusion that
    # gets an award challenged — so an artifact that is not deliverable does not
    # get the deliverable's name.
    #
    # Keyed to the WATERMARK, not to coverage alone: Marvin ruled the filename
    # for the provisional case, but a blocked run (exit 3) is equally
    # not-for-distribution and carries the same mailbox hazard. One rule — "if
    # it is stamped PRELIMINARY, it is not called scorecard" — rather than two.
    # Flagged for his and Floyd's ruling as a generalization of his §2.3.
    #
    # scorecard_run.json and the audit artifacts KEEP their stable names: they
    # are machine-read, they never travel to a board, and the skill and tests
    # resolve them by path.
    card_name = "scorecard-PRELIMINARY" if watermark else "scorecard"
    base = os.path.join(args.out_dir, card_name)
    write_html(html, base + ".html")
    print(f"\nHTML -> {base}.html")
    if not args.html_only:
        try:
            render_pdf(html, base + ".pdf", engine=args.engine)
            print(f"PDF  -> {base}.pdf")
        except ScorecardError as e:
            print(f"[WARN] PDF render skipped: {e}", file=sys.stderr)

    # provenance JSON for the board / audit trail
    pack_json = None
    if pack is not None:
        # The declaration and the hash comparison land in scorecard_run.json
        # EVERY RUN (§4.5). The provenance block is recorded but not yet
        # rendered — P1-6 keys the document language to it; at P1-4 the award
        # file simply carries the estimator of record from the first pack run
        # forward, which is what makes P1-6 purely additive (§6.2).
        pack_json = {
            "pack_file": os.path.basename(pack.path),
            "pack_format_version": pack.pack_format_version,
            "matrix_run_id": pack.matrix_run_id,
            "matrix_file_name": pack.matrix_file_name,
            "emitted_at": pack.emitted_at,
            "binding": pack.binding,
            "bid_opening_date": pack.bid_opening_date,
            "addenda_through": pack.addenda_through,
            "scoring_completed_date": pack.scoring_completed_date,
            "framework_basis": pack.framework_basis,
            "framework_lock_date": pack.framework_lock_date,
            "framework_ruling_note": pack.framework_ruling_note,
            "framework_hash": pack.framework_hash,
            "standing_framework_version": pack.standing_version,
            "standing_framework_effective_date": pack.standing_effective_date,
            "standing_framework_hash": pack.standing_hash,
            "standing_framework_available": pack.standing_available,
            "baseline_provenance": pack.baseline_provenance,
            "matrix_exclusions": [
                {"firm": f, "reason": r} for f, r in pack.matrix_exclusions],
            "additional_exclusions": [
                {"firm": f, "reason": r} for f, r in pack.additional_exclusions],
        }
    run_json_path = os.path.join(args.out_dir, "scorecard_run.json")
    with open(run_json_path, "w", encoding="utf-8") as fh:
        json.dump({
            "run_id": result["meta"]["run_id"],
            "full_coverage": result["full_coverage"],
            "overall_label": result["overall_label"],
            # exit-contract v2 (P1-1): the code this run returns and WHY, so the
            # award file records the delivery status rather than leaving it to
            # a shell exit nobody archives.
            "exit_code": exit_code,
            "audit_verdict": audit_verdict,
            "watermark": [r["token"] for r in watermark],
            # HOW the inputs arrived (§9.3). Cheap, honest, and it puts the
            # provenance in the award file rather than relying on prose the
            # operator may never read.
            "input_channel": result["input_channel"],
            "pack": pack_json,
            # consumed-sheet provenance (Marvin P0-7): name, mode, and the
            # exact disclosure line the card renders.
            "sheet": result.get("sheet"),
            "log": result["log"],
            "bidders": [{
                # `.get` because a provisional run carries NO rank key at all —
                # the rank is absent, not blank (Marvin P1-2 §3.3.2). The json
                # records null so a reader can tell "not ranked" from "rank 0",
                # which is the same reason blanks are None-and-never-omitted.
                "name": b["name"], "rank": b.get("rank"), "total": b["total"],
                "per_sf": b["per_sf"], "tier": b["tier"],
                "overall": b["overall"],
            } for b in result["bidders"]],
        }, fh, indent=2, default=str)
    print(f"JSON -> {run_json_path}")

    # ---- Scorecard Summary companion (plain-English; matched-set, every run) ----
    try:
        summary_paths = render_summary(
            result, args.out_dir, engine=args.engine, html_only=args.html_only,
            watermark=watermark)
        print(f"SUMMARY -> {summary_paths['summary_html']}")
        if "summary_pdf" in summary_paths:
            print(f"SUMMARY -> {summary_paths['summary_pdf']}")
    except ScorecardError as e:
        print(f"[WARN] summary render skipped: {e}", file=sys.stderr)

    # ---- EXIT-CONTRACT v2 (P1-1, Floyd verdict (e)) ------------------------
    # 3 = delivered WITH an audit blocker; 4 = delivered PROVISIONAL; 0 = clean.
    # Precedence 3 > 4 lives in resolve_exit_code, not here.
    #
    # Exit 3 replaces the old exit 1, which is the whole point of this item: 1
    # meant "everything was written, do not deliver it" — the exact opposite of
    # the matrix engine's 1 ("environment, nothing written"), and an
    # orchestrating skill that learned matrix habits would mis-handle it.
    if exit_code == EXIT_DELIVERED_WITH_BLOCKER:
        print("[FAIL] Self-audit found a BLOCKER — do NOT deliver this "
              "scorecard as final; remediate and re-run. The artifacts exist "
              "and every page is stamped PRELIMINARY.", file=sys.stderr)
    elif exit_code == EXIT_DELIVERED_PROVISIONAL:
        print("[PROVISIONAL] Delivered on an incomplete evaluation record — "
              "this is a working document, not an award document.",
              file=sys.stderr)
    return exit_code


def _load_json(path):
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _parse_exclusions(exclude_csv, exclusions_json):
    """Merge --exclude (comma-separated) and --exclusions (JSON list or
    {"exclude": [...]}) into a de-duplicated list of names. Returns None when no
    exclusions are supplied (preserving the include-all default)."""
    names = []
    if exclude_csv:
        names.extend(n.strip() for n in str(exclude_csv).split(",") if n.strip())
    if exclusions_json:
        items = (exclusions_json.get("exclude")
                 if isinstance(exclusions_json, dict) else exclusions_json)
        if isinstance(items, list):
            names.extend(str(n).strip() for n in items if str(n).strip())
    # de-dup preserving order
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out or None


if __name__ == "__main__":
    raise SystemExit(main())
