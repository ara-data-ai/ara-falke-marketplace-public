#!/usr/bin/env bash
# =============================================================================
# Scorecard SKILL behavior eval  (distinct from the 40 modeling pytest tests)
# =============================================================================
# Verifies the SKILL itself is well-formed and that a sample invocation produces
# the expected artifacts. NOT a model/curve test — that is tests/test_*.py.
#
# Run:
#   bash eval/run_eval.sh
# Exit 0 = pass, non-zero = fail (count of failed checks).
#
# Env:
#   SCORECARD_PYTHON=/path/to/python   explicit engine-interpreter override
#   REQUIRE_PHASE_B=1                  a missing Phase-B matrix fixture is a
#                                      FAIL instead of a SKIP (release.sh sets
#                                      this on the canonical-tree gate, where
#                                      the untracked fixture must exist)
#
# Two phases:
#   A. STATIC skill hygiene — frontmatter + progressive-disclosure structure +
#      trigger / audit / upload-detection / scoring-gate language.
#   B. INVOCATION smoke — full gated render (SF confirm + baseline confirm +
#      both scoring inputs) on a synthetic producer-written fixture, HTML-only
#      so no PDF engine is required; asserts the artifacts exist, the run JSON
#      carries a coverage flag, the default-ON audit wrote audit_report.md,
#      and the SF gate hard-stops AT THE SF GATE (stop message asserted).
#
# Phase-B fixture boundary (do not "upgrade" this in passing):
#   The smoke runs against tests/fixtures/create_matrix_4bidders.xlsx — a fully
#   SYNTHETIC workbook written by a v0.3-era run of the in-plugin matrix
#   producer, now the scorecard suite's BACK-COMPAT pin (P0-2 landed: the LIVE
#   cross-engine producer→parser compat gate is
#   engines/scorecard/tests/test_producer_live_compat.py, which generates
#   fresh workbooks with the CURRENT matrix engine on every pytest run and
#   rides release.sh's existing pytest gates). This smoke stays on the pin —
#   it tests the SKILL wiring, not producer freshness; the pytest gate owns
#   freshness. The fixture is untracked-local by .gitignore (*.xlsx), so
#   Phase B self-skips in the scrubbed release stage and in the public bundle;
#   the canonical-tree release gate runs it with REQUIRE_PHASE_B=1.
# =============================================================================
set -u

# Resolve paths relative to this script so the eval is location-independent
# and fully self-contained inside the plugin bundle.
EVAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$EVAL_DIR/.." && pwd)"
SKILL_MD="$SKILL_DIR/SKILL.md"
RUNBOOK_MD="$SKILL_DIR/reference/runbook.md"
# Bundle root is two levels up from skills/build-scorecard/.
BUNDLE_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
ENGINE_DIR="$BUNDLE_ROOT/engines/scorecard"
# Synthetic producer-written fixture (see the Phase-B boundary note above).
MATRIX="$ENGINE_DIR/tests/fixtures/create_matrix_4bidders.xlsx"
OUT="$(mktemp -d 2>/dev/null || echo /tmp/scorecard_eval_out)"
mkdir -p "$OUT"

# Engine interpreter — resolution order (each step named in failures):
#   1. SCORECARD_PYTHON        explicit override (CI / dev machines)
#   2. plugin venv             ${CLAUDE_PLUGIN_DATA}/venv/bin/python — what
#                              scripts/bootstrap.sh installs and the runbook
#                              mandates for real runs
#   3. /usr/bin/python3        system fallback (canonical dev tree; bare
#                              `python3` can resolve to a Homebrew build
#                              without the engine deps — never use it)
if [ -n "${SCORECARD_PYTHON:-}" ]; then
  PYTHON_BIN="$SCORECARD_PYTHON"
  PY_HOW="SCORECARD_PYTHON env override"
elif [ -n "${CLAUDE_PLUGIN_DATA:-}" ] && [ -x "${CLAUDE_PLUGIN_DATA}/venv/bin/python" ]; then
  PYTHON_BIN="${CLAUDE_PLUGIN_DATA}/venv/bin/python"
  PY_HOW="plugin venv (\${CLAUDE_PLUGIN_DATA}/venv, bootstrap.sh)"
else
  PYTHON_BIN="/usr/bin/python3"
  PY_HOW="system fallback /usr/bin/python3"
fi

FAIL=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

# flat <file> — the file as ONE normalized line: markdown emphasis/backticks
# stripped, whitespace collapsed. Prose checks grep THIS, not the raw file.
#
# Why: these docs are wrapped prose. A line-based grep for a phrase like
# "does not imply --sf-confirmed" fails the moment the sentence wraps or a word
# is bolded — the RULE is intact, the check breaks. A gate that fails on reflow
# gets weakened by whoever hits it on a deadline. Normalize, then assert the
# substance.
flat() {
  # strip line-leading blockquote/list markers FIRST (a wrapped blockquote puts
  # a "> " in the middle of the sentence once newlines collapse), then drop
  # emphasis/backticks, then join and collapse.
  sed -e 's/^[[:space:]]*>[[:space:]]*//' -e 's/^[[:space:]]*[-*][[:space:]]//' "$1" 2>/dev/null \
    | tr '\n' ' ' | sed -e 's/[*`_]//g' -e 's/[[:space:]][[:space:]]*/ /g'
}

echo "=== Phase A: static skill hygiene ==="

# A1: SKILL.md exists.
[ -f "$SKILL_MD" ] && pass "SKILL.md present" || { fail "SKILL.md missing at $SKILL_MD"; }

# A2: frontmatter has the four required keys.
for key in "name:" "description:" "allowed-tools:" "argument-hint:"; do
  if grep -q "^$key" "$SKILL_MD" 2>/dev/null; then pass "frontmatter has $key"
  else fail "frontmatter missing $key"; fi
done

# A3: progressive disclosure — body must reference the reference/ files, not
#     re-narrate them. Require links to all three reference docs.
for ref in "reference/runbook.md" "reference/inputs.md" "reference/config.md"; do
  if grep -q "$ref" "$SKILL_MD" 2>/dev/null && [ -f "$SKILL_DIR/$ref" ]; then
    pass "references existing $ref"
  else fail "missing reference to / file $ref"; fi
done

# A4: de-dup guard — the full run command should live ONLY in the runbook, not
#     be copy-pasted into SKILL.md. SKILL.md should NOT contain the band flags
#     inline (that's the duplication the audit flagged).
if grep -q -- "--band-low <low" "$SKILL_MD" 2>/dev/null; then
  fail "SKILL.md re-narrates the full command (should live only in runbook.md)"
else pass "no duplicated run command in SKILL.md body"; fi

# A5: trigger hygiene — description should carry trigger language, not contract
#     detail. Check the description mentions a user-facing trigger word.
if grep -iqE "regenerate|generate|refresh|build" "$SKILL_MD" 2>/dev/null; then
  pass "description carries a trigger verb"
else fail "description lacks a user-facing trigger verb"; fi

# A6: explicit trigger phrase — the description must contain the canonical
#     "create the Scorecard" trigger (case-insensitive) so the skill fires on
#     the user's actual phrasing.
if grep -iq "create the Scorecard" "$SKILL_MD" 2>/dev/null; then
  pass "description carries the 'create the Scorecard' trigger phrase"
else fail "description missing the 'create the Scorecard' trigger phrase"; fi

# A7: audit-step mention — SKILL.md must reference the post-engine audit step
#     so the orchestrator knows to run it before shipping.
if grep -iqE "audit step|audit_report|audit report" "$SKILL_MD" 2>/dev/null; then
  pass "SKILL.md mentions the audit step"
else fail "SKILL.md does not mention the audit step"; fi

# A8: upload-detection rule — runbook.md must carry the Upload Detection
#     section so the skill resolves session-uploaded matrices correctly.
if [ -f "$RUNBOOK_MD" ] && grep -iqE "upload detection|@path|mnt/uploads" "$RUNBOOK_MD" 2>/dev/null; then
  pass "runbook.md carries the upload-detection rule"
else fail "runbook.md missing the upload-detection rule"; fi

# A9: SF per-run verify — SKILL.md + runbook must document reading/verifying the
#     SF from THIS matrix and the --sf-confirmed accept path.
if grep -iqE "sf-confirmed|matrix lists" "$SKILL_MD" 2>/dev/null \
   && grep -iq -- "--sf-confirmed" "$RUNBOOK_MD" 2>/dev/null; then
  pass "SF per-run verify + --sf-confirmed documented"
else fail "SF per-run verify / --sf-confirmed not documented"; fi

# A10: out-dir prompt — the skill must document ASKING where to save.
if grep -iqE "where (the outputs should|to) (go|save)|ask .*out-dir|save the outputs" "$SKILL_MD" 2>/dev/null; then
  pass "save-location (out-dir) prompt documented in SKILL.md"
else fail "save-location (out-dir) prompt not documented in SKILL.md"; fi

# A11: email draft (no auto-send) — must be documented.
if grep -iqE "draft .*email|submission email" "$SKILL_MD" 2>/dev/null \
   && grep -iqE "not auto-send|never auto-send|do NOT auto-send|auto-send" "$SKILL_MD" 2>/dev/null; then
  pass "submission-email DRAFT (no auto-send) documented in SKILL.md"
else fail "submission-email DRAFT / no-auto-send not documented in SKILL.md"; fi

# A12: Scorecard Summary artifact — must be documented in SKILL.md + runbook.
if grep -iq "scorecard_summary" "$SKILL_MD" 2>/dev/null \
   && grep -iq "scorecard_summary" "$RUNBOOK_MD" 2>/dev/null; then
  pass "scorecard_summary artifact documented"
else fail "scorecard_summary artifact not documented"; fi

# A13: required scoring uploads + no-fallback — SKILL.md must document BOTH the
#      --scoring-framework and --category-scores uploads AND the no-fallback
#      hard-stop (the scorecard cannot be produced without the two files).
if grep -iq -- "--scoring-framework" "$SKILL_MD" 2>/dev/null \
   && grep -iq -- "--category-scores" "$SKILL_MD" 2>/dev/null \
   && grep -iqE "no fallback|cannot be produced" "$SKILL_MD" 2>/dev/null; then
  pass "scoring-framework + category-scores uploads + no-fallback documented in SKILL.md"
else fail "scoring-framework / category-scores uploads / no-fallback not documented in SKILL.md"; fi

# A14: scoring flags in the runbook command — the escape-hatch command in
#      runbook.md must carry BOTH --scoring-framework and --category-scores.
if grep -iq -- "--scoring-framework" "$RUNBOOK_MD" 2>/dev/null \
   && grep -iq -- "--category-scores" "$RUNBOOK_MD" 2>/dev/null; then
  pass "both scoring flags present in runbook.md command"
else fail "runbook.md missing --scoring-framework / --category-scores"; fi

# ---------------------------------------------------------------------------
# The run pack (P1-4). The pack is the PRIMARY documented path; these checks
# gate the four claims the skill text must never lose.
# ---------------------------------------------------------------------------

# A15: the pack is documented as the normal path, by flag AND by artifact name
#      (the operator has to be able to ask for the file by name).
if grep -q -- "--inputs" "$SKILL_MD" 2>/dev/null \
   && grep -iq "Scorecard Inputs.xlsx" "$SKILL_MD" 2>/dev/null \
   && grep -q -- "--inputs" "$RUNBOOK_MD" 2>/dev/null; then
  pass "run pack (--inputs + '<Project> - Scorecard Inputs.xlsx') documented"
else fail "run pack not documented as the primary path (--inputs / pack filename)"; fi

# A16: PRE-FILLED IS NOT PRE-CONFIRMED (Marvin §5 / Floyd's protected list).
#      The single most important sentence in the ruling: the pack supplies data,
#      never a gate decision. If this line ever leaves the skill text, an
#      operator can be told the pack "handles" the confirmations.
SKILL_FLAT="$(flat "$SKILL_MD")"
RUNBOOK_FLAT="$(flat "$RUNBOOK_MD")"
if printf '%s' "$SKILL_FLAT" | grep -iqE "pre-filled is not pre-confirmed" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "does not imply --sf-confirmed" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "does not imply --baseline-confirmed"; then
  pass "pre-filled-is-not-pre-confirmed rule documented (both gates survive the pack)"
else fail "SKILL.md does not state that --inputs implies NEITHER --sf-confirmed NOR --baseline-confirmed"; fi

# A17: one channel per run — the mutual-exclusion rule, in both docs, with the
#      "edit the pack, don't patch it with a flag" remedy.
if printf '%s' "$SKILL_FLAT" | grep -iqE "\-\-inputs is mutually exclusive with" \
   && printf '%s' "$RUNBOOK_FLAT" | grep -iqE "\-\-inputs is mutually exclusive with" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "edits? the pack"; then
  pass "pack/flag mutual-exclusion rule + edit-the-pack remedy documented"
else fail "mutual-exclusion rule (--inputs vs the individual flags) not documented in both docs"; fi

# A17b: the BAND flags are in the exclusion list too. Called out separately
#       because the band is the one pack fact whose flags read like ordinary
#       run parameters — it is the conflict a reader is most likely to assume
#       away, and the one that silently re-banded a confirmed card before the
#       engine enforced it. Both docs must name them.
if printf '%s' "$SKILL_FLAT" | grep -iqE "\-\-band-low" \
   && printf '%s' "$RUNBOOK_FLAT" | grep -iqE "\-\-band-low"; then
  pass "band flags named in the pack mutual-exclusion rule (both docs)"
else fail "band flags (--band-low/--band-high/--mid) not documented as conflicting with --inputs"; fi

# A18: the escape hatch — legitimate uses named, AND the integrity-smell
#      question (Marvin §9.3: a named question, never a block).
if printf '%s' "$SKILL_FLAT" | grep -iqE "escape hatch" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "legacy matrices" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "archival re-render" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "(weren.t|were not) pipeline-originated"; then
  pass "escape hatch: legitimate uses + integrity-smell question documented"
else fail "escape-hatch legitimacy rules / integrity-smell question missing from SKILL.md"; fi

# A19: W8 honesty — Falke has NO standing framework, so the runbook must not
#      imply a drift control exists. Assert the honest disclosure language.
if grep -iqE "no standing evaluation framework was on file" "$RUNBOOK_MD" 2>/dev/null \
   && grep -iqE "has no standing evaluation framework|no standing evaluation framework artifact" "$RUNBOOK_MD" 2>/dev/null; then
  pass "standing-framework W8 honesty documented (no control claimed that doesn't exist)"
else fail "runbook.md implies a standing-framework drift control Falke does not have"; fi

# A20: no pack on a quarantined (exit-3) matrix — the operator will ask where
#      their pack is; the answer must be in the text, not invented.
if grep -iqE "quarantin" "$RUNBOOK_MD" 2>/dev/null \
   && grep -iqE "no pack|emits.*no pack" "$RUNBOOK_MD" 2>/dev/null; then
  pass "no-pack-on-quarantined-matrix documented"
else fail "runbook.md does not explain that a quarantined (exit-3) matrix gets no pack"; fi

# ---------------------------------------------------------------------------
# EXIT CONTRACT v2 (P1-1) + the PROVISIONAL pathway (P1-2). The exit code is
# the ONLY thing the skill reads to decide what to tell the operator and
# whether to offer the email, so the table and the five handlers are contract,
# not commentary.
# ---------------------------------------------------------------------------

# A21: all five Handle-exit-N sections exist. Matches the create-matrix idiom.
MISSING_HANDLERS=""
for n in 0 1 2 3 4; do
  printf '%s' "$SKILL_FLAT" | grep -iqE "Handle exit $n" || MISSING_HANDLERS="$MISSING_HANDLERS $n"
done
if [ -z "$MISSING_HANDLERS" ]; then
  pass "Handle-exit-N sections present for 0/1/2/3/4"
else fail "SKILL.md missing Handle-exit section(s) for:$MISSING_HANDLERS"; fi

# A22: the exit table + precedence. 3 > 4 is the rule an orchestrator gets
#      wrong by defaulting to "higher number = worse".
if printf '%s' "$SKILL_FLAT" | grep -iqE "Exit-code contract" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "3 > 4|3 &gt; 4" \
   && printf '%s' "$RUNBOOK_FLAT" | grep -iqE "3 > 4|3 &gt; 4"; then
  pass "exit-code contract table + 3>4 precedence documented in both docs"
else fail "exit table / 3>4 precedence missing from SKILL.md or runbook.md"; fi

# A23: exit 2 keeps the fiduciary framing; exit 1 must NOT borrow it (a typo'd
#      path is not "the gate working" — saying so trains the operator to
#      discount the message that matters).
if printf '%s' "$SKILL_FLAT" | grep -iqE "gate working, not an error"; then
  pass "exit-2 'gate working, not an error' framing present"
else fail "exit-2 fiduciary framing missing from SKILL.md"; fi

# A24: THE EMAIL SUPPRESSION (Marvin §6). The email is the one mechanism that
#      pushes the document OUTWARD; offering it on a provisional run is the
#      tool proposing the operator send a working document to a board. Keyed to
#      the exit code — no coverage inspection.
if printf '%s' "$SKILL_FLAT" | grep -iqE "(do not|never) offer the submission email on exit 4|never offer it on exit 3 or exit 4" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "on exit 0 ONLY|on exit 0 only"; then
  pass "submission email suppressed on exit 3/4, offered on exit 0 only"
else fail "email-suppression rule missing — a provisional run must NOT offer the submission email"; fi

# A25: the provisional card's subtractive controls. All four, because each one
#      is a ranking claim the skill could helpfully re-add in its own prose.
PROV_MISS=""
printf '%s' "$SKILL_FLAT" | grep -iqE "alphabetical" || PROV_MISS="$PROV_MISS no-alphabetical-listing"
printf '%s' "$SKILL_FLAT" | grep -iqE "not ranked" || PROV_MISS="$PROV_MISS no-'not ranked'"
printf '%s' "$SKILL_FLAT" | grep -iqE "names no winner|no leader" || PROV_MISS="$PROV_MISS no-leader-rule"
printf '%s' "$SKILL_FLAT" | grep -iqE "Pending — |No Overall" || PROV_MISS="$PROV_MISS no-withheld-Overall"
if [ -z "$PROV_MISS" ]; then
  pass "provisional card: no rank / no leader / no Overall / alphabetical all documented"
else fail "provisional controls missing from SKILL.md:$PROV_MISS"; fi

# A26: exit 4 is the NORMAL iterative path, not a failure. If the skill frames
#      it as a defect the operator will chase "fixing" it — or worse, wait to
#      run until the grid is full, which is the workflow P1-2 exists to enable.
if printf '%s' "$SKILL_FLAT" | grep -iqE "normal path, not a failure|not a failure|expected mid-evaluation"; then
  pass "exit 4 framed as the normal mid-evaluation path, not a failure"
else fail "SKILL.md frames exit 4 as a failure (or does not say it is normal)"; fi

# A27: --no-audit prohibited for board runs (Floyd verdict (e)) AND the trap
#      named: it exits 0 but writes PRELIMINARY artifacts.
if printf '%s' "$SKILL_FLAT" | grep -iqE "\-\-no-audit" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "PROHIBITED for board runs|prohibited for board runs" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "not audited"; then
  pass "--no-audit prohibited for board runs + 'not audited' watermark documented"
else fail "--no-audit prohibition / not-audited watermark missing from SKILL.md"; fi

# A28: the filename IS the deliverability signal. This is the rule that
#      survives a screenshot into a board packet, and the one that covers the
#      --no-audit exit-0 case the table alone gets wrong.
if printf '%s' "$SKILL_FLAT" | grep -iqE "scorecard-PRELIMINARY" \
   && printf '%s' "$RUNBOOK_FLAT" | grep -iqE "scorecard-PRELIMINARY" \
   && printf '%s' "$SKILL_FLAT" | grep -iqE "filename is the deliverability signal"; then
  pass "PRELIMINARY filename rule documented as the deliverability signal"
else fail "PRELIMINARY filename / deliverability-signal rule missing"; fi

# A29: audit-step doc drift (Floyd's P1-1 row). The audit is IN-ENGINE,
#      default-ON and PRE-render — the docs used to describe a separate step
#      the orchestrator runs afterwards.
if printf '%s' "$RUNBOOK_FLAT" | grep -iqE "There is no separate audit step" \
   && printf '%s' "$RUNBOOK_FLAT" | grep -iqE "before the render|BEFORE the render" \
   && ! printf '%s' "$SKILL_FLAT" | grep -iqE "Run the audit step"; then
  pass "audit documented as in-engine, default-ON, pre-render (no separate step)"
else fail "audit-step doc drift: docs still describe a separate/after-the-render audit step"; fi

# A30: blank = not yet scored (P1-2's trigger, and the operator-facing change
#      to the Scores grid). Plus the all-blank floor.
INPUTS_FLAT="$(flat "$SKILL_DIR/reference/inputs.md")"
if printf '%s' "$INPUTS_FLAT" | grep -iqE "blank cell = not yet scored|means not yet scored" \
   && printf '%s' "$INPUTS_FLAT" | grep -iqE "all-blank grid stops the run|entirely blank grid stops the run"; then
  pass "blank-score semantics (= not yet scored) + all-blank floor documented"
else fail "inputs.md does not document blank = not-yet-scored / the all-blank floor"; fi


echo ""
echo "=== Phase B: invocation smoke (HTML-only) ==="
echo "  engine interpreter: $PYTHON_BIN  [$PY_HOW]"

if [ ! -f "$MATRIX" ]; then
  if [ -n "${REQUIRE_PHASE_B:-}" ]; then
    fail "Phase B REQUIRED (REQUIRE_PHASE_B set) but the matrix fixture is missing: $MATRIX — regenerate it with tests/fixtures/_make_create_matrix_fixtures.py"
  else
    echo "  SKIP  Phase-B matrix fixture not present at:"
    echo "        $MATRIX"
    echo "        (Untracked-local by design — .gitignore excludes *.xlsx — so the"
    echo "         scrubbed release stage and the public bundle skip Phase B; Phase A"
    echo "         still gates. Regenerate on a dev machine with"
    echo "         tests/fixtures/_make_create_matrix_fixtures.py.)"
  fi
elif ! "$PYTHON_BIN" -c "import yaml, openpyxl, jinja2" >/dev/null 2>&1; then
  MISSING="$("$PYTHON_BIN" -c '
import importlib.util as u
print(", ".join(m for m in ("yaml", "openpyxl", "jinja2") if u.find_spec(m) is None))' 2>/dev/null \
    || echo "interpreter did not run")"
  fail "engine interpreter unusable: $PYTHON_BIN [$PY_HOW] is missing: ${MISSING:-unknown}. Set SCORECARD_PYTHON to a Python with the engine deps (engines/requirements.txt), or run the plugin bootstrap so \${CLAUDE_PLUGIN_DATA}/venv exists."
else
  # Synthetic per-run scoring inputs, generated INTO THE TEMP DIR at eval time:
  #   framework = the tracked scoring-framework-template.xlsx (it IS Falke's
  #   current 8-category framework and parses valid as-is);
  #   scores    = one 1-10 row per fixture firm (fully fictional), written here
  #   so the untracked-xlsx rule never applies and the file can never go stale
  #   against the fixture's firm list.
  SCORES_XLSX="$OUT/eval_category_scores.xlsx"
  "$PYTHON_BIN" - "$SCORES_XLSX" <<'PYEOF' > "$OUT/_eval_scores_gen.log" 2>&1
import sys
import openpyxl
from openpyxl.styles import Font

out_path = sys.argv[1]
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Category_Scores"
labels = ["Pricing", "Scope", "Condo Exp", "CO Risk",
          "Reputation", "Financial", "Controls", "Docs"]
ws.cell(row=1, column=1,
        value="DETAILED CATEGORY SCORES (1-10) — skill-eval smoke "
              "(SYNTHETIC: fictional firms/figures)").font = Font(bold=True)
for col, header in enumerate(["Firm"] + labels, start=1):
    ws.cell(row=2, column=col, value=header).font = Font(bold=True)
# The four synthetic firms in tests/fixtures/create_matrix_4bidders.xlsx,
# exactly as the scorecard displays them (scores rows must match the scored
# bidder field or the engine hard-stops — that gate is tested elsewhere).
firms = {
    "Alpine Restoration Group": [8, 8, 7, 7, 8, 7, 7, 8],
    "Bayside Builders LLC":     [7, 7, 7, 6, 7, 7, 6, 7],
    "Cypress Construction Co.": [6, 7, 6, 6, 6, 6, 6, 6],
    "Driftwood Contractors":    [5, 6, 5, 5, 6, 5, 5, 5],
}
for row, (firm, scores) in enumerate(firms.items(), start=3):
    ws.cell(row=row, column=1, value=firm)
    for col, score in enumerate(scores, start=2):
        ws.cell(row=row, column=col, value=score)
wb.save(out_path)
PYEOF
  if [ -f "$SCORES_XLSX" ]; then
    pass "synthetic category-scores xlsx generated for the fixture firms"
  else
    fail "could not generate the category-scores xlsx (see $OUT/_eval_scores_gen.log)"
  fi

  # B-positive: the full gated render — SF confirmed (accept the fixture's
  # matrix GSF; an equal explicit --sf-basis would correctly trip audit C3),
  # baseline confirmed, both scoring inputs supplied. --html-only so no PDF
  # engine is required. The default-ON audit runs inside the engine.
  ( cd "$ENGINE_DIR" && "$PYTHON_BIN" -m scorecard.cli \
      --matrix "$MATRIX" \
      --project-name "Eval Smoke Condo · Restoration" \
      --sf-confirmed \
      --band-low 1.05 --band-high 1.40 --mid 1.20 \
      --baseline examples/sample_baseline.json --baseline-confirmed \
      --scoring-framework templates/scoring-framework-template.xlsx \
      --category-scores "$SCORES_XLSX" \
      --out-dir "$OUT" --html-only ) > "$OUT/_eval.log" 2>&1
  RC=$?
  [ $RC -eq 0 ] && pass "engine exited 0" || fail "engine exited $RC (see $OUT/_eval.log)"
  [ -f "$OUT/scorecard.html" ] && pass "scorecard.html produced" || fail "scorecard.html missing"
  [ -f "$OUT/scorecard_run.json" ] && pass "scorecard_run.json produced" || fail "scorecard_run.json missing"
  if grep -q '"full_coverage"' "$OUT/scorecard_run.json" 2>/dev/null; then
    pass "run JSON carries a coverage flag"
  else fail "run JSON missing full_coverage flag"; fi

  # B-audit: audit_report.md must exist alongside the other artifacts. The
  # audit step is wired into the engine and --audit is default-ON, so this
  # assertion runs unconditionally.
  if [ -f "$OUT/audit_report.md" ]; then
    pass "audit_report.md produced"
  else
    fail "audit_report.md missing (audit step did not write it)"
  fi

  # B-negative: missing SF decision MUST hard-stop (no matrix-GSF fallback).
  # Every other required input is supplied so the SF gate is the one under
  # test; the stop message is asserted so a different gate can't false-pass
  # this check.
  ( cd "$ENGINE_DIR" && "$PYTHON_BIN" -m scorecard.cli \
      --matrix "$MATRIX" --project-name "x" \
      --band-low 1.05 --band-high 1.40 --mid 1.20 \
      --baseline examples/sample_baseline.json --baseline-confirmed \
      --scoring-framework templates/scoring-framework-template.xlsx \
      --category-scores "$SCORES_XLSX" \
      --out-dir "$OUT" --html-only ) > "$OUT/_eval_neg.log" 2>&1
  if [ $? -ne 0 ] && grep -q "SF basis not confirmed" "$OUT/_eval_neg.log" 2>/dev/null; then
    pass "missing SF decision hard-stops at the SF gate (no silent fallback)"
  else fail "missing SF decision did NOT stop at the SF gate (see $OUT/_eval_neg.log)"; fi

  # B-pack-exclusion: --inputs + an individual flag MUST hard-stop (Marvin
  # §9.2, one channel per run). The CLI enforces this BEFORE it opens the pack,
  # so a non-existent pack path still exercises the real check — no pack
  # fixture needed here (the live producer->consumer pack suite is
  # engines/scorecard/tests/test_producer_live_compat.py, which release.sh
  # already gates; this asserts the CONTRACT THE SKILL TEXT PROMISES).
  ( cd "$ENGINE_DIR" && "$PYTHON_BIN" -m scorecard.cli \
      --matrix "$MATRIX" --project-name "x" \
      --inputs "$OUT/no_such_pack.xlsx" \
      --baseline examples/sample_baseline.json \
      --sf-confirmed --baseline-confirmed \
      --out-dir "$OUT" --html-only ) > "$OUT/_eval_pack_excl.log" 2>&1
  if [ $? -ne 0 ] && grep -q -- "--inputs supplies the same facts as" "$OUT/_eval_pack_excl.log" 2>/dev/null; then
    pass "--inputs + --baseline hard-stops on the mutual-exclusion rule"
  else fail "--inputs + an individual flag did NOT hard-stop (see $OUT/_eval_pack_excl.log)"; fi

  # B-pack-not-a-pack: --inputs pointed at a workbook that is not a run pack
  # must stop loudly and name what it is missing — the docs tell the operator
  # this is the gate working, so the message has to actually be there. The
  # matrix fixture is a real, valid workbook that is NOT a pack.
  ( cd "$ENGINE_DIR" && "$PYTHON_BIN" -m scorecard.cli \
      --matrix "$MATRIX" --project-name "x" \
      --inputs "$MATRIX" \
      --sf-confirmed --baseline-confirmed \
      --out-dir "$OUT" --html-only ) > "$OUT/_eval_pack_bad.log" 2>&1
  if [ $? -ne 0 ] && grep -q "is not a scorecard run pack" "$OUT/_eval_pack_bad.log" 2>/dev/null; then
    pass "--inputs on a non-pack workbook hard-stops naming the missing tabs"
  else fail "--inputs on a non-pack workbook did NOT stop cleanly (see $OUT/_eval_pack_bad.log)"; fi

  # B-pack-band: the band flags conflict with --inputs too. This is a
  # REGRESSION PIN, not a duplicate of the --baseline check above: the band
  # arrives as its own set of flags that read like ordinary run parameters, and
  # before they were added to PACK_CONFLICTING_FLAGS a pack run accepted
  # --band-low and silently rendered a card whose band contradicted the
  # Baseline tab the owner had just confirmed at preview.
  ( cd "$ENGINE_DIR" && "$PYTHON_BIN" -m scorecard.cli \
      --matrix "$MATRIX" --project-name "x" \
      --inputs "$OUT/no_such_pack.xlsx" --band-low 1.05 \
      --sf-confirmed --baseline-confirmed \
      --out-dir "$OUT" --html-only ) > "$OUT/_eval_pack_band.log" 2>&1
  if [ $? -ne 0 ] && grep -q -- "--band-low" "$OUT/_eval_pack_band.log" 2>/dev/null; then
    pass "--inputs + --band-low hard-stops naming the band flag"
  else fail "--inputs + --band-low did NOT hard-stop (the band silently overrides the pack — see $OUT/_eval_pack_band.log)"; fi

  # B-pack-band-zero: the falsiness edge. The band flags are floats, so
  # `--band-low 0` is FALSY — a conflict check written as a truthiness test
  # (`if getattr(args, dest)`) lets a zero band through and re-bands a
  # confirmed card at $0. The predicate must be "was the flag supplied"
  # (`is not None`). Pinned here because it is invisible in review and reads
  # like a harmless simplification.
  ( cd "$ENGINE_DIR" && "$PYTHON_BIN" -m scorecard.cli \
      --matrix "$MATRIX" --project-name "x" \
      --inputs "$OUT/no_such_pack.xlsx" --band-low 0 \
      --sf-confirmed --baseline-confirmed \
      --out-dir "$OUT" --html-only ) > "$OUT/_eval_pack_band0.log" 2>&1
  if [ $? -ne 0 ] && grep -q -- "--band-low" "$OUT/_eval_pack_band0.log" 2>/dev/null; then
    pass "--inputs + --band-low 0 hard-stops (falsy value does not evade the check)"
  else fail "--inputs + --band-low 0 evaded the conflict check — the predicate regressed to truthiness (see $OUT/_eval_pack_band0.log)"; fi

  # -------------------------------------------------------------------------
  # EXIT CONTRACT v2 (P1-1) + PROVISIONAL (P1-2), live. The skill text tells the
  # operator what each exit means and gates the submission email on it, so the
  # codes are a CONTRACT WITH THE SKILL, not an engine detail. The truth table
  # itself is unit-tested (tests/test_exit_contract.py); these assert the two
  # ends the skill actually observes — the code and the filename.
  # -------------------------------------------------------------------------

  # B-exit-0 already asserted above ("engine exited 0"), and it wrote
  # scorecard.html — assert the clean run is NOT named PRELIMINARY, which is
  # the other half of the filename rule.
  if [ -f "$OUT/scorecard.html" ] && [ ! -f "$OUT/scorecard-PRELIMINARY.html" ]; then
    pass "exit 0 (full coverage) writes scorecard.html — the deliverable name"
  else fail "a clean run did not produce the plain scorecard.html name"; fi

  # B-exit-1: a bad --matrix path is ENVIRONMENT (1), not an input gate (2).
  # The distinction is the whole point of P1-1: exit 1 must mean the same thing
  # here as in the matrix engine (nothing written), and it must be a clean
  # [STOP], not the traceback it used to be.
  ( cd "$ENGINE_DIR" && "$PYTHON_BIN" -m scorecard.cli \
      --matrix "$OUT/no_such_matrix.xlsx" --project-name "x" \
      --sf-confirmed --band-low 1.05 --band-high 1.40 --mid 1.20 \
      --baseline examples/sample_baseline.json --baseline-confirmed \
      --scoring-framework templates/scoring-framework-template.xlsx \
      --category-scores "$SCORES_XLSX" \
      --out-dir "$OUT" --html-only ) > "$OUT/_eval_exit1.log" 2>&1
  RC1=$?
  if [ $RC1 -eq 1 ] && grep -q "\[STOP\]" "$OUT/_eval_exit1.log" 2>/dev/null \
     && ! grep -q "Traceback" "$OUT/_eval_exit1.log" 2>/dev/null; then
    pass "bad --matrix path exits 1 (environment) with a clean [STOP], no traceback"
  else fail "bad --matrix path: expected exit 1 + [STOP] + no traceback, got exit $RC1 (see $OUT/_eval_exit1.log)"; fi

  # B-exit-4: a PARTIALLY scored grid renders PROVISIONAL. Generated here with
  # two cells left blank — blank = not-yet-scored is P1-2's trigger, and this
  # asserts the three facts the skill's exit-4 handler promises the operator:
  # the code, the renamed artifacts, and the composed watermark reason.
  PARTIAL_XLSX="$OUT/eval_category_scores_partial.xlsx"
  "$PYTHON_BIN" - "$PARTIAL_XLSX" <<'PYEOF' > "$OUT/_eval_partial_gen.log" 2>&1
import sys
import openpyxl
from openpyxl.styles import Font

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Category_Scores"
labels = ["Pricing", "Scope", "Condo Exp", "CO Risk",
          "Reputation", "Financial", "Controls", "Docs"]
ws.cell(row=1, column=1, value="DETAILED CATEGORY SCORES — PARTIAL (skill-eval)")
for col, header in enumerate(["Firm"] + labels, start=1):
    ws.cell(row=2, column=col, value=header).font = Font(bold=True)
firms = {
    "Alpine Restoration Group": [8, 8, 7, 7, 8, 7, 7, 8],
    "Bayside Builders LLC":     [7, 7, 7, 6, 7, 7, 6, 7],
    "Cypress Construction Co.": [6, 7, 6, 6, 6, 6, 6, 6],
    "Driftwood Contractors":    [5, 6, 5, 5, 6, 5, 5, 5],
}
for row, (firm, scores) in enumerate(firms.items(), start=3):
    ws.cell(row=row, column=1, value=firm)
    for col, score in enumerate(scores, start=2):
        # leave two cells of the first bidder UNSCORED -> partial coverage
        if row == 3 and col in (2, 3):
            continue
        ws.cell(row=row, column=col, value=score)
wb.save(sys.argv[1])
PYEOF
  OUT4="$OUT/prov"; mkdir -p "$OUT4"
  ( cd "$ENGINE_DIR" && "$PYTHON_BIN" -m scorecard.cli \
      --matrix "$MATRIX" --project-name "Eval Provisional · Restoration" \
      --sf-confirmed --band-low 1.05 --band-high 1.40 --mid 1.20 \
      --baseline examples/sample_baseline.json --baseline-confirmed \
      --scoring-framework templates/scoring-framework-template.xlsx \
      --category-scores "$PARTIAL_XLSX" \
      --out-dir "$OUT4" --html-only ) > "$OUT4/_eval_prov.log" 2>&1
  RC4=$?
  [ $RC4 -eq 4 ] && pass "partial coverage exits 4 (delivered PROVISIONAL)" \
    || fail "partial coverage exited $RC4, expected 4 (see $OUT4/_eval_prov.log)"
  if [ -f "$OUT4/scorecard-PRELIMINARY.html" ] \
     && [ -f "$OUT4/scorecard_summary-PRELIMINARY.html" ] \
     && [ ! -f "$OUT4/scorecard.html" ]; then
    pass "provisional run renames BOTH card and summary to -PRELIMINARY"
  else fail "provisional artifacts not renamed (the deliverable name must not be reused)"; fi
  if grep -q "PRELIMINARY — evaluation incomplete" "$OUT4/_eval_prov.log" 2>/dev/null; then
    pass "provisional watermark names its reason (evaluation incomplete)"
  else fail "provisional run did not report the composed watermark reason"; fi
  # the card must not rank: run json records rank null, not a number.
  if "$PYTHON_BIN" -c "
import json, sys
run = json.load(open('$OUT4/scorecard_run.json'))
sys.exit(0 if (run['full_coverage'] is False
               and all(b['rank'] is None for b in run['bidders'])) else 1)
" 2>/dev/null; then
    pass "provisional run json: full_coverage false, every rank null (not ranked)"
  else fail "provisional run json still carries ranks — the field was ranked on an incomplete record"; fi

  # B-no-audit: exits 0 but the ARTIFACT carries the disclosure. This is the
  # one case where exit 0 is not deliverable, which is exactly why the skill's
  # rule is "the filename is the signal", not "0 means ship it".
  OUTNA="$OUT/noaudit"; mkdir -p "$OUTNA"
  ( cd "$ENGINE_DIR" && "$PYTHON_BIN" -m scorecard.cli \
      --matrix "$MATRIX" --project-name "Eval NoAudit" \
      --sf-confirmed --band-low 1.05 --band-high 1.40 --mid 1.20 \
      --baseline examples/sample_baseline.json --baseline-confirmed \
      --scoring-framework templates/scoring-framework-template.xlsx \
      --category-scores "$SCORES_XLSX" \
      --out-dir "$OUTNA" --html-only --no-audit ) > "$OUTNA/_eval_na.log" 2>&1
  RCNA=$?
  if [ $RCNA -eq 0 ] && [ -f "$OUTNA/scorecard-PRELIMINARY.html" ] \
     && grep -q "PRELIMINARY — not audited" "$OUTNA/_eval_na.log" 2>/dev/null; then
    pass "--no-audit exits 0 but renames + stamps 'not audited' (exit 0 != deliverable)"
  else fail "--no-audit did not produce a 'not audited' PRELIMINARY artifact at exit 0 (rc=$RCNA, see $OUTNA/_eval_na.log)"; fi
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: PASS (all checks)"
  exit 0
else
  echo "RESULT: FAIL ($FAIL check(s) failed)"
  exit "$FAIL"
fi
