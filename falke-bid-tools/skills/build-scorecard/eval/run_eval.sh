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

# A14: scoring flags in the runbook command — the generic command in runbook.md
#      must carry BOTH --scoring-framework and --category-scores.
if grep -iq -- "--scoring-framework" "$RUNBOOK_MD" 2>/dev/null \
   && grep -iq -- "--category-scores" "$RUNBOOK_MD" 2>/dev/null; then
  pass "both scoring flags present in runbook.md command"
else fail "runbook.md missing --scoring-framework / --category-scores"; fi


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
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: PASS (all checks)"
  exit 0
else
  echo "RESULT: FAIL ($FAIL check(s) failed)"
  exit "$FAIL"
fi
