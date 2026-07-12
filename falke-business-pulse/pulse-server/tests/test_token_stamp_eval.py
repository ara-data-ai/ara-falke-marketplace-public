"""WS3 — lightweight headless eval: the skill MUST stamp the run token.

Floyd's alarm-fatigue guard. If the skill stops stamping
`<!-- falke-pulse-run: {result["cutoff"]} -->` into the saved pulse HTML, the 8787
viewer falls to a PERPETUAL amber "SCAN STATUS UNKNOWN" — training the eye to
ignore the banner that matters. This eval guards that contract cheaply, in-suite,
with no live model:

  1. SKILL.md CONTRACT — the stamp instruction exists, is unambiguous, references
     `result["cutoff"]` verbatim, and gives the exact comment format.
  2. ROUND-TRIP — a pulse stamped with cutoff C is recognized by the SAME parser
     the viewer uses (server._pulse_run_token) as carrying token C; an unstamped
     pulse yields None (=> viewer ambers, as intended).

A TRUE end-to-end headless eval (optional, NOT in the in-suite run) — run the skill
headlessly and assert the saved pulse carries a token matching the read tool's
cutoff:

    claude -p "run my morning pulse" \\
      --allowedTools mcp__plugin_falke-business-pulse_apple-mail__read_apple_mail,...
    # then, for the newest pulse-YYYY-MM-DD.html and the cutoff it used:
    python -c "import sys; sys.path.insert(0,'falke-business-pulse/pulse-server'); \\
      from tests.test_token_stamp_eval import assert_pulse_stamps_token as a; \\
      a(open(sys.argv[1]).read(), sys.argv[2])" <pulse.html> "<cutoff>"

`assert_pulse_stamps_token` below is that runnable check (reused by CI + by hand).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # pulse-server/ (holds server.py)
sys.path.insert(0, str(ROOT))

import server  # noqa: E402

SKILL_MD = ROOT.parent / "skills" / "falke-business-pulse" / "SKILL.md"


def assert_pulse_stamps_token(pulse_html: str, expected_cutoff: str) -> bool:
    """Headless check: the saved pulse carries the expected run token, parsed by the
    SAME function the viewer uses. Raises AssertionError on mismatch/absence."""
    token = server._pulse_run_token(pulse_html)
    assert token == expected_cutoff, f"expected run token {expected_cutoff!r}, got {token!r}"
    return True


class TestTokenStampEval(unittest.TestCase):
    def test_skill_md_states_the_stamp_contract(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("falke-pulse-run:", text, "SKILL.md must define the stamp format")
        self.assertIn('result["cutoff"]', text, "stamp must use result['cutoff'] verbatim")
        self.assertIn("verbatim", text.lower(), "SKILL.md must say copy the token verbatim")

    def test_stamped_pulse_round_trips_via_viewer_parser(self):
        cutoff = "2026-07-11 06:00:00"
        html = f"<!DOCTYPE html><html><!-- falke-pulse-run: {cutoff} --><body>ok</body></html>"
        self.assertTrue(assert_pulse_stamps_token(html, cutoff))

    def test_unstamped_pulse_yields_no_token(self):
        # No stamp => parser returns None => the viewer ambers (the failure mode
        # this eval exists to catch).
        self.assertIsNone(server._pulse_run_token("<html><body>no stamp here</body></html>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
