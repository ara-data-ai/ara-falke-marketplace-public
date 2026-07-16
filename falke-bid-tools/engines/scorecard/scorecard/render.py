"""Render layer — assemble Anna's Jinja2 context and produce HTML + PDF.

Honors Anna's color-coding contract (style-spec §3):
  - Section B tier_class from tier key.
  - Section E overall_class from parameterized score cuts.
  - Section C row-best tint for top-ranked rows.
PDF engines: headless Chromium via Playwright is the DEFAULT in the Falke
environment (Chromium headless shell is what is installed; invoked with
print_background=True and prefer_css_page_size=True so Anna's @page size is
honored). WeasyPrint is an OPTIONAL alternative engine (not installed by
default); select it explicitly with engine="weasyprint".
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from .config import Config
from .errors import RenderError
from .mechanical import TIER_CSS, TIER_LABELS

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
TEMPLATE_FILE = "scorecard-template.html"
SUMMARY_TEMPLATE_FILE = "scorecard-summary-template.html"


def overall_css(overall: Optional[float], cfg: Config) -> str:
    """Map Overall /100 to Anna's score color class (style-spec §3)."""
    cuts = cfg.block("score_color_cuts")
    if overall is None:
        return "score-mid"
    if overall >= cuts["high_min"]:
        return "score-high"
    if overall < cuts["low_max"]:
        return "score-low"
    return "score-mid"


def fmt_money(v: float) -> str:
    if v is None or (isinstance(v, float) and v != v):  # NaN
        return "—"
    return f"${v:,.0f}"


def fmt_score_cell(score) -> str:
    """1-10 cell; None -> the analyst flag marker."""
    return str(score) if score is not None else "—*"


# Internal placeholder marker used in EXAMPLE qualitative-note inputs to show the
# shape of a bullet. It is draft scaffolding, never a board-facing fact, so the
# render strips it before any bullet reaches the visible card (Floyd C1).
_PLACEHOLDER_MARK = "(placeholder — confirm)"


def _strip_placeholder(text: str) -> Optional[str]:
    """Remove the draft placeholder marker from a qualitative bullet.

    Returns the cleaned bullet, or None if nothing of substance remains (so the
    caller can drop an all-placeholder bullet rather than render an empty one).
    """
    if not isinstance(text, str) or _PLACEHOLDER_MARK not in text:
        return text
    cleaned = text.replace(_PLACEHOLDER_MARK, "").strip()
    # tidy punctuation orphaned by the removal (e.g. "...signal ." -> "...signal.")
    cleaned = cleaned.rstrip(" .")
    return (cleaned + ".") if cleaned else None


def build_disclosures(result: Dict, cfg: Config) -> Dict:
    """Surface the four board-facing data notes the run already produces.

    Driven entirely by the run data (no hardcoded project values); each category
    is omitted when it has no items so the template can skip its sub-section. The
    completeness band is the SAME source of truth the self-audit's C8 uses
    (audit.DIV_BAND) so the card and the audit never disagree.
    """
    from .audit import DIV_BAND  # single source of truth for the C8 band

    parsed = result["parsed"]
    bidders = result["bidders"]

    # (a) Completeness (C8): included bidders whose populated CSI-division count
    # falls outside the expected band (excluding the zero-trap, which is a
    # blocker handled elsewhere).
    completeness = [
        {"name": b.name, "divisions": b.populated_divisions}
        for b in parsed.included_blocks
        if b.populated_divisions != 0
        and not (DIV_BAND[0] <= b.populated_divisions <= DIV_BAND[1])
    ]

    # (b) Duplicate handling: each dropped duplicate column, with kept vs dropped
    # total and the unresolved delta.
    duplicates = []
    for b in parsed.blocks:
        if b.included or not b.drop_reason or "duplicate" not in b.drop_reason.lower():
            continue
        kept = next((k for k in parsed.included_blocks if k.norm == b.norm), None)
        kept_total = kept.grand_total if kept else None
        dropped_total = b.grand_total
        delta = (abs((dropped_total or 0) - (kept_total or 0))
                 if (kept_total is not None and dropped_total is not None) else None)
        duplicates.append({
            "name": b.name,
            "kept_total": fmt_money(kept_total),
            "dropped_total": fmt_money(dropped_total),
            "delta": fmt_money(delta),
        })

    # (c) Bid-anchoring fingerprints: baseline lines matching a bidder subtotal
    # within tolerance (the modeled baseline may be partly bid-derived).
    fingerprints = [
        {"bidder": h.bidder_name,
         "baseline_label": h.baseline_label,
         "bidder_label": h.bidder_subtotal_label,
         "pct_delta": f"{h.pct_delta:.2f}%"}
        for h in result.get("fingerprints", [])
    ]

    # (d) Provisional scoring: bidders scored below full qualitative coverage.
    provisional = [
        {"name": b["name"], "coverage": f"{b['overall']['coverage'] * 100:.0f}%"}
        for b in bidders
        if b["overall"].get("coverage", 1.0) < 0.999
    ]

    return {
        "completeness": completeness,
        "div_band_low": DIV_BAND[0],
        "div_band_high": DIV_BAND[1],
        "duplicates": duplicates,
        "fingerprints": fingerprints,
        "provisional": provisional,
        # convenience flag: render the section only if SOMETHING needs surfacing
        "any": bool(completeness or duplicates or fingerprints or provisional),
    }


def render_template(template_file: str, context: Dict) -> str:
    """Render any template in TEMPLATE_DIR to an HTML string via the shared env.

    Autoescape is ON: bidder names, qualitative notes, and (once the LLMScorer
    is wired) LLM-drafted strengths/risks/rationale flow into the HTML, so they
    are treated as untrusted output and HTML-escaped to prevent injection/XSS
    (OWASP-LLM "treat model output as untrusted"). The scorecard and its summary
    companion share this one env so both inherit the same escaping contract.
    """
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except Exception as e:  # pragma: no cover
        raise RenderError(f"jinja2 not available: {e}")
    path = os.path.join(TEMPLATE_DIR, template_file)
    if not os.path.exists(path):
        raise RenderError(f"Template not found: {path}")
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "htm", "xml"]),
    )
    tpl = env.get_template(template_file)
    return tpl.render(**context)


def render_html(context: Dict) -> str:
    """Render the SCORECARD template to an HTML string (see render_template)."""
    return render_template(TEMPLATE_FILE, context)


def render_pdf(html: str, out_pdf: str, *, engine: str = "chromium") -> str:
    """Render HTML string to PDF. engine ∈ {auto, chromium, weasyprint}.

    DEFAULT is Chromium/Playwright — that is the engine installed in the Falke
    environment. Under "auto" Chromium is tried FIRST and WeasyPrint (optional,
    not installed by default) is the fallthrough. Returns the output path.
    Raises RenderError if no engine is available (NO silent fallback to a broken
    PDF).
    """
    chromium_err = None
    if engine in ("auto", "chromium"):
        try:
            from playwright.sync_api import sync_playwright
            tmp_html = out_pdf + ".tmp.html"
            with open(tmp_html, "w", encoding="utf-8") as fh:
                fh.write(html)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto("file://" + os.path.abspath(tmp_html))
                # Anna §2: print_background MUST be true; prefer_css_page_size
                # honors Anna's @page size.
                page.pdf(path=out_pdf, print_background=True,
                         prefer_css_page_size=True)
                browser.close()
            os.remove(tmp_html)
            return out_pdf
        except Exception as e:
            if engine == "chromium":
                raise RenderError(f"Chromium/Playwright render failed: {e}")
            # else fall through to optional WeasyPrint under 'auto'
            chromium_err = e
    if engine in ("auto", "weasyprint"):
        try:
            from weasyprint import HTML
            HTML(string=html, base_url=TEMPLATE_DIR).write_pdf(out_pdf)
            return out_pdf
        except Exception as e:
            raise RenderError(
                f"No working PDF engine. Chromium error: "
                f"{chromium_err if chromium_err is not None else 'n/a'}; "
                f"WeasyPrint error: {e}. Chromium/Playwright is the default in "
                f"the Falke environment; WeasyPrint is an optional alternative."
            )
    raise RenderError(f"Unknown render engine {engine!r}.")


def write_html(html: str, out_html: str) -> str:
    with open(out_html, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_html


def build_context(result: Dict, cfg: Config) -> Dict:
    """Translate the pipeline result dict into Anna's `context` object.

    `result` is the dict produced by pipeline.run_scorecard(): it carries
    project meta, baseline rows, bidder mechanical rows, section C rows, scores,
    ranking, and the overall column.
    """
    meta = result["meta"]
    bidders = result["bidders"]  # ordered as supplied (matrix order)
    ranking = result["ranking"]  # ordered best->worst

    # ---- summary chips ----
    band = result["baseline"]["band_value"]
    top2 = [r["name"] for r in ranking[:2]]
    value_tier = next((r for r in ranking if r["tier"] == "MID"), None)

    # ---- Section B ----
    bid_rows = []
    for b in bidders:
        bid_rows.append({
            "bidder": b["name"],
            "total": fmt_money(b["total"]),
            "per_sf": b["per_sf"],
            "tier_label": TIER_LABELS[b["tier"]],
            "tier_class": TIER_CSS[b["tier"]],
            "quick_read": b["quick_read"],
        })

    # ---- Section C ----
    outcome_rows = []
    best_names = {r["name"] for r in ranking[:2]}
    for b in bidders:
        c = b["section_c"]
        outcome_rows.append({
            "bidder": b["name"],
            "bid": f"${b['bid_m']:.2f}M",
            "variance": c["variance_text"],
            "expected": c["expected_text"],
            "volatility": c["volatility_text"],
            "interpretation": c["interpretation"],
            "row_class": "row-best" if b["name"] in best_names else "",
        })

    # ---- Section E ----
    # Columns are DATA-DRIVEN from the run's categories (the scoring-framework
    # xlsx when supplied, else the legacy canonical 8) — never hardcoded here.
    categories = result["categories"]
    score_columns = [{"name": c["short_label"],
                      "weight": f"{c['weight_pct']:g}%"}
                     for c in categories]
    score_rows = []
    for b in bidders:
        scores = [fmt_score_cell(b["scores"].get(c["key"])) for c in categories]
        overall_display = b["overall"]["display"]
        score_rows.append({
            "firm": b["name"],
            "scores": scores,
            "overall": overall_display,
            "overall_class": overall_css(b["overall"]["numeric"], cfg),
        })

    # ---- Section F ----
    # Draft placeholder markers in qualitative-note INPUTS are scaffolding only;
    # strip them so no raw "(placeholder — confirm)" copy reaches the board
    # (Floyd C1). A bullet that is nothing but a placeholder is dropped.
    def _clean_bullets(items):
        out = []
        for s in items:
            cleaned = _strip_placeholder(s)
            if cleaned:
                out.append(cleaned)
        return out

    contractor_summaries = []
    for r in ranking:
        contractor_summaries.append({
            "name": r["name"],
            "rank": r["rank"],
            "strengths": _clean_bullets(r["strengths"]),
            "risks": _clean_bullets(r["risks"]),
        })

    # ---- Section G ----
    hierarchy_items = [{"n": r["rank"], "name": r["name"], "top3": r["rank"] <= 3}
                       for r in ranking]

    return {
        "project_name": meta["project_name"],
        "subtitle": meta["subtitle"],
        "cost_band_label": (f"Modeled Cost Band (Takeoff + "
                            f"{cfg.run.region} trade pricing)"),
        "cost_band_value": band,
        "top_tier_label": "Top Tier (Takeoff-aligned)",
        "top_tier_value": " ".join(f"{i+1}) {n}" for i, n in enumerate(top2)),
        "value_tier_label": "Value Tier",
        "value_tier_value": (f"{value_tier['rank']}) {value_tier['name']} "
                             f"(moderate savings, higher volatility)"
                             if value_tier else "—"),
        "section_a_title": result["baseline"]["title"],
        "baseline_rows": result["baseline"]["rows"],
        "baseline_subtotals": result["baseline"]["subtotals"],
        "baseline_band": result["baseline"]["band_row"],
        "section_b_title": (f"B. Falke Matrix – Bid Totals Normalized "
                            f"($/SF uses {cfg.run.sf_basis:,.0f} SF)"),
        "bid_rows": bid_rows,
        "section_c_title": ("C. Probability-Adjusted Outcome "
                            "(Expected Final Cost + Volatility Band) — MODELED, "
                            "calibrated to original presentation"),
        "outcome_rows": outcome_rows,
        "section_d_title": "D. Scoring Framework (Categories and Weights)",
        "framework_rows": result["framework_rows"],
        "section_e_title": ("E. Detailed Category Scores (1–10; weights in "
                            "header). " + result["overall_label"]),
        "score_columns": score_columns,
        "score_rows": score_rows,
        "section_f_title": "F. Contractor Summaries (Strengths vs Risk Flags)",
        "contractor_summaries": contractor_summaries,
        "section_g_title": "G. Final Hierarchy (Takeoff-Validated)",
        "hierarchy_items": hierarchy_items,
        "disclosures_title": "H. Disclosures & Data Notes",
        "disclosures": build_disclosures(result, cfg),
        # consumed-sheet disclosure (Marvin P0-7 ruling): renders on the card
        # UNCONDITIONALLY, in every mode — even a leaked draft carries it.
        "sheet_disclosure": (result.get("sheet") or {}).get("disclosure", ""),
        "footer_note": meta["footer_note"],
    }
