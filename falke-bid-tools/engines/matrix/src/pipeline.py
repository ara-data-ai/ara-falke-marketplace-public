"""
FALKE Matrix Pipeline — Orchestrator
=====================================
Loads all bid JSONs from an interim dir, validates against BidDocument,
normalizes, runs cross-bid stats, and writes the per-project bid-comparison
Excel matrix.

Usage (run as a module from the bundled engine root):
    PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/engines/matrix" \
      "${CLAUDE_PLUGIN_DATA}/venv/bin/python" -m src.pipeline \
      --interim-dir <session-scoped dir of extracted *.json> \
      --out <user-chosen output .xlsx> \
      --project-config <project.json/.yaml with project identity> \
      [--sf-basis <value> | --sf-confirmed]

    --interim-dir    REQUIRED. Where the extraction agents wrote the bid JSONs.
    --out            REQUIRED. Where to write the matrix (user-chosen).
    --project-config REQUIRED. Per-run project identity (name/address/SF-basis).
    --sf-basis       OPTIONAL. Explicit $/SF denominator (overrides config/extract).
    --sf-confirmed   OPTIONAL. Accept the extracted/config GSF as the SF basis.
    --expect-bids    OPTIONAL. The number of bids the caller expects to land in
                     the matrix (the skill layer knows how many PDFs it
                     extracted). Mismatch with the loaded valid-bid count is a
                     hard-stop (exit 2) BEFORE anything is written — a second,
                     independent count on top of the exit-4 disclosure below.

Exit-code contract (v2 — F1/X, Floyd ruling R-2):
    0 — clean: the matrix was written, tied out with zero failures, and EVERY
        submitted input bid is in it (no dropped inputs on exit 0 — standing
        gate criterion). Deliberate skips (skip=true / template placeholders)
        are not drops.
    1 — environment / nothing to do: --interim-dir missing, or zero valid bids.
    2 — input gate (hard stop, file NOT written):
          * SF-basis gate: a confirmed $/SF denominator was not supplied (a
            missing required input — a fiduciary decision, never silently
            guessed; scoping §1.3/§1.4, M2), or project config invalid.
          * --expect-bids N was supplied and N != the loaded valid-bid count.
          * duplicate contractor name: two interim files claim the same
            (case-folded) contractor_name — usually a stale JSON from a prior
            run. Never silently guessed, never disambiguated into board-facing
            headers (F2, Floyd ruling R-3).
    3 — delivered with verification failures: Stage 6b found ≥1 post-write
        tie-out failure. The file IS delivered but LOUD-QUARANTINED (RED banner +
        cell marks + AUDIT flag); each flagged figure must be verified against the
        source bid (STAGE6B-QUARANTINE-DISCLOSURE-SPEC.md).
    4 — delivered, but one or more inputs were EXCLUDED: an input bid failed
        JSON parse / schema validation / structured intake / normalization and
        is NOT in the matrix. Each exclusion is a RED INPUT_EXCLUDED row on the
        AUDIT sheet naming the file and the reason. The matrix is otherwise
        verified. Precedence: if Stage 6b ALSO quarantined, exit is 3 (the
        rendering defect is the louder class); the INPUT_EXCLUDED rows still
        land on the AUDIT sheet either way, so no drop is ever silent.

Pipeline stages:
    0.5. Deterministic structured intake: any *.xlsx / *.csv bid files in the
         interim dir are parsed (no vision, no agents) into BidDocument JSONs
    1. Glob all *.json from the interim dir
    2. Skip files where skip=true OR contractor_name is blank/template-like
    3. Validate each JSON against BidDocument (Pydantic v2)
    4. normalize_bid() on each valid BidDocument
    5. compute_cross_bid_stats() on the full list of NormalizedBids
    6. write_matrix() → per-project xlsx
    7. Print summary report
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

# Resolve project root so imports work when run as python3 -m src.pipeline
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.audit import AuditCode, AuditItem, AuditStatus, audit_bids
from src.config_errors import MatrixConfigError
from src.intake_structured import run_structured_intake
from src.models import BidDocument
from src.normalize import build_normalized_view, compute_cross_bid_stats, normalize_bid
from src.normalized_models import NormalizedBid
from src.reconcile import reconcile_written_matrix
from src.run_config import SF_GATE_STOP, RunInputs, load_run_config, resolve_sf_basis
from src.write_matrix import apply_quarantine, write_matrix

# ---------------------------------------------------------------------------
# Skip predicates
# ---------------------------------------------------------------------------

GENERIC_CONTRACTOR_NAMES: set[str] = {
    "contractor name",
    "contractor",
    "",
    "template",
    "blank",
    "none",
    "n/a",
}


def _should_skip(raw: dict, file_path: Path) -> tuple[bool, str]:
    """
    Return (True, reason) if this JSON should be skipped, else (False, '').

    Skip conditions:
      1. File has skip=True at the top level (explicit skip flag).
      2. contractor_name is absent, None, or matches a generic placeholder.
    """
    # Condition 1: explicit skip flag
    if raw.get("skip") is True:
        return True, "skip=true in file"

    # Condition 2: blank or template-like contractor name
    contractor = raw.get("contractor_name")
    if contractor is None:
        return True, "contractor_name is null"
    if str(contractor).strip().lower() in GENERIC_CONTRACTOR_NAMES:
        return True, f"contractor_name is generic placeholder: {contractor!r}"

    return False, ""


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    interim_dir: Path,
    out_path: Path,
    project_config: Path,
    sf_basis: float | None = None,
    sf_confirmed: bool = False,
    expect_bids: int | None = None,
) -> None:
    interim_dir = Path(interim_dir)
    out_path = Path(out_path)

    print("=" * 70)
    print("FALKE Matrix Pipeline — Bid Comparison Run")
    print("=" * 70)

    # --- Validate paths ---
    if not interim_dir.exists():
        print(f"ERROR: --interim-dir not found at {interim_dir}")
        sys.exit(1)

    print(f"Interim dir   : {interim_dir}")
    print(f"Project config: {project_config}")
    print(f"Output        : {out_path}")
    print()

    # --- Stage 0.5: Deterministic structured intake (.xlsx / .csv bids) ---
    # Falke's structured bid submissions bypass vision extraction entirely:
    # each per-bid spreadsheet in the interim dir is parsed deterministically
    # into a BidDocument JSON (intake_structured.py), which Stage 1 then picks
    # up exactly like an agent-extracted bid. An unrecognized layout fails
    # LOUDLY per file and is reported with the validation errors — never
    # silently dropped, never guessed.
    intake_errors: list[tuple[str, str]] = []
    intake_ok, intake_failed = run_structured_intake(interim_dir)
    if intake_ok or intake_failed:
        print(f"Stage 0.5: Structured intake — {len(intake_ok)} parsed, "
              f"{len(intake_failed)} failed")
        for fname, json_name in intake_ok:
            print(f"  OK: {fname} → {json_name} (deterministic parse)")
        for fname, err in intake_failed:
            print(f"  ERROR: {fname} — {err}")
            intake_errors.append((fname, f"structured intake failed: {err}"))
        print()

    # --- Stage 1: Glob all JSON files ---
    json_files = sorted(interim_dir.glob("*.json"))
    print(f"Found {len(json_files)} JSON file(s) in {interim_dir}:")
    for f in json_files:
        print(f"  {f.name}")
    print()

    # --- Stages 2–3: Load, skip-check, validate ---
    skipped: list[tuple[str, str]] = []
    validation_errors: list[tuple[str, str]] = list(intake_errors)
    valid_docs: list[BidDocument] = []
    valid_sources: list[str] = []   # source file per valid doc (same order)

    for file_path in json_files:
        print(f"Loading: {file_path.name}")

        # Load raw JSON
        try:
            with open(file_path, encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            print(f"  ERROR: JSON parse failed — {e}")
            validation_errors.append((file_path.name, f"JSON parse error: {e}"))
            continue

        # Skip check
        should_skip, skip_reason = _should_skip(raw, file_path)
        if should_skip:
            print(f"  SKIP: {skip_reason}")
            skipped.append((file_path.name, skip_reason))
            continue

        # Pydantic validation
        try:
            doc = BidDocument.model_validate(raw)
            print(f"  OK: {doc.contractor_name!r} — {len(doc.divisions)} divisions")
            valid_docs.append(doc)
            valid_sources.append(file_path.name)
        except ValidationError as e:
            # Log validation error and skip this bid rather than crashing
            first_error = e.errors()[0] if e.errors() else {}
            msg = f"Pydantic validation failed ({e.error_count()} errors): {first_error.get('msg', str(e))}"
            print(f"  WARNING: {msg}")
            validation_errors.append((file_path.name, msg))
            continue

    print()
    print(f"Valid bids: {len(valid_docs)}, Skipped: {len(skipped)}, Errors: {len(validation_errors)}")
    print()

    if not valid_docs:
        print("ERROR: No valid bids to process. Exiting.")
        sys.exit(1)

    # --- Stage 3a: Input-identity gates (hard stops BEFORE anything is written) ---

    # Gate 1 (F2, Floyd R-3): duplicate contractor name across loaded bids.
    # Every downstream stage (writer columns, reconcile's name→column map,
    # quarantine relocation) keys bidders by contractor_name, so a duplicate
    # name collapses two bids onto one identity: reconcile verifies one bid
    # against the other's column (FALSE quarantine on a correct workbook) or,
    # if the duplicates are identical, silently skips verifying one column.
    # Never silently guess; never disambiguate into board-facing headers.
    by_name: dict[str, list[str]] = {}
    for doc, src in zip(valid_docs, valid_sources):
        key = " ".join(str(doc.contractor_name).split()).casefold()
        by_name.setdefault(key, []).append(src)
    dup_names = {k: v for k, v in by_name.items() if len(v) > 1}
    if dup_names:
        print("STOP (exit 2): duplicate contractor name(s) across the loaded bid files.")
        for _key, files in sorted(dup_names.items()):
            display = next(
                doc.contractor_name for doc, src in zip(valid_docs, valid_sources)
                if src == files[0]
            )
            # Operator message — Marvin's W-D ruling M-4, verbatim.
            print(
                f"\n  Two bid files carry the same contractor name "
                f"'{display}'. Most often this is a stale extraction JSON "
                f"from an earlier run still in the interim folder — delete "
                f"the stale file and re-run. If {display} genuinely "
                f"submitted two proposals (e.g. a base bid plus an alternate "
                f"or breakout bid), rename the second file's "
                f"`contractor_name` to \"{display} - Alternate\" so it "
                f"levels as its own column. The matrix never merges two "
                f"files or silently drops one."
            )
            print("  The files:")
            for fname in files:
                print(f"    - {fname}")
        print("\n  No file was written.")
        sys.exit(2)

    # Gate 2 (F1/B-1, Floyd R-2): caller-asserted bid count. The skill layer
    # knows how many PDFs it extracted; a mismatch here means an input never
    # made it into the run — stop before writing rather than deliver short.
    if expect_bids is not None and expect_bids != len(valid_docs):
        print(f"STOP (exit 2): --expect-bids {expect_bids}, but "
              f"{len(valid_docs)} valid bid(s) loaded "
              f"({len(skipped)} skipped, {len(validation_errors)} failed).")
        for fname, reason in skipped:
            print(f"  SKIPPED  {fname}: {reason}")
        for fname, reason in validation_errors:
            print(f"  FAILED   {fname}: {reason}")
        print("  Fix or remove the missing/failed input(s) — or correct "
              "--expect-bids — and re-run. No file was written.")
        sys.exit(2)

    # --- Stage 3b: Resolve project identity + the SF-basis gate (M1/M2) ---
    # The $/SF denominator is a fiduciary decision. Hard-stop (exit 2) unless the
    # user supplied --sf-basis or confirmed the extracted/config GSF.
    print("Stage 3b: Resolving project identity + SF basis ...")
    try:
        base = load_run_config(str(project_config), validate=False)
    except MatrixConfigError as e:
        print(f"ERROR: {e}")
        sys.exit(2)

    extracted_gsf = base.gross_sf
    if extracted_gsf is None:
        for d in valid_docs:
            if d.total_gsf:
                extracted_gsf = float(d.total_gsf)
                break

    resolved, source = resolve_sf_basis(sf_basis, sf_confirmed, extracted_gsf)
    if resolved == SF_GATE_STOP:
        print(f"STOP (exit 2): SF basis not resolved — {source}")
        sys.exit(2)

    try:
        run = load_run_config(
            str(project_config),
            overrides={"gross_sf": resolved, "sf_source": source},
            validate=True,
        )
    except MatrixConfigError as e:
        print(f"STOP (exit 2): {e}")
        sys.exit(2)
    print(f"  Project: {run.project_name} | SF basis: {run.gross_sf:,.0f} "
          f"{run.sf_basis_label or 'SF'} ({source})")
    print()

    # --- Stage 4: Normalize each bid (faithful mirror, Option C §1) ---
    # normalize_bid no longer moves reclass dollars — it builds the as-submitted
    # mirror and attaches reclass_recommendations. build_normalized_view then
    # produces the leveled (moved-dollar) view for cross-bid math and the
    # Leveled_Normalized sheet (§3).
    print("Stage 4: Normalizing bids (faithful mirror) ...")
    mirror_bids: list[NormalizedBid] = []
    leveled_bids: list[NormalizedBid] = []
    for doc in valid_docs:
        print(f"  normalize_bid({doc.contractor_name!r})")
        try:
            mirror = normalize_bid(doc)
            mirror_bids.append(mirror)
            leveled_bids.append(build_normalized_view(mirror, doc))
            if mirror.normalization_warnings:
                for w in mirror.normalization_warnings:
                    print(f"    [WARN] {w}")
        except Exception as e:
            print(f"  ERROR normalizing {doc.contractor_name!r}: {e}")
            validation_errors.append((doc.contractor_name, f"normalize_bid error: {e}"))
    print()

    # --- Stage 5: Cross-bid stats on the LEVELED buckets (§4) ---
    # Cross-bid statistics are only honest on the normalized buckets, so they
    # compute against the leveled bids; the mirror keeps each bid's own numbers.
    print("Stage 5: Computing cross-bid statistics (leveled buckets) ...")
    leveled_bids = compute_cross_bid_stats(leveled_bids)
    for bid in leveled_bids:
        pct = bid.footer.gc_fee_pct
        pct_str = f"{pct:.1f}%" if pct is not None else "N/A"
        print(f"  {bid.contractor_name}: gc_fee_pct={pct_str}, "
              f"implicit_gaps={bid.implicit_gap_count}, "
              f"summary_flags={len(bid.summary_flags)}")
    print()

    # --- Stage 5b: Audit on the leveled buckets (cross-bid signals tagged leveled) ---
    print("Stage 5b: Running extraction & normalization audit ...")
    audit_items = audit_bids(leveled_bids)

    # F1 (Floyd R-2): every dropped input becomes a RED INPUT_EXCLUDED row on
    # the AUDIT sheet — the exclusion must be visible on the instrument itself,
    # not only in console prose. Deliberate skips are not drops.
    dropped_inputs = list(validation_errors)
    for identifier, reason in dropped_inputs:
        audit_items.append(AuditItem(
            contractor_name=identifier,
            status=AuditStatus.RED,
            code=AuditCode.INPUT_EXCLUDED,
            message=(f"Input bid EXCLUDED from this matrix — {reason}. "
                     f"This bid is NOT in any column or total. Re-extract or "
                     f"fix the input and re-run before using this matrix for "
                     f"an award."),
        ))

    normalized_bids = mirror_bids
    red_count    = sum(1 for a in audit_items if a.status.value == "RED")
    yellow_count = sum(1 for a in audit_items if a.status.value == "YELLOW")
    green_count  = sum(1 for a in audit_items if a.status.value == "GREEN")
    print(f"  Audit: {red_count} RED | {yellow_count} YELLOW | {green_count} GREEN")
    print()

    # --- Stage 6: Write Excel matrix ---
    print("Stage 6: Writing bid-comparison matrix ...")
    summaries = write_matrix(
        bids=normalized_bids,
        output_path=out_path,
        run=run,
        audit_items=audit_items,
        leveled_bids=leveled_bids,
    )
    print()

    # --- Stage 6b: Post-write reconciliation (closed-loop tie-out) ---
    # Read the just-saved .xlsx back and assert the four tie-out invariants. A
    # tie-out failure means the ENGINE's own rendering is defective (a validated
    # number landed in the wrong cell) — it is NOT a finding about a contractor's
    # bid. Per Derick's decision (Marvin's STAGE6B-QUARANTINE-DISCLOSURE-SPEC.md),
    # Stage 6b NO LONGER hard-stops. Instead it LOUD-QUARANTINES: the file IS
    # delivered, but every affected figure is flagged with a RED banner on the
    # board-facing Bid_Form + Leveled_Normalized sheets, a RED cell mark + verify
    # comment, and a RED AUDIT row + QUARANTINE summary line. The run then exits
    # with a DISTINCT code 3 ("delivered with verification failures") so it is
    # never mistaken for a clean exit 0. The SF-basis gate above keeps exit 2 (a
    # missing-required-input hard-stop, a different case).
    print("Stage 6b: Reconciling the written matrix (post-write tie-out) ...")
    tieout_failures = reconcile_written_matrix(
        output_path=out_path,
        bids=normalized_bids,
        audit_item_count=len(audit_items),
        leveled_bids=leveled_bids,
    )
    if tieout_failures:
        print(f"  POST-WRITE TIE-OUT FAILED — {len(tieout_failures)} mismatch(es):")
        for f in tieout_failures:
            loc = f" [{f.division_csi}]" if f.division_csi else ""
            print(f"    RED {f.code.value} | {f.contractor_name}{loc}: {f.message}")
        print()
        # LOUD QUARANTINE: deliver the file with the defect flagged (banner + cell
        # marks + AUDIT line), then exit 3. We do NOT refuse to deliver.
        rendered_n = apply_quarantine(
            output_path=out_path,
            failures=tieout_failures,
            bids=normalized_bids,
            leveled_bids=leveled_bids,
        )
        count_phrase = (
            "one or more" if rendered_n < 0 else str(rendered_n)
        )
        plural = "" if rendered_n == 1 else "S"
        print(f"DELIVERED WITH {count_phrase} VERIFICATION FAILURE{plural} "
              f"(exit 3) — flagged on the Bid_Form banner + AUDIT tab. The file "
              f"WAS delivered; verify each flagged figure against the source bid "
              f"before relying on it for an award.")
        if dropped_inputs:
            print(f"NOTE: {len(dropped_inputs)} input bid(s) were ALSO EXCLUDED "
                  f"from this matrix (RED INPUT_EXCLUDED rows on the AUDIT tab). "
                  f"Exit 3 takes precedence over exit 4; both problems are on "
                  f"the AUDIT sheet.")
        sys.exit(3)
    print(f"  Tie-out OK: grand totals, footer arithmetic, division subtotals, "
          f"and audit-row count all reconcile (within ${1}).")
    print()

    # --- Stage 7: Summary report ---
    print("=" * 70)
    print("PIPELINE SUMMARY")
    print("=" * 70)

    print(f"\nOutput file: {out_path}\n")

    print("Processed bids:")
    for s in summaries:
        if not s.get("matched"):
            print(f"  ✗ {s['contractor']} — NOT MATCHED to any template column")
            continue

        divs = s.get("divisions_written", [])
        footer = s.get("footer_written", {})
        warns = s.get("warnings", [])

        print(f"\n  {s['contractor']}:")
        print(f"    Template col index : {s['name_col']}")
        print(f"    Divisions written  : {len(divs)}")
        for d in divs:
            state_tag = f" [{d['state']}]" if d['state'] != 'AMOUNT' else ""
            print(f"      {d['csi_code']:15s} row {d['row']:3d} → ${d['amount']:>12,.2f}{state_tag}")
        print(f"    Footer written:")
        for k, v in footer.items():
            print(f"      {k:25s} → ${v:>12,.2f}")
        if warns:
            print(f"    Warnings:")
            for w in warns:
                print(f"      {w}")

    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for fname, reason in skipped:
            print(f"  {fname}: {reason}")

    if validation_errors:
        print(f"\nValidation/processing errors ({len(validation_errors)}):")
        for fname, reason in validation_errors:
            print(f"  {fname}: {reason}")

    # F1 (Floyd R-2, standing gate criterion): NO dropped inputs on exit 0.
    # The matrix was delivered and tied out, but it does not contain every
    # submitted input — say so with a distinct exit code, loudly.
    if dropped_inputs:
        print(f"\nDELIVERED, BUT {len(dropped_inputs)} INPUT BID(S) EXCLUDED "
              f"(exit 4) — each exclusion is a RED INPUT_EXCLUDED row on the "
              f"AUDIT tab. The matrix is INCOMPLETE: do not use it for an "
              f"award until the excluded bid(s) are fixed and the run repeats "
              f"clean (exit 0).")
        sys.exit(4)

    print("\nDone.")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="src.pipeline",
        description="FALKE bid-comparison matrix pipeline (per-project).",
    )
    parser.add_argument(
        "--interim-dir",
        required=True,
        type=Path,
        help="Directory of extracted per-bid *.json (written by the "
             "extraction agents into a session-scoped run dir).",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        dest="out_path",
        help="Output .xlsx path (user-chosen output dir).",
    )
    parser.add_argument(
        "--project-config",
        required=True,
        type=Path,
        dest="project_config",
        help="Per-run project identity (project.json/.yaml): project_name, "
             "project_address, sf_basis_label, optional gross_sf/rfp_label.",
    )
    parser.add_argument(
        "--sf-basis",
        type=float,
        default=None,
        help="Explicit $/SF denominator — overrides config/extraction, no prompt.",
    )
    parser.add_argument(
        "--sf-confirmed",
        action="store_true",
        help="Accept the extracted/config GSF as the SF basis (suggest-and-confirm).",
    )
    parser.add_argument(
        "--expect-bids",
        type=int,
        default=None,
        dest="expect_bids",
        help="Number of bids the caller expects in the matrix (the skill layer "
             "knows its PDF count). Mismatch with the loaded valid-bid count "
             "hard-stops (exit 2) before anything is written.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    run_pipeline(
        interim_dir=args.interim_dir,
        out_path=args.out_path,
        project_config=args.project_config,
        sf_basis=args.sf_basis,
        sf_confirmed=args.sf_confirmed,
        expect_bids=args.expect_bids,
    )
