"""Scorecard Summary — the plain-English board companion to the scorecard.

Renders into prose what the pipeline already computed in the run result (the
same dict that feeds the scorecard + audit). It is NOT a new analysis: every
field is a templated sentence with values drawn from `result` per Marvin's
content rubric (the scorecard summary content rubric). No
re-ranking, no re-scoring, no free-form generation of the conclusions — the only
model-drafted language rides through the per-bidder `ranking[].strengths/.risks`
bullets the pipeline already produced.

`build_summary_context(result)` returns the context object Anna's
`scorecard-summary-template.html` expects:
  project_name, winner_name, winner_rationale,
  ranked_bidders[ {rank,name,tier,overall} ], runners_up_note,
  methodology_plain, caveats[str], bottom_line, footer_note.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .mechanical import (TIER_DEFENSIVE, TIER_LABELS, TIER_MID, TIER_PREMIUM,
                         TIER_RISK, TIER_TOP)

# Decisive vs close-call cut on the Overall gap to rank 2 (rubric §2a).
CLOSE_CALL_GAP = 5.0


def _fmt_money_m(total: float) -> str:
    """$X.XXM (two decimals) from a raw dollar total — matches the card."""
    if total is None or (isinstance(total, float) and total != total):  # NaN
        return "an amount the matrix did not report"
    return f"${total / 1e6:.2f}M"


def _per_sf_int(per_sf) -> str:
    """$/SF as a whole integer string (rubric §0). Falls back to '—'."""
    try:
        return f"{int(round(float(per_sf)))}"
    except (TypeError, ValueError):
        return "—"


def _overall_display(b: Dict) -> str:
    """The Overall the card shows for this bidder (curved if applied, else the
    weighted average; provisional marker preserved). Reuses the pipeline's own
    display string — never recomputed here."""
    return b["overall"].get("display", "—")


def _tier_label(tier: str) -> str:
    """Short tier label for the ranking table — the same map the card uses."""
    return TIER_LABELS.get(tier, tier)


def _bidder_by_name(result: Dict, name: str) -> Dict:
    return next(b for b in result["bidders"] if b["name"] == name)


def _winner_rationale(w: Dict, *, provisional: bool, close_call: bool,
                      r2: Optional[Dict]) -> str:
    """The 'why', keyed to tier (rubric §3). RISK gets the guardrail framing
    (no celebration); close-call names both leaders; provisional softens the
    certainty language. Returns one plain-English paragraph."""
    name = w["name"]
    money = _fmt_money_m(w["total"])
    psf = _per_sf_int(w["per_sf"])
    variance = w["section_c"]["variance_text"]
    tier = w["tier"]

    # Guardrail: a RISK-tier top bid is a flag, not a recommendation (rubric §3
    # guardrail). Never write a celebratory "why".
    if tier == TIER_RISK:
        return (
            f"{name} ranks first on the numbers, but at {money} "
            f"(about ${psf}/SF) their price sits materially below the cost we "
            f"independently modeled for this scope. A gap that size usually "
            f"means scope was excluded or margin was compressed, not that the "
            f"work is genuinely cheaper. We do not treat this as a "
            f"recommendation: ARA recommends the board resolve the scope "
            f"question — confirm what is and isn't included — before any bid is "
            f"treated as the pick. Flagged for review."
        )

    # Close call (gap < 5): name BOTH leaders; the number does not separate them.
    if close_call and r2 is not None:
        return (
            f"{name} and {r2['name']} finished essentially tied at the top "
            f"(Overall {_overall_display(w)} vs {_overall_display(r2)}). On "
            f"price, {name} is {w['section_c']['variance_text']} and "
            f"{r2['name']} is {r2['section_c']['variance_text']}. The number "
            f"alone does not separate them; the board should weigh the "
            f"qualitative differences below to choose between two defensible "
            f"best-value picks."
        )

    # Clear winner, by tier.
    if tier == TIER_TOP:
        why = (
            f"{name} is our recommended best value. Their price of {money} "
            f"(about ${psf}/SF) lands right on the cost we independently "
            f"modeled for this scope — neither padded with premium nor cut so "
            f"thin that work looks to be missing. That balance is the single "
            f"best signal a bid is realistic and likely to hold through "
            f"construction without a wave of change orders."
        )
    elif tier == TIER_MID:
        why = (
            f"{name} is our recommended best value. Their price of {money} "
            f"(about ${psf}/SF) sits modestly below our modeled baseline — "
            f"slightly aggressive, but plausible — and their qualifications "
            f"carried them to the top of the ranking. We'd want the board to "
            f"confirm the scope and allowance structure hold at that price, but "
            f"on balance they offer the strongest overall package."
        )
    elif tier in (TIER_DEFENSIVE, TIER_PREMIUM):
        why = (
            f"{name} is our recommended best value — note this is not the "
            f"lowest price. At {money} (about ${psf}/SF) they price above our "
            f"modeled baseline, but they earned the top overall score on "
            f"qualifications and a conservative, low-surprise posture: fewer "
            f"change orders, lower dispute risk. For a board that values cost "
            f"certainty over the lowest headline number, that can be the better "
            f"total outcome."
        )
    else:  # pragma: no cover — all tiers handled above; defensive default
        why = (
            f"{name} is our recommended best value at {money} (about "
            f"${psf}/SF), earning the top overall score once price and "
            f"qualifications are weighed together."
        )

    # Provisional: downgrade certainty — current front-runner on a provisional
    # score, not a settled recommendation (rubric §2b).
    if provisional:
        why = (
            f"On the qualitative review completed so far, {why} "
            f"Because that review is not yet finished, treat this as the "
            f"current front-runner on a provisional score rather than a settled "
            f"recommendation — the ranking could still shift."
        )
    return why


def _runner_reason(tier: str) -> str:
    """One plain-English reason a runner-up ranked lower, by tier (rubric §4)."""
    return {
        TIER_TOP: "also well-aligned on price; edged out on qualifications / "
                  "overall score.",
        TIER_MID: "lower price, but modestly below our baseline — moderate "
                  "change-order drift risk we'd want managed.",
        TIER_RISK: "lowest headline price, but materially below our modeled "
                   "baseline — that gap usually signals excluded scope or thin "
                   "margin, and carries the highest change-order and dispute "
                   "risk. Cheapest is not safest here.",
        TIER_DEFENSIVE: "priced above baseline — a conservative, lower-risk "
                        "posture, but more upfront capital than the "
                        "recommendation.",
        TIER_PREMIUM: "priced well above baseline — the most conservative bid, "
                      "and the most expensive; a best-value penalty applies.",
    }.get(tier, "ranked lower on the combined price-and-qualifications score.")


def _runners_up_note(result: Dict, ranking: List[Dict]) -> str:
    """One line each for the next 1-2 bidders + a closing line if the field is
    deep (rubric §4). Empty string when there is only one comparable bid."""
    if len(ranking) < 2:
        return "Only one comparable bid was received, so there is no runner-up to compare against."

    parts = []
    for r in ranking[1:3]:
        b = _bidder_by_name(result, r["name"])
        parts.append(
            f"{r['name']} (#{r['rank']}, ${_per_sf_int(b['per_sf'])}/SF) — "
            f"{_runner_reason(r['tier'])}"
        )
    note = " ".join(parts)
    remaining = len(ranking) - 3
    if remaining > 0:
        note += (f" The remaining {remaining} "
                 f"{'bid' if remaining == 1 else 'bids'} ranked below these and "
                 f"are detailed in the full scorecard.")
    return note


def _methodology_plain(sf_basis_text: str) -> str:
    """The fixed 2-3 sentence plain-English method (rubric §5). Only the SF
    basis is filled."""
    return (
        f"We first modeled what this scope should cost — a baseline, expressed "
        f"both as a dollar range and as a price per square foot (on a "
        f"{sf_basis_text}-SF basis). Every bid is then measured against that "
        f"baseline and sorted into a tier — right on target, below it, or above "
        f"it — because a price only means something next to the cost of the "
        f"work it's supposed to cover. Finally we combine that price read with "
        f"qualitative scores (the contractor's experience, references, "
        f"schedule, and risk) into a single overall ranking, so the board "
        f"compares like for like instead of just comparing headline totals."
    )


def _sf_basis_text(result: Dict) -> str:
    """Pull the SF basis out of the footer_note ('SF basis N ·') for the
    methodology sentence; fall back to a neutral phrase if not present."""
    footer = result.get("meta", {}).get("footer_note", "") or ""
    marker = "SF basis "
    if marker in footer:
        tail = footer.split(marker, 1)[1].strip()
        token = tail.split(" ", 1)[0].rstrip(",.;·")
        if token:
            return token
    return "modeled"


def _build_caveats(result: Dict) -> List[str]:
    """The board-facing disclosures (rubric §6). Order: baseline-derived (a),
    curve-or-provisional (b), judgment (c), informs-not-decides (d), no-legal-
    advice (e). (a),(c),(d),(e) are ALWAYS present; (b) flips between the curve
    note and the provisional note by coverage; a conflict-of-interest line is
    NOT speculated here (rubric §6 only adds it on a real signal)."""
    caveats: List[str] = []

    # (a) ALWAYS — the baseline is bid-derived, not an independent yardstick.
    baseline_caveat = (
        "Our cost baseline was built using the bids themselves as a reference, "
        "not from a fully independent estimate. So treat it as a sensible "
        "reference point for comparing the bids to each other — not as an "
        "outside, second-opinion price. If the board wants a truly independent "
        "check, that would be a separate estimate."
    )
    fingerprints = result.get("fingerprints") or []
    if fingerprints:
        h = fingerprints[0]
        baseline_caveat += (
            f" In fact, the tool detected that the baseline line "
            f"'{h.baseline_label}' matches {h.bidder_name}'s number very "
            f"closely, which is why we say the baseline leans on the bids "
            f"rather than standing apart from them."
        )
    caveats.append(baseline_caveat)

    # (b) CONDITIONAL — provisional note when coverage is incomplete, else the
    # presentation-curve note when a curve was applied. (When neither, omit (b).)
    full_coverage = bool(result.get("full_coverage", False))
    overall_label = result.get("overall_label", "") or ""
    if not full_coverage:
        coverages = [b["overall"].get("coverage", 1.0) for b in result["bidders"]]
        worst_pct = min(coverages) * 100 if coverages else 0
        caveats.append(
            f"These overall scores are provisional — the qualitative review was "
            f"only about {worst_pct:.0f}% complete for some bidders, so the "
            f"presentation adjustment was held back and the ranking could shift "
            f"once the review is finished."
        )
    elif "PRESENTATION ADJUSTMENT" in overall_label:
        caveats.append(
            "The single 'overall' number you see has been adjusted for "
            "presentation — it spreads the field out and applies a value "
            "penalty to the most expensive bid so the comparison reads clearly. "
            "The raw, unadjusted scores are shown alongside in the full "
            "scorecard. The adjustment changes how the scores display, not who "
            "ranks where on the underlying work."
        )

    # (c) ALWAYS — qualitative scores are informed judgment.
    caveats.append(
        "The experience, references, schedule, and risk scores are our "
        "professional judgment from the bidders' documents — they're considered "
        "and consistent, but they are judgment, not measured fact."
    )

    # (d) ALWAYS — this informs the award; it does not make it.
    caveats.append(
        "This scorecard is decision support for the board. It does not award "
        "the contract or replace the board's own due diligence — checking "
        "licensing, bonding, insurance, references, and any conflicts of "
        "interest before voting."
    )

    # (e) ALWAYS — not legal advice (regulated FL condo setting).
    caveats.append(
        "Nothing here is legal advice. Questions about board procedure, "
        "contract terms, or statutory compliance should go to the "
        "association's attorney."
    )
    return caveats


def _bottom_line(w: Dict, *, provisional: bool, close_call: bool,
                 r2: Optional[Dict]) -> str:
    """The one-sentence bottom line, selected by winner certainty (rubric §7)."""
    name = w["name"]
    money = _fmt_money_m(w["total"])
    tier = w["tier"]

    if tier == TIER_RISK:
        return (
            "the lowest-priced bid sits materially below what this work should "
            "cost — we recommend resolving that scope gap before treating any "
            "bid as the recommendation."
        )
    if close_call and r2 is not None:
        return (
            f"{name} and {r2['name']} are neck-and-neck at the top — both are "
            f"defensible best-value picks, and the choice comes down to the "
            f"qualitative differences noted below."
        )
    if provisional:
        return (
            f"{name} is the current front-runner at {money}, on a provisional "
            f"score — we recommend confirming the remaining qualitative review "
            f"before the board treats this as final."
        )
    if tier in (TIER_DEFENSIVE, TIER_PREMIUM):
        return (
            f"we recommend {name} at {money} — not the cheapest, but the "
            f"strongest overall package once price and risk are weighed "
            f"together."
        )
    # TOP / MID clear winner
    return (
        f"we recommend {name} as the best value at {money} — their price "
        f"matches what the work should cost and they earned the top overall "
        f"score; it is not the lowest bid, and that is by design."
    )


def build_summary_context(result: Dict) -> Dict:
    """Derive Anna's summary context object from the pipeline `result` dict.

    `result` is exactly what `pipeline.run_scorecard()` returns. We reuse its
    already-computed fields (ranking, per-bidder tier/total/per_sf/overall,
    full_coverage, overall_label, fingerprints, meta) — nothing is recomputed.
    """
    ranking = result["ranking"]  # rank-sorted 1..N by the pipeline
    if not ranking:
        raise ValueError("Cannot build a summary: the run produced no ranked bidders.")

    winner_name = ranking[0]["name"]
    w = _bidder_by_name(result, winner_name)
    r2 = _bidder_by_name(result, ranking[1]["name"]) if len(ranking) > 1 else None

    # gap = Overall(rank1) - Overall(rank2); close call when < 5 (rubric §2a).
    close_call = False
    if r2 is not None:
        o1 = w["overall"].get("numeric")
        o2 = r2["overall"].get("numeric")
        if o1 is not None and o2 is not None:
            close_call = (o1 - o2) < CLOSE_CALL_GAP

    provisional = not bool(result.get("full_coverage", False))

    ranked_bidders = [
        {
            "rank": r["rank"],
            "name": r["name"],
            "tier": _tier_label(r["tier"]),
            "overall": _overall_display(_bidder_by_name(result, r["name"])),
        }
        for r in ranking
    ]

    return {
        "project_name": result["meta"]["project_name"],
        "winner_name": winner_name,
        "winner_rationale": _winner_rationale(
            w, provisional=provisional, close_call=close_call, r2=r2),
        "ranked_bidders": ranked_bidders,
        "runners_up_note": _runners_up_note(result, ranking),
        "methodology_plain": _methodology_plain(_sf_basis_text(result)),
        "caveats": _build_caveats(result),
        "bottom_line": _bottom_line(
            w, provisional=provisional, close_call=close_call, r2=r2),
        "footer_note": result["meta"].get("footer_note", ""),
    }
