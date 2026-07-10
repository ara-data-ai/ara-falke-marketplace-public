"""read_core — the security-critical core of the Apple Mail READ tool.

Read side of the CoS morning scan. Mirrors draft_core's hardening discipline and
shares the same osascript-arg-safety machinery (static .applescript + list argv,
shell=False). Kept separate from MCP transport (server.py) so the full read test
suite runs WITHOUT the MCP SDK and WITHOUT driving Mail.app (fake driver).

Conditions implemented here:
  COND-8  (account allow-list — the privacy control). read_apple_mail() lists
          accounts (list_accounts.applescript: name + email, NO message read),
          matches each account's email DOMAIN against the allow-list, and only
          THEN reads the inbox of an ALLOW-LISTED account. A non-allow-listed
          account (e.g. a personal Gmail) is NEVER passed to the read script —
          enforcement is at the account boundary, BEFORE any message is read,
          by construction (not filter-after-read). Fail closed: empty/garbage
          allow-list => read NOTHING. Every account read/skip is logged.
  COND-7  (AppleScript string-injection). The account name + cutoff go to
          osascript as ARGUMENTS (list argv, shell=False) read by `on run argv`,
          never interpolated into AppleScript source — same as the draft tool.
  bounded-delta. read_account.applescript scans ONLY the named account's INBOX,
          filtered `whose date received > cutoff`. Never every mailbox of every
          account (the ~500x-slower, Mail-stalling pattern).
  cached-body integrity. A message present in the delta window but with a blank/
          partial body (cached/not-yet-downloaded) is SKIPPED + logged — never
          returned as if it were the real message.
  COND-5  (fail-loud + run-log). osascript timeout / error / Mail-not-running
          fails LOUD (raises) and writes a read-log entry; never silently returns
          an empty/partial scan as success.
  COND-2  READ-ONLY. The only Mail operations are property reads + a `whose`
          filter (in the two static read scripts). No write/draft/delete/move/
          mark verb. ReadMailDriver exposes only list_accounts + read_inbox.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import (
    MAX_READ_BODY_LEN,
    MIN_BODY_CHARS,
    read_allowed_accounts,
    read_run_log_path,
)

# --- Paths to the STATIC AppleScript files (never templated) ----------------
_HERE = Path(__file__).resolve().parent
LIST_ACCOUNTS_SCRIPT = _HERE / "applescript" / "list_accounts.applescript"
READ_ACCOUNT_SCRIPT = _HERE / "applescript" / "read_account.applescript"

OSASCRIPT = "/usr/bin/osascript"

# Framing control bytes emitted by read_account.applescript (must match it).
_US = "\x1f"  # unit separator — between fields of a record
_GS = "\x1d"  # group separator — between records

# Cutoff must be exactly "YYYY-MM-DD HH:MM:SS" before it reaches osascript.
_CUTOFF_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

# Extract the domain from an account's email address for the allow-list match.
_EMAIL_DOMAIN_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


class ReadValidationError(ValueError):
    """Bad read inputs (e.g. malformed cutoff). Fail closed: read nothing."""


class ReadMailError(RuntimeError):
    """osascript / Mail.app failed during a read (timeout, error, Mail closed).

    COND-5: fail LOUD — never silently return an empty/partial scan as success.
    """


@dataclass(frozen=True)
class MailAccount:
    name: str
    email: str

    @property
    def domain(self) -> str:
        m = _EMAIL_DOMAIN_RE.match((self.email or "").strip())
        return m.group(1).lower() if m else ""


@dataclass(frozen=True)
class Message:
    account: str
    sender: str
    subject: str
    date: str
    body: str


# --------------------------------------------------------------------------- #
# COND-7 — cutoff normalization to the exact AppleScript-expected shape.
# --------------------------------------------------------------------------- #
def normalize_cutoff(since_iso: str) -> str:
    """Normalize an ISO timestamp to "YYYY-MM-DD HH:MM:SS" for read_account.

    Accepts common ISO-8601 forms (with 'T', with timezone/offset, with
    microseconds) and reduces to the second. Raises ReadValidationError on
    anything we can't parse — fail closed rather than feed osascript garbage.
    The normalized value is still passed as an ARGUMENT, never inlined (COND-7);
    normalization is belt-and-suspenders so the AppleScript date parse is robust.
    """
    if not isinstance(since_iso, str) or not since_iso.strip():
        raise ReadValidationError("since_iso is required (ISO-8601 datetime)")
    s = since_iso.strip()
    # Fast path: already in the exact shape.
    if _CUTOFF_RE.match(s):
        return s
    parsed = None
    candidate = s.replace("T", " ")
    # datetime.fromisoformat handles offsets/microseconds on 3.7+ (mostly).
    for attempt in (s, candidate):
        try:
            parsed = _dt.datetime.fromisoformat(attempt)
            break
        except ValueError:
            continue
    if parsed is None:
        # Last resort: try a few explicit formats.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = _dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ReadValidationError(f"unparseable since_iso: {since_iso!r}")
    # Drop any tz; Mail's `date received` compares in local time.
    parsed = parsed.replace(tzinfo=None, microsecond=0)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# ReadMailDriver — the ONLY boundary that touches Mail.app for reading.
# Mockable for tests. Exposes EXACTLY two read-only operations.
# --------------------------------------------------------------------------- #
class ReadMailDriver:
    """Runs the two static read AppleScript files via osascript (shell=False,
    list argv). Tests inject a fake, so the suite never needs Mail.app/TCC.

    READ-ONLY by construction: list_accounts (name+email, no message read) and
    read_inbox (one named account's inbox, bounded delta). No write op exists.
    """

    def __init__(self, timeout: float = 90.0):
        # Read can be slower than draft (delta scan over the slow AppleScript
        # bridge); a bounded timeout still fails loud rather than hanging Mail.
        self.timeout = timeout

    def _run(self, script: Path, args: list[str]) -> str:
        # shell=False, list argv: untrusted args are DATA to osascript; the
        # script path is fixed. This is COND-7 at the process boundary.
        cmd = [OSASCRIPT, str(script), *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                shell=False,  # explicit: never a shell string
            )
        except FileNotFoundError as exc:  # osascript missing => not on macOS
            raise ReadMailError(f"osascript not available: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            # COND-5: a delta scan that hangs Mail is a silent-miss risk -> loud.
            raise ReadMailError(
                f"osascript read timed out after {self.timeout}s "
                f"(Mail may be stalled / mailbox too large): {exc}"
            ) from exc
        if proc.returncode != 0:
            raise ReadMailError(
                f"osascript read failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    def list_accounts(self) -> list[MailAccount]:
        """Enumerate Mail accounts (name + email only). NO message is read."""
        out = self._run(LIST_ACCOUNTS_SCRIPT, [])
        accounts: list[MailAccount] = []
        for line in out.splitlines():
            line = line.rstrip("\r")
            if not line.strip():
                continue
            # name<TAB>email — split on the FIRST tab only (names may contain tabs
            # in theory; emails never do, so rsplit keeps the email intact).
            if "\t" in line:
                name, email = line.split("\t", 1)
            else:
                name, email = line, ""
            accounts.append(MailAccount(name=name.strip(), email=email.strip()))
        return accounts

    def read_inbox(self, account_name: str, cutoff: str) -> list[tuple[str, str, str, str]]:
        """Read ONE named account's inbox, bounded by cutoff (delta scan).

        Returns raw (sender, subject, date, body) tuples — base64-free; the
        AppleScript control-char framing is decoded here. No filtering/skip
        logic here (that's read_apple_mail's job, so it can be logged centrally).
        """
        out = self._run(READ_ACCOUNT_SCRIPT, [account_name, cutoff])
        records: list[tuple[str, str, str, str]] = []
        if not out.strip():
            return records
        for rec in out.split(_GS):
            if rec == "":
                continue
            fields = rec.split(_US)
            # Pad defensively to 4 fields.
            while len(fields) < 4:
                fields.append("")
            sender, subject, date, body = fields[0], fields[1], fields[2], fields[3]
            records.append((sender, subject, date, body))
        return records


# --------------------------------------------------------------------------- #
# Read run-log (COND-5 + COND-8 audit trail)
# --------------------------------------------------------------------------- #
def _log(entry: dict, path: str | None = None) -> None:
    path = path or read_run_log_path()
    entry = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), **entry}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------- #
# read_apple_mail — the one orchestrated read operation.
#   list accounts -> COND-8 allow-list filter (BEFORE any message read)
#   -> per allowed account: bounded delta read -> cached-body skip -> collect.
# --------------------------------------------------------------------------- #
def read_apple_mail(
    since_iso: str,
    accounts=None,
    driver: ReadMailDriver | None = None,
    log_path: str | None = None,
) -> list[dict]:
    """Return new messages since `since_iso` from ALLOW-LISTED accounts only.

    Args:
        since_iso: ISO-8601 cutoff; only messages newer than this are returned.
        accounts:  optional explicit subset of account-domains to read. It is
                   INTERSECTED with the COND-8 allow-list — it can only ever
                   NARROW, never widen, what may be read. (Defense in depth.)
        driver:    injectable ReadMailDriver (tests pass a fake).
        log_path:  read run-log path override.

    Returns a list of {account, sender, subject, date, body} dicts.

    COND-8: a non-allow-listed account is never passed to the read script — its
    inbox is never enumerated, zero message reads. Fail closed: if the allow-list
    is empty, NOTHING is read.
    COND-5: on osascript timeout/error/Mail-not-running, raises ReadMailError
    (fail loud) after logging — never returns a partial scan as success.
    """
    driver = driver or ReadMailDriver()

    # Normalize the cutoff (fail closed on garbage) BEFORE any Mail call.
    try:
        cutoff = normalize_cutoff(since_iso)
    except ReadValidationError as exc:
        _log({"event": "read_rejected", "reason": str(exc), "since_iso": since_iso}, log_path)
        raise

    allow = set(read_allowed_accounts())

    # Optional caller-supplied narrowing: intersect, never widen.
    if accounts is not None:
        requested = {a.strip().lower() for a in accounts if isinstance(a, str) and a.strip()}
        allow = allow & requested

    # COND-8 fail-closed: empty allow-list => read NOTHING.
    if not allow:
        _log(
            {
                "event": "read_fail_closed",
                "reason": "read allow-list is empty/misconfigured — reading NOTHING",
                "configured_allow": sorted(read_allowed_accounts()),
                "requested": sorted(accounts) if accounts else None,
            },
            log_path,
        )
        return []

    # Phase 1: enumerate accounts (name + email; NO message read). Fail loud on error.
    try:
        all_accounts = driver.list_accounts()
    except ReadMailError as exc:
        _log({"event": "read_list_accounts_failed", "reason": str(exc)}, log_path)
        raise

    # COND-8 enforcement AT THE ACCOUNT BOUNDARY, before any message is read.
    read_accts: list[MailAccount] = []
    skipped_accts: list[dict] = []
    for acct in all_accounts:
        domain = acct.domain
        if domain and domain in allow:
            read_accts.append(acct)
        else:
            skipped_accts.append({"name": acct.name, "email": acct.email, "domain": domain})

    # Audit trail: which accounts were read vs skipped (provable zero personal reads).
    _log(
        {
            "event": "read_accounts_resolved",
            "allow_list": sorted(allow),
            "read_accounts": [{"name": a.name, "domain": a.domain} for a in read_accts],
            "skipped_accounts": skipped_accts,
            "cutoff": cutoff,
        },
        log_path,
    )

    results: list[dict] = []

    # Phase 2: read ONLY allow-listed accounts' inboxes (bounded delta).
    for acct in read_accts:
        try:
            raw = driver.read_inbox(acct.name, cutoff)
        except ReadMailError as exc:
            # COND-5: fail loud — do NOT swallow into a partial success.
            _log(
                {
                    "event": "read_account_failed",
                    "account": acct.name,
                    "domain": acct.domain,
                    "reason": str(exc),
                    "alert": "READ FAILED for an allow-listed account — scan is "
                    "incomplete. Failing loud (do not digest a partial scan).",
                },
                log_path,
            )
            raise

        kept = 0
        skipped_blank = 0
        for sender, subject, date, body in raw:
            body = body or ""
            # Cached-body integrity: a present message with a blank/partial body
            # is a not-yet-downloaded cached-mode read — skip+log, never return
            # a blank as if it were the real message.
            if len(body.strip()) < MIN_BODY_CHARS:
                skipped_blank += 1
                _log(
                    {
                        "event": "read_blank_body_skipped",
                        "account": acct.name,
                        "sender": sender,
                        "subject": subject,
                        "date": date,
                        "reason": "empty/partial body (cached/not-yet-downloaded) "
                        "— skipped, not returned as a blank message",
                    },
                    log_path,
                )
                continue
            # Bound an oversized untrusted body (truncate + flag).
            if len(body) > MAX_READ_BODY_LEN:
                body = body[:MAX_READ_BODY_LEN]
            results.append(
                {
                    "account": acct.name,
                    "sender": sender,
                    "subject": subject,
                    "date": date,
                    "body": body,
                }
            )
            kept += 1

        _log(
            {
                "event": "read_account_done",
                "account": acct.name,
                "domain": acct.domain,
                "messages_returned": kept,
                "blank_skipped": skipped_blank,
            },
            log_path,
        )

    _log(
        {
            "event": "read_complete",
            "accounts_read": [a.name for a in read_accts],
            "total_messages": len(results),
            "cutoff": cutoff,
        },
        log_path,
    )
    return results
