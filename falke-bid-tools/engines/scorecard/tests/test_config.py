"""Hard-stop parameter validation + config integrity."""
import pytest

from scorecard.config import RunInputs, load_config
from scorecard.errors import ConfigError, MissingParameterError


def test_missing_sf_basis_hard_stops():
    ri = RunInputs(sf_basis=None, modeled_mid_takeoff=3.4, band_low=3.35, band_high=3.55)
    with pytest.raises(MissingParameterError) as ei:
        ri.validate()
    assert "sf_basis" in str(ei.value)
    # critical: must NOT mention or use the matrix GSF as a fallback
    assert "12000" not in str(ei.value).replace(",", "")


def test_missing_band_hard_stops():
    ri = RunInputs(sf_basis=16000, modeled_mid_takeoff=3.4, band_low=None, band_high=3.55)
    with pytest.raises(MissingParameterError):
        ri.validate()


def test_variance_mid_defaults_to_band_center():
    ri = RunInputs(sf_basis=16000, modeled_mid_takeoff=3.4, band_low=3.35, band_high=3.55)
    ri.validate()
    assert ri.variance_mid == pytest.approx(3.45)


def test_band_order_enforced():
    ri = RunInputs(sf_basis=16000, modeled_mid_takeoff=3.4, band_low=3.6, band_high=3.55)
    with pytest.raises(ConfigError):
        ri.validate()


def test_weights_sum_to_one():
    cfg = load_config(overrides={"sf_basis": 16000, "band_low": 3.35,
                                 "band_high": 3.55, "modeled_mid_takeoff": 3.4})
    assert abs(sum(cfg.weights.values()) - 1.0) < 1e-9


def test_band_per_sf_uses_parameter_not_gsf():
    cfg = load_config(overrides={"sf_basis": 16000, "band_low": 3.35,
                                 "band_high": 3.55, "modeled_mid_takeoff": 3.4})
    # 3.35M / 16000 ~ 209, 3.55M / 16000 ~ 222 (Marvin §2.3)
    assert round(cfg.run.band_low_per_sf) == 209
    assert round(cfg.run.band_high_per_sf) == 222
