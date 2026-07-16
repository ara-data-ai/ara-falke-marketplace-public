"""Exit-contract v2 + the PRELIMINARY watermark (P1-1; Floyd verdict (e)).

The decision is a pure function of (audit verdict, coverage), so it is pinned
here without driving a render. That matters for one specific reason: **exit 4
has no CLI-reachable trigger until P1-2 lands** — every CLI path today produces
100% coverage by construction. Unit-testing the contract is what keeps the
exit-4 path real and proven rather than hypothetical code waiting for a ruling.

The LIVE behaviour (a real blocked run renders a real watermarked artifact) is
pinned in test_producer_live_compat.py, where it belongs.
"""
from __future__ import annotations

import pytest

from scorecard.exit_codes import (EXIT_CLEAN, EXIT_DELIVERED_PROVISIONAL,
                                  EXIT_DELIVERED_WITH_BLOCKER, VERDICT_FAIL,
                                  VERDICT_PASS, VERDICT_WARN,
                                  resolve_exit_code, resolve_watermark,
                                  watermark_headline)
from scorecard.render import watermark_background_uri


# ---------------------------------------------------------------------------
# resolve_exit_code — the whole truth table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("verdict,coverage,expected", [
    (VERDICT_PASS, True, EXIT_CLEAN),
    # PASS-WITH-WARNINGS is exit 0, and this is load-bearing rather than a
    # detail: W8 fires on EVERY run until Falke adopts a standing framework, so
    # every honest run today is PASS-WITH-WARNINGS. Treating it as non-clean
    # would make the abnormal signal the normal state, and a control that fires
    # on the honest case gets clicked past.
    (VERDICT_WARN, True, EXIT_CLEAN),
    (VERDICT_FAIL, True, EXIT_DELIVERED_WITH_BLOCKER),
    (VERDICT_PASS, False, EXIT_DELIVERED_PROVISIONAL),
    (VERDICT_WARN, False, EXIT_DELIVERED_PROVISIONAL),
    # precedence 3 > 4: a blocker is a stronger statement than incompleteness
    (VERDICT_FAIL, False, EXIT_DELIVERED_WITH_BLOCKER),
    # --no-audit: no verdict exists. Not a blocker (none is known); the ARTIFACT
    # carries the disclosure via the "not audited" mark.
    (None, True, EXIT_CLEAN),
    (None, False, EXIT_DELIVERED_PROVISIONAL),
])
def test_exit_code_truth_table(verdict, coverage, expected):
    assert resolve_exit_code(audit_verdict=verdict,
                             full_coverage=coverage) == expected


def test_blocker_beats_provisional():
    """Precedence 3 > 4, stated as its own pin because it is the one rule a
    future edit is most likely to invert by accident."""
    assert resolve_exit_code(audit_verdict=VERDICT_FAIL,
                             full_coverage=False) == EXIT_DELIVERED_WITH_BLOCKER


# ---------------------------------------------------------------------------
# resolve_watermark — a LIST, never a boolean (Marvin P1-2 §2.4)
# ---------------------------------------------------------------------------
def test_clean_run_has_no_watermark():
    """The control that must never misfire: a deliverable card carries no mark.
    A watermark on every card is a watermark on no card."""
    assert resolve_watermark(audit_verdict=VERDICT_PASS,
                             full_coverage=True) == []
    assert resolve_watermark(audit_verdict=VERDICT_WARN,
                             full_coverage=True) == []
    assert watermark_headline([]) == ""


def test_reasons_compose_rather_than_suppress_each_other():
    """Marvin §2.4, binding: one mechanism, composed reasons, built as a list.
    Suppressing one reason to show the other is exactly the C-1 defect Floyd
    made me fix on the framework-basis note."""
    reasons = resolve_watermark(audit_verdict=VERDICT_FAIL, full_coverage=False)
    tokens = [r["token"] for r in reasons]
    assert tokens == ["evaluation incomplete", "audit blocker"]
    assert watermark_headline(reasons) == (
        "PRELIMINARY — evaluation incomplete · audit blocker")


def test_unaudited_provisional_run_says_both():
    reasons = resolve_watermark(audit_verdict=None, full_coverage=False)
    assert [r["token"] for r in reasons] == ["evaluation incomplete",
                                             "not audited"]


@pytest.mark.parametrize("verdict,coverage,token", [
    (VERDICT_FAIL, True, "audit blocker"),
    (None, True, "not audited"),
    (VERDICT_PASS, False, "evaluation incomplete"),
])
def test_each_reason_alone(verdict, coverage, token):
    reasons = resolve_watermark(audit_verdict=verdict, full_coverage=coverage)
    assert [r["token"] for r in reasons] == [token]
    assert all(r["detail"] for r in reasons)


def test_blocker_run_never_claims_the_audit_is_pending():
    """Floyd's literal string is "PRELIMINARY — audit pending", written when
    artifacts were rendered BEFORE the audit. Under audit-first that is FALSE on
    a blocker run — the audit ran and found something — and putting a falsehood
    on a board document to match a string would invert the point of the program.
    "Pending" belongs only where no verdict exists."""
    blocked = watermark_headline(
        resolve_watermark(audit_verdict=VERDICT_FAIL, full_coverage=True))
    assert "pending" not in blocked.lower()
    assert "audit blocker" in blocked


# ---------------------------------------------------------------------------
# the tiling mark
# ---------------------------------------------------------------------------
def test_watermark_background_carries_the_reasons_not_just_the_word():
    """The diagonal is the layer that survives a crop, so it has to say WHY."""
    uri = watermark_background_uri("PRELIMINARY — audit blocker",
                                   ["audit blocker"])
    from urllib.parse import unquote
    svg = unquote(uri.split(",", 1)[1])
    assert svg.startswith("<svg")
    assert "PRELIMINARY" in svg
    assert "AUDIT BLOCKER" in svg
    # rotated: a horizontal band reads as a header and crops off like one
    assert "rotate(-24" in svg


def test_watermark_background_escapes_xml():
    """The tokens are ours today, but an unescaped '&' would produce a silently
    broken data URI — i.e. a watermark that renders as nothing at all."""
    uri = watermark_background_uri("x", ["a & b"])
    from urllib.parse import unquote
    assert "&amp;" in unquote(uri.split(",", 1)[1])
