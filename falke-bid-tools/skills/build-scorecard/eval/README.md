# Scorecard SKILL-behavior eval

Verifies the **skill** behaves correctly — distinct from the modeling tests in
the bundled `engines/scorecard/tests/` (those validate the math/curves).

## Run it

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/build-scorecard/eval/run_eval.sh"
```

(The script resolves its own location, so it also runs from a plain relative
path inside the bundle.)

Exit 0 = all checks pass. Non-zero = number of failed checks.

## What it covers

- **Phase A (static, always runs):** SKILL.md has the four frontmatter keys
  (`name`, `description`, `allowed-tools`, `argument-hint`); the body uses
  progressive disclosure (references the three `reference/` files, which exist);
  no duplicated run command in the body; the description carries a trigger verb.
  Plus the **run-pack contract** (P1-4): the pack is documented as the primary
  path by flag and by artifact name; **pre-filled is not pre-confirmed**
  (`--inputs` implies NEITHER `--sf-confirmed` NOR `--baseline-confirmed`); the
  one-channel-per-run mutual-exclusion rule and its edit-the-pack remedy; the
  escape hatch's legitimate uses plus the integrity-smell question; the honest
  W8 language (Falke has no standing framework, so the docs must not imply a
  drift control exists); and the no-pack-on-a-quarantined-matrix rule.

  Prose checks grep a **normalized** copy of the doc (`flat()` — blockquote/list
  markers and markdown emphasis stripped, lines joined). These files are wrapped
  prose: a line-based grep for a phrase fails the moment a sentence reflows or a
  word is bolded, and a check that breaks on reflow is a check that gets
  weakened by whoever hits it on a deadline.
- **Phase B (invocation smoke):** runs the FULL gated render `--html-only` —
  `--sf-confirmed`, `--baseline-confirmed`, and BOTH required scoring inputs
  (the tracked `scoring-framework-template.xlsx` as the framework; a synthetic
  category-scores xlsx generated into the temp dir at eval time) — and asserts
  `scorecard.html` + `scorecard_run.json` + `audit_report.md` are produced,
  the JSON carries a coverage flag, and that omitting the SF decision
  hard-stops **at the SF gate** (the stop message is asserted, so a different
  gate can't false-pass the check). Phase B's matrix is the synthetic
  producer-written fixture `engines/scorecard/tests/fixtures/
  create_matrix_4bidders.xlsx` (v0.3-era output that parses today — the LIVE
  cross-engine compat test against the current producer is a separate pytest
  gate, per the Floyd 2026-07-15 ruling). The fixture is untracked-local
  (`.gitignore` excludes `*.xlsx`), so Phase B auto-skips where it is absent
  (e.g. the scrubbed release stage) — set `REQUIRE_PHASE_B=1` to make that a
  FAIL instead (release.sh does, on the canonical-tree gate).

  Phase B also asserts two **run-pack CLI contracts the skill text promises**:
  `--inputs` alongside an individual flag hard-stops on the mutual-exclusion
  rule, and `--inputs` pointed at a non-pack workbook hard-stops naming the
  missing tabs. Neither needs a pack fixture (the exclusion check runs before
  the pack is opened), which keeps this smoke on the SKILL wiring. The live
  producer→consumer pack suite — emitting a pack with the current matrix engine
  and rendering through this scorecard — is
  `engines/scorecard/tests/test_producer_live_compat.py`, a pytest gate
  release.sh already runs. Do not duplicate it here.

The engine interpreter resolves in this order, and failures name the resolved
path: `SCORECARD_PYTHON` env override → `${CLAUDE_PLUGIN_DATA}/venv/bin/python`
(the bootstrap venv) → `/usr/bin/python3`. If the resolved interpreter lacks
the engine deps, the eval FAILS loudly naming the missing module(s) — it never
falls back to bare `python3`.

## Manual companion

`trigger-cases.md` — the human checklist for whether Claude loads the skill on
the right requests (a script can't assert the model's tool selection).
