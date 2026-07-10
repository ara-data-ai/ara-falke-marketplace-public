"""Config loading + hard-stop parameter validation.

The baseline band PARAMETER is validated here (hard stop if absent). The SF
basis is handled as a SUGGEST-AND-CONFIRM gate at the CLI layer (owner's relaxed
decision): the CLI reads the matrix's own Row-10 GSF and offers it as a
SUGGESTED default, but a render still REQUIRES the user to either supply an
explicit --sf-basis (override) OR pass --sf-confirmed to accept the matrix GSF.
By the time a Config is validated, sf_basis must be RESOLVED (the CLI injects
either the explicit value or, on confirmation, the matrix GSF) — config still
hard-stops if it is missing, so $/SF is never computed without a value, and the
matrix GSF is never silently adopted without that explicit confirmation.
``sf_source`` records which path produced the value ('explicit' |
'matrix-confirmed') so the audit (C3) can tell a confirmed-matrix SF apart from
an accidental GSF conflation.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import yaml

from .errors import ConfigError, MissingParameterError

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "scorecard_config.yaml",
)


@dataclass
class RunInputs:
    """The per-run external PARAMETERS. None means 'not supplied' -> hard stop."""

    sf_basis: Optional[float]
    modeled_mid_takeoff: Optional[float]
    band_low: Optional[float]
    band_high: Optional[float]
    variance_mid: Optional[float] = None  # default derived = band center
    # PRESENTATION labels (NOT modeled values). Defaults to a generic South FL
    # region label out of the box; override per project. region_full defaults to
    # region when only the short label is supplied; pricing_year defaults to the
    # current year (NEVER a hardcoded year).
    region: Optional[str] = None          # short label, e.g. "South FL"
    region_full: Optional[str] = None     # long label, e.g. "South Florida"
    pricing_year: Optional[int] = None    # baseline/pricing year
    # how sf_basis was resolved by the CLI gate: 'explicit' (user --sf-basis) or
    # 'matrix-confirmed' (user accepted the matrix Row-10 GSF via --sf-confirmed).
    # None for direct/programmatic callers (treated as explicit). Informational —
    # surfaced in the footer/audit so a confirmed-matrix SF is auditable.
    sf_source: Optional[str] = None

    def validate(self) -> None:
        """Hard stop on any missing required parameter. NO silent defaults."""
        missing = []
        if self.sf_basis is None:
            missing.append(
                "sf_basis ($/SF area basis). Must be RESOLVED before validation — "
                "supply --sf-basis (override), pass --sf-confirmed to accept the "
                "matrix Row-10 GSF, or set run_inputs.sf_basis in a --config file."
            )
        if self.modeled_mid_takeoff is None:
            missing.append("modeled_mid_takeoff ($M) — Section A modeled mid (Marvin §2).")
        if self.band_low is None:
            missing.append("band_low ($M) — modeled baseline band lower bound.")
        if self.band_high is None:
            missing.append("band_high ($M) — modeled baseline band upper bound.")
        if missing:
            raise MissingParameterError(
                "Required external PARAMETER(s) not supplied — the skill STOPS "
                "rather than guessing (owner's decision):\n  - "
                + "\n  - ".join(missing)
            )
        if self.sf_basis <= 0:
            raise ConfigError(f"sf_basis must be positive, got {self.sf_basis!r}.")
        if not (self.band_low < self.band_high):
            raise ConfigError(
                f"band_low ({self.band_low}) must be < band_high ({self.band_high})."
            )
        # Derive canonical Section C mid = band center if not explicitly supplied.
        if self.variance_mid is None:
            self.variance_mid = round((self.band_low + self.band_high) / 2.0, 4)
        # Presentation-label defaults (project-agnostic generic region labels out
        # of the box). These are NOT hard-stop parameters.
        if self.region is None:
            self.region = "South FL"
        if self.region_full is None:
            # default the long label to the short one when only region is given
            self.region_full = "South Florida" if self.region == "South FL" else self.region
        if self.pricing_year is None:
            self.pricing_year = _dt.datetime.now().year

    @property
    def band_low_per_sf(self) -> float:
        return self.band_low * 1e6 / self.sf_basis

    @property
    def band_high_per_sf(self) -> float:
        return self.band_high * 1e6 / self.sf_basis

    @property
    def mid_per_sf(self) -> float:
        return self.modeled_mid_takeoff * 1e6 / self.sf_basis


@dataclass
class Config:
    """Full resolved config: run inputs + all modeling/QA parameters."""

    run: RunInputs
    raw: Dict[str, Any] = field(default_factory=dict)

    # convenience accessors into raw blocks
    def block(self, name: str) -> Dict[str, Any]:
        return self.raw.get(name, {})

    @property
    def weights(self) -> Dict[str, float]:
        return self.raw["weights"]

    def validate(self) -> None:
        self.run.validate()
        w = self.weights
        total = sum(w.values())
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ConfigError(
                f"Scoring weights must sum to 1.0; got {total} from {w}."
            )


def load_config(
    config_path: Optional[str] = None,
    *,
    overrides: Optional[Dict[str, Any]] = None,
    validate: bool = True,
) -> Config:
    """Load YAML config and apply optional run-input overrides.

    overrides may contain keys: sf_basis, modeled_mid_takeoff, band_low,
    band_high, variance_mid (e.g. from CLI flags). Anything supplied here wins
    over the YAML `run_inputs` block.

    validate=False loads the config WITHOUT the run-input hard stops (so callers
    that only need the static blocks — e.g. the CLI's SF gate reading the
    matrix config block to detect the Row-10 GSF — can do so before sf_basis is
    resolved). The full run path always validates (validate=True).
    """
    path = config_path or DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        raise ConfigError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    ri = dict(raw.get("run_inputs", {}))
    if overrides:
        for k in ("sf_basis", "modeled_mid_takeoff", "band_low", "band_high",
                  "variance_mid", "region", "region_full", "pricing_year",
                  "sf_source"):
            if k in overrides and overrides[k] is not None:
                ri[k] = overrides[k]

    run = RunInputs(
        sf_basis=ri.get("sf_basis"),
        modeled_mid_takeoff=ri.get("modeled_mid_takeoff"),
        band_low=ri.get("band_low"),
        band_high=ri.get("band_high"),
        variance_mid=ri.get("variance_mid"),
        region=ri.get("region"),
        region_full=ri.get("region_full"),
        pricing_year=ri.get("pricing_year"),
        sf_source=ri.get("sf_source"),
    )
    cfg = Config(run=run, raw=raw)
    if validate:
        cfg.validate()
    return cfg
