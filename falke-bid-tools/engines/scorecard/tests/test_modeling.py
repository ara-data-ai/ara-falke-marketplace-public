"""Modeling tests: Section C reproduction and the scipy re-fit.

The re-fit tests are the FIRST build step called out: confirm the Section C
params land in Darvish's stated ranges and reproduce his anchor table.

CURVE RETIRED (P0-6, Floyd consolidated ruling verdict d): the Overall
presentation-curve tests that lived here (test_refit_overall_curve_in_range,
test_overall_curve_reproduces_6_of_7, test_overall_curve_premium_penalty_
applies, test_overall_curve_defensive_no_penalty_deadband) are RETIRED with
the curve itself. Overall is the honest weighted average; historical curve
reproduction survives only in the local-only golden eval
(eval/golden/test_gold_modeling.py, against frozen coefficients).
"""
import math

import pytest

from scorecard.modeling import (ANCHOR_SECTION_C, VARIANCE_MID_FOR_ANCHORS,
                                expected_final, refit_all, refit_drift,
                                refit_volatility, volatility_pct, _v_from_bid)


# ---- default config blocks mirroring scorecard_config.yaml ----
VOL = {"v0": 6.0, "slope_under": 0.72, "slope_over": 0.115, "floor": 2.0,
       "cap": 35.0, "half_width_min": 1.5, "half_width_frac": 0.18}
DRIFT = {"creep": 0.030, "buffer": 0.05, "lambda0": 0.43, "lambda_slope": 0.19,
         "lambda_max": 0.55, "lambda_over": 0.0, "band_k": 0.5, "band_floor": 0.05}


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


def test_refit_all_runs():
    """Section C re-fits only — the Overall curve re-fit is retired (P0-6)."""
    results = refit_all()
    assert {r.name for r in results} == {"volatility", "drift"}


def test_curve_is_retired_from_the_engine():
    """P0-6 guard: no Overall presentation curve survives in the modeling
    layer — nothing in the production path can adjust the ranked number."""
    import scorecard.modeling as modeling
    for gone in ("overall_curve", "apply_overall", "refit_overall_curve",
                 "ANCHOR_OVERALL"):
        assert not hasattr(modeling, gone), gone


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
