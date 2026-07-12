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
  COND-7  (AppleScript string-injection). The account name, cutoff, and ceiling
          go to osascript as ARGUMENTS (list argv, shell=False) read by
          `on run argv`, never interpolated into AppleScript source.
  bounded read + CAP. read_account.applescript examines the NEWEST min(total,
          ceiling) messages of the named account's INBOX by index (one bulk date
          fetch) and returns every in-window one — O(ceiling), never the
          O(inbox) `whose date received > cutoff` walk that times out at 90s on
          years-large inboxes. The completeness DECISION (`saturated`/CAPPED)
          is made HERE in unit-tested Python, not in the AppleScript.
  cached-body integrity. A message present in the delta window but with a blank/
          partial body (cached/not-yet-downloaded) is SKIPPED + logged — never
          returned as if it were the real message.
  COND-5  (fail-loud + max-availability). A SYSTEMIC failure (list_accounts /
          Mail not running / osascript missing / total wipeout) raises LOUD. A
          per-account timeout/stall degrades THAT account (skip + status
          "partial"); a CAPPED account marks the scan "partial". A partial scan
          is surfaced to the human and written to a machine-readable marker the
          viewer renders BY CONSTRUCTION — never presented as a clean success.
  COND-2  READ-ONLY. The only Mail operations are property reads (in the two
          static read scripts). No write/draft/delete/move/mark verb.
          ReadMailDriver exposes only list_accounts + read_inbox.
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
    READ_MAX_MESSAGES_PER_ACCOUNT,
    read_allowed_accounts,
    read_run_log_path,
    read_scan_status_path,
    SCAN_STATUS_BASENAME,
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


class ReadMailTimeout(ReadMailError):
    """A per-account read exceeded the driver timeout.

    A distinct subtype so the orchestrator can apply the max-availability policy:
    a per-account read TIMEOUT is DEGRADABLE (skip that ONE account, mark the scan
    partial, keep the accounts that did return), whereas a non-timeout
    ReadMailError (Mail not running, auth failure, osascript missing, list_accounts
    failure) stays FAIL-LOUD (systemic — raise). Degradation NEVER presents a
    partial scan as a clean success: the partial status is surfaced to the human
    (COND-5's real invariant).
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


def _to_int(s: str) -> int:
    """Parse an integer META field; 0 on anything unparseable (defensive)."""
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return 0


def _is_saturated(examined: int, boundary_in_window: bool, total: int) -> bool:
    """COND-5 completeness DECISION (BOUNDARY rule; unit-tested from the INVARIANT).

    Invariant: if in-window mail could exist that we did NOT examine, `saturated` MUST
    be True. The reader examined the newest `examined` = min(total, ceiling) messages
    by index and collected EVERY in-window one among them (no early stop). There is
    unexamined mail iff `total > examined`. The OLDEST-BY-INDEX examined message (the
    far end of the examined range) is the window boundary: if it is STILL in-window
    (`boundary_in_window`), the cutoff falls BEYOND the examined range, so in-window
    mail may sit among the unexamined messages => CAPPED.

    Complete (not saturated) otherwise: we examined the whole inbox (`total <=
    examined`), OR the far-end boundary is already out of window (the delta ended
    within the examined range — so a small delta on a huge inbox is never falsely
    capped). This is order-robust: it does NOT rely on "we saw some out-of-window
    message" (which a single interleaved message could satisfy while in-window mail
    still sits beyond the ceiling).
    """
    return total > examined and boundary_in_window


# --------------------------------------------------------------------------- #
# ReadMailDriver — the ONLY boundary that touches Mail.app for reading.
# Mockable for tests. Exposes EXACTLY two read-only operations.
# --------------------------------------------------------------------------- #
class ReadMailDriver:
    """Runs the two static read AppleScript files via osascript (shell=False,
    list argv). Tests inject a fake, so the suite never needs Mail.app/TCC.

    READ-ONLY by construction: list_accounts (name+email, no message read) and
    read_inbox (one named account's inbox, capped newest-first read). No write
    op exists.
    """

    def __init__(self, timeout: float = 90.0):
        # Read can be slower than draft (index scan over the slow AppleScript
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
            # A scan that hangs Mail must never silently vanish. Raise the
            # TIMEOUT subtype so the orchestrator can degrade this ONE account
            # (max-availability) while still surfacing the scan as partial — never
            # as a clean success (COND-5). Systemic failures stay plain
            # ReadMailError (below) and remain fully fail-loud.
            raise ReadMailTimeout(
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

    def read_inbox(
        self, account_name: str, cutoff: str
    ) -> tuple[list[tuple[str, str, str, str]], int, bool, int]:
        """Read ONE named account's inbox, newest-first: examine the newest
        min(total, ceiling) messages by index and return EVERY in-window message
        among them (ordering-independent — NO early stop, so an interleaved
        out-of-window message cannot truncate the collection).

        Returns (records, examined, boundary_in_window, total):
          - records:            in-window (sender, subject, date, body) tuples — the
                                AppleScript control-char framing is decoded here.
          - examined:           how many messages were examined (min(total, ceiling)).
          - boundary_in_window: True iff the OLDEST-BY-INDEX examined message (the far
                                end of the examined range) is still in window.
          - total:              total messages in the inbox.
        read_apple_mail decides `saturated`/CAPPED from these via _is_saturated()
        (saturated = total > examined AND boundary_in_window) — the completeness
        DECISION lives in unit-tested Python, not the AppleScript.

        Output format: META `examined US boundary_in_window US total`, then the
        GS-framed records — split on the FIRST newline (META vs records). The META
        line is unspoofable: message bodies (which may carry newlines) all appear
        AFTER it, and the script emits it before any record.
        """
        out = self._run(
            READ_ACCOUNT_SCRIPT,
            [account_name, cutoff, str(READ_MAX_MESSAGES_PER_ACCOUNT)],
        )
        # META header (before the first newline) vs the record stream (after it).
        meta, _, stream = out.partition("\n")
        meta_fields = meta.split(_US)
        examined = _to_int(meta_fields[0]) if len(meta_fields) > 0 else 0
        boundary_in_window = (
            meta_fields[1].strip() == "1" if len(meta_fields) > 1 else False
        )
        total = _to_int(meta_fields[2]) if len(meta_fields) > 2 else 0

        records: list[tuple[str, str, str, str]] = []
        if stream.strip():
            for rec in stream.split(_GS):
                if rec == "":
                    continue
                fields = rec.split(_US)
                # Pad defensively to 4 fields.
                while len(fields) < 4:
                    fields.append("")
                records.append((fields[0], fields[1], fields[2], fields[3]))
        return records, examined, boundary_in_window, total


# --------------------------------------------------------------------------- #
# Read run-log (COND-5 + COND-8 audit trail)
# --------------------------------------------------------------------------- #
def _log(entry: dict, path: str | None = None) -> None:
    path = path or read_run_log_path()
    entry = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), **entry}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _write_scan_status(
    status: str,
    accounts_failed: list[dict],
    accounts_capped: list[dict],
    cutoff: str,
    path: str,
) -> None:
    """Overwrite the machine-readable LAST-SCAN integrity marker (COND-5 backstop).

    Written DETERMINISTICALLY by the read core on every completed read — NOT by the
    model/skill. The pulse viewer (pulse-server) reads this marker and injects the
    "incomplete scan" banner into the served HTML BY CONSTRUCTION when status is
    "partial", so a prompt-injection in a SURVIVING account's message cannot
    suppress the human-facing warning (the render is not the model's decision).
    Records only account name + domain — never message content (C1). Best-effort:
    a marker-write failure must NEVER break or fail the read itself.
    """
    # Clobber guard: the marker is its OWN dedicated file. Refuse to write to
    # anything whose basename isn't the marker's — so a mispointed path can never
    # overwrite config.json / any other file.
    if os.path.basename(path) != SCAN_STATUS_BASENAME:
        return
    payload = {
        "status": status,
        "accounts_failed": [
            {"account": f["account"], "domain": f["domain"]} for f in accounts_failed
        ],
        "accounts_capped": [
            {"account": c["account"], "domain": c["domain"]} for c in accounts_capped
        ],
        "cutoff": cutoff,
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass  # the marker is a backstop; never let its write error fail the read


# --------------------------------------------------------------------------- #
# read_apple_mail — the one orchestrated read operation.
#   list accounts -> COND-8 allow-list filter (BEFORE any message read)
#   -> per allowed account: capped newest-first read -> cached-body skip -> collect.
# --------------------------------------------------------------------------- #
def read_apple_mail(
    since_iso: str,
    accounts=None,
    driver: ReadMailDriver | None = None,
    log_path: str | None = None,
    status_path: str | None = None,
) -> dict:
    """Return new messages since `since_iso` from ALLOW-LISTED accounts only.

    Args:
        since_iso: ISO-8601 cutoff; only messages newer than this are returned.
        accounts:  optional explicit subset of account-domains to read. It is
                   INTERSECTED with the COND-8 allow-list — it can only ever
                   NARROW, never widen, what may be read. (Defense in depth.)
        driver:    injectable ReadMailDriver (tests pass a fake).
        log_path:  read run-log path override.
        status_path: last-scan integrity-marker path override (the viewer reads
                   this to render the partial-scan banner structurally).

    Returns a structured result dict:
        {
          "status": "ok" | "partial",
          "messages": [ {account, sender, subject, date, body}, ... ],
          "accounts_read":   [account names read successfully this run],
          "accounts_failed": [ {account, domain, reason}, ... ],  # per-account read
                             #   failures degraded (skipped) this run (timeout/stall)
          "accounts_capped": [ {account, domain}, ... ],  # read hit the per-account
                             #   message ceiling; OLDER in-window mail may be unread
          "cutoff": "<normalized cutoff>",  # RUN TOKEN — matches the status marker;
                                            # stamp it into the saved pulse so the
                                            # viewer can correlate marker <-> pulse.
        }
    status is "partial" iff at least one allow-listed account either FAILED (timeout/
    stall, skipped) or was CAPPED (read but older in-window mail may be unread) while
    others still returned — the caller MUST surface a partial scan PROMINENTLY (never
    render it as a complete pulse). "ok" otherwise.

    COND-8: a non-allow-listed account (e.g. a personal Gmail/iCloud in the same
    Mail.app) is never passed to the read script — its inbox is never enumerated,
    zero message reads. Fail closed: if the allow-list is empty, NOTHING is read.
    COND-5 (max-availability): ANY per-account read failure — a 90s TIMEOUT or a
    pre-timeout STALL surfacing as rc!=0 (e.g. AppleEvent -1712) — degrades that ONE
    account (skip + flag partial) instead of aborting the whole scan, but a partial
    scan is NEVER presented as a clean success (status is "partial" and the failed
    accounts are named). SYSTEMIC conditions still raise ReadMailError (fail loud):
    a failure at account ENUMERATION (list_accounts — Mail not running / auth /
    osascript missing) and a TOTAL wipeout (zero accounts succeeded). On every
    completed read the integrity status is ALSO written to a machine-readable marker
    (read_scan_status_path) so the viewer can surface a partial scan BY
    CONSTRUCTION — not by model discretion.
    """
    driver = driver or ReadMailDriver()
    status_path = status_path or read_scan_status_path()

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
        # Clear/overwrite the integrity marker: this run read nothing by policy,
        # which is a clean (not degraded) state — a prior "partial" must not linger.
        _write_scan_status("ok", [], [], cutoff, status_path)
        return {
            "status": "ok",
            "messages": [],
            "accounts_read": [],
            "accounts_failed": [],
            "accounts_capped": [],
            "cutoff": cutoff,  # run token: stamp into the saved pulse (viewer correlates)
        }

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

    # Phase 2: read ONLY allow-listed accounts' inboxes (capped newest-first).
    # Max-availability (COND-5): a per-account read failure degrades THAT account
    # (skip + flag PARTIAL) so one slow/stalled inbox can't kill the scan; systemic
    # conditions and a total wipeout still fail loud, and any partial is surfaced
    # below — never presented as a clean success.
    accounts_read: list[str] = []
    accounts_failed: list[dict] = []
    accounts_capped: list[dict] = []
    for acct in read_accts:
        try:
            raw, examined, boundary_in_window, total = driver.read_inbox(acct.name, cutoff)
        except ReadMailError as exc:
            # Per-account failure isolation: ANY error from a SINGLE account's
            # read_inbox degrades THAT account (skip + flag PARTIAL): a 90s
            # subprocess TIMEOUT (ReadMailTimeout) OR a pre-timeout per-account
            # STALL surfacing as rc!=0 (e.g. AppleEvent -1712). Enumeration
            # (list_accounts, Phase 1) already succeeded, so the other accounts
            # remain attemptable — this is per-account, not systemic. SYSTEMIC
            # conditions (Mail not running / auth / osascript missing) surface at
            # list_accounts (Phase 1 -> fail-loud) or as a TOTAL wipeout (floor
            # below -> fail-loud). NOT swallowed: recorded + surfaced as PARTIAL so
            # a partial is never presented as complete (COND-5).
            kind = "timeout" if isinstance(exc, ReadMailTimeout) else "stall"
            accounts_failed.append(
                {"account": acct.name, "domain": acct.domain, "reason": str(exc)}
            )
            _log(
                {
                    "event": "read_account_degraded",
                    "account": acct.name,
                    "domain": acct.domain,
                    "kind": kind,
                    "reason": str(exc),
                    "alert": "READ FAILED for an allow-listed account (per-account "
                    f"{kind}) — that account is SKIPPED this run and the scan is "
                    "marked PARTIAL (max-availability). Surfaced to the human; NOT "
                    "presented as a complete scan.",
                },
                log_path,
            )
            continue

        accounts_read.append(acct.name)

        # CAP (COND-5): the completeness DECISION is made HERE (testable Python), not
        # in the AppleScript. The reader examined the newest min(total, ceiling)
        # messages and collected EVERY in-window one regardless of order; it is
        # saturated iff there is unexamined mail (total > examined) AND the far-end
        # boundary of the examined range is still in-window (in-window mail may sit
        # beyond it). Surface a saturated account as CAPPED (scan marked partial) —
        # never a silent truncation. C1: log account + domain + counts only.
        if _is_saturated(examined, boundary_in_window, total):
            accounts_capped.append({"account": acct.name, "domain": acct.domain})
            _log(
                {
                    "event": "read_account_capped",
                    "account": acct.name,
                    "domain": acct.domain,
                    "cap": READ_MAX_MESSAGES_PER_ACCOUNT,
                    "examined": examined,
                    "total": total,
                    "alert": "READ CAP reached for an allow-listed account — the "
                    "newest in-window messages were read, but OLDER in-window mail "
                    "may be unread. Scan marked PARTIAL (capped); surfaced to the "
                    "human, NOT presented as a complete scan.",
                },
                log_path,
            )

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

    # Fail-loud floor: max-availability degrades on per-account failures, but a
    # TOTAL wipeout — accounts were attempted and EVERY one FAILED (timed out or
    # stalled; zero succeeded) — is not a scan. Fail loud rather than present
    # emptiness as a success (COND-5).
    if accounts_failed and not accounts_read:
        _log(
            {
                "event": "read_total_wipeout",
                "attempted": [a.name for a in read_accts],
                "failed": [f["account"] for f in accounts_failed],
                "alert": "ALL allow-listed account reads FAILED (timed out or "
                "stalled; zero returned) — failing loud rather than reporting an "
                "empty scan as success.",
            },
            log_path,
        )
        raise ReadMailError(
            f"all {len(accounts_failed)} allow-listed account read(s) failed "
            "(timed out or stalled) — no account returned; failing loud "
            "(COND-5: not an empty success)."
        )

    # COND-5: a partial scan is any account that failed (timeout/stall) OR was
    # CAPPED (read but older in-window mail may be unread). Either way the scan is
    # incomplete and must never be presented as clean.
    status = "partial" if (accounts_failed or accounts_capped) else "ok"

    _log(
        {
            "event": "read_complete",
            "status": status,
            "accounts_read": accounts_read,
            "accounts_failed": [f["account"] for f in accounts_failed],
            "accounts_capped": [c["account"] for c in accounts_capped],
            "total_messages": len(results),
            "cutoff": cutoff,
        },
        log_path,
    )
    # COND-5 structural backstop: record this scan's integrity status where
    # pulse-server can inject the partial-scan banner independent of the model.
    _write_scan_status(status, accounts_failed, accounts_capped, cutoff, status_path)
    return {
        "status": status,
        "messages": results,
        "accounts_read": accounts_read,
        "accounts_failed": accounts_failed,
        "accounts_capped": accounts_capped,
        "cutoff": cutoff,  # run token: stamp into the saved pulse (viewer correlates)
    }
