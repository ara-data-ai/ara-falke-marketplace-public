# Per-project inputs

Per-project inputs live in xlsx templates / JSON / CLI, never in the shipped
config. Copy the blanks, fill them for the project, pass them on the CLI.

## Fill-in xlsx templates (the primary per-job inputs)

In the bundled `engines/scorecard/templates/` (these SHIP in the plugin and are
the files Falke fills out per job):

- `baseline-template.xlsx` → the Section A modeled baseline in ONE file: the
  trade-scope lines AND the band (low/mid/high $M). Pass with `--baseline`. The
  estimator's takeoff; the engine does NOT derive it from bidder numbers
  (circularity). `value` cells feed the QA fingerprint test. (A legacy
  `baseline.json` still works with `--baseline`.)
- `scoring-framework-template.xlsx` → sheet `Scoring_Framework` (Category |
  Short Label | Weight (%) | What it captures; weights sum to 100). REQUIRED,
  pass with `--scoring-framework`. Pre-filled with Falke's current 8-category
  framework; edit categories/weights per run. Sections D/E render dynamically
  from it.
- `category-scores-template.xlsx` → sheet `Category_Scores` (Firm | one 1–10
  column per Short Label; one row per scored bidder). REQUIRED, pass with
  `--category-scores`. The Overall /100 is COMPUTED, never supplied. These two
  xlsx files SUPERSEDE the old `--overrides` qual-scores JSON as the source of
  scores (passing `--overrides` now hard-stops, exit 2).

## Blank JSON templates (optional inputs)

In the bundled `engines/scorecard/examples/_templates/`:

- `aliases.template.json` → optional short display names for the board card
  (`--aliases`).
- `exclusions.template.json` → optional human set-aside ruling (`--exclusions`,
  or `--exclude "Name,Name"`).

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
