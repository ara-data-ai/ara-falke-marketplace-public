# Falke Business Pulse (Cowork plugin)

One plugin that bundles **the morning Chief-of-Staff routine**: the
`/Falke-business-pulse` skill + the host-native **`apple-mail` MCP server**
(read + draft + the one fixed Teams post — never send-mail), the self-healing
**pulse viewer** (`http://127.0.0.1:8787`, bookmarkable, Refresh button,
7:00 AM weekday auto-refresh), zero-touch bootstrap, test suites, and golden
classification fixtures. Distributed via the public marketplace
(`ara-falke-marketplace-public`), with the private marketplace as the source
repo.

> **Status: PRODUCTION — security-review approved (full), all conditions
> closed.** Every release runs `scripts/release.sh`, which enforces the test
> sweep, the version-bump rule, the internal-name scrub, and byte-parity
> between the private source and the public marketplace. Runtime map:
> `docs/RUNTIME-LAYOUT.md`. Design record: `docs/adr/`. Classification eval:
> `eval/README.md`.

## Layout (mirrors falke-bid-tools; verified against the INSTALL-DESIGN)

```
falke-business-pulse/
├── .claude-plugin/plugin.json    # manifest (name = falke-business-pulse, kebab-case)
├── .mcp.json                     # auto-registers the apple-mail MCP server (command/args/env)
├── hooks/hooks.json              # SessionStart -> scripts/bootstrap.sh
├── scripts/bootstrap.sh          # one-Python-dep persistent-venv bootstrap (trimmed bid-tools pattern)
├── skills/
│   └── falke-business-pulse/     # the /Falke-business-pulse skill (COPY of the phase2-build source)
│       ├── SKILL.md
│       └── reference/            # teams-card.md, categories.md, config.md
└── apple-mail/                   # the EXISTING independently reviewed MCP server, vendored UNCHANGED
    ├── server.py                 # two tools: read_apple_mail, create_apple_mail_draft (stdio)
    ├── read_core.py / draft_core.py / config.py
    ├── requirements.txt          # mcp>=1.2.0 (the only runtime dep)
    └── applescript/              # the four static .applescript files the core modules call
```

The MCP server is **vendored, not rebuilt** — copied unchanged from
`FALKE/01_Chief_of_Staff/phase2-build/apple-mail-draft-mcp/`. Its core modules
resolve the AppleScript files via `Path(__file__).parent / "applescript"`, so the
`applescript/` directory MUST stay a sibling of `read_core.py`/`draft_core.py` —
which the vendored layout preserves. The server's tests, dev README, and live-test
harness are intentionally NOT vendored (runtime needs only the source + scripts).

## Install (employee — two terminal lines)

> ### ⚠️ One-time prerequisite — Python ≥ 3.10 (do this FIRST)
>
> The bundled mail tool's runtime dependency (the `mcp` SDK) needs **Python
> ≥ 3.10**. **Stock macOS ships Python 3.9**, which is too old — so on a fresh Mac
> you must install a newer Python **before** the install below, or the mail tool
> won't set up.
>
> **Install it once:**
> ```
> brew install python@3.12
> ```
> (Any Python ≥ 3.10 works — 3.10/3.11/3.12/3.13. If you don't have Homebrew, get
> it from https://brew.sh first.)
>
> **This is the one thing that breaks the otherwise dead-simple install if it's
> missed.** You don't have to check your version by hand — the first-run bootstrap
> has a Python-≥3.10 guard that **fails loud** with this exact guidance
> (`ERROR: need Python >=3.10 for the mail tool … Install it (e.g. 'brew install
> python@3.12') and reopen Cowork.`) if no suitable Python is found. Install
> Python, reopen Cowork, and the setup continues.

Then the two terminal lines:

```
/plugin marketplace add https://<private-falke-plugins-repo>.git
/plugin install falke-business-pulse@ara-falke
```

Approve the "Will install" summary (it lists the `apple-mail` MCP server + the
skill — expected), then `/reload-plugins`. First session shows
"Setting up dependencies…" for ~30s while the bootstrap hook prepares the mail
tool. Then: the skill's **first-run setup** asks for the per-person config (below)
and you grant the one-time macOS Automation permission. Full step-by-step is the
INSTALL-DESIGN + the polished employee guide.

## Per-person config

Two things vary per person — the **Dropbox project folder** and the (optional)
**Teams webhook URL**. The skill **collects these on first run** (SKILL.md
Step 0.5) and saves them to `~/.falke-business-pulse/config.json` in the person's
home directory (FileVault-protected, outside git and outside the Dropbox folder).
They are **not** pasted as env vars at install.

| What | Where | Default | Secret? |
|---|---|---|---|
| Read account allow-list | `APPLE_MAIL_READ_ALLOWED_ACCOUNTS` (`.mcp.json` env) | `falkecorp.com,falkehoa.com` | no |
| Recipient allow-list (drafts) | `APPLE_MAIL_DRAFT_ALLOWED_DOMAINS` (`.mcp.json` env) | `falkecorp.com` | no |
| From-account allow-list (sender) | `APPLE_MAIL_DRAFT_FROM_ACCOUNTS` (`.mcp.json` env) | `falkecorp.com,falkehoa.com` | no |
| Dropbox project folder | collected on first run → `~/.falke-business-pulse/config.json` | `~/Library/CloudStorage/Dropbox/<project>` (Available offline) | no |
| Teams webhook URL (**optional**) | collected on first run → `~/.falke-business-pulse/config.json` | *(per channel — skip to run without the Teams post)* | **YES — never commit** |

The three allow-lists ship with safe Falke defaults in `.mcp.json` and are
overridden only if a person's case differs. The Dropbox path and Teams webhook are
**not** env vars — the skill's first-run setup prompts for them and writes them to
`~/.falke-business-pulse/config.json` (re-runnable anytime via "reconfigure my
pulse" / "update my Teams webhook"). **Teams is optional:** skip the webhook and
the pulse runs normally without the Teams post.

The **Teams webhook URL is a secret** and is deliberately NOT in `.mcp.json` and
NOT in the repo — it lives only in the local `config.json` (outside git, outside
the Dropbox folder), never baked into the plugin (COND-3).

## Pulse viewer — bookmarked webpage with a Refresh button (`pulse-server/`)

A localhost-only web viewer so the pulse is the first thing you see when you
open Chrome each morning. Serves the **latest** pulse HTML at
**http://127.0.0.1:8787** with a toolbar showing
"Data last refreshed on hh:mm:ss dd/mm/yyyy" and a **Refresh** button that
runs the skill headlessly in Claude Code and reloads the page when done.

**Install: automatic.** The plugin's first-run bootstrap (SessionStart hook)
installs the viewer and the 7:00 AM weekday refresh on macOS — nothing to run
by hand. It also downloads a private standalone Python into the plugin data
dir if the laptop has none (no Homebrew, no admin password). Manual fallback
if ever needed:

```sh
cd <plugin dir>/pulse-server
./install.sh --with-morning-run
```

Then bookmark http://127.0.0.1:8787 — or set it as a Chrome startup page
(Chrome > Settings > On startup > Open a specific page).

**How it holds up:**

- The server is a launchd agent with `KeepAlive` — starts at login, restarts on
  crash, survives sleep/wake (localhost has no remote sockets to drop).
- Serving and refreshing **fail independently**: if a refresh fails, the page
  still shows the last good pulse with its timestamp, and the button reports
  the error ("open Claude Code and run the pulse manually"). Never a blank page.
- Bound to 127.0.0.1 only; `POST /refresh` rejects non-localhost browser
  origins; concurrent refreshes are refused (409).
- The optional 7:00 AM weekday run just POSTs to `/refresh` — one code path.

> **Accepted residual risk (security review):** an Origin-less local client (any
> process already running as the user) can still POST `/refresh` and trigger a
> paid agent run — a denial-of-wallet nuisance, not a data-exposure path; the
> 409 concurrency guard bounds it to one run at a time.

**Config** (optional key in `~/.falke-business-pulse/config.json`). The refresh
command itself is fixed in the server — not configurable, so a config-file
write can never become arbitrary command execution (security review):

| Key | Default | What it is |
|---|---|---|
| `pulse_html_dir` | `~/Claude/Projects/Falke-Business-Pulse` | where the skill saves `pulse-*.html` |

**Refresh-path prerequisites** (the viewer works without them; the button needs
them): Claude Code CLI installed and signed in, and Mail automation permission
granted. The installer registers this plugin at CLI user scope automatically
(headless `claude -p` runs use the CLI plugin registry, not the Desktop
Directory install) — soft-fail, so a missing CLI or network never blocks the
viewer itself. Each Refresh click is a real agent run (a few
minutes) — it is a morning brief, not a live dashboard.
