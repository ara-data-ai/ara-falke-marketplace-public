"""Falke Pulse local viewer — serves the latest morning-pulse HTML at
http://127.0.0.1:8787 with a Refresh button that triggers a headless
Claude Code run of the falke-business-pulse skill.

Design constraints (see README + the security review):
- SERVING and REFRESHING fail independently: the page always shows the last
  successful pulse; a broken refresh path only disables the button, never the view.
- Bound to 127.0.0.1 ONLY. Never expose on the network.
- The refresh command is a FIXED argv run with shell=False — not configurable,
  not shell-interpreted (Floyd F3). Only pulse_html_dir is config-overridable.
- /status never returns raw CLI output (mail fragments can appear in error
  tails — Floyd F5); full stderr goes to the refresh log file only.
- Every response carries a strict CSP so mail-derived pulse content cannot
  script against the endpoints (Floyd F6); Host allowlist blocks DNS-rebinding
  (Floyd F2).
- Stdlib only — no dependencies beyond Python >=3.10 (already required by the plugin).
"""

from __future__ import annotations

import glob
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

# Only the pulse directory is configurable (config.json key of the same name).
DEFAULTS = {
    # Directory the skill writes pulse-YYYY-MM-DD.html files into.
    "pulse_html_dir": "~/Claude/Projects/Falke-Business-Pulse",
}

# FIXED refresh argv (Floyd F3: no shell, no config override — a config-file
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
        # Take only the known, safe key — never a command (Floyd F3).
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


def _render(nonce: str) -> bytes:
    path = _latest_pulse()
    bar = TOOLBAR.replace("__STAMP__", _stamp(path)).replace("__NONCE__", nonce)
    if not path:
        return PLACEHOLDER.replace("__TOOLBAR__", bar).encode()
    with open(path, encoding="utf-8") as f:
        html = f.read()
    # Inject the toolbar immediately after the opening <body> tag.
    injected, n = re.subn(r"(<body[^>]*>)", r"\1" + bar.replace("\\", "\\\\"), html, count=1)
    return (injected if n else bar + html).encode()


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
            # return it over HTTP (Floyd F5).
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
        # except our nonce'd toolbar script (Floyd F6). Inline styles and data:
        # images are what the template legitimately uses.
        script = f"script-src 'nonce-{nonce}'; " if nonce else "script-src 'none'; "
        return (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            + script
            + "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )

    def _host_ok(self) -> bool:
        # DNS-rebinding defense (Floyd F2): an attacker page that rebinds its
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
