"""Unit tests for the R-SAFE cap: (1) ReadMailDriver.read_inbox parsing the
read_account.applescript output, and (2) the BOUNDARY-based completeness DECISION
`_is_saturated` (moved out of the untested AppleScript into Python).

Output format: `META\\n<GS-framed records>` where META =
`examined US boundary_in_window(0|1) US total`.

The AppleScript itself (newest-first bulk fetch, timing, the boundary date read) is
live-only. This pins the PYTHON side of that contract — META decode, record framing,
and the saturation decision — testable by stubbing the osascript boundary (_run).

The saturation tests are written from the INVARIANT, NOT from the code's behavior:
  if in-window mail could exist that we did NOT examine, `saturated` MUST be True.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import READ_MAX_MESSAGES_PER_ACCOUNT  # noqa: E402
from read_core import ReadMailDriver, _GS, _US, _is_saturated  # noqa: E402


class _StubDriver(ReadMailDriver):
    """A ReadMailDriver whose osascript boundary (_run) returns a fixed string, so
    the read_inbox parser can be exercised without Mail.app / osascript."""

    def __init__(self, out: str):
        super().__init__()
        self._out = out
        self.last_args: list[str] | None = None

    def _run(self, script, args):  # type: ignore[override]
        self.last_args = list(args)
        return self._out


def _rec(*fields: str) -> str:
    return _US.join(fields)


def _meta(examined: int, boundary_iw: int, total: int) -> str:
    return f"{examined}{_US}{boundary_iw}{_US}{total}"


class TestReadInboxParse(unittest.TestCase):
    def test_metadata_and_records_decoded(self):
        out = _meta(500, 1, 12000) + "\n" + _GS.join([
            _rec("a@x.com", "Subj1", "2026-07-11 07:00:00", "body one"),
            _rec("b@y.com", "Subj2", "2026-07-11 06:30:00", "body two"),
        ])
        records, examined, boundary_iw, total = _StubDriver(out).read_inbox("ARA", "cut")
        self.assertEqual(examined, 500)
        self.assertTrue(boundary_iw)
        self.assertEqual(total, 12000)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], ("a@x.com", "Subj1", "2026-07-11 07:00:00", "body one"))

    def test_empty_stream_meta_only(self):
        records, examined, boundary_iw, total = _StubDriver(_meta(0, 0, 0) + "\n").read_inbox("ARA", "x")
        self.assertEqual(records, [])
        self.assertEqual((examined, boundary_iw, total), (0, False, 0))

    def test_short_record_is_padded_to_four_fields(self):
        out = _meta(1, 0, 1) + "\n" + _rec("a@x.com", "Subj")  # only 2 fields
        records, *_ = _StubDriver(out).read_inbox("ARA", "x")
        self.assertEqual(records, [("a@x.com", "Subj", "", "")])

    def test_body_newlines_do_not_break_meta_split(self):
        # A message body containing newlines must NOT be mistaken for the META
        # delimiter (the FIRST newline separates META from the record stream).
        body = "line1\nline2\nline3"
        out = _meta(9, 0, 9) + "\n" + _rec("a@x.com", "S", "2026-07-11 07:00:00", body)
        records, examined, boundary_iw, _ = _StubDriver(out).read_inbox("ARA", "x")
        self.assertEqual(examined, 9)
        self.assertFalse(boundary_iw)
        self.assertEqual(records[0][3], body)  # body preserved intact

    def test_garbage_meta_defaults_defensively(self):
        records, examined, boundary_iw, total = _StubDriver("junk\n").read_inbox("ARA", "x")
        self.assertEqual((examined, boundary_iw, total), (0, False, 0))
        self.assertEqual(records, [])

    def test_ceiling_is_passed_to_osascript(self):
        drv = _StubDriver(_meta(0, 0, 0) + "\n")
        drv.read_inbox("ARA", "2026-07-11 06:00:00")
        self.assertEqual(drv.last_args, ["ARA", "2026-07-11 06:00:00",
                                         str(READ_MAX_MESSAGES_PER_ACCOUNT)])


class TestIsSaturated(unittest.TestCase):
    """Boundary rule: saturated = (total > examined) AND boundary_in_window.
    Written from the INVARIANT: if in-window mail could exist beyond what we
    examined, `saturated` MUST be True."""

    CEIL = 500  # examined == ceiling in the >ceiling-inbox cases below

    def test_busy_inbox_boundary_in_window_is_saturated(self):
        # THE corner: a >ceiling-in-window day (± an interleaved out-of-order old
        # message anywhere in the examined range). We examined the full ceiling,
        # there is more mail beyond it (total > examined), and the FAR-END boundary
        # of the examined range is still in window => in-window mail may sit beyond
        # the ceiling => MUST be saturated (this is the case the old rule missed).
        self.assertTrue(_is_saturated(self.CEIL, boundary_in_window=True, total=12000))

    def test_small_delta_on_huge_inbox_not_falsely_capped(self):
        # Small 24h delta on a years-large inbox: we examined the ceiling, more mail
        # exists beyond it, but the far-end boundary is already OUT of window => the
        # delta ended within the examined range => complete, NOT falsely capped.
        self.assertFalse(_is_saturated(self.CEIL, boundary_in_window=False, total=12000))

    def test_whole_inbox_examined_is_complete(self):
        # Examined the ENTIRE inbox (total <= examined) => nothing unexamined =>
        # complete, regardless of the boundary flag.
        self.assertFalse(_is_saturated(42, boundary_in_window=True, total=42))
        self.assertFalse(_is_saturated(42, boundary_in_window=False, total=42))
        self.assertFalse(_is_saturated(0, boundary_in_window=False, total=0))

    def test_total_equals_examined_at_ceiling_not_capped(self):
        # Edge: exactly ceiling messages in the inbox — examined all of them =>
        # total > examined is False => complete (no false-cap at total == ceiling).
        self.assertFalse(_is_saturated(self.CEIL, boundary_in_window=True, total=self.CEIL))


if __name__ == "__main__":
    unittest.main(verbosity=2)
