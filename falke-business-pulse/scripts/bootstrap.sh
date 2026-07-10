#!/usr/bin/env bash
# falke-business-pulse — first-run dependency bootstrap (SessionStart hook).
#
# Makes the plugin self-sufficient on a fresh, non-technical laptop:
#   1. Finds Python >=3.10 — and if none exists on macOS, downloads a private
#      standalone CPython into ${CLAUDE_PLUGIN_DATA} (no Homebrew, no admin
#      password, nothing touched outside the plugin's own data dir).
#   2. Creates the venv for the apple-mail MCP server (mcp>=1.2.0), reinstalling
#      only when the bundled requirements.txt changes (diff-stamp pattern).
#   3. macOS local sessions only: installs/refreshes the pulse-viewer launchd
#      agents (localhost:8787 + 7:00 AM weekday refresh), re-running only when
#      the pulse-server files change (hash-stamp), so the server isn't churned
#      every session.
#
# Idempotent and quiet on the happy path. Floyd reviews this for the execution
# threat model before ship.

set -euo pipefail

DATA="${CLAUDE_PLUGIN_DATA:?CLAUDE_PLUGIN_DATA not set}"
ROOT="${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set}"

VENV="${DATA}/venv"
REQ="${ROOT}/apple-mail/requirements.txt"
STAMP="${DATA}/requirements.installed.txt"

mkdir -p "${DATA}"

# ---------------------------------------------------------------------------
# 1. Python >=3.10 (the mcp SDK floor). Candidates include a previously
#    downloaded standalone copy under DATA. macOS stock python3 is often 3.9,
#    which CANNOT install `mcp`.
# ---------------------------------------------------------------------------
PYBIN=""
for cand in "${DATA}/python/bin/python3" python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "${cand}" >/dev/null 2>&1 && \
     "${cand}" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
    PYBIN="${cand}"
    break
  fi
done

# No suitable Python + we're on a Mac -> download a pinned standalone CPython
# (astral-sh/python-build-standalone, install_only build) into DATA. One-time,
# ~45 MB, no admin rights, fully contained in the plugin data dir.
if [ -z "${PYBIN}" ] && [ "$(uname -s)" = "Darwin" ]; then
  echo "[falke-business-pulse] No Python >=3.10 found — downloading a private copy (one-time, ~45 MB)..." >&2
  # Pinned release + per-arch SHA-256 (from the published .sha256 sibling
  # assets). Verified fail-closed below — a tampered or truncated download
  # never becomes the interpreter that runs the mail agent (Floyd gate F1).
  case "$(uname -m)" in
    arm64)  PBS_ARCH="aarch64"
            PBS_SHA="5dfd4d81ad8ea0407e6153ed998a5fba332275c60ece81c6db2b58e443de60b9" ;;
    *)      PBS_ARCH="x86_64"
            PBS_SHA="ea29fd1b174daf71ff0b3d14e7a36f8afc3ded2a2b086d1d39b8a4ee95dada24" ;;
  esac
  PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250106/cpython-3.12.8%2B20250106-${PBS_ARCH}-apple-darwin-install_only.tar.gz"
  curl -fsSL "${PBS_URL}" -o "${DATA}/python.tar.gz"
  if ! echo "${PBS_SHA}  ${DATA}/python.tar.gz" | shasum -a 256 -c - >/dev/null 2>&1; then
    rm -f "${DATA}/python.tar.gz"
    echo "[falke-business-pulse] ERROR: Python download failed checksum verification — refusing to install it." >&2
    echo "[falke-business-pulse] Retry by reopening the session; if it persists, contact ARA." >&2
    exit 1
  fi
  tar -xzf "${DATA}/python.tar.gz" -C "${DATA}"
  rm -f "${DATA}/python.tar.gz"
  PYBIN="${DATA}/python/bin/python3"
  echo "[falke-business-pulse] Python ready (checksum verified)." >&2
fi

if [ -z "${PYBIN}" ]; then
  echo "[falke-business-pulse] ERROR: need Python >=3.10 for the mail tool, none found." >&2
  echo "[falke-business-pulse] Install it (e.g. 'brew install python@3.12') and reopen the session." >&2
  exit 1
fi

# Absolute path (launchd plists cannot use a bare command name).
case "${PYBIN}" in
  /*) PYABS="${PYBIN}" ;;
  *)  PYABS="$(command -v "${PYBIN}")" ;;
esac

# ---------------------------------------------------------------------------
# 2. MCP-server venv — reinstall only if the bundled manifest changed.
# ---------------------------------------------------------------------------
if [ ! -f "${STAMP}" ] || ! diff -q "${REQ}" "${STAMP}" >/dev/null 2>&1; then
  echo "[falke-business-pulse] Setting up dependencies (first run may take ~30s)..." >&2

  "${PYABS}" -m venv "${VENV}"
  "${VENV}/bin/pip" install --quiet --upgrade pip
  "${VENV}/bin/pip" install --quiet -r "${REQ}"

  # Stamp success LAST so a mid-install failure retries next session.
  cp "${REQ}" "${STAMP}"
  echo "[falke-business-pulse] Setup complete." >&2
fi

# ---------------------------------------------------------------------------
# 3. Pulse viewer (macOS local sessions only — launchd doesn't exist in the
#    Cowork cloud sandbox). Install/refresh only when the files change.
# ---------------------------------------------------------------------------
PS_DIR="${ROOT}/pulse-server"
# The stamp lives in the DURABLE server dir, NOT ${DATA}: ${DATA} is per plugin
# SCOPE (Desktop vs CLI-user have separate data dirs), so a per-DATA stamp made
# every newly-seen scope re-run the installer — re-pointing launchd plists and
# RESTARTING the server from inside the very headless session the server's own
# Refresh button spawned (self-kill, observed live 2026-07-03). One durable
# stamp means all scopes agree the viewer is already installed.
PS_STAMP="$HOME/Library/Application Support/falke-pulse-server/.installed-sig"
if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1 && [ -d "${PS_DIR}" ]; then
  PS_SIG="$(cat "${PS_DIR}/server.py" "${PS_DIR}/install.sh" "${PS_DIR}"/launchd/*.plist 2>/dev/null | shasum | cut -d' ' -f1)"
  # Healthy = stamp current AND the loaded plist anchored to the DURABLE copy
  # AND the server actually answering. Any miss -> reinstall (idempotent).
  # This is the self-heal layer: a stale plugin copy re-pointing the plist, a
  # deleted Application Support file, or any novel death KeepAlive can't fix
  # gets repaired at the next session start instead of staying down until a
  # human notices (incident 2026-07-03). NOTE: the curl means a session opened
  # while the server is mid-restart may reinstall once — harmless, idempotent.
  PS_HEALTHY=0
  if [ -f "${PS_STAMP}" ] && [ "$(cat "${PS_STAMP}")" = "${PS_SIG}" ] && \
     grep -q "falke-pulse-server/server.py" "$HOME/Library/LaunchAgents/com.falke.pulse-server.plist" 2>/dev/null && \
     curl -s --max-time 3 -o /dev/null "http://127.0.0.1:8787/status"; then
    PS_HEALTHY=1
  fi
  if [ "${PS_HEALTHY}" -eq 0 ]; then
    echo "[falke-business-pulse] Installing/repairing the pulse viewer — bookmark http://127.0.0.1:8787" >&2
    PYTHON="${PYABS}" bash "${PS_DIR}/install.sh" --with-morning-run >&2
    mkdir -p "$(dirname "${PS_STAMP}")"
    echo "${PS_SIG}" > "${PS_STAMP}"
  fi
fi

exit 0
