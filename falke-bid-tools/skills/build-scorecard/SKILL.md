---
name: scorecard
description: >
  Generate a Falke-branded board bid-comparison scorecard (PDF) from a
  bid-comparison matrix (.xlsx). TRIGGER on phrases like "create the Scorecard",
  "create the matrix scorecard", "build the scorecard", "regenerate the
  scorecard", or "refresh the scorecard" (case-insensitive) for a condo/HOA
  construction project. The normal path is ONE upload beside the matrix: the
  "<Project> - Scorecard Inputs.xlsx" run pack that create-matrix emits, which
  carries the baseline, the framework, the scores, the aliases and the
  exclusions (--inputs). The skill still asks the two owner decisions the pack
  never answers — the SF basis and the baseline confirmation — plus where to
  save, then runs the engine and acts on its exit code: a run that is blocked or
  still being scored is watermarked PRELIMINARY, is not ranked, and gets no
  submission-email offer. On a clean run it offers to DRAFT (never auto-send) a
  submission email. Output is Falke-branded per Anna's template.
argument-hint: "[matrix.xlsx] [inputs-pack.xlsx] [--project-name ...] [--sf-basis ...]"
allowed-tools: Read, Bash(* -m scorecard.cli *), Bash(python3 -m scorecard.cli *), Bash(python3 -m pytest *)
---

# Falke Bid-Comparison Scorecard

Generate the Falke-branded board scorecard PDF by running the **scorecard
engine** (a Python package) over a bid matrix. Your job is to resolve the
uploaded files, gather the decisions the engine cannot make, run the engine, and
then **act on the exit code it returns** — not to recompute anything by hand. The
self-audit is inside the engine and runs before the render; there is no separate
audit step for you to run.

## Engine location

The engine is the Python package bundled at:

```
${CLAUDE_PLUGIN_ROOT}/engines/scorecard
```

Run it as a module with the bootstrapped venv interpreter — see the exact
command in `reference/runbook.md`. This skill references that package as-is; it
does not duplicate it.

## The run pack — the normal path (ONE upload)

`create-matrix` ends every clean run by writing a **`<Project> - Scorecard
Inputs.xlsx`** beside the matrix. That workbook is the scorecard's input
channel: four tabs (`Settings` / `Baseline` / `Framework` / `Scores`) with the
bidder names, the project identity, the SF echo and the starting framework
**already filled in by the pipeline**. Falke fills in the baseline band and the
scores offline, then brings back that ONE file:

```
--matrix "<the matrix>.xlsx"  --inputs "<Project> - Scorecard Inputs.xlsx"
```

It replaces the three separate uploads (baseline + scoring framework + category
scores) and the aliases/exclusions flags. **Ask for it by name.** If the
operator has the matrix but not the pack, it is sitting in the same folder the
matrix was saved to — have them look there before you reach for the escape
hatch below.

**Pre-filled is NOT pre-confirmed.** `--inputs` does **not** imply
`--sf-confirmed` and does **not** imply `--baseline-confirmed`. Both gates run
exactly as they always have, per run, answered by a human on the command line.
The pack carries **data**; the gates consume **decisions**. There is no
confirmation field anywhere in the pack, and hand-adding one (e.g. an
`sf_confirmed` row on `Settings`) hard-stops the run (exit 2) naming the field —
that is the gate working, not an error.

**One channel per run.** `--inputs` is mutually exclusive with every flag that
supplies a fact the pack supplies — `--baseline`, `--scoring-framework`,
`--category-scores`, `--aliases`, `--exclude`, `--exclusions`, and the band
flags `--band-low`/`--band-high`/`--mid`. Passing both = exit 2 naming the
conflicting flag, by design: there are no precedence rules between two input
channels. You cannot patch a pack with a flag, and that is the point — **if
something in the pack is wrong, the operator edits the pack** (the Baseline tab,
the Scores grid) and you re-run.

## When to STOP and ask

This is a board deliverable, so do **not** guess inputs. Stop and ask if any of
these is missing — the engine hard-stops anyway, and this skill **prompts before
invoking the engine**:

- `--matrix` — path to the bid-comparison `.xlsx` (resolve per Upload Detection
  in `reference/runbook.md`; if ambiguous, ask — never guess "most recent").
- `--inputs` — the run pack (or, on the escape-hatch path, the individual
  `--baseline` + `--scoring-framework` + `--category-scores` files; **there is
  NO fallback** — with neither channel the scorecard CANNOT be produced and the
  render hard-stops, exit 2).
- `--project-name` — the board title. Still an operator input on every run; the
  pack does not supply it.
- **SF basis** — read PER RUN from THIS matrix and confirmed by the owner (Step
  2). Every matrix carries its own square-footage; never reuse a remembered
  value. The pack's SF cell is an **echo**, not an answer.
- **baseline confirmation** — the owner confirms the baseline before any render
  (Step 2).
- **save location** — ask where the outputs go (`--out-dir`); do not assume.

## Workflow

1. **Resolve the inputs and state them back.** Open with the full manifest —
   the matrix, the run pack, a project name, and a save location — and name what
   you have and what is missing, so the operator knows in one glance whether
   they can finish today. The engine consumes the workbook's
   `Leveled_Normalized` sheet by DEFAULT (Marvin's P0-7 ruling); `--sheet
   Bid_Form` is for reconciliation/dispute/debug runs ONLY, never a board
   deliverable when a leveled sheet exists. See `reference/runbook.md`.
2. **Preview once; confirm both gates together.** Run `--preview-baseline`
   (with `--inputs` — it renders nothing and works the same on either channel).
   ONE preview echoes BOTH the matrix-detected SF and the baseline, so present
   them together and ask for both decisions in one message:
   - *"The matrix lists **{N} SF** — I'll use that for the $/SF figures. Correct,
     or a different square-footage?"*
   - *"This baseline is the yardstick every bid is measured against. [Relay any
     fingerprint hit verbatim: line X matches bidder Y's number within Z%, which
     means the baseline may be partly derived from the bids rather than
     independent.] Confirm it, or tell me what to change."*

   Then **STOP for two explicit answers.** Do not proceed on silence. SF
   confirmed → `--sf-confirmed`; a different number → `--sf-basis <that>`.
   Baseline confirmed → `--baseline-confirmed`; changes → the owner edits the
   pack's Baseline tab, then re-preview and re-confirm. Loop until confirmed. A
   render missing either decision hard-stops (exit 2).
3. **Ask where to save (REQUIRED).** Before rendering, ask for the `--out-dir`.
4. **Run the engine.** Use the command in `reference/runbook.md`, carrying the
   SF decision from Step 2, `--baseline-confirmed`, and the input channel. For a
   first build/QA run add `--refit`. The self-audit is **in-engine and default
   ON** — you do not run it as a separate step, and it now runs **before** the
   artifacts are rendered, which is what lets a problem run stamp its own
   disclosure onto the card.
5. **Read the exit code first, then the run log.** The exit code tells you what
   happened and what to do — see **Handle exit N** below; do not infer the
   outcome from the log. Then surface from the log: QA-fingerprint hits,
   duplicate drops, and any **pack-binding WARN** (see "Pack binding" in
   `reference/runbook.md` — an I5/I7 warning asks the operator to confirm the
   pack belongs to this matrix run; relay it and get the answer). These are board
   disclosure items, not auto-fails.
6. **Confirm the artifacts and report per the exit code.** Check `--out-dir`,
   and surface the Scorecard Summary to the user (the plain-English companion the
   Falke reviewer reads). **The filename is the deliverability signal** — see the
   rule below.
7. **Offer to draft the submission email — on exit 0 ONLY.** After a clean
   scorecard + summary, ALWAYS offer to DRAFT an email for submitting the
   scorecard (e.g. to the board / client) — subject + body, pulling the winner
   and key points from `scorecard_run.json` / the Scorecard Summary — for the
   user to review and send. Do NOT auto-send: sending on the user's behalf needs
   explicit per-instance permission; drafting is fine. **Never offer it on exit
   3 or exit 4** (see Handle exit 4).
8. **Hand to Floyd.** This is a solution deliverable; it goes through Floyd's
   gate before it ships. The `scorecard_run.json` plus `audit_report.md` are the
   audit trail.

## Exit-code contract (v2)

| Exit | Meaning | Artifacts? | Deliverable? |
|------|---------|-----------|--------------|
| 0 | Clean — rendered, audit PASS or PASS-WITH-WARNINGS | Yes | Yes |
| 1 | Environment / nothing to do (bad `--matrix` path, missing dependency) | **No** | — |
| 2 | Input-gate hard stop (SF/baseline unconfirmed, bad inputs, channel conflict) | **No** | — |
| 3 | Delivered WITH an audit blocker | Yes | **No** |
| 4 | Delivered PROVISIONAL (qualitative evaluation incomplete) | Yes | **No** |

Precedence: **3 > 4** — a run that is both blocked and provisional exits 3, the
louder class.

**The filename is the deliverability signal — trust it over the exit code.** An
artifact the engine will not vouch for is not called `scorecard`: it is written
as `scorecard-PRELIMINARY.pdf` / `.html` (and `scorecard_summary-PRELIMINARY.*`)
and every page carries a tiling **PRELIMINARY** watermark naming *why*
(`PRELIMINARY — evaluation incomplete · audit blocker`). Exit codes do not
survive a screenshot; the mark and the name do. **If the file says PRELIMINARY,
it does not go to a board — whatever the exit code says.** (`--no-audit` is the
one case that exits 0 *and* writes PRELIMINARY files. It is prohibited for board
runs; see below.)

### Handle exit 0 — clean

Rendered, audited, fully scored. Report the artifacts, surface the Summary, offer
the submission email (Step 7), and hand to Floyd.

Note that **PASS-WITH-WARNINGS is exit 0** and is the normal, honest state of a
run today — the standing-framework warning (W8) fires on every run until Falke
adopts one. Surface the warnings as board-disclosure items; do not report a
warned run as a failure.

### Handle exit 1 — environment; nothing was written

The process could not run: an unreadable `--matrix` path, or a missing Python
dependency. Nothing was read and nothing was written. Read the `[STOP]` message,
fix the path or the environment, and re-run. This is **not** a fiduciary gate and
should not be framed as one — a typo'd path is not "the gate working," and saying
so would teach the operator to discount the message that matters.

### Handle exit 2 — an input gate stopped the run; nothing was written

**That is the gate working, not an error** — say so plainly, and never re-run to
"get around" it. The message names the gate. The usual causes: the SF basis or
the baseline is unconfirmed (relay the question, get the owner's answer, re-run
with the decision); a required input is missing or invalid (the message names the
tab/field/file — relay it verbatim); or both input channels were passed (see the
mutual-exclusion rule above). Fix the input, re-run.

### Handle exit 3 — delivered WITH an audit blocker

The self-audit found a BLOCKER. The artifacts exist — the engine renders them
deliberately, stamped and renamed, rather than leaving the operator with nothing
to look at — but they are **not deliverable**.

**Lead the report with the disclosure.** Do not bury it under the results, and do
not present the card as final. Name the failing checks from `audit_report.md`,
tell the user the run must be remediated and repeat clean before the scorecard
goes anywhere, and do not route it to Floyd until it does.

### Handle exit 4 — delivered PROVISIONAL (this is the normal path, not a failure)

The qualitative evaluation is not finished — some category scores are still
blank. **This is the expected mid-evaluation state**, and the card is built *for*
it: scoring legitimately happens after matrix review and bidder interviews, over
days. Do not treat it as a defect or apologize for it; report it as progress.

What the provisional card deliberately does **not** do — do not add any of it
back in your own words:

- **No ranking.** Bidders list **alphabetically**, labeled "not ranked."
- **No leader.** The Summary names no winner and no front-runner.
- **No Overall.** The column reads *"Pending — N of M categories outstanding"*
  instead of a number.
- **The Summary is a status document**, headed "EVALUATION IN PROGRESS — no
  recommendation yet" — not a hedged recommendation.

The price picture is complete and reported; it is the *comparison* that is
withheld. Report the **coverage worklist** the engine produces (what is
outstanding, expressed in framework weight, not cell count) so the operator knows
exactly what is left to score, and tell them a full run re-renders as a
deliverable card once the grid is complete.

**Do NOT offer the submission email on exit 4.** Read the exit code — no coverage
inspection needed. The email is the one mechanism that pushes the document
*outward*; offering it here would be the tool proposing that the operator send a
working document to a board.

## `--no-audit` — PROHIBITED for board runs

The self-audit is in-engine and default ON. `--no-audit` skips it and **must
never be used for a board deliverable**. It survives for ARA debugging only.

An unaudited run exits **0** — nothing failed, and the operator explicitly asked
to skip — but nothing was checked either, so the artifacts are named
`scorecard-PRELIMINARY.*` and every page is stamped **"PRELIMINARY — not
audited."** That is the one case where exit 0 is not deliverable, and it is why
the filename rule above outranks the exit code. Do not pass this flag on the
user's behalf; if a user asks for it, say plainly that it produces a card that
cannot go to a board.

## The escape hatch — individual input files

The individual flags (`--baseline`, `--scoring-framework`, `--category-scores`,
`--aliases`, `--exclude`/`--exclusions`) remain supported **indefinitely**, and
they are the right answer in exactly three cases:

- **Legacy matrices** — the matrix predates the pack, so no pack exists.
- **Archival re-render** — reproducing a historical card from hand-built inputs.
- **ARA engineering / debugging.**

Outside those, using individual files **when a pack exists for that matrix run**
is an integrity smell: it means the firm names were re-typed by a human rather
than originating from the pipeline, which is the one thing the pack exists to
guarantee. The engine cannot see the matrix's output folder, so it cannot detect
this — **you handle it, by asking a named question, not by blocking:**

> *"This matrix came from create-matrix, which emits a Scorecard Inputs workbook
> beside it. Using hand-built inputs instead means the firm names weren't
> pipeline-originated — confirm you mean to."*

The operator may have legitimately lost the file, and blocking on a guess would
be the tool overreaching. Ask, accept the answer, and proceed. The run records
`input_channel: pack | individual` in `scorecard_run.json` either way, and the
audit WARNs when individual files are used against a pipeline-stamped matrix —
so the smell lands in the award file, not just in the conversation.

On this path the three Falke-filled templates are REQUIRED with **no fallback**
(the scorecard CANNOT be produced without `--scoring-framework` and
`--category-scores`; weights and scores are never invented or reused from a
previous run). See `reference/inputs.md` for the templates and how to hand them
to a Cowork operator.

## Reference (load only when needed)

- `reference/runbook.md` — the exact run command (pack and escape-hatch),
  Upload Detection, sheet selection, the gates, pack binding, the Audit Step,
  and the coverage/Overall rule (the one source of truth for how to invoke).
- `reference/inputs.md` — the run pack's tabs and who fills what, the
  escape-hatch templates, and the Cowork template-delivery rule.
- `reference/config.md` — the tunable config reference (what lives in
  `config/scorecard_config.yaml` and what is a CLI parameter).
- For the engine's own deep docs (modeling specs, package internals), see the
  bundled `engines/scorecard/` package and its docstrings.

## Eval

Before trusting a change to this skill, run the skill-behavior eval:
`eval/run_eval.sh` (see `eval/README.md`). It is separate from the engine's
modeling pytest suite and checks that the skill triggers and produces the
artifacts.
