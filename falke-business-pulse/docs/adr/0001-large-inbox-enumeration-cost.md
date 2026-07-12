# ADR 0001 — Large personal-inbox enumeration cost (~90s AppleScript scan)

- **Status:** ACCEPTED — Option A (message-count cap) implemented in **0.3.3**.
- **Date:** 2026-07-11
- **Component:** `falke-business-pulse` — `apple-mail/` read path
- **Related:** WS1 (per-account stall degrade)

> **Update (0.3.3):** Option A is now shipped. `read_account.applescript` examines
> the NEWEST `min(total, config.READ_MAX_MESSAGES_PER_ACCOUNT)` (500) messages by
> index (dates bulk-fetched in one round-trip) and returns EVERY in-window one —
> ordering-independent, no early stop (R-SAFE, closing the interleaved-message silent
> drop). `read_core` makes the completeness DECISION in unit-tested Python via a
> BOUNDARY rule (`_is_saturated` = `total > examined` AND the oldest-by-index
> examined message is still in-window) and surfaces a saturated account as CAPPED
> (`accounts_capped`,
> `status: "partial"`, banner) — never a silent truncation (COND-5). The ~90s
> enumeration timeout is eliminated in the logic; **the raw osascript speed and
> Mail's index-ordering are confirmed only by a live run** (mocks can't measure them
> — see `scripts/smoke_read_shape.py --live`). Option D (provider API) remains the
> strategic follow-up behind a Floyd threat-model review.

## Context — the root cost

The morning scan reads each allow-listed account's inbox through
`apple-mail/applescript/read_account.applescript`, run via `osascript` from
`ReadMailDriver.read_inbox`. The script does a bounded-delta query:

> read the account's **INBOX**, messages `whose date received > cutoff`.

For the two ARA business accounts this is cheap. For the **personal iCloud
account** it is not: Apple Mail evaluates `whose date received > cutoff` by
**walking the whole inbox** (there is no script-reachable server-side date index
via the Mail scripting bridge), so a large iCloud inbox (tens of thousands of
messages) takes **~90 seconds** — right at the `ReadMailDriver` 90s timeout. This
is the original defect that motivated the graceful
degradation, and WS1 (per-account degrade):

- Empirically, the full 5-domain allow-list timed out on iCloud at 90s; scoped to
  the two ARA domains it completed in ~67s.
- The cost is intrinsic to **enumerate-then-filter over AppleScript**, not to the
  amount of *new* mail. Even a delta of zero still pays the full enumeration.

So the personal path is both the **highest-latency** and the
**highest-timeout-risk** part of every run.

## Mitigation already in place (resilience only)

- **WS1 — degrade a per-account stall.** If iCloud stalls/times out, that account
  is skipped and the scan is marked `partial` (surfaced structurally on the 8787
  viewer) instead of killing the whole scan. This is **resilience, not a fix** —
  iCloud still contributes nothing on a slow morning.

> A first-run 3-day personal look-back was considered (WS2) but **dropped**: the
> first-run default is already 24h, so a 3-day window would read *more*, not less,
> and would **not** reduce the enumeration cost — which is inbox-size-driven, not
> window-driven. Personal accounts now use the same 24h first-run default as
> business accounts.

WS1 does not address the underlying ~90s enumeration. This ADR is about that.

## Options considered

### A. `maxMessages` / message-count cap in the AppleScript
Fetch only the newest N messages of the inbox (e.g. `messages 1 thru N`) instead
of evaluating `whose date received > cutoff` over everything, then date-filter in
Python.
- **Pro:** smallest change; stays in the current AppleScript/osascript design; no
  new auth or dependencies; big latency win (walk N, not the whole inbox).
- **Con:** a cap can **miss** messages on a busy day (if > N arrived since the
  cutoff) — a correctness/completeness risk on the personal path; needs a sensible
  N and a "hit the cap → flag partial" signal (reuses the WS1 partial machinery).
- **Risk:** low-moderate (bounded by the partial-flag).

### B. Index-friendlier AppleScript query
Try to get Mail to use an internal index — e.g. iterate `messages` in stored order
and stop early once older than cutoff (messages are roughly date-ordered), rather
than a set-wide `whose`.
- **Pro:** no new dependencies; potentially avoids the full walk.
- **Con:** relies on undocumented ordering/'`whose`'-planner behavior that varies
  by Mail version and account type; brittle; may not actually skip the walk.
- **Risk:** moderate (fragile across macOS/Mail updates).

### C. Incremental sync / local state
Persist the last-seen message id/date per account and ask only for messages after
it, and/or cache prior results.
- **Pro:** amortizes cost across runs.
- **Con:** the *query mechanism* is still the slow `whose` walk unless combined
  with A or D; adds state to maintain and invalidate; more moving parts.
- **Risk:** moderate; complexity for partial benefit alone.

### D. Provider API instead of AppleScript (Gmail API / IMAP / JMAP)
Read personal mail via the provider's own API — Gmail API for the Gmail account,
IMAP (`SEARCH SINCE`) or JMAP for iCloud — which have **server-side date indexes**
and return a delta in well under a second.
- **Pro:** removes the root cost entirely; fast, reliable deltas; scales to large
  inboxes; no Mail.app enumeration.
- **Con:** biggest change — new auth (OAuth for Gmail; app-specific password /
  token for iCloud IMAP), new dependency and **new attack surface / secret
  storage**, and it **diverges from the COND-8 "read only what Mail already has
  configured" privacy model** (a Floyd threat-model review would be required).
  IMAP for iCloud needs an app-specific password the user must mint.
- **Risk:** high effort + new security surface; highest payoff.

## Recommendation

**Adopt Option A (message-count cap) as the near-term fix**, wired to the existing
partial-scan machinery: cap the personal-account fetch to the newest N messages,
date-filter in Python, and **flag `status:"partial"` if the cap is hit while older
un-fetched messages could still be within the cutoff** (so a busy day is never
silently truncated — same COND-5 discipline as WS1). This gets the big latency win
with a small, in-design change and no new auth/attack surface. Keep WS2 (bounds the
first run) and WS1 (degrade on stall) as the safety net.

**Revisit Option D (provider API) as the strategic direction** if the personal
path becomes central and the cap's completeness tradeoff proves limiting — but only
behind a Floyd threat-model review, because it adds authentication, secret storage,
and a departure from the "Mail-configured accounts only" COND-8 model.

Options B and C are **not recommended** on their own: B is too fragile across Mail
versions; C adds state without removing the root `whose`-walk cost.

## Consequences

- If A is adopted: `read_account.applescript` and `ReadMailDriver.read_inbox` gain
  a message-count bound (and a "cap hit" signal); the read path stays osascript-
  based and COND-8-aligned; a new completeness tradeoff (cap vs. busy-day
  truncation) is made explicit and surfaced as partial.
- If nothing is adopted: the ~90s personal enumeration remains; WS1 keeps it from
  killing the scan, but the personal contribution is unreliable on slow mornings.

## Not decided here

The value of N (message cap), and whether iCloud vs. Gmail warrant different
strategies (Gmail API is cleaner than iCloud IMAP), are left for the
implementation ADR if Option A is approved.
