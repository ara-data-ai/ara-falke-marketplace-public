"""Per-run project-identity config + the SF-basis confirm/override gate.

Foundation for matrix M1/M2 (scoping §1.2–§1.4). Mirrors the scorecard engine's
config idiom: a small per-project YAML (`project.json`/`.yaml`) is authoritative,
loaded with ``yaml.safe_load`` into a validated ``RunInputs`` dataclass whose
``validate()`` hard-stops (no silent defaults) on a missing required identity
field or an unresolved SF basis.

The SF basis is a FIDUCIARY decision (§1.4): a wrong GSF silently corrupts every
$/SF cell. So this module supplies the SAME suggest-and-confirm gate the
scorecard uses — ``resolve_sf_basis()`` returns (value, source) or signals the
caller to hard-stop (exit 2):
  * explicit --sf-basis        -> ('explicit')           used as-is, no prompt;
  * --sf-confirmed (no basis)  -> ('matrix-confirmed')   accept the extracted GSF;
  * neither, on a render       -> STOP (exit 2) naming the extracted SF.
``sf_source`` is carried on RunInputs so the audit can tell a confirmed-matrix SF
apart from an explicit one.

Christine (M1/M2) consumes ``load_run_config`` + ``RunInputs`` and wires
``resolve_sf_basis`` into the create-matrix CLI gate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

import yaml

from src.config_errors import MatrixConfigError, MissingParameterError

# Sentinel return for the gate: caller must hard-stop (exit 2). Kept distinct
# from a resolved (value, source) tuple so the CLI branch is unambiguous.
SF_GATE_STOP = "STOP"


@dataclass
class RunInputs:
    """The five-field project identity for one matrix run (§1.2).

    project_name / project_address / gross_sf are REQUIRED (hard stop if None).
    gross_sf is the $/SF denominator — the one identity field that drives
    leveling math. sf_basis_label is recommended (printed next to the $/SF
    header so the board knows what the denominator means, §1.4). rfp_label is an
    optional provenance stamp.
    """

    project_name: Optional[str]
    project_address: Optional[str]
    gross_sf: Optional[float]
    sf_basis_label: Optional[str] = None
    rfp_label: Optional[str] = None
    # how gross_sf was resolved by the CLI gate: 'explicit' (user --sf-basis) or
    # 'matrix-confirmed' (user accepted the extracted GSF via --sf-confirmed).
    # None for direct/programmatic callers (treated as explicit). Informational —
    # surfaced in the audit so a confirmed-matrix SF is auditable.
    sf_source: Optional[str] = None

    def validate(self) -> None:
        """Hard stop on any missing required field. NO silent defaults (§1.3)."""
        missing = []
        if not (self.project_name and str(self.project_name).strip()):
            missing.append(
                "project_name — board title + per-contractor label rows."
            )
        if not (self.project_address and str(self.project_address).strip()):
            missing.append("project_address — details line (display only).")
        if self.gross_sf is None:
            missing.append(
                "gross_sf ($/SF basis). Must be RESOLVED before validation — "
                "supply --sf-basis (override) or pass --sf-confirmed to accept "
                "the extracted GSF (see resolve_sf_basis)."
            )
        if missing:
            raise MissingParameterError(
                "Required project-identity field(s) not supplied — the matrix "
                "STOPS rather than guessing (owner's decision, §1.3):\n  - "
                + "\n  - ".join(missing)
            )
        if self.gross_sf <= 0:
            raise MatrixConfigError(
                f"gross_sf must be positive, got {self.gross_sf!r}."
            )


def load_run_config(
    config_path: Optional[str] = None,
    *,
    overrides: Optional[dict] = None,
    validate: bool = True,
) -> RunInputs:
    """Load a per-project identity YAML into a (optionally validated) RunInputs.

    overrides (e.g. from CLI flags / the resolved SF gate) win over file values.
    validate=False loads WITHOUT the hard stops (so a caller can read the file
    before the SF basis is resolved); the full run path validates.
    """
    raw: dict = {}
    if config_path:
        if not os.path.exists(config_path):
            raise MatrixConfigError(f"project config not found: {config_path}")
        with open(config_path, "r", encoding="utf-8") as fh:
            try:
                loaded = yaml.safe_load(fh)
            except yaml.YAMLError as e:
                raise MatrixConfigError(f"project config is not valid YAML: {e}")
        raw = dict(loaded or {})

    if overrides:
        for k in ("project_name", "project_address", "gross_sf",
                  "sf_basis_label", "rfp_label", "sf_source"):
            if k in overrides and overrides[k] is not None:
                raw[k] = overrides[k]

    ri = RunInputs(
        project_name=raw.get("project_name"),
        project_address=raw.get("project_address"),
        gross_sf=raw.get("gross_sf"),
        sf_basis_label=raw.get("sf_basis_label"),
        rfp_label=raw.get("rfp_label"),
        sf_source=raw.get("sf_source"),
    )
    if validate:
        ri.validate()
    return ri


def resolve_sf_basis(
    sf_basis: Optional[float],
    sf_confirmed: bool,
    extracted_gsf: Optional[float],
) -> Tuple:
    """SF-basis suggest-and-confirm gate (mirrors the scorecard CLI gate, M2).

    Returns either ``(value, source)`` on success or ``(SF_GATE_STOP, message)``
    when the caller must hard-stop (exit 2). The matrix never computes $/SF
    against an unconfirmed denominator.

      * sf_basis given            -> (sf_basis, 'explicit');
      * sf_confirmed, gsf present -> (extracted_gsf, 'matrix-confirmed');
      * sf_confirmed, no gsf      -> STOP (nothing to confirm);
      * neither                   -> STOP (suggest the extracted GSF).
    """
    if sf_basis is not None:
        return (sf_basis, "explicit")
    if sf_confirmed:
        if extracted_gsf is None:
            return (SF_GATE_STOP,
                    "--sf-confirmed given but no GSF was extracted to confirm — "
                    "supply --sf-basis <value> explicitly.")
        return (extracted_gsf, "matrix-confirmed")
    # neither an explicit basis nor confirmation on a render -> suggest + stop.
    if extracted_gsf is None:
        return (SF_GATE_STOP,
                "SF basis not confirmed and no GSF was extracted to suggest — "
                "re-run with --sf-basis <value> to set it explicitly.")
    return (SF_GATE_STOP,
            f"SF basis not confirmed — the bids report {extracted_gsf:,.0f} SF; "
            f"re-run with --sf-basis <value> to override, or --sf-confirmed to "
            f"accept it.")
