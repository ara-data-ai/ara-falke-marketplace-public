"""Tests for the Scorecard Summary companion (scorecard/summary.py).

Drives summary.build_summary_context with SYNTHETIC pipeline-result fixtures
(no client xlsx required) across the rubric's branch cases:
  - clear winner (gap >= 5 -> decisive language)
  - neck-and-neck (gap < 5 -> close-call language)
  - provisional coverage (full_coverage False -> provisional framing, curve held)
  - RISK-tier top bid (guardrail framing, NOT a recommendation)
  - the 3 mandatory caveats always present
Plus: the winner is framed as BEST VALUE (not lowest price), and the summary
template renders to HTML through the shared (autoescape-on) Jinja env.
"""
import os

import pytest

from scorecard.render import SUMMARY_TEMPLATE_FILE, render_template
from scorecard.summary import build_summary_context


# --------------------------------------------------------------------------- #
# Synthetic result builder — mirrors pipeline.run_scorecard()'s output shape.   #
# --------------------------------------------------------------------------- #
def _bidder(name, rank, tier, total, per_sf, overall_numeric, overall_display,
            coverage=1.0, applied=True, variance_text=None):
    return {
        "name": name,
        "rank": rank,
        "total": total,
        "per_sf": per_sf,
        "tier": tier,
        "bid_m": round(total / 1e6, 2),
        "section_c": {
            "variance_text": variance_text or f"+$0.00M vs $3.45M mid",
        },
        "overall": {
            "numeric": overall_numeric,
            "display": overall_display,
            "coverage": coverage,
            "applied": applied,
        },
    }


def _result(bidders, *, full_coverage=True, overall_label=None,
            fingerprints=None, project_name="Sample Condominium · Lobby Renovation",
            sf_basis=16000):
    """Assemble a minimal result dict: ranking is rank-sorted like the pipeline."""
    ranking = [
        {"name": b["name"], "rank": b["rank"], "tier": b["tier"],
         "strengths": [], "risks": []}
        for b in sorted(bidders, key=lambda b: b["rank"])
    ]
    if overall_label is None:
        overall_label = ("Overall = applied PRESENTATION ADJUSTMENT (compression "
                         "+ price-value penalty); raw weighted average alongside."
                         if full_coverage else
                         "Overall = honest weighted average (PROVISIONAL — curve "
                         "withheld until qualitative coverage = 100%).")
    return {
        "meta": {
            "run_id": "deadbeef0001",
            "project_name": project_name,
            "footer_note": (f"Falke Corp · prepared by ARA · 2026-05-30 · "
                            f"base/grand-total construction cost only · SF basis "
                            f"{sf_basis:,.0f} · run deadbeef0001"),
        },
        "bidders": bidders,
        "ranking": ranking,
        "overall_label": overall_label,
        "full_coverage": full_coverage,
        "fingerprints": fingerprints or [],
    }


class _FP:
    """Minimal stand-in for mechanical.FingerprintHit (only the attrs used)."""
    def __init__(self, baseline_label, bidder_name):
        self.baseline_label = baseline_label
        self.bidder_name = bidder_name


# --------------------------------------------------------------------------- #
# Fixtures per branch                                                          #
# --------------------------------------------------------------------------- #
def _clear_winner_result():
    # Gap = 84 - 74 = 10 (>= 5): decisive. Winner is TOP tier (best value).
    return _result([
        _bidder("Acme", 1, "TOP", 3360000, 210, 84, "84"),
        _bidder("Cascade", 2, "MID", 3050000, 191, 74, "74"),
        _bidder("Dorne", 3, "DEFENSIVE", 3680000, 230, 69, "69"),
    ])


def _neck_and_neck_result():
    # Gap = 84 - 82 = 2 (< 5): close call.
    return _result([
        _bidder("Acme", 1, "TOP", 3360000, 210, 84, "84"),
        _bidder("Borealis", 2, "TOP", 3370000, 211, 82, "82"),
        _bidder("Cascade", 3, "MID", 3050000, 191, 70, "70"),
    ])


def _provisional_result():
    # full_coverage False: provisional framing, curve withheld.
    return _result([
        _bidder("Acme", 1, "TOP", 3360000, 210, 83, "83* (prov., 75% coverage)",
                coverage=0.75, applied=False),
        _bidder("Cascade", 2, "MID", 3050000, 191, 70, "70* (prov., 50% coverage)",
                coverage=0.50, applied=False),
    ], full_coverage=False)


def _risk_top_result():
    # rank-1 bidder is RISK tier: guardrail framing.
    return _result([
        _bidder("Granite", 1, "RISK", 1950000, 122, 65, "65",
                variance_text="−$1.45M vs $3.45M mid"),
        _bidder("Acme", 2, "TOP", 3360000, 210, 60, "60"),
    ])


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_clear_winner_decisive_language():
    ctx = build_summary_context(_clear_winner_result())
    assert ctx["winner_name"] == "Acme"
    rationale = ctx["winner_rationale"].lower()
    bottom = ctx["bottom_line"].lower()
    # decisive, best-value framing — NOT a close call, NOT provisional
    assert "recommended best value" in rationale
    assert "essentially tied" not in rationale
    assert "front-runner" not in rationale and "provisional" not in rationale
    assert "best value" in bottom
    # the dollar figure is the $X.XXM form, not a raw integer dump
    assert "$3.36M" in ctx["winner_rationale"]


def test_winner_is_best_value_not_lowest_price():
    # Acme ($3.36M) wins over the cheaper Cascade ($3.05M): best value, by design.
    ctx = build_summary_context(_clear_winner_result())
    assert ctx["winner_name"] == "Acme"
    assert "not the lowest bid" in ctx["bottom_line"].lower() or \
           "by design" in ctx["bottom_line"].lower()
    # the cheapest bidder did NOT win
    cheapest = min(_clear_winner_result()["bidders"], key=lambda b: b["total"])
    assert cheapest["name"] != ctx["winner_name"]


def test_neck_and_neck_close_call_language():
    ctx = build_summary_context(_neck_and_neck_result())
    rationale = ctx["winner_rationale"].lower()
    bottom = ctx["bottom_line"].lower()
    # both leaders named; the number does not separate them
    assert "essentially tied" in rationale
    assert "acme" in rationale and "borealis" in rationale
    assert "neck-and-neck" in bottom
    assert "acme" in bottom and "borealis" in bottom


def test_provisional_coverage_framing_and_curve_withheld():
    ctx = build_summary_context(_provisional_result())
    rationale = ctx["winner_rationale"].lower()
    bottom = ctx["bottom_line"].lower()
    caveats = " ".join(ctx["caveats"]).lower()
    # front-runner on a provisional score, not a settled recommendation
    assert "front-runner" in rationale or "provisional" in rationale
    assert "provisional" in bottom and "front-runner" in bottom
    # the provisional caveat is present and the curve note is NOT (curve withheld)
    assert "provisional" in caveats
    assert "held back" in caveats
    assert "adjusted for presentation" not in caveats


def test_risk_tier_top_guardrail_not_recommendation():
    ctx = build_summary_context(_risk_top_result())
    rationale = ctx["winner_rationale"].lower()
    bottom = ctx["bottom_line"].lower()
    # NOT celebratory: no "recommended best value"; flags the scope gap + review
    assert "recommended best value" not in rationale
    assert "materially below" in rationale
    assert "scope" in rationale
    assert "review" in rationale or "flagged" in rationale
    # bottom line is the guardrail line — resolve the gap before recommending
    assert "materially below" in bottom
    assert "before treating any bid" in bottom


def test_three_mandatory_caveats_always_present():
    # Across clear-winner, provisional, and RISK results, the always-on caveats
    # (bid-derived baseline, informed judgment, informs-not-decides, no legal
    # advice) must appear every time.
    for r in (_clear_winner_result(), _provisional_result(), _risk_top_result(),
              _neck_and_neck_result()):
        caveats = " ".join(build_summary_context(r)["caveats"]).lower()
        assert "bids themselves as a reference" in caveats         # (a) baseline
        assert "professional judgment" in caveats                  # (c) judgment
        assert "does not award the contract" in caveats            # (d) informs
        assert "nothing here is legal advice" in caveats           # (e) legal


def test_curve_caveat_present_when_curve_applied_full_coverage():
    ctx = build_summary_context(_clear_winner_result())  # full coverage, curve on
    caveats = " ".join(ctx["caveats"]).lower()
    assert "adjusted for presentation" in caveats
    # and the provisional note is absent on a fully-covered run
    assert "held back" not in caveats


def test_fingerprint_appends_honest_baseline_note():
    r = _clear_winner_result()
    r["fingerprints"] = [_FP("Direct trades subtotal", "Harbor")]
    caveats = build_summary_context(r)["caveats"]
    baseline_caveat = caveats[0].lower()
    assert "direct trades subtotal" in baseline_caveat
    assert "harbor" in baseline_caveat
    assert "leans on the bids" in baseline_caveat


def test_ranked_bidders_and_runners_up_shape():
    ctx = build_summary_context(_clear_winner_result())
    rb = ctx["ranked_bidders"]
    assert [b["rank"] for b in rb] == [1, 2, 3]
    assert rb[0]["name"] == "Acme"
    # tier rendered as the human label, overall as the card's display string
    assert "TOP" in rb[0]["tier"]
    assert rb[0]["overall"] == "84"
    # runners-up names the next 1-2 with a plain reason
    note = ctx["runners_up_note"]
    assert "Cascade" in note and "Dorne" in note


def test_methodology_includes_sf_basis_and_is_plain():
    ctx = build_summary_context(_clear_winner_result())
    method = ctx["methodology_plain"]
    assert "16,000-SF" in method  # SF basis pulled from footer_note
    assert "per square foot" in method.lower()


def test_summary_template_renders_to_html():
    # The companion template renders through the shared autoescape-on env, so
    # the rendered body carries the HTML-ESCAPED form of each field (apostrophes
    # in the caveats become &#39;). Assert against the escaped text — that is the
    # autoescape contract working as designed.
    from markupsafe import escape

    ctx = build_summary_context(_clear_winner_result())
    html = render_template(SUMMARY_TEMPLATE_FILE, ctx)
    assert "Scorecard Summary" in html
    assert "Acme" in html
    assert str(escape(ctx["bottom_line"])) in html
    # all caveats made it into the rendered list (escaped)
    for c in ctx["caveats"]:
        assert str(escape(c)) in html


def test_summary_template_autoescapes_untrusted_names():
    # A bidder name with HTML must be escaped (autoescape contract; OWASP-LLM).
    r = _clear_winner_result()
    r["bidders"][0]["name"] = "Acme <script>x</script>"
    r["ranking"][0]["name"] = "Acme <script>x</script>"
    ctx = build_summary_context(r)
    html = render_template(SUMMARY_TEMPLATE_FILE, ctx)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_single_bidder_has_no_runner_up():
    r = _result([_bidder("Acme", 1, "TOP", 3360000, 210, 86, "86")])
    ctx = build_summary_context(r)
    assert "only one comparable bid" in ctx["runners_up_note"].lower()


def test_render_summary_writes_both_artifacts(tmp_path):
    """Drive pipeline.render_summary end-to-end on a synthetic result and confirm
    it writes scorecard_summary.html (and the .pdf when a PDF engine is present),
    naming the winner (Acme) with best-value reasoning. When no Chromium/
    Playwright engine is installed, the HTML still lands (html_only fallback) so
    the wiring is proven without the client xlsx."""
    from scorecard.errors import RenderError
    from scorecard.pipeline import render_summary

    out = str(tmp_path)
    try:
        paths = render_summary(_clear_winner_result(), out, engine="chromium")
        assert os.path.exists(paths["summary_pdf"])
        assert paths["summary_pdf"].endswith("scorecard_summary.pdf")
    except RenderError:
        # No PDF engine in this environment — prove the HTML path still writes.
        paths = render_summary(
            _clear_winner_result(), out, engine="chromium", html_only=True)
        assert "summary_pdf" not in paths

    html_path = paths["summary_html"]
    assert html_path.endswith("scorecard_summary.html")
    assert os.path.exists(html_path)
    body = open(html_path, encoding="utf-8").read()
    assert "Acme" in body
    assert "recommended best value" in body
    assert "not the lowest bid" in body


def _playwright_available():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _playwright_available(),
                    reason="Playwright/Chromium not installed in this env.")
def test_render_summary_produces_real_pdf():
    """When Chromium/Playwright is present, render_summary writes a real,
    non-empty scorecard_summary.pdf (the SAME engine path as the scorecard).
    Writes to a stable /tmp path so the artifact can be inspected directly."""
    from scorecard.pipeline import render_summary

    out = "/tmp/falke_summary_render_check"
    os.makedirs(out, exist_ok=True)
    paths = render_summary(_clear_winner_result(), out, engine="chromium")
    pdf = paths["summary_pdf"]
    assert os.path.exists(pdf)
    with open(pdf, "rb") as fh:
        head = fh.read(5)
    assert head[:4] == b"%PDF"          # valid PDF magic
    assert os.path.getsize(pdf) > 1000  # non-trivial document
