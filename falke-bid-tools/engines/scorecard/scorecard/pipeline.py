"""Top-level orchestration: matrix + parameters + (optional) qualitative inputs
-> a result dict ready for render.build_context().

Honors the owner's decisions:
  - Hybrid: auto-compute matrix facts; REQUIRE sf_basis + baseline band (hard stop).
  - Overall /100 IS the honest weighted average — nothing adjusts it. (The
    presentation curve was retired under P0-6, Floyd consolidated ruling
    verdict d: a device that re-orders the award ranking is scoring, not
    presentation.)
  - Qualitative scores LLM-drafted with human override; missing -> null + flag.
  - Sheet consumption is an explicit, ruled decision (Marvin P0-7): default =
    the leveled view; every card carries the consumed-sheet disclosure.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Dict, List, Optional

from .config import Config
from .errors import MissingParameterError, ScorecardError
from .matrix import (MatrixParser, ParsedMatrix, apply_display_aliases,
                     apply_exclusions, normalize_name)
from .mechanical import (TIER_LABELS, build_mechanical, fingerprint_test,
                         rank_bidders)
from .modeling import (expected_final_band, volatility_band)
from .narratives import (FRAMEWORK_ROWS, SHEET_DISCLOSURES, TIER_QUICK_READ,
                         merge_qualitative_notes, section_c_interpretation)
from .scoring import (CATEGORY_DISPLAY, CATEGORY_ORDER, apply_overrides,
                      build_bidder_scores)
from .scoring_inputs import build_scores_from_inputs


def _fmt_weight_pct(weight: float) -> str:
    """25.0 -> '25%', 12.5 -> '12.5%'."""
    return f"{weight:g}%"


def _crosscheck_scored_firms(parsed: ParsedMatrix, category_scores: Dict) -> None:
    """Every SCORED (included) bidder must have a scores row, and every scores
    row must name a scored bidder — a row for an unknown or EXCLUDED bidder is
    an error. Matched on the same display-name normalization the matrix parser
    uses (normalize_name over the displayed bidder name)."""
    included = [b.name for b in parsed.included_blocks]
    included_norm = {normalize_name(n): n for n in included}
    scores_norm = {normalize_name(f): f for f in category_scores}
    missing = [n for k, n in included_norm.items() if k not in scores_norm]
    unknown = [f for k, f in scores_norm.items() if k not in included_norm]
    if missing or unknown:
        parts = ["Category Scores do not match the scored bidder field."]
        if missing:
            parts.append(
                "Scored bidder(s) WITHOUT a scores row: "
                + ", ".join(sorted(missing)) + ".")
        if unknown:
            parts.append(
                "Scores row(s) for unknown/excluded bidder(s): "
                + ", ".join(sorted(unknown)) + ".")
        parts.append(
            "The scores file needs exactly one row per SCORED bidder "
            "(firm names as displayed on the card; excluded bidders must "
            "not appear). Scored field: " + ", ".join(sorted(included)) + ".")
        raise ScorecardError(" ".join(parts))


def _fmt_variance(variance_m: float) -> str:
    sign = "+" if variance_m >= 0 else "−"
    return f"{sign}${abs(variance_m):.2f}M"


def _fmt_vol(low: float, high: float) -> str:
    return f"{low:.0f}–{high:.0f}%"


def _fmt_expected(center: float, low: float, high: float, headroom: float) -> str:
    # near-zero headroom (over-baseline) collapses to a "~$X.XXM" point
    if (high - low) <= 0.051 and headroom <= 0.001:
        return f"~${center:.2f}M"
    return f"${low:.2f}–${high:.2f}M"


def run_scorecard(
    xlsx_path: str,
    cfg: Config,
    *,
    baseline_lines: Optional[List[Dict]] = None,
    qualitative_notes: Optional[Dict[str, Dict]] = None,
    overrides: Optional[Dict[str, Dict]] = None,
    exclude: Optional[List[str]] = None,
    aliases: Optional[Dict[str, str]] = None,
    project_name: Optional[str] = None,
    framework: Optional[List[Dict]] = None,
    category_scores: Optional[Dict[str, Dict]] = None,
    sheet: Optional[str] = None,
) -> Dict:
    """Run the full pipeline. Returns a result dict + run log.

    baseline_lines: the nine Section A trade lines (+subtotals/band) as PARAMETER
        input: [{"scope","basis","cost"(display)}, ...]. Numeric 'value' keys
        (if present) feed the fingerprint test.
    qualitative_notes: {bidder_name: {"strengths":[...], "risks":[...]}}.
    overrides: {bidder_name: {category: {"score","by","note"}}} — keyed by the
        DISPLAYED bidder name (after any alias is applied), so the short display
        names ('Acme', 'Granite', ...) match.
    exclude: optional list of bidder names to REMOVE from the scored field per a
        human ruling (matched on normalized name). Default behavior remains
        include-all-and-flag; this only fires when a ruling is supplied (e.g. to
        apply a set-aside ruling). Each exclusion is logged.
    aliases: optional display-name map (raw/normalized firm name -> short display
        name). Merged over config['aliases'] (caller wins). Applied to the shown
        bidder name; the raw matrix name is retained in the log for audit
        (Marvin §1.5). Default empty (no rename).
    framework / category_scores: the parsed per-run scoring xlsx inputs
        (scoring_inputs.parse_scoring_framework / parse_category_scores). When
        supplied (the CLI REQUIRES them for every render) they are the SINGLE
        SOURCE OF TRUTH for category weights and 1–10 scores: config['weights']
        and the `overrides` qual-scores channel are superseded, Sections D/E
        render dynamically from the framework, and coverage is 100% by
        construction. Supplying `overrides` together with them is an error (no
        second source of scores). The legacy scaffold path (mechanical seeds +
        overrides + config weights) remains ONLY for direct programmatic
        callers that omit both.
    sheet: optional EXPLICIT sheet selection (CLI --sheet). Overrides the
        config matrix.sheet_name for this run. Default (None) resolves per
        Marvin's P0-7 ruling: Leveled_Normalized when present; a single-sheet
        legacy workbook uses its only sheet; a producer workbook missing the
        leveled view hard-stops. The consumed sheet + mode + the mandatory
        on-card disclosure are recorded on result['sheet'].
    """
    # ---- scoring-inputs contract (xlsx = single source of truth) ----
    if (framework is None) != (category_scores is None):
        raise MissingParameterError(
            "Scoring inputs must be supplied TOGETHER: a scoring framework AND "
            "category scores (CLI: --scoring-framework + --category-scores). "
            "There is no default for either."
        )
    if framework is not None and overrides:
        raise ScorecardError(
            "overrides (qual-scores JSON) is superseded — with a scoring "
            "framework + category scores supplied, the xlsx files are the "
            "SINGLE source of category weights and 1–10 scores. Put the scores "
            "in the category-scores xlsx instead."
        )
    # project_name is a REQUIRED parameter — never silently inherit a prior
    # project's name onto a board deliverable (owner's decision; no defaults).
    if not project_name or not project_name.strip():
        raise MissingParameterError(
            "project_name is required — supply it explicitly (CLI: --project-name). "
            "The skill STOPS rather than inheriting another project's name."
        )

    run_id = uuid.uuid4().hex[:12]
    log: List[str] = [f"run_id={run_id} ts={_dt.datetime.utcnow().isoformat()}Z"]

    # ---- 1. PARSE (generic detection) ----
    matrix_cfg = dict(cfg.block("matrix"))
    if sheet:
        matrix_cfg["sheet_name"] = sheet   # explicit CLI --sheet wins
    parser = MatrixParser(matrix_cfg)
    parsed: ParsedMatrix = parser.parse(
        xlsx_path,
        peer_fraction=cfg.block("qa").get("completeness_peer_fraction", 0.5),
    )
    # ---- 1a0. DISPLAY ALIASES (optional; default empty) ----
    # Rewrite the SHOWN bidder name to the board-card short names BEFORE anything
    # downstream keys off it (overrides lookup, ranking, render). The
    # raw matrix name is retained on the block + logged for audit (Marvin §1.5).
    # Caller-supplied aliases win over the config block.
    alias_map: Dict[str, str] = dict(cfg.block("aliases") or {})
    if aliases:
        alias_map.update(aliases)
    apply_display_aliases(parsed, alias_map)
    # ---- 1a. APPLY human exclusion ruling (optional; default include-all) ----
    # The parser FLAGS completeness/duplicate outliers (Marvin §1.4) but never
    # auto-drops a non-duplicate bidder. This is the explicit channel to APPLY
    # a set-aside ruling so the curated field reproduces. Logged per exclusion.
    apply_exclusions(parsed, exclude)
    log.extend(parsed.log)

    # ---- 1b. SCORING INPUTS (xlsx single source of truth) or legacy scaffold --
    # With a framework: weights + Section D rows + Section E columns all come
    # from the framework file; scores come from the category-scores file; the
    # scores firms must match the SCORED (included) field exactly.
    if framework is not None:
        _crosscheck_scored_firms(parsed, category_scores)
        weights_in_use = {f["key"]: f["weight"] / 100.0 for f in framework}
        categories_meta = [{"key": f["key"], "short_label": f["short_label"],
                            "weight_pct": f["weight"]} for f in framework]
        framework_rows = [{"category": f["category"],
                           "weight": _fmt_weight_pct(f["weight"]),
                           "captures": f["description"]} for f in framework]
        scores_by_norm = {normalize_name(firm): s
                          for firm, s in category_scores.items()}
        # ranking tiebreak category: the pricing-style category when the
        # framework carries one, else the framework's lead category.
        pricing_key = next((f["key"] for f in framework
                            if "pricing" in f["key"]), framework[0]["key"])
        log.append(
            f"SCORING INPUTS: framework={len(framework)} categories "
            f"(weights sum {sum(f['weight'] for f in framework):g}%); "
            f"category scores for {len(category_scores)} firm(s). The xlsx "
            f"inputs are the single source of weights/scores (config weights "
            f"and overrides bypassed)."
        )
    else:
        weights_in_use = cfg.weights
        categories_meta = [{"key": k, "short_label": CATEGORY_DISPLAY[k],
                            "weight_pct": cfg.weights[k] * 100.0}
                           for k in CATEGORY_ORDER]
        framework_rows = FRAMEWORK_ROWS
        scores_by_norm = None
        pricing_key = "pricing"

    # ---- 2. MECHANICAL ----
    mech = build_mechanical(parsed, cfg)
    if not mech:
        raise RuntimeError("No included bidders after parsing — cannot score.")
    peer_median_div = sorted(parsed.included_blocks,
                             key=lambda b: b.populated_divisions)[
        len(parsed.included_blocks) // 2].populated_divisions or 1

    # ---- 3. QA fingerprint test (Marvin §2.2) ----
    fingerprints = []
    if baseline_lines:
        numeric_lines = [{"label": r.get("scope", "?"), "value": r["value"]}
                         for r in baseline_lines if "value" in r]
        if numeric_lines:
            fingerprints = fingerprint_test(numeric_lines, parsed, xlsx_path, cfg)
            for h in fingerprints:
                log.append(
                    f"FINGERPRINT: baseline '{h.baseline_label}' "
                    f"({h.baseline_value:,.0f}) ~ {h.bidder_name} "
                    f"'{h.bidder_subtotal_label}' ({h.bidder_value:,.0f}), "
                    f"delta {h.pct_delta:.3f}% — possible bid-anchoring; "
                    f"board disclosure item (Marvin §2.2)."
                )

    # ---- 4. MODELING: Section C + scoring scaffold per bidder ----
    vol_cfg = cfg.block("volatility")
    drift_cfg = cfg.block("drift")
    variance_mid = cfg.run.variance_mid
    band_high_per_sf = cfg.run.band_high_per_sf

    # INTEGRATION POINT: at invocation time, construct a live LLMScorer (see
    # scoring.LLMScorer) over the bidder PDFs to draft the four external
    # categories. Without one, build_bidder_scores degrades them to null+flag
    # (NotImplementedLLMScorer documents the default no-op contract). Overrides
    # below apply the human-confirmed/analyst values.
    bidders_out: List[Dict] = []
    rank_input: List[Dict] = []

    for m in mech:
        v = m.variance_frac
        vol_central, vol_low, vol_high = volatility_band(v, vol_cfg)
        exp_center, exp_low, exp_high = expected_final_band(
            m.bid_m, v, variance_mid, drift_cfg, vol_central)
        target = variance_mid * (1.0 + drift_cfg["buffer"])
        headroom = max(0.0, target - m.bid_m)

        if framework is not None:
            # scores come SOLELY from the category-scores xlsx (cross-checked
            # above, so the lookup is guaranteed to resolve).
            firm_scores = scores_by_norm[normalize_name(m.name)]
            scores = build_scores_from_inputs(
                m.name, framework, firm_scores, run_id=run_id)
        else:
            # legacy scaffold (mechanical seeds + degradation + overrides)
            block = next(b for b in parsed.included_blocks if b.name == m.name)
            completeness_ratio = (block.populated_divisions / peer_median_div
                                  if peer_median_div else 1.0)
            scores = build_bidder_scores(
                m.name, m.tier, vol_central,
                populated_divisions=block.populated_divisions,
                peer_median=peer_median_div,
                completeness_ratio=completeness_ratio,
                cfg=cfg, run_id=run_id,
            )
            if overrides and m.name in overrides:
                apply_overrides(scores, overrides[m.name])

        # ---- Overall /100 = the HONEST weighted average, nothing else.
        # (Presentation curve + tier_bonus retired under P0-6: a device that
        # can re-order the award ranking is scoring, not presentation.)
        wa = scores.weighted_average_x10(weights_in_use)
        wavg = wa["wavg"]
        overall_numeric = round(wavg, 1) if wavg is not None else None
        if wavg is None:
            overall_display = "—*"
        elif wa["coverage"] < 0.999:
            overall_display = (f"{wavg:.0f}* (prov., "
                               f"{wa['coverage']*100:.0f}% coverage)")
        else:
            overall_display = f"{wavg:.0f}"

        quick_read = TIER_QUICK_READ[m.tier]
        bidder = {
            "name": m.name,
            "total": m.total,
            "per_sf": m.per_sf,
            "tier": m.tier,
            "bid_m": m.bid_m,
            "quick_read": quick_read,
            "flags": m.flags,
            "section_c": {
                "variance_text": f"{_fmt_variance(m.variance_m)} vs ${variance_mid:.2f}M mid",
                "expected_text": _fmt_expected(exp_center, exp_low, exp_high, headroom),
                "volatility_text": _fmt_vol(vol_low, vol_high),
                "interpretation": section_c_interpretation(m.tier),
                "vol_central": vol_central,
                "exp_center": exp_center,
            },
            "scores": {c["key"]: scores.categories[c["key"]].effective_score
                       for c in categories_meta},
            "scores_detail": scores,
            "overall": {
                "numeric": overall_numeric,
                "display": overall_display,
                "weighted_average": overall_numeric,
                "coverage": wa["coverage"],
            },
        }
        bidders_out.append(bidder)
        rank_input.append({
            "name": m.name,
            "overall": overall_numeric,
            "pricing_score": scores.categories[pricing_key].effective_score,
            "per_sf_over_band": max(0.0, m.per_sf - band_high_per_sf),
            "tier": m.tier,
        })

    # ---- 5. RANKING (Marvin §9) ----
    ranked = rank_bidders(rank_input)
    rank_lookup = {r["name"]: i + 1 for i, r in enumerate(ranked)}
    for b in bidders_out:
        b["rank"] = rank_lookup[b["name"]]

    ranking_out = []
    for r in ranked:
        b = next(bb for bb in bidders_out if bb["name"] == r["name"])
        qn = (qualitative_notes or {}).get(r["name"])
        bullets = merge_qualitative_notes(b["tier"], qn)
        # surface completeness/duplicate flags as risk bullets
        for f in b["flags"]:
            bullets["risks"].append(f)
        ranking_out.append({
            "name": r["name"], "rank": rank_lookup[r["name"]], "tier": b["tier"],
            "strengths": bullets["strengths"], "risks": bullets["risks"],
        })

    # ---- 6. coverage summary + provenance ----
    full_coverage = all(b["overall"]["coverage"] >= 0.999 for b in bidders_out)
    if full_coverage:
        overall_label = "Overall = honest weighted average (deterministic)."
    else:
        overall_label = ("Overall = honest weighted average (PROVISIONAL — "
                         "qualitative coverage below 100%).")

    # ---- baseline (Section A) — PARAMETER, passed through ----
    baseline = _build_baseline_block(cfg, baseline_lines)

    result = {
        "meta": {
            "run_id": run_id,
            "project_name": project_name,
            "matrix_path": xlsx_path,
            "subtitle": ("Presentation-Enhanced Takeoff + Bid Comparison + "
                         "Scoring & Rankings (modeled; no matrix data changes)"),
            "footer_note": (f"Falke Corp · prepared by ARA · "
                            f"{_dt.date.today().isoformat()} · base/grand-total "
                            f"construction cost only · SF basis "
                            f"{cfg.run.sf_basis:,.0f} · sheet "
                            f"{parsed.sheet_name} ({parsed.sheet_mode}) · "
                            f"run {run_id}"),
        },
        # consumed-sheet provenance (Marvin P0-7): name, mode, and the
        # mandatory board-facing disclosure line — rendered ON the card and
        # recorded in scorecard_run.json.
        "sheet": {
            "name": parsed.sheet_name,
            "mode": parsed.sheet_mode,
            "disclosure": SHEET_DISCLOSURES[parsed.sheet_mode],
        },
        "baseline": baseline,
        "bidders": bidders_out,
        "ranking": ranking_out,
        "framework_rows": framework_rows,
        "categories": categories_meta,
        "overall_label": overall_label,
        "full_coverage": full_coverage,
        "fingerprints": fingerprints,
        "parsed": parsed,
        "log": log,
    }
    return result


def preview_baseline(
    xlsx_path: str,
    cfg: Config,
    *,
    baseline_lines: Optional[List[Dict]] = None,
    sheet: Optional[str] = None,
) -> Dict:
    """Build a human-readable ECHO of the supplied cost baseline + run the
    bid-anchoring fingerprint check, WITHOUT rendering a scorecard.

    Backs the CLI `--preview-baseline` mode: the owner SEES the baseline (the
    yardstick the whole scorecard hangs off) and CONFIRMS it before any card is
    built. Returns {"echo": [str, ...], "fingerprints": [FingerprintHit, ...]}.

    Reuses mechanical.fingerprint_test (the SAME detector the audit's C9 check
    relies on via the pipeline's `fingerprints`) — the 0.2%-tolerance math is
    NOT duplicated here.
    """
    run = cfg.run
    echo: List[str] = []
    echo.append("=== COST BASELINE PREVIEW (no scorecard rendered) ===")
    echo.append(f"region={run.region_full}  pricing_year={run.pricing_year}  "
                f"sf_basis={run.sf_basis:,.0f} SF")
    # Surface the SF-basis SOURCE so the owner can confirm or override it. When
    # the basis is the matrix's own Row-10 GSF (the SUGGESTED default), say so
    # explicitly and name the two ways to lock it for a render.
    if run.sf_source == "matrix-confirmed":
        echo.append(f"  SF basis source: SUGGESTED from matrix Row-10 'TOTAL GSF' "
                    f"({run.sf_basis:,.0f} SF). To render, re-run with "
                    f"--sf-confirmed to ACCEPT it, or --sf-basis <value> to "
                    f"OVERRIDE it.")
    else:
        echo.append(f"  SF basis source: EXPLICIT --sf-basis ({run.sf_basis:,.0f} "
                    f"SF).")
    echo.append("")

    # ---- trade-scope lines + subtotal lines (label + cost), in supplied order
    trade_lines = []
    subtotal_lines = []
    if baseline_lines:
        for r in baseline_lines:
            kind = r.get("kind")
            if kind == "band":
                continue
            label = r.get("scope", "?")
            cost = r.get("cost", "")
            if kind == "subtotal":
                subtotal_lines.append((label, cost))
            else:
                trade_lines.append((label, cost))

    if trade_lines:
        echo.append("Trade-scope lines:")
        for label, cost in trade_lines:
            echo.append(f"  - {label}: {cost}")
    else:
        echo.append("Trade-scope lines: (none supplied — no --baseline file)")
    echo.append("")
    if subtotal_lines:
        echo.append("Subtotals (direct-trades subtotal + OH&P):")
        for label, cost in subtotal_lines:
            echo.append(f"  - {label}: {cost}")
        echo.append("")

    # ---- band in $M and in $/SF (using sf_basis) ----
    mid_m = run.modeled_mid_takeoff
    echo.append("Modeled cost band:")
    echo.append(f"  ${run.band_low:.2f}M–${run.band_high:.2f}M "
                f"(mid ${mid_m:.2f}M)")
    echo.append(f"  ${run.band_low_per_sf:.0f}–${run.band_high_per_sf:.0f}/SF, "
                f"mid ${run.mid_per_sf:.0f}/SF  (sf_basis {run.sf_basis:,.0f})")
    echo.append("")

    # ---- bid-anchoring fingerprint check (SAME logic the audit uses) ----
    fingerprints = []
    if baseline_lines:
        matrix_cfg = dict(cfg.block("matrix"))
        if sheet:
            matrix_cfg["sheet_name"] = sheet
        parser = MatrixParser(matrix_cfg)
        parsed = parser.parse(
            xlsx_path,
            peer_fraction=cfg.block("qa").get("completeness_peer_fraction", 0.5),
        )
        numeric_lines = [{"label": r.get("scope", "?"), "value": r["value"]}
                         for r in baseline_lines if "value" in r]
        if numeric_lines:
            fingerprints = fingerprint_test(numeric_lines, parsed, xlsx_path, cfg)

    echo.append("Bid-anchoring fingerprint check:")
    if fingerprints:
        for h in fingerprints:
            echo.append(
                f"  ⚠ Baseline line '{h.baseline_label}' "
                f"(${h.baseline_value:,.0f}) matches bidder {h.bidder_name} "
                f"subtotal (${h.bidder_value:,.0f}) within {h.pct_delta:.2f}% — "
                f"this baseline may be bid-derived, not independent. "
                f"Confirm this is intended."
            )
    else:
        echo.append("  No bid-anchoring fingerprints detected.")

    return {"echo": echo, "fingerprints": fingerprints}


def _build_baseline_block(cfg: Config, baseline_lines: Optional[List[Dict]]) -> Dict:
    run = cfg.run
    band_value = (f"${run.band_low:.2f}M – ${run.band_high:.2f}M "
                  f"(${run.band_low_per_sf:.0f}–${run.band_high_per_sf:.0f}/SF)")
    rows = []
    subtotals = []
    if baseline_lines:
        for r in baseline_lines:
            entry = {"scope": r.get("scope", ""), "basis": r.get("basis", ""),
                     "cost": r.get("cost", "")}
            if r.get("kind") == "subtotal":
                subtotals.append(entry)
            elif r.get("kind") == "band":
                continue
            else:
                rows.append(entry)
    band_row = {"scope": "MODELED PROJECT COST BAND", "basis": "Takeoff + OH&P",
                "cost": band_value}
    return {
        "title": (f"A. Takeoff-Based Modeled Cost Baseline "
                  f"({run.region_full}, {run.pricing_year})"),
        "rows": rows,
        "subtotals": subtotals,
        "band_row": band_row,
        "band_value": band_value,
    }


def audit_run(
    result: Dict,
    cfg: Config,
    out_dir: str,
    *,
    aliases: Optional[Dict[str, str]] = None,
):
    """Run the deterministic self-audit AFTER artifacts are generated, and write
    audit_report.md + audit.json to out_dir (Marvin's rubric §4).

    Pure QA pass: re-derives the critical numbers/structural facts from the
    parsed matrix + run inputs and compares them to what the pipeline emitted.
    Returns (AuditResult, paths). Does NOT replace Floyd's gate — it is the
    in-flight per-run QA. The parsed matrix is carried on result["parsed"]; cfg
    is the run_inputs.
    """
    from .audit import audit as _audit
    from .audit import write_audit_artifacts

    parsed = result["parsed"]
    ar = _audit(parsed, cfg, result, aliases=aliases)
    paths = write_audit_artifacts(ar, parsed, cfg, result, out_dir)
    return ar, paths


def render_summary(
    result: Dict,
    out_dir: str,
    *,
    engine: str = "chromium",
    html_only: bool = False,
) -> Dict[str, str]:
    """Render the plain-English Scorecard Summary companion alongside the card.

    Derives Anna's summary context from the SAME run `result` (no recompute;
    see summary.build_summary_context), renders it through the shared Jinja env
    (autoescape on) and the SAME Chromium/Playwright PDF path as the scorecard,
    and writes scorecard_summary.html (+ scorecard_summary.pdf unless
    html_only) to out_dir. Returns the written paths.

    This is gated by the caller behind a real, confirmed render (never on
    --preview-baseline), so a normal render produces the scorecard, the audit,
    AND this summary as a matched set. Imports are local to keep the summary an
    optional companion that never weighs on the core pipeline import path.
    """
    import os as _os

    from .render import (SUMMARY_TEMPLATE_FILE, render_pdf, render_template,
                         write_html)
    from .summary import build_summary_context

    ctx = build_summary_context(result)
    html = render_template(SUMMARY_TEMPLATE_FILE, ctx)
    _os.makedirs(out_dir, exist_ok=True)
    base = _os.path.join(out_dir, "scorecard_summary")
    paths: Dict[str, str] = {"summary_html": write_html(html, base + ".html")}
    if not html_only:
        # Surface a render failure to the caller (same contract as the card):
        # NO silent fallback to a broken/absent PDF.
        paths["summary_pdf"] = render_pdf(html, base + ".pdf", engine=engine)
    return paths
