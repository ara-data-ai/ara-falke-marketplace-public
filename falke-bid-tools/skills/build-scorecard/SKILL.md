---
name: scorecard
description: >
  Generate a Falke-branded board bid-comparison scorecard (PDF) from a
  bid-comparison matrix (.xlsx). TRIGGER on phrases like "create the Scorecard",
  "create the matrix scorecard", "build the scorecard", "regenerate the
  scorecard", or "refresh the scorecard" (case-insensitive) for a condo/HOA
  construction project. The matrix is normally a session-uploaded .xlsx (Claude
  Code: @path token from drag/drop or @-reference; Cowork:
  /sessions/<id>/mnt/uploads/<file>) — resolve the upload path per the rule in
  reference/runbook.md and ASK to confirm if ambiguous (do not guess
  most-recent). The SF basis is read PER RUN from THIS matrix and the user is
  asked to confirm it (--sf-confirmed) or override it (--sf-basis); the modeled
  baseline band (low/high/mid $M) is also required and confirmed. Every run ALSO
  REQUIRES two Falke-filled uploads — a scoring-framework .xlsx
  (--scoring-framework) and a category-scores .xlsx (--category-scores); there
  is NO fallback: the scorecard CANNOT be produced without both, weights and
  scores are never invented or reused from a previous run, and the render
  hard-stops (exit 2) without them. The render also hard-stops (exit 2) without
  an SF decision and without --baseline-confirmed; this skill PROMPTS for
  what's missing. The skill also asks WHERE to save, and after the run it
  produces a plain-English Scorecard Summary alongside the scorecard, runs an
  audit (FAIL stops delivery), and offers to DRAFT (never auto-send) a
  submission email. Output is Falke-branded per Anna's template.
argument-hint: "[matrix.xlsx] [--project-name ...] [--sf-basis ...] [--baseline baseline.xlsx] [--scoring-framework scoring-framework.xlsx] [--category-scores category-scores.xlsx]"
allowed-tools: Read, Bash(* -m scorecard.cli *), Bash(python3 -m scorecard.cli *), Bash(python3 -m pytest *)
---

# Falke Bid-Comparison Scorecard

Generate the Falke-branded board scorecard PDF by running the **scorecard
engine** (a Python package) over a bid matrix. Your job is to resolve the
uploaded matrix, gather the required parameters, run the engine, run the audit
step, and confirm the artifacts — not to recompute anything by hand.

## Engine location

The engine is the Python package bundled at:

```
${CLAUDE_PLUGIN_ROOT}/engines/scorecard
```

Run it as a module with the bootstrapped venv interpreter — see the exact
command in `reference/runbook.md`. This skill references that package as-is; it
does not duplicate it. The three blank fill-in templates Falke completes per job
live under `${CLAUDE_PLUGIN_ROOT}/engines/scorecard/templates/`.

## Resolving the uploaded matrix

The matrix almost always arrives as a session upload. Resolve the exact path
per the **Upload Detection** rule in `reference/runbook.md` (Claude Code uses
the `@path` token from drag/drop or an `@`-reference; Cowork uses
`/sessions/<id>/mnt/uploads/<file>`). If the path is ambiguous — multiple
`.xlsx` files in the upload area, or no clear path — **stop and ask the user
to confirm the exact path**. Do not guess "most recent."

## When to STOP and ask

This is a board deliverable, so do **not** guess inputs. Stop and ask the user
if any of these is missing — the engine hard-stops anyway and this skill
**prompts for missing parameters** before invoking the engine:

- `--matrix` — path to the bid-comparison `.xlsx` (resolve per Upload Detection)
- `--project-name` — board title (never inherit another project's)
- **SF basis** — read PER RUN from THIS matrix, then verified with the user (see
  Step 3). Every matrix carries its OWN square-footage and it differs every job;
  never reuse a remembered/fixed value. Surface the detected number and confirm
  it before using it for $/SF. The render hard-stops (exit 2) without either
  `--sf-confirmed` (accept the matrix SF) or `--sf-basis <value>` (override).
- **baseline xlsx** (`--baseline baseline.xlsx`) — Falke fills out the
  `baseline-template.xlsx` template (at
  `${CLAUDE_PLUGIN_ROOT}/engines/scorecard/templates/baseline-template.xlsx`)
  with the trade lines AND the band (low/mid/high $M), then uploads it. The
  engine reads both from the single file. A `.json` baseline still works too;
  the CLI flags `--band-low`/`--band-high`/`--mid` can still override the
  xlsx-supplied band values when given explicitly.
- **baseline confirmation** — the cost baseline must be previewed and explicitly
  confirmed by the owner before any render (see Step 4); the final run hard-stops
  (exit 2) without `--baseline-confirmed`.
- **scoring framework xlsx** (`--scoring-framework scoring-framework.xlsx`) —
  REQUIRED, no fallback. Falke fills out the `scoring-framework-template.xlsx`
  template (at
  `${CLAUDE_PLUGIN_ROOT}/engines/scorecard/templates/scoring-framework-template.xlsx`,
  pre-filled with Falke's current 8-category framework), then uploads it. Sheet
  `Scoring_Framework`: Category | Short Label | Weight (%) | What it captures;
  weights must sum to 100. Categories and weights can differ every run — the
  engine renders Sections D/E dynamically from this file (see Step 5).
- **category scores xlsx** (`--category-scores category-scores.xlsx`) —
  REQUIRED, no fallback. Falke fills out the `category-scores-template.xlsx`
  template (at
  `${CLAUDE_PLUGIN_ROOT}/engines/scorecard/templates/category-scores-template.xlsx`),
  then uploads it. Sheet `Category_Scores`: Firm | one column per Short Label;
  scores 1–10, one row per scored bidder. The Overall /100 is computed by the
  engine — never supplied. These two files supersede the old `--overrides`
  qual-scores JSON as the source of scores (see Step 5).
- **save location** — ask the user where to save the outputs (`--out-dir`) before
  rendering (see Step 6); do not assume a path.

If you have the matrix but not the parameters, ask for them; do not substitute
the matrix GSF, invent a band, or invent weights/scores.

## Workflow

1. **Resolve the matrix.** Detect the uploaded file per the Upload Detection
   section in `reference/runbook.md`. If ambiguous, confirm with the user.
   The engine consumes the workbook's `Leveled_Normalized` sheet by DEFAULT
   (Marvin's P0-7 ruling; legacy single-sheet matrices use their only sheet,
   with the legacy disclosure on the card). `--sheet Bid_Form` is for
   reconciliation/dispute/debug runs ONLY — never a board deliverable when a
   leveled sheet exists. See "Sheet selection" in `reference/runbook.md`.
2. **Collect parameters.** Get the four required parameters from the user
   (prompt if missing). For per-project inputs: the baseline uses the
   `baseline-template.xlsx` (Falke fills it out and uploads it; a `.json`
   baseline still works); the scoring framework and category scores use their
   own xlsx templates (see Step 5 — they supersede the old `--overrides`
   qual-scores JSON as the source of scores); for aliases, copy the blanks in
   `reference/inputs.md` and have the user fill them.
3. **Verify the SF basis from THIS matrix (REQUIRED).** Run the engine with
   `--preview-baseline` (it renders nothing and echoes the matrix-detected
   square-footage, labeled "SUGGESTED from matrix Row-10"). Read THAT number off
   THIS run — it is a per-run read of the submitted file, never a remembered or
   default value (every matrix has its own SF). Present it plainly and ask:
   *"The matrix lists **{N} SF**. I'll use that for the $/SF figures — is that
   correct, or should I use a different square-footage?"* Then **STOP for the
   answer.** If the user confirms → the final render uses `--sf-confirmed`. If the
   user gives a different number → the final render uses `--sf-basis <that>`. A
   render with neither hard-stops (exit 2). See `reference/runbook.md`.
4. **Confirm the baseline (REQUIRED).** The same `--preview-baseline` run also
   echoes the baseline — the trade-scope lines, the subtotal + OH&P, and the
   modeled band in both $ and $/SF — AND surfaces any baseline-anchoring
   fingerprint hits. Show the owner, with the honest framing: *"This baseline is
   the yardstick every bid is measured against. The tool detected that line X
   matches bidder Y's number within Z% — meaning the baseline may be partly
   derived from the bids rather than independent. Please confirm this baseline is
   correct, or tell me what to change."* Then **STOP and wait for the owner's
   explicit answer.** Do not proceed on silence or assumption.
   - **Confirmed** → the final render uses `--baseline-confirmed`.
   - **Changes** → the owner edits the baseline xlsx (trade lines and/or band
     values), then re-run `--preview-baseline` and re-confirm. Loop until confirmed.
   The final run MUST include `--baseline-confirmed`; the engine hard-stops
   (exit 2) without it.
5. **Collect the scoring framework + category scores (REQUIRED, no fallback).**
   Every real render needs BOTH Falke-filled xlsx files: the scoring framework
   (`--scoring-framework`) and the per-bidder category scores
   (`--category-scores`). If the user has not uploaded them, **STOP and ask** —
   tell the user plainly that there is NO fallback: the Scorecard CANNOT be
   produced without these two files; provide them or the run stops (exit 2).
   Point them at the two templates to fill out (under
   `${CLAUDE_PLUGIN_ROOT}/engines/scorecard/templates/`):
   `scoring-framework-template.xlsx` (pre-filled with Falke's current 8-category
   framework) and `category-scores-template.xlsx`. Never invent weights or
   scores, and never reuse a previous run's files unless the user names them —
   every run has different bidders and may carry different categories/weights
   (the engine renders Sections D/E dynamically from the framework file).
   `--preview-baseline` (Steps 3–4) does NOT require these files; only the real
   render does. See `reference/runbook.md`.
6. **Ask where to save (REQUIRED).** Before rendering, ASK the user where the
   outputs should go (the `--out-dir`) — do not assume a path.
7. **Run the engine.** Use the command in `reference/runbook.md` (include the
   SF decision from Step 3 — `--sf-confirmed` or `--sf-basis <value>` —
   `--baseline-confirmed` from Step 4, and `--scoring-framework` +
   `--category-scores` from Step 5). For a first build/QA run, add `--refit`
   to re-fit the Section C models and print them vs the published modeling
   ranges.
8. **Read the run log.** The engine prints a RUN LOG; surface any QA-fingerprint
   hits, duplicate drops, or completeness flags to the user (these are board
   disclosure items, not auto-fails).
9. **Run the audit step.** Run the post-engine audit per the **Audit Step**
   section in `reference/runbook.md`. It returns PASS / PASS-WITH-WARNINGS /
   FAIL. **A FAIL stops the ship** — do not present the deliverable. Surface
   PASS-WITH-WARNINGS items to the user before handing off.
10. **Confirm the artifacts.** Check `--out-dir` for `scorecard.pdf` (Falke-
    branded per Anna's template), `scorecard.html`, the auto-produced
    `scorecard_summary.html` + `scorecard_summary.pdf` (the plain-English winner +
    why + caveats companion the Falke reviewer reads — **surface it to the user**),
    `scorecard_run.json`, and `audit_report.md`. Report the coverage state from
    `scorecard_run.json` (`full_coverage`, `overall_label`) — see the coverage
    rule in `reference/runbook.md`.
11. **Offer to draft the submission email (ALWAYS).** After the scorecard +
    summary are produced, ALWAYS offer to DRAFT an email for submitting the
    scorecard (e.g. to the board / client) — subject + body, pulling the winner
    and key points from `scorecard_run.json` / the Scorecard Summary — for the
    user to review and send. Do NOT auto-send: sending on the user's behalf needs
    explicit per-instance permission; drafting is fine.
12. **Hand to Floyd.** This is a solution deliverable; it goes through Floyd's
    gate before it ships. The `scorecard_run.json` plus `audit_report.md` are
    the audit trail.

## Reference (load only when needed)

- `reference/runbook.md` — the exact run command, the inputs, the outputs, the
  Upload Detection rule, the Sheet-selection rule, the Audit Step, and the
  coverage/Overall rule (the one source of truth for how to invoke).
- `reference/inputs.md` — the per-project input templates and where to find the
  blanks and the synthetic sample validation examples.
- `reference/config.md` — the tunable config reference (what lives in
  `config/scorecard_config.yaml` and what is a CLI parameter).
- For the engine's own deep docs (modeling specs, package internals), see the
  bundled `engines/scorecard/` package and its docstrings.

## Eval

Before trusting a change to this skill, run the skill-behavior eval:
`eval/run_eval.sh` (see `eval/README.md`). It is separate from the engine's
modeling pytest suite and checks that the skill triggers and produces the
artifacts.
