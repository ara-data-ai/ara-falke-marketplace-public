# GOLDEN-SET v1 — "Harbor Point Tower II" synthetic gold project

> **PUBLIC-SAFE BY DESIGN.** Everything in `gold-project-v1/` is fictional:
> project, address, bidder names, and every dollar figure were authored from
> scratch by Marvin (2026-07-14) to exercise the leveled-design semantics.
> **Zero real client tokens.** This set is meant to ship in the PUBLIC
> marketplace artifact and is whitelisted in `../.gitignore` (the rest of
> `eval/golden/` remains confidential, untracked real-project material).

## What this is

The **canonical, executable definition of "what a correct leveled tab looks
like"** (design review `DESIGN-REVIEW-v04-marvin-domain.md` §4; Derick-approved
T1+T2). One synthetic 6-bidder project, small enough to be hand-leveled
exhaustively, engineered so a single pipeline run exercises every major
semantic stratum. The hand-leveled truth lives in:

- `expectations.yaml` — cell-level truth for the clean run (exit 0)
- `expectations-quarantine.yaml` — the deliberate tie-out-break run (exit 3)

Every number was computed BY HAND from the interim JSONs; the arithmetic is
shown in comments inside the expectation files so any reviewer can re-derive
it without running the engine.

## Contents

| Path | What |
|---|---|
| `project.yaml` | Synthetic project identity (GSF 100,000 — $/SF math is trivially checkable) |
| `interim/*.json` | Six BidDocument extractions (the pipeline's input contract) |
| `overlay/known_firms.local.yaml` | Synthetic known-firm overlay (reclass path, works on private AND public trees) |
| `interim-quarantine/*.json` | Two-bidder variant that deliberately breaks Stage 6b (bond-on-top) |
| `expectations.yaml` | Hand-leveled cell-level expectations, main set |
| `expectations-quarantine.yaml` | Expectations for the quarantine variant |

## Scenario roster (bidder → what it proves)

| Bidder | Scenario | Rules / semantics proven |
|---|---|---|
| **Alpha Restoration Group LLC** | Clean full-scope bid (DIV 07 lump-sum since the re-arm swap); fully additive footer with bond-inside-GT; add alternate | R9–R11 benchmarks, R15 neutral, R29 High-confidence (DIV 07), footer composition + FEES SUBTOTAL, alternates fenced from base, ARITHMETIC_VERIFIED |
| **Beacon Shoreline Builders Inc.** | Stated-$0-over-Excluded division (DIV 09); priced-allowance ITEMIZED division (DIV 07 — the GOLD-DEV-10 shape-(b) re-arm, deliberately on the reclass bidder); insurance-folded ADDITIVE other_fees; **known-firm reclass** (dumpster DIV 11→01 via the synthetic overlay firm) | R28 red "Excluded" token, REM-1 $0 survival on the leveled view (GOLD-DEV-10 fix), Q4 allowance-in-subtotal-and-median, untouched-division byte-identical pass-through (a regression re-deriving all divisions shows 360,000 + false R20 on DIV 07), additive-vs-memo other_fees, Option C mirror-vs-leveled split (90,000 vs 102,000), KNOWN_FIRM_RECLASSIFIED, phantom-gap suppression (vacated DIV 11) |
| **Cypress Coast Plumbing Co.** | Partial-scope single-trade bid (the red wall); stated-$0 lump division; line-level explicit zero | R5 missing-pricing reds ×4, R6 zero red (division + line), SCOPE_GAP_IMPLICIT register, sorts first (lowest leveled total) |
| **Delta Gulf Contracting LLC** | Division math errors on BOTH sides of max($5, 0.5%): DIV 03 delta $8,000 (beyond) / DIV 07 delta $1,500 (within); overpriced DIV 09 | R20 red + tolerance boundary, R13 yellow, GOLD-DEV-1/2 register entries |
| **Eastline Mechanical & Restoration Corp** | Legacy CSI-1995 2-digit bid (codes 01/03/07/09/15), signature-detected; code-15 split with one unroutable line | Signature remap (CODE_FORMAT_REMAPPED ×4), split routing to DIV 22, CODE_SPLIT_UNMATCHED RED, legacy 17-0xx fees carried in footer fields, R12 cyan (DIV 01) |
| **Fairway Building Group Inc.** | By-owner verbatim "Not Applicable" (ENC-1); Not-Comparable division (ENC-2); engine-derived subtotal (REM-2); MEMO other_fees; deduct alternate | ENC-1 token, ENC-2 fencing (DIV 09 n=3 not 5), REM-2 disclosure appended to a cyan comment, memo other_fees suppressed to 0, R12 cyan |
| **Quayside Restoration Co.** (variant) | Bond quoted OUTSIDE the stated grand total | R21 GT red SURVIVING delivery, FOOTER_DISCREPANCY RED, **Stage 6b bidder-error branch** (GOLD-DEV-6 fixed): a faithfully-reproduced bidder footer inconsistency exits 0 with NO quarantine — the quarantine chain itself is proven by the harness fault-injection step |
| **Quarry Bay Builders LLC** (variant) | Clean single-division peer | n=2 benchmark, Low confidence, no false flags alongside a flagged peer |

Cross-cutting: `<3-valid-bids` variance-paint suppression is proven in DIV 02
(exactly 2 valid bids, both cells qualify for paint, both suppressed,
`gate_suppressed == 2`). The dual-GT-style discrepancy scenario lives in the
**variant**, not the main set — it proves the Stage 6b BIDDER-ERROR branch
(GOLD-DEV-6 fixed: exit 0, R21 + FOOTER_DISCREPANCY carry the story). The
true-tool-defect quarantine chain is proven by the harness **fault-injection
step** on a corrupted copy of the main-run workbook.

## The harness contract (what Christine's runner must do)

1. **Overlay install:** back up any existing
   `engines/matrix/config/known_firms.local.yaml`, install
   `overlay/known_firms.local.yaml` in its place (the gold run must execute
   with EXACTLY this overlay — swap, never merge), restore afterward. The
   filename is gitignored, so a leftover copy cannot ship.
2. **Main run:**
   ```bash
   PYTHONPATH=<engine root> python3 -m src.pipeline \
     --interim-dir eval/golden/gold-project-v1/interim \
     --project-config eval/golden/gold-project-v1/project.yaml \
     --sf-basis 100000 --out <tmp>/gold-v1.xlsx
   ```
   Assert exit code 0, then assert `expectations.yaml` cell by cell using the
   `locator_contract` (label-anchored, never absolute coordinates — same idiom
   as `reconcile.py`). Tolerances are defined at the top of the file.
3. **Bidder-footer-error run:** same command with
   `--interim-dir eval/golden/gold-project-v1/interim-quarantine`; assert exit
   code 0 (GOLD-DEV-6 fixed — a faithfully-reproduced bidder footer error no
   longer quarantines), the surviving R21 red + FOOTER_DISCREPANCY row, and
   the quarantine chain's ABSENCE, per `expectations-quarantine.yaml`.
4. **Fault-injection step:** corrupt one GRAND TOTAL cell (plus one commented
   division-subtotal cell, for the compose proof) in a COPY of the main-run
   workbook; call `reconcile_written_matrix` + `apply_quarantine` directly;
   assert the RED banner on both sheets, the composed cell marks, the RED
   POST_WRITE_TIEOUT_FAILURE rows, and the QUARANTINE summary line. (The
   pipeline exit-3 mapping is regression-covered in tests/test_reconcile.py.)
5. **Report every mismatch** (no fail-fast), exit nonzero on any mismatch.
   A missing overlay must surface as loud expectation failures (Beacon's
   DIV 01/DIV 11 and the KNOWN_FIRM_RECLASSIFIED row), never as a silent pass.

## Versioning & release gate (the standing rule)

- **The expectations version WITH the engine.** Any engine change that
  legitimately alters an expected value updates the expectation file **in the
  same change**, with the delta explained. Entries tagged `defect:` encode
  current behavior for a registered defect (`known_deviations`) — the fixing
  PR flips the expectation.
- **Release gate: no engine version ships until the harness passes** on both
  runs (main exit 0 + variant exit 0) AND the fault-injection step, AND
- **Private-side companion rule:** the engagement's confidential companion set
  of real hand-leveled fixtures (one directory up, local-only) is regenerated
  and re-diffed by Marvin at each release so
  the real-data anchor never lags the engine. The real set NEVER enters the
  public artifact; this synthetic set is the only golden material that ships.
- Marvin's sign-off criteria remain on top of the harness: zero missed
  material exclusions on the board-facing tab, correct instrument
  classification, totals reconciled end to end.

## Known-deviation register

Hand-leveling this set surfaced **ten engine findings** (GOLD-DEV-1 … 10,
documented at the bottom of `expectations.yaml`): benchmark R7 not excluding
red-flagged subtotals, the dual $1-vs-Falke tolerance on the audit register,
the exclusion-register hole (no AuditItem for excluded divisions), the
NC-unfenced audit median, the NC false-RED arithmetic row, the misattributed
quarantine language for bidder footer errors, median-$0 scope-gap noise, the
writer-side phantom-gap landmine (deliberately not triggered), the
statistically unreachable 2σ GC-fee outlier at small n, and — highest
priority — **GOLD-DEV-10: the leveled view silently re-derives EVERY division
subtotal of a reclass-matched bidder** (drops allowances, erases stated-vs-
lines math errors, blanks stated $0s). These route to the design queue via
program coordination; each is encoded as *current behavior* where it appears
so the harness stays green until the fix lands.
