-- list_accounts.applescript
--
-- COND-8 (account allow-list — the privacy control), phase 1 of 2.
--
-- This script ONLY enumerates the accounts configured in Mail and returns, per
-- account, its NAME and its email ADDRESS. It does NOT enumerate mailboxes and
-- does NOT read a single message. It exists so the Python layer can decide which
-- accounts are on the allow-list BEFORE any message is ever touched — enforcement
-- at the account boundary, by construction, not filtering-after-read.
--
-- Output: one line per account, fields TAB-separated:
--     <account name>\t<primary email address>
-- An account may expose multiple addresses (`email addresses` is a list); we emit
-- the FIRST (primary). The Python side matches on the address's DOMAIN, not the
-- display name (which a user can rename) — see read_core.py.
--
-- Read-only: emits NO write verb (no save/send/delete/move) and reads NO message
-- content. The only properties touched are account `name` and `email addresses`.
--
-- argv: none.

on run argv
	set outLines to {}
	tell application "Mail"
		repeat with acct in accounts
			set acctName to (name of acct) as text
			set addrList to (email addresses of acct)
			if (count of addrList) > 0 then
				set acctEmail to (item 1 of addrList) as text
			else
				set acctEmail to ""
			end if
			set end of outLines to (acctName & tab & acctEmail)
		end repeat
	end tell

	-- Join with linefeed. (AppleScript text item delimiters.)
	set AppleScript's text item delimiters to linefeed
	set outText to outLines as text
	set AppleScript's text item delimiters to ""
	return outText
end run
