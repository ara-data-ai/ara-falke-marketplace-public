#!/usr/bin/env bash
# =============================================================================
# create-matrix SKILL behavior eval — Phase A (static hygiene) only
# =============================================================================
# Verifies the SKILL document itself is well-formed and still carries every
# load-bearing contract the orchestration depends on. The matrix ENGINE is
# covered by its own pytest suite + golden gate (engines/matrix/tests/ +
# eval/), which release.sh already runs — this eval is the missing skill-layer
# counterpart (Floyd P0-4 / Boris A4, 2026-07-15).
#
# Phase B (an invocation smoke) is deliberately absent: create-matrix is an
# orchestration skill — Claude spawns extraction agents and only then calls the
# engine — so there is no meaningful single-command smoke a bash script can
# run that the engine's own golden gate doesn't already cover. Trigger-behavior
# testing (does the model LOAD the skill) is the skill-creator automated loop,
# tracked separately as P2-9.
#
# Run:
#   bash eval/run_eval.sh
# Exit 0 = pass, non-zero = fail (count of failed checks).
# =============================================================================
set -u

EVAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$EVAL_DIR/.." && pwd)"
SKILL_MD="$SKILL_DIR/SKILL.md"

FAIL=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

echo "=== Phase A: static skill hygiene (create-matrix) ==="

# A1: SKILL.md exists.
[ -f "$SKILL_MD" ] && pass "SKILL.md present" || fail "SKILL.md missing at $SKILL_MD"

# A2: frontmatter has the required keys.
for key in "name:" "description:" "allowed-tools:" "argument-hint:"; do
  if grep -q "^$key" "$SKILL_MD" 2>/dev/null; then pass "frontmatter has $key"
  else fail "frontmatter missing $key"; fi
done

# A3: canonical trigger phrases — the description must fire on the user's
#     actual phrasings.
for phrase in "create the matrix" "compare these bids"; do
  if grep -iq "$phrase" "$SKILL_MD" 2>/dev/null; then
    pass "carries the '$phrase' trigger phrase"
  else fail "missing the '$phrase' trigger phrase"; fi
done

# A4: SF-basis confirmation gate — both resolution flags plus the exit-2
#     hard-stop must be documented (the fiduciary $/SF denominator gate).
if grep -q -- "--sf-confirmed" "$SKILL_MD" 2>/dev/null \
   && grep -q -- "--sf-basis" "$SKILL_MD" 2>/dev/null \
   && grep -iqE "hard-stop.*exit 2|exit 2.*hard-stop|stops with exit 2" "$SKILL_MD" 2>/dev/null; then
  pass "SF-basis gate (--sf-confirmed / --sf-basis / exit-2 hard-stop) documented"
else fail "SF-basis gate not fully documented (--sf-confirmed / --sf-basis / exit 2)"; fi

# A5: exit-code contract v2 — the skill must carry the 0-4 contract table AND
#     the dedicated handling sections for the delivered-with-flags states.
if grep -iq "Exit-code contract" "$SKILL_MD" 2>/dev/null \
   && grep -iq "Handle exit 3" "$SKILL_MD" 2>/dev/null \
   && grep -iq "Handle exit 4" "$SKILL_MD" 2>/dev/null; then
  pass "exit-code contract v2 + Handle-exit-3/4 sections present"
else fail "exit-code contract v2 / Handle-exit-3/4 sections missing"; fi

# A6: exit-3 framing rule — quarantine is a tool/needs-review condition, never
#     a finding about a contractor's bid (fiduciary framing, do not weaken).
if grep -iqE "never a finding about a contractor" "$SKILL_MD" 2>/dev/null; then
  pass "exit-3 'never a finding about a contractor' framing present"
else fail "exit-3 fiduciary framing rule missing"; fi

# A7: --expect-bids defense-in-depth — the independent bid-count gate must be
#     documented as pass-it-every-run.
if grep -q -- "--expect-bids" "$SKILL_MD" 2>/dev/null \
   && grep -iqE "expect-bids.*every run|every run.*expect-bids|pass it on every run" "$SKILL_MD" 2>/dev/null; then
  pass "--expect-bids documented as an every-run gate"
else fail "--expect-bids every-run rule not documented"; fi

# A8: extraction trust boundary — the embedded agent brief must mark the PDF
#     as untrusted data, never instructions (prompt-injection guard).
if grep -iq "UNTRUSTED" "$SKILL_MD" 2>/dev/null \
   && grep -iqE "never as instructions|not.* as instructions" "$SKILL_MD" 2>/dev/null; then
  pass "extraction-agent trust boundary (PDF = untrusted data) present"
else fail "extraction-agent trust boundary missing from the embedded brief"; fi

# A9: hard stop on un-extracted bids — the run must never proceed with a
#     missing bid on its own initiative.
if grep -iqE "hard stop on any un-extracted bid" "$SKILL_MD" 2>/dev/null; then
  pass "hard-stop-on-missing-bid rule present"
else fail "hard-stop-on-missing-bid rule missing"; fi

# A10: upload-detection ambiguity rule — never guess "most recent".
if grep -iqE "most recent|most-recent" "$SKILL_MD" 2>/dev/null \
   && grep -iqE "stop and ask|STOP and ask" "$SKILL_MD" 2>/dev/null; then
  pass "upload ambiguity rule (never guess most-recent; stop and ask) present"
else fail "upload ambiguity rule missing"; fi

# A11: defensive filename handling — untrusted filenames are quoted and
#     metacharacter-rejected before shell interpolation.
if grep -iqE "filenames are untrusted|defensive filename" "$SKILL_MD" 2>/dev/null; then
  pass "defensive filename-handling rule present"
else fail "defensive filename-handling rule missing"; fi

# A12: wave/retry reliability engineering — capped concurrency and jittered
#     backoff must stay documented (529/429 protection).
if grep -iqE "waves of at most" "$SKILL_MD" 2>/dev/null \
   && grep -iqE "jitter" "$SKILL_MD" 2>/dev/null; then
  pass "capped-wave + jittered-retry reliability rules present"
else fail "capped-wave / jittered-retry rules missing"; fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: PASS (all checks)"
  exit 0
else
  echo "RESULT: FAIL ($FAIL check(s) failed)"
  exit "$FAIL"
fi
