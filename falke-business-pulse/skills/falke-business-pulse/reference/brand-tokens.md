# Falke brand tokens — the canonical values

Change a color HERE first, then propagate to the four consumers below and
re-verify. (The values are necessarily inlined where they render — HTML/e-mail
clients and Adaptive Cards can't import a token file — so this page is the
source of truth that keeps the copies honest.)

| Token | Value | Use |
|---|---|---|
| Falke orange | `#F15022` | Accent rule, buttons, category ② highlight, bullets |
| Falke deep orange | `#C7491A` | Header gradient end, eyebrow/emphasis text |
| Falke navy | `#1A2A33` | Toolbar, headings, category numbers |
| Muted ink | `#5E6E76` | Secondary text |
| Alert red (functional) | `#A4161A` / edge `#6E0D10` | INCOMPLETE-SCAN banner — deliberately OUTSIDE the brand palette (alert must not look like brand chrome; consciously signed off at the security review) |
| Alert amber (functional) | `#8A5A00` / edge `#5C3C00` | SCAN-STATUS-UNKNOWN banner — same exception |

## Consumers (update all four on any change)

1. `reference/digest-template.html` — the saved/e-mailable pulse page
2. `pulse-server/server.py` — viewer toolbar + structural banners
3. `reference/teams-card.md` + `apple-mail/teams_core.py` — the Teams card
4. `SKILL.md` Step 3.5 — the inline-widget adaptation rules (brand colors kept
   fixed across light/dark themes)

Verify after a change: `grep -rn "<old-hex>" .` from the plugin root must
return nothing.
