"""Typed errors for the scorecard skill.

DataOps discipline: every failure mode is an explicit, named exception with an
actionable message. NO silent fallbacks anywhere in the pipeline. A missing
required PARAMETER raises MissingParameterError and STOPS the run (Marvin §3,
§10; owner's decision: "if a parameter is missing, STOP and ask, don't guess").
"""


class ScorecardError(Exception):
    """Base class for all scorecard skill errors."""


class MissingParameterError(ScorecardError):
    """A required external PARAMETER was not supplied.

    Raised for sf_basis / baseline band absence. The skill must STOP and ask,
    never guess or default to the matrix GSF (Marvin §3).
    """


class MatrixStructureError(ScorecardError):
    """The matrix did not match the expected structural contract.

    Raised when bidder-block detection, the grand-total row, or sub-header
    quartet cannot be located (Marvin §0/§1).
    """


class GrandTotalNotFoundError(MatrixStructureError):
    """Could not locate the compared-total row.

    Neither 'GRAND TOTAL CONSTRUCTION COST' nor a safe fallback total row below
    the markup adders was found (Marvin §1.2).
    """


class ConfigError(ScorecardError):
    """The config block is malformed (e.g. weights do not sum to 1.0)."""


class CoverageError(ScorecardError):
    """The Overall presentation curve was requested without 100% qualitative
    coverage. The curve is calibrated on full 8-category averages and MUST NOT
    be applied to a provisional/partial score (Darvish §3.4)."""


class RenderError(ScorecardError):
    """PDF/HTML rendering failed (no engine available, template error)."""
