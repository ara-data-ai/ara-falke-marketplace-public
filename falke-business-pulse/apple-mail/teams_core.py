"""teams_core — the deterministic Teams-digest post (the ONE automated send).

Design (consolidation release; closes the headless-Teams gap):
- The model/skill passes DATA FIELDS ONLY (tldr, waiting, needs_response, ...).
  This module builds the ENTIRE fixed Adaptive Card itself, so an injection in a
  scanned message can never restructure the card, add Action.* elements, change
  the destination, or grow new fields — the card template is code, not prompt
  output. (This module is the EXECUTABLE source of truth for the card;
  reference/teams-card.md documents the rationale and visual convention.)
- Action-free by construction: no Action.* element exists anywhere in the
  template. The only image is the OPTIONAL configured brand logo
  (config `falke_logo_url`) — never a URL from scanned content.
- The webhook URL is a SECRET: read from ~/.falke-business-pulse/config.json
  (`teams_webhook_url`), used only for the POST, never echoed into the result,
  the run-log, or an error message.
- Fail-closed + fail-soft: no webhook => clean "skipped" (never an error — Teams
  is optional); malformed/oversized fields => bounded/truncated with a flag;
  network/HTTP failure => "error" with a GENERIC reason (detail in the run-log,
  minus the URL). A Teams failure must never break the rest of the pulse.
- Stdlib only (urllib) — same zero-dependency discipline as the rest.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import urllib.error
import urllib.request

from config import run_log_path

CONFIG_PATH = os.path.expanduser("~/.falke-business-pulse/config.json")

# Bounds (COND-1 hardening: untrusted-derived text is length-bounded).
MAX_FIELD_LEN = 3000          # per data slot
MAX_PAYLOAD_BYTES = 24_000    # keep well under the ~25KB Workflows ceiling
POST_TIMEOUT_SECONDS = 15

# Falke brand (see reference/brand-tokens.md — the canonical token list).
_NAVY = "#1A2A33"

# The fixed section chrome, in the client's preferred order (② is the headline).
_SECTIONS = (
    ("TL;DR", "tldr"),
    ("② WAITING ON A CONTACT — TIME-SENSITIVE", "waiting"),
    ("① NEEDS YOUR RESPONSE", "needs_response"),
    ("③ HIGH-PRIORITY", "high_priority"),
    ("TODAY & THIS WEEK", "calendar"),
    ("DROPBOX — SURFACED TODAY", "dropbox"),
)


def _log(entry: dict) -> None:
    path = run_log_path()
    entry = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), **entry}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _bound(text, flag: list) -> str:
    """Coerce a data slot to a bounded plain string (truncate + flag oversize)."""
    s = str(text) if text is not None else ""
    if len(s) > MAX_FIELD_LEN:
        flag.append(True)
        s = s[: MAX_FIELD_LEN - 12] + "\n\n[truncated]"
    return s


def build_card(
    date_str: str,
    tldr: str = "",
    waiting: str = "",
    needs_response: str = "",
    high_priority: str = "",
    calendar: str = "",
    dropbox: str = "",
    drafts_note: str = "",
    scan_warning: str = "",
    logo_url: str | None = None,
) -> dict:
    """Build the FIXED Falke digest card. Data lands only in bounded text slots.

    Action-free, fixed section order, self-identifying header (COND-3). Returns
    the complete Workflows `message` envelope ready to POST.
    """
    truncated: list = []
    header_items: list[dict] = []
    if logo_url:
        header_items.append(
            {"type": "Image", "url": str(logo_url), "height": "28px", "altText": "FALKE"}
        )
    header_items.append(
        {
            "type": "TextBlock",
            "text": "FALKE",
            "weight": "Bolder",
            "size": "Large",
            "color": "Light",
            "spacing": "None",
        }
    )
    header_items.append(
        {
            "type": "TextBlock",
            "text": f"Falke CoS · Morning Pulse · {_bound(date_str, truncated)}",
            "isSubtle": True,
            "color": "Light",
            "spacing": "None",
            "wrap": True,
        }
    )

    body: list[dict] = [
        {"type": "Container", "style": "emphasis", "bleed": True, "items": header_items}
    ]

    if scan_warning:
        body.append(
            {
                "type": "Container",
                "style": "attention",
                "bleed": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "⚠ INCOMPLETE SCAN — " + _bound(scan_warning, truncated),
                        "weight": "Bolder",
                        "wrap": True,
                    }
                ],
            }
        )

    slot_values = {
        "tldr": tldr,
        "waiting": waiting,
        "needs_response": needs_response,
        "high_priority": high_priority,
        "calendar": calendar,
        "dropbox": dropbox,
    }
    for title, key in _SECTIONS:
        body.append(
            {
                "type": "TextBlock",
                "text": title,
                "weight": "Bolder",
                "size": "Small",
                "spacing": "Large",
            }
        )
        body.append(
            {
                "type": "TextBlock",
                "text": _bound(slot_values[key], truncated) or "—",
                "wrap": True,
                "spacing": "Small",
            }
        )

    body.append(
        {
            "type": "TextBlock",
            "text": _bound(drafts_note, truncated)
            or "Automated Falke CoS digest — drafts are never sent automatically.",
            "isSubtle": True,
            "size": "Small",
            "wrap": True,
            "spacing": "Large",
            "separator": True,
        }
    )

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "msteams": {"width": "Full"},
                    "body": body,
                },
            }
        ],
    }


def _assert_card_safe(payload: dict) -> None:
    """Structural self-check before POST (fail closed): action-free, one
    attachment, AdaptiveCard 1.5. Defense in depth — build_card is the only
    producer, but the wire payload is asserted anyway."""
    assert payload.get("type") == "message"
    atts = payload.get("attachments")
    assert isinstance(atts, list) and len(atts) == 1
    content = atts[0].get("content", {})
    assert content.get("type") == "AdaptiveCard" and content.get("version") == "1.5"
    blob = json.dumps(payload)
    assert '"Action.' not in blob, "card must stay action-free"


def post_teams_digest(
    date_str: str,
    tldr: str = "",
    waiting: str = "",
    needs_response: str = "",
    high_priority: str = "",
    calendar: str = "",
    dropbox: str = "",
    drafts_note: str = "",
    scan_warning: str = "",
    transport=None,
) -> dict:
    """Post the fixed morning-digest card to the ONE configured Teams channel.

    Returns {"status": "posted" | "skipped" | "error", "reason": <generic>}.
    - No `teams_webhook_url` in config => {"status": "skipped"} — Teams is
      OPTIONAL; skipping is clean, never an error.
    - The webhook URL never appears in the result, the run-log, or any error.
    - `transport` is injectable for tests (callable(url, body_bytes) -> int).
    """
    cfg = _load_config()
    webhook = cfg.get("teams_webhook_url")
    if not isinstance(webhook, str) or not webhook.strip().startswith("https://"):
        _log({"event": "teams_post_skipped", "reason": "no webhook configured"})
        return {"status": "skipped", "reason": "Teams is off — no webhook configured."}

    card = build_card(
        date_str=date_str,
        tldr=tldr,
        waiting=waiting,
        needs_response=needs_response,
        high_priority=high_priority,
        calendar=calendar,
        dropbox=dropbox,
        drafts_note=drafts_note,
        scan_warning=scan_warning,
        logo_url=cfg.get("falke_logo_url") if isinstance(cfg.get("falke_logo_url"), str) else None,
    )
    try:
        _assert_card_safe(card)
    except AssertionError as exc:
        _log({"event": "teams_post_skipped", "reason": f"off-template: {exc}"})
        return {"status": "error", "reason": "digest card failed the fixed-template check — not posted."}

    body = json.dumps(card).encode("utf-8")
    if len(body) > MAX_PAYLOAD_BYTES:
        _log({"event": "teams_post_skipped", "reason": f"payload too large ({len(body)}B)"})
        return {"status": "error", "reason": "digest card too large — not posted."}

    try:
        if transport is not None:
            code = transport(webhook.strip(), body)
        else:
            req = urllib.request.Request(
                webhook.strip(),
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=POST_TIMEOUT_SECONDS) as resp:
                code = resp.status
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # Generic outward reason; detail (minus the URL) to the run-log only.
        _log({"event": "teams_post_failed", "reason": type(exc).__name__})
        return {"status": "error", "reason": "Teams post failed (network) — digest not delivered to the channel."}

    if not (200 <= int(code) < 300):
        _log({"event": "teams_post_failed", "reason": f"http {code}"})
        return {"status": "error", "reason": f"Teams post failed (HTTP {code}) — digest not delivered."}

    _log({"event": "teams_post_ok", "bytes": len(body)})
    return {"status": "posted", "reason": "digest delivered to the configured channel."}
