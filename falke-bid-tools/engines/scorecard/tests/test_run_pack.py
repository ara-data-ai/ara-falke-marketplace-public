"""Unit tests for the run-pack schema (P1-4).

The pack's END-TO-END behavior is covered by the LIVE cross-engine gate in
test_producer_live_compat.py, where a real pack is emitted by the in-tree matrix
engine and consumed by this scorecard — that is the P0-2 lesson and it is where
the seam is actually proven. What lives HERE is only what does not need the
producer: the semantic-hash contract and the schema's own invariants.
"""
from __future__ import annotations

import os

import pytest

from scorecard import pack_schema as ps
from scorecard.scoring_inputs import parse_scoring_framework

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK_TEMPLATE = os.path.join(
    HERE, "..", "templates", "scoring-framework-template.xlsx")


# ---------------------------------------------------------------------------
# The semantic hash (§4.3)
# ---------------------------------------------------------------------------
def test_hash_ignores_row_order():
    """Row order is not the plan."""
    rows = [("Pricing", 25), ("Scope", 75)]
    assert (ps.framework_semantic_hash(rows)
            == ps.framework_semantic_hash(list(reversed(rows))))


def test_hash_ignores_label_case_and_spacing():
    assert (ps.framework_semantic_hash([("Condo Exp", 15)])
            == ps.framework_semantic_hash([("  condo   exp ", 15)]))


def test_hash_ignores_weight_typing():
    """An Excel cell may hand back 25, 25.0 or a float that prints long. None
    of those is a change to Falke's evaluation policy."""
    assert (ps.framework_semantic_hash([("Pricing", 25)])
            == ps.framework_semantic_hash([("Pricing", 25.0)]))


def test_hash_ignores_descriptions():
    """Descriptions are prose; categories and weights are the plan. If rewording
    a 'What it captures' cell fired a fiduciary alarm, the alarm would cry wolf
    — and an alarm that cries wolf is worse than no alarm."""
    a = ps.framework_semantic_hash([("Pricing", 25, "one description")])
    b = ps.framework_semantic_hash([("Pricing", 25, "a totally different one")])
    assert a == b


def test_hash_changes_when_a_weight_changes():
    assert (ps.framework_semantic_hash([("Pricing", 25), ("Scope", 75)])
            != ps.framework_semantic_hash([("Pricing", 30), ("Scope", 70)]))


def test_hash_changes_when_a_category_changes():
    assert (ps.framework_semantic_hash([("Pricing", 100)])
            != ps.framework_semantic_hash([("Schedule", 100)]))


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------
def test_shipped_default_matches_the_shipped_template():
    """DEFAULT_FRAMEWORK_ROWS is a copy of the shipped
    scoring-framework-template.xlsx rows, kept in the stdlib-only schema module
    so the matrix emitter can pre-fill the Framework tab without importing
    openpyxl-shaped scorecard code. A copy can drift; this is the tripwire.

    Note what these rows are NOT: a standing framework. They are ARA's shipped
    DEFAULT starting content (§10.2). Measuring policy drift against them would
    be the tool asserting a fact it does not know.
    """
    template = parse_scoring_framework(FRAMEWORK_TEMPLATE)
    assert [(r["category"], r["short_label"], r["weight"], r["description"])
            for r in template] == [
        (c, l, float(w), d) for c, l, w, d in ps.DEFAULT_FRAMEWORK_ROWS]


def test_shipped_default_weights_sum_to_100():
    assert sum(w for _c, _l, w, _d in ps.DEFAULT_FRAMEWORK_ROWS) == 100


def test_schema_contains_no_confirmation_field():
    """R1, enforced as a test because it is the single most important line in
    the ruling. No cell in the pack may satisfy a gate — so no confirmation
    field may exist to be auto-satisfied, hand-added, or quietly introduced by a
    maintainer chasing convenience.

    If this test fails, do not update the test. Delete the field.
    """
    every_label = " ".join(
        ps.SETTINGS_SCALAR_FIELDS + ps.BASELINE_SCALAR_FIELDS
        + ps.FRAMEWORK_SCALAR_FIELDS + ps.SCORES_SCALAR_FIELDS
        + tuple(label for label, _h in ps.SETTINGS_TABLE_BLOCKS)
    ).lower()
    for forbidden in ("confirm", "sf_confirmed", "baseline_confirmed",
                      "audit", "skip"):
        assert forbidden not in every_label, (
            f"'{forbidden}' appears in the pack schema — see pack_schema.R1")


def test_schema_carries_no_session_state():
    """R7 + §7.2. The pack is an award-file artifact read years later by people
    who were not in the room. Everything in it is a durable fact about the
    EVALUATION; nothing in it is a fact about the SESSION.

    `sheet` is the one Marvin is firmest on: a `sheet` cell would let a pack
    circulate with Bid_Form baked in and produce an un-leveled board card
    without anyone typing a flag — a gate bypass by data. Sheet selection stays
    a CLI flag, only.
    """
    every_label = " ".join(
        ps.SETTINGS_SCALAR_FIELDS + tuple(l for l, _h in ps.SETTINGS_TABLE_BLOCKS)
    ).lower()
    for forbidden in ("sheet", "out-dir", "out dir", "save", "email",
                      "recipient", "curve", "path"):
        assert forbidden not in every_label, (
            f"'{forbidden}' appears in the Settings schema — see pack_schema.R7")


def test_band_lives_only_on_baseline():
    """R6, one home per fact. Two homes for one fact is how the --overrides and
    band-override hazards happened."""
    assert all("band" not in ps.norm_label(f) for f in ps.SETTINGS_SCALAR_FIELDS)
    assert all(f in ps.BASELINE_SCALAR_FIELDS for f in ps.BASELINE_BAND_FIELDS)


def test_pack_has_exactly_four_tabs():
    """§11 — the pack's tab list is FOUR. The qualification register and the
    alternates/allowances register (P2-7) are not ride-alongs; when they are
    greenlit, R2's label addressing makes a fifth tab additive."""
    assert ps.PACK_SHEETS == ("Settings", "Baseline", "Framework", "Scores")


@pytest.mark.parametrize("name,expected", [
    ("Harborview Tower", "Harborview Tower - Scorecard Inputs.xlsx"),
    ("A/B Tower: Phase 1", "A-B Tower- Phase 1 - Scorecard Inputs.xlsx"),
])
def test_pack_filename_survives_path_hostile_characters(name, expected):
    assert ps.pack_filename(name) == expected
