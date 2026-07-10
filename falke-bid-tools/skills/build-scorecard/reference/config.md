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
  supply its own via CLI flags or its own `--config` file.
- **Config (tunable model, rarely changed):** tier fractions, the Section C
  volatility/drift coefficients, the Overall presentation curve (`apply_curve`
  default `false`), scoring weights, score anchors, QA tolerances, LLM rubric
  settings, and matrix-parse labels.

## To retune

Edit the relevant block in `scorecard_config.yaml` and re-run with `--refit` to
confirm coefficients land in the published modeling ranges. Do NOT fill the
blank `run_inputs` in the shipped default — pass per-project values on the CLI
or a copied per-project config.
