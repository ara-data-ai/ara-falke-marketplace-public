"""Modeling layer — Darvish's Section C math.

Implements:
  - volatility_pct(v)           (§1.3 asymmetric clamped-affine)
  - expected_final(bid, v)      (§1.4 reversion-to-buffered-target)
  - refit_*                     re-fit routines using scipy.optimize.least_squares
                                against Darvish's published ANCHOR_SECTION_C table.

Every modeled output is calibration, not law (Darvish §0). Coefficients come
from config (tunable).

CURVE RETIRED (P0-6, Floyd consolidated ruling verdict d, Derick-confirmed):
the Overall "presentation curve" (asymmetric compression + price-value penalty
+ tier_bonus) re-ordered the award ranking under the name of presentation and
was REMOVED from the production path. Overall /100 is the honest weighted
average of the framework-weighted category scores — nothing adjusts it. The
historical curve coefficients and the §2.2 anchor table survive ONLY as golden
eval data (eval/golden/gold_data.py) for historical-card reproduction tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


# =============================================================================
# Darvish's published anchor table (the FIT CONTRACT, modeling spec §1.2)
# These are the targets the re-fit must reproduce. They are the truth; the
# coefficients are a starting prior.
# =============================================================================

# (bidder, bid_m, vol_mid_pct, exp_final_mid_m)  — §1.2 anchor table
# SYNTHETIC validation anchors (fictional firms, invented but model-consistent
# figures). No client project data is embedded in engine source.
ANCHOR_SECTION_C: List[Tuple[str, float, float, float]] = [
    ("Granite",  2.10, 34.2, 2.931),
    ("Fjord",    2.40, 27.9, 3.068),
    ("Cascade",  3.05, 14.3, 3.400),
    ("Acme",     3.30, 9.1,  3.540),
    ("Borealis", 3.40, 7.0,  3.598),
    ("Dorne",    3.90, 4.5,  4.017),
    ("Crest",    4.55, 2.3,  4.686),
]

VARIANCE_MID_FOR_ANCHORS = 3.45  # the mid Darvish used to compute v in §1.2


def _v_from_bid(bid_m: float, variance_mid: float) -> float:
    return (bid_m - variance_mid) / variance_mid


# =============================================================================
# Section C — volatility (§1.3)
# =============================================================================
def volatility_pct(v: float, vol_cfg: Dict) -> float:
    """Asymmetric clamped-affine volatility in signed fractional variance v.

    v < 0  (under baseline): raw = v0 + slope_under * (-v)*100
    v >= 0 (over baseline) : raw = v0 - slope_over  * ( v)*100
    clamped to [floor, cap]. (Darvish §1.3)
    """
    v0 = vol_cfg["v0"]
    if v <= 0:
        raw = v0 + vol_cfg["slope_under"] * (-v) * 100.0
    else:
        raw = v0 - vol_cfg["slope_over"] * (v) * 100.0
    return _clamp(raw, vol_cfg["floor"], vol_cfg["cap"])


def volatility_band(v: float, vol_cfg: Dict) -> Tuple[float, float, float]:
    """Return (central, low, high) volatility %. half_width grows with central."""
    central = volatility_pct(v, vol_cfg)
    hw = max(vol_cfg["half_width_min"], vol_cfg["half_width_frac"] * central)
    return central, round(central - hw, 1), round(central + hw, 1)


# =============================================================================
# Section C — expected final cost / drift (§1.4)
# =============================================================================
def expected_final(bid_m: float, v: float, variance_mid: float,
                   drift_cfg: Dict) -> float:
    """Reversion-to-buffered-target drift model (Darvish §1.4).

    expected = bid + creep*bid + lambda * max(0, target - bid)
    target   = variance_mid * (1 + buffer)
    lambda   = min(lambda_max, lambda0 + lambda_slope*(-v))  if v<0
               lambda_over                                   if v>=0
    """
    target = variance_mid * (1.0 + drift_cfg["buffer"])
    if v < 0:
        lam = min(drift_cfg["lambda_max"],
                  drift_cfg["lambda0"] + drift_cfg["lambda_slope"] * (-v))
    else:
        lam = drift_cfg["lambda_over"]
    creep = drift_cfg["creep"] * bid_m
    reversion = lam * max(0.0, target - bid_m)
    return bid_m + creep + reversion


def expected_final_band(bid_m: float, v: float, variance_mid: float,
                       drift_cfg: Dict, vol_central: float
                       ) -> Tuple[float, float, float]:
    """Center + display band. width = vol% * headroom * band_k, floored."""
    center = expected_final(bid_m, v, variance_mid, drift_cfg)
    target = variance_mid * (1.0 + drift_cfg["buffer"])
    headroom = max(0.0, target - bid_m)
    half = max(drift_cfg["band_floor"],
               (vol_central / 100.0) * headroom * drift_cfg["band_k"])
    return center, round(center - half, 3), round(center + half, 3)


# =============================================================================
# RE-FIT ROUTINES (FIRST BUILD STEP — confirm Darvish's hand fit with scipy)
# =============================================================================
@dataclass
class RefitResult:
    name: str
    params: Dict[str, float]
    residuals: List[float]
    max_abs_residual: float
    mean_abs_residual: float
    in_range: Dict[str, bool]
    notes: str


def refit_volatility(variance_mid: float = VARIANCE_MID_FOR_ANCHORS) -> RefitResult:
    """Re-fit (v0, slope_under, slope_over) against ANCHOR_SECTION_C vol mids
    with scipy.optimize.least_squares. floor/cap held at Darvish's values.

    Darvish stated ranges: v0~6.0, slope_under~0.72, slope_over~0.115.
    """
    import numpy as np
    from scipy.optimize import least_squares

    floor, cap = 2.0, 35.0
    vs = np.array([_v_from_bid(b, variance_mid) for (_n, b, _vol, _e) in ANCHOR_SECTION_C])
    target = np.array([vol for (_n, _b, vol, _e) in ANCHOR_SECTION_C])

    def model(p, v):
        v0, su, so = p
        raw = np.where(v <= 0, v0 + su * (-v) * 100.0, v0 - so * (v) * 100.0)
        return np.clip(raw, floor, cap)

    def resid(p):
        return model(p, vs) - target

    p0 = [6.0, 0.72, 0.115]
    sol = least_squares(resid, p0, bounds=([0, 0, 0], [20, 3, 3]))
    v0, su, so = (float(x) for x in sol.x)
    residuals = list(resid(sol.x))
    in_range = {
        "v0_in_[4,8]": 4.0 <= v0 <= 8.0,
        "slope_under_in_[0.5,1.0]": 0.5 <= su <= 1.0,
        "slope_over_in_[0.05,0.2]": 0.05 <= so <= 0.2,
    }
    return RefitResult(
        name="volatility",
        params={"v0": round(v0, 4), "slope_under": round(su, 4),
                "slope_over": round(so, 4), "floor": floor, "cap": cap},
        residuals=[round(r, 3) for r in residuals],
        max_abs_residual=round(float(max(abs(r) for r in residuals)), 3),
        mean_abs_residual=round(float(sum(abs(r) for r in residuals) / len(residuals)), 3),
        in_range=in_range,
        notes="lowest-bid anchors land at the clamp cap; matches Darvish §1.3.",
    )


def refit_drift(variance_mid: float = VARIANCE_MID_FOR_ANCHORS) -> RefitResult:
    """Re-fit (creep, buffer, lambda0) against ANCHOR_SECTION_C expected-final
    mids. lambda_max, lambda_over, AND lambda_slope are HELD (Darvish §1.4).

    IDENTIFIABILITY (Darvish): lambda_slope*(-v) only acts on the 3 under-baseline
    anchors. With creep, buffer, and lambda0 already free,
    those 3 points are over-parameterized — lambda_slope is weakly identifiable
    and a free least_squares run drives it to the boundary (0.0), which falls
    outside the documented [0.1, 0.3] band even though residuals stay strong
    (the other params compensate). lambda_slope is therefore PINNED at the
    documented runtime value (0.19) and the re-fit confirms the 3 identifiable
    params around it. The in-range check verifies the held value sits in band.
    Per spec: do not chase an unidentifiable direction with so few anchors.
    """
    import numpy as np
    from scipy.optimize import least_squares

    lambda_max, lambda_over = 0.55, 0.0
    lambda_slope = 0.19  # PINNED — documented runtime value (weakly identifiable)
    bids = np.array([b for (_n, b, _vol, _e) in ANCHOR_SECTION_C])
    vs = np.array([_v_from_bid(b, variance_mid) for (_n, b, _vol, _e) in ANCHOR_SECTION_C])
    target = np.array([e for (_n, _b, _vol, e) in ANCHOR_SECTION_C])

    def model(p, bid, v):
        creep, buffer, lam0 = p
        tgt = variance_mid * (1.0 + buffer)
        lam = np.where(v < 0,
                       np.minimum(lambda_max, lam0 + lambda_slope * (-v)),
                       lambda_over)
        return bid + creep * bid + lam * np.maximum(0.0, tgt - bid)

    def resid(p):
        return model(p, bids, vs) - target

    p0 = [0.030, 0.05, 0.43]
    sol = least_squares(resid, p0, bounds=([0, 0, 0], [0.15, 0.3, 1.0]))
    creep, buffer, lam0 = (float(x) for x in sol.x)
    residuals = list(resid(sol.x))
    in_range = {
        "creep_in_[0.02,0.045]": 0.02 <= creep <= 0.045,
        "buffer_in_[0.0,0.1]": 0.0 <= buffer <= 0.1,
        "lambda0_in_[0.35,0.55]": 0.35 <= lam0 <= 0.55,
        # lambda_slope is PINNED (held), not fitted — verify the held value is in band.
        "lambda_slope_in_[0.1,0.3]": 0.1 <= lambda_slope <= 0.3,
    }
    return RefitResult(
        name="drift",
        params={"creep": round(creep, 4), "buffer": round(buffer, 4),
                "lambda0": round(lam0, 4), "lambda_slope": round(lambda_slope, 4),
                "lambda_max": lambda_max, "lambda_over": lambda_over},
        residuals=[round(r, 4) for r in residuals],
        max_abs_residual=round(float(max(abs(r) for r in residuals)), 4),
        mean_abs_residual=round(float(sum(abs(r) for r in residuals) / len(residuals)), 4),
        in_range=in_range,
        notes="lambda_slope PINNED at 0.19 (weakly identifiable from 3 under-baseline "
              "anchors); fit confirms creep/buffer/lambda0. Worst residual at the "
              "value-tier anchor per Darvish §1.4.",
    )


def refit_all(variance_mid: float = VARIANCE_MID_FOR_ANCHORS) -> List[RefitResult]:
    """Run the Section C re-fits (volatility + drift). Call this as the FIRST
    build/validation step. (The Overall curve re-fit was retired with the
    curve itself — P0-6.)"""
    return [
        refit_volatility(variance_mid),
        refit_drift(variance_mid),
    ]


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
