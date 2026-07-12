"""Falke Pulse local viewer — serves the latest morning-pulse HTML at
http://127.0.0.1:8787 with a Refresh button that triggers a headless
Claude Code run of the falke-business-pulse skill.

Design constraints (see README + the security review):
- SERVING and REFRESHING fail independently: the page always shows the last
  successful pulse; a broken refresh path only disables the button, never the view.
- Bound to 127.0.0.1 ONLY. Never expose on the network.
- The refresh command is a FIXED argv run with shell=False — not configurable,
  not shell-interpreted (security review F3). Only pulse_html_dir is config-overridable.
- /status never returns raw CLI output (mail fragments can appear in error
  tails — security review F5); full stderr goes to the refresh log file only.
- Every response carries a strict CSP so mail-derived pulse content cannot
  script against the endpoints (security review F6); Host allowlist blocks DNS-rebinding
  (security review F2).
- Stdlib only — no dependencies beyond Python >=3.10 (already required by the plugin).
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8787
CONFIG_PATH = os.path.expanduser("~/.falke-business-pulse/config.json")
REFRESH_LOG = os.path.expanduser("~/Library/Logs/falke-pulse-server/refresh.log")
# Last-scan integrity marker written by the apple-mail read core (read_core.py /
# config.read_scan_status_path). COND-5 structural backstop: if the last read was
# `status: "partial"`, the served HTML gets a prominent "incomplete scan" banner
# injected HERE, by construction — so a prompt-injection in a surviving message
# cannot suppress the human-facing warning (the banner is not the model's choice).
SCAN_STATUS_PATH = os.path.expanduser("~/.falke-business-pulse/last-scan-status.json")

# Only the pulse directory is configurable (config.json key of the same name).
DEFAULTS = {
    # Directory the skill writes pulse-YYYY-MM-DD.html files into.
    "pulse_html_dir": "~/Claude/Projects/Falke-Business-Pulse",
}

# FIXED refresh argv (security review F3: no shell, no config override — a config-file
# write must not become persistent arbitrary exec). --permission-mode
# acceptEdits lets the skill write the HTML + state files without prompting;
# --allowedTools pre-approves the plugin's two mail tools so the headless run
# never stalls on a permission prompt (headless runs cannot prompt).
REFRESH_ARGS = [
    "-p", "run my morning pulse",
    "--permission-mode", "acceptEdits",
    "--allowedTools",
    "mcp__plugin_falke-business-pulse_apple-mail__read_apple_mail,"
    "mcp__plugin_falke-business-pulse_apple-mail__create_apple_mail_draft",
]

# launchd runs us with a minimal PATH; resolve the claude CLI explicitly.
CLAUDE_CANDIDATES = [
    shutil.which("claude"),
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/.claude/local/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
]

_state_lock = threading.Lock()
_state = {"refreshing": False, "last_error": None, "last_exit": None}


def _config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            loaded = json.load(f)
        # Take only the known, safe key — never a command (security review F3).
        if isinstance(loaded, dict) and isinstance(loaded.get("pulse_html_dir"), str):
            cfg["pulse_html_dir"] = loaded["pulse_html_dir"]
    except (OSError, ValueError):
        pass
    return cfg


def _claude_bin() -> str | None:
    for cand in CLAUDE_CANDIDATES:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _latest_pulse() -> str | None:
    d = os.path.expanduser(_config()["pulse_html_dir"])
    files = glob.glob(os.path.join(d, "pulse-*.html"))
    return max(files, key=os.path.getmtime) if files else None


def _stamp(path: str | None) -> str:
    if not path:
        return "no pulse generated yet"
    t = datetime.fromtimestamp(os.path.getmtime(path))
    return "Data last refreshed on " + t.strftime("%H:%M:%S %d/%m/%Y")


def _log_refresh(text: str) -> None:
    try:
        os.makedirs(os.path.dirname(REFRESH_LOG), exist_ok=True)
        with open(REFRESH_LOG, "a") as f:
            f.write(f"--- {datetime.now().isoformat()} ---\n{text}\n")
    except OSError:
        pass


TOOLBAR = """
<div id="pulse-toolbar" style="position:sticky;top:0;z-index:9999;display:flex;
  align-items:center;gap:14px;padding:8px 16px;background:#1A2A33;color:#fff;
  font:12px 'Helvetica Neue',Arial,sans-serif;border-bottom:3px solid #F15022;">
  <strong style="letter-spacing:.08em;">FALKE PULSE</strong>
  <span id="pulse-stamp" style="color:#c8d2d8;">__STAMP__</span>
  <button id="pulse-refresh" style="margin-left:auto;
    background:#F15022;color:#fff;border:none;border-radius:4px;
    padding:6px 14px;font:bold 12px 'Helvetica Neue',Arial,sans-serif;
    cursor:pointer;">Refresh</button>
</div>
<script nonce="__NONCE__">
document.getElementById('pulse-refresh').addEventListener('click', async () => {
  const b = document.getElementById('pulse-refresh');
  b.disabled = true; b.textContent = 'Refreshing\\u2026';
  try {
    const r = await fetch('/refresh', {method: 'POST'});
    if (!r.ok) throw new Error(await r.text());
    const poll = setInterval(async () => {
      const s = await (await fetch('/status')).json();
      if (!s.refreshing) {
        clearInterval(poll);
        if (s.last_error) {
          b.disabled = false; b.textContent = 'Refresh';
          alert(s.last_error);
        } else { location.reload(); }
      }
    }, 5000);
  } catch (e) {
    b.disabled = false; b.textContent = 'Refresh';
    alert('Could not start refresh \\u2014 open Claude Code and run the pulse manually.');
  }
});
</script>
"""

PLACEHOLDER = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Falke Pulse</title></head><body style="font-family:'Helvetica Neue',Arial,sans-serif;">
__TOOLBAR__
<div style="max-width:600px;margin:80px auto;text-align:center;color:#5E6E76;">
  <h2 style="color:#1A2A33;">No pulse yet</h2>
  <p>Click <strong>Refresh</strong> above to run this morning's pulse,
  or run the falke-business-pulse skill from Claude Code.</p>
</div></body></html>"""


def _scan_status() -> dict:
    """Read the last-scan integrity marker written by the read core. Returns {} on
    ANY problem (absent / unreadable / not JSON / not a dict) — never raises."""
    try:
        with open(SCAN_STATUS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _pulse_run_token(page_html: str) -> str | None:
    """Extract the run token the pulse-save step stamped into the served HTML
    (`<!-- falke-pulse-run: TOKEN -->`), or None if absent/unparseable. The token is
    the read tool's `cutoff`, so it correlates the served pulse to the status
    marker by IDENTITY — not by which file is newer (the marker is written DURING
    the read, the pulse saved AFTER, so mtime ordering is the wrong signal)."""
    m = re.search(r"<!--\s*falke-pulse-run:\s*(.*?)\s*-->", page_html)
    return m.group(1).strip() if m else None


# --- On-brand banner styling, inlined for SELF-CONTAINED injection -------
# The served HTML gets the banner prepended into an arbitrary pulse, so the styling
# is INLINE (no dependency on the pulse's <style>, no <script>, no external assets —
# CSP-safe under style-src 'unsafe-inline'). This mirrors the class-based
# .scan-banner block in reference/digest-template.html for the inline
# (model-rendered) pulse. FUNCTIONAL alert palette held outside the brand-orange
# rule: RED #A4161A (edge/account-chip #6E0D10), AMBER #8A5A00 (edge #5C3C00), white
# type; IBM Plex Sans body + IBM Plex Mono status code/account chip (system
# fallbacks). NOTE: the AMBER #8A5A00-vs-brand-orange functional-color exception is
# flagged for the security review's conscious sign-off.
_BANNER_BASE = (
    "position:sticky;top:0;z-index:10000;display:flex;align-items:center;"
    "flex-wrap:wrap;gap:8px 14px;padding:11px 18px;color:#FFFFFF;"
    "font-family:'IBM Plex Sans','Helvetica Neue',Helvetica,Arial,'Liberation Sans',sans-serif;"
    "font-size:13px;line-height:1.4;-webkit-print-color-adjust:exact;print-color-adjust:exact;"
)
_BANNER_CODE = (
    "font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
    "font-weight:700;font-size:11px;letter-spacing:0.14em;white-space:nowrap;"
    "padding:2px 9px;border:1px solid rgba(255,255,255,0.55);border-radius:2px;"
)
_BANNER_ACCOUNTS = (
    "font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
    "font-weight:700;letter-spacing:0.01em;background:#6E0D10;color:#fff;"
    "padding:2px 8px;border-radius:2px;white-space:normal;"
)


def _incomplete_banner(info: dict) -> str:
    """RED 'incomplete scan' banner (brand styling) naming the affected account(s)
    — those that FAILED (timed out / stalled, contributed nothing) or were CAPPED
    (read but older in-window mail may be unread). Built ENTIRELY from the Python-
    written marker (account name, HTML-escaped) — no model output flows into it, so an
    injected 'hide the warning' instruction cannot remove it and the account name
    cannot smuggle markup. No script (CSP-safe). Load-bearing (asserted):
    id="pulse-scan-warning", the string "INCOMPLETE SCAN", and the escaped account
    name(s) as visible text."""
    failed = [
        html.escape(str(f.get("account", "?")))
        for f in (info.get("accounts_failed") or []) if isinstance(f, dict)
    ]
    capped = [
        html.escape(str(c.get("account", "?")))
        for c in (info.get("accounts_capped") or []) if isinstance(c, dict)
    ]
    escaped = failed + capped
    names = ", ".join(escaped) or "one or more accounts"
    # "is missing" when mail is definitely absent (failures only); "may be missing"
    # once any account was CAPPED (older in-window mail may or may not exist).
    verb = "is" if not capped else "may be"
    tail = (
        "That account was not fully read this run (timed out or read-capped)"
        if len(escaped) == 1
        else "Those accounts were not fully read this run (timed out or read-capped)"
    )
    return (
        f'<div id="pulse-scan-warning" role="alert" style="{_BANNER_BASE}'
        'background:#A4161A;border-left:6px solid #6E0D10;border-bottom:3px solid #6E0D10;">'
        '<svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" '
        'focusable="false" style="display:block;flex:0 0 auto;">'
        '<path d="M12 2.8 L21.6 20.4 L2.4 20.4 Z" fill="#fff"/>'
        '<line x1="12" y1="9.2" x2="12" y2="14.4" stroke="#A4161A" stroke-width="2.2" '
        'stroke-linecap="round"/><circle cx="12" cy="17.4" r="1.25" fill="#A4161A"/></svg>'
        f'<span style="{_BANNER_CODE}">INCOMPLETE SCAN</span>'
        f'<span style="font-weight:500;"><strong style="font-weight:700;">This pulse {verb} '
        f'missing mail from</strong> <span style="{_BANNER_ACCOUNTS}">{names}</span>. '
        f"{tail} &mdash; treat the pulse below as partial.</span></div>"
    )


def _neutral_banner() -> str:
    """AMBER 'scan status unknown' banner (brand styling). N-A: this is the
    AUTHORITATIVE completeness surface, so it must NEVER imply 'complete' when it
    cannot confirm the served pulse matches the marker — say so instead of staying
    silent. Default on ANY uncertainty. Load-bearing (asserted): id and the string
    "SCAN STATUS UNKNOWN". No script (CSP-safe)."""
    return (
        f'<div id="pulse-scan-warning" role="status" style="{_BANNER_BASE}'
        'background:#8A5A00;border-left:6px solid #5C3C00;border-bottom:3px solid #5C3C00;">'
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true" '
        'focusable="false" style="display:block;flex:0 0 auto;">'
        '<path d="M12 3.4 L21 19.4 L3 19.4 Z" fill="none" stroke="#fff" stroke-width="2" '
        'stroke-linejoin="round"/><line x1="12" y1="9" x2="12" y2="13.6" stroke="#fff" '
        'stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.4" r="1.1" '
        'fill="#fff"/></svg>'
        f'<span style="{_BANNER_CODE}">SCAN STATUS UNKNOWN</span>'
        '<span style="font-weight:500;"><strong style="font-weight:700;">Treat this pulse '
        "as possibly incomplete.</strong> Couldn&rsquo;t confirm it matches the latest "
        "scan status.</span></div>"
    )


def _status_banner(page_html) -> str:
    """The structural scan-completeness banner injected into the SERVED HTML.

    Correlation is by RUN TOKEN (identity), NOT mtime: the marker is written DURING
    the read and the pulse saved AFTER, so "marker older than pulse" is the NORMAL
    ordering, not a staleness signal. Decision:
      - marker status "partial"  -> RED INCOMPLETE banner (names accounts). This is
        driven by the marker's status ALONE, independent of the token — so a
        spoofed pulse token can NEVER hide a genuine partial.
      - marker present, "ok", and its run token (cutoff) MATCHES the token stamped
        in this pulse -> no banner (confirmed complete).
      - marker ABSENT, or either token missing/unparseable, or tokens DIFFER
        -> amber SCAN-STATUS-UNKNOWN (default to caution on ANY uncertainty).
    """
    info = _scan_status()
    if info.get("status") == "partial":
        return _incomplete_banner(info)  # status-driven; token-independent
    if page_html is not None:
        marker_token = info.get("cutoff")
        pulse_token = _pulse_run_token(page_html)
        if not info or not marker_token or not pulse_token or marker_token != pulse_token:
            return _neutral_banner()
    return ""


def _render(nonce: str) -> bytes:
    path = _latest_pulse()
    bar = TOOLBAR.replace("__STAMP__", _stamp(path)).replace("__NONCE__", nonce)
    page = None
    if path:
        with open(path, encoding="utf-8") as f:
            page = f.read()
    # COND-5 structural backstop: prepend the scan-completeness banner (or "") ABOVE
    # the toolbar. It is built from the Python-written marker, so it CANNOT be
    # suppressed by anything the model rendered into the pulse body.
    chrome = _status_banner(page) + bar
    if not path:
        return PLACEHOLDER.replace("__TOOLBAR__", chrome).encode()
    # Inject banner+toolbar immediately after the opening <body> tag.
    injected, n = re.subn(r"(<body[^>]*>)", r"\1" + chrome.replace("\\", "\\\\"), page, count=1)
    return (injected if n else chrome + page).encode()


def _run_refresh() -> None:
    claude = _claude_bin()
    if not claude:
        with _state_lock:
            _state.update(
                refreshing=False, last_exit=-1,
                last_error="Refresh failed: Claude Code CLI not found — open Claude Code and run the pulse manually.",
            )
        _log_refresh("claude CLI not found in any known location")
        return
    cwd = os.path.expanduser(_config()["pulse_html_dir"])
    try:
        proc = subprocess.run(
            [claude, *REFRESH_ARGS], shell=False, cwd=cwd,
            capture_output=True, text=True, timeout=900,
        )
        if proc.returncode == 0:
            err = None
        else:
            # Raw CLI output can contain mail fragments — log it, never
            # return it over HTTP (security review F5).
            _log_refresh(f"exit {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
            err = (
                f"Refresh failed (exit {proc.returncode}) — open Claude Code and run "
                "the pulse manually. Details: ~/Library/Logs/falke-pulse-server/refresh.log"
            )
        with _state_lock:
            _state.update(refreshing=False, last_exit=proc.returncode, last_error=err)
    except Exception as e:  # timeout, missing cwd, etc. — fail loud in the log
        _log_refresh(f"exception: {type(e).__name__}: {e}")
        with _state_lock:
            _state.update(
                refreshing=False, last_exit=-1,
                last_error="Refresh failed — open Claude Code and run the pulse manually. "
                           "Details: ~/Library/Logs/falke-pulse-server/refresh.log",
            )


class Handler(BaseHTTPRequestHandler):
    def _csp(self, nonce: str | None) -> str:
        # Pulse HTML is generated from mail-derived content: same-origin as the
        # /refresh + /status endpoints, so scripts in it must be impossible
        # except our nonce'd toolbar script (security review F6). Inline styles and data:
        # images are what the template legitimately uses.
        script = f"script-src 'nonce-{nonce}'; " if nonce else "script-src 'none'; "
        return (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            + script
            + "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )

    def _host_ok(self) -> bool:
        # DNS-rebinding defense (security review F2): an attacker page that rebinds its
        # hostname to 127.0.0.1 still sends its own Host header — reject it.
        host = (self.headers.get("Host") or "").strip()
        return re.match(r"^(127\.0\.0\.1|localhost)(:\d+)?$", host) is not None

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              nonce: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", self._csp(nonce))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            self._send(403, b"forbidden", "text/plain")
            return
        if self.path == "/status":
            with _state_lock:
                body = json.dumps(_state).encode()
            self._send(200, body, "application/json")
        else:
            nonce = secrets.token_urlsafe(16)
            self._send(200, _render(nonce), nonce=nonce)

    def do_POST(self):
        if not self._host_ok():
            self._send(403, b"forbidden", "text/plain")
            return
        if self.path != "/refresh":
            self._send(404, b"not found", "text/plain")
            return
        origin = self.headers.get("Origin", "")
        if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            self._send(403, b"forbidden", "text/plain")
            return
        with _state_lock:
            if _state["refreshing"]:
                self._send(409, b"refresh already running", "text/plain")
                return
            _state.update(refreshing=True, last_error=None, last_exit=None)
        threading.Thread(target=_run_refresh, daemon=True).start()
        self._send(202, b"started", "text/plain")

    def log_message(self, *args):  # keep launchd logs quiet
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
