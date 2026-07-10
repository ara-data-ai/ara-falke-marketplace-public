"""Qualitative 1-10 scoring scaffold (Darvish modeling spec §3).

Design rules (non-negotiable, §3.1):
  1. Evidence-grounded — no evidence, no fabricated score.
  2. Structured output — each category returns the CategoryScore schema below.
  3. Human-override by construction — override slot supersedes & is logged.
  4. No silent invention — absent evidence -> score=null + "ANALYST INPUT REQUIRED".

What the skill can ALWAYS compute without bidder docs (mechanical fallback, §3.4):
  - Pricing (25%)  : fully from tier (Marvin §7.3).
  - CO Risk (15%)  : seeded from volatility % (Darvish §3.3).
  - Scope (15%)    : provisional from exclusion count + line-completeness.
  - Docs (5%)      : provisional from completeness ratio + arithmetic check.
The four external categories (Condo Exp, Reputation, Financial, Controls = 40%)
have NO matrix source -> null + flag unless a bidder doc / analyst supplies them.

The live LLM call (drafting scores from bidder PDFs) is an INTEGRATION POINT —
see `LLMScorer` (a stubbed protocol). Wire a real client at invocation time.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from .config import Config

# canonical category keys + display names + weights live in config['weights']
CATEGORY_ORDER = [
    "pricing", "scope", "condo_exp", "co_risk",
    "reputation", "financial", "controls", "docs",
]
CATEGORY_DISPLAY = {
    "pricing": "Pricing",
    "scope": "Scope",
    "condo_exp": "Condo Exp",
    "co_risk": "CO Risk",
    "reputation": "Reputation",
    "financial": "Financial",
    "controls": "Controls",
    "docs": "Docs",
}

ANALYST_FLAG = "ANALYST INPUT REQUIRED"

# which categories the skill can mechanically seed without bidder docs
MECHANICAL_CATEGORIES = {"pricing", "co_risk"}
PROVISIONAL_CATEGORIES = {"scope", "docs"}      # partial mechanical anchor
EXTERNAL_CATEGORIES = {"condo_exp", "reputation", "financial", "controls"}


@dataclass
class CategoryScore:
    """Structured per-category output (Darvish §3.1.2)."""

    category: str
    score: Optional[int]                     # 1-10 or None (never invented)
    rationale: str = ""
    evidence_quotes: List[str] = field(default_factory=list)
    confidence: str = "low"                  # high|medium|low
    evidence_status: str = "absent"          # sufficient|partial|absent
    source: str = "mechanical_seed"          # llm|mechanical_seed|human_override
    flag: Optional[str] = None
    # human override slot (§3.1.3) — when set, supersedes downstream
    override_score: Optional[int] = None
    override_by: Optional[str] = None
    override_note: Optional[str] = None
    run_id: Optional[str] = None
    ts: str = field(default_factory=lambda: _dt.datetime.utcnow().isoformat() + "Z")

    @property
    def effective_score(self) -> Optional[int]:
        """Override wins; else the modeled/mechanical score."""
        return self.override_score if self.override_score is not None else self.score

    @property
    def is_scored(self) -> bool:
        return self.effective_score is not None


@dataclass
class BidderScores:
    name: str
    categories: Dict[str, CategoryScore]

    def weighted_average_x10(self, weights: Dict[str, float]) -> Dict:
        """Honest weighted average, x10 to the /100 scale.

        Returns {wavg, wavg_full_coverage_equiv, coverage}.

        Per Darvish §3.4: at PARTIAL coverage we do NOT rescale to look like a
        full /100. `wavg` is the weighted SUM over scored categories x10 (so a
        55%-coverage card reads lower than a fully-scored one — by design),
        reported alongside the explicit coverage. At FULL coverage (scored
        weight = 1.0) this equals the conventional weighted average and is the
        number the §2 curve consumes.

        `wavg_full_coverage_equiv` (the rescaled mean of scored cats x10) is
        provided ONLY for diagnostics / analyst context, never shown as the
        board /100 at partial coverage.
        """
        num = 0.0
        scored_weight = 0.0
        for cat, w in weights.items():
            cs = self.categories.get(cat)
            # COVERAGE counts a category as covered when it has an EFFECTIVE
            # score — i.e. either an LLM/mechanical score OR a supplied human
            # override (CategoryScore.is_scored -> effective_score is not None,
            # and effective_score prefers override_score). A manual override
            # therefore lifts coverage exactly like an LLM score, so the gold
            # overrides (all 8 categories supplied) take coverage to 100% and
            # the §2 curve un-gates. Absent evidence AND no override stays null
            # (is_scored False) -> not counted -> ANALYST INPUT REQUIRED and the
            # curve stays withheld below 100%.
            if cs and cs.is_scored:
                num += cs.effective_score * w
                scored_weight += w
        if scored_weight == 0:
            return {"wavg": None, "wavg_full_coverage_equiv": None, "coverage": 0.0}
        wavg = num * 10.0                                  # NOT rescaled (§3.4)
        rescaled = (num / scored_weight) * 10.0            # diagnostic only
        return {
            "wavg": round(wavg, 2),
            "wavg_full_coverage_equiv": round(rescaled, 2),
            "coverage": round(scored_weight, 4),
        }

    def unscored_categories(self) -> List[str]:
        return [c for c in CATEGORY_ORDER
                if c in self.categories and not self.categories[c].is_scored]


# ----------------------------------------------------------------------------
# mechanical seeds (always available, no bidder docs needed)
# ----------------------------------------------------------------------------
def seed_pricing(tier: str, cfg: Config) -> CategoryScore:
    """Pricing 1-10 from tier (Marvin §7.3). MECHANICAL, always available."""
    ps = cfg.block("pricing_scores")
    mapping = {
        "TOP": ps["top"], "MID": ps["mid"], "DEFENSIVE": ps["defensive"],
        "PREMIUM": ps["premium"], "RISK": ps["risk_low"],
    }
    score = mapping.get(tier, ps["risk_floor"])
    return CategoryScore(
        category="pricing", score=int(score), confidence="high",
        evidence_status="sufficient", source="mechanical_seed",
        rationale=f"Tier={tier}: $/SF vs modeled band (Marvin §4.1/§7.3).",
    )


def seed_co_risk(volatility_central: float, cfg: Config) -> CategoryScore:
    """CO-Risk 1-10 seeded from volatility % (Darvish §3.3). Higher=lower risk.
    LLM may judgment-adjust ±1 with evidence; here we return the seed."""
    score = None
    for band in cfg.block("co_risk_seed"):
        if volatility_central <= band["max_vol"]:
            score = band["score"]
            break
    if score is None:
        score = cfg.block("co_risk_seed")[-1]["score"]
    return CategoryScore(
        category="co_risk", score=int(score), confidence="medium",
        evidence_status="partial", source="mechanical_seed",
        rationale=(f"Seeded from modeled volatility {volatility_central:.1f}% "
                   f"(Darvish §3.3). LLM may adjust ±1 with allowance/markup-cap "
                   f"evidence."),
    )


def seed_scope(populated_divisions: int, peer_median: int,
               exclusion_count: Optional[int], cfg: Config) -> CategoryScore:
    """Provisional Scope score from line-completeness + exclusion count (§3.3).
    Flagged for judgment confirmation; severity of exclusions needs the doc."""
    ratio = (populated_divisions / peer_median) if peer_median else 1.0
    if ratio >= 0.99:
        base = 7
    elif ratio >= 0.85:
        base = 6
    elif ratio >= 0.6:
        base = 4
    else:
        base = 3
    note = (f"Provisional: line-completeness {populated_divisions}/{peer_median} "
            f"peer-median (ratio {ratio:.2f}).")
    if exclusion_count is not None:
        note += f" Exclusion count={exclusion_count} (severity needs the doc)."
    return CategoryScore(
        category="scope", score=int(base), confidence="low",
        evidence_status="partial", source="mechanical_seed",
        rationale=note,
        flag="PROVISIONAL — confirm exclusion severity from bidder document.",
    )


def seed_docs(completeness_ratio: float, arithmetic_ok: Optional[bool],
              cfg: Config) -> CategoryScore:
    """Provisional Docs score from completeness ratio + arithmetic flag (§3.3)."""
    if completeness_ratio >= 0.99 and arithmetic_ok is not False:
        base = 8
    elif completeness_ratio >= 0.85:
        base = 6
    else:
        base = 4
    return CategoryScore(
        category="docs", score=int(base), confidence="low",
        evidence_status="partial", source="mechanical_seed",
        rationale=(f"Provisional: completeness ratio {completeness_ratio:.2f}, "
                   f"arithmetic_ok={arithmetic_ok}. Confirm from bid form."),
        flag="PROVISIONAL — confirm submission quality from bid form.",
    )


def absent_external(category: str) -> CategoryScore:
    """The four external categories with no doc -> null + ANALYST INPUT REQUIRED.
    NEVER imputed to a middle score (§3.4 — the cardinal rule)."""
    return CategoryScore(
        category=category, score=None, confidence="low",
        evidence_status="absent", source="mechanical_seed",
        rationale="No bidder document / external evidence supplied.",
        flag=ANALYST_FLAG,
    )


def build_bidder_scores(
    name: str,
    tier: str,
    volatility_central: float,
    *,
    populated_divisions: int,
    peer_median: int,
    completeness_ratio: float,
    exclusion_count: Optional[int] = None,
    arithmetic_ok: Optional[bool] = None,
    cfg: Config,
    run_id: Optional[str] = None,
) -> BidderScores:
    """Assemble the full 8-category scaffold from mechanical seeds + degradation.

    External categories start as null+flag. A live LLM scorer (integration point)
    or analyst overrides fill them at invocation time.
    """
    cats: Dict[str, CategoryScore] = {}
    cats["pricing"] = seed_pricing(tier, cfg)
    cats["co_risk"] = seed_co_risk(volatility_central, cfg)
    cats["scope"] = seed_scope(populated_divisions, peer_median, exclusion_count, cfg)
    cats["docs"] = seed_docs(completeness_ratio, arithmetic_ok, cfg)
    for cat in EXTERNAL_CATEGORIES:
        cats[cat] = absent_external(cat)
    for cs in cats.values():
        cs.run_id = run_id
    return BidderScores(name=name, categories=cats)


def apply_overrides(bs: BidderScores, overrides: Dict[str, Dict]) -> None:
    """Apply human overrides (§3.1.3). overrides: {category: {score, by, note}}.
    Logged via the CategoryScore fields; effective_score then uses the override.
    """
    for cat, ov in (overrides or {}).items():
        cs = bs.categories.get(cat)
        if cs is None:
            continue
        cs.override_score = ov.get("score")
        cs.override_by = ov.get("by")
        cs.override_note = ov.get("note")
        cs.source = "human_override"
        cs.ts = _dt.datetime.utcnow().isoformat() + "Z"


# ----------------------------------------------------------------------------
# LLM scorer — INTEGRATION POINT (stubbed)
# ----------------------------------------------------------------------------
class LLMScorer(Protocol):
    """Protocol for the live LLM scoring call. Implement at invocation time.

    A concrete implementation:
      - reads bidder proposal/bid-form PDFs (whichever the project supplies),
      - prompts at low temp (cfg.llm.temperature) with the §3.3 rubric anchors,
      - returns one CategoryScore per category with evidence_quotes, confidence,
        and evidence_status, NEVER inventing a score where evidence is absent
        (returns score=None + ANALYST_FLAG instead).

    The skill calls .score(bidder_name, category, document_text, anchors_cfg).
    """

    def score(self, bidder_name: str, category: str, document_text: str,
              anchors_cfg: Dict) -> CategoryScore: ...


class NotImplementedLLMScorer:
    """Default no-op scorer: returns absent (null+flag) for every category.

    This guarantees graceful degradation when no live LLM client is wired —
    the skill produces a PROVISIONAL scorecard with explicit coverage, never a
    hallucinated score. Replace with a real client to activate LLM drafting.
    """

    def score(self, bidder_name: str, category: str, document_text: str,
              anchors_cfg: Dict) -> CategoryScore:
        return absent_external(category) if category in EXTERNAL_CATEGORIES \
            else CategoryScore(category=category, score=None,
                               evidence_status="absent", flag=ANALYST_FLAG,
                               rationale="No live LLM scorer wired (stub).")
