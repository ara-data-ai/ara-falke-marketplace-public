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
from urllib.parse import quote

from .config import Config
from .errors import RenderError
from .exit_codes import watermark_headline
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
    """1-10 cell; NOT YET SCORED -> a visibly EMPTY cell.

    It used to render "—*". Marvin P1-2 §2.3 is explicit that blanks must be
    "visibly blank (not 0, not — in a way that reads as a value)" — and he is
    right that a dash-plus-asterisk is a value: it sits in the column looking
    like a score of some kind, and a reader scanning the grid cannot tell it
    from a real mark without hunting for the legend. An empty cell is
    unambiguous, and the row's own Overall cell carries the count of what is
    outstanding.
    """
    return str(score) if score is not None else ""


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

    # (e) Framework basis: how the evaluation PLAN was set, RENDERED FROM THE
    # DECLARATION — never hard-coded. That is F1's lesson, which cost a P0 to
    # learn: three unconditional, mutually contradictory claims about one
    # empirical fact shipped on a board document because the language was baked
    # into the template instead of keyed to a declared input. So: the tool says
    # only what the owner declared, and when nothing was declared it says nothing.
    framework_basis = build_framework_basis_note(result)

    return {
        "completeness": completeness,
        "div_band_low": DIV_BAND[0],
        "div_band_high": DIV_BAND[1],
        "duplicates": duplicates,
        "fingerprints": fingerprints,
        "provisional": provisional,
        "framework_basis": framework_basis,
        # convenience flag: render the section only if SOMETHING needs surfacing
        "any": bool(completeness or duplicates or fingerprints or provisional
                    or framework_basis),
    }


def watermark_background_uri(headline: str, tokens: List[str]) -> str:
    """A tiled, rotated SVG data URI — the watermark layer that reaches EVERY page.

    THIS IS THE CONTROL, and the mechanism is not cosmetic preference (Marvin
    P1-2 §2.4: "a page-wide diagonal watermark on every page, not a header
    badge. A badge is a caveat wearing a watermark's costume; it crops off with
    the header.").

    The obvious implementation — a `position: fixed` diagonal — DOES NOT WORK,
    and it fails silently in the worst possible direction. I built it that way
    first and drove a real 4-page PDF through Chrome: PRELIMINARY appeared on
    page 1 and on no other page. It looked right in the HTML, it looked right on
    page 1 of the PDF, and pages 2-4 were clean-looking board content. That is
    precisely the artifact this item exists to prevent, produced by the control
    meant to prevent it.

    What does work, verified the same way: a repeating background-image on the
    ROOT element. Chromium propagates the root background to the page canvas and
    paints it on every printed page. So the mark tiles down the whole document,
    several per page — which also means no single crop removes it.

    The tile carries the composed headline itself, not just the word: the
    diagonal is the layer that survives, so it has to say WHY, not merely that.

    ON THE OPACITY. It is tuned against a REAL rendered page, not by taste. The
    card is panel-dense — Sections F/H carry opaque fills — so the mark is
    occluded wherever a panel sits and only reads in the gaps between them. A
    wash faint enough to be tasteful is a wash a reader can miss on a busy page,
    and "the reader had to hunt for it" fails the only test that matters here:
    is the card safe when read WRONG? So it is pitched to be unmissable in the
    gaps while staying legible-through — checked page by page on a 4-page PDF.
    """
    tokens_line = " · ".join(tokens).upper()
    # Escaped for XML, then percent-encoded for the data URI. Both matter: firm
    # names never reach here, but the tokens are ours and & would still break it.
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="620" height="260">'
        '<g transform="rotate(-24 310 130)">'
        '<text x="310" y="126" text-anchor="middle" '
        'font-family="Helvetica,Arial,sans-serif" font-size="58" '
        'font-weight="bold" fill="rgba(179,38,30,0.28)">PRELIMINARY</text>'
        f'<text x="310" y="158" text-anchor="middle" '
        'font-family="Helvetica,Arial,sans-serif" font-size="19" '
        'font-weight="bold" letter-spacing="2" '
        f'fill="rgba(179,38,30,0.34)">{esc(tokens_line)}</text>'
        '</g></svg>'
    )
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


def _terminated(text: str) -> str:
    """End a free-text operator note with a sentence terminator (Floyd F-4).

    The ruling note is interpolated mid-paragraph on the card's most sensitive
    line, so an operator who omits the final period produces
    "…on 2026-06-10 Weights set after bid opening…" — two sentences run
    together on the one line the board is meant to weigh most carefully.
    """
    stripped = str(text or "").strip()
    if not stripped:
        return ""
    return stripped if stripped[-1] in ".!?" else stripped + "."


def build_framework_basis_note(result: Dict) -> str:
    """The framework-basis disclosure (Marvin §4.5) — the card AND the summary.

    TWO INDEPENDENT FACTS, COMPOSED — never one substituted for the other
    (Floyd C-1, 2026-07-16). This function previously returned early on the W8
    branch and swallowed the operator's declaration with it. Because Falke has
    no standing framework on file, W8 fires on EVERY run today — so an operator
    who did the honest thing and declared a post-opening re-weighting got a
    board packet in which that fact appeared in no human-readable artifact.
    That is the F1 failure class the whole P0 program existed to close: the card
    silent about something it knows. Worse, it punished precisely the operator
    the declaration model exists to reward.

      1. The standing-framework CLAIM is CONDITIONAL. Without a reference on
         file the card must not claim one exists — it cannot say what it does
         not know.
      2. The operator's own DECLARATION is NOT conditional. It has nothing to
         do with the missing reference, and `revised-post-opening` is ruled
         "mandatory, unconditional, on the card."

    The two are independent and both get said. The only reason `standing` and
    `project-specific` stay silent under W8 is that their §4.5 texts each
    ASSERT something about the standing framework ("applied unmodified", "they
    differ from Falke's standing framework") and neither sentence can be said
    without one. `revised-post-opening` references no standing framework at all,
    so it composes cleanly and always renders.
    """
    pack = result.get("pack")
    if pack is None:
        return ""

    parts = []
    # (1) the conditional standing-framework claim
    if not pack.standing_available:
        parts.append("No standing evaluation framework was on file for this "
                     "run; the categories and weights below were supplied for "
                     "this project.")
    # (2) the operator's declaration — independent of (1)
    parts.append(_declaration_sentence(pack))
    return " ".join(p for p in parts if p)


def _declaration_sentence(pack) -> str:
    """The §4.5 text for the declared basis, suppressed CLAUSE BY CLAUSE.

    Floyd's governing rule (C-3, 2026-07-16), verbatim:

        Suppress only clauses that assert a fact the tool does not have. Never
        suppress a clause that carries a fact the operator declared. Clause
        level, not branch level.

    My first pass applied it at BRANCH level and dropped `project-specific`
    whole when no standing framework was on file. Wrong, for three reasons
    worth keeping written down:

      * The W8 sentence is BASIS-INDEPENDENT — it renders identically for
        `standing` and `project-specific` — so it cannot be carrying the
        declaration's substance. "Supplied for this project" speaks to the
        provenance of the CONTENT; the two-clock doctrine is entirely about
        WHEN, and the lock date is the only thing that says when.
      * W3 is a BLOCKER keyed on the lock date. Auditing a date and then
        omitting it from the artifact the audit protects is incoherent.
      * Bias: if the adverse declaration composes and the favourable one does
        not, the card only ever speaks when there is bad news — which makes its
        silence ambiguous rather than reassuring.

    Applying the rule to each branch's clauses:

    | branch                | clause                          | W8 disposition |
    |-----------------------|---------------------------------|----------------|
    | standing              | "is Falke's standing framework, | SUPPRESS — the |
    |                       |  applied unmodified"            | whole text is  |
    |                       |                                 | the claim      |
    | project-specific      | "locked on <date>, before bids  | RENDER —       |
    |                       |  were opened"                   | operator fact  |
    |                       | "they differ from Falke's       | SUPPRESS —     |
    |                       |  standing framework"            | compares to    |
    |                       |                                 | nothing        |
    |                       | "recorded in the award file"    | RENDER         |
    | revised-post-opening  | (all clauses)                   | RENDER — makes |
    |                       |                                 | no claim about |
    |                       |                                 | a standing fw  |
    """
    basis = pack.framework_basis
    has_standing = pack.standing_available

    # MANDATORY, UNCONDITIONAL, ON THE CARD (§4.5). Every clause is
    # operator-declared and none references a standing framework, so the whole
    # text survives W8. This is THE risk disclosure of the declaration model.
    if basis == "revised-post-opening":
        return (f"Evaluation categories and weights were revised after bids "
                f"were opened. {_terminated(pack.framework_ruling_note)} "
                f"Weights set after bid opening are disclosed here so the "
                f"board can weigh them accordingly.")

    if basis == "project-specific":
        if has_standing:
            return (f"Evaluation categories and weights were set for this "
                    f"project and locked on {pack.framework_lock_date}, before "
                    f"bids were opened. They differ from Falke's standing "
                    f"framework; the ruling is recorded in the award file.")
        # W8: the lock date and the pre-opening claim RENDER; the "differ from"
        # comparison does not (there is nothing on file to differ from); the
        # award-file pointer does.
        #
        # Wording note: the naive composition stutters — the W8 sentence
        # already said "supplied for this project", so leading with "set for
        # this project" repeats it and buries the one fact this clause exists
        # to carry, which is WHEN. So it leads with the date. "They" takes its
        # antecedent from the W8 sentence ("the categories and weights below"),
        # which build_framework_basis_note always emits immediately before this
        # under W8 — if that order ever changes, this pronoun dangles.
        return (f"They were locked on {pack.framework_lock_date}, before bids "
                f"were opened; the ruling is recorded in the award file.")

    if basis == "standing":
        # The entire sentence is a standing-framework claim — "applied
        # unmodified" relative to WHAT? Suppressed whole, correctly.
        if not has_standing:
            return ""
        return (f"Evaluation categories and weights are Falke's standing "
                f"evaluation framework (version {pack.standing_version}, "
                f"effective {pack.standing_effective_date}), applied "
                f"unmodified.")
    return ""


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


def build_context(result: Dict, cfg: Config, *,
                  watermark: Optional[List[Dict]] = None) -> Dict:
    """Translate the pipeline result dict into Anna's `context` object.

    `result` is the dict produced by pipeline.run_scorecard(): it carries
    project meta, baseline rows, bidder mechanical rows, section C rows, scores,
    ranking, and the overall column.

    `watermark` is exit_codes.resolve_watermark()'s reason list — a LIST, never
    a boolean (Marvin P1-2 §2.4). Default None = a clean run, and a clean run
    has no watermark node in the DOM at all. The caller (cli.py) resolves it
    from the audit verdict, which is why the audit now runs BEFORE the render.
    """
    meta = result["meta"]
    bidders = result["bidders"]  # matrix order, or ALPHABETICAL when provisional
    ranking = result["ranking"]  # best->worst, or alphabetical-and-unranked

    # ---- PROVISIONAL: every ranking claim on this card is withheld ---------
    # (Marvin P1-2 §3.3). Not softened, not asterisked — ABSENT. There are five
    # distinct places this card asserts an order, and a caveat in Section H
    # protects none of them from a phone photo:
    #   1. the "Top Tier" chip           (names the leaders)
    #   2. the "Value Tier" chip         (names a rank)
    #   3. Section C's top-2 row tint    (a rank claim in visual form —
    #                                     highlighting IS asserting)
    #   4. Section F's "(Rank #N)"       (per-bidder rank label)
    #   5. Section G "Final Hierarchy"   (the ranking itself)
    # Sections A/B/C keep every mechanical fact: the baseline, $/SF, tiers and
    # variance are fully known regardless of scoring, and suppressing them would
    # be the tool pretending not to know something it knows — the C-1 error with
    # the sign flipped. It is also most of what makes the provisional card
    # genuinely useful, which is what keeps the honest path from being the
    # useless path.
    full_coverage = bool(result.get("full_coverage", True))

    # ---- summary chips ----
    band = result["baseline"]["band_value"]
    top2 = [r["name"] for r in ranking[:2]] if full_coverage else []
    value_tier = (next((r for r in ranking if r["tier"] == "MID"), None)
                  if full_coverage else None)

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
    # tinting the top two IS a ranking claim, just made in colour rather than
    # in words — and colour survives a screenshot better than prose does.
    best_names = {r["name"] for r in ranking[:2]} if full_coverage else set()
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

    # Section F survives provisional: strengths/risks are per-bidder FACTS, and
    # the board-readiness of a fact does not depend on its neighbours. Only the
    # rank LABEL goes (r has no "rank" key at partial coverage).
    contractor_summaries = []
    for r in ranking:
        contractor_summaries.append({
            "name": r["name"],
            "rank": r.get("rank"),
            "strengths": _clean_bullets(r["strengths"]),
            "risks": _clean_bullets(r["risks"]),
        })

    # ---- Section G — the Final Hierarchy IS the ranking, so at partial
    # coverage there is nothing to render. Empty list; the template skips the
    # whole section rather than printing an empty heading that invites the
    # reader to wonder what was removed.
    hierarchy_items = ([{"n": r["rank"], "name": r["name"], "top3": r["rank"] <= 3}
                        for r in ranking] if full_coverage else [])

    return {
        "project_name": meta["project_name"],
        "subtitle": meta["subtitle"],
        "cost_band_label": (f"Modeled Cost Band (Takeoff + "
                            f"{cfg.run.region} trade pricing)"),
        "cost_band_value": band,
        "top_tier_label": ("Top Tier (Takeoff-aligned)" if full_coverage
                           else "Evaluation status"),
        "top_tier_value": (" ".join(f"{i+1}) {n}" for i, n in enumerate(top2))
                           if full_coverage else "In progress — not ranked"),
        "value_tier_label": "Value Tier",
        "value_tier_value": (f"{value_tier['rank']}) {value_tier['name']} "
                             f"(moderate savings, higher volatility)"
                             if value_tier else "—"),
        # §3.3.3 — alphabetical is the only order that carries no claim, AND
        # ANNOUNCES that it carries none. Saying so is half the control: an
        # unexplained order still invites the top row to be read as the leader.
        "listing_note": ("" if full_coverage else
                         "Listed alphabetically — not ranked. Ranking requires "
                         "a complete qualitative record."),
        "coverage": result.get("coverage") or {},
        "full_coverage": full_coverage,
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
        # PRELIMINARY watermark (P1-1). Empty list = clean run = no mark, and
        # no watermark CSS/DOM node at all — a clean card is byte-identical to
        # what it was before this item.
        "watermark": watermark or [],
        "watermark_headline": watermark_headline(watermark or []),
        "watermark_bg": (
            watermark_background_uri(
                watermark_headline(watermark),
                [r["token"] for r in watermark])
            if watermark else ""),
        "footer_note": meta["footer_note"],
    }
