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
# Two phases:
#   A. STATIC skill hygiene — frontmatter + progressive-disclosure structure +
#      trigger / audit / upload-detection language.
#   B. INVOCATION smoke — run the engine on the synthetic sample validation inputs
#      (HTML-only so no PDF engine is required) and assert the artifacts exist
#      and the run JSON carries a coverage flag, and that the audit step wrote
#      audit_report.md (the audit is wired and default-ON in the engine).
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
MATRIX="$ENGINE_DIR/examples/sample_matrix_fixture.xlsx"
OUT="$(mktemp -d 2>/dev/null || echo /tmp/scorecard_eval_out)"
mkdir -p "$OUT"

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


echo ""
echo "=== Phase B: invocation smoke (HTML-only) ==="

if [ ! -f "$MATRIX" ]; then
  echo "  SKIP  bundled eval fixture not present at:"
  echo "        $MATRIX"
  echo "        (Phase B is skipped; Phase A still gates. The fixture should be"
  echo "         bundled at engines/scorecard/examples/sample_matrix_fixture.xlsx.)"
else
  ( cd "$ENGINE_DIR" && python3 -m scorecard.cli \
      --matrix "$MATRIX" \
      --project-name "Sample Condominium · Lobby Renovation" \
      --sf-basis 16000 --band-low 3.35 --band-high 3.55 --mid 3.40 \
      --baseline examples/sample_baseline.json --baseline-confirmed \
      --qual-notes examples/sample_qual_notes.json \
      --aliases examples/sample_aliases.json \
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

  # B-negative: missing --sf-basis MUST hard-stop (no matrix-GSF fallback).
  ( cd "$ENGINE_DIR" && python3 -m scorecard.cli \
      --matrix "$MATRIX" --project-name "x" \
      --band-low 3.35 --band-high 3.55 --mid 3.40 \
      --baseline examples/sample_baseline.json \
      --out-dir "$OUT" --html-only ) > "$OUT/_eval_neg.log" 2>&1
  if [ $? -ne 0 ]; then pass "missing --sf-basis hard-stops (no silent fallback)"
  else fail "missing --sf-basis did NOT stop the run"; fi
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: PASS (all checks)"
  exit 0
else
  echo "RESULT: FAIL ($FAIL check(s) failed)"
  exit "$FAIL"
fi
