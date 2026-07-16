# Runbook — running the scorecard engine

The single source of truth for how to invoke the engine. The skill body points
here; do not restate the command elsewhere.

> The scorecard PDF is **Falke-branded** per Anna's template — the engine
> renders it that way by default; no separate branding step is required.

## Upload Detection (resolve the matrix path)

The matrix is normally a session upload. Resolve the exact path before
invoking the engine — never guess "most recent."

- **Claude Code (desktop / CLI):** the user supplies the matrix as an `@path`
  token (drag/drop into the prompt or an explicit `@`-reference). Use that
  exact path. If the user only typed a filename, expand it against the project
  workspace or the resolved `@`-path.
- **Cowork (managed session):** uploaded files land at
  `/sessions/<session-id>/mnt/uploads/<file>`. List that directory; if exactly
  one `.xlsx` matches the user's stated filename, use it.
- **Ambiguous case (REQUIRED behavior):** if the upload area contains
  **multiple `.xlsx` files**, or no clear `@path` token was provided, or the
  filename the user mentioned doesn't uniquely match a file, **stop and ask
  the user to confirm the exact path.** Do **not** pick the most-recently-
  modified file, do not infer from project-name similarity, and do not silently
  fall back to a previous run's matrix.

State the resolved path back to the user before running the engine.

## Sheet selection (ruled default — Marvin P0-7)

The engine decides WHICH sheet of the workbook it consumes; it is never a
workbook-ordering accident:

- **Default: `Leveled_Normalized`** when present — the apples-to-apples board
  comparison basis (grand totals are producer-verified identical to the
  as-submitted mirror; leveling reclassifies line items between trades, it
  never changes a bidder's price). No flag needed.
- **Legacy matrices** (single sheet, no leveled view — pre-leveling format)
  consume their only sheet; the card carries the legacy disclosure.
- **A producer workbook MISSING the leveled sheet hard-stops (exit 2)** naming
  the expected sheet. There is no silent fallback; re-generate the matrix or
  pass `--sheet` to make a non-default read an explicit, logged choice.
- **`--sheet Bid_Form` (mirror mode) is for reconciliation/verification,
  dispute support, or debugging ONLY.** A mirror run is an INTERNAL artifact —
  never deliver a board-facing final from the mirror when a leveled sheet
  exists (its division figures are not apples-to-apples, and its card renders
  a warning disclosure unconditionally). If Falke ever wants an "as-submitted"
  board view as a product feature, route that decision through Derick — it is
  not a flag flip on a render.

Every card renders the consumed-sheet disclosure line, and
`scorecard_run.json` records `sheet.name` / `sheet.mode` / `sheet.disclosure`.

The engine also checks the workbook's producer stamp (create-matrix writes
producer + format version as invisible document properties). An unstamped
workbook (legacy / pre-stamp) is allowed and logged; a stamp OUTSIDE the
scorecard's supported range hard-stops (exit 2) naming both versions —
regenerate the matrix or update the scorecard, never force-parse.

## Prompt-for-missing-parameters

The engine hard-stops if any of the four numeric/title parameters is missing.
**Before invoking the engine, prompt the user for any of these you don't
have** — do not substitute defaults, do not pull the SF basis from the matrix
GSF, and do not invent a band:

- `--project-name` — board title (no default).
- **SF basis** — read PER RUN from THIS matrix (see "SF basis — per-run read &
  verify" below). Never reuse a remembered/fixed value.
- `--band-low` / `--band-high` / `--mid` — the modeled baseline band ($M).

If the user supplies the matrix but not the parameters, ask for the parameters
explicitly and wait. If the user supplies the parameters but the matrix path
is ambiguous, resolve the path per Upload Detection above first.

## SF basis — per-run read & verify (REQUIRED)

Every matrix carries its OWN square-footage and it differs every job — read it
from THE submitted file for THIS run, never from a remembered/fixed value.

- `--preview-baseline` echoes the matrix-detected SF labeled "SUGGESTED from
  matrix Row-10". Read that number off THIS run and present it to the user:
  *"The matrix lists {N} SF. I'll use that for the $/SF figures — is that
  correct, or should I use a different square-footage?"* Then STOP for the answer.
- `--sf-confirmed` — pass this on the real render when the user accepts the
  matrix-detected SF.
- `--sf-basis <value>` — pass this on the real render when the user gives a
  different square-footage (override).
- A real render with NEITHER `--sf-confirmed` nor `--sf-basis` hard-stops with
  **exit 2**, naming the detected SF in the message.

## Baseline confirmation (REQUIRED gate, runs BEFORE the render)

The cost baseline is the yardstick every bid is scored against, and it can be
bid-derived rather than independent. The render is therefore gated on explicit
owner confirmation of the baseline.

- `--preview-baseline` — prints a human-readable echo of the baseline (trade-scope
  lines, subtotal + OH&P, modeled band in $ and $/SF) plus any baseline-anchoring
  fingerprint hits, and renders nothing. Run it with the SAME matrix, `--sf-basis`,
  band, and baseline inputs as the real run — no other change.
- `--baseline-confirmed` — REQUIRED for any real render. The engine hard-stops
  with **exit 2** if it is missing.

**Canonical flow:** preview → confirm → render. Run `--preview-baseline`, show the
owner the echo + any fingerprint hits with honest bid-anchoring framing, and STOP
for an explicit answer. If the owner changes the baseline, they edit the baseline
input and you re-run `--preview-baseline` and re-confirm — loop until confirmed.
Only then run the final scorecard WITH `--baseline-confirmed`. Never pass
`--baseline-confirmed` without an explicit owner confirmation on this baseline.

## Scoring-inputs gate (REQUIRED, no fallback)

Every real render needs TWO Falke-filled xlsx uploads — there is NO default and
nothing is reused from a previous run (every job has different bidders and may
carry a different framework):

- `--scoring-framework <scoring-framework.xlsx>` — sheet `Scoring_Framework`:
  Category | Short Label | Weight (%) | What it captures; weights must sum to
  100. The single source of Section D categories/weights and the Overall /100
  weighting. Pre-filled blank at
  `${CLAUDE_PLUGIN_ROOT}/engines/scorecard/templates/scoring-framework-template.xlsx`.
- `--category-scores <category-scores.xlsx>` — sheet `Category_Scores`: Firm |
  one 1–10 column per framework Short Label; one row per SCORED bidder. The
  single source of Section E scores; the Overall /100 is COMPUTED, never
  supplied. Blank at
  `${CLAUDE_PLUGIN_ROOT}/engines/scorecard/templates/category-scores-template.xlsx`.

A render missing either file — or failing validation (weights sum 100; score
columns match the framework short labels; scores 1–10; firms match the scored
bidder field) — hard-stops with **exit 2**. `--preview-baseline` does NOT
require them (it renders nothing). Passing the deprecated `--overrides` alongside
them also hard-stops (exit 2) — scores can never arrive from two sources.

## Command (generic)

```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/engines/scorecard" \
  "${CLAUDE_PLUGIN_DATA}/venv/bin/python" -m scorecard.cli \
  --matrix "<path to your bid matrix>.xlsx" \
  --project-name "<Project · Scope>" \
  --sf-confirmed  `# OR --sf-basis <SF> to override the matrix SF` \
  --band-low <low $M> --band-high <high $M> --mid <mid $M> \
  --baseline <your baseline.xlsx OR baseline.json> --baseline-confirmed \
  --scoring-framework <your scoring-framework.xlsx> \
  --category-scores <your category-scores.xlsx> \
  --out-dir <out dir>
```

`--baseline` accepts the filled `baseline-template.xlsx` (band + trade lines in
one file) OR a legacy `baseline.json`. `--scoring-framework` + `--category-scores`
are REQUIRED (no fallback) and SUPERSEDE the old `--overrides` qual-scores JSON —
passing `--overrides` now hard-stops (exit 2). See "Scoring-inputs gate" below.

**Ask the user where to save (REQUIRED).** Prompt for the `--out-dir` before
rendering — do not assume a path.

**HTML-only switch (REQUIRED check before the real render).** The bootstrap hook
writes a render-mode marker at `${CLAUDE_PLUGIN_DATA}/render-mode` (`chromium`
or `html-only`, depending on whether the Chromium install succeeded). Read it
and append `--html-only` when it says `html-only`:

```bash
if [ "$(cat "${CLAUDE_PLUGIN_DATA}/render-mode" 2>/dev/null)" = "html-only" ]; then
  HTML_ONLY="--html-only"
else
  HTML_ONLY=""
fi
# ... add $HTML_ONLY to the scorecard.cli invocation above.
```

Add `--refit` on the first build/QA run to re-fit the Section C models
(volatility + drift) with scipy and print them against the published modeling
ranges. (The Overall presentation curve is RETIRED — P0-6; Overall is the
honest weighted average and nothing adjusts it.) (`--html-only`
is also forced automatically by the render-mode marker above when no PDF engine
is available.)

Run `python3 -m scorecard.cli --help` for the full flag list — argparse is the
authoritative contract for every option (optional flags: `--sheet`,
`--qual-notes`, `--aliases`, `--exclude`, `--exclusions`, `--variance-mid`,
`--region`, `--region-full`, `--baseline-year`, `--config`, `--engine`).

## Required vs optional (hard-stop behavior)

Required each run — `--project-name` has no default; the engine STOPS without
`--matrix`, `--project-name`, the band (`--band-low`/`--band-high`/`--mid`), and
an SF decision. For SF, a real render needs EITHER `--sf-confirmed` (accept the
matrix-detected SF) OR `--sf-basis <value>` (override) — neither hard-stops with
exit 2, naming the detected SF (see "SF basis — per-run read & verify"). Plus
`--baseline-confirmed` is REQUIRED for any real render (hard-stops exit 2 without
it). `--preview-baseline` is the no-render preview mode that echoes the detected
SF and the baseline to satisfy both gates. Everything else is optional.

## Outputs (`--out-dir`)

- `scorecard.pdf` — the Falke-branded board deliverable.
- `scorecard.html` — preview.
- `scorecard_summary.html` + `scorecard_summary.pdf` — auto-produced on every
  real render: the plain-English Scorecard Summary (winner + why + caveats,
  Falke-branded), the companion the Falke reviewer reads. Surface it to the user.
- `scorecard_run.json` — provenance/audit: `run_id`, `full_coverage`,
  `overall_label`, the consumed sheet (`sheet.name` / `sheet.mode` /
  `sheet.disclosure`), per-bidder rank/total/$/SF/tier/overall, and the run log.
- `audit_report.md` — written by the Audit Step (see below).

## Audit Step (runs AFTER the engine, BEFORE ship)

The engine emits the artifacts; the audit step then validates them and writes
`audit_report.md` to `--out-dir`. **Run the audit before presenting the
deliverable to the user.** It returns one of three verdicts:

- **PASS** — artifacts are clean; present the deliverable and hand to Floyd's
  gate.
- **PASS-WITH-WARNINGS** — artifacts are usable but carry disclosure items
  (e.g., partial qualitative coverage, QA-fingerprint hits, duplicate drops,
  missing optional inputs). **Surface every warning to the user** before
  handing off; the warnings are board-disclosure items, not auto-fails.
- **FAIL** — artifacts are not shippable (e.g., engine error, missing PDF,
  `scorecard_run.json` malformed, parameters silently substituted,
  Falke-branding template not applied). **FAIL stops the ship.** Do not
  present the deliverable; report the failing checks to the user and do not
  route to Floyd until they are fixed and the audit re-runs to PASS or
  PASS-WITH-WARNINGS.

The audit reads `scorecard_run.json` plus the rendered `scorecard.pdf` /
`scorecard.html`, so it must run from the same `--out-dir` the engine wrote.

## The coverage / Overall-/100 rule (read before reporting results)

The Overall /100 column IS the honest weighted average of the framework-
weighted category scores — nothing adjusts it (the former presentation curve
was RETIRED under P0-6 because it could re-order the award ranking). At
partial qualitative coverage the engine ships **provisional weighted
averages** (never rescaled) and labels the column PROVISIONAL with the
coverage %. Check `full_coverage` / `overall_label` in `scorecard_run.json`
and report which state the run is in. Do not present a provisional run as
final.

## PDF engine

Default `--engine chromium` (Chromium/Playwright, installed in the Falke env;
renders with `print_background=True` + `prefer_css_page_size=True`). `weasyprint`
is the optional alternative. Do NOT run inline `pip install` — package installs
are denied by environment policy.

## Synthetic sample validation values

The validation run (reproduces the sample card) lives in the bundled
`engines/scorecard/examples/sample_run.yaml`. Use it as the worked example; it
is NOT a default, and all firms/figures are fictional.

## Submission email — DRAFT only (never auto-send)

After the scorecard + summary are produced, ALWAYS offer to DRAFT a submission
email (e.g. to the board / client): subject + body, pulling the winner and key
points from `scorecard_run.json` / the Scorecard Summary, for the user to review
and send. DRAFTING is fine; do NOT auto-send — sending on the user's behalf needs
explicit per-instance permission.
