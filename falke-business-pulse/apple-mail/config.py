"""Configuration for the Apple Mail draft MCP server.

COND-6 recipient-domain allow-list lives here. An injection cannot draft to an
arbitrary stranger: every recipient (to + cc) must be on an allow-listed domain.

The allow-list is configurable via the env var APPLE_MAIL_DRAFT_ALLOWED_DOMAINS
(comma-separated), falling back to the Falke default. Keep it conservative —
this is the control that bounds the worst-case recipient of a fully
injection-controlled run (the security review §2.1, the one residual it will not hand-wave).
"""

from __future__ import annotations

import os

# Falke's known contact domains. Default; extend per engagement via the env var.
DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = ("falkecorp.com",)

# COND-6 (sender side) — the FROM-account allow-list. A person drafts ONLY from
# their own Falke mailbox, so the SENDER address of every draft must be on this
# list (in addition to being a really-configured account in Mail, which the
# AppleScript verifies). Entries may be a full address (e.g.
# "pm@falkecorp.com") or a bare DOMAIN (e.g. "falkecorp.com"); a
# from-account matches if its full address is listed OR its domain is listed.
# Configurable via APPLE_MAIL_DRAFT_FROM_ACCOUNTS (comma-separated).
#
# Fail-closed semantics: an UNSET env var falls back to this conservative default;
# an explicitly-EMPTY value admits NOTHING (no account may draft) — drafting from
# the wrong account is the live bug, so a misconfigured allow-list refuses rather
# than guesses.
DEFAULT_FROM_ACCOUNTS: tuple[str, ...] = ("falkecorp.com", "falkehoa.com")

# Field bounds (COND-6 / COND-7 hardening). Untrusted subject/body are length-
# bounded so an injection can't, e.g., blow memory or smuggle a huge payload.
MAX_SUBJECT_LEN = 998          # RFC 5322 line-length sanity bound for a header
MAX_BODY_LEN = 100_000         # generous for a digest; bounds untrusted body
MAX_RECIPIENTS = 25            # bounds the number of (allow-listed) recipients
MAX_ADDRESS_LEN = 254          # RFC 5321 max email address length

# COND-5 (body-clean verification) recency window, in seconds. After `save`, the
# draft-exists check matches a draft in the sender account's Drafts on
# subject + recipient + "created within this many seconds of now". Tight enough
# that a pre-existing same-subject/same-recipient draft is not falsely matched,
# loose enough to absorb the save + osascript round-trip latency.
DRAFT_VERIFY_WINDOW_SECONDS = 120

# Run-log location (COND-5). JSONL, one entry per attempt. Created on first write.
DEFAULT_RUN_LOG = os.path.expanduser(
    "~/Library/Logs/apple-mail-draft-mcp/run-log.jsonl"
)


def allowed_domains() -> tuple[str, ...]:
    """Return the active recipient-domain allow-list (env override or default)."""
    raw = os.environ.get("APPLE_MAIL_DRAFT_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return DEFAULT_ALLOWED_DOMAINS
    domains = tuple(
        d.strip().lower() for d in raw.split(",") if d.strip()
    )
    return domains or DEFAULT_ALLOWED_DOMAINS


def from_accounts_allowed() -> tuple[str, ...]:
    """Return the active FROM-account allow-list (env override or default).

    Entries are lower-cased. Each is either a full email address (contains '@')
    or a bare domain. See `from_account_allowed()` for the match rule.

    Fail-closed: env var UNSET -> default; env var SET-but-empty -> EMPTY tuple
    (no account may draft).
    """
    raw = os.environ.get("APPLE_MAIL_DRAFT_FROM_ACCOUNTS")
    if raw is None:
        return DEFAULT_FROM_ACCOUNTS
    # Explicitly set: honor it literally, including "set to empty" => admit nothing.
    return tuple(e.strip().lower() for e in raw.split(",") if e.strip())


def from_account_allowed(from_account: str) -> bool:
    """True iff `from_account` is permitted as a draft sender by the allow-list.

    A from-account matches if its FULL ADDRESS is on the list, OR its DOMAIN is on
    the list. Match is case-insensitive. An empty allow-list admits nothing.
    """
    addr = (from_account or "").strip().lower()
    if not addr or "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[1]
    allow = from_accounts_allowed()
    return addr in allow or domain in allow


def run_log_path() -> str:
    """Return the run-log path (env override or default)."""
    return os.environ.get("APPLE_MAIL_DRAFT_RUN_LOG", DEFAULT_RUN_LOG)


# --------------------------------------------------------------------------- #
# READ path config (COND-8 account allow-list — the privacy control)
# --------------------------------------------------------------------------- #
# The read tool reads ONLY accounts whose email-address DOMAIN is on this list.
# Default = both Falke domains (R2 folds falkehoa.com into Phase 2). A personal
# Gmail/iCloud in the same Mail.app is NOT on the list, so it is skipped entirely
# (zero message reads). Configurable via APPLE_MAIL_READ_ALLOWED_ACCOUNTS
# (comma-separated domains).
#
# CRITICAL fail-closed semantics differ from the draft allow-list: if the read
# allow-list is explicitly set to empty/garbage, the read tool reads NOTHING and
# logs (over-reading is a privacy breach, so the safe default is read-nothing —
# see memo §1B.4 / COND-8). An UNSET env var falls back to the conservative Falke
# default (both domains); an explicitly-empty value falls through to read-nothing.
DEFAULT_READ_ALLOWED_ACCOUNTS: tuple[str, ...] = ("falkecorp.com", "falkehoa.com")

# Per-account read CEILING (the message-count cap; ported from ara-business-pulse
# ADR 0001). The read script examines the inbox NEWEST-FIRST by index — the newest
# min(total, ceiling) messages — instead of the O(inbox) `whose date received >
# cutoff` walk that times out at 90s on years-large inboxes (Falke's Exchange
# accounts carry years of history). Rationale for 500: a since-last-run (24h)
# delta almost never exceeds 500 messages even on a busy business inbox, so a
# clean day is never falsely flagged; and examining ~500 message headers stays
# well under the 90s timeout. If a genuinely busier-than-500 window occurs, the
# account is flagged CAPPED (surfaced, never silently truncated — COND-5).
READ_MAX_MESSAGES_PER_ACCOUNT = 500

# Bounds for the read path (untrusted-body hardening). Bodies longer than this are
# truncated (and flagged) rather than pulled wholesale into context.
MAX_READ_BODY_LEN = 200_000     # generous per-message body cap for the digest scan
# A body with fewer than this many non-whitespace chars on a present message is
# treated as a blank/partial cached-mode read and skipped+logged (cached-body
# integrity — never return a blank body as if it were the real message).
MIN_BODY_CHARS = 1

# Read run-log (COND-5 read side). JSONL; records which accounts were read vs
# skipped (the COND-8 audit trail) and any fail-loud read errors.
DEFAULT_READ_RUN_LOG = os.path.expanduser(
    "~/Library/Logs/apple-mail-draft-mcp/read-log.jsonl"
)


def read_allowed_accounts() -> tuple[str, ...]:
    """Return the active READ account-domain allow-list (COND-8).

    Fail-closed semantics for read:
      - env var UNSET  -> the conservative Falke default (both domains).
      - env var SET but empty / whitespace / no valid domain after parsing
        -> EMPTY tuple => the read tool reads NOTHING (fail closed on misconfig).
      - env var SET with domains -> exactly those domains (lower-cased).
    """
    raw = os.environ.get("APPLE_MAIL_READ_ALLOWED_ACCOUNTS")
    if raw is None:
        return DEFAULT_READ_ALLOWED_ACCOUNTS
    # Explicitly set: honor it literally, including "set to empty" => read nothing.
    return tuple(d.strip().lower() for d in raw.split(",") if d.strip())


def read_run_log_path() -> str:
    """Return the read run-log path (env override or default)."""
    return os.environ.get("APPLE_MAIL_READ_RUN_LOG", DEFAULT_READ_RUN_LOG)


# Machine-readable LAST-SCAN integrity marker (COND-5 structural backstop).
# Written deterministically by the read core on every completed read — NOT by the
# model/skill. The pulse viewer reads this marker and injects the partial-scan
# banner into the served HTML BY CONSTRUCTION — so the human-facing warning does
# not depend on the model/skill choosing to render it (a prompt-injection in a
# surviving message cannot suppress it). Lives in the same local-config dir as
# the viewer's config.json.
DEFAULT_SCAN_STATUS_FILE = os.path.expanduser(
    "~/.falke-business-pulse/last-scan-status.json"
)
SCAN_STATUS_BASENAME = "last-scan-status.json"


def read_scan_status_path() -> str:
    """Return the last-scan-status marker path — NON-overridable by design.

    Returns the fixed dedicated file (no env override). The writer (read core) and
    the reader (pulse-server, which hard-codes the IDENTICAL path) must resolve to
    the SAME location or the partial-scan banner would silently stop working,
    and an env-pointable marker write would be a clobber footgun. Kept as a
    function so the writer has a single resolver.
    """
    return DEFAULT_SCAN_STATUS_FILE
