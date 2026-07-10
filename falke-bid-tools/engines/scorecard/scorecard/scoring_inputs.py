"""Parse the two REQUIRED per-run scoring xlsx inputs.

Every real render needs TWO Falke-filled files (NO fallback — the CLI
hard-stops with exit 2 without them; see cli.py):

  * the SCORING FRAMEWORK (scoring-framework-template.xlsx format) — the
    categories, weights, and descriptions that drive Section D and the
    Overall /100 weighting. Sheet ``Scoring_Framework``:
      Row 1: title
      Row 2: headers — Category | Short Label | Weight (%) | What it captures
      Row 3+: one category per row (stop at first fully-blank row)

  * the DETAILED CATEGORY SCORES (category-scores-template.xlsx format) — the
    per-bidder 1–10 scores that drive Section E. Sheet ``Category_Scores``:
      Row 1: title
      Row 2: headers — Firm | one column per framework Short Label
      Row 3+: one SCORED bidder per row (stop at first fully-blank row)
    The Overall /100 is COMPUTED by the engine and never supplied here.

These two files are the SINGLE SOURCE OF TRUTH for category weights and 1–10
scores: they supersede the config ``weights`` block and the old ``--overrides``
qual-scores JSON for any run that supplies them. Categories/weights may differ
per run — Sections D/E render dynamically from the framework.

All failures raise ValueError with a user-friendly, actionable message (the
CLI prints it as ``[STOP] ...`` and exits 2).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .matrix import normalize_name
from .scoring import BidderScores, CategoryScore

FRAMEWORK_SHEET = "Scoring_Framework"
SCORES_SHEET = "Category_Scores"

FRAMEWORK_HEADERS = ("Category", "Short Label", "Weight (%)", "What it captures")
SCORES_FIRM_HEADER = "Firm"

WEIGHT_SUM_TOL = 0.01
SCORE_MIN, SCORE_MAX = 1.0, 10.0


def _norm_header(s: Any) -> str:
    """Case/space-insensitive header key ('Short  Label' -> 'short label')."""
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _load_sheet(path: str, sheet: str, kind: str):
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise ValueError(
            "openpyxl is required to read xlsx scoring inputs "
            "(pip install openpyxl)."
        ) from exc
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        raise ValueError(f"Cannot open {kind} xlsx '{path}': {exc}") from exc
    if sheet not in wb.sheetnames:
        available = ", ".join(wb.sheetnames) if wb.sheetnames else "(none)"
        raise ValueError(
            f"Expected a sheet named '{sheet}' in '{path}'; found: {available}. "
            f"Use the shipped template so the sheet name matches."
        )
    return wb[sheet]


def parse_scoring_framework(path: str) -> List[Dict[str, Any]]:
    """Parse a scoring-framework xlsx.

    Returns a list of dicts, one per category, in sheet order:
      {"category", "short_label", "weight" (float, percent), "description",
       "key" (normalized short-label slug used internally)}

    Validates: >=1 category row; non-empty Category + Short Label; numeric
    weights summing to 100 (+/-0.01); no duplicate short labels.
    """
    ws = _load_sheet(path, FRAMEWORK_SHEET, "scoring-framework")

    # header row 2 sanity check (catch a file built off the wrong template)
    got = [_norm_header(ws.cell(row=2, column=c).value) for c in (1, 2, 3)]
    want = [_norm_header(h) for h in FRAMEWORK_HEADERS[:3]]
    if got != want:
        raise ValueError(
            f"'{path}': row 2 must carry the template headers "
            f"{' | '.join(FRAMEWORK_HEADERS)}; found "
            f"{[ws.cell(row=2, column=c).value for c in range(1, 5)]}. "
            f"Fill out scoring-framework-template.xlsx (do not reshape it)."
        )

    rows: List[Dict[str, Any]] = []
    r = 3
    while True:
        vals = [ws.cell(row=r, column=c).value for c in (1, 2, 3, 4)]
        if all(v is None or str(v).strip() == "" for v in vals):
            break
        category, short_label, weight, description = vals
        if category is None or str(category).strip() == "":
            raise ValueError(
                f"'{path}' row {r}: 'Category' (column A) is blank. Every "
                f"framework row needs a category name."
            )
        if short_label is None or str(short_label).strip() == "":
            raise ValueError(
                f"'{path}' row {r}: 'Short Label' (column B) is blank for "
                f"category '{str(category).strip()}'. The short label is what "
                f"the Category_Scores column headers must match."
            )
        try:
            weight_f = float(weight)
        except (TypeError, ValueError):
            raise ValueError(
                f"'{path}' row {r}: 'Weight (%)' (column C) must be numeric; "
                f"got {weight!r} for category '{str(category).strip()}'."
            )
        rows.append({
            "category": str(category).strip(),
            "short_label": str(short_label).strip(),
            "weight": weight_f,
            "description": str(description).strip() if description is not None else "",
            "key": normalize_name(str(short_label)),
        })
        r += 1

    if not rows:
        raise ValueError(
            f"No framework rows found in '{path}' (expected data starting at "
            f"row 3 of the '{FRAMEWORK_SHEET}' sheet). Fill out "
            f"scoring-framework-template.xlsx."
        )

    # duplicate short labels (matched on the normalized slug)
    seen: Dict[str, str] = {}
    for row in rows:
        if row["key"] in seen:
            raise ValueError(
                f"'{path}': duplicate Short Label — '{row['short_label']}' "
                f"collides with '{seen[row['key']]}'. Short labels must be "
                f"unique (they become the Category_Scores column headers)."
            )
        if not row["key"]:
            raise ValueError(
                f"'{path}': Short Label '{row['short_label']}' contains no "
                f"letters/digits — give it a usable label."
            )
        seen[row["key"]] = row["short_label"]

    total = sum(row["weight"] for row in rows)
    if abs(total - 100.0) > WEIGHT_SUM_TOL:
        hint = ""
        if abs(total - 1.0) <= 0.001:
            hint = (" (The weights sum to 1.0 — if the Weight column is "
                    "Excel-percent-formatted, the cells store fractions; enter "
                    "plain numbers like 25 instead.)")
        raise ValueError(
            f"'{path}': framework weights must sum to 100 (+/-{WEIGHT_SUM_TOL}); "
            f"they sum to {total:g}. Adjust the Weight (%) column.{hint}"
        )
    return rows


def parse_category_scores(
    path: str,
    framework: List[Dict[str, Any]],
) -> Dict[str, Dict[str, float]]:
    """Parse a category-scores xlsx against an already-parsed framework.

    Returns {firm (as written): {framework short_label: score}}.

    Validates: score columns exactly match the framework's short labels
    (order-insensitive; the error names missing/extra columns); every score
    numeric and within 1–10 (blank = error); no duplicate firms (matched on
    the same normalized-name rule the matrix parser uses).
    """
    ws = _load_sheet(path, SCORES_SHEET, "category-scores")

    # ---- header row 2: Firm | <short labels...> ----
    if _norm_header(ws.cell(row=2, column=1).value) != _norm_header(SCORES_FIRM_HEADER):
        raise ValueError(
            f"'{path}': row 2 column A must be the '{SCORES_FIRM_HEADER}' "
            f"header; found {ws.cell(row=2, column=1).value!r}. Fill out "
            f"category-scores-template.xlsx (do not reshape it)."
        )
    file_labels: List[str] = []
    c = 2
    while True:
        v = ws.cell(row=2, column=c).value
        if v is None or str(v).strip() == "":
            break
        file_labels.append(str(v).strip())
        c += 1

    fw_by_key = {row["key"]: row["short_label"] for row in framework}
    file_by_key: Dict[str, str] = {}
    for lab in file_labels:
        k = normalize_name(lab)
        if k in file_by_key:
            raise ValueError(
                f"'{path}': duplicate score column '{lab}' (also present as "
                f"'{file_by_key[k]}'). One column per framework Short Label."
            )
        file_by_key[k] = lab

    missing = [fw_by_key[k] for k in fw_by_key if k not in file_by_key]
    extra = [file_by_key[k] for k in file_by_key if k not in fw_by_key]
    if missing or extra:
        parts = [f"'{path}': score columns must exactly match the scoring "
                 f"framework's Short Labels."]
        if missing:
            parts.append("Missing column(s): " + ", ".join(missing) + ".")
        if extra:
            parts.append("Unexpected column(s): " + ", ".join(extra) + ".")
        parts.append("Framework short labels: "
                     + ", ".join(row["short_label"] for row in framework) + ".")
        raise ValueError(" ".join(parts))

    # column index per framework label (order-insensitive match)
    col_for_label: Dict[str, int] = {}
    for idx, lab in enumerate(file_labels, start=2):
        col_for_label[normalize_name(lab)] = idx

    # ---- data rows 3+ ----
    scores: Dict[str, Dict[str, float]] = {}
    seen_firms: Dict[str, str] = {}
    r = 3
    n_cols = 1 + len(file_labels)
    while True:
        vals = [ws.cell(row=r, column=cc).value for cc in range(1, n_cols + 1)]
        if all(v is None or str(v).strip() == "" for v in vals):
            break
        firm_val = vals[0]
        if firm_val is None or str(firm_val).strip() == "":
            raise ValueError(
                f"'{path}' row {r}: scores present but 'Firm' (column A) is "
                f"blank. Every scores row needs the bidder's name."
            )
        firm = str(firm_val).strip()
        fk = normalize_name(firm)
        if fk in seen_firms:
            raise ValueError(
                f"'{path}': duplicate firm '{firm}' (also listed as "
                f"'{seen_firms[fk]}'). One row per scored bidder."
            )
        seen_firms[fk] = firm

        firm_scores: Dict[str, float] = {}
        for row in framework:
            col = col_for_label[row["key"]]
            raw = ws.cell(row=r, column=col).value
            if raw is None or str(raw).strip() == "":
                raise ValueError(
                    f"'{path}' row {r}: score for '{firm}' / "
                    f"'{row['short_label']}' is blank. Every scored bidder "
                    f"needs a 1–10 score in every category."
                )
            try:
                score = float(raw)
            except (TypeError, ValueError):
                raise ValueError(
                    f"'{path}' row {r}: score for '{firm}' / "
                    f"'{row['short_label']}' must be numeric 1–10; got {raw!r}."
                )
            if not (SCORE_MIN <= score <= SCORE_MAX):
                raise ValueError(
                    f"'{path}' row {r}: score for '{firm}' / "
                    f"'{row['short_label']}' must be within 1–10; got {score:g}."
                )
            firm_scores[row["short_label"]] = (
                int(score) if float(score).is_integer() else score)
        scores[firm] = firm_scores
        r += 1

    if not scores:
        raise ValueError(
            f"No bidder rows found in '{path}' (expected data starting at "
            f"row 3 of the '{SCORES_SHEET}' sheet). One row per SCORED bidder."
        )
    return scores


def build_scores_from_inputs(
    name: str,
    framework: List[Dict[str, Any]],
    firm_scores: Dict[str, float],
    *,
    run_id: Optional[str] = None,
) -> BidderScores:
    """Assemble a BidderScores from the xlsx inputs (single source of truth).

    Every category is human-supplied via the Category Scores file, so
    coverage is always 100% — the curve gate (Darvish §3.4) is satisfied by
    construction. Provenance is recorded on each CategoryScore.
    """
    cats: Dict[str, CategoryScore] = {}
    for row in framework:
        cats[row["key"]] = CategoryScore(
            category=row["key"],
            score=firm_scores[row["short_label"]],
            confidence="high",
            evidence_status="sufficient",
            source="category_scores_xlsx",
            rationale=(f"Supplied via Category Scores xlsx "
                       f"('{row['short_label']}')."),
            run_id=run_id,
        )
    return BidderScores(name=name, categories=cats)
