-- create_draft.applescript
--
-- COND-7 (AppleScript string-injection prevention) — load-bearing file.
--
-- This script is STATIC. It is NEVER built by string-interpolating untrusted
-- values. Every untrusted value (from-account, recipients, subject, body)
-- arrives at runtime as an element of `argv` and is bound to an AppleScript
-- variable. To the AppleScript runtime those values are DATA, not source — a
-- value containing quotes, `& (do shell script "...")`, `send`, `delete`,
-- backslashes, etc. is treated as literal text and CANNOT execute. This is the
-- AppleScript-layer analog of a parameterized SQL query.
--
-- SENDER ACCOUNT (FIX 1 — the live bug):
--   `make new outgoing message` WITHOUT a sender lets Mail assign the draft to
--   the DEFAULT account (iCloud on the live Mac) and saves it into THAT account's
--   Drafts — even though the intended sender is a different (Google/Workspace)
--   account. So this script REQUIRES a from-account address (argv item 1) and:
--     1. enumerates Mail's configured accounts,
--     2. finds the account whose `email addresses` contains a case-insensitive
--        match for the from-account address,
--     3. builds that account's proper sender string ("Full Name <email>" when a
--        full name exists, else the bare matched address — both forms Mail's
--        "From:" dropdown accepts), and
--     4. sets the outgoing message's `sender` to it.
--   If NO configured account matches the from-account, the script FAILS CLOSED
--   (errors out, no draft) rather than silently falling back to the default
--   account — that silent fallback IS the bug. Setting `sender` makes Mail
--   associate the draft with the matching account and save it into THAT account's
--   Drafts, so the draft both lands in the right Drafts and sends from the right
--   address.
--
-- BODY-CLEAN VERIFICATION (FIX 2):
--   The draft body is EXACTLY the human's body — no marker line is appended. The
--   companion draft_exists.applescript verifies the save landed by locating, in
--   the SENDER account's Drafts mailbox, a recent draft matching subject + the
--   to-recipient + recency (not a body marker). So nothing visible to the human
--   is added to the message.
--
-- argv contract (positional, all strings):
--   item 1            : from-account sender address (e.g. pm@falkecorp.com)
--   item 2            : subject
--   item 3            : body
--   item 4            : count of "to" recipients (integer as text)
--   item 5 .. (4+N)   : the N "to" recipient addresses
--   item (5+N) ..     : the CC recipient addresses (zero or more)
--
-- The ONLY Mail verb this script emits is `save`. There is no `send`, no
-- `delete`, no `move`, no `set` on any other message. (COND-2 at the script
-- layer.) The one `set` is on the new message's own `sender` property.

on run argv
	set theFromAccount to item 1 of argv
	set theSubject to item 2 of argv
	set theBody to item 3 of argv
	set toCount to (item 4 of argv) as integer

	-- Collect the "to" recipients: items 5 .. (4 + toCount)
	set toAddresses to {}
	if toCount > 0 then
		repeat with i from 5 to (4 + toCount)
			set end of toAddresses to (item i of argv)
		end repeat
	end if

	-- Anything after the "to" block is a CC address.
	set ccAddresses to {}
	set firstCcIndex to (5 + toCount)
	if firstCcIndex ≤ (count of argv) then
		repeat with i from firstCcIndex to (count of argv)
			set end of ccAddresses to (item i of argv)
		end repeat
	end if

	tell application "Mail"
		-- Resolve the from-account to a configured account's proper sender string.
		-- FAIL CLOSED if no configured account matches (no silent default fallback).
		set theSender to my resolveSender(theFromAccount)
		if theSender is missing value then
			error "from-account not a configured Mail account: " & theFromAccount
		end if

		-- Create the message FROM the resolved account. Setting `sender` at
		-- creation makes Mail associate the draft with that account and save it
		-- into THAT account's Drafts.
		set newMessage to make new outgoing message with properties {sender:theSender, subject:theSubject, content:theBody, visible:false}
		tell newMessage
			repeat with addr in toAddresses
				make new to recipient at end of to recipients with properties {address:(addr as text)}
			end repeat
			repeat with addr in ccAddresses
				make new cc recipient at end of cc recipients with properties {address:(addr as text)}
			end repeat
		end tell
		-- The one and only write verb. Deposits the message into the sender
		-- account's Drafts.
		save newMessage
		-- Return the OUTGOING-message id for logging only. It does NOT survive the
		-- save into Drafts (Mail re-ids the saved draft), so verification keys off
		-- subject+recipient+recency in the sender account's Drafts, not this id.
		set outgoingId to (id of newMessage) as text
	end tell

	return outgoingId
end run

-- Resolve a from-account ADDRESS to the proper Mail "From:" sender STRING for the
-- account that owns it. Returns `missing value` if no configured account has that
-- address (case-insensitive, via `ignoring case`). Builds "Full Name <email>"
-- when the account has a full name, else the bare matched address — both forms
-- Mail accepts as `sender`.
on resolveSender(fromAddress)
	set wanted to (fromAddress as text)
	tell application "Mail"
		repeat with acct in accounts
			set acctAddrs to {}
			try
				set acctAddrs to (email addresses of acct)
			end try
			repeat with a in acctAddrs
				set thisAddr to (a as text)
				set isMatch to false
				ignoring case
					if thisAddr is wanted then set isMatch to true
				end ignoring
				if isMatch then
					-- Matched this account. Build its sender string.
					set fullName to ""
					try
						set fullName to (full name of acct) as text
					end try
					if fullName is not "" then
						return fullName & " <" & thisAddr & ">"
					else
						return thisAddr
					end if
				end if
			end repeat
		end repeat
	end tell
	return missing value
end resolveSender
