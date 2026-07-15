"""Matrix config-foundation unit tests (Boris).

Covers the loader/validation/matcher/gate built for the matrix generalization
sprint: known_firms.yaml loading + schema validation (C8), the firm matcher +
collision detection (C3), and the RunInputs identity validation + SF-basis gate
(M2). Mirrors the scorecard engine's test idioms (test_config.py /
test_cli_sf_gate.py): real YAML written to tmp_path, typed-error assertions, no
mocking.

Run from the engine root (engines/matrix/):
    python3 -m pytest tests/test_config_foundation.py -v
"""
from __future__ import annotations

import textwrap

import pytest

from src.config_errors import KnownFirmsConfigError, MissingParameterError, MatrixConfigError
from src.firm_config import (
    DEFAULT_KNOWN_FIRMS_PATH,
    load_known_firms,
)
from src.run_config import (
    SF_GATE_STOP,
    RunInputs,
    load_run_config,
    resolve_sf_basis,
)


def _write(tmp_path, text):
    p = tmp_path / "known_firms.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


# A synthetic, ACTIVE overlay firm (no `example` flag) used to prove the
# merge + match + reclass mechanism without any real firm name.
_SYNTHETIC_OVERLAY = """
    firms:
      - firm_id: acme
        match: ["acme"]
        code_format_profile: csi_1995_2digit
        reclassifications:
          - rule_id: ACME_DUMPSTER
            from: "DIV 11 00 00"
            to:   "DIV 01 00 00"
            when_description_contains_all: ["dumpster"]
"""


def _scaffold_with_overlay(tmp_path, overlay_text):
    """Write the shipped scaffold + a sibling local overlay in tmp_path and
    return the scaffold path (the loader auto-discovers the sibling)."""
    scaffold = tmp_path / "known_firms.yaml"
    scaffold.write_text(open(DEFAULT_KNOWN_FIRMS_PATH, encoding="utf-8").read(),
                        encoding="utf-8")
    (tmp_path / "known_firms.local.yaml").write_text(
        textwrap.dedent(overlay_text), encoding="utf-8")
    return str(scaffold)


# ---------------------------------------------------------------------------
# Shipped file — ONE test file serves BOTH build heads (release-process R-5):
#   * PRIVATE canonical: known_firms.yaml carries the REAL recurring-firm
#     library + an INERT schema example -> the private-build tests run.
#   * PUBLIC artifact (produced from canonical by the scripted scrub, which
#     swaps ONLY known_firms.yaml for the scaffold): the scaffold-build test
#     runs instead. No test file is ever edited by the release scrub.
# The section self-selects on whether the shipped file carries active firms.
# ---------------------------------------------------------------------------

_SHIPPED_HAS_ACTIVE_FIRMS = any(
    not f.example for f in load_known_firms(DEFAULT_KNOWN_FIRMS_PATH).firms)


def _has_reclass(firm, frm, to, kws):
    kws = {k.lower() for k in kws}
    return any(
        r.from_division == frm and r.to_division == to
        and kws <= {k.lower() for k in r.when_description_contains_all}
        for r in firm.reclassifications
    )


@pytest.mark.skipif(not _SHIPPED_HAS_ACTIVE_FIRMS,
                    reason="shipped file is the public scaffold (release-scrub "
                           "artifact) — private-build assertions do not apply")
def test_shipped_known_firms_carries_active_real_firms():
    """The private-repo base ships ACTIVE recurring firms (not just the inert
    example), so they match directly with NO overlay. Verified structurally +
    behaviorally — the firm NAMES are asserted only via the config's own match
    terms, never hard-coded here (real names live solely in known_firms.yaml)."""
    cfg = load_known_firms(DEFAULT_KNOWN_FIRMS_PATH)
    active = [f for f in cfg.firms if not f.example]
    assert active, "base file must ship at least one active (non-example) firm"

    # a firm that reclassifies the two known destructive moves
    # (dumpster DIV 11 -> DIV 01 AND flooring-labor DIV 13 -> DIV 09)
    assert any(
        _has_reclass(f, "DIV 11 00 00", "DIV 01 00 00", ["dumpster"])
        and _has_reclass(f, "DIV 13 00 00", "DIV 09 00 00", ["flooring", "labor"])
        for f in active
    ), "base must carry the two-move destructive-reclass firm"

    # a firm selecting the legacy csi_1995_2digit code-format profile
    assert any(f.code_format_profile == "csi_1995_2digit" for f in active), (
        "base must carry the legacy-code-format firm")

    # each active firm matches a bid carrying its OWN (config-defined) match term
    for f in active:
        assert cfg.match(f.match[0] + " Construction LLC").firm is not None


def test_shipped_example_firm_is_inert():
    """The inert schema-example entry is schema-validated but filtered from
    matching, so it can never fire against a real bid (spec §1.4)."""
    cfg = load_known_firms(DEFAULT_KNOWN_FIRMS_PATH)
    ex = next(f for f in cfg.firms if f.firm_id == "example_restoration")
    assert ex.example is True
    assert ex.code_format_profile == "csi_1995_2digit"
    assert [r.rule_id for r in ex.reclassifications] == ["EXAMPLE_DUMPSTER"]
    assert cfg.match("examplecontractor").firm is None
    assert cfg.match("examplecontractor").matched_firm_ids == []


@pytest.mark.skipif(_SHIPPED_HAS_ACTIVE_FIRMS,
                    reason="shipped file is the private real-firm library — "
                           "scaffold-build assertions do not apply")
def test_shipped_scaffold_is_sole_inert_example():
    """PUBLIC build only: the scrubbed shipped file is a public-safe scaffold —
    its sole firm is the inert schema example (zero real names) and the match
    set is empty (safe-absent, spec §1.2/§1.4/§4)."""
    cfg = load_known_firms(DEFAULT_KNOWN_FIRMS_PATH)
    assert {f.firm_id for f in cfg.firms} == {"example_restoration"}
    assert cfg.match("Any Contractor LLC").firm is None


def test_absent_overlay_loads_base_library():
    """No local overlay next to the base file → the merged set is just the base
    library (nothing dropped, nothing invented); absent overlay is the normal,
    safe case on BOTH build heads. A contractor matching no configured term
    matches nothing."""
    cfg = load_known_firms(DEFAULT_KNOWN_FIRMS_PATH)
    assert "example_restoration" in {f.firm_id for f in cfg.firms}
    if _SHIPPED_HAS_ACTIVE_FIRMS:  # private base: active firms survive the merge
        assert any(not f.example for f in cfg.firms)
    assert cfg.match("Any Restoration LLC").firm is None


# ---------------------------------------------------------------------------
# Loader / schema validation (C8)
# ---------------------------------------------------------------------------

def test_malformed_yaml_hard_stops(tmp_path):
    path = _write(tmp_path, "firms: [oops: : :\n")
    with pytest.raises(KnownFirmsConfigError, match="not valid YAML"):
        load_known_firms(path)


def test_missing_file_hard_stops():
    with pytest.raises(KnownFirmsConfigError, match="not found"):
        load_known_firms("/no/such/known_firms.yaml")


def test_bad_from_division_hard_stops(tmp_path):
    path = _write(tmp_path, """
        firms:
          - firm_id: x
            match: ["x"]
            reclassifications:
              - rule_id: R
                from: "DIV 99 00 00"
                to:   "DIV 09 00 00"
                when_description_contains_all: ["flooring"]
    """)
    with pytest.raises(KnownFirmsConfigError, match="`from`.*not a canonical"):
        load_known_firms(path)


def test_bad_to_division_fabrication_hard_stops(tmp_path):
    path = _write(tmp_path, """
        firms:
          - firm_id: x
            match: ["x"]
            reclassifications:
              - rule_id: R
                from: "DIV 13 00 00"
                to:   "DIV 99 00 00"
                when_description_contains_all: ["flooring"]
    """)
    with pytest.raises(KnownFirmsConfigError, match="fabricate a non-canonical"):
        load_known_firms(path)


def test_from_equals_to_hard_stops(tmp_path):
    path = _write(tmp_path, """
        firms:
          - firm_id: x
            match: ["x"]
            reclassifications:
              - rule_id: R
                from: "DIV 09 00 00"
                to:   "DIV 09 00 00"
                when_description_contains_all: ["flooring"]
    """)
    with pytest.raises(KnownFirmsConfigError, match="no-op reclass"):
        load_known_firms(path)


def test_empty_match_hard_stops(tmp_path):
    path = _write(tmp_path, """
        firms:
          - firm_id: x
            match: []
    """)
    with pytest.raises(KnownFirmsConfigError, match="`match` must be a non-empty"):
        load_known_firms(path)


def test_empty_keywords_hard_stops(tmp_path):
    path = _write(tmp_path, """
        firms:
          - firm_id: x
            match: ["x"]
            reclassifications:
              - rule_id: R
                from: "DIV 13 00 00"
                to:   "DIV 09 00 00"
                when_description_contains_all: []
    """)
    with pytest.raises(KnownFirmsConfigError, match="keyword guard"):
        load_known_firms(path)


def test_two_rule_cycle_hard_stops(tmp_path):
    path = _write(tmp_path, """
        firms:
          - firm_id: x
            match: ["x"]
            reclassifications:
              - rule_id: A
                from: "DIV 13 00 00"
                to:   "DIV 09 00 00"
                when_description_contains_all: ["flooring"]
              - rule_id: B
                from: "DIV 09 00 00"
                to:   "DIV 13 00 00"
                when_description_contains_all: ["tile"]
    """)
    with pytest.raises(KnownFirmsConfigError, match="cycle"):
        load_known_firms(path)


def test_duplicate_firm_id_hard_stops(tmp_path):
    path = _write(tmp_path, """
        firms:
          - firm_id: dup
            match: ["a"]
          - firm_id: dup
            match: ["b"]
    """)
    with pytest.raises(KnownFirmsConfigError, match="duplicate firm_id"):
        load_known_firms(path)


def test_validate_false_skips_deep_checks(tmp_path):
    # bad division code passes when validate=False (structure-only parse).
    path = _write(tmp_path, """
        firms:
          - firm_id: x
            match: ["x"]
            reclassifications:
              - rule_id: R
                from: "DIV 99 00 00"
                to:   "DIV 09 00 00"
                when_description_contains_all: ["flooring"]
    """)
    cfg = load_known_firms(path, validate=False)
    assert cfg.firms[0].firm_id == "x"


# ---------------------------------------------------------------------------
# Firm matcher + collision (C3)
# ---------------------------------------------------------------------------

def test_overlay_firm_matches_when_present(tmp_path):
    """An ACTIVE overlay firm (auto-discovered sibling) matches by its term —
    proving the merge + match mechanism with a synthetic name."""
    path = _scaffold_with_overlay(tmp_path, _SYNTHETIC_OVERLAY)
    cfg = load_known_firms(path)
    res = cfg.match("Acme Restoration LLC")
    assert not res.ambiguous
    assert res.firm is not None and res.firm.firm_id == "acme"


def test_overlay_match_term_is_collision_safe(tmp_path):
    # a distinctive full-token match term must not fire on a bare substring.
    overlay = """
        firms:
          - firm_id: pinnacle
            match: ["pinnacle builders"]
            code_format_profile: csi_1995_2digit
    """
    path = _scaffold_with_overlay(tmp_path, overlay)
    cfg = load_known_firms(path)
    assert cfg.match("Pinnacle Builders Group").firm.firm_id == "pinnacle"
    # a name that merely shares a fragment must NOT match.
    assert cfg.match("Summit Widgets Inc.").firm is None


def test_match_none_for_unknown_firm():
    cfg = load_known_firms(DEFAULT_KNOWN_FIRMS_PATH)
    res = cfg.match("Coastal Concrete Restoration LLC")
    assert not res.ambiguous and res.firm is None and res.matched_firm_ids == []


def test_collision_is_ambiguous_no_first_wins(tmp_path):
    # constructed collision fixture (GS-7): two synthetic entries both hit one name.
    path = _write(tmp_path, """
        firms:
          - firm_id: acme
            match: ["acme"]
          - firm_id: acme_restoration
            match: ["acme restoration"]
    """)
    cfg = load_known_firms(path)
    res = cfg.match("Acme Restoration LLC")
    assert res.ambiguous
    assert res.firm is None  # no first-wins
    assert set(res.matched_firm_ids) == {"acme", "acme_restoration"}


def test_overlay_collision_with_scaffold_id_overrides(tmp_path):
    """An overlay firm_id colliding with a scaffold id REPLACES the scaffold
    entry (local wins), not a duplicate error (spec §1.3)."""
    overlay = """
        firms:
          - firm_id: example_restoration
            match: ["acme"]
    """
    path = _scaffold_with_overlay(tmp_path, overlay)
    cfg = load_known_firms(path)
    ex = next(f for f in cfg.firms if f.firm_id == "example_restoration")
    assert ex.match == ["acme"]      # overlay content won
    assert ex.example is False       # overlay is an active (non-example) firm
    assert cfg.match("Acme Restoration LLC").firm.firm_id == "example_restoration"


def test_overlay_invalid_reclass_hard_stops(tmp_path):
    """A merged overlay firm with an invalid reclass rule hard-stops (spec §1.3)."""
    overlay = """
        firms:
          - firm_id: bad
            match: ["bad"]
            reclassifications:
              - rule_id: BAD
                from: "DIV 11 00 00"
                to:   "DIV 99 00 00"
                when_description_contains_all: ["dumpster"]
    """
    path = _scaffold_with_overlay(tmp_path, overlay)
    with pytest.raises(KnownFirmsConfigError, match="not a canonical"):
        load_known_firms(path)


# ---------------------------------------------------------------------------
# RunInputs identity validation (M1) + SF-basis gate (M2)
# ---------------------------------------------------------------------------

def test_run_inputs_valid():
    ri = RunInputs(project_name="P", project_address="A", gross_sf=10000.0)
    ri.validate()  # no raise


def test_missing_project_name_hard_stops():
    ri = RunInputs(project_name="", project_address="A", gross_sf=10000.0)
    with pytest.raises(MissingParameterError, match="project_name"):
        ri.validate()


def test_missing_gross_sf_hard_stops():
    ri = RunInputs(project_name="P", project_address="A", gross_sf=None)
    with pytest.raises(MissingParameterError, match="gross_sf"):
        ri.validate()


def test_nonpositive_gross_sf_hard_stops():
    ri = RunInputs(project_name="P", project_address="A", gross_sf=0.0)
    with pytest.raises(MatrixConfigError, match="must be positive"):
        ri.validate()


def test_load_run_config_overrides_win(tmp_path):
    p = tmp_path / "project.yaml"
    p.write_text("project_name: FromFile\nproject_address: A\ngross_sf: 5000\n")
    ri = load_run_config(str(p), overrides={"gross_sf": 9999, "sf_source": "explicit"})
    assert ri.project_name == "FromFile"
    assert ri.gross_sf == 9999
    assert ri.sf_source == "explicit"


def test_sf_gate_explicit():
    assert resolve_sf_basis(12345.0, False, None) == (12345.0, "explicit")


def test_sf_gate_confirmed_uses_extracted():
    assert resolve_sf_basis(None, True, 8000.0) == (8000.0, "matrix-confirmed")


def test_sf_gate_neither_stops_with_suggestion():
    val, msg = resolve_sf_basis(None, False, 8000.0)
    assert val == SF_GATE_STOP and "8,000 SF" in msg


def test_sf_gate_confirmed_but_no_gsf_stops():
    val, msg = resolve_sf_basis(None, True, None)
    assert val == SF_GATE_STOP and "explicitly" in msg
