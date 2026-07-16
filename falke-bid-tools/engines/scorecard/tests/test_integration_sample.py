"""End-to-end validation against a sample bid matrix (synthetic gold standard).

Asserts the parser detects the right field and reproduces the sample scorecard's
Section B totals/$/SF/tiers and the ranking. Skips if the binary xlsx is absent
(so unit tests still run in CI without the client file). All firms and figures
referenced here are fictional.
"""
import pytest

from scorecard.config import load_config
from scorecard.errors import MissingParameterError
from scorecard.matrix import MatrixParser, apply_display_aliases
from scorecard.pipeline import run_scorecard
from .conftest import (BASELINE_JSON, DROPPED, GOLD_ALIASES, GOLD_EXCLUSIONS,
                       GOLD_OVERALL, GOLD_OVERRIDES_JSON, GOLD_PER_SF,
                       GOLD_TIERS, GOLD_TOTALS, SAMPLE_XLSX)

import json
import os


def _cfg():
    return load_config(overrides={
        "sf_basis": 16000, "band_low": 3.35, "band_high": 3.55,
        "modeled_mid_takeoff": 3.40, "variance_mid": 3.45})


def _baseline():
    with open(BASELINE_JSON, "r", encoding="utf-8") as fh:
        return json.load(fh)


requires_xlsx = pytest.mark.skipif(
    not os.path.exists(SAMPLE_XLSX),
    reason="sample matrix not present in Inputs/ (client binary).")


@requires_xlsx
def test_parser_detects_generic_blocks():
    cfg = _cfg()
    parser = MatrixParser(cfg.block("matrix"))
    parsed = parser.parse(SAMPLE_XLSX)
    # ground truth: 10 detected blocks, +5 stride, width 4, sheet Bid_Form
    assert parsed.sheet_name == "Bid_Form"
    assert parsed.header_row == 8
    assert parsed.block_stride == 5
    assert parsed.block_width == 4
    assert len(parsed.blocks) == 10
    # grand-total row is 164 and is NOT the construction-cost subtotal (154)
    assert parsed.grand_total_row == 164
    # matrix GSF detected as 12000 but reported only
    assert parsed.gsf_value == 12000


@requires_xlsx
def test_completeness_counts_division_subtotals_not_false_zero():
    """Regression (Defect: false 0/20). The completeness counter must read the
    COST_SUBTOTALS column where division subtotals actually sit — NOT the empty
    per-line COST column. The source headers carry in-cell newlines
    ('COST \\nSUBTOTALS') and some blocks wrap the 'SUBTOTALS' half off the
    single quartet row, so the COST_SUBTOTAL bucket previously failed to resolve
    into b.cols for the Crest (E-H, col F) and Dorne (J-M, col K) blocks and the
    counter logged a false 0/20.

    Direct cell ground truth: Crest col F = 17/20 populated; Dorne col K = 20/20;
    Acme ~16/20. No included bidder may report a false 0 against ~20 division
    rows (Marvin §1.4: never auto-drop on a counter artifact)."""
    cfg = _cfg()
    parser = MatrixParser(cfg.block("matrix"))
    parsed = parser.parse(SAMPLE_XLSX)
    # apply the board-card display aliases so block names line up with the sample
    # short names ('Acme', etc.); does not affect parsing/counts.
    apply_display_aliases(parsed, GOLD_ALIASES)
    # Build the lookup from INCLUDED bidders ONLY: the matrix carries two Dorne
    # blocks, and the dropped col-AX duplicate (included=False, 17/20) would
    # otherwise clobber the kept col-J Dorne (included=True, 20/20) under dict
    # last-wins. Filtering to included blocks keeps a dropped duplicate from
    # shadowing a kept bidder.
    by_name = {b.name: b for b in parsed.blocks if b.included}

    # the COST_SUBTOTAL column must resolve for the two previously-broken blocks
    assert "cost_subtotal" in by_name["Crest"].cols, by_name["Crest"].cols
    assert "cost_subtotal" in by_name["Dorne"].cols, by_name["Dorne"].cols

    # the two blocks that previously reported a false 0 now read their true
    # counts. Dorne col K is fully populated (20/20, firm ground truth); Crest
    # col F is ~17/20 (allow +/-1 for benign division-row detection variance —
    # the load-bearing proof is the resolved bucket above + a non-zero count).
    assert 16 <= by_name["Crest"].populated_divisions <= 18, \
        by_name["Crest"].populated_divisions
    assert by_name["Dorne"].populated_divisions == 20, \
        by_name["Dorne"].populated_divisions
    # a known-good middle block
    assert by_name["Acme"].populated_divisions >= 15, \
        by_name["Acme"].populated_divisions

    # NO included bidder should report a false zero against ~20 division rows
    assert len(parsed.division_rows) >= 18
    for b in parsed.included_blocks:
        assert b.populated_divisions > 0, b.name


@requires_xlsx
def test_seven_kept_bidders_and_drops():
    cfg = _cfg()
    parser = MatrixParser(cfg.block("matrix"))
    parsed = parser.parse(SAMPLE_XLSX)
    included = {b.name for b in parsed.included_blocks}
    # exactly the 7 keepers (duplicate Dorne dropped; Harbor/Borealis flagged
    # but the duplicate is a hard drop)
    assert "Dorne" in included
    # the AX duplicate Dorne must be dropped
    dorne_blocks = [b for b in parsed.blocks if b.norm == "dorne"]
    assert len(dorne_blocks) == 2
    assert sum(1 for b in dorne_blocks if b.included) == 1
    kept_dorne = next(b for b in dorne_blocks if b.included)
    assert abs(kept_dorne.grand_total - GOLD_TOTALS["Dorne"]) < 1.0


@requires_xlsx
def test_row164_totals_match_pdf_for_all_7():
    cfg = _cfg()
    parser = MatrixParser(cfg.block("matrix"))
    parsed = parser.parse(SAMPLE_XLSX)
    apply_display_aliases(parsed, GOLD_ALIASES)
    # Build the lookup from INCLUDED bidders ONLY. The matrix carries TWO Dorne
    # blocks; the col-AX duplicate is dropped (included=False) in favor of the
    # kept col-J Dorne. Building by_name over ALL blocks let the dropped
    # duplicate clobber the kept one (dict last-wins), so by_name['Dorne'] was
    # the wrong total. Filter to the kept field.
    by_name = {b.name: b for b in parsed.included_blocks}
    for name, gold in GOLD_TOTALS.items():
        assert name in by_name, f"missing bidder {name}"
        assert abs(by_name[name].grand_total - gold) < 1.0, name


@requires_xlsx
def test_end_to_end_per_sf_tiers_and_ranking():
    cfg = _cfg()
    # Curated end-to-end view: apply the §1.4 set-aside ruling so the scored
    # field is exactly the 7 keepers (Harbor + Borealis excluded). This matches
    # the gold $/SF, tiers, and DROPPED constants below. The skill's DEFAULT
    # remains include-all-and-flag (regression-guarded by
    # test_exclusion_ruling_curates_seven_bidder_field).
    result = run_scorecard(
        SAMPLE_XLSX, cfg, baseline_lines=_baseline(),
        exclude=GOLD_EXCLUSIONS, aliases=GOLD_ALIASES,
        project_name="Sample Condominium · Lobby Renovation")
    by_name = {b["name"]: b for b in result["bidders"]}

    # all 7 $/SF match the card
    for name, gold in GOLD_PER_SF.items():
        assert by_name[name]["per_sf"] == gold, name

    # all 7 tiers match Marvin §4.1
    for name, tier in GOLD_TIERS.items():
        assert by_name[name]["tier"] == tier, name

    # dropped bidders absent from the included field
    for d in DROPPED:
        assert d not in by_name

    # ranking: with default config the curve is OFF and coverage is partial, so
    # the ranking is by the honest weighted average over scored categories.
    # Pricing+CO-Risk dominate, so the ORDER must still place the in-band/over
    # bidders above the high-risk ones. Assert the high-risk pair sits last.
    order = [r["name"] for r in result["ranking"]]
    assert order.index("Granite") >= 5
    assert order.index("Fjord") >= 5


@requires_xlsx
def test_raw_wavg_is_the_overall_contract():
    """P0-6: the Overall presentation curve is RETIRED. The published raw
    wavg column IS the Overall contract now — descending GOLD_WAVG gives the
    honest ranking order (Crest raw #3, which the retired curve demoted to
    #5 via the $/SF penalty — the reordering that killed it)."""
    from .conftest import GOLD_RANK_ORDER_RAW, GOLD_WAVG

    order = sorted(GOLD_WAVG, key=lambda n: -GOLD_WAVG[n])
    assert order == GOLD_RANK_ORDER_RAW
    assert order.index("Crest") == 2
    # and the engine no longer carries a curve to apply
    import scorecard.modeling as modeling
    assert not hasattr(modeling, "overall_curve")


@requires_xlsx
def test_fingerprint_test_flags_harbor_and_crest():
    """QA fingerprint: baseline lines within <0.2% of a bidder subtotal.
    Direct trades ~ Harbor's construction-cost subtotal (row 154); Flooring ~
    Crest's Wood&Plastics division subtotal (Marvin §2.2). Both must fire."""
    cfg = _cfg()
    result = run_scorecard(
        SAMPLE_XLSX, cfg, baseline_lines=_baseline(), aliases=GOLD_ALIASES,
        project_name="Sample Condominium · Lobby Renovation")
    hits = result["fingerprints"]
    assert len(hits) >= 2, [(h.bidder_name, h.bidder_value) for h in hits]
    hit_bidders = {h.bidder_name for h in hits}
    # the two documented fingerprints must both fire
    assert any("Harbor" in h or "harbor" in h.lower() for h in hit_bidders), hit_bidders
    assert any(h == "Crest" or "crest" in h.lower() for h in hit_bidders), \
        hit_bidders


@requires_xlsx
def test_exclusion_ruling_curates_seven_bidder_field():
    """Item 3: applying the §1.4 set-aside ruling (exclude Harbor + Borealis)
    curates the scored field to exactly the 7 keepers, and the exclusion is
    LOGGED with a reason. Default (no exclude) still includes the 9 non-duplicate
    bidders (regression: don't change the default)."""
    cfg = _cfg()
    # default: include-all-and-flag (Harbor + Borealis still scored, just ranked)
    default = run_scorecard(
        SAMPLE_XLSX, cfg, baseline_lines=_baseline(), aliases=GOLD_ALIASES,
        project_name="Sample Condominium · Lobby Renovation")
    default_names = {b["name"] for b in default["bidders"]}
    assert any("Harbor" in n for n in default_names)
    assert any("Borealis" in n for n in default_names)

    # with the ruling applied: exactly the 7, both set-asides gone, each logged
    curated = run_scorecard(
        SAMPLE_XLSX, cfg, baseline_lines=_baseline(), exclude=GOLD_EXCLUSIONS,
        aliases=GOLD_ALIASES, project_name="Sample Condominium · Lobby Renovation")
    names = {b["name"] for b in curated["bidders"]}
    assert names == set(GOLD_OVERALL.keys()), names
    assert not any("Harbor" in n for n in names)
    assert not any(n == "Borealis Builders Solutions" for n in names)
    log = "\n".join(curated["log"])
    assert "EXCLUSION (ruling)" in log
    assert log.lower().count("exclusion (ruling)") >= 2  # both set-asides logged


@requires_xlsx
def test_full_reproduction_raw_wavg_on_100pct_coverage():
    """THE CURRENT CONTRACT (post-P0-6): with (a) Harbor + Borealis excluded
    (§1.4 ruling) and (b) the published Section-E 1-10 category scores supplied
    as overrides (=> 100% coverage), every bidder's Overall IS the honest
    weighted average — reproducing the published wavg within +/-2.5 — and the
    ranking follows it (raw order; the retired curve's reordering must NOT
    reproduce)."""
    from .conftest import GOLD_RANK_ORDER_RAW, GOLD_WAVG

    cfg = _cfg()

    with open(GOLD_OVERRIDES_JSON, "r", encoding="utf-8") as fh:
        overrides = json.load(fh)
    overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}

    result = run_scorecard(
        SAMPLE_XLSX, cfg,
        baseline_lines=_baseline(),
        overrides=overrides,
        exclude=GOLD_EXCLUSIONS,
        aliases=GOLD_ALIASES,
        project_name="Sample Condominium · Lobby Renovation",
    )
    by_name = {b["name"]: b for b in result["bidders"]}

    # curated field == exactly the 7 scored bidders
    assert set(by_name) == set(GOLD_WAVG.keys()), set(by_name)

    # 100% qualitative coverage; Overall == the raw weighted average
    for name in GOLD_WAVG:
        ov = by_name[name]["overall"]
        assert ov["coverage"] >= 0.999, (name, ov["coverage"])
        assert ov["numeric"] == ov["weighted_average"], (name, ov)
    assert result["full_coverage"] is True

    # Overall reproduces the published wavg within +/-2.5; ranking is raw
    TOL = 2.5
    for name, wavg_gold in GOLD_WAVG.items():
        got = by_name[name]["overall"]["numeric"]
        assert got is not None, name
        assert abs(got - wavg_gold) <= TOL, (name, got, wavg_gold)
    order = [r["name"] for r in result["ranking"]]
    assert order == GOLD_RANK_ORDER_RAW, order
