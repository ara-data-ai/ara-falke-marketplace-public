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
- **Phase B (invocation smoke):** runs the engine on the synthetic sample
  validation inputs `--html-only`, asserts `scorecard.html` + `scorecard_run.json`
  are produced and the JSON carries a coverage flag, and asserts that omitting
  `--sf-basis` hard-stops (no silent matrix-GSF fallback). Phase B uses the
  bundled fixture at `engines/scorecard/examples/sample_matrix_fixture.xlsx`
  and auto-skips if it is missing.

## Manual companion

`trigger-cases.md` — the human checklist for whether Claude loads the skill on
the right requests (a script can't assert the model's tool selection).
