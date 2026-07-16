# Per-project inputs

Per-project inputs live in the run pack (or, on the escape hatch, in xlsx/JSON
templates) — never in the shipped config.

## The run pack — `<Project> - Scorecard Inputs.xlsx` (the normal input)

Emitted by `create-matrix` beside the matrix on every clean run. Four tabs. The
operator never re-types a firm name; the pipeline already knows them.

| Tab | Filled by the pipeline | Filled by Falke |
|---|---|---|
| `Settings` | Pack/matrix format + run id, matrix file name, emitted-at, project name + address, the SF echo + label, the standing-framework reference, and the `Matrix Exclusions` block (bids the matrix itself dropped, with reasons) | **`Bid Opening Date` (REQUIRED)** and `Addenda Through`; optional `Display Aliases` (Matrix Name → Display Name) and `Additional Exclusions` (Firm + **a reason — mandatory**) |
| `Baseline` | nothing — the producer knows nothing about Falke's estimate | The band (`Band Low/High/Mid ($M)`), the trade-scope lines, and the provenance block: **`Provenance` (`independent` \| `bid-informed` \| `bid-derived`) and `Estimator of Record` are both REQUIRED**, plus basis date/documents and the MI/SIRS questions |
| `Framework` | Pre-filled with the starting framework (categories, short labels, weights, descriptions) | **`Framework Basis` (REQUIRED)**: `standing` \| `project-specific` \| `revised-post-opening`; a `Ruling Note` is required for anything but `standing`; a `Framework Lock Date` is required for `project-specific`. Edit the weights/categories if this run's plan differs (weights must sum to 100) |
| `Scores` | The `Firm` column — the matrix's own roster, in matrix order. **Do not edit it**; deleting a row does not exclude a bidder (use `Additional Exclusions`, with a reason) | **`Scoring Completed Date` (REQUIRED)** and the 1–10 grid, one column per framework Short Label. **A blank cell = not yet scored** (→ a PROVISIONAL run, exit 4); an all-blank grid stops the run. The Overall /100 is COMPUTED, never supplied |

**Score what you know; leave the rest blank.** A blank cell in the `Scores` grid
means **not yet scored** — always, never zero and never "doesn't apply". You do
not have to finish the grid before running: a partially-scored run renders a
PROVISIONAL card (exit 4) that reports the complete price picture and withholds
the ranking until the evaluation is done. That is the normal iterative path —
score after the matrix review and the interviews, re-run when the grid fills up.
The only floor is that an entirely blank grid stops the run (exit 2). Blanks
cost the bidder the category's full weight (nothing is rescaled), so a blank can
never flatter anyone.

Things worth telling an operator plainly:

- **Blank required cells hard-stop (exit 2)** naming the tab, the field and why
  it matters. That is the gate working, not an error — relay the message.
- **Don't reshape the pack.** Renamed labels, moved blocks, or extra fields
  hard-stop naming the field. Blocks are found by label, so re-emit the pack
  from the matrix run rather than editing its structure.
- **The two dates are the clocks the evaluation is audited against** (bid
  opening, scoring completed). A date the tool cannot read is a date it cannot
  audit — `YYYY-MM-DD`.
- **An exclusion is a ruling.** No silent drops: excluding a bidder requires a
  written reason, because the award file has to say why. The matrix's own
  exclusions are the matrix run's record and are re-derived from its AUDIT
  sheet — they are not editable here.

## Escape-hatch templates (legacy / archival / debugging only)

In the bundled `engines/scorecard/templates/` (these SHIP in the plugin and are
the files Falke fills out per job when there is no pack):

- `baseline-template.xlsx` → the Section A modeled baseline in ONE file: the
  trade-scope lines AND the band (low/mid/high $M). Pass with `--baseline`. The
  estimator's takeoff; the engine does NOT derive it from bidder numbers
  (circularity). `value` cells feed the QA fingerprint test. (A legacy
  `baseline.json` still works with `--baseline`.)
- `scoring-framework-template.xlsx` → sheet `Scoring_Framework` (Category |
  Short Label | Weight (%) | What it captures; weights sum to 100). REQUIRED on
  this path, pass with `--scoring-framework`. Sections D/E render dynamically
  from it.
- `category-scores-template.xlsx` → sheet `Category_Scores` (Firm | one 1–10
  column per Short Label; one row per scored bidder; a blank cell means not yet
  scored → a PROVISIONAL run). REQUIRED on this path,
  pass with `--category-scores`. The Overall /100 is COMPUTED, never supplied.
  These two xlsx files SUPERSEDE the old `--overrides` qual-scores JSON as the
  source of scores (passing `--overrides` now hard-stops, exit 2).

**Handing a template to an operator (REQUIRED behavior).** Never give a user a
bare `${CLAUDE_PLUGIN_ROOT}/...` path — in a managed Cowork session they cannot
open the read-only plugin install dir. **Copy the template into the session
output/upload area and offer the copy for download.** (On the pack path this
does not come up: the pack lands in the folder the operator chose for the
matrix, which they already have.)

## Blank JSON templates (escape-hatch optional inputs)

In the bundled `engines/scorecard/examples/_templates/`:

- `aliases.template.json` → optional short display names for the board card
  (`--aliases`).
- `exclusions.template.json` → optional human set-aside ruling (`--exclusions`,
  or `--exclude "Name,Name"`).

On a pack run these come from the `Settings` tab instead, and passing the flags
alongside `--inputs` hard-stops (exit 2). Prefer asking the operator in
conversation ("treat X and Y as the same firm; drop Z, and why") and writing it
onto the pack's Settings tab over handing a non-technical user JSON.

## Filled validation examples (synthetic sample)

In the bundled `engines/scorecard/examples/`: `sample_baseline.json`,
`sample_scoring_framework.xlsx`, `sample_category_scores.xlsx`,
`sample_aliases.json`, `sample_gold_overrides.json`. Use these as worked
examples of a filled set (all firms/figures are fictional).

## Matrix format the engine expects

Counts/widths are DETECTED, not assumed, but the matrix must follow the
structural format: a bidder-name row; a `COST / COST SUBTOTALS / $/SF / $/SF
SUBTOTALS` sub-header quartet below it; per-division `... SUBTOTAL` rows; and a
`GRAND TOTAL CONSTRUCTION COST` row (the compared total — never the pre-markup
`CONSTRUCTION COST SUBTOTAL`).
