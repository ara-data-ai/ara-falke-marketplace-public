"""known_firms.yaml loader, schema validation, and firm matcher.

This is the config foundation for the matrix generalization (Marvin §4, Floyd
C3/C8). It is the ONLY home for firm-specific behavior — no firm names in engine
source. It mirrors the scorecard engine's config idiom: YAML + ``yaml.safe_load``
+ a validated dataclass with a hard-stop ``validate()`` that raises a typed error
with a clear message (never a stack trace).

What this module exposes to Christine's engine refactor (M4/M5):
  - ``load_known_firms(path=None, *, validate=True) -> KnownFirmsConfig``
  - ``KnownFirmsConfig.match(contractor_name) -> FirmMatchResult``

It deliberately does NOT do code-format SIGNATURE detection (the
``csi_1995_2digit`` detector of §1) or audit emission — those are Christine's
M3/M5. This module only resolves the firm-NAME match + collision (C3) and
validates the reclass rules the matched firm carries.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml

from src.canon import CANONICAL_DIVISIONS
from src.config_errors import KnownFirmsConfigError

DEFAULT_KNOWN_FIRMS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "known_firms.yaml",
)

# Out-of-band local overlay (gitignored, never shipped). Auto-discovered in the
# SAME directory as the shipped scaffold; absent is the expected safe default.
DEFAULT_OVERLAY_FILENAME = "known_firms.local.yaml"

# Canonical division codes the reclass `from`/`to` must resolve into.
_CANONICAL_CODES = frozenset(d["csi_code"] for d in CANONICAL_DIVISIONS)


@dataclass(frozen=True)
class Reclassification:
    """One firm-specific, destructive reclass rule (moves dollars from→to)."""

    rule_id: str
    from_division: str
    to_division: str
    when_description_contains_all: List[str]


@dataclass(frozen=True)
class Firm:
    """One known-firm quirk profile."""

    firm_id: str
    match: List[str]
    reclassifications: List[Reclassification] = field(default_factory=list)
    code_format_profile: Optional[str] = None
    example: bool = False   # INERT schema-doc firm (scaffold); never matched.


@dataclass(frozen=True)
class FirmMatchResult:
    """Outcome of matching a contractor_name against the firm library (C3).

    - ``ambiguous`` True  -> name matched >1 firm: the engine MUST apply NO
      reclass and NO firm-selected profile, and emit RED KNOWN_FIRM_AMBIGUOUS.
      ``firm`` is None; ``matched_firm_ids`` lists the colliding firms.
    - ``ambiguous`` False, ``firm`` set    -> exactly one match; apply it.
    - ``ambiguous`` False, ``firm`` None   -> no known-firm handling.

    Signature-based code-format detection (§1) is name-independent and runs
    regardless of this result — that is Christine's M3, not this module.
    """

    firm: Optional[Firm]
    matched_firm_ids: List[str]

    @property
    def ambiguous(self) -> bool:
        return len(self.matched_firm_ids) >= 2


@dataclass(frozen=True)
class KnownFirmsConfig:
    """Validated firm library + the matcher Christine calls per bid."""

    firms: List[Firm]

    def match(self, contractor_name: Optional[str]) -> FirmMatchResult:
        """Return the firm match for a contractor name (C3 collision rule).

        A bidder matches firm F iff ANY of F.match terms is a case-insensitive
        substring of contractor_name. >1 match -> ambiguous (no first-wins).

        `example` firms (the inert schema-doc scaffold entry) are filtered out
        BEFORE matching, so a synthetic firm is schema-validated but can never
        match a real bid or cause an ambiguity (spec §1.4).
        """
        name = (contractor_name or "").lower()
        candidates = [f for f in self.firms if not f.example]
        matched = [
            f for f in candidates
            if any(term.lower() in name for term in f.match)
        ]
        if len(matched) == 1:
            return FirmMatchResult(firm=matched[0],
                                   matched_firm_ids=[matched[0].firm_id])
        # 0 matches -> no handling; >=2 -> ambiguous (firm stays None).
        return FirmMatchResult(firm=None,
                               matched_firm_ids=[f.firm_id for f in matched])


def _validate_reclassifications(firm_id: str,
                                rules: List[Reclassification]) -> None:
    """Hard-stop schema checks for one firm's reclass rules (C8)."""
    for r in rules:
        loc = f"firm '{firm_id}', rule '{r.rule_id}'"
        if r.from_division not in _CANONICAL_CODES:
            raise KnownFirmsConfigError(
                f"{loc}: `from` division {r.from_division!r} is not a canonical "
                f"division. It must be one of CANONICAL_DIVISIONS (canon.py)."
            )
        if r.to_division not in _CANONICAL_CODES:
            raise KnownFirmsConfigError(
                f"{loc}: `to` division {r.to_division!r} is not a canonical "
                f"division — a reclass must never fabricate a non-canonical "
                f"target. It must be one of CANONICAL_DIVISIONS (canon.py)."
            )
        if r.from_division == r.to_division:
            raise KnownFirmsConfigError(
                f"{loc}: `from` and `to` are both {r.from_division!r} — a "
                f"no-op reclass is almost certainly a config error."
            )
        if not r.when_description_contains_all:
            raise KnownFirmsConfigError(
                f"{loc}: `when_description_contains_all` is empty — a reclass "
                f"with no keyword guard would move EVERY line in the `from` "
                f"division and is forbidden."
            )

    # 2-rule cycle within one firm: A->B and B->A would ping-pong dollars.
    edges = {(r.from_division, r.to_division) for r in rules}
    for (src, dst) in edges:
        if (dst, src) in edges:
            raise KnownFirmsConfigError(
                f"firm '{firm_id}': reclass rules form a cycle "
                f"{src} -> {dst} -> {src}. Reclass must be a one-way move."
            )


def _parse_firm(raw: dict, index: int) -> Firm:
    """Parse + shallow-validate one firm entry from raw YAML."""
    if not isinstance(raw, dict):
        raise KnownFirmsConfigError(
            f"firms[{index}] is not a mapping (got {type(raw).__name__})."
        )
    firm_id = raw.get("firm_id")
    if not firm_id or not isinstance(firm_id, str):
        raise KnownFirmsConfigError(
            f"firms[{index}] is missing a non-empty string `firm_id`."
        )

    match = raw.get("match")
    if not isinstance(match, list) or not match \
            or not all(isinstance(m, str) and m.strip() for m in match):
        raise KnownFirmsConfigError(
            f"firm '{firm_id}': `match` must be a non-empty list of non-empty "
            f"strings (the name-substring terms)."
        )

    rules: List[Reclassification] = []
    for j, rr in enumerate(raw.get("reclassifications", []) or []):
        if not isinstance(rr, dict):
            raise KnownFirmsConfigError(
                f"firm '{firm_id}': reclassifications[{j}] is not a mapping."
            )
        rule_id = rr.get("rule_id")
        if not rule_id or not isinstance(rule_id, str):
            raise KnownFirmsConfigError(
                f"firm '{firm_id}': reclassifications[{j}] missing string "
                f"`rule_id`."
            )
        kws = rr.get("when_description_contains_all")
        if not isinstance(kws, list) \
                or not all(isinstance(k, str) for k in (kws or [])):
            raise KnownFirmsConfigError(
                f"firm '{firm_id}', rule '{rule_id}': "
                f"`when_description_contains_all` must be a list of strings."
            )
        rules.append(Reclassification(
            rule_id=rule_id,
            from_division=rr.get("from"),
            to_division=rr.get("to"),
            when_description_contains_all=list(kws or []),
        ))

    return Firm(
        firm_id=firm_id,
        match=list(match),
        reclassifications=rules,
        code_format_profile=raw.get("code_format_profile"),
        example=bool(raw.get("example", False)),
    )


def _load_firms_file(path: str) -> List[Firm]:
    """Parse + shape-check ONE firms YAML file into a list of Firm.

    Runs the per-file duplicate-`firm_id` check (a duplicate WITHIN one file
    silently shadows and is a hard-stop). Does NOT run the deep reclass semantic
    validation or the cross-file merge — the caller does those.
    """
    with open(path, "r", encoding="utf-8") as fh:
        try:
            raw = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            raise KnownFirmsConfigError(
                f"{os.path.basename(path)} is not valid YAML: {e}"
            )
    raw = raw or {}
    if not isinstance(raw, dict):
        raise KnownFirmsConfigError(
            f"{os.path.basename(path)} top level must be a mapping with a "
            f"`firms` key."
        )
    firms_raw = raw.get("firms", [])
    if not isinstance(firms_raw, list):
        raise KnownFirmsConfigError(
            f"{os.path.basename(path)}: `firms` must be a list."
        )
    firms = [_parse_firm(fr, i) for i, fr in enumerate(firms_raw)]

    # Per-file duplicate firm_id check (a duplicate within one file would
    # silently shadow). A firm_id present in BOTH scaffold and overlay is the
    # intentional override case, handled by the merge — not a duplicate error.
    seen = set()
    for f in firms:
        if f.firm_id in seen:
            raise KnownFirmsConfigError(
                f"duplicate firm_id {f.firm_id!r} in {os.path.basename(path)}."
            )
        seen.add(f.firm_id)
    return firms


def load_known_firms(
    config_path: Optional[str] = None,
    *,
    validate: bool = True,
) -> KnownFirmsConfig:
    """Load the shipped scaffold, MERGE the optional local overlay, validate.

    The shipped ``known_firms.yaml`` is a public-safe scaffold (schema + one
    inert synthetic example firm). Real recurring-firm quirks live in a
    gitignored ``known_firms.local.yaml`` distributed out of band (spec §1). At
    load time:

    1. Load + shape-check the scaffold (required; missing is an error).
    2. Auto-discover ``known_firms.local.yaml`` in the SAME directory. If it is
       absent, skip it silently — an absent overlay is the expected, safe
       default (spec §4), not an error.
    3. Merge by ``firm_id``: scaffold firms first, then overlay firms. On a
       ``firm_id`` collision the OVERLAY entry wins (intentional override, not a
       duplicate error); overlay ids not in the scaffold are appended.
    4. Validate the MERGED firm list (so an override cannot smuggle in an
       invalid reclass rule), unless ``validate=False`` (shape-only).

    Hard-stops with a clear ``KnownFirmsConfigError`` (never a stack trace) on
    any schema violation (C8).
    """
    scaffold_path = config_path or DEFAULT_KNOWN_FIRMS_PATH
    if not os.path.exists(scaffold_path):
        raise KnownFirmsConfigError(
            f"known_firms.yaml not found: {scaffold_path}")
    scaffold_firms = _load_firms_file(scaffold_path)

    # Auto-discover the optional local overlay in the scaffold's directory.
    overlay_path = os.path.join(
        os.path.dirname(scaffold_path), DEFAULT_OVERLAY_FILENAME)
    overlay_firms = (
        _load_firms_file(overlay_path) if os.path.exists(overlay_path) else []
    )

    # Merge by firm_id: scaffold first (insertion order preserved), overlay
    # wins on collision, new overlay ids appended. dict preserves order and
    # replaces the value in place on a colliding key.
    merged: "dict[str, Firm]" = {}
    for f in scaffold_firms:
        merged[f.firm_id] = f
    for f in overlay_firms:
        merged[f.firm_id] = f
    firms = list(merged.values())

    if validate:
        for f in firms:
            _validate_reclassifications(f.firm_id, f.reclassifications)

    return KnownFirmsConfig(firms=firms)
