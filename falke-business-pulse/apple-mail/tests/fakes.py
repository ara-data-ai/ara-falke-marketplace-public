"""Read-side test doubles for read_core (no Mail.app / TCC / MCP SDK required).

This is the READ-side subset of the canonical `tests/fakes.py` in the FALKE dev
tree (apple-mail-draft-mcp). The marketplace plugin currently ships read-path
tests only; the draft-side doubles (RecordingMailDriver, etc.) live in the FALKE
tree and can be ported here if/when draft tests are added to the plugin repo.
"""

from __future__ import annotations

from config import READ_MAX_MESSAGES_PER_ACCOUNT
from read_core import MailAccount, ReadMailDriver, ReadMailError, ReadMailTimeout


class FakeReadMailDriver(ReadMailDriver):
    """A ReadMailDriver backed by an in-memory 'Mail world'.

    `world` maps account NAME -> {"email": <addr>, "messages": [(sender, subject,
    date, body), ...]}. The driver records EVERY account name it was asked to
    read via read_inbox() in `self.read_calls`, so a COND-8 test can assert a
    non-allow-listed (personal) account was NEVER passed to read_inbox — i.e.
    ZERO message reads occurred against it. Enforcement happens in read_core
    (the account boundary), BEFORE this driver's read_inbox is ever called.

    Fault injection (all optional; empty defaults == the plain FALKE fake):
      - `timeout_accounts`  : read_inbox() on one of these raises ReadMailTimeout,
        modeling a large inbox exceeding the 90s ReadMailDriver timeout.
        A per-account DEGRADABLE fault (max-availability).
      - `error_accounts`    : read_inbox() on one of these raises a plain
        ReadMailError modeling a pre-timeout per-account STALL (rc!=0, e.g.
        AppleEvent -1712). Per WS1 this is now ALSO per-account DEGRADABLE (it is
        scoped to one account; enumeration already succeeded).
      - `list_accounts_error`: list_accounts() itself raises ReadMailError, modeling
        a SYSTEMIC enumeration failure (Mail not running / auth) — stays FAIL-LOUD.
    """

    def __init__(
        self,
        world: dict[str, dict],
        timeout_accounts: set[str] | None = None,
        error_accounts: set[str] | None = None,
        list_accounts_error: bool = False,
        saturated_accounts: set[str] | None = None,
    ):
        super().__init__()
        self._world = world
        self._timeout_accounts = set(timeout_accounts or ())
        self._error_accounts = set(error_accounts or ())
        self._list_accounts_error = list_accounts_error
        # Accounts whose read hit the per-account ceiling (CAP): read_inbox returns
        # saturated=True (older in-window mail may be unread => read_core flags CAPPED).
        self._saturated_accounts = set(saturated_accounts or ())
        self.list_accounts_calls = 0
        self.read_calls: list[str] = []  # account names read_inbox was called on
        self.read_cutoffs: list[str] = []

    def list_accounts(self):  # type: ignore[override]
        self.list_accounts_calls += 1
        if self._list_accounts_error:
            # Systemic enumeration failure (e.g. Mail not running) — fail loud.
            raise ReadMailError("osascript list_accounts failed (modeled)")
        return [
            MailAccount(name=name, email=info.get("email", ""))
            for name, info in self._world.items()
        ]

    def read_inbox(self, account_name, cutoff):  # type: ignore[override]
        # Record that a message-level read was attempted against this account.
        self.read_calls.append(account_name)
        self.read_cutoffs.append(cutoff)
        if account_name in self._timeout_accounts:
            # Model the live defect: enumerating this (large) inbox
            # exceeds the ReadMailDriver timeout. Raising the TIMEOUT subtype lets
            # read_core degrades this ONE account (max-availability).
            raise ReadMailTimeout(
                f"osascript read timed out (modeled) for {account_name!r}"
            )
        if account_name in self._error_accounts:
            # Pre-timeout per-account STALL surfacing as rc!=0 (AppleEvent -1712).
            raise ReadMailError(
                f"osascript read failed (rc=1): AppleEvent timed out (-1712) "
                f"for {account_name!r}"
            )
        info = self._world.get(account_name)
        records = list(info.get("messages", [])) if info else []
        if account_name in self._saturated_accounts:
            # Model a SATURATED read: there is unexamined mail (total > examined) AND
            # the far-end boundary of the examined range is still in window
            # (boundary_in_window=True) => _is_saturated True => read_core flags CAPPED.
            # (records, examined, boundary_in_window, total)
            return records, READ_MAX_MESSAGES_PER_ACCOUNT, True, READ_MAX_MESSAGES_PER_ACCOUNT + 100
        # Normal read: examined the whole inbox (total == examined) => complete
        # (total > examined is False) => never falsely capped.
        return records, len(records), False, len(records)
