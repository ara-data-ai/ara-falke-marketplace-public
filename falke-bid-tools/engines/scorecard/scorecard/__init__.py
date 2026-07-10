"""Falke bid-comparison scorecard skill.

Regenerates the board scorecard from a bid-comparison matrix (xlsx), per the
three specs:
  - scorecard-logic-spec.md      (Marvin  — matrix structure, tiers, QA)
  - scorecard-modeling-spec.md   (Darvish — Section C, Overall curve, rubric)
  - scorecard-template/          (Anna    — Jinja2 HTML -> PDF)

Layering (Marvin's "two-layer artifact" framing):
  - MECHANICAL layer: totals, $/SF, tiers, variance, ranking  -> deterministic.
  - PARAMETER+JUDGMENT layer: baseline band, SF basis, Section C model,
    Overall curve, qualitative 1-10 scoring -> governed inputs / labeled models.
"""
__version__ = "1.0.0"
