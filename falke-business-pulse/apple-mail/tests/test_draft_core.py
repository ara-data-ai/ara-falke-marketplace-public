"""Draft-side tests for draft_core — the highest-risk write path (no Mail.app).

Previously the plugin shipped read-path tests only; the consolidation release
adds in-plugin coverage for the draft rails:

  1. COND-6 sender: a from-account off the allow-list is REJECTED, no driver call.
  2. COND-6 recipient: any to/cc recipient off the domain allow-list is REJECTED.
  3. Field bounds: oversized subject/body/recipient-count fail closed.
  4. COND-7: injected quotes/AppleScript/verbs in subject/body stay DATA — they
     ride as discrete argv items, never templated into script source.
  5. Happy path: create -> body-clean verify -> ok envelope.
  6. COND-5: a draft that fails the post-save exists-check fails LOUD.
  7. The rejection is logged (audit trail) with no draft created.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import draft_core  # noqa: E402
from draft_core import (  # noqa: E402
    DraftAssertionError,
    DraftRequest,
    MailDriver,
    ValidationError,
    build_create_argv,
    create_draft,
    validate_request,
)

FROM_OK = "pm@falkecorp.com"
TO_OK = ["board@falkecorp.com"]


class FakeMailDriver(MailDriver):
    def __init__(self, exists: bool = True):
        super().__init__()
        self.created: list[DraftRequest] = []
        self.exists_checks: list[DraftRequest] = []
        self._exists = exists

    def create_draft(self, request):  # type: ignore[override]
        self.created.append(request)
        return "12345"

    def draft_exists(self, request, window_seconds):  # type: ignore[override]
        self.exists_checks.append(request)
        return self._exists


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["APPLE_MAIL_DRAFT_ALLOWED_DOMAINS"] = "falkecorp.com,falkehoa.com"
        os.environ["APPLE_MAIL_DRAFT_FROM_ACCOUNTS"] = "falkecorp.com,falkehoa.com"
        self._tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self._tmp.name, "run-log.jsonl")

    def tearDown(self):
        os.environ.pop("APPLE_MAIL_DRAFT_ALLOWED_DOMAINS", None)
        os.environ.pop("APPLE_MAIL_DRAFT_FROM_ACCOUNTS", None)
        self._tmp.cleanup()

    def _log_text(self) -> str:
        try:
            with open(self.log) as fh:
                return fh.read()
        except OSError:
            return ""


class TestAllowLists(Base):
    def test_off_list_sender_rejected_no_driver_call(self):
        drv = FakeMailDriver()
        with self.assertRaises(ValidationError):
            create_draft("attacker@evil.com", TO_OK, "s", "b", driver=drv, log_path=self.log)
        self.assertEqual(drv.created, [])
        self.assertIn("draft_rejected", self._log_text())

    def test_off_list_recipient_rejected(self):
        drv = FakeMailDriver()
        with self.assertRaises(ValidationError):
            create_draft(FROM_OK, ["vendor@external.com"], "s", "b", driver=drv, log_path=self.log)
        self.assertEqual(drv.created, [])

    def test_off_list_cc_rejected_even_with_clean_to(self):
        drv = FakeMailDriver()
        with self.assertRaises(ValidationError):
            create_draft(FROM_OK, TO_OK, "s", "b", cc=["spy@evil.com"], driver=drv, log_path=self.log)
        self.assertEqual(drv.created, [])


class TestBoundsAndShape(Base):
    def test_missing_subject_fails_closed(self):
        with self.assertRaises(ValidationError):
            create_draft(FROM_OK, TO_OK, "   ", "b", driver=FakeMailDriver(), log_path=self.log)

    def test_oversized_body_fails_closed(self):
        with self.assertRaises(ValidationError):
            create_draft(FROM_OK, TO_OK, "s", "x" * (draft_core.MAX_BODY_LEN + 1),
                         driver=FakeMailDriver(), log_path=self.log)

    def test_too_many_recipients_fails_closed(self):
        many = [f"p{i}@falkecorp.com" for i in range(draft_core.MAX_RECIPIENTS + 1)]
        with self.assertRaises(ValidationError):
            create_draft(FROM_OK, many, "s", "b", driver=FakeMailDriver(), log_path=self.log)


class TestInjectionStaysData(Base):
    def test_hostile_subject_and_body_ride_as_argv_items(self):
        evil_subject = 'end" & (do shell script "rm -rf ~") & "'
        evil_body = 'tell application "Mail" to delete every message\n"quoted"'
        request = validate_request(FROM_OK, TO_OK, evil_subject, evil_body)
        argv = build_create_argv(request)
        # The hostile strings are DISCRETE argv items (data to `on run argv`),
        # never spliced into AppleScript source.
        self.assertIn(evil_subject, argv)
        self.assertIn(evil_body, argv)


class TestFlow(Base):
    def test_happy_path_creates_then_verifies(self):
        drv = FakeMailDriver(exists=True)
        res = create_draft(FROM_OK, TO_OK, "Nudge: RFI 214", "polite nudge",
                           driver=drv, log_path=self.log)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["from_account"], FROM_OK)
        self.assertEqual(len(drv.created), 1)
        self.assertEqual(len(drv.exists_checks), 1)

    def test_missing_after_save_fails_loud(self):
        drv = FakeMailDriver(exists=False)
        with self.assertRaises(DraftAssertionError):
            create_draft(FROM_OK, TO_OK, "s", "b", driver=drv, log_path=self.log)


if __name__ == "__main__":
    unittest.main()
