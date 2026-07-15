#!/bin/zsh
# release.sh — the ONE way to ship falke-business-pulse.
#
# Encodes the two release rules adopted at the security review (2026-07-12):
#   RULE 1: a push alone is NOT a release — installed copies only pull content
#           when plugin.json's version bumps. This script REFUSES to ship an
#           unbumped version.
#   RULE 2: the public marketplace copy must stay byte-identical to the private
#           source (the public/private split is bid-tools-only). This script
#           does the sync itself, so it can't be forgotten.
#
# Usage:  ./scripts/release.sh /path/to/ara-falke-marketplace-public-clone
#         (run from anywhere; the plugin root is derived from this script)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"          # plugin root (private source)
PRIVATE_REPO="$(cd "$HERE/.." && pwd)"            # private marketplace repo root
PUBLIC_CLONE="${1:?usage: release.sh /path/to/public-marketplace-clone}"

fail() { echo "[release] BLOCKED: $1" >&2; exit 1; }

# --- 0. sanity ---------------------------------------------------------------
[ -f "$PUBLIC_CLONE/.claude-plugin/marketplace.json" ] || fail "'$PUBLIC_CLONE' is not a marketplace clone"
git -C "$PRIVATE_REPO" diff --quiet || fail "private repo has unstaged changes — commit or stash first"
git -C "$PRIVATE_REPO" diff --cached --quiet || fail "private repo has staged-uncommitted changes"

# --- 1. RULE 1: version must be bumped vs the public marketplace HEAD --------
VERSION=$(python3 -c "import json; print(json.load(open('$HERE/.claude-plugin/plugin.json'))['version'])")
git -C "$PUBLIC_CLONE" fetch -q origin main
PUB_VERSION=$(git -C "$PUBLIC_CLONE" show origin/main:falke-business-pulse/.claude-plugin/plugin.json 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])" 2>/dev/null || echo "none")
[ "$VERSION" != "$PUB_VERSION" ] || fail "version $VERSION already published — bump plugin.json first (a push alone is not a release)"
echo "[release] shipping $PUB_VERSION -> $VERSION"

# --- 2. quality gates: full test sweep + compile ------------------------------
echo "[release] running test suites..."
( cd "$HERE/apple-mail" && python3 -m unittest discover -s tests -q )
( cd "$HERE/pulse-server" && python3 -m unittest discover -s tests -q )
( cd "$HERE" && python3 scripts/smoke_read_shape.py >/dev/null ) || fail "offline smoke failed"
python3 -m py_compile "$HERE"/apple-mail/*.py "$HERE"/pulse-server/server.py
osacompile -o /dev/null "$HERE"/apple-mail/applescript/read_account.applescript
bash -n "$HERE/scripts/bootstrap.sh" && zsh -n "$HERE/pulse-server/install.sh"
echo "[release] all gates green."

# --- 3. internal-name scrub guard (public repo is client-visible) -------------
# The pattern is assembled from split strings so this file can never match its
# own source (the whole tree is swept, no exclusions — the audit rule).
SCRUB_PATTERN="flo""yd|der""ick|bes""sie|\ban""na\b|mag""gie|bor""is|cian""dro|darv""ish"
if grep -rniE "$SCRUB_PATTERN" "$HERE" \
     --include="*.py" --include="*.sh" --include="*.md" --include="*.applescript" \
     --include="*.json" --include="*.html" 2>/dev/null | grep -v __pycache__; then
  fail "internal names found (above) — scrub before shipping to the public repo"
fi

# --- 4. push private, sync public byte-identically, push public ---------------
git -C "$PRIVATE_REPO" push
rsync -a --delete --exclude "__pycache__" "$HERE/" "$PUBLIC_CLONE/falke-business-pulse/"
MSG=$(git -C "$PRIVATE_REPO" log -1 --pretty=%s)
git -C "$PUBLIC_CLONE" add -A
if git -C "$PUBLIC_CLONE" diff --cached --quiet; then
  echo "[release] public already identical — nothing to sync."
else
  git -C "$PUBLIC_CLONE" commit -q -m "$MSG (sync from private)"
  git -C "$PUBLIC_CLONE" push -q
fi

# --- 5. verify byte-parity, then roll the local CLI copy ----------------------
if ! diff -r -x "__pycache__" "$HERE" "$PUBLIC_CLONE/falke-business-pulse" >/dev/null; then
  fail "public copy is NOT byte-identical after sync — investigate before installing anywhere"
fi
claude plugin update falke-business-pulse@ara-falke 2>/dev/null | tail -1 || true

echo "[release] DONE: $VERSION on both marketplaces (byte-verified). Fleet machines pick it up via 'claude plugin update' / Directory sync."
