#!/bin/zsh
# Install the Falke Pulse local viewer: launchd-managed localhost server
# (http://127.0.0.1:8787) + optional 7:00 AM weekday refresh.
# Run once per laptop:  ./install.sh [--with-morning-run]
# Remove everything it installed:  ./install.sh --uninstall
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"

# --- Uninstall: remove exactly what install created; keep the user's data ----
if [[ "${1:-}" == "--uninstall" ]]; then
  for name in com.falke.pulse-server com.falke.pulse-morning; do
    launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
    rm -f "$AGENTS/$name.plist"
    echo "[pulse-server] removed: $name"
  done
  rm -rf "$HOME/Library/Application Support/falke-pulse-server"
  echo "[pulse-server] removed: durable server copy + install stamp"
  echo "[pulse-server] KEPT (yours, delete manually if wanted):"
  echo "  ~/.falke-business-pulse/            (config, run state, scan marker)"
  echo "  ~/Claude/Projects/Falke-Business-Pulse/  (your saved pulses)"
  echo "  ~/Library/Logs/falke-pulse-server/  and  ~/Library/Logs/apple-mail-draft-mcp/  (logs)"
  echo "[pulse-server] Uninstall complete. The plugin itself: 'claude plugin uninstall falke-business-pulse@ara-falke' + remove from the Cowork Directory."
  exit 0
fi
# Caller (bootstrap.sh) passes the resolved interpreter via $PYTHON; fall back
# to whatever python3 is on PATH for manual installs.
PYTHON="${PYTHON:-$(command -v python3 || true)}"

if [[ -z "$PYTHON" ]]; then
  echo "[pulse-server] ERROR: python3 not found on PATH." >&2
  exit 1
fi

mkdir -p "$AGENTS" "$HOME/Library/Logs/falke-pulse-server"

# Copy server.py to a DURABLE location before pointing launchd at it. $HERE
# can be an ephemeral plugin-cache path (e.g. /var/folders/.../T/...) that
# macOS wipes — a plist pointing there leaves KeepAlive respawning a missing
# file and the viewer dead until the next plugin update. The stable copy is
# refreshed on every install run.
STABLE_DIR="$HOME/Library/Application Support/falke-pulse-server"
mkdir -p "$STABLE_DIR"
cp "$HERE/server.py" "$STABLE_DIR/server.py"

install_plist() {
  local name="$1"
  sed -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__SERVER__|$STABLE_DIR/server.py|g" \
      -e "s|__HOME__|$HOME|g" \
      "$HERE/launchd/$name.plist" > "$AGENTS/$name.plist"
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$name.plist"
  echo "[pulse-server] Installed + started: $name"
}

install_plist "com.falke.pulse-server"

if [[ "${1:-}" == "--with-morning-run" ]]; then
  install_plist "com.falke.pulse-morning"
fi

# ---------------------------------------------------------------------------
# CLI-scope plugin registration (zero-touch Refresh). Headless `claude -p`
# runs — and terminal/worktree sessions — use the user-level CLI plugin
# registry, NOT the Desktop Directory install. Register the plugin there too
# so the Refresh button's headless run can load it. SOFT-FAIL discipline: the
# viewer must still install even if the CLI is missing or the network is down
# (Refresh reports its own generic error; serving is never blocked).
MARKETPLACE_URL="https://github.com/ara-data-ai/ara-falke-marketplace-public.git"
MARKETPLACE_NAME="ara-falke"
PLUGIN_NAME="falke-business-pulse"

CLAUDE_BIN=""
for cand in "$(command -v claude || true)" "$HOME/.local/bin/claude" \
            "$HOME/.claude/local/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
  if [[ -n "$cand" && -x "$cand" ]]; then CLAUDE_BIN="$cand"; break; fi
done

if [[ -z "$CLAUDE_BIN" ]]; then
  echo "[pulse-server] WARNING: claude CLI not found — skipping CLI plugin registration (the Refresh button needs it)."
elif ! "$CLAUDE_BIN" plugin list 2>/dev/null | grep -q "${PLUGIN_NAME}@${MARKETPLACE_NAME}"; then
  echo "[pulse-server] Registering ${PLUGIN_NAME} for headless runs (CLI user scope)..."
  "$CLAUDE_BIN" plugin marketplace list 2>/dev/null | grep -q "${MARKETPLACE_NAME}" || \
    "$CLAUDE_BIN" plugin marketplace add "$MARKETPLACE_URL" || \
    echo "[pulse-server] WARNING: could not add marketplace (offline?) — Refresh won't work until this succeeds; it retries on the next plugin update."
  "$CLAUDE_BIN" plugin install "${PLUGIN_NAME}@${MARKETPLACE_NAME}" || \
    echo "[pulse-server] WARNING: CLI plugin install failed — Refresh won't work until this succeeds; it retries on the next plugin update."
else
  # Already registered: pull it up to the current version (security review D6 — the
  # CLI-scope copy doesn't track Desktop-side updates by itself; observed
  # live 2026-07-03: a stale 0.2.2 CLI copy re-pointed launchd at an
  # ephemeral path and killed the viewer). Soft-fail: offline just warns.
  "$CLAUDE_BIN" plugin update "${PLUGIN_NAME}@${MARKETPLACE_NAME}" || \
    echo "[pulse-server] WARNING: CLI plugin update failed (offline?) — Refresh runs the previously registered version until it succeeds."
fi

echo "[pulse-server] Done. Bookmark http://127.0.0.1:8787 in Chrome"
echo "[pulse-server] (Chrome > Settings > On startup > Open specific page)."
