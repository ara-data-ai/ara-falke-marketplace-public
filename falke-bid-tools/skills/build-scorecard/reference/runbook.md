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

## Input channel — the run pack (`--inputs`) vs the individual flags

Every render needs the baseline, the framework and the scores. They arrive by
**one channel or the other, never both.**

### The pack (`--inputs <Project> - Scorecard Inputs.xlsx`) — the normal path

`create-matrix` writes the pack beside the matrix at the end of every clean run
(its Stage 6c). Four tabs — `Settings` / `Baseline` / `Framework` / `Scores` —
with the bidder roster, the project identity, the SF echo and the starting
framework already filled by the pipeline; Falke fills the band and the scores
offline. One `--inputs` supplies **the baseline (band + trade lines), the
framework, the scores, the display aliases and the exclusions.** Tab-by-tab
detail: `reference/inputs.md`.

- **The band comes from the pack's `Baseline` tab** — and on a pack run you
  *can't* pass `--band-low`/`--band-high`/`--mid` instead: the run stops
  (exit 2) naming the conflicting flag. That is the gate working, not an error.
  The pack is the one home for the band, and the operator confirmed the pack's
  band at preview — a flag that quietly replaced it afterwards would invalidate
  the confirmation they just gave. If the band is wrong, edit the pack's
  `Baseline` tab and re-preview.
- **`--project-name` is still required** on the command line, every run.
- **The SF echo on `Settings` is a suggestion, not an answer.** If it diverges
  from the matrix the engine WARNs; adopting it takes an explicit
  `--sf-basis <value>`. A suggestion does not become a confirmation by
  traveling through a spreadsheet.
- **No pack on a quarantined matrix.** A matrix run that exited **3** (loud
  quarantine — the pipeline's post-write self-check failed on ≥1 figure) emits
  **no pack**, deliberately: that workbook carries a "verify the flagged figures"
  banner, and a scoring kit for it would invite scoring a workbook the producer
  has disowned. If the operator has a matrix and no pack, check whether the
  matrix run quarantined — the answer is to fix the matrix and re-run it, not to
  hand-build inputs.

### Mutual exclusion (binding — exit 2)

`--inputs` is mutually exclusive with **every flag that supplies a fact the pack
supplies**: `--baseline`, `--scoring-framework`, `--category-scores`,
`--aliases`, `--exclude`, `--exclusions`, and the band —
`--band-low`, `--band-high`, `--mid`. Passing both hard-stops (exit 2) naming
each conflicting flag. There are deliberately **no merge semantics and no
precedence rules** — not even "the flag wins". A pack plus an overriding flag
produces a card whose `Settings` tab says one thing and whose inputs say
another. **If the pack is wrong, edit the pack.**

`--sf-basis`, `--sf-confirmed` and `--baseline-confirmed` are **not** in that
list and never will be: they are not input channels, they are gate decisions,
and they always come from the command line — in every mode, including
`--inputs`.

### The escape hatch (individual flags)

Legitimate indefinitely for: **legacy matrices** (predate the pack), **archival
re-renders** from hand-built inputs, and **ARA engineering/debugging**. Used
against a pipeline-stamped matrix that has a pack, it is an integrity smell —
ask the named question in SKILL.md, do not block. The run records
`input_channel: pack | individual` in `scorecard_run.json`, and the audit WARNs
on `individual` against a stamped matrix.

## Prompt-for-missing-parameters

**Before invoking the engine, prompt the user for any of these you don't
have** — do not substitute defaults, do not pull the SF basis from the matrix
GSF, and do not invent a band:

- `--project-name` — board title (no default).
- **SF basis** — read PER RUN from THIS matrix (see "SF basis — per-run read &
  verify" below). Never reuse a remembered/fixed value.
- **the input channel** — the pack (`--inputs`), or the individual files on the
  escape-hatch path. On the escape-hatch path only, the band
  (`--band-low`/`--band-high`/`--mid`) comes from the CLI or from the filled
  `baseline-template.xlsx`.

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

Every real render needs the framework and the scores. There is NO default and
nothing is reused from a previous run (every job has different bidders and may
carry a different framework). **A pack satisfies this gate by SUPPLYING them**
— it does not bypass it: a pack whose `Framework` or `Scores` tab is unusable
hard-stops (exit 2) with the same shape of message these files do.

On the escape-hatch path, the two Falke-filled xlsx uploads are:

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
columns match the framework short labels; any score present is 1–10; firms match
the scored bidder field; the grid is not entirely blank) — hard-stops with
**exit 2**. A blank CELL is not a validation error: it means not-yet-scored and
produces a PROVISIONAL run (exit 4). `--preview-baseline` does NOT
require them (it renders nothing). Passing the deprecated `--overrides` alongside
them also hard-stops (exit 2) — scores can never arrive from two sources.

## Command — the pack path (normal)

```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/engines/scorecard" \
  "${CLAUDE_PLUGIN_DATA}/venv/bin/python" -m scorecard.cli \
  --matrix "<path to your bid matrix>.xlsx" \
  --inputs "<Project> - Scorecard Inputs.xlsx" \
  --project-name "<Project · Scope>" \
  --sf-confirmed  `# OR --sf-basis <SF> to override the matrix SF` \
  --baseline-confirmed \
  --out-dir <out dir>
```

The preview is the same command with `--preview-baseline` and neither
confirmation flag — the pack works identically in preview mode (it echoes the
baseline from the pack's `Baseline` tab and the SF from the matrix, and renders
nothing).

## Command — the escape hatch (legacy / archival / debugging)

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
are REQUIRED on this path (no fallback) and SUPERSEDE the old `--overrides`
qual-scores JSON — passing `--overrides` now hard-stops (exit 2). See
"Scoring-inputs gate" below. On this path the band flags override the xlsx band
values when supplied explicitly — that precedence exists **only here**, where
there is one input channel to begin with. Mixing any of these flags with
`--inputs` hard-stops (exit 2).

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
`--qual-notes`, `--variance-mid`, `--region`, `--region-full`,
`--baseline-year`, `--config`, `--engine`; `--aliases`, `--exclude` and
`--exclusions` are escape-hatch-only — on a pack run they come from the
`Settings` tab and passing them alongside `--inputs` hard-stops).

## Required vs optional (hard-stop behavior)

Required each run: `--matrix`, `--project-name` (no default), an input channel
(`--inputs`, or the individual files), the band, and an SF decision.

- **Band** — from the pack's `Baseline` tab on the pack path (the band flags
  are refused there, exit 2); from `--band-low`/`--band-high`/`--mid` or the
  filled `baseline-template.xlsx` on the escape-hatch path, where a flag
  overrides the xlsx value. Missing entirely → hard-stop.
- **SF** — a real render needs EITHER `--sf-confirmed` (accept the
  matrix-detected SF) OR `--sf-basis <value>` (override); with neither it
  hard-stops (exit 2) naming the detected SF. `--inputs` does not satisfy this.
- **Baseline** — `--baseline-confirmed` is REQUIRED for any real render
  (hard-stops exit 2 without it). `--inputs` does not satisfy this either.

`--preview-baseline` is the no-render preview mode that echoes the detected SF
and the baseline so the owner can answer both gates. Everything else is optional.

## Pack binding (what the engine checks, and what it asks YOU to relay)

On a pack run the engine binds the pack to the matrix **before** the gates and
before any artifact. Hard stops (exit 2, nothing rendered): a project-identity
mismatch (wrong building — no legitimate reading), a firm-roster mismatch
between the pack's `Scores` tab and the matrix, an edited producer-filled field,
an unreadable/unstamped-by-an-unknown-producer pack, or a pack whose format is
outside this scorecard's supported range (pack and scorecard ship in the same
plugin at the same version — update one or re-emit the other).

Two tiers WARN instead and **ask the operator to confirm** — relay them and get
an answer:

- **I5 — different matrix run, roster reconciles.** The pack was emitted by a
  different run of the matrix (e.g. the matrix was corrected and re-run after
  scoring began). The firms, project and SF all reconcile, so it is most likely
  the corrected re-run — confirm it is the matrix they mean.
- **I7 — unstamped matrix.** The matrix carries no run identity (legacy or
  hand-built). The roster reconciles, but the inputs cannot be proven
  pipeline-originated.

Both land in `scorecard_run.json` under `pack.binding` and in the audit. A clean
bind (I4) is not a warning and needs no relay.

## Standing evaluation framework — what the card can and cannot claim

The pack's `Framework` tab carries a **declaration** (`Framework Basis`:
`standing` | `project-specific` | `revised-post-opening`) and the card discloses
the evaluation plan from that declaration. Two honest facts to hold onto:

- **Falke has no standing evaluation framework artifact today.** Every run
  therefore takes the bootstrap path: the pack is pre-filled with ARA's shipped
  default framework as **starting content**, `Settings` records `Standing
  Framework Version = none (shipped default)`, the weights-drift check WARNs,
  and the card says *"No standing evaluation framework was on file for this run;
  the categories and weights below were supplied for this project."* It claims
  nothing further, and neither should you — **do not tell an operator that a
  drift control is protecting them.** ARA's shipped default is a vendor artifact,
  not Falke's policy; measuring drift from it would be the tool asserting a fact
  it does not know.
- `create-matrix` takes an optional `--standing-framework <standing-framework.xlsx>`
  for when Falke adopts a versioned, dated framework file of their own. Until
  they do, there is nothing to pass.

If the operator declares anything other than `standing` on the `Framework` tab,
a `Ruling Note` is required (the pack hard-stops without one), and
`revised-post-opening` is disclosed on the card unconditionally — weights set
after bids were opened are the board's business.

## Outputs (`--out-dir`)

On a clean run (exit 0):

- `scorecard.pdf` — the Falke-branded board deliverable.
- `scorecard.html` — preview.
- `scorecard_summary.html` + `scorecard_summary.pdf` — auto-produced on every
  real render: the plain-English Scorecard Summary (winner + why + caveats,
  Falke-branded), the companion the Falke reviewer reads. Surface it to the user.

Machine-read, stable names on every run (they never travel to a board):

- `scorecard_run.json` — provenance/audit: `run_id`, `full_coverage`,
  `overall_label`, `watermark` (the PRELIMINARY reason tokens; empty on a clean
  run), the consumed sheet (`sheet.name` / `sheet.mode` / `sheet.disclosure`),
  `input_channel` (`pack` | `individual`), the `pack` block on a pack run (pack
  file + format, matrix run id, binding tier, the bid-opening and
  scoring-completed dates, the framework declaration + hash, the standing
  reference, the baseline provenance declaration, and every recorded exclusion
  with its reason), and per-bidder rank/total/$/SF/tier/overall — where `rank` is
  **null on a provisional run** (the rank is absent, not blank, so a reader can
  tell "not ranked" from "rank 0").
- `audit_report.md` + `audit.json` — written by the in-engine self-audit.

On a blocked (exit 3), provisional (exit 4) or unaudited (`--no-audit`) run, the
four board-facing files above become `scorecard-PRELIMINARY.*` and
`scorecard_summary-PRELIMINARY.*`, watermarked on every page. Same content shape,
different name, and not for a board.

## The self-audit (IN-ENGINE, default ON, runs BEFORE the render)

**There is no separate audit step to run.** The audit is part of the engine, on
by default, and since P1-1 it runs **before** the artifacts are rendered — which
is what lets a blocked run stamp its own status onto the card. It reads the
pipeline result (never the rendered HTML) and writes `audit_report.md` +
`audit.json` to `--out-dir`. Its verdict is folded into the exit code:

- **PASS** — clean → **exit 0**. Present the deliverable, hand to Floyd's gate.
- **PASS-WITH-WARNINGS** — usable, carries disclosure items (QA-fingerprint
  hits, duplicate drops, the standing-framework W8 warning, missing optional
  inputs) → **exit 0**. **Surface every warning**; they are board-disclosure
  items, not auto-fails. This is the normal state of an honest run today: W8
  fires on every run until Falke adopts a standing framework, so treating a
  warned run as non-clean would make the abnormal signal the normal state.
- **FAIL** — a BLOCKER → **exit 3**. The artifacts still exist, renamed
  `scorecard-PRELIMINARY.*` and watermarked. **Lead with the disclosure**, report
  the failing checks, and do not route to Floyd until the run repeats clean.

`--no-audit` skips it: **prohibited for board runs**, debugging only. The run
exits 0 (nothing failed; the operator asked to skip) but nothing was checked, so
every page is stamped **"PRELIMINARY — not audited"** and the files are renamed.

## Exit-code contract (v2)

| Exit | Meaning | Artifacts? | Deliverable? |
|------|---------|-----------|--------------|
| 0 | Clean — audit PASS or PASS-WITH-WARNINGS | Yes | Yes |
| 1 | Environment / nothing to do — bad `--matrix` path, missing dependency | **No** | — |
| 2 | Input-gate hard stop — the gate WORKING | **No** | — |
| 3 | Delivered WITH an audit blocker | Yes | **No** |
| 4 | Delivered PROVISIONAL — qualitative evaluation incomplete | Yes | **No** |

Precedence **3 > 4** (a blocked provisional run is a blocked run — the louder
class wins, mirroring the matrix engine's contract). Exit 1 means the same thing
here as it does in the matrix engine — *environment, nothing written* — which is
the collision P1-1 fixed: exit 1 used to mean "everything was written and you
must not deliver it," the exact opposite instruction under the same number.

**The filename is the deliverability signal.** An artifact the engine will not
vouch for is not called `scorecard`: it is `scorecard-PRELIMINARY.*` /
`scorecard_summary-PRELIMINARY.*`, watermarked page-wide with its composed
reasons — `PRELIMINARY — evaluation incomplete · audit blocker`. Reasons compose;
they never suppress each other. An exit code does not survive a screenshot into a
board packet — the name and the mark do. `scorecard_run.json`, `audit_report.md`
and `audit.json` keep stable names: they are machine-read and never travel.

Handle-exit-N guidance for the operator conversation lives in SKILL.md.

## The coverage / Overall-/100 rule (read before reporting results)

The Overall /100 column IS the honest weighted average of the framework-weighted
category scores — nothing adjusts it (the former presentation curve was RETIRED
under P0-6 because it could re-order the award ranking).

**A blank score means "not yet scored" — always.** Never zero, never a middle
value, never "doesn't apply". Blanks are safe because the engine does **not**
rescale: an unscored category costs the bidder its full weight, so a blank is
self-penalizing and can never be used to advantage a favourite. (That is also why
there is no `n/a` sentinel — dropping a category from one bidder's denominator
would raise their number by removing evidence.)

At **partial coverage the run is PROVISIONAL (exit 4)** and the card withholds
the comparison rather than hedging it: no rank, no leader, no Overall number
(the column reads "Pending — N of M categories outstanding"), bidders listed
alphabetically. The withholding is **run-wide, not per bidder** — a card printing
78 next to "Pending" would make the comparison it just refused to make, with the
fully-scored bidder reading as leader by default.

The one floor: a grid with **every** cell blank hard-stops (exit 2). The tool
refuses to render nothing; it never refuses to render little. The leveled matrix
already carries every price fact on its own, so a scorecard with nothing
qualitative to combine is the matrix at lower resolution in a board document's
clothes.

Check the exit code first; `full_coverage` / `overall_label` / `coverage` in
`scorecard_run.json` carry the detail. Never present a provisional run as final.

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
