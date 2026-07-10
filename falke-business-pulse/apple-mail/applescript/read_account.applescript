-- read_account.applescript
--
-- COND-7 (AppleScript string-injection prevention) + bounded-delta read.
--
-- This script is STATIC. The account name and the cutoff timestamp arrive at
-- runtime as elements of `argv` and are bound to variables — they are DATA to the
-- AppleScript runtime, never source. A value containing quotes, an ampersand
-- shell-call payload, a Mail verb word, backslashes, etc. is literal text and
-- cannot execute. (Same parameterized-query discipline as create_draft.applescript.)
--
-- It reads ONLY the INBOX of ONE named, allow-listed account (the Python layer
-- has already filtered to allow-listed accounts before invoking this — COND-8),
-- and ONLY messages whose `date received` is AFTER the cutoff (bounded delta).
-- It does NOT enumerate every mailbox of every account — that is the ~500x-slower
-- pattern that can stall Mail; this is the bounded-delta design the memo requires.
--
-- READ-ONLY: the only Mail operations are property reads + a `whose` filter.
-- There is NO save / send / message deletion / move / set. (COND-2, read side.)
-- No shell is invoked (no `do shell script`).
--
-- argv contract (positional, all strings):
--   item 1 : account name (exact, as returned by list_accounts.applescript)
--   item 2 : cutoff as "YYYY-MM-DD HH:MM:SS" (space separator, not T)
--
-- Output framing (control-character framed so untrusted bodies — which may carry
-- tabs, newlines, quotes, injection text — can NEVER break the record structure):
--   * fields within a record are separated by US  (ASCII 0x1F, "unit separator")
--   * records are separated by         GS  (ASCII 0x1D, "group separator")
--   * field order: sender US subject US date US body
-- Each field value has any US/GS/control bytes STRIPPED before framing, so a
-- crafted body cannot inject a separator. The Python side splits on GS then US.
-- A message whose body is empty/blank (cached / not-yet-downloaded) is emitted
-- with an EMPTY body field; Python detects that and skips+logs it (cached-body
-- integrity).

on stripCtrl(theText)
	-- Remove the framing/control bytes from a field so content can't break framing.
	if theText is missing value then return ""
	set theText to theText as text
	set us to (ASCII character 31)
	set gs to (ASCII character 29)
	set AppleScript's text item delimiters to {us, gs}
	set parts to text items of theText
	set AppleScript's text item delimiters to ""
	set cleaned to parts as text
	return cleaned
end stripCtrl

on run argv
	set acctName to item 1 of argv
	set cutoffText to item 2 of argv
	set cutoffDate to (my parseISO(cutoffText))

	set us to (ASCII character 31)
	set gs to (ASCII character 29)
	set outRecords to {}

	tell application "Mail"
		-- Resolve the one named account. If it doesn't exist, error (Python treats
		-- a nonzero exit / error as fail-loud).
		set theAccount to (first account whose name is acctName)
		-- The account's inbox only (bounded scope) — not every mailbox.
		-- Provider-agnostic resolution: IMAP/Gmail name the inbox "INBOX";
		-- Microsoft 365 / Exchange do NOT (e.g. "Inbox"). Try the IMAP name
		-- first, then fall back to a case-insensitive match against THIS
		-- account's own mailboxes. Fails loud (errors) if no inbox resolves.
		set theInbox to (my resolveInbox(theAccount))
		-- Bounded DELTA: only messages newer than the cutoff.
		set newMsgs to (messages of theInbox whose date received > cutoffDate)

		repeat with m in newMsgs
			set theSender to ""
			set theSubject to ""
			set theDate to ""
			set theBody to ""
			try
				set theSender to (sender of m) as text
			end try
			try
				set theSubject to (subject of m) as text
			end try
			try
				set theDate to ((date received of m) as text)
			end try
			try
				set theBody to (content of m) as text
			end try

			set rec to (my stripCtrl(theSender)) & us & (my stripCtrl(theSubject)) & us & (my stripCtrl(theDate)) & us & (my stripCtrl(theBody))
			set end of outRecords to rec
		end repeat
	end tell

	set AppleScript's text item delimiters to gs
	set outText to outRecords as text
	set AppleScript's text item delimiters to ""
	return outText
end run

-- Resolve the inbox of ONE account, provider-agnostically and within that
-- account's own mailboxes only (preserves COND-8 — never the app-level unified
-- `inbox`, which would span every account). IMAP/Gmail expose "INBOX"; Exchange
-- exposes a differently-cased/named inbox. Try the literal IMAP name first
-- (fast, common path), then fall back to a case-insensitive name match against
-- this account's mailboxes. Fail LOUD (error) if no inbox can be resolved.
on resolveInbox(theAccount)
	tell application "Mail"
		try
			return (mailbox "INBOX" of theAccount)
		end try
		repeat with mb in (mailboxes of theAccount)
			set mbName to ""
			try
				set mbName to (name of mb) as text
			end try
			if my nameLooksLikeInbox(mbName) then return mb
		end repeat
	end tell
	error "no inbox mailbox could be resolved for the account"
end resolveInbox

-- True iff a mailbox name is the account's inbox (case-insensitive exact match
-- on "inbox"). Mirrors the draft-side nameLooksLikeDrafts discipline. Exact
-- (not substring) so it can't match e.g. an "Inbox Archive" subfolder.
on nameLooksLikeInbox(mbName)
	if mbName is "" then return false
	ignoring case
		if mbName is "inbox" then return true
	end ignoring
	return false
end nameLooksLikeInbox

-- Parse "YYYY-MM-DD HH:MM:SS" into an AppleScript date from integer components.
-- The cutoff is our own code's value, but it is still bound as data and the date
-- is built from integers — never string-templated into source.
on parseISO(s)
	set s to s as text
	set theYear to (text 1 thru 4 of s) as integer
	set theMonth to (text 6 thru 7 of s) as integer
	set theDay to (text 9 thru 10 of s) as integer
	set theHour to (text 12 thru 13 of s) as integer
	set theMin to (text 15 thru 16 of s) as integer
	set theSec to (text 18 thru 19 of s) as integer
	set d to current date
	set year of d to theYear
	set month of d to theMonth
	set day of d to theDay
	set hours of d to theHour
	set minutes of d to theMin
	set seconds of d to theSec
	return d
end parseISO
