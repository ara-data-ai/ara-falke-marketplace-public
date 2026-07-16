"""Deterministic self-audit ("Floyd-lite, every run") — Marvin's rubric.

A pure-Python QA pass run AFTER the scorecard is generated and BEFORE the final
artifact is named. It RE-DERIVES the critical numbers and structural facts from
``(matrix_parse, run_inputs, pipeline_result)`` and compares them to what the
pipeline actually emitted — any disagreement is surfaced, never silently
reconciled (rubric §0).

Determinism contract: NO LLM, NO network. Pure arithmetic, set logic, regex.
Every check runs every time (failures do NOT short-circuit) so the report is
always complete. Implements checks C1..C16 from the scorecard audit rubric,
plus C17 (producer quarantine marker) and C18 (cross-sheet grand-total
tie-out) from Marvin's P0-7 sheet ruling, plus C19..C22 from his P1-4 run-pack
ratification: C19 framework drift vs the declared basis (which folds in P2-4's
orphaned framework-vs-canonical weights-drift check — it is free here and
orphaned there), C20 the two-clock date coherence, C21 input-channel provenance,
C22 run-pack binding provenance.

C19-C22 are INFO no-ops when the run supplies individual input files rather than
a pack: there is no declaration to judge, and a check that invents a finding out
of an absent input is worse than no check.

Severity -> verdict (rubric §2):
  - any BLOCKER fail              -> FAIL
  - else any WARN fail           -> PASS-WITH-WARNINGS
  - else                          -> PASS
  - INFO never gates (observational evidence only).
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .matrix import ParsedMatrix, normalize_name

# severities
BLOCKER = "BLOCKER"
WARN = "WARN"
INFO = "INFO"

# statuses
PASS = "pass"
FAIL = "fail"

# verdicts
V_PASS = "PASS"
V_WARN = "PASS-WITH-WARNINGS"
V_FAIL = "FAIL"

# tolerances (rubric §2)
MONEY_TOL = 0.01            # penny-exact
GSF_GUARD = 0.05            # 5% rounding guard for C3
DIV_BAND = (16, 20)         # plausibility band for populated divisions (C8)


@dataclass
class CheckResult:
    name: str
    title: str
    severity: str
    status: str                 # pass | fail
    verdict_line: str
    evidence: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# helpers — independent re-derivation off the parse / inputs
# ----------------------------------------------------------------------------
def _row164_total_for(matrix_parse: ParsedMatrix, bidder_name: str) -> Optional[float]:
    """Re-read the grand total the parser pulled from the GRAND TOTAL row for the
    block whose (display or raw) name matches. Independent of pipeline output."""
    target = normalize_name(bidder_name)
    for b in matrix_parse.blocks:
        if normalize_name(b.name) == target or b.norm == target:
            return b.grand_total
    return None


def _band_label_for(per_sf: float, run) -> str:
    """Re-run the Section B band rule (mirrors mechanical.assign_tier) from the
    run band so the audit is a genuine re-derivation, not an echo."""
    band_low = run.band_low_per_sf
    band_high = run.band_high_per_sf
    band_low_int = math.floor(band_low)
    band_high_int = math.floor(band_high)
    mid_floor = 0.90 * band_low
    premium_floor = 1.20 * band_high
    if per_sf < mid_floor:
        return "RISK"
    if per_sf < band_low_int:
        return "MID"
    if per_sf <= band_high_int:
        return "TOP"
    if per_sf <= premium_floor:
        return "DEFENSIVE"
    return "PREMIUM"


def _log_text(pipeline_result: Dict) -> str:
    return "\n".join(pipeline_result.get("log", []))


# ----------------------------------------------------------------------------
# C1 — Totals reconcile to Row 164
# ----------------------------------------------------------------------------
def check_c1(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    per_bidder = []
    max_delta = 0.0
    bad = None
    for b in pipeline_result["bidders"]:
        row164 = _row164_total_for(matrix_parse, b["name"])
        pt = b["total"]
        delta = abs(pt - row164) if (row164 is not None and pt == pt) else float("inf")
        per_bidder.append({"name": b["name"], "pipeline_total": pt, "row164": row164,
                           "delta": (None if delta == float("inf") else round(delta, 4))})
        if delta != float("inf"):
            max_delta = max(max_delta, delta)
        if delta > MONEY_TOL and bad is None:
            bad = (b["name"], pt, row164, delta)
    n = len(pipeline_result["bidders"])
    if bad is None:
        return CheckResult("C1", "Totals reconcile to Row 164", BLOCKER, PASS,
                           f"C1 PASS — all {n} bidders reconcile to Row 164 (max delta ${max_delta:,.2f}).",
                           {"max_delta_usd": round(max_delta, 4), "per_bidder": per_bidder})
    nm, pt, r164, d = bad
    dl = "n/a" if d == float("inf") else f"${d:,.2f}"
    r164s = "None" if r164 is None else format(r164, ",.2f")
    return CheckResult("C1", "Totals reconcile to Row 164", BLOCKER, FAIL,
                       f"C1 FAIL — {nm}: pipeline_total=${pt:,.2f} vs Row 164 {r164s} "
                       f"(delta {dl}). BLOCKER.",
                       {"max_delta_usd": None if d == float("inf") else round(d, 4),
                        "per_bidder": per_bidder})


# ----------------------------------------------------------------------------
# C2 — $/SF math
# ----------------------------------------------------------------------------
def check_c2(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    sf = run_inputs.run.sf_basis
    per_bidder = []
    bad = None
    for b in pipeline_result["bidders"]:
        recomputed = int(round(b["total"] / sf)) if b["total"] == b["total"] else None
        ok = (recomputed == b["per_sf"])
        per_bidder.append({"name": b["name"], "displayed": b["per_sf"], "recomputed": recomputed})
        if not ok and bad is None:
            bad = (b["name"], b["per_sf"], recomputed)
    if bad is None:
        return CheckResult("C2", "$/SF math", BLOCKER, PASS,
                           f"C2 PASS — $/SF recomputes for all bidders against sf_basis={sf:,.0f}.",
                           {"sf_basis": sf, "per_bidder": per_bidder})
    nm, disp, rec = bad
    return CheckResult("C2", "$/SF math", BLOCKER, FAIL,
                       f"C2 FAIL — {nm}: displayed ${disp}/SF vs recomputed ${rec}/SF "
                       f"(basis {sf:,.0f}). BLOCKER.",
                       {"sf_basis": sf, "per_bidder": per_bidder})


# ----------------------------------------------------------------------------
# C3 — SF-basis vs the matrix GSF
# ----------------------------------------------------------------------------
def check_c3(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    sf = run_inputs.run.sf_basis
    gsf = matrix_parse.gsf_value
    sf_source = getattr(run_inputs.run, "sf_source", None)
    ev = {"sf_basis": sf, "matrix_gsf": gsf, "sf_source": sf_source}
    if gsf is None:
        return CheckResult("C3", "SF-basis vs matrix GSF", BLOCKER, PASS,
                           f"C3 PASS — sf_basis={sf:,.0f}; no matrix GSF detected to conflate.", ev)
    rel = abs(sf - gsf) / gsf if gsf else 1.0
    if sf != gsf and rel > GSF_GUARD:
        return CheckResult("C3", "SF-basis vs matrix GSF", BLOCKER, PASS,
                           f"C3 PASS — sf_basis={sf:,.0f} distinct from matrix GSF "
                           f"({gsf:,.0f}); matrix GSF reported-only.", ev)
    # The basis equals/near the matrix GSF. Under the SF suggest-and-confirm gate
    # this is LEGITIMATE iff the user EXPLICITLY confirmed the matrix GSF
    # (sf_source='matrix-confirmed') — the matrix GSF is then a deliberately
    # accepted basis, not an accidental conflation. Without that explicit
    # confirmation it remains a BLOCKER (the original must-never guard).
    if sf_source == "matrix-confirmed":
        return CheckResult("C3", "SF-basis vs matrix GSF", BLOCKER, PASS,
                           f"C3 PASS — sf_basis={sf:,.0f} equals matrix GSF, but this was "
                           f"EXPLICITLY confirmed (--sf-confirmed); matrix GSF accepted as "
                           f"the SF basis by owner decision (auditable).", ev)
    return CheckResult("C3", "SF-basis vs matrix GSF", BLOCKER, FAIL,
                       f"C3 FAIL — sf_basis equals/near matrix GSF ({gsf:,.0f}) WITHOUT "
                       f"explicit --sf-confirmed. The matrix GSF must not silently drive "
                       f"$/SF (spec §3). BLOCKER.", ev)


# ----------------------------------------------------------------------------
# C4 — Tier assignment matches the band rule
# ----------------------------------------------------------------------------
def check_c4(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    run = run_inputs.run
    per_bidder = []
    bad = None
    for b in pipeline_result["bidders"]:
        expected = _band_label_for(b["per_sf"], run)
        per_bidder.append({"name": b["name"], "per_sf": b["per_sf"],
                           "pipeline_tier": b["tier"], "expected_tier": expected})
        if expected != b["tier"] and bad is None:
            bad = (b["name"], b["per_sf"], b["tier"], expected)
    lo = int(round(run.band_low_per_sf)); hi = int(round(run.band_high_per_sf))
    if bad is None:
        return CheckResult("C4", "Tier assignment matches band rule", BLOCKER, PASS,
                           f"C4 PASS — tiers consistent with band rule [{lo}-{hi}] for all bidders.",
                           {"band_low_per_sf": lo, "band_high_per_sf": hi, "per_bidder": per_bidder})
    nm, psf, pt, et = bad
    return CheckResult("C4", "Tier assignment matches band rule", BLOCKER, FAIL,
                       f"C4 FAIL — {nm} ${psf}/SF assigned '{pt}', rule says '{et}'. BLOCKER.",
                       {"band_low_per_sf": lo, "band_high_per_sf": hi, "per_bidder": per_bidder})


# ----------------------------------------------------------------------------
# C5 — Ranking integrity
# ----------------------------------------------------------------------------
def check_c5(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    bidders = pipeline_result["bidders"]
    n = len(bidders)
    full_coverage = bool(pipeline_result.get("full_coverage", False))

    # PARTIAL COVERAGE: there is no ranking, and THE ABSENCE IS THE CHECK
    # (Marvin P1-2 §4.3). C5 used to carry a partial-coverage special case that
    # accepted equal-Overall ties "at partial coverage"; that case is DELETED,
    # because at partial coverage there is nothing to tie. This ruling removes a
    # special case rather than adding one — which is a good sign about the
    # ruling.
    if not full_coverage:
        ranked = [b["name"] for b in bidders if "rank" in b]
        ev = {"n": n, "full_coverage": False, "bidders_carrying_a_rank": ranked}
        if ranked:
            return CheckResult(
                "C5", "Ranking integrity", BLOCKER, FAIL,
                f"C5 FAIL — the evaluation record is incomplete, so no bidder "
                f"may carry a rank, but {len(ranked)} do ({', '.join(ranked)}). "
                f"A rank computed on ragged coverage orders the evaluator's "
                f"calendar, not the bidders. BLOCKER.", ev)
        return CheckResult(
            "C5", "Ranking integrity", BLOCKER, PASS,
            f"C5 PASS — partial coverage: no bidder carries a rank, as ruled "
            f"({n} bidder(s) listed, unranked).", ev)

    ranks = [b["rank"] for b in bidders]
    ev = {"ranks": sorted(ranks), "n": n, "full_coverage": full_coverage}
    if sorted(ranks) != list(range(1, n + 1)):
        detail = "duplicate" if len(set(ranks)) != len(ranks) else "gap"
        return CheckResult("C5", "Ranking integrity", BLOCKER, FAIL,
                           f"C5 FAIL — ranks not contiguous 1..{n} ({detail}: {sorted(ranks)}). BLOCKER.", ev)
    ordered = sorted(bidders, key=lambda b: b["rank"])
    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i + 1]
        oa = a["overall"]["numeric"]; ob = b["overall"]["numeric"]
        oa = oa if oa is not None else -1
        ob = ob if ob is not None else -1
        # A genuine inversion (higher-ranked bidder has a STRICTLY lower Overall)
        # is a BLOCKER at any coverage — the sort must be descending by Overall.
        if oa < ob - 1e-9:
            return CheckResult("C5", "Ranking integrity", BLOCKER, FAIL,
                               f"C5 FAIL — inversion at rank {a['rank']}: {a['name']} "
                               f"overall {oa} < {b['name']} {ob}. BLOCKER.", ev)
        # The documented tiebreak: on equal Overall, the lower total ranks
        # higher. Enforced unconditionally now — this branch is only reachable
        # at full coverage (the partial-coverage special case that used to
        # excuse collapsed provisional Overalls is gone, along with the
        # provisional Overalls themselves).
        if abs(oa - ob) <= 1e-9 and a["total"] > b["total"] + MONEY_TOL:
            return CheckResult("C5", "Ranking integrity", BLOCKER, FAIL,
                               f"C5 FAIL — tiebreak violated at rank {a['rank']}: {a['name']} "
                               f"total ${a['total']:,.0f} > {b['name']} ${b['total']:,.0f} on "
                               f"equal overall. BLOCKER.", ev)
    return CheckResult("C5", "Ranking integrity", BLOCKER, PASS,
                       f"C5 PASS — ranks contiguous 1..{n}, sort + tiebreak "
                       f"consistent.", ev)


# ----------------------------------------------------------------------------
# C6 — Variance signs and magnitudes
# ----------------------------------------------------------------------------
def check_c6(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    run = run_inputs.run
    band_mid = run.variance_mid                                   # $M
    band_mid_psf = run.variance_mid * 1e6 / run.sf_basis
    per_bidder = []
    sign_bad = None
    for b in pipeline_result["bidders"]:
        bid_m = b["bid_m"]
        expected_var = round(bid_m - band_mid, 2)                 # re-derived variance ($M)
        psf_sign_ok = ((b["per_sf"] - band_mid_psf) >= 0) == (expected_var >= 0)
        per_bidder.append({"name": b["name"], "variance_m": expected_var,
                           "psf_sign_ok": psf_sign_ok})
        if not psf_sign_ok and sign_bad is None:
            sign_bad = (b["name"], expected_var, b["per_sf"])
    if sign_bad is not None:
        nm, v, psf = sign_bad
        return CheckResult("C6", "Variance signs and magnitudes", BLOCKER, FAIL,
                           f"C6 FAIL — {nm}: variance ${v}M inconsistent with $/SF {psf} vs "
                           f"band_mid {band_mid_psf:.0f}/SF (sign flip). BLOCKER.",
                           {"band_mid_m": band_mid, "per_bidder": per_bidder})
    return CheckResult("C6", "Variance signs and magnitudes", BLOCKER, PASS,
                       f"C6 PASS — variance signs consistent with band_mid=${band_mid*1e6:,.0f}.",
                       {"band_mid_m": band_mid, "per_bidder": per_bidder})


# ----------------------------------------------------------------------------
# C7 — Duplicate handling is logged
# ----------------------------------------------------------------------------
def check_c7(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    log = _log_text(pipeline_result)
    norms: Dict[str, List] = {}
    for b in matrix_parse.blocks:
        norms.setdefault(b.norm, []).append(b)
    dups = {nrm: blks for nrm, blks in norms.items() if len(blks) > 1}
    out_norms = {normalize_name(b["name"]) for b in pipeline_result["bidders"]}
    bad = None
    handled = []
    for nrm, blks in dups.items():
        # Count survivors from the INCLUDED/scored bidder set only. A dropped
        # duplicate carries included=False and must NOT count toward survivors —
        # the previous logic matched the shared normalized name against the
        # output set, which counted BOTH same-named blocks (the kept one AND the
        # dropped col-AX block) as survivors and produced a phantom "2 survivors"
        # BLOCKER on a clean run. A block survives only if it is itself included
        # AND its name is present in the scored output.
        kept = sum(1 for b in blks if b.included
                   and normalize_name(b.name) in out_norms)
        display = blks[0].name
        # A drop is "logged" if there is an explicit DUPLICATE … DROPPED … kept
        # line naming THIS firm (name-specific, not any drop line anywhere).
        has_log = re.search(
            r"(?im)^DUPLICATE:.*" + re.escape(display) + r".*DROPPED.*kept", log
        ) is not None
        handled.append({"name": display, "kept_in_output": kept, "logged": bool(has_log)})
        # Correctly handled when EXACTLY one survivor remains AND a drop is logged.
        # A genuine failure is: >1 survivor (two same-named bidders both scored),
        # or a drop with no log line.
        if (kept != 1 or not has_log) and bad is None:
            bad = (display, kept, has_log)
    if not dups:
        return CheckResult("C7", "Duplicate handling is logged", BLOCKER, PASS,
                           "C7 PASS — no normalized duplicate firm names in matrix.",
                           {"duplicates": []})
    if bad is None:
        names = ", ".join(h["name"] for h in handled)
        return CheckResult("C7", "Duplicate handling is logged", BLOCKER, PASS,
                           f"C7 PASS — {len(dups)} duplicate(s) handled with explicit drop log: {names}.",
                           {"duplicates": handled})
    nm, kept, logged = bad
    if not logged:
        reason = "no drop log line"
    elif kept > 1:
        reason = f"{kept} survivors"
    else:
        reason = f"{kept} survivors (expected exactly 1)"
    return CheckResult("C7", "Duplicate handling is logged", BLOCKER, FAIL,
                       f"C7 FAIL — duplicate '{nm}' resolved without explicit log ({reason}). BLOCKER.",
                       {"duplicates": handled})


# ----------------------------------------------------------------------------
# C8 — Completeness flags (the historic false-zero trap)
# ----------------------------------------------------------------------------
def check_c8(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    incl = matrix_parse.included_blocks
    per_bidder = [{"name": b.name, "populated_divisions": b.populated_divisions}
                  for b in incl]
    zero = [b.name for b in incl if b.populated_divisions == 0]
    out_of_band = [b.name for b in incl
                   if not (DIV_BAND[0] <= b.populated_divisions <= DIV_BAND[1])
                   and b.populated_divisions != 0]
    n = len(incl)
    if zero:
        return CheckResult("C8", "Completeness flags (false-zero trap)", BLOCKER, FAIL,
                           f"C8 FAIL — {zero[0]}: populated_divisions=0. BLOCKER.",
                           {"per_bidder": per_bidder, "zero": zero, "out_of_band": out_of_band})
    if out_of_band:
        return CheckResult("C8", "Completeness flags (false-zero trap)", WARN, FAIL,
                           f"C8 FAIL — populated_divisions out of band {DIV_BAND} for: "
                           f"{', '.join(out_of_band)}. WARN.",
                           {"per_bidder": per_bidder, "zero": [], "out_of_band": out_of_band})
    return CheckResult("C8", "Completeness flags (false-zero trap)", WARN, PASS,
                       f"C8 PASS — populated_divisions in {list(DIV_BAND)} for all {n} included bidders.",
                       {"per_bidder": per_bidder, "zero": [], "out_of_band": []})


# ----------------------------------------------------------------------------
# C9 — Baseline-anchoring fingerprint disclosed
# ----------------------------------------------------------------------------
def check_c9(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    log = _log_text(pipeline_result)
    fps = pipeline_result.get("fingerprints", []) or []
    missing = []
    for h in fps:
        bidder = getattr(h, "bidder_name", None)
        if bidder is None:
            continue
        if not re.search(r"(?im)^.*FINGERPRINT:.*" + re.escape(bidder), log):
            missing.append(bidder)
    ev = {"hits": len(fps), "missing_disclosure": missing}
    if missing:
        return CheckResult("C9", "Baseline-anchoring fingerprint disclosed", WARN, FAIL,
                           f"C9 FAIL — fingerprint hit near {missing[0]} not disclosed in log. WARN.", ev)
    return CheckResult("C9", "Baseline-anchoring fingerprint disclosed", WARN, PASS,
                       f"C9 PASS — {len(fps)} fingerprint hit(s) all disclosed in log.", ev)


# ----------------------------------------------------------------------------
# C10 — No silently auto-dropped bidder
# ----------------------------------------------------------------------------
def check_c10(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    log = _log_text(pipeline_result)
    out_norms = {normalize_name(b["name"]) for b in pipeline_result["bidders"]}
    bad = None
    accounted = []
    for b in matrix_parse.blocks:
        if normalize_name(b.name) in out_norms or b.norm in out_norms:
            continue
        nm = b.name
        is_dup = re.search(r"(?im)^DUPLICATE:.*" + re.escape(nm) + r".*DROPPED", log) is not None
        is_excl = (re.search(r"(?im)^EXCLUSION \(ruling\):.*" + re.escape(nm), log) is not None
                   or (b.drop_reason and "EXCLUDED by ruling" in (b.drop_reason or "")))
        accounted.append({"name": nm, "duplicate": bool(is_dup), "ruled_exclusion": bool(is_excl)})
        if not (is_dup or is_excl) and bad is None:
            bad = nm
    if bad is not None:
        return CheckResult("C10", "No silently auto-dropped bidder", BLOCKER, FAIL,
                           f"C10 FAIL — '{bad}' missing from output with no matching ruling "
                           f"or duplicate log. BLOCKER.", {"missing": accounted})
    return CheckResult("C10", "No silently auto-dropped bidder", BLOCKER, PASS,
                       "C10 PASS — every matrix bidder accounted for (kept | duplicate | ruled exclusion).",
                       {"missing": accounted})


# ----------------------------------------------------------------------------
# C11 — Overall is the honest weighted average (curve retired, P0-6)
# ----------------------------------------------------------------------------
def check_c11(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    """Re-derive each bidder's Overall from its 1-10 category scores and the
    run's category weights (weighted SUM over scored categories x10 — the
    scoring contract), and require the emitted Overall to equal it. Nothing —
    curve, bonus, or manual edit — may adjust the ranked number (P0-6, Floyd
    consolidated ruling verdict d).

    AT PARTIAL COVERAGE C11 ASSERTS THE OVERALL IS ABSENT (Marvin P1-2 §4.3).
    As built this check compared a re-derived `expected` (a number) against the
    `emitted` Overall — and §3.3 deliberately withholds the emitted Overall at
    partial coverage, so it would have FAILED on every honest provisional run.
    The absence is its own re-derivable claim, not a hole in the check:
    WITHHOLDING IS NOT ADJUSTING.
    """
    full_coverage = bool(pipeline_result.get("full_coverage", False))
    if not full_coverage:
        emitted = [b["name"] for b in pipeline_result["bidders"]
                   if b["overall"].get("numeric") is not None]
        ev = {"full_coverage": False, "bidders_with_an_overall": emitted}
        if emitted:
            return CheckResult(
                "C11", "Overall = honest weighted average (no adjustment)",
                BLOCKER, FAIL,
                f"C11 FAIL — the evaluation record is incomplete, so no Overall "
                f"may be emitted, but {len(emitted)} bidder(s) carry one "
                f"({', '.join(emitted)}). An Overall computed on a partial "
                f"record is not a weaker answer to the same question; it is a "
                f"different question. BLOCKER.", ev)
        return CheckResult(
            "C11", "Overall = honest weighted average (no adjustment)",
            BLOCKER, PASS,
            "C11 PASS — partial coverage: the Overall is withheld for every "
            "bidder, as ruled.", ev)

    cats = pipeline_result.get("categories", []) or []
    weights = {c["key"]: c["weight_pct"] / 100.0 for c in cats}
    per_bidder = []
    bad = None
    for b in pipeline_result["bidders"]:
        num = 0.0
        any_scored = False
        for key, w in weights.items():
            s = (b.get("scores") or {}).get(key)
            if s is not None:
                num += float(s) * w
                any_scored = True
        expected = round(num * 10.0, 1) if any_scored else None
        emitted = b["overall"].get("numeric")
        ok = (expected is None and emitted is None) or (
            expected is not None and emitted is not None
            and abs(emitted - expected) <= 0.05)
        per_bidder.append({"name": b["name"], "emitted": emitted,
                           "recomputed_wavg": expected})
        if not ok and bad is None:
            bad = (b["name"], emitted, expected)
    if bad is None:
        return CheckResult(
            "C11", "Overall = honest weighted average (no adjustment)",
            BLOCKER, PASS,
            f"C11 PASS — every Overall re-derives as the raw weighted average "
            f"of its category scores ({len(per_bidder)} bidder(s)); no "
            f"adjustment present.",
            {"per_bidder": per_bidder})
    nm, emitted, expected = bad
    return CheckResult(
        "C11", "Overall = honest weighted average (no adjustment)",
        BLOCKER, FAIL,
        f"C11 FAIL — {nm}: emitted Overall {emitted} != recomputed raw "
        f"weighted average {expected}. Something adjusted the ranked number "
        f"(the presentation curve is retired — P0-6). BLOCKER.",
        {"per_bidder": per_bidder})


# ----------------------------------------------------------------------------
# C12 — Qualitative coverage gating
# ----------------------------------------------------------------------------
def check_c12(matrix_parse, run_inputs, pipeline_result,
              *, summary_context=None) -> CheckResult:
    """C12 VERIFIES THAT THE DOCUMENT MAKES NO CLAIM THE RUN IS NOT ENTITLED TO.

    Rewritten to Marvin's P1-2 §4.2 contract. The old check enforced an asterisk
    ("63* (prov., 60% coverage)") and a `applied` curve flag — half of it was
    already DEAD (the curve was retired under P0-6, so `applied` is always
    False), and the other half policed exactly the marker §2.2 rules
    insufficient: a caveat does not survive a phone photo of the ranking table.

    Note the shape of the win: C12 GETS SIMPLER. It stops policing a label and
    starts re-deriving an ABSENCE. Absences are cheap to check and impossible
    to fudge.

    Every assertion is a BLOCKER, and that passes Marvin's own discriminator
    (the one Floyd adopted as the program standard): each violation is the
    DOCUMENT CONTRADICTING ITSELF — the run computed partial coverage and the
    document made a full-coverage claim. The operator is not even a party to it.
    That is not the tool disagreeing with anyone.
    """
    bidders = pipeline_result["bidders"]
    cats = pipeline_result.get("categories", []) or []
    weights = {c["key"]: c["weight_pct"] / 100.0 for c in cats}
    full_coverage = bool(pipeline_result.get("full_coverage", False))
    watermark_tokens = [r["token"] for r in (pipeline_result.get("watermark") or [])]

    # (e) every per-bidder coverage figure re-derives from the scored-cell count
    #     against the framework weights — INDEPENDENTLY, not echoed.
    per_bidder = []
    bad_cov = None
    for b in bidders:
        scored_weight = sum(
            w for key, w in weights.items()
            if (b.get("scores") or {}).get(key) is not None)
        emitted = b["overall"].get("coverage")
        per_bidder.append({"name": b["name"], "emitted_coverage": emitted,
                           "recomputed_coverage": round(scored_weight, 6)})
        if emitted is None or abs(emitted - scored_weight) > 1e-6:
            if bad_cov is None:
                bad_cov = (b["name"], emitted, scored_weight)
    ev = {"per_bidder": per_bidder, "full_coverage": full_coverage,
          "watermark": watermark_tokens}

    if bad_cov is not None:
        nm, emitted, recomputed = bad_cov
        return CheckResult(
            "C12", "Document claims match the run's entitlement", BLOCKER, FAIL,
            f"C12 FAIL — {nm}: emitted coverage {emitted} does not re-derive "
            f"from the scored cells against the framework weights "
            f"({recomputed:.6g}). BLOCKER.", ev)

    if not full_coverage:
        # (a) no bidder carries a rank
        ranked = [b["name"] for b in bidders if "rank" in b]
        if ranked:
            return CheckResult(
                "C12", "Document claims match the run's entitlement", BLOCKER,
                FAIL,
                f"C12 FAIL — partial coverage, but {len(ranked)} bidder(s) "
                f"carry a rank ({', '.join(ranked)}). A provisional card does "
                f"not rank. BLOCKER.", ev)
        # (b) no Overall numeric is emitted for any bidder
        scored = [b["name"] for b in bidders
                  if b["overall"].get("numeric") is not None]
        if scored:
            return CheckResult(
                "C12", "Document claims match the run's entitlement", BLOCKER,
                FAIL,
                f"C12 FAIL — partial coverage, but {len(scored)} bidder(s) "
                f"carry an Overall ({', '.join(scored)}). A provisional card "
                f"does not print an Overall. BLOCKER.", ev)
        # (c) the summary names no leader
        leader = (summary_context or {}).get("winner_name")
        if leader:
            return CheckResult(
                "C12", "Document claims match the run's entitlement", BLOCKER,
                FAIL,
                f"C12 FAIL — partial coverage, but the summary names a leader "
                f"({leader!r}). A front-runner is a ranking claim. BLOCKER.",
                ev)
        # (d) the watermark is present and carries the evaluation-incomplete
        #     reason
        if "evaluation incomplete" not in watermark_tokens:
            return CheckResult(
                "C12", "Document claims match the run's entitlement", BLOCKER,
                FAIL,
                f"C12 FAIL — partial coverage, but the artifacts do not carry "
                f"the 'evaluation incomplete' watermark reason (got "
                f"{watermark_tokens or 'none'}). BLOCKER.", ev)
        return CheckResult(
            "C12", "Document claims match the run's entitlement", BLOCKER, PASS,
            "C12 PASS — partial coverage: no rank, no Overall, no named leader, "
            "and the artifacts are watermarked 'evaluation incomplete'. "
            "Coverage re-derives for every bidder.", ev)

    # FULL coverage — the converse (§4.2): no provisional marking, no
    # evaluation-incomplete watermark reason, ranking present and complete.
    if "evaluation incomplete" in watermark_tokens:
        return CheckResult(
            "C12", "Document claims match the run's entitlement", BLOCKER, FAIL,
            "C12 FAIL — full coverage, but the artifacts carry the "
            "'evaluation incomplete' watermark reason. BLOCKER.", ev)
    unranked = [b["name"] for b in bidders if "rank" not in b]
    if unranked:
        return CheckResult(
            "C12", "Document claims match the run's entitlement", BLOCKER, FAIL,
            f"C12 FAIL — full coverage, but {len(unranked)} bidder(s) carry no "
            f"rank ({', '.join(unranked)}). BLOCKER.", ev)
    return CheckResult(
        "C12", "Document claims match the run's entitlement", BLOCKER, PASS,
        f"C12 PASS — full coverage: every bidder ranked and scored, no "
        f"provisional marking. Coverage re-derives for all {len(bidders)} "
        f"bidder(s).", ev)


# ----------------------------------------------------------------------------
# C13 — PII / scope discipline (over the report text we are about to write)
# ----------------------------------------------------------------------------
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\b\(\d{3}\)\s*\d{3}-\d{4}\b|\b\d{3}-\d{3}-\d{4}\b")


def check_c13(matrix_parse, run_inputs, pipeline_result, report_text: str = "") -> CheckResult:
    text = report_text or ""
    ssn = _SSN_RE.search(text)
    email = _EMAIL_RE.search(text)
    phone = _PHONE_RE.search(text)
    excl = pipeline_result.get("exclusion_register") or {}
    over600 = [k for k, v in excl.items() if isinstance(v, str) and len(v) > 600]
    over400 = [k for k, v in excl.items() if isinstance(v, str) and 400 < len(v) <= 600]
    ev = {"ssn": bool(ssn), "email": bool(email), "phone": bool(phone),
          "blocks_over_600": over600, "blocks_over_400": over400}
    if ssn or email or phone:
        hit = "SSN-shape" if ssn else ("email" if email else "phone")
        m = ssn or email or phone
        return CheckResult("C13", "PII / scope discipline", BLOCKER, FAIL,
                           f"C13 FAIL — {hit} pattern at offset {m.start()}. BLOCKER.", ev)
    if over600:
        return CheckResult("C13", "PII / scope discipline", BLOCKER, FAIL,
                           f"C13 FAIL — exclusion block(s) exceed 600 chars (not summarized): "
                           f"{', '.join(over600)}. BLOCKER.", ev)
    if over400:
        return CheckResult("C13", "PII / scope discipline", WARN, FAIL,
                           f"C13 FAIL — exclusion block(s) 400-600 chars (tighten summary): "
                           f"{', '.join(over400)}. WARN.", ev)
    return CheckResult("C13", "PII / scope discipline", BLOCKER, PASS,
                       "C13 PASS — no PII patterns detected; exclusion blocks summarized.", ev)


# ----------------------------------------------------------------------------
# C14 — Bucket separation (Marvin's hard rule)
# ----------------------------------------------------------------------------
def check_c14(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    # The compared total must equal the Row 164 grand total and must NOT include
    # alternates / allowances / unit-price extensions. We re-derive from the
    # parse: equality to row164 proves no extra bucket was folded in (a fold-in
    # would push pipeline.total ABOVE row164 by the alternate/allowance sum).
    per_bidder = []
    bad = None
    for b in pipeline_result["bidders"]:
        row164 = _row164_total_for(matrix_parse, b["name"])
        leak = (b["total"] - row164) if (row164 is not None and b["total"] == b["total"]) else None
        per_bidder.append({"name": b["name"], "pipeline_total": b["total"], "row164": row164,
                           "leak": None if leak is None else round(leak, 2)})
        if leak is not None and abs(leak) > 1.0 and bad is None:
            bad = (b["name"], leak)
    if bad is not None:
        nm, leak = bad
        return CheckResult("C14", "Bucket separation", BLOCKER, FAIL,
                           f"C14 FAIL — {nm} total deviates from Row 164 by ${leak:,.0f} "
                           f"(alternates/allowances/unit prices may be folded in). BLOCKER.",
                           {"per_bidder": per_bidder})
    return CheckResult("C14", "Bucket separation", BLOCKER, PASS,
                       "C14 PASS — compared totals are Row 164 only; "
                       "alternates/allowances/unit prices held separate.",
                       {"per_bidder": per_bidder})


# ----------------------------------------------------------------------------
# C15 — Alias map round-trip
# ----------------------------------------------------------------------------
def check_c15(matrix_parse, run_inputs, pipeline_result, aliases=None) -> CheckResult:
    aliases = aliases or {}
    norm_alias = {normalize_name(k): str(v).strip()
                  for k, v in aliases.items() if k and v}
    raw_norms = {b.norm for b in matrix_parse.blocks}
    per_bidder = []
    bad = None
    alias_count = 0
    for b in pipeline_result["bidders"]:
        disp_norm = normalize_name(b["name"])
        # display == a raw matrix name (no alias) OR an alias maps a raw -> display
        traced = disp_norm in raw_norms
        if not traced:
            for raw_norm in raw_norms:
                if norm_alias.get(raw_norm) and \
                        normalize_name(norm_alias[raw_norm]) == disp_norm:
                    traced = True
                    alias_count += 1
                    break
        per_bidder.append({"name": b["name"], "traced": traced})
        if not traced and bad is None:
            bad = b["name"]
    if bad is not None:
        return CheckResult("C15", "Alias map round-trip", WARN, FAIL,
                           f"C15 FAIL — display name '{bad}' has no raw source. WARN.",
                           {"per_bidder": per_bidder})
    return CheckResult("C15", "Alias map round-trip", WARN, PASS,
                       f"C15 PASS — display names trace to raw matrix names "
                       f"({alias_count} alias(es) applied).",
                       {"per_bidder": per_bidder})


# ----------------------------------------------------------------------------
# C16 — Exclusion register coverage
# ----------------------------------------------------------------------------
def check_c16(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    # Per-bidder exclusion narrative + the summarized register both live in the
    # pipeline_result (the pipeline is the single source the audit reads), NOT on
    # a parse instance attribute — so the check can't be polluted by a stray
    # attribute set on a (possibly id-reused) ParsedMatrix instance. The current
    # parser does not extract exclusion narrative, so absent both keys we report
    # INFO (nothing to cover), never a false WARN.
    excl_text = pipeline_result.get("exclusions_text") or {}
    register = pipeline_result.get("exclusion_register") or {}
    has_excl = {k for k, v in excl_text.items() if v and len(str(v)) > 0}
    if not excl_text:
        return CheckResult("C16", "Exclusion register coverage", INFO, PASS,
                           "C16 INFO — no per-bidder exclusion narrative extracted "
                           "(register not applicable this run).",
                           {"has_exclusions": [], "register_keys": list(register.keys())})
    reg_norms = {normalize_name(k) for k in register}
    missing = [b for b in has_excl if normalize_name(b) not in reg_norms]
    if missing:
        return CheckResult("C16", "Exclusion register coverage", WARN, FAIL,
                           f"C16 FAIL — {missing[0]} carries exclusions text but no register "
                           f"entry. WARN.",
                           {"has_exclusions": sorted(has_excl), "missing": missing})
    return CheckResult("C16", "Exclusion register coverage", WARN, PASS,
                       f"C16 PASS — exclusion register covers all {len(has_excl)} bidders with "
                       f"narrative content.",
                       {"has_exclusions": sorted(has_excl), "missing": []})


# ----------------------------------------------------------------------------
# C17 — Producer quarantine marker (Marvin P0-7 hard rule 5)
# ----------------------------------------------------------------------------
def check_c17(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    flagged = bool(getattr(matrix_parse, "quarantine_flag", False))
    ev = {"quarantine_banner": flagged, "sheet": matrix_parse.sheet_name}
    if flagged:
        return CheckResult(
            "C17", "Producer quarantine marker", BLOCKER, FAIL,
            "C17 FAIL — the workbook carries the producer's Stage-6b "
            "quarantine banner (POST_WRITE_TIEOUT_FAILURE: \"AUTOMATED CHECK "
            "FAILED\"). Figures failed the matrix tool's own self-check; a "
            "scorecard must not be delivered from this workbook. Re-run the "
            "matrix cleanly first. BLOCKER.", ev)
    return CheckResult(
        "C17", "Producer quarantine marker", BLOCKER, PASS,
        "C17 PASS — no producer quarantine banner on the consumed sheet.", ev)


# ----------------------------------------------------------------------------
# C18 — Cross-sheet grand-total tie-out (Marvin P0-7 hard rule 4)
# ----------------------------------------------------------------------------
def check_c18(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    """The producer enforces mirror-GT == leveled-GT. When the workbook
    carries BOTH sheets, independently parse the OTHER sheet and tie every
    bidder's grand total out against the consumed parse. A mismatch means the
    producer contract itself is broken -> BLOCKER. Single-data-sheet
    workbooks (legacy) report INFO/not-applicable."""
    from .matrix import LEVELED_SHEET, MIRROR_SHEET, MatrixParser

    path = pipeline_result.get("meta", {}).get("matrix_path")
    consumed = matrix_parse.sheet_name
    ev: Dict[str, Any] = {"consumed_sheet": consumed, "matrix_path": path}
    if not path or not os.path.exists(path):
        return CheckResult(
            "C18", "Cross-sheet grand-total tie-out", WARN, FAIL,
            "C18 FAIL — matrix path unavailable to the audit; cross-sheet "
            "tie-out not performed. WARN.", ev)
    try:
        import openpyxl as _oxl
        wb = _oxl.load_workbook(path, read_only=True, data_only=True)
        names = list(wb.sheetnames)
    except Exception as exc:
        return CheckResult(
            "C18", "Cross-sheet grand-total tie-out", WARN, FAIL,
            f"C18 FAIL — could not re-open the workbook for the cross-sheet "
            f"tie-out ({exc}). WARN.", ev)
    other = None
    for cand in (LEVELED_SHEET, MIRROR_SHEET):
        if cand != consumed and cand in names:
            other = cand
            break
    ev["other_sheet"] = other
    if other is None:
        return CheckResult(
            "C18", "Cross-sheet grand-total tie-out", INFO, PASS,
            "C18 INFO — single data sheet in workbook; cross-sheet tie-out "
            "not applicable.", ev)
    try:
        mcfg = dict(run_inputs.block("matrix"))
        mcfg["sheet_name"] = other
        other_parsed = MatrixParser(mcfg).parse(path)
    except Exception as exc:
        return CheckResult(
            "C18", "Cross-sheet grand-total tie-out", WARN, FAIL,
            f"C18 FAIL — the other sheet ({other}) did not parse for the "
            f"tie-out ({exc}). WARN.", ev)

    def _totals(blocks):
        out: Dict[str, List] = {}
        for b in blocks:
            out.setdefault(b.norm, []).append(b.grand_total)
        return {k: sorted((x for x in v if x is not None)) for k, v in out.items()}

    mine = _totals(matrix_parse.blocks)
    theirs = _totals(other_parsed.blocks)
    ev["consumed_totals"] = mine
    ev["other_totals"] = theirs
    if set(mine) != set(theirs):
        only_mine = sorted(set(mine) - set(theirs))
        only_theirs = sorted(set(theirs) - set(mine))
        return CheckResult(
            "C18", "Cross-sheet grand-total tie-out", BLOCKER, FAIL,
            f"C18 FAIL — bidder fields differ between {consumed} and {other} "
            f"(only on consumed: {only_mine}; only on other: {only_theirs}). "
            f"Producer contract broken. BLOCKER.", ev)
    for norm in mine:
        a, b = mine[norm], theirs[norm]
        if len(a) != len(b) or any(abs(x - y) > MONEY_TOL for x, y in zip(a, b)):
            return CheckResult(
                "C18", "Cross-sheet grand-total tie-out", BLOCKER, FAIL,
                f"C18 FAIL — grand totals for '{norm}' differ between "
                f"{consumed} ({a}) and {other} ({b}). The producer enforces "
                f"mirror-GT == leveled-GT; a mismatch means the producer "
                f"contract itself is broken. BLOCKER.", ev)
    return CheckResult(
        "C18", "Cross-sheet grand-total tie-out", BLOCKER, PASS,
        f"C18 PASS — all {len(mine)} bidder grand totals tie out between "
        f"{consumed} and {other} (penny-exact).", ev)


# ----------------------------------------------------------------------------
# C19 — Framework drift vs the declaration (Marvin §4.4, W1/W2/W5/W6/W8)
# ----------------------------------------------------------------------------
# THE CRUX OF P1-4, so the reasoning is written down here rather than in a
# review doc nobody reads at 5pm:
#
#   SEVERITY KEYS ON THE DECLARATION, NOT ON THE DRIFT.
#   Declared drift is a WARN. Undeclared drift, and any self-contradicting
#   declaration, is a BLOCKER. The harm was never the deviation. The harm is
#   the silence.
#
# A blanket BLOCKER on drift would fire on every roofing-only package and every
# legitimate re-weighting — and a control that fires on the honest case trains
# the operator to click past it. Note also what this check does NOT do: it never
# fires because the framework disagrees with the tool's preference. Every
# BLOCKER below is the document contradicting ITSELF. That is the test to apply
# to any check you are tempted to add here.
def check_c19(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    pack = pipeline_result.get("pack")
    if pack is None:
        return CheckResult(
            "C19", "Framework drift vs declared basis", INFO, PASS,
            "C19 INFO — no run pack supplied; there is no declared framework "
            "basis to check drift against.", {"input_channel": "individual"})

    basis = pack.framework_basis
    ev = {
        "framework_basis": basis,
        "framework_hash": pack.framework_hash,
        "standing_hash": pack.standing_hash,
        "standing_version": pack.standing_version,
        "standing_effective_date": pack.standing_effective_date,
        "ruling_note": pack.framework_ruling_note,
    }

    # W8 — the bootstrap. Falke has no standing framework on file (§10.2), so
    # there is nothing to measure drift FROM. The check cannot claim what it does
    # not know: measuring against ARA's shipped default would be reporting a
    # policy-drift finding about a vendor artifact, which is F1's error with a
    # different subject. It degrades honestly and says so.
    if not pack.standing_available:
        return CheckResult(
            "C19", "Framework drift vs declared basis", WARN, FAIL,
            "C19 WARN — no standing evaluation framework was on file for this "
            "run, so departure from Falke's evaluation policy CANNOT be "
            "detected. The categories and weights below were supplied for this "
            "project and are disclosed as such; no claim is made about policy "
            "drift. This degrades to a warning until Falke adopts a versioned, "
            "dated standing-framework.xlsx of their own.", ev)

    drifted = pack.framework_hash != pack.standing_hash
    ev["drifted"] = drifted

    # W1 + W2 — undeclared drift / a declaration that contradicts its own
    # content. Mechanically one condition; both are BLOCKER, and both are the
    # same shape as F1's fingerprint-contradiction gate.
    if basis == "standing" and drifted:
        return CheckResult(
            "C19", "Framework drift vs declared basis", BLOCKER, FAIL,
            f"C19 FAIL — the Framework tab declares basis 'standing' (Falke's "
            f"standing framework, unmodified) but its categories/weights do NOT "
            f"match standing framework {pack.standing_version} "
            f"(effective {pack.standing_effective_date}). The declaration "
            f"contradicts its own content: either restore the standing weights, "
            f"or declare what these weights actually are ('project-specific' or "
            f"'revised-post-opening') with a ruling note. BLOCKER.", ev)

    if basis == "standing":
        return CheckResult(
            "C19", "Framework drift vs declared basis", WARN, PASS,
            f"C19 PASS — categories and weights match Falke's standing "
            f"framework {pack.standing_version} (effective "
            f"{pack.standing_effective_date}), applied unmodified.", ev)

    # W5 / W6 — declared, disclosed, legitimate. Logged; no friction beyond that.
    return CheckResult(
        "C19", "Framework drift vs declared basis", WARN, FAIL,
        f"C19 WARN — the evaluation framework departs from Falke's standing "
        f"framework {pack.standing_version} and says so: declared "
        f"'{basis}' with the ruling recorded "
        f"(\"{pack.framework_ruling_note}\"). Legitimate and disclosed on the "
        f"card; surfaced here so the departure is in the award file.", ev)


# ----------------------------------------------------------------------------
# C20 — Two-clock coherence (Marvin §4.4, W3/W4)
# ----------------------------------------------------------------------------
# How one workbook honors two clocks: it DECLARES both dates and audits their
# ordering. The file is a container, not a semantic merge — which is what makes
# Floyd's protected-list note ("the run pack packages them, it does not merge
# their semantics") operationally true rather than merely asserted.
#
# Honest scope note (§4.1): the pack does not introduce the post-hoc tuning
# exposure and does not eliminate it. Every cell in the pack is written after
# bids are open, so no file SHAPE can enforce the plan-lock; only an artifact
# that exists before bid opening can. What this check does is refuse to let a
# self-contradicting record pass silently.
def check_c20(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    from .run_pack import as_date

    pack = pipeline_result.get("pack")
    if pack is None:
        return CheckResult(
            "C20", "Framework/scoring date coherence", INFO, PASS,
            "C20 INFO — no run pack supplied; no declared dates to check.", {})

    bid_open = as_date(pack.bid_opening_date)
    lock = as_date(pack.framework_lock_date)
    scored = as_date(pack.scoring_completed_date)
    ev = {"bid_opening_date": pack.bid_opening_date,
          "framework_lock_date": pack.framework_lock_date,
          "scoring_completed_date": pack.scoring_completed_date,
          "framework_basis": pack.framework_basis}

    # W3 — 'project-specific' claims a PRE-opening lock and states a POST-opening
    # date. The declaration contradicts its own dates.
    if (pack.framework_basis == "project-specific" and lock and bid_open
            and lock > bid_open):
        return CheckResult(
            "C20", "Framework/scoring date coherence", BLOCKER, FAIL,
            f"C20 FAIL — the Framework tab declares basis 'project-specific', "
            f"which means the weights were locked BEFORE bids were opened — but "
            f"the stated lock date ({pack.framework_lock_date}) is AFTER the "
            f"bid opening date ({pack.bid_opening_date}). The declaration "
            f"contradicts its own dates. If the weights were in fact set after "
            f"opening, declare 'revised-post-opening' — that is legitimate and "
            f"it is disclosed on the card. BLOCKER.", ev)

    # W4 is UNDEFINED, not evaded, on a provisional run: there is no scoring
    # completion date because the record is open (§5.3). Name the skip and why
    # — the C-1 lesson: say the thing you know, and say what you could not check
    # rather than leaving it to be inferred.
    if lock and not scored:
        return CheckResult(
            "C20", "Framework/scoring date coherence", BLOCKER, PASS,
            f"C20 PASS — W3 coherent (bid opening {pack.bid_opening_date}; "
            f"framework lock {pack.framework_lock_date}). W4 NOT EVALUATED: "
            f"the evaluation record is open, so there is no completion date to "
            f"order the framework lock against.", ev)

    # W4 — the plan was locked after the record was made. Incoherent on its face.
    if lock and scored and lock > scored:
        return CheckResult(
            "C20", "Framework/scoring date coherence", BLOCKER, FAIL,
            f"C20 FAIL — the framework lock date ({pack.framework_lock_date}) "
            f"is AFTER the scoring completed date "
            f"({pack.scoring_completed_date}): the evaluation plan was locked "
            f"after the evaluation record was made. One of the two dates is "
            f"wrong. BLOCKER.", ev)

    return CheckResult(
        "C20", "Framework/scoring date coherence", BLOCKER, PASS,
        f"C20 PASS — declared dates are coherent (bid opening "
        f"{pack.bid_opening_date or 'n/a'}; framework lock "
        f"{pack.framework_lock_date or 'n/a'}; scoring completed "
        f"{pack.scoring_completed_date or 'n/a'}).", ev)


# ----------------------------------------------------------------------------
# C21 — Input-channel provenance (Marvin §9.3)
# ----------------------------------------------------------------------------
# Using the individual flags when a pack SHOULD exist means the firm names were
# not pipeline-originated — the one thing the pack was built to guarantee.
#
# The engine cannot reliably DETECT this (it cannot see the matrix run's output
# dir), so it is handled honestly in two places: the skill asks a named question,
# and this check puts the smell in the award file rather than relying on prose
# the operator may never read. It is a WARN, never a block: the operator may have
# legitimately lost the file, and blocking on a guess would be the tool
# overreaching.
def check_c21(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    channel = pipeline_result.get("input_channel", "individual")
    stamped = bool(matrix_parse.producer_stamp)
    ev = {"input_channel": channel, "matrix_stamped": stamped}

    if channel == "pack":
        return CheckResult(
            "C21", "Input-channel provenance", WARN, PASS,
            "C21 PASS — inputs arrived via the run pack, so the scored-firm "
            "names originate from the matrix pipeline rather than from re-entry.",
            ev)
    if stamped:
        return CheckResult(
            "C21", "Input-channel provenance", WARN, FAIL,
            "C21 WARN — this matrix was produced by create-matrix (it carries "
            "the producer stamp), which emits a Scorecard Inputs pack, but the "
            "scoring inputs were supplied as individual hand-built files. The "
            "firm names in this run were therefore not pipeline-originated. "
            "Legitimate if the pack was lost or this is an archival re-render — "
            "recorded here either way.", ev)
    return CheckResult(
        "C21", "Input-channel provenance", WARN, PASS,
        "C21 PASS — individual inputs against an unstamped/legacy matrix; no "
        "pack would exist for it. This is the supported legacy path.", ev)


# ----------------------------------------------------------------------------
# C22 — Run-pack binding provenance (Marvin §8.3, tiers I4/I5/I7)
# ----------------------------------------------------------------------------
# The hard tiers (I3 wrong building, I6 roster mismatch, I8 edited producer
# field) never reach the audit — they exit 2 before anything is rendered. What
# lands here are the CONFIRMABLE tiers, so the award file records which one
# applied.
def check_c22(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    pack = pipeline_result.get("pack")
    if pack is None:
        return CheckResult(
            "C22", "Run-pack binding provenance", INFO, PASS,
            "C22 INFO — no run pack supplied; no pack-to-matrix binding to "
            "record.", {})

    binding = pack.binding or {}
    tier = binding.get("tier")
    ev = dict(binding)

    if tier == "I4":
        return CheckResult(
            "C22", "Run-pack binding provenance", WARN, PASS,
            f"C22 PASS — the run pack is bound to this matrix run "
            f"({binding.get('run_id_matrix')}); inputs are pipeline-originated.",
            ev)
    if tier == "I5":
        return CheckResult(
            "C22", "Run-pack binding provenance", WARN, FAIL,
            f"C22 WARN — the run pack was built from a DIFFERENT matrix run "
            f"(pack {binding.get('run_id_pack') or '(none)'} vs matrix "
            f"{binding.get('run_id_matrix')}). The scored-firm roster, project "
            f"identity, and SF all reconcile, so this is consistent with a "
            f"corrected-matrix re-run — but the operator confirmed it rather "
            f"than the tool proving it. Recorded.", ev)
    line = ("C22 WARN — this matrix carries no run identity (it predates the "
            "stamp or was built by hand), so the pack cannot be proven to have "
            "come from it. The roster reconciles; provenance was confirmed by "
            "the operator. Recorded.")
    # Floyd F-3: name the degradation rather than leave it inferable. An
    # identity-less matrix means the cross-project check could not run at all —
    # the one tier Marvin ruled "exit 2, ALWAYS. No warning tier."
    if binding.get("cross_project_check_ran") is False:
        line += (" The matrix also carries no project identity, so the "
                 "cross-project check (I3) COULD NOT RUN — nothing in this run "
                 "verifies that the pack belongs to this building.")
    return CheckResult(
        "C22", "Run-pack binding provenance", WARN, FAIL, line, ev)


# ----------------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------------
@dataclass
class AuditResult:
    verdict: str
    overall_status: str          # pass | fail
    checks: List[CheckResult]
    counts: Dict[str, int]
    run_id: str
    generated_at: str

    def to_json_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "1.0",
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "verdict": self.verdict,
            "overall_status": self.overall_status,
            "counts": self.counts,
            "checks": [
                {"name": c.name, "title": c.title, "severity": c.severity,
                 "status": c.status, "verdict_line": c.verdict_line,
                 "evidence": c.evidence}
                for c in self.checks
            ],
        }


def _counts(checks: List[CheckResult]) -> Dict[str, int]:
    return {
        "blocker": sum(1 for c in checks if c.severity == BLOCKER and c.status == FAIL),
        "warn": sum(1 for c in checks if c.severity == WARN and c.status == FAIL),
        "info": sum(1 for c in checks if c.severity == INFO),
        "pass": sum(1 for c in checks if c.status == PASS),
    }


def _verdict(checks: List[CheckResult]) -> str:
    has_blocker = any(c.severity == BLOCKER and c.status == FAIL for c in checks)
    has_warn = any(c.severity == WARN and c.status == FAIL for c in checks)
    if has_blocker:
        return V_FAIL
    if has_warn:
        return V_WARN
    return V_PASS


def audit(matrix_parse, run_inputs, pipeline_result,
          *, aliases: Optional[Dict[str, str]] = None,
          report_text: str = "",
          summary_context: Optional[Dict] = None) -> AuditResult:
    """Run all 18 checks (C1..C18). Pure: no LLM, no network. Failures do NOT
    short-circuit — every check runs so the report is complete.

    matrix_parse    : ParsedMatrix (the structured parse).
    run_inputs      : Config (carries run.sf_basis, band, variance_mid).
    pipeline_result : the run_scorecard() result dict.
    aliases         : the alias map supplied to the run (for C15 round-trip).
    report_text     : the assembled audit_report.md body (for C13's PII scan);
                      when empty C13 still runs over exclusion-block lengths.
    summary_context : the built summary context (for C12's "the summary names
                      no leader" assertion). None = not built yet; C12 then
                      checks only what it can see, and says so by omission
                      rather than by inventing a pass.
    """
    checks: List[CheckResult] = [
        check_c1(matrix_parse, run_inputs, pipeline_result),
        check_c2(matrix_parse, run_inputs, pipeline_result),
        check_c3(matrix_parse, run_inputs, pipeline_result),
        check_c4(matrix_parse, run_inputs, pipeline_result),
        check_c5(matrix_parse, run_inputs, pipeline_result),
        check_c6(matrix_parse, run_inputs, pipeline_result),
        check_c7(matrix_parse, run_inputs, pipeline_result),
        check_c8(matrix_parse, run_inputs, pipeline_result),
        check_c9(matrix_parse, run_inputs, pipeline_result),
        check_c10(matrix_parse, run_inputs, pipeline_result),
        check_c11(matrix_parse, run_inputs, pipeline_result),
        check_c12(matrix_parse, run_inputs, pipeline_result,
                  summary_context=summary_context),
        check_c13(matrix_parse, run_inputs, pipeline_result, report_text=report_text),
        check_c14(matrix_parse, run_inputs, pipeline_result),
        check_c15(matrix_parse, run_inputs, pipeline_result, aliases=aliases),
        check_c16(matrix_parse, run_inputs, pipeline_result),
        check_c17(matrix_parse, run_inputs, pipeline_result),
        check_c18(matrix_parse, run_inputs, pipeline_result),
        check_c19(matrix_parse, run_inputs, pipeline_result),
        check_c20(matrix_parse, run_inputs, pipeline_result),
        check_c21(matrix_parse, run_inputs, pipeline_result),
        check_c22(matrix_parse, run_inputs, pipeline_result),
    ]
    verdict = _verdict(checks)
    return AuditResult(
        verdict=verdict,
        overall_status=FAIL if verdict == V_FAIL else PASS,
        checks=checks,
        counts=_counts(checks),
        run_id=pipeline_result.get("meta", {}).get("run_id", "unknown"),
        generated_at=_dt.datetime.utcnow().isoformat() + "Z",
    )


# ----------------------------------------------------------------------------
# report rendering
# ----------------------------------------------------------------------------
def _verdict_paragraph(ar: AuditResult) -> str:
    if ar.verdict == V_PASS:
        return ("All checks passed. The re-derived totals, $/SF, tiers, ranking, and "
                "structural facts agree with what the scorecard emitted. The scorecard "
                "is safe to deliver to the board.")
    if ar.verdict == V_WARN:
        warns = [c.verdict_line for c in ar.checks if c.severity == WARN and c.status == FAIL]
        return ("No blocking discrepancies, but one or more warnings were raised and MUST "
                "be surfaced verbatim in the cover note (do not bury them): " + " ".join(warns))
    blockers = [c.verdict_line for c in ar.checks if c.severity == BLOCKER and c.status == FAIL]
    return ("BLOCKING discrepancy detected — DO NOT deliver this scorecard as final. Route "
            "back to the pipeline for remediation, then re-audit: " + " ".join(blockers))


def render_report_md(ar: AuditResult, matrix_parse, run_inputs, pipeline_result) -> str:
    run = run_inputs.run
    incl = [b["name"] for b in pipeline_result["bidders"]]
    excluded = []
    duplicates = []
    for b in matrix_parse.blocks:
        if b.drop_reason and "duplicate" in (b.drop_reason or "").lower():
            duplicates.append(b.name)
        elif b.drop_reason and "EXCLUDED by ruling" in (b.drop_reason or ""):
            excluded.append(b.name)
    lines: List[str] = []
    lines.append(f"# Scorecard Self-Audit — Run {ar.run_id}")
    lines.append(f"**Generated:** {ar.generated_at}")
    lines.append(f"**Overall verdict:** {ar.verdict}")
    lines.append(f"**Summary:** {ar.counts['blocker']} blocker(s), "
                 f"{ar.counts['warn']} warning(s), {ar.counts['info']} info.")
    lines.append("")
    lines.append("## Verdict")
    lines.append(_verdict_paragraph(ar))
    lines.append("")
    fails = [c for c in ar.checks if c.status == FAIL]
    if fails:
        lines.append("## Warnings & blockers (spelled out)")
        for c in fails:
            lines.append(f"- **[{c.severity}] {c.name}** — {c.verdict_line}")
        lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| Check | Severity | Status | Verdict line |")
    lines.append("|---|---|---|---|")
    for c in ar.checks:
        vl = c.verdict_line.replace("|", "\\|")
        lines.append(f"| {c.name} {c.title} | {c.severity} | {c.status.upper()} | {vl} |")
    lines.append("")
    lines.append("## Evidence appendix")
    gsf = matrix_parse.gsf_value
    sf_source = getattr(run, "sf_source", None)
    if sf_source == "matrix-confirmed":
        sf_src_label = "explicitly confirmed matrix Row-10 GSF (--sf-confirmed)"
        gsf_note = "— ACCEPTED as the SF basis by owner confirmation"
    else:
        sf_src_label = "explicit --sf-basis / run inputs"
        gsf_note = "— REPORTED ONLY"
    lines.append(f"- consumed sheet: {matrix_parse.sheet_name} "
                 f"(mode: {getattr(matrix_parse, 'sheet_mode', 'n/a')} — "
                 f"Marvin P0-7)")
    lines.append(f"- sf_basis: {run.sf_basis:,.0f} (source: {sf_src_label})")
    lines.append(f"- matrix GSF: {('—' if gsf is None else format(gsf, ',.0f'))} "
                 f"(source: matrix) {gsf_note}")
    lines.append(f"- band: low={run.band_low} high={run.band_high} "
                 f"mid={run.variance_mid} ($M)")
    lines.append(f"- included bidders: {', '.join(incl)}")
    lines.append(f"- excluded bidders (by ruling): "
                 f"{', '.join(excluded) if excluded else 'none'}")
    lines.append(f"- duplicates dropped: "
                 f"{', '.join(duplicates) if duplicates else 'none'}")
    lines.append("")
    return "\n".join(lines)


def write_audit_artifacts(ar: AuditResult, matrix_parse, run_inputs,
                          pipeline_result, out_dir: str) -> Dict[str, str]:
    """Write audit_report.md + audit.json to out_dir. Returns the paths.

    The report is rendered FIRST, then C13 is re-run over the rendered body so its
    PII scan covers the exact text being written (rubric C13). The re-run only
    tightens the verdict (it can flip PASS->FAIL on a leak); it never silently
    downgrades a failure.
    """
    os.makedirs(out_dir, exist_ok=True)
    report = render_report_md(ar, matrix_parse, run_inputs, pipeline_result)
    c13 = check_c13(matrix_parse, run_inputs, pipeline_result, report_text=report)
    ar.checks = [c13 if c.name == "C13" else c for c in ar.checks]
    ar.counts = _counts(ar.checks)
    ar.verdict = _verdict(ar.checks)
    ar.overall_status = FAIL if ar.verdict == V_FAIL else PASS
    report = render_report_md(ar, matrix_parse, run_inputs, pipeline_result)

    md_path = os.path.join(out_dir, "audit_report.md")
    json_path = os.path.join(out_dir, "audit.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(ar.to_json_dict(), fh, indent=2, default=str)
    return {"report_md": md_path, "audit_json": json_path}
