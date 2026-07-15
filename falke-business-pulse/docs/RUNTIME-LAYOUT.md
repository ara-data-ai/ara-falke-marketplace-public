# Runtime layout — every path, port, env var, and constant this product touches

The single map. If something is weird on a machine, start here. Anything the
product reads or writes that is NOT on this page is a bug — add it or remove it.

## Filesystem

| Location | What lives there | Written by | Lifecycle |
|---|---|---|---|
| `~/.falke-business-pulse/config.json` | Per-person config: `dropbox_project_folder`, optional `teams_webhook_url` (**SECRET**), optional `falke_logo_url`, optional `pulse_html_dir` override | Skill first-run setup (Step 0.5) | Survives everything; user-owned |
| `~/.falke-business-pulse/last-run.txt` | The scan cutoff — timestamp of the last successful run. **CANONICAL, the only copy** (a relative `state/` path is a defect) | Skill Step 7 | Survives everything |
| `~/.falke-business-pulse/last-scan-status.json` | Machine-written scan-integrity marker (ok/partial + affected accounts + run token). Drives the viewer's structural banner | `read_core._write_scan_status` — never the model | Overwritten every read |
| `~/Claude/Projects/Falke-Business-Pulse/pulse-*.html` | The saved daily pulses (each stamped `<!-- falke-pulse-run: token -->`) | Skill Step 3 | User-owned; viewer serves the newest |
| `~/Library/Application Support/falke-pulse-server/server.py` | The DURABLE viewer copy launchd runs (never run from a plugin cache path) | `pulse-server/install.sh` | Refreshed each install; removed by `--uninstall` |
| `~/Library/Application Support/falke-pulse-server/.installed-sig` | Cross-scope install stamp (prevents per-session reinstall churn) | `scripts/bootstrap.sh` | Removed by `--uninstall` |
| `~/Library/LaunchAgents/com.falke.pulse-server.plist` | Viewer launchd agent (KeepAlive) | `install.sh` | Removed by `--uninstall` |
| `~/Library/LaunchAgents/com.falke.pulse-morning.plist` | 7:00 AM weekday refresh (curls `/refresh`) | `install.sh --with-morning-run` | Removed by `--uninstall` |
| `~/Library/Logs/falke-pulse-server/{out,err}.log` | Viewer stdout/stderr (launchd) | launchd | Grows; safe to delete |
| `~/Library/Logs/falke-pulse-server/refresh.log` | Full headless-refresh CLI output (incl. detail withheld from `/status`) | viewer `_run_refresh` | Grows; safe to delete |
| `~/Library/Logs/apple-mail-draft-mcp/run-log.jsonl` | Draft + Teams-post audit trail (JSONL) | `draft_core` / `teams_core` | Grows; audit — keep |
| `~/Library/Logs/apple-mail-draft-mcp/read-log.jsonl` | Read audit trail: accounts read vs skipped, caps, degradations (COND-8/COND-5 evidence) | `read_core` | Grows; audit — keep |
| `${CLAUDE_PLUGIN_DATA}/venv/` | The MCP server's Python venv (per plugin scope) | `scripts/bootstrap.sh` | Rebuilt on requirements change |
| `${CLAUDE_PLUGIN_DATA}/python/` | Standalone CPython (only if the Mac had no ≥3.10; SHA-256-verified download) | `scripts/bootstrap.sh` | One-time |

## Network

| Endpoint | Direction | Purpose |
|---|---|---|
| `127.0.0.1:8787` | serve (localhost ONLY) | The pulse viewer. `GET /` page, `GET /status` refresh state, `POST /refresh` headless run. Host-allowlisted, CSP'd, nonce'd |
| `teams_webhook_url` (from config) | outbound POST | The ONE automated send — fixed Adaptive Card via `post_teams_digest`. URL is a secret, never logged |
| `github.com/astral-sh/python-build-standalone` | outbound GET (rare) | Pinned, checksum-verified Python download when the Mac has none |
| `github.com/ara-data-ai/ara-falke-marketplace-public.git` | outbound git | CLI-scope plugin self-registration/update (installer) |

## Environment variables (set in `.mcp.json`, read by `apple-mail/config.py`)

| Var | Default | Meaning |
|---|---|---|
| `APPLE_MAIL_READ_ALLOWED_ACCOUNTS` | `falkecorp.com,falkehoa.com` | COND-8 read allow-list (account domains). Explicitly empty ⇒ read NOTHING |
| `APPLE_MAIL_DRAFT_ALLOWED_DOMAINS` | `falkecorp.com` | COND-6 recipient allow-list |
| `APPLE_MAIL_DRAFT_FROM_ACCOUNTS` | `falkecorp.com,falkehoa.com` | COND-6 sender allow-list |
| `APPLE_MAIL_DRAFT_RUN_LOG` / `APPLE_MAIL_READ_RUN_LOG` | the Logs paths above | Audit-log overrides (tests only) |

## Load-bearing constants (in code, deliberately NOT configurable)

| Constant | Value | Where | Why fixed |
|---|---|---|---|
| `READ_MAX_MESSAGES_PER_ACCOUNT` | 500 | `config.py` | The timeout cap (ADR 0001); capped accounts surface as `partial`, never silently |
| Read driver timeout | 90 s/account | `read_core.ReadMailDriver` | Fail-loud bound |
| `REFRESH_ARGS` | fixed argv | viewer `server.py` | A config write must never become command exec (security review F3) |
| Scan-marker path | fixed | `config.read_scan_status_path` | Writer and banner-reader must agree; env-pointable would be a clobber footgun |
| Viewer port | 8787 | viewer `server.py` | 8788 is the ARA mirror; fixed so bookmarks/plists never drift |
| Card template | code | `teams_core.build_card` | The model passes data fields only; the card cannot be restructured |

## What is deliberately NOT anywhere

No `Mail.Send` (drafts only). No mail read outside the two allow-listed domains.
No `Action.*` elements in the Teams card. No secrets in git, env vars, or logs
— the webhook lives only in `config.json`. No network exposure beyond localhost.
