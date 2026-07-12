#!/usr/bin/env python3
"""WS4 — install / first-run smoke for the apple-mail read path.

DEFAULT (offline) mode closes the MCP-serialization gap: read_apple_mail returns
the documented dict shape and is JSON-serializable (the MCP layer serializes the
return to JSON, so a non-serializable field would only surface over the wire). It
runs WITHOUT the live MCP SDK and WITHOUT touching Apple Mail (a tiny in-process
fake) and WITHOUT writing the real marker/run-log (temp dir).

--live mode (run this post-publish; NEEDS real Mail + TCC) is the cap's
boundary/completeness/SPEED probe (R-SAFE):
  * SPEED — times a real read; the ~90s per-account timeout should be gone.
  * BOUNDARY DECISION — with an account name, it reads that ONE account directly and
    prints examined / total / boundary_in_window / in-window record count and the
    resulting saturated/CAPPED verdict. This exercises the boundary DECISION itself,
    not just "did the pulse return something".
  * No per-account timeout (accounts_failed empty) is the primary PASS signal.
CAVEAT: a healthy-looking returned/message count does NOT prove completeness — the
silent-miss class is an account that SHOULD be capped but isn't; watch
`accounts_capped` and the boundary verdict, not the raw count.
It uses a recent (3-day) cutoff and temp marker/log paths (never the real marker).

Run:
    python3 falke-business-pulse/scripts/smoke_read_shape.py                     # offline
    python3 falke-business-pulse/scripts/smoke_read_shape.py --live             # full read
    python3 falke-business-pulse/scripts/smoke_read_shape.py --live "iCloud"    # + boundary

Exit 0 = PASS, 1 = FAIL/CHECK.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
APPLE_MAIL = os.path.normpath(os.path.join(HERE, "..", "apple-mail"))
sys.path.insert(0, APPLE_MAIL)

from read_core import MailAccount, ReadMailDriver, read_apple_mail  # noqa: E402

DOCUMENTED_KEYS = {
    "status", "messages", "accounts_read", "accounts_failed", "accounts_capped",
    "accounts_skipped_dark", "cutoff",
}


class _FakeDriver(ReadMailDriver):
    """Stand-in for Apple Mail — one ARA account, one message. No osascript.
    read_inbox returns (records, examined, boundary_in_window, total)."""

    def list_accounts(self):  # type: ignore[override]
        return [MailAccount(name="ARA", email="operator@falkecorp.com")]

    def read_inbox(self, account_name, cutoff):  # type: ignore[override]
        recs = [("client@falkecorp.com", "Subject", "2026-07-11 07:00:00", "body text")]
        # examined == total => total > examined is False => complete (never capped).
        return recs, len(recs), False, len(recs)  # examined, boundary_in_window, total


def offline() -> int:
    os.environ.setdefault("APPLE_MAIL_READ_ALLOWED_ACCOUNTS", "falkecorp.com")
    os.environ["APPLE_MAIL_READ_KNOWN_SENDERS"] = ""  # deterministic; personal dark
    tmp = tempfile.mkdtemp()
    res = read_apple_mail(
        "2026-07-11 06:00:00",
        driver=_FakeDriver(),
        log_path=os.path.join(tmp, "log.jsonl"),
        status_path=os.path.join(tmp, "last-scan-status.json"),  # never the real marker
    )
    ok = True
    if set(res) != DOCUMENTED_KEYS:
        print(f"FAIL: keys {sorted(set(res))} != {sorted(DOCUMENTED_KEYS)}")
        ok = False
    try:
        json.loads(json.dumps(res))  # serialization round-trip (the MCP-wire check)
    except (TypeError, ValueError) as exc:
        print(f"FAIL: result is not JSON-serializable: {exc}")
        ok = False
    try:
        import server  # noqa: F401
        print("[info] MCP tool module imports (SDK present).")
    except Exception as exc:
        print(f"[info] MCP SDK not importable here ({exc.__class__.__name__}); "
              "shape validated via read_core.")
    print("PASS: read_apple_mail returns the documented, JSON-serializable shape."
          if ok else "SMOKE FAILED.")
    return 0 if ok else 1


def live(account: str | None) -> int:
    """Real read against Apple Mail — SPEED + boundary DECISION (R-SAFE)."""
    from read_core import ReadMailDriver, _is_saturated  # local: needs osascript
    since = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    # Direct BOUNDARY observation for ONE named account: exercises the saturation
    # decision itself. (Reads the named account directly — run it on YOUR own
    # allow-listed account.)
    if account:
        recs, examined, boundary_in_window, total = ReadMailDriver().read_inbox(account, since)
        sat = _is_saturated(examined, boundary_in_window, total)
        print(f"[boundary] account={account!r} examined={examined} total={total} "
              f"boundary_in_window={boundary_in_window} in_window_records={len(recs)} "
              f"=> saturated/CAPPED={sat}")
        print("[boundary] read: saturated=True => older in-window mail may be unread "
              "(raise the ceiling or narrow the window); saturated=False with a recent "
              "`since` should return your recent mail. A healthy record COUNT alone "
              "does NOT prove completeness — the boundary verdict is the signal.")

    tmp = tempfile.mkdtemp()
    t0 = time.monotonic()
    res = read_apple_mail(  # real ReadMailDriver; temp paths => no real marker/log
        since,
        log_path=os.path.join(tmp, "log.jsonl"),
        status_path=os.path.join(tmp, "last-scan-status.json"),
    )
    elapsed = time.monotonic() - t0
    by_acct = Counter(m["account"] for m in res["messages"])
    print(f"[live] elapsed={elapsed:.1f}s  status={res['status']}  since={since}")
    print(f"[live] accounts_read   = {res['accounts_read']}")
    print(f"[live] accounts_failed = {[f['account'] for f in res['accounts_failed']]}")
    print(f"[live] accounts_capped = {[c['account'] for c in res['accounts_capped']]}")
    print(f"[live] messages_by_account = {dict(by_acct)}  (total {len(res['messages'])})")
    print("[live] COMPLETENESS: a healthy message count does NOT prove completeness — "
          "the silent-miss class is an account that SHOULD be capped but is not. Watch "
          "`accounts_capped`; for a busy account, pass its name to observe the boundary "
          "verdict above.")
    # Primary PASS: no per-account read timed out or stalled (the 90s bug is gone).
    ok = not res["accounts_failed"]
    print("PASS: real read completed with no per-account timeout/stall." if ok else
          "CHECK: a per-account read failed (timeout/stall) — see accounts_failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    _args = sys.argv[1:]
    if "--live" in _args:
        _rest = [a for a in _args if a != "--live"]
        sys.exit(live(_rest[0] if _rest else None))
    sys.exit(offline())
