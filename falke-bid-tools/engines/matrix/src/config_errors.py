"""Typed errors for the matrix config foundation.

Mirrors the scorecard engine's errors.py idiom: every failure mode is an
explicit, named exception with an actionable message (not a stack trace). NO
silent fallbacks. A malformed config or a missing/unconfirmed required parameter
STOPS the run rather than guessing (owner's decision, scoping §1.3).
"""
from __future__ import annotations


class MatrixConfigError(Exception):
    """Base class for all matrix config-foundation errors."""


class KnownFirmsConfigError(MatrixConfigError):
    """known_firms.yaml is malformed or fails schema validation.

    Raised (with a clear, human-readable message) on: a reclass `from`/`to`
    division not in CANONICAL_DIVISIONS; an empty `match` list; an empty
    `when_description_contains_all` keyword list; `from == to`; a 2-rule cycle
    within one firm; or a `to` that would fabricate a non-canonical division.
    """


class MissingParameterError(MatrixConfigError):
    """A required external PARAMETER was not supplied.

    Raised for the project-identity fields (project_name, project_address,
    gross_sf) and an unresolved sf_basis. The run STOPS and asks, never guesses
    (scoping §1.3 — SF basis is a fiduciary decision, not a fact to scrape).
    """
