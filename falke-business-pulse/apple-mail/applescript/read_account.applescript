-- read_account.applescript
--
-- COND-7 (AppleScript string-injection prevention) + bounded-delta read + CAP.
--
-- This script is STATIC. The account name, cutoff timestamp, and message ceiling
-- arrive at runtime as elements of `argv` and are bound to variables — they are
-- DATA to the AppleScript runtime, never source. A value containing quotes, an
-- ampersand shell-call payload, a Mail verb word, backslashes, etc. is literal
-- text and cannot execute. (Same parameterized-query discipline as create_draft.)
--
-- It reads ONLY the INBOX of ONE named, allow-listed account (the Python layer
-- has already filtered to allow-listed accounts before invoking this — COND-8).
--
-- CAP (ADR docs/adr/0001 — fixes the ~90s timeout on years-large personal inboxes):
-- instead of `messages whose date received > cutoff` — which walks the ENTIRE
-- inbox (O(inbox)) and timed out at 90s — this examines the NEWEST min(total,
-- ceiling) messages by index and returns every in-window message among them.
--
-- R-SAFE (ordering-INDEPENDENT completeness — Floyd's COND-5 remediation):
--   * NO early stop. Every one of the examined messages is checked and every
--     in-window one (date received > cutoff) is collected, REGARDLESS of order — so
--     a single message moved/delivered out of order (recent index, old date) can no
--     longer truncate the collection (the previous early-stop silently dropped
--     in-window mail sitting behind such a message).
--   * The SATURATION/COMPLETENESS DECISION is NOT made here — this script returns
--     the in-window records plus the raw metadata Python needs (examined count,
--     whether the OLDEST-BY-INDEX examined message is still in window, total count)
--     and read_core (unit-tested Python) decides `saturated`.
--   * SPEED: the newest range's dates are bulk-fetched in ONE osascript round-trip
--     (`date received of messages lo thru hi`), not `ceiling` individual
--     `message idx` accesses — bounded to O(ceiling) work, no O(inbox) `whose` walk.
--     Full properties are fetched only for the (few) in-window messages.
--
-- READ-ONLY: the only Mail operations are property reads. There is NO save / send /
-- delete / move / set. (COND-2, read side.) No shell is invoked.
--
-- argv contract (positional, all strings):
--   item 1 : account name (exact, as returned by list_accounts.applescript)
--   item 2 : cutoff as "YYYY-MM-DD HH:MM:SS" (space separator, not T)
--   item 3 : ceiling — max messages to examine (integer as text)
--
-- Output framing:
--   * FIRST a META line: `examined US boundary_in_window(0|1) US total`, terminated
--     by the FIRST newline. Emitted BEFORE any record; message bodies (which may
--     contain newlines) all appear AFTER it, so a crafted body cannot spoof it.
--   * THEN the in-window message records: fields separated by US (0x1F), records by
--     GS (0x1D), field order: sender US subject US date US body. Each field has
--     US/GS/control bytes STRIPPED before framing, so a crafted body cannot inject a
--     separator. Python splits on the FIRST newline (META vs records), then GS/US.
-- A message whose body is empty/blank (cached / not-yet-downloaded) is emitted with
-- an EMPTY body field; Python detects that and skips+logs it (cached-body integrity).

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
	set ceilingText to item 3 of argv
	set cutoffDate to (my parseISO(cutoffText))
	set ceiling to (ceilingText as integer)

	set us to (ASCII character 31)
	set gs to (ASCII character 29)
	set outRecords to {}
	set examined to 0
	set boundaryInWindow to false
	set totalCount to 0

	tell application "Mail"
		-- Resolve the one named account. If it doesn't exist, error (Python treats
		-- a nonzero exit / error as fail-loud).
		set theAccount to (first account whose name is acctName)
		-- The account's inbox only (bounded scope) — not every mailbox.
		set theInbox to (my resolveInbox(theAccount))
		set totalCount to (count of messages of theInbox)

		if totalCount > 0 then
			-- Newest-first index direction (endpoint comparison; positional reads).
			set d1 to (date received of message 1 of theInbox)
			set dN to (date received of message totalCount of theInbox)
			set newestIsFirst to (not (d1 < dN))

			-- Examine the NEWEST min(totalCount, ceiling) messages, as an index range.
			set examineCount to totalCount
			if examineCount > ceiling then set examineCount to ceiling
			if newestIsFirst then
				set lo to 1
				set hi to examineCount
			else
				set lo to (totalCount - examineCount + 1)
				set hi to totalCount
			end if

			-- Bulk-fetch the dates for the range in ONE round-trip (fast; NOT the
			-- O(inbox) `whose` walk). item i of dList <-> inbox index (lo + i - 1).
			set dList to (date received of messages lo thru hi of theInbox)
			set nFetched to (count of dList)

			repeat with i from 1 to nFetched
				set examined to examined + 1
				set mDate to item i of dList
				if (mDate is missing value) or (mDate > cutoffDate) then
					-- IN-WINDOW (undated emitted defensively — never silently dropped;
					-- Python's cached-body integrity check handles blanks). Collect it
					-- regardless of order. Fetch full props for THIS message only (the
					-- in-window count is small for a delta), so we don't pull ~ceiling
					-- bodies each run.
					set idx to (lo + i - 1)
					set m to message idx of theInbox
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
				end if
				-- No else / no early-stop: every examined message is checked, so an
				-- interleaved out-of-window message cannot truncate the collection.
			end repeat

			-- BOUNDARY signal (Floyd's R-SAFE saturation rule): is the OLDEST-BY-INDEX
			-- examined message (the far end of the examined range) STILL in window? If
			-- so, the cutoff falls BEYOND what we examined, so in-window mail may sit
			-- among the unexamined messages (Python flags CAPPED when total > examined).
			-- Free — it's just the far end of the dList we already fetched. Undated =>
			-- treated as in-window (conservative: flag rather than risk a silent miss).
			if newestIsFirst then
				set boundaryDate to (item nFetched of dList)
			else
				set boundaryDate to (item 1 of dList)
			end if
			set boundaryInWindow to ((boundaryDate is missing value) or (boundaryDate > cutoffDate))
		end if
	end tell

	-- META: examined US boundary_in_window(0|1) US total. The completeness DECISION
	-- (saturated = total > examined AND boundary_in_window) is made in read_core
	-- (unit-tested Python), NOT here.
	set biwField to "0"
	if boundaryInWindow then set biwField to "1"
	set metaLine to (examined as text) & us & biwField & us & (totalCount as text)

	set AppleScript's text item delimiters to gs
	set outText to outRecords as text
	set AppleScript's text item delimiters to ""
	return metaLine & (ASCII character 10) & outText
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
