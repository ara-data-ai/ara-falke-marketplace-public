"""Templated narrative text (Marvin §4.2, §5, §6, §8; Darvish §1.5).

The TIER-keyed sentences are reproducible templates (JUDGMENT but deterministic).
The firm-specific flourishes are NOT reproducible from the matrix and are sourced
from a per-bidder qualitative note input (default empty -> templated bullet only).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .mechanical import (TIER_DEFENSIVE, TIER_MID, TIER_PREMIUM, TIER_RISK,
                         TIER_TOP)

# Section B "quick read" — templated per tier (Marvin §4.2)
TIER_QUICK_READ = {
    TIER_TOP: "Market-aligned with modeled baseline; realistic South-FL $/SF.",
    TIER_MID: "Aggressive but plausible; modestly below band — moderate drift risk.",
    TIER_DEFENSIVE: "Above baseline; conservative posture, lower drift, higher cost.",
    TIER_PREMIUM: "Far above baseline; most conservative, lowest drift, highest cost.",
    TIER_RISK: ("Materially below modeled baseline; high scope-drift / CO risk "
                "unless scope materially excluded."),
}

# Section C interpretation — keyed to variance bucket (Darvish §1.5)
def section_c_interpretation(tier: str) -> str:
    return {
        TIER_RISK: "Most under-baseline; highest volatility and dispute risk.",
        TIER_MID: "Moderate savings with elevated allowance/drift exposure.",
        TIER_TOP: "Best balance of realism and execution maturity.",
        TIER_DEFENSIVE: "Defensive posture; higher upfront capital, low drift.",
        TIER_PREMIUM: "Very conservative; lowest drift probability, highest cost.",
    }.get(tier, "")


# Section F price-posture bullets — templatable from tier (Marvin §8)
def price_posture_bullets(tier: str) -> Dict[str, List[str]]:
    """Returns {'strengths': [...], 'risks': [...]} from tier alone."""
    if tier == TIER_TOP:
        return {"strengths": ["Pricing aligned to modeled takeoff baseline."],
                "risks": ["Standard allowance-creep exposure on a renovation."]}
    if tier == TIER_MID:
        return {"strengths": ["Moderate savings vs baseline."],
                "risks": ["Modestly under baseline — watch allowance structure."]}
    if tier == TIER_DEFENSIVE:
        return {"strengths": ["Conservative pricing; lower change-order drift."],
                "risks": ["Above baseline — higher upfront capital cost."]}
    if tier == TIER_PREMIUM:
        return {"strengths": ["Most conservative posture; lowest drift risk."],
                "risks": ["Far above baseline — best-value penalty applies."]}
    # RISK
    return {"strengths": ["Lowest headline price."],
            "risks": ["Materially below baseline; must assume exclusions or "
                      "margin compression — elevated dispute/CO risk."]}


def merge_qualitative_notes(
    tier: str,
    qual_notes: Optional[Dict[str, List[str]]],
) -> Dict[str, List[str]]:
    """Combine templated price-posture bullets with optional firm-specific notes.
    qual_notes: {'strengths': [...], 'risks': [...]} from the qualitative layer.
    """
    base = price_posture_bullets(tier)
    if qual_notes:
        base["strengths"] = base["strengths"] + list(qual_notes.get("strengths", []))
        base["risks"] = base["risks"] + list(qual_notes.get("risks", []))
    return base


# Section D framework table (Marvin §6, static config, verbatim each run)
FRAMEWORK_ROWS = [
    {"category": "Market-aligned pricing", "weight": "25%",
     "captures": "Closeness to takeoff baseline & South-FL $/SF realism."},
    {"category": "Scope completeness / clarity", "weight": "15%",
     "captures": "Inclusions/exclusions/allowances quality; fewer silent omissions."},
    {"category": "Condo-specific execution experience", "weight": "15%",
     "captures": "Occupied high-rise / common-area execution."},
    {"category": "Change order exposure risk", "weight": "15%",
     "captures": "Drift likelihood from under-baseline pricing / allowances."},
    {"category": "Reputation & longevity", "weight": "10%",
     "captures": "Track record, references."},
    {"category": "Financial strength / stability", "weight": "10%",
     "captures": "Implied resilience given price posture / scale."},
    {"category": "Project controls & infrastructure", "weight": "5%",
     "captures": "RFI / submittal / change-control discipline."},
    {"category": "Documentation quality / professionalism", "weight": "5%",
     "captures": "Form completeness / accuracy, submission clarity."},
]
