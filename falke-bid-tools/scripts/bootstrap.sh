#!/usr/bin/env bash
# falke-bid-tools — first-run dependency bootstrap (SessionStart hook).
#
# Installs the union of both engines' Python deps ONCE into a persistent venv
# under ${CLAUDE_PLUGIN_DATA}, reinstalling only when the bundled
# requirements.txt changes (the canonical diff-stamp pattern from
# code.claude.com/docs/en/plugins-reference, verified 2026-05-30).
#
# Chromium is the riskiest step. If `playwright install chromium` fails in the
# sandbox, we DO NOT abort — we write an HTML-only marker so the scorecard skill
# switches to `--html-only` and still produces a deliverable (HTML, no PDF).
# This makes the PDF/HTML fallback SWITCHABLE per Boris's spike verdict.
#
# Idempotent and quiet on the happy path. Floyd reviews this for the execution
# threat model before ship.

set -euo pipefail

DATA="${CLAUDE_PLUGIN_DATA:?CLAUDE_PLUGIN_DATA not set}"
ROOT="${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set}"

VENV="${DATA}/venv"
REQ="${ROOT}/engines/requirements.txt"
STAMP="${DATA}/requirements.installed.txt"
RENDER_MODE="${DATA}/render-mode"   # "chromium" or "html-only" — read by the skill

mkdir -p "${DATA}"

# Reinstall only if the bundled manifest differs from what we last installed
# (covers both first run and a dependency-changing plugin update).
if [ ! -f "${STAMP}" ] || ! diff -q "${REQ}" "${STAMP}" >/dev/null 2>&1; then
  echo "[falke-bid-tools] Setting up dependencies (first run may take ~1-2 min)..." >&2

  python3 -m venv "${VENV}"
  "${VENV}/bin/pip" install --quiet --upgrade pip
  "${VENV}/bin/pip" install --quiet -r "${REQ}"

  # --- Chromium: the risky step. Failure is NON-fatal (switch to HTML-only). ---
  if "${VENV}/bin/playwright" install chromium >/dev/null 2>&1; then
    echo "chromium" > "${RENDER_MODE}"
    echo "[falke-bid-tools] PDF rendering ready (Chromium)." >&2
  else
    echo "html-only" > "${RENDER_MODE}"
    echo "[falke-bid-tools] WARN: Chromium install failed — scorecard will produce HTML only (no PDF). See README." >&2
  fi

  # Stamp success LAST so a mid-install failure retries next session.
  cp "${REQ}" "${STAMP}"
  echo "[falke-bid-tools] Setup complete." >&2
fi

exit 0
