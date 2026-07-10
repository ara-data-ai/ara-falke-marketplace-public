"""draft_core — the security-critical core of the Apple Mail draft MCP server.

This module is deliberately separate from the MCP transport wiring (server.py)
so the full test suite (COND-2/5/6/7) runs WITHOUT the MCP SDK and WITHOUT
driving Mail.app. The only boundary that touches Mail.app is `MailDriver`,
which is mockable.

Conditions implemented here:
  COND-7  build_argv(): untrusted values (from-account, subject, body,
          recipients) become osascript ARGUMENTS, never inlined AppleScript
          source. _run_osascript() invokes osascript with a LIST argv
          (shell=False) and a STATIC .applescript file — there is no string
          interpolation of untrusted data into source anywhere.
  COND-6  validate_request(): {from_account, recipient, subject, body} all
          present + well formed, the from-account on the FROM-account allow-list,
          recipients on the recipient-domain allow-list, length bounds. On any
          violation -> raise ValidationError -> caller fails CLOSED (no draft).
  COND-5  create_draft(): create_draft.applescript sets the outgoing message's
          `sender` to the from-account so Mail saves the draft into THAT account's
          Drafts (FIX 1). After `save`, draft_exists.applescript verifies the draft
          landed by matching, in the SENDER account's Drafts, a recent draft on
          subject + the to-recipient + recency (FIX 2 — BODY-CLEAN: no marker is
          added to the draft body). If MISSING -> raise DraftAssertionError (fail
          LOUD) and log. Plus a run-log entry per try.
  COND-2  the only Mail verb reachable from this module is `save` (in
          create_draft.applescript, plus a `set` on the new message's own
          `sender`) and a read-only existence query. There is no function, no
          code path, that sends/deletes/moves/modifies another message.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from config import (
    DRAFT_VERIFY_WINDOW_SECONDS,
    MAX_ADDRESS_LEN,
    MAX_BODY_LEN,
    MAX_RECIPIENTS,
    MAX_SUBJECT_LEN,
    allowed_domains,
    from_account_allowed,
    from_accounts_allowed,
    run_log_path,
)

# --- Paths to the STATIC AppleScript files (never templated) ----------------
_HERE = Path(__file__).resolve().parent
CREATE_DRAFT_SCRIPT = _HERE / "applescript" / "create_draft.applescript"
DRAFT_EXISTS_SCRIPT = _HERE / "applescript" / "draft_exists.applescript"

OSASCRIPT = "/usr/bin/osascript"

# Conservative email-address shape. Bounds the address before it ever reaches
# osascript; the allow-list then bounds the DOMAIN. (Belt and suspenders — the
# arg-passing of COND-7 already makes the value non-executable; this stops
# malformed/garbage recipients reaching Mail at all.)
_ADDRESS_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


class ValidationError(ValueError):
    """COND-6 fail-closed: request is malformed / off allow-list. No draft made."""


class DraftAssertionError(RuntimeError):
    """COND-5 fail-loud: save returned but the draft is not in Drafts."""


class MailDriverError(RuntimeError):
    """osascript / Mail.app failed (e.g. TCC grant missing, Mail not running)."""


@dataclass(frozen=True)
class DraftRequest:
    from_account: str
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# COND-6 — validation + recipient-domain allow-list (fail closed)
# --------------------------------------------------------------------------- #
def _domain_of(address: str) -> str:
    m = _ADDRESS_RE.match(address.strip())
    if not m:
        raise ValidationError(f"malformed recipient address: {address!r}")
    return m.group(1).lower()


def validate_request(
    from_account,
    to,
    subject,
    body,
    cc=None,
) -> DraftRequest:
    """Validate inputs and enforce BOTH allow-lists.

    Raises ValidationError on ANY problem so the caller fails CLOSED — no draft is
    created when the shape is wrong, the sender is off the FROM-account allow-list,
    or a recipient is off the recipient-domain allow-list.
    """
    allowed = allowed_domains()

    # FROM-account (sender) — required, well-formed, and on the from-account
    # allow-list. A person drafts ONLY from their own Falke mailbox. The
    # AppleScript additionally fails closed if the address isn't a really-
    # configured account in Mail.
    if not isinstance(from_account, str) or not from_account.strip():
        raise ValidationError("from_account (sender address) is required")
    from_account = from_account.strip()
    if len(from_account) > MAX_ADDRESS_LEN:
        raise ValidationError(f"from_account too long: {from_account!r}")
    _domain_of(from_account)  # shape check; raises on malformed
    if not from_account_allowed(from_account):
        raise ValidationError(
            f"from_account {from_account!r} not on the from-account allow-list "
            f"{from_accounts_allowed()}"
        )

    # Normalize recipient inputs to lists of non-empty strings.
    def _as_list(value, label):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValidationError(f"{label} must be a string or list of strings")
        out = []
        for item in value:
            if not isinstance(item, str):
                raise ValidationError(f"{label} entries must be strings")
            item = item.strip()
            if item:
                out.append(item)
        return out

    to_list = _as_list(to, "to")
    cc_list = _as_list(cc, "cc")

    # {recipient, subject, body} all present.
    if not to_list:
        raise ValidationError("at least one 'to' recipient is required")
    if not isinstance(subject, str) or not subject.strip():
        raise ValidationError("subject is required and must be non-empty")
    if not isinstance(body, str) or not body.strip():
        raise ValidationError("body is required and must be non-empty")

    # Length bounds (untrusted data hardening).
    if len(subject) > MAX_SUBJECT_LEN:
        raise ValidationError(f"subject exceeds {MAX_SUBJECT_LEN} chars")
    if len(body) > MAX_BODY_LEN:
        raise ValidationError(f"body exceeds {MAX_BODY_LEN} chars")
    if len(to_list) + len(cc_list) > MAX_RECIPIENTS:
        raise ValidationError(f"too many recipients (max {MAX_RECIPIENTS})")

    # Per-recipient: shape + length + DOMAIN allow-list.
    for addr in to_list + cc_list:
        if len(addr) > MAX_ADDRESS_LEN:
            raise ValidationError(f"recipient address too long: {addr!r}")
        domain = _domain_of(addr)  # raises on malformed
        if domain not in allowed:
            raise ValidationError(
                f"recipient domain {domain!r} not on allow-list {allowed}"
            )

    return DraftRequest(
        from_account=from_account, to=to_list, subject=subject, body=body, cc=cc_list
    )


# --------------------------------------------------------------------------- #
# COND-7 — argv construction: untrusted values become ARGUMENTS, not source
# --------------------------------------------------------------------------- #
def build_create_argv(request: DraftRequest) -> list[str]:
    """Build the positional argv for create_draft.applescript.

    The returned list is passed to osascript as ARGUMENTS (shell=False, list
    form). No element is ever interpolated into AppleScript source. The
    AppleScript reads them via `on run argv` as DATA — including the from-account
    sender address, which it resolves to a configured Mail account and sets as the
    message `sender` (FIX 1) so the draft lands in that account's Drafts.

    Layout (must match create_draft.applescript's `on run argv`):
      [from_account, subject, body, str(len(to)), *to, *cc]
    """
    argv: list[str] = [
        request.from_account,
        request.subject,
        request.body,
        str(len(request.to)),
    ]
    argv.extend(request.to)
    argv.extend(request.cc)
    return argv


def build_exists_argv(request: DraftRequest, window_seconds: int) -> list[str]:
    """Build the positional argv for draft_exists.applescript (BODY-CLEAN verify).

    Verification matches a recent draft in the SENDER account's Drafts on
    subject + the (first) to-recipient + recency — no body marker. All values are
    osascript ARGUMENTS, never inlined into source.

    Layout (must match draft_exists.applescript's `on run argv`):
      [from_account, subject, to_recipient, str(window_seconds)]
    """
    return [
        request.from_account,
        request.subject,
        request.to[0],
        str(int(window_seconds)),
    ]


# --------------------------------------------------------------------------- #
# MailDriver — the ONLY boundary that touches Mail.app. Mockable for tests.
# --------------------------------------------------------------------------- #
class MailDriver:
    """Runs the static AppleScript files via osascript (shell=False, list argv).

    Tests inject a fake driver, so the suite never needs Mail.app or the TCC
    grant. This class is the single place osascript is invoked.
    """

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def _run(self, script: Path, args: list[str]) -> str:
        # shell=False, list argv: untrusted args are data to osascript, the
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
            raise MailDriverError(f"osascript not available: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise MailDriverError(f"osascript timed out: {exc}") from exc
        if proc.returncode != 0:
            raise MailDriverError(
                f"osascript failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    def create_draft(self, request: DraftRequest) -> str:
        """Run create_draft.applescript; set the message `sender` to the request's
        from-account so the draft lands in THAT account's Drafts (FIX 1), and
        return the outgoing message's id (audit log only — verification keys off
        subject+recipient+recency, not this id, because Mail re-ids on save)."""
        argv = build_create_argv(request)
        outgoing_id = self._run(CREATE_DRAFT_SCRIPT, argv)
        if not outgoing_id:
            raise MailDriverError("create_draft returned no outgoing message id")
        return outgoing_id

    def draft_exists(self, request: DraftRequest, window_seconds: int) -> bool:
        """Run draft_exists.applescript (read-only); True iff a recent draft in the
        SENDER account's Drafts matches subject + the to-recipient + recency
        (BODY-CLEAN — no marker in the draft body). Robust across Mail's save id
        re-assignment and per-account Drafts mailboxes."""
        argv = build_exists_argv(request, window_seconds)
        result = self._run(DRAFT_EXISTS_SCRIPT, argv)
        return result.strip() == "EXISTS"


# --------------------------------------------------------------------------- #
# Run-log (COND-5 audit trail)
# --------------------------------------------------------------------------- #
def _log(entry: dict, path: str | None = None) -> None:
    path = path or run_log_path()
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        **entry,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------- #
# create_draft — the one orchestrated operation (validate -> save -> assert)
# --------------------------------------------------------------------------- #
def create_draft(
    from_account,
    to,
    subject,
    body,
    cc=None,
    driver: MailDriver | None = None,
    log_path: str | None = None,
    verify_window_seconds: int = DRAFT_VERIFY_WINDOW_SECONDS,
) -> dict:
    """Create ONE Apple Mail draft FROM `from_account`. The single operation this
    module exposes.

    Flow:
      1. validate_request (COND-6) — from-account + recipient allow-lists; fail
         CLOSED + log on any violation.
      2. driver.create_draft -> sets the message `sender` to the from-account
         (FIX 1) then `save` only (COND-2 / COND-7), so the draft lands in that
         account's Drafts.
      3. driver.draft_exists — BODY-CLEAN verify: match a recent draft in the
         SENDER account's Drafts on subject + the to-recipient + recency (FIX 2);
         if MISSING, fail LOUD (COND-5).
      4. log success.

    Returns {"status": "ok", "draft_id": ..., "from_account": ...,
    "recipients": [...]} on success. Raises ValidationError / DraftAssertionError
    / MailDriverError otherwise (always after logging).
    """
    driver = driver or MailDriver()

    # 1. COND-6 — validate + BOTH allow-lists, fail closed.
    try:
        request = validate_request(from_account, to, subject, body, cc)
    except ValidationError as exc:
        _log(
            {
                "event": "draft_rejected",
                "reason": str(exc),
                "from_account": from_account,
                "to": to,
                "subject_present": bool(isinstance(subject, str) and subject.strip()),
            },
            log_path,
        )
        raise

    recipients = request.to + request.cc

    # 2. COND-2 / COND-7 — sender-set + save-only, args-not-source. The outgoing id
    # is logged for audit; it is NOT what we verify against (Mail re-ids the saved
    # draft).
    try:
        outgoing_id = driver.create_draft(request)
    except MailDriverError as exc:
        _log(
            {
                "event": "draft_save_failed",
                "reason": str(exc),
                "from_account": request.from_account,
                "to": request.to,
                "cc": request.cc,
                "subject": request.subject,
            },
            log_path,
        )
        raise

    # 3. COND-5 — fail-loud BODY-CLEAN draft-exists assertion in the sender
    # account's Drafts (subject + to-recipient + recency).
    try:
        exists = driver.draft_exists(request, verify_window_seconds)
    except MailDriverError as exc:
        _log(
            {
                "event": "draft_assertion_error",
                "reason": f"existence query failed: {exc}",
                "outgoing_id": outgoing_id,
                "from_account": request.from_account,
                "to": request.to,
            },
            log_path,
        )
        raise DraftAssertionError(
            f"could not verify draft (from {request.from_account}, "
            f"subject {request.subject!r}) exists: {exc}"
        ) from exc

    if not exists:
        _log(
            {
                "event": "draft_missing_after_save",
                "outgoing_id": outgoing_id,
                "from_account": request.from_account,
                "to": request.to,
                "cc": request.cc,
                "subject": request.subject,
                "alert": "DRAFT NOT FOUND AFTER SAVE — no recent draft matching "
                "subject + recipient was found in the sender account's Drafts. The "
                "TCC grant may have silently broken, the sender account may be "
                "wrong, or the draft did not persist. Investigate.",
            },
            log_path,
        )
        raise DraftAssertionError(
            f"draft not found in {request.from_account}'s Drafts after save "
            f"(no recent match for subject {request.subject!r} + recipient "
            f"{request.to[0]!r}) — failing loud (save did not verifiably land; "
            "TCC Automation grant may have silently broken)"
        )

    # 4. success.
    _log(
        {
            "event": "draft_created",
            "outgoing_id": outgoing_id,
            "from_account": request.from_account,
            "to": request.to,
            "cc": request.cc,
            "subject": request.subject,
        },
        log_path,
    )
    return {
        "status": "ok",
        "draft_id": outgoing_id,
        "from_account": request.from_account,
        "recipients": recipients,
    }
