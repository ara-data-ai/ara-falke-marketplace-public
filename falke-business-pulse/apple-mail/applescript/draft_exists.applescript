-- draft_exists.applescript
--
-- COND-5 (fail-loud draft-exists assertion) — load-bearing file.
--
-- Static script. Verifies, after a `save`, that the just-created draft actually
-- landed in the SENDER account's Drafts mailbox. Prints "EXISTS" or "MISSING".
--
-- BODY-CLEAN VERIFICATION (FIX 2 — no marker in the draft body):
--   The previous version appended a "mcp-draft-verify:<nonce>" marker line to the
--   draft body and searched for it. That is unacceptable for a client-facing
--   draft — the human would send a body with our marker in it. Now that the draft
--   reliably routes to the sender account's OWN Drafts (FIX 1), we instead match
--   on three independent, body-clean dimensions inside THAT account's Drafts:
--       (1) exact subject match, AND
--       (2) the intended to-recipient address is among the message's recipients,
--           AND
--       (3) the message was created within the last `windowSeconds` seconds
--           (recency), measured as (current date) - (date of the message).
--   Subject + recipient + a tight recency window in the correct account's Drafts
--   makes a false EXISTS negligible: a pre-existing draft would have to share the
--   exact subject AND the exact recipient AND have been composed in the last ~2
--   minutes. The draft body is left exactly as the human will send it.
--
-- WHY THE SENDER ACCOUNT, NOT "all Drafts":
--   FIX 1 sets the outgoing message's `sender`, so Mail saves the draft into the
--   matching account's per-account Drafts. We look there (and, defensively, also
--   the app-level `drafts mailbox` for the single-/default-account case). This is
--   tighter and faster than scanning every account's Drafts.
--
-- READ-ONLY: emits NO write verb — no save, no send, no message removal, no move.
--
-- argv contract (positional, all strings):
--   item 1 : from-account sender address (the account whose Drafts to search)
--   item 2 : subject (exact match)
--   item 3 : the intended to-recipient address (must be a recipient of the match)
--   item 4 : recency window in seconds (integer as text); a match must be newer

-- How many of the newest messages per Drafts mailbox to inspect. A just-created
-- draft is among the newest; this bounds the scan so it is fast on large folders.
property kScanLimit : 25

on run argv
	set theFromAccount to item 1 of argv
	set theSubject to item 2 of argv
	set theRecipient to item 3 of argv
	set windowSeconds to (item 4 of argv) as integer

	set found to false

	tell application "Mail"
		-- (a) The sender account's OWN Drafts mailbox(es) — the FIX-1 save target.
		try
			repeat with acct in accounts
				if my accountHasAddress(acct, theFromAccount) then
					try
						repeat with mb in (mailboxes of acct)
							set mbName to ""
							try
								set mbName to (name of mb) as text
							end try
							if my nameLooksLikeDrafts(mbName) then
								if my matchInMailbox(mb, theSubject, theRecipient, windowSeconds, kScanLimit) then
									set found to true
									exit repeat
								end if
							end if
						end repeat
					end try
				end if
				if found then exit repeat
			end repeat
		end try

		-- (b) Defensive fallback: the app-level Drafts mailbox (single-/default-
		--     account case where per-account enumeration may not expose Drafts).
		if not found then
			try
				if my matchInMailbox(drafts mailbox, theSubject, theRecipient, windowSeconds, kScanLimit) then
					set found to true
				end if
			end try
		end if
	end tell

	if found then
		return "EXISTS"
	else
		return "MISSING"
	end if
end run

-- True iff any of the newest `limit` messages of `mb` matches subject + recipient
-- + recency (created within `windowSeconds` of now).
on matchInMailbox(mb, theSubject, theRecipient, windowSeconds, limit)
	tell application "Mail"
		set msgs to messages of mb
		set n to (count of msgs)
		if n = 0 then return false
		set upper to n
		if upper > limit then set upper to limit
		set nowDate to (current date)
		repeat with i from 1 to upper
			set theMsg to (item i of msgs)

			-- (1) exact subject match.
			set msgSubject to ""
			try
				set msgSubject to (subject of theMsg) as text
			end try
			if msgSubject is theSubject then

				-- (3) recency: created within windowSeconds of now. Use the
				-- message's date; if unreadable, do not match on recency.
				set isRecent to false
				try
					set msgDate to (date received of theMsg)
				on error
					set msgDate to missing value
				end try
				if msgDate is missing value then
					try
						set msgDate to (date sent of theMsg)
					on error
						set msgDate to missing value
					end try
				end if
				if msgDate is not missing value then
					if (nowDate - msgDate) ≤ windowSeconds and (nowDate - msgDate) ≥ -60 then
						set isRecent to true
					end if
				end if

				if isRecent then
					-- (2) the intended recipient is among this draft's recipients.
					if my recipientPresent(theMsg, theRecipient) then return true
				end if
			end if
		end repeat
	end tell
	return false
end matchInMailbox

-- True iff `wantedAddr` is among the to/cc recipients of `theMsg` (case-insensitive).
on recipientPresent(theMsg, wantedAddr)
	tell application "Mail"
		set rcpts to {}
		try
			set rcpts to (address of (to recipients of theMsg))
		end try
		try
			set rcpts to rcpts & (address of (cc recipients of theMsg))
		end try
		repeat with r in rcpts
			set thisAddr to (r as text)
			ignoring case
				if thisAddr is wantedAddr then return true
			end ignoring
		end repeat
	end tell
	return false
end recipientPresent

-- True iff `acct` is configured with `wantedAddr` among its email addresses.
on accountHasAddress(acct, wantedAddr)
	tell application "Mail"
		set acctAddrs to {}
		try
			set acctAddrs to (email addresses of acct)
		end try
		repeat with a in acctAddrs
			set thisAddr to (a as text)
			ignoring case
				if thisAddr is wantedAddr then return true
			end ignoring
		end repeat
	end tell
	return false
end accountHasAddress

-- True iff a mailbox name looks like a Drafts mailbox (case-insensitive substring
-- "draft"). Covers "Drafts", "[Gmail]/Drafts", and localized variants.
on nameLooksLikeDrafts(mbName)
	if mbName is "" then return false
	ignoring case
		if mbName contains "draft" then return true
	end ignoring
	return false
end nameLooksLikeDrafts
