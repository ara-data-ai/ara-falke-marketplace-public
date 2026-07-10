"""Qualitative scoring scaffold: degradation, override, no-invention."""
import pytest

from scorecard.config import load_config
from scorecard.scoring import (ANALYST_FLAG, EXTERNAL_CATEGORIES, apply_overrides,
                               build_bidder_scores, seed_co_risk, seed_pricing)


@pytest.fixture
def cfg():
    return load_config(overrides={"sf_basis": 16000, "band_low": 3.35,
                                  "band_high": 3.55, "modeled_mid_takeoff": 3.4})


def test_pricing_seed_from_tier(cfg):
    assert seed_pricing("TOP", cfg).score == 9
    assert seed_pricing("MID", cfg).score == 8
    assert seed_pricing("DEFENSIVE", cfg).score == 7
    assert seed_pricing("PREMIUM", cfg).score == 6
    assert seed_pricing("RISK", cfg).score == 4


def test_co_risk_seed_map(cfg):
    # Crest vol ~3 -> 9; Acme ~6.5 -> 8; Cascade ~12.9 -> 6; Fjord ~29 -> 3; Granite ~32 -> 2
    assert seed_co_risk(3.0, cfg).score == 9
    assert seed_co_risk(6.5, cfg).score == 8
    assert seed_co_risk(12.9, cfg).score == 6
    assert seed_co_risk(29.0, cfg).score == 3
    assert seed_co_risk(32.0, cfg).score == 2


def test_external_categories_are_null_not_invented(cfg):
    bs = build_bidder_scores(
        "TestCo", "TOP", 6.5, populated_divisions=10, peer_median=10,
        completeness_ratio=1.0, cfg=cfg)
    for cat in EXTERNAL_CATEGORIES:
        cs = bs.categories[cat]
        assert cs.score is None, f"{cat} must be null, never invented"
        assert cs.flag == ANALYST_FLAG
        assert cs.evidence_status == "absent"


def test_coverage_is_partial_without_external(cfg):
    bs = build_bidder_scores(
        "TestCo", "TOP", 6.5, populated_divisions=10, peer_median=10,
        completeness_ratio=1.0, cfg=cfg)
    wa = bs.weighted_average_x10(cfg.weights)
    # pricing 25 + co_risk 15 + scope 15 + docs 5 = 60% coverage
    assert wa["coverage"] == pytest.approx(0.60)
    assert wa["wavg"] is not None  # computed over scored cats only


def test_override_supersedes_and_logs(cfg):
    bs = build_bidder_scores(
        "TestCo", "TOP", 6.5, populated_divisions=10, peer_median=10,
        completeness_ratio=1.0, cfg=cfg)
    apply_overrides(bs, {"condo_exp": {"score": 9, "by": "Falke PM", "note": "3 named jobs"}})
    cs = bs.categories["condo_exp"]
    assert cs.effective_score == 9
    assert cs.source == "human_override"
    assert cs.override_by == "Falke PM"
