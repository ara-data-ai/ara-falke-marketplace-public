# Config reference

The tunable config is the bundled
`engines/scorecard/config/scorecard_config.yaml` — the single source of truth
for every modeled coefficient (nothing fitted is hard-coded in the package).
That file is fully commented; read it directly for specifics. This page only
orients you.

## What is a CLI PARAMETER vs config

- **PARAMETER (per run, supply each time):** `sf_basis`, `band_low`,
  `band_high`, `modeled_mid_takeoff`, optional `variance_mid`, and the
  presentation labels (`region`, `region_full`, `pricing_year`). The shipped
  config ships these BLANK (null) on purpose, so every project is forced to
  supply its own. On a `--inputs` (run pack) run the band comes from the pack's
  `Baseline` tab and the band flags are refused (exit 2 — one channel per run);
  on the escape-hatch path it comes from the CLI band flags or the filled
  `baseline-template.xlsx`. The SF basis is never taken from either file — it is
  a per-run gate decision on the command line.
- **Config (tunable model, rarely changed):** tier fractions, the Section C
  volatility/drift coefficients, scoring weights, score anchors, QA
  tolerances, LLM rubric settings, and matrix-parse labels (including
  `matrix.sheet_name` — null = the ruled Leveled_Normalized default per
  Marvin P0-7; the CLI `--sheet` flag wins). The former Overall presentation
  curve block is RETIRED (P0-6): Overall is the honest weighted average.

## To retune

Edit the relevant block in `scorecard_config.yaml` and re-run with `--refit` to
confirm coefficients land in the published modeling ranges. Do NOT fill the
blank `run_inputs` in the shipped default — pass per-project values on the CLI
or a copied per-project config.
