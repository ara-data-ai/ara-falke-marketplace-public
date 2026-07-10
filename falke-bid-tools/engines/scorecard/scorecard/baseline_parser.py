"""Parse a Falke baseline xlsx (baseline-template.xlsx format) into the
scorecard's internal baseline representation.

Returns a tuple: (band_low, band_high, band_mid, baseline_lines)
  - band_*: float ($M) — read from the Band section (rows 3–5)
  - baseline_lines: list[dict] — the trade-line dicts the engine already expects
    (keys: scope, basis, cost, value, kind[optional])

Raises ValueError with a user-friendly message if the sheet is missing,
band values are absent/non-numeric, or no trade lines are found.

Template layout (Baseline sheet):
  Row 1: header title
  Row 2: blank
  Row 3: "Band Low ($M)"   | [value]
  Row 4: "Band High ($M)"  | [value]
  Row 5: "Band Mid ($M)"   | [value]
  Row 6: blank separator
  Row 7: column headers — Scope | Basis | Cost ($) | Value | Kind
  Row 8+: trade line data (stop at first fully-blank row)
"""
from __future__ import annotations

from typing import Optional, Tuple, List, Dict, Any


def parse_baseline_xlsx(
    path: str,
) -> Tuple[float, float, float, List[Dict[str, Any]]]:
    """Parse a baseline xlsx file.

    Returns (band_low, band_high, band_mid, baseline_lines).

    Raises ValueError with a descriptive message on any structural problem.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ValueError(
            "openpyxl is required to read xlsx baseline files "
            "(pip install openpyxl)."
        ) from exc

    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        raise ValueError(f"Cannot open xlsx file '{path}': {exc}") from exc

    if "Baseline" not in wb.sheetnames:
        available = ", ".join(wb.sheetnames) if wb.sheetnames else "(none)"
        raise ValueError(
            f"Expected a sheet named 'Baseline' in '{path}'; "
            f"found: {available}."
        )

    ws = wb["Baseline"]

    # ---- Section A: band values (rows 3–5, column B = index 2) ----
    def _cell_float(row: int, label: str) -> float:
        val = ws.cell(row=row, column=2).value
        if val is None:
            raise ValueError(
                f"Band value '{label}' is missing (row {row}, column B) "
                f"in '{path}'. Fill in the Band section of the baseline xlsx."
            )
        try:
            return float(val)
        except (TypeError, ValueError):
            raise ValueError(
                f"Band value '{label}' (row {row}, column B) must be numeric; "
                f"got {val!r} in '{path}'."
            )

    band_low = _cell_float(3, "Band Low ($M)")
    band_high = _cell_float(4, "Band High ($M)")
    band_mid = _cell_float(5, "Band Mid ($M)")

    # ---- Section B: trade lines (row 8+, stop at first fully-blank row) ----
    # Columns: A=scope, B=basis, C=cost_str, D=value, E=kind
    lines: List[Dict[str, Any]] = []
    row = 8
    while True:
        scope_val = ws.cell(row=row, column=1).value
        basis_val = ws.cell(row=row, column=2).value
        cost_val = ws.cell(row=row, column=3).value
        value_val = ws.cell(row=row, column=4).value
        kind_val = ws.cell(row=row, column=5).value

        # Stop at the first fully-blank row (all five columns empty/None)
        if all(v is None for v in (scope_val, basis_val, cost_val, value_val, kind_val)):
            break

        # Skip rows where scope is blank (e.g. trailing partial rows)
        if scope_val is None or str(scope_val).strip() == "":
            row += 1
            continue

        scope_str = str(scope_val).strip()
        basis_str = str(basis_val).strip() if basis_val is not None else ""

        # Value must be numeric
        if value_val is None:
            raise ValueError(
                f"Trade line row {row}: 'Value' column (D) is blank for "
                f"scope '{scope_str}' in '{path}'. Each row needs a numeric value."
            )
        try:
            value_num = float(value_val)
        except (TypeError, ValueError):
            raise ValueError(
                f"Trade line row {row}: 'Value' column (D) must be numeric; "
                f"got {value_val!r} for scope '{scope_str}' in '{path}'."
            )
        value_int = int(round(value_num))

        # cost_str: use as-is if present, otherwise format from value
        if cost_val is not None and str(cost_val).strip():
            cost_str = str(cost_val).strip()
        else:
            cost_str = f"${value_int:,.0f}"

        line: Dict[str, Any] = {
            "scope": scope_str,
            "basis": basis_str,
            "cost": cost_str,
            "value": value_int,
        }
        if kind_val is not None and str(kind_val).strip():
            line["kind"] = str(kind_val).strip()

        lines.append(line)
        row += 1

    if not lines:
        raise ValueError(
            f"No trade lines found in '{path}' (expected data starting at row 8 "
            f"of the 'Baseline' sheet). Fill in the trade-line section."
        )

    return band_low, band_high, band_mid, lines
