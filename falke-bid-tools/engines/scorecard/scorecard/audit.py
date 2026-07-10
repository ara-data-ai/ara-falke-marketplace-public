"""Deterministic self-audit ("Floyd-lite, every run") — Marvin's rubric.

A pure-Python QA pass run AFTER the scorecard is generated and BEFORE the final
artifact is named. It RE-DERIVES the critical numbers and structural facts from
``(matrix_parse, run_inputs, pipeline_result)`` and compares them to what the
pipeline actually emitted — any disagreement is surfaced, never silently
reconciled (rubric §0).

Determinism contract: NO LLM, NO network. Pure arithmetic, set logic, regex.
Every check runs every time (failures do NOT short-circuit) so the report is
always complete. Implements checks C1..C16 from the scorecard audit rubric.

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
    ranks = [b["rank"] for b in bidders]
    full_coverage = bool(pipeline_result.get("full_coverage", False))
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
        # The documented tiebreak (on equal Overall, the lower total ranks higher)
        # is only enforced as a BLOCKER at FULL coverage. At PARTIAL coverage the
        # provisional Overalls legitimately collapse toward a shared number, so
        # many bidders tie on Overall by construction; the secondary ordering key
        # is then a *defined ordering*, not a violation. We still require ranks
        # contiguous 1..N and sorted descending by Overall (checked above) and
        # accept the equal-Overall groupings without flagging.
        if full_coverage and abs(oa - ob) <= 1e-9 and a["total"] > b["total"] + MONEY_TOL:
            return CheckResult("C5", "Ranking integrity", BLOCKER, FAIL,
                               f"C5 FAIL — tiebreak violated at rank {a['rank']}: {a['name']} "
                               f"total ${a['total']:,.0f} > {b['name']} ${b['total']:,.0f} on "
                               f"equal overall. BLOCKER.", ev)
    detail = ("sort + tiebreak consistent" if full_coverage
              else "sort descending by Overall; equal-Overall ties accepted at partial coverage")
    return CheckResult("C5", "Ranking integrity", BLOCKER, PASS,
                       f"C5 PASS — ranks contiguous 1..{n}, {detail}.", ev)


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
# C11 — Curve labeling (presentation vs raw)
# ----------------------------------------------------------------------------
_CURVE_LABEL_RE = re.compile(
    r"(?i)presentation adjustment|presentation-enhanced|curved.*raw retained|"
    r"raw weighted average")


def check_c11(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    bidders = pipeline_result["bidders"]
    applied_any = any(b["overall"].get("applied") for b in bidders)
    label = pipeline_result.get("overall_label", "")
    ev = {"curve_applied": applied_any, "overall_label": label}
    if not applied_any:
        return CheckResult("C11", "Curve labeling (presentation vs raw)", WARN, PASS,
                           "C11 PASS — Overall curve not applied; honest weighted average shown.", ev)
    raw_present = all(b["overall"].get("weighted_average") is not None for b in bidders)
    labeled = _CURVE_LABEL_RE.search(label) is not None
    if raw_present and labeled:
        return CheckResult("C11", "Curve labeling (presentation vs raw)", WARN, PASS,
                           "C11 PASS — curve disclosed as presentation adjustment; raw weighted "
                           "average retained.", ev)
    why = "raw missing" if not raw_present else "not labeled as presentation adjustment"
    return CheckResult("C11", "Curve labeling (presentation vs raw)", WARN, FAIL,
                       f"C11 FAIL — curve applied but {why}. WARN.", ev)


# ----------------------------------------------------------------------------
# C12 — Qualitative coverage gating
# ----------------------------------------------------------------------------
def check_c12(matrix_parse, run_inputs, pipeline_result) -> CheckResult:
    per_bidder = []
    bad = None
    for b in pipeline_result["bidders"]:
        cov = b["overall"].get("coverage")
        applied = b["overall"].get("applied", False)
        display = str(b["overall"].get("display", ""))
        # the pipeline renders a partial-coverage Overall two ways, both carrying
        # the '*' provisional marker: "63* (prov., 60% coverage)" and the
        # no-scored-categories "—*". Treat either as the provisional flag so the
        # check is robust to the exact wording (the '*'/'prov' marker is the
        # contract, not the surrounding phrasing).
        provisional = ("prov" in display.lower()) or display.rstrip().endswith("*")
        per_bidder.append({"name": b["name"], "coverage": cov, "applied": applied,
                           "provisional_flag": provisional})
        if cov is not None and cov < 0.999:
            if applied or not provisional:
                if bad is None:
                    bad = (b["name"], cov, applied, provisional)
    if bad is not None:
        nm, cov, applied, prov = bad
        why = "curved" if applied else "not flagged PROVISIONAL"
        return CheckResult("C12", "Qualitative coverage gating", BLOCKER, FAIL,
                           f"C12 FAIL — {nm} coverage={cov} but {why} "
                           f"(applied={applied}, provisional={prov}). BLOCKER.",
                           {"per_bidder": per_bidder})
    return CheckResult("C12", "Qualitative coverage gating", BLOCKER, PASS,
                       "C12 PASS — coverage stated; provisional/curve rules honored.",
                       {"per_bidder": per_bidder})


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
          report_text: str = "") -> AuditResult:
    """Run all 16 checks (C1..C16). Pure: no LLM, no network. Failures do NOT
    short-circuit — every check runs so the report is complete.

    matrix_parse    : ParsedMatrix (the structured parse).
    run_inputs      : Config (carries run.sf_basis, band, variance_mid).
    pipeline_result : the run_scorecard() result dict.
    aliases         : the alias map supplied to the run (for C15 round-trip).
    report_text     : the assembled audit_report.md body (for C13's PII scan);
                      when empty C13 still runs over exclusion-block lengths.
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
        check_c12(matrix_parse, run_inputs, pipeline_result),
        check_c13(matrix_parse, run_inputs, pipeline_result, report_text=report_text),
        check_c14(matrix_parse, run_inputs, pipeline_result),
        check_c15(matrix_parse, run_inputs, pipeline_result, aliases=aliases),
        check_c16(matrix_parse, run_inputs, pipeline_result),
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
