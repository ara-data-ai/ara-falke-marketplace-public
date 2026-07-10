"""Modeling tests: Section C reproduction, Overall curve, and the scipy re-fit.

The re-fit tests are the FIRST build step called out: confirm the curve
params land in Darvish's stated ranges and reproduce his anchor tables.
"""
import math

import pytest

from scorecard.modeling import (ANCHOR_OVERALL, ANCHOR_SECTION_C,
                                VARIANCE_MID_FOR_ANCHORS, expected_final,
                                overall_curve, refit_all, refit_drift,
                                refit_overall_curve, refit_volatility,
                                volatility_pct, _v_from_bid)


# ---- default config blocks mirroring scorecard_config.yaml ----
VOL = {"v0": 6.0, "slope_under": 0.72, "slope_over": 0.115, "floor": 2.0,
       "cap": 35.0, "half_width_min": 1.5, "half_width_frac": 0.18}
DRIFT = {"creep": 0.030, "buffer": 0.05, "lambda0": 0.43, "lambda_slope": 0.19,
         "lambda_max": 0.55, "lambda_over": 0.0, "band_k": 0.5, "band_floor": 0.05}
CURVE = {"anchor": 70, "k_low": 0.62, "k_high": 1.18, "pen_coef": 0.39,
         "premium_floor": 234}


# ===========================================================================
# RE-FIT (scipy.optimize.least_squares) vs Darvish's published ranges
# ===========================================================================
def test_refit_volatility_in_range():
    rr = refit_volatility(VARIANCE_MID_FOR_ANCHORS)
    assert all(rr.in_range.values()), rr.in_range
    # the lowest-bid anchors sit at the cap; max residual must be modest
    assert rr.max_abs_residual < 6.0, rr.residuals


def test_refit_drift_in_range_and_strong():
    rr = refit_drift(VARIANCE_MID_FOR_ANCHORS)
    assert all(rr.in_range.values()), rr.in_range
    # Darvish: worst residual at the value-tier anchor; allow a little slack
    assert rr.max_abs_residual < 0.08, rr.residuals
    assert rr.mean_abs_residual < 0.03, rr.residuals


def test_refit_overall_curve_in_range():
    rr = refit_overall_curve()
    assert all(rr.in_range.values()), rr.in_range
    # 6 of 7 within +/-2.5. The value-tier anchor (Cascade, ~-5) sits off the
    # fitted curve by design: it carries the bespoke Value-Tier +5 promotion and
    # is EXCLUDED from the fit (Darvish §2.5); its residual is reported, not
    # chased. This matches what the documented runtime curve produces; the
    # gold-card path (test_overall_curve_reproduces_6_of_7) is unaffected.
    within = sum(1 for r in rr.residuals if abs(r) <= 2.5)
    assert within >= 6, rr.residuals


def test_refit_all_runs():
    results = refit_all()
    assert {r.name for r in results} == {"volatility", "drift", "overall_curve"}


# ===========================================================================
# Section C reproduction with the DEFAULT (Darvish hand-fit) params
# ===========================================================================
def test_volatility_reproduces_anchor_bands():
    """5 of 7 model points land inside Darvish's printed band (§1.3)."""
    inside = 0
    for (_n, bid, vol_mid, _e) in ANCHOR_SECTION_C:
        v = _v_from_bid(bid, VARIANCE_MID_FOR_ANCHORS)
        model = volatility_pct(v, VOL)
        if abs(model - vol_mid) <= 5.0:
            inside += 1
    assert inside >= 5


def test_volatility_preserves_lower_over_bid_gt_higher_over_bid_ordering():
    """Asymmetric over-baseline slope must keep the lower over-band bid's
    volatility above the higher over-band bid's (Dorne > Crest) (§1.3)."""
    v_dorne = _v_from_bid(3.90, VARIANCE_MID_FOR_ANCHORS)
    v_crest = _v_from_bid(4.55, VARIANCE_MID_FOR_ANCHORS)
    assert volatility_pct(v_dorne, VOL) > volatility_pct(v_crest, VOL)


def test_expected_final_strong_fit():
    """Max residual vs Darvish printed mids < $0.06M (§1.4 verdict: strong)."""
    worst = 0.0
    for (_n, bid, _vol, exp_mid) in ANCHOR_SECTION_C:
        v = _v_from_bid(bid, VARIANCE_MID_FOR_ANCHORS)
        model = expected_final(bid, v, VARIANCE_MID_FOR_ANCHORS, DRIFT)
        worst = max(worst, abs(model - exp_mid))
    assert worst < 0.06, worst


# ===========================================================================
# Overall curve reproduction with DEFAULT params (§2.5)
# ===========================================================================
def test_overall_curve_reproduces_6_of_7():
    within2 = 0
    for (firm, wavg, card, psf) in ANCHOR_OVERALL:
        model = round(max(0, min(100, overall_curve(wavg, psf, CURVE))))
        if abs(model - card) <= 2:
            within2 += 1
    assert within2 >= 6


def test_overall_curve_premium_penalty_applies():
    """A premium bidder (Crest, 252 $/SF) gets the price-value penalty -> 65 (§2.5)."""
    model = round(overall_curve(72.0, 252, CURVE))
    assert model == 65


def test_overall_curve_defensive_no_penalty_deadband():
    """A defensive bidder (Dorne, 215 $/SF < 234 premium floor) gets NO penalty
    (§2.4 deadband)."""
    # 70 + 0.62*(69.0-70) = 69.38 -> 69, no penalty
    model = round(overall_curve(69.0, 215, CURVE))
    assert model == 69
