"""Exit-contract v2 — the scorecard's process contract (P1-1; Floyd verdict (e),
adopting Boris §D's shape exactly).

THE CONTRACT
------------
| code | meaning                        | artifacts | who reads it            |
|------|--------------------------------|-----------|-------------------------|
| 0    | clean — rendered, audit PASS   | yes       | deliver it              |
|      | or PASS-WITH-WARNINGS          |           |                         |
| 1    | environment / nothing to do    | NO        | fix the environment     |
| 2    | input-gate hard stop           | NO        | the gate is WORKING     |
| 3    | delivered WITH an audit blocker| yes       | lead with the disclosure|
| 4    | delivered PROVISIONAL          | yes       | rank is provisional     |

Precedence: **3 > 4**. A run that is both blocked and provisional is a blocked
run — the louder class wins, mirroring the matrix engine's exit 3 > 4.

WHY THIS EXISTS
---------------
Exit 1 was overloaded. It meant "everything was written and you must not
deliver it" — the exact OPPOSITE of the matrix engine's v2 contract, where 1
means "environment / nothing to do, nothing written". An orchestrating skill
that learned matrix habits would mis-handle a scorecard blocker, and P1-4's
C-2 proved the collision was live: a malformed pack (traceback, exit 1) was
indistinguishable BY EXIT CODE from a rendered card carrying audit blockers
(exit 1). Same number, opposite instructions.

WHAT EXIT 1 IS
--------------
"The process could not run; nothing was read and nothing was written." Two ways
in, and only one of them is ours:

  * An unreadable ``--matrix`` path — cli.py's pre-flight. Before P1-1 this
    raised a raw FileNotFoundError out of openpyxl as an uncaught traceback at
    exit 1: the right code by accident, with a stack trace where the message
    should be. It is now a deliberate `[STOP]`. Exit 1 and not 2 per Boris §D's
    table ("bad matrix path") and Floyd's "adopt the shape exactly" — and
    because a typo'd path is emphatically NOT "the gate working", which is the
    framing exit 2 carries. Every other input file is already guarded and
    correctly reports 2.
  * A missing Python dependency at import (`ModuleNotFoundError: yaml`), which
    the interpreter exits 1 for by default, before main() is entered.

Both are honestly "environment / nothing written". What exit 1 no longer means,
and this is the point of the whole item, is "everything was written and you must
not deliver it".

THE WATERMARK IS PART OF THE CONTRACT, NOT DECORATION
-----------------------------------------------------
An exit code does not survive a screenshot. A board packet is screenshotted,
printed, forwarded, and pasted into an email — and at that moment the exit code
is gone and only the pixels remain. Exit 3 REQUIRES the artifacts to exist
("delivered WITH audit blocker"), so refusing to render is not available to us:
the artifact must therefore disclose its own status ON ITS FACE.

That is why `resolve_watermark` lives beside `resolve_exit_code` and is derived
from the SAME two inputs. They are two renderings of one fact, and keeping them
in one module is what stops them drifting into disagreement.
"""
from __future__ import annotations

from typing import List, Optional

EXIT_CLEAN = 0
EXIT_ENVIRONMENT = 1
EXIT_INPUT_GATE = 2
EXIT_DELIVERED_WITH_BLOCKER = 3
EXIT_DELIVERED_PROVISIONAL = 4

# audit verdicts (mirrors audit.V_PASS / V_WARN / V_FAIL; None = no audit ran)
VERDICT_PASS = "PASS"
VERDICT_WARN = "PASS-WITH-WARNINGS"
VERDICT_FAIL = "FAIL"


def resolve_exit_code(*, audit_verdict: Optional[str],
                      full_coverage: bool) -> int:
    """The delivered-run exit code, from the audit verdict + coverage.

    Only ever returns 0, 3 or 4: by the time it is called the render succeeded,
    so the not-delivered codes (1, 2) are already behind us. Pure and total, so
    the contract is testable without driving a render — which matters, because
    exit 4 has no CLI-reachable trigger until P1-2 lands (see below).

    ``audit_verdict`` is None when --no-audit was passed. That is NOT a blocker
    (no blocker is known) and it is not clean either — nothing was checked. The
    engine returns 0 because nothing failed and the operator explicitly asked to
    skip; the ARTIFACT is what carries the disclosure, via the "not audited"
    watermark (see resolve_watermark). The skill prohibits --no-audit for board
    runs.

    PASS-WITH-WARNINGS is exit 0, per Boris §D and Floyd's "adopt the shape
    exactly". This is load-bearing, not a detail: W8 fires on EVERY run until
    Falke adopts a standing framework, so every honest run today is
    PASS-WITH-WARNINGS. Treating that as non-clean would make the abnormal
    signal the normal state — a control that fires on the honest case trains
    the operator to click past it, and a dead control is worse than none
    because it purchases false assurance.
    """
    if audit_verdict == VERDICT_FAIL:
        return EXIT_DELIVERED_WITH_BLOCKER          # precedence 3 > 4
    if not full_coverage:
        return EXIT_DELIVERED_PROVISIONAL
    return EXIT_CLEAN


def coverage_watermark(*, full_coverage: bool) -> List[dict]:
    """The watermark reasons derivable from COVERAGE ALONE.

    Split out because the two halves of the mark are known at different moments:
    coverage is known the instant the pipeline finishes, the audit verdict only
    after the audit runs. run_scorecard sets this half on its result so that
    (a) C12(d) always has a real artifact-facing fact to check rather than
    depending on the CLI having reached a later line, and (b) a direct
    programmatic caller gets the same honest mark the CLI produces.

    resolve_watermark composes this with the audit half. There is exactly one
    place each reason is worded.
    """
    if full_coverage:
        return []
    return [{
        "token": "evaluation incomplete",
        "detail": ("The qualitative evaluation is not finished. This is a "
                   "working document, not an award document."),
    }]


def resolve_watermark(*, audit_verdict: Optional[str],
                      full_coverage: bool) -> List[dict]:
    """The reasons this artifact is PRELIMINARY. Empty = deliverable, no mark.

    ONE MECHANISM, COMPOSED REASONS (Marvin P1-2 §2.4, binding on this build).
    Returns a LIST of ``{"token", "detail"}`` — never a boolean — because the
    states are independent facts and a run can carry several. An unaudited
    provisional render is BOTH, and suppressing one reason to show the other is
    exactly the C-1 defect Floyd just made me fix on the framework-basis note.
    A list absorbs the next reason without anyone inventing a second mechanism.

      * ``token``  — the short form for the composed headline, e.g.
                     "PRELIMINARY — evaluation incomplete · audit blocker".
      * ``detail`` — the full sentence for the banner, which has room to say
                     what the reader should actually do.

    ON THE WORDING, because it departs from a literal string in the ruling:
    Floyd's phrase is "PRELIMINARY — audit pending", written when artifacts were
    rendered BEFORE the audit. Under audit-first (this item) that phrase is
    false on a blocker run — the audit is not pending, it ran and it found
    something — and putting a falsehood on a board document to match a string
    would invert the whole point of the program. So the tokens say what is true:
    `audit blocker` when it ran and failed, `not audited` when it never ran
    (which is where "pending" genuinely belongs), `evaluation incomplete` for
    coverage. Marvin's format is honored exactly; only the words are accurate.

    ``full_coverage=False`` is P1-2's trigger and has NO CLI-reachable path
    today — the seam is here and tested; the trigger is his ruling to make.
    """
    # Coverage first: it is the reason the reader most needs, and Marvin's
    # canonical composition reads "evaluation incomplete · audit ...".
    reasons: List[dict] = coverage_watermark(full_coverage=full_coverage)
    if audit_verdict is None:
        reasons.append({
            "token": "not audited",
            "detail": ("The self-audit was SKIPPED for this run (--no-audit). "
                       "Nothing here has been checked against the source "
                       "matrix."),
        })
    elif audit_verdict == VERDICT_FAIL:
        reasons.append({
            "token": "audit blocker",
            "detail": ("The self-audit found a BLOCKER. Do not distribute this "
                       "to the board until it is remediated and the run "
                       "repeats clean."),
        })
    return reasons


def watermark_headline(reasons: List[dict]) -> str:
    """'PRELIMINARY — evaluation incomplete · audit blocker' (Marvin §2.4)."""
    if not reasons:
        return ""
    return "PRELIMINARY — " + " · ".join(r["token"] for r in reasons)
