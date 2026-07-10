"""MECHANICAL layer unit tests: $/SF, tiers, variance (no matrix needed)."""
import pytest

from scorecard.config import load_config
from scorecard.matrix import (SUB_COST, SUB_COST_SUBTOTAL, SUB_PSF,
                              SUB_PSF_SUBTOTAL, BidderBlock, ParsedMatrix,
                              apply_exclusions, classify_subheader,
                              normalize_name)
from scorecard.mechanical import (TIER_DEFENSIVE, TIER_MID, TIER_PREMIUM,
                                  TIER_RISK, TIER_TOP, assign_tier,
                                  compute_per_sf)
from .conftest import BAND_HIGH, BAND_LOW, GOLD_PER_SF, GOLD_TIERS, GOLD_TOTALS, MID, SF_BASIS


@pytest.fixture
def cfg():
    return load_config(overrides={
        "sf_basis": SF_BASIS, "band_low": BAND_LOW, "band_high": BAND_HIGH,
        "modeled_mid_takeoff": MID})


def test_per_sf_reproduces_gold_for_all_7():
    """Every $/SF must equal round(grand-total / SF basis) and match the card."""
    for name, total in GOLD_TOTALS.items():
        assert compute_per_sf(total, SF_BASIS) == GOLD_PER_SF[name], name


def test_per_sf_never_uses_matrix_gsf():
    # if someone wrongly used the matrix GSF (12000), Crest would be ~367 not 275
    assert compute_per_sf(GOLD_TOTALS["Crest"], 12000) != GOLD_PER_SF["Crest"]
    assert compute_per_sf(GOLD_TOTALS["Crest"], SF_BASIS) == 275


def test_tiers_reproduce_gold_for_all_7(cfg):
    for name, total in GOLD_TOTALS.items():
        per_sf = compute_per_sf(total, SF_BASIS)
        assert assign_tier(per_sf, cfg) == GOLD_TIERS[name], name


def test_tier_boundaries(cfg):
    # band ~209-221; MID floor 0.9*209.4~188.4; PREMIUM floor 1.2*221.9=266.25
    assert assign_tier(120, cfg) == TIER_RISK       # Granite (far below)
    assert assign_tier(170, cfg) == TIER_RISK       # below MID floor
    assert assign_tier(191, cfg) == TIER_MID        # Cascade
    # band_low_per_sf = 3.35e6/16000 = 209.38; the card PRINTS in-band bidders at
    # $209/SF and labels them TOP. The integer 209 must land in TOP, not slip to
    # MID on the fractional gap. Regression for Defect 2.
    assert assign_tier(209, cfg) == TIER_TOP        # band edge (displayed int)
    assert assign_tier(221, cfg) == TIER_TOP        # band edge
    assert assign_tier(230, cfg) == TIER_DEFENSIVE  # Dorne
    assert assign_tier(266, cfg) == TIER_DEFENSIVE  # at premium floor (inclusive defensive)
    assert assign_tier(275, cfg) == TIER_PREMIUM    # Crest


def test_classify_subheader_tolerates_newline_headers():
    """Regression (Defect: false 0/20 root cause). Some source matrices carry
    the two-word sub-headers with IN-CELL NEWLINES and a source typo
    ('COST \\nSUBTOTALS', '$/SXFX \\nSUBTOTALS'). classify_subheader must still
    bucket these so the per-block COST_SUBTOTALS column (e.g. Crest col F, Dorne
    col K) resolves — runs unconditionally (no client xlsx needed)."""
    assert classify_subheader("COST") == SUB_COST
    assert classify_subheader("COST \nSUBTOTALS") == SUB_COST_SUBTOTAL
    assert classify_subheader("COST  SUBTOTALS") == SUB_COST_SUBTOTAL  # double-space
    assert classify_subheader("$/SF") == SUB_PSF
    assert classify_subheader("$/SXFX \nSUBTOTALS") == SUB_PSF_SUBTOTAL
    assert classify_subheader("$/SF SUBTOTALS") == SUB_PSF_SUBTOTAL
    assert classify_subheader("Acme") is None
    assert classify_subheader(None) is None


def _block(raw, col):
    return BidderBlock(raw_name=raw, name=raw, norm=normalize_name(raw),
                       start_col=col, cols={SUB_COST: col}, grand_total=1.0)


def _parsed(blocks):
    return ParsedMatrix(
        sheet_name="Bid_Form", header_row=8, block_width=4, block_stride=5,
        grand_total_row=164, grand_total_label="GRAND TOTAL", gsf_value=None,
        gsf_row=None, blocks=blocks, division_rows=[(20, "01")])


def test_apply_exclusions_default_includes_all():
    """Item 3 regression: with NO ruling supplied the field is unchanged
    (include-all-and-flag default preserved)."""
    p = _parsed([_block("Acme", 25), _block("Harbor Builders", 40)])
    log = apply_exclusions(p, None)
    assert log == []
    assert {b.name for b in p.included_blocks} == {"Acme", "Harbor Builders"}


def test_apply_exclusions_applies_ruling_and_logs():
    """Ruling removes matched bidders (suffix-tolerant: 'Harbor Builders Inc.'
    matches block 'Harbor Builders'), logs each, and a no-op name is surfaced."""
    p = _parsed([_block("Acme", 25), _block("Harbor Builders", 40),
                 _block("Borealis Builders Solutions", 45)])
    log = apply_exclusions(
        p, ["Harbor Builders Inc.", "Borealis Builders Solutions", "Nonexistent Co"])
    included = {b.name for b in p.included_blocks}
    assert included == {"Acme"}, included
    # both set-asides excluded + logged
    assert sum("EXCLUSION (ruling)" in line for line in log) == 2
    # the typo'd ruling name surfaces as a visible no-op, not silent
    assert any("no-op" in line and "Nonexistent" in line for line in log)


def test_apply_exclusions_does_not_overmatch_short_names():
    """A short abbreviation 'BBS' must not be excluded by a 'Borealis Builders
    Solutions' ruling (norm 'bbs' is not contained in 'borealisbuilderssolutions');
    the short-name guard prevents accidental containment over-matching."""
    p = _parsed([_block("BBS", 25), _block("Borealis Builders Solutions", 45)])
    apply_exclusions(p, ["Borealis Builders Solutions"])
    assert {b.name for b in p.included_blocks} == {"BBS"}
