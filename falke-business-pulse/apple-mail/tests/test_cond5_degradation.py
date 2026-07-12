"""COND-5 / COND-8 tests for the Falke capped read (no Mail.app / TCC / MCP SDK).

Falke-specific port of the ara-business-pulse graceful-degradation suite,
minus the personal/known-senders machinery (Falke has none — its COND-8 is the
plain domain allow-list: falkecorp.com + falkehoa.com read in full, everything
else skipped at the account boundary with zero message reads).

Invariants under test:
  1. Per-account TIMEOUT degrades that ONE account: scan continues, status is
     "partial", the failed account is named, surviving messages are kept.
  2. Per-account STALL (rc!=0) degrades identically (per-account, not systemic).
  3. A CAPPED account (ceiling hit, boundary still in-window) marks the scan
     "partial" and is named in accounts_capped — never a silent truncation.
  4. TOTAL wipeout (every attempted account failed) raises — never an empty "ok".
  5. Clean scan => status "ok", marker written as "ok".
  6. COND-8: a non-allow-listed account is NEVER passed to read_inbox.
  7. The scan-status marker is written for the viewer (partial + ok), and the
     basename clobber-guard refuses to write anywhere else.
  8. Systemic list_accounts failure raises (fail loud).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import read_core  # noqa: E402
from fakes import FakeReadMailDriver  # noqa: E402

CUTOFF = "2026-07-12 06:00:00"

BUSINESS_1 = "FalkeCorp"       # falkecorp.com — allow-listed
BUSINESS_2 = "FalkeHOA"        # falkehoa.com — allow-listed
PERSONAL = "gmail.com"         # personal — NOT allow-listed


def _world():
    return {
        BUSINESS_1: {
            "email": "pm@falkecorp.com",
            "messages": [
                ("board@oceania2.com", "RFI 214", "2026-07-12 07:10:00", "please advise"),
                ("vendor@robmar.com", "submittal", "2026-07-12 07:12:00", "attached"),
            ],
        },
        BUSINESS_2: {
            "email": "info@falkehoa.com",
            "messages": [
                ("owner@unit402.com", "leak follow-up", "2026-07-12 07:15:00", "any update?"),
            ],
        },
        PERSONAL: {
            "email": "someone@gmail.com",
            "messages": [
                ("newsletter@promo.com", "sale", "2026-07-12 07:20:00", "buy now"),
            ],
        },
    }


class TestCond5Degradation(unittest.TestCase):
    def setUp(self):
        # Confine allow-list, run-log, and marker to this test.
        os.environ["APPLE_MAIL_READ_ALLOWED_ACCOUNTS"] = "falkecorp.com,falkehoa.com"
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self._tmp.name, "read-log.jsonl")
        self.status_path = os.path.join(self._tmp.name, "last-scan-status.json")

    def tearDown(self):
        os.environ.pop("APPLE_MAIL_READ_ALLOWED_ACCOUNTS", None)
        self._tmp.cleanup()

    def _read(self, driver):
        return read_core.read_apple_mail(
            CUTOFF, driver=driver, log_path=self.log_path, status_path=self.status_path
        )

    def _marker(self):
        with open(self.status_path, encoding="utf-8") as fh:
            return json.load(fh)

    # 1. Per-account timeout degrades, does not kill the scan.
    def test_timeout_degrades_one_account_scan_partial(self):
        driver = FakeReadMailDriver(_world(), timeout_accounts={BUSINESS_1})
        result = self._read(driver)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([f["account"] for f in result["accounts_failed"]], [BUSINESS_1])
        self.assertEqual(result["accounts_read"], [BUSINESS_2])
        # The surviving account's messages are kept.
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["account"], BUSINESS_2)
        # Marker records the partial for the viewer's structural banner.
        self.assertEqual(self._marker()["status"], "partial")
        self.assertEqual(self._marker()["accounts_failed"][0]["account"], BUSINESS_1)

    # 2. Per-account stall (rc!=0) degrades identically.
    def test_stall_degrades_one_account_scan_partial(self):
        driver = FakeReadMailDriver(_world(), error_accounts={BUSINESS_2})
        result = self._read(driver)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([f["account"] for f in result["accounts_failed"]], [BUSINESS_2])
        self.assertEqual(result["accounts_read"], [BUSINESS_1])
        self.assertEqual(len(result["messages"]), 2)

    # 3. A capped account marks the scan partial and is named.
    def test_capped_account_marks_scan_partial(self):
        driver = FakeReadMailDriver(_world(), saturated_accounts={BUSINESS_1})
        result = self._read(driver)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            [c["account"] for c in result["accounts_capped"]], [BUSINESS_1]
        )
        # Capped is NOT failed: the account still returned its newest messages.
        self.assertEqual(result["accounts_failed"], [])
        self.assertIn(BUSINESS_1, result["accounts_read"])
        self.assertEqual(self._marker()["status"], "partial")
        self.assertEqual(self._marker()["accounts_capped"][0]["account"], BUSINESS_1)

    # 4. Total wipeout raises — never an empty success.
    def test_total_wipeout_raises(self):
        driver = FakeReadMailDriver(
            _world(), timeout_accounts={BUSINESS_1}, error_accounts={BUSINESS_2}
        )
        with self.assertRaises(read_core.ReadMailError):
            self._read(driver)

    # 5. Clean scan: ok + marker ok.
    def test_clean_scan_ok_and_marker_ok(self):
        driver = FakeReadMailDriver(_world())
        result = self._read(driver)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["accounts_failed"], [])
        self.assertEqual(result["accounts_capped"], [])
        self.assertEqual(sorted(result["accounts_read"]), sorted([BUSINESS_1, BUSINESS_2]))
        self.assertEqual(len(result["messages"]), 3)
        self.assertEqual(self._marker()["status"], "ok")
        # The run token round-trips: marker cutoff == result cutoff.
        self.assertEqual(self._marker()["cutoff"], result["cutoff"])

    # 6. COND-8: the personal account is never passed to read_inbox.
    def test_personal_account_never_read(self):
        driver = FakeReadMailDriver(_world())
        result = self._read(driver)
        self.assertNotIn(PERSONAL, driver.read_calls)
        self.assertNotIn(PERSONAL, result["accounts_read"])
        for m in result["messages"]:
            self.assertNotEqual(m["account"], PERSONAL)

    # 7. Marker clobber-guard: refuses to write to a non-marker basename.
    def test_marker_clobber_guard(self):
        rogue = os.path.join(self._tmp.name, "config.json")
        read_core._write_scan_status("partial", [], [], CUTOFF, rogue)
        self.assertFalse(os.path.exists(rogue))

    # 8. Systemic enumeration failure raises.
    def test_list_accounts_failure_raises(self):
        driver = FakeReadMailDriver(_world(), list_accounts_error=True)
        with self.assertRaises(read_core.ReadMailError):
            self._read(driver)


if __name__ == "__main__":
    unittest.main()
