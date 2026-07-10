# Per-person config the skill + MCP server read

Per-person config splits into **two groups by who collects it and when**:

**Group A — the three mail allow-lists: MCP server `env` defaults (set at
install).** They ship with safe Falke defaults in the plugin's `.mcp.json` and are
enforced **inside** the MCP server; you override only if a person differs. They are
**NOT** prompted by the skill and **NOT** secrets.

| What | Where it's read | Default / example | Notes |
|---|---|---|---|
| **Read account allow-list** | `APPLE_MAIL_READ_ALLOWED_ACCOUNTS` (MCP env) | `falkecorp.com,falkehoa.com` | Both Falke domains (R2). Matched on email **domain**, not display name. Empty/garbage ⇒ read nothing (fail closed). A personal account is skipped automatically. |
| **From-account allow-list (sender)** | `APPLE_MAIL_DRAFT_FROM_ACCOUNTS` (MCP env) | `falkecorp.com,falkehoa.com` | Bounds which account a draft can be composed **FROM** — the person's own Falke mailbox(es). `create_apple_mail_draft`'s required `from_account` must be on this list, or the draft is rejected. A nudge is always drafted from the person's own account so it lands in their Drafts and sends from their address. |
| **Recipient allow-list (drafts)** | `APPLE_MAIL_DRAFT_ALLOWED_DOMAINS` (MCP env) | `falkecorp.com` (add `falkehoa.com` if drafting for that domain) | Bounds who an injection could ever draft to. Keep conservative. |

**Group B — the two per-deployment values: collected by the skill on FIRST RUN,
not at install.** The skill (SKILL.md Step 0.5) asks for these the first time it
runs and persists them to a local config file. Nothing to paste at install time.

| What | Where it's persisted | Default / example | Notes |
|---|---|---|---|
| **Dropbox project folder** | `~/.falke-business-pulse/config.json` → `dropbox_project_folder` | `~/Library/CloudStorage/Dropbox/<Falke project folder>` | Collected on first run. Must be set **"Available offline"** so Cowork reads files on disk, not cloud placeholders. `[VERIFY]` exact path per Mac. |
| **Teams webhook URL — OPTIONAL** | `~/.falke-business-pulse/config.json` → `teams_webhook_url` (**SECRET**) | *(per channel — or skipped)* | Collected on first run; the person may **skip** it ("skip" / "not yet"). If absent, Step 6 is skipped and the rest of the pulse still delivers; add it later by saying "update my Teams webhook". **Never** commit, never write to the digest/chat, never put in the Dropbox folder, never echo to a log. Bound to one channel. Rotate if leaked (COND-3). |

### The local config file (`~/.falke-business-pulse/config.json`)

The skill persists Group B here — **on local disk, in the person's home directory
(FileVault-protected), NOT in git and NOT inside the Dropbox project folder.**
Shape:

```json
{ "dropbox_project_folder": "~/Library/CloudStorage/Dropbox/<Falke project>",
  "teams_webhook_url": "https://..." }
```

`teams_webhook_url` is omitted/`null` when Teams is skipped. The skill reads this
file at the top of every run; if it's absent or missing `dropbox_project_folder`
(or the user says "reconfigure my pulse" / "update my Teams webhook") it re-runs
the short first-run setup conversation, otherwise it proceeds with no prompting.

> **Why `~/.falke-business-pulse/`, not `${CLAUDE_PLUGIN_DATA}`:**
> `${CLAUDE_PLUGIN_DATA}` is the framework's persistent-state dir, but plain
> skill-side file read/write under it can trip Cowork's protected-directory
> permission prompt (live-grounded 2026-06-24, claude-code issue #41156 —
> re-verify). A plain `~/` path is the simplest reliable mechanism and is equally
> outside git + outside Dropbox + FileVault-protected.

Run-state (not config): `state/last-run.txt` in the project folder holds the last
successful run timestamp = the next run's `since_iso` cutoff. Absent ⇒ default to
24h ago.

> At install the employee only confirms the `.mcp.json` allow-list defaults (Group
> A — already safe) and grants one macOS permission. The Dropbox path and the
> optional Teams webhook (Group B) are collected by the skill itself on first run —
> nothing to paste at install.
