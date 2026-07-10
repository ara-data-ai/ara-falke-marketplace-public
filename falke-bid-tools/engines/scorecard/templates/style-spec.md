# Falke Bid-Scorecard — Style Spec & Token Sheet

Companion to `scorecard-template.html`. This is the canonical reference for the
look so any run reproduces the same board-grade artifact. Designed by Anna (ARA)
to match the target board-scorecard PDF.

---

## 1. Design tokens (single source of truth)

All tokens are declared once as CSS custom properties in `:root` of the template.
Edit them there to retheme globally; the values below are the canonical set.

### Color

| Token | Hex | Used for |
|---|---|---|
| `--navy` | `#0E2440` | Header band, all table header rows, section headings |
| `--navy-deep` | `#0A1B30` | Darker edge of the header-band gradient |
| `--gold` | `#C8A24B` | Accent rule under header; Section G hierarchy outline |
| `--ink` | `#1B2733` | Primary body text |
| `--muted` | `#5C6B7A` | Chip values, footer, secondary text |
| `--paper` | `#FFFFFF` | Page background |
| `--chip-bg` | `#F2F4F6` | Summary chips, hierarchy bar fill |
| `--chip-border` | `#DBE0E6` | Chip borders |
| `--zebra` | `#F5F7F9` | Even table rows |
| `--rule` | `#E2E7EC` | Hairline cell/section borders |
| `--header-text` | `#FFFFFF` | Text on navy |

### Semantic (tier / score / pane) colors

| Token | Hex | Meaning |
|---|---|---|
| `--green-bg` / `--green-fg` | `#D8EFD9` / `#1F6B2E` | TOP TIER · high Overall · Strengths pane |
| `--green-tint` | `#EAF6EB` | Section C best-row tint (faint) |
| `--amber-bg` / `--amber-fg` | `#FCEFC2` / `#8A6314` | MID · DEFENSIVE · PREMIUM tier chips |
| `--amber-soft` | `#FAF3D9` | Section A "MODELED PROJECT COST BAND" highlight row |
| `--red-bg` / `--red-fg` | `#F8D7DA` / `#A12A36` | HIGH RISK · low Overall · Risk-flags pane |

> Color choices are tuned for contrast on print (foreground text passes WCAG AA
> on its own fill) and to read calmly under board-room projection, not just on a
> bright screen.

### Typography

System font stack (no embedding, print-safe, matches the source's humanist sans):
`"Helvetica Neue", Helvetica, Arial, "Liberation Sans", sans-serif`.
If Falke/ARA licenses a brand face, replace only the first stack entry.

| Token | Size | Used for |
|---|---|---|
| `--fs-title` | 24pt | Header band project title |
| `--fs-subtitle` | 8.5pt | Header band right-aligned subtitle |
| `--fs-section` | 12pt | Section A–G headings |
| `--fs-chip-lbl` / `--fs-chip-val` | 8pt / 9.5pt | Summary chip label / value |
| `--fs-th` | 7.5pt | Table header cells |
| `--fs-td` | 8pt | Table body cells (base) |
| `--fs-small` | 7pt | Footer / provenance |

Weights: 600 for the title and table headers, 700 for section headings and
emphasis (subtotal / band rows, tier chips, score cells), 400 body.

### Spacing

| Token | Value | Used for |
|---|---|---|
| `--gap` | 16px | Gap between summary chips |
| `--pad-cell` | 5px 8px | Table cell padding |
| Section top margin | 18px | Space above each A–G section |
| Page margin (`@page`) | 0.5in top, 0.55in sides/bottom | Print safe area |

---

## 2. Page / print setup

- **Page size:** Landscape Letter `11in x 8.5in` (matches the wide source). Set in
  `@page { size: 11in 8.5in }`. Change here if a different stock is needed.
- **Backgrounds must print.** All color-coding is background fill. The template
  sets `print-color-adjust: exact`. In headless Chromium pass
  `printBackground: true`; WeasyPrint prints backgrounds by default.
- **Table page breaks:** `thead { display: table-header-group }` repeats the navy
  header row on each page when a long table spans pages; `tr { break-inside:
  avoid }` keeps rows intact. Contractor blocks and the hierarchy bar also use
  `break-inside: avoid`.
- **Recommended renderer:** headless Chromium (Playwright/Puppeteer) with
  `preferCSSPageSize: true`, or WeasyPrint. Both honor every primitive used.

---

## 3. Color-coding contract (logic-spec output → CSS class)

Christine maps the logic-spec classification onto these classes. The class
governs the fill; the visible label text is supplied separately, so MID /
DEFENSIVE / PREMIUM can share the amber chip while reading differently — exactly
as the source PDF does.

### Section B — tier chip (`tier_class`), per logic-spec §4.1

| Tier (logic spec) | $/SF rule | `tier_class` | Color |
|---|---|---|---|
| TOP TIER (aligned) | within band `184–195` | `tier-top` | green |
| MID (aggressive) | modestly below, `~166–184` | `tier-mid` | amber |
| DEFENSIVE (above) | above band, `195–~234` | `tier-defensive` | amber |
| PREMIUM (far above) | `> ~234` | `tier-premium` | amber |
| HIGH RISK (under) | far below, `< ~166` | `tier-risk` | red |

### Section E — Overall /100 cell (`overall_class`)

| Condition (recommended cut — parameterize) | `overall_class` | Color |
|---|---|---|
| Overall ≥ 75 | `score-high` | green |
| 60 ≤ Overall < 75 | `score-mid` | neutral (no fill) |
| Overall < 60 | `score-low` | red |

Reproduces the source (86/82/76 green; 70/64 neutral; 58/48 red). Expose the 75
and 60 cuts as parameters so Falke can tune without editing CSS.

### Section C — best-row tint (`row_class`)

`row-best` = faint green tint for the top-ranked rows (tints the top two, e.g.
Acme & Borealis); empty string = normal zebra row.

### Sections F (panes) — fixed by design

Strengths pane = green fill; Risk-flags pane = red/pink fill. No mapping needed.

---

## 4. Templating convention

Primary: **Jinja2** (`{{ var }}`, `{% for %}`, `{% if %}`). Render via
`jinja2.Template(...).render(**context)`. Every loop is *also* wrapped in
`<!-- REPEAT:name --> ... <!-- /REPEAT:name -->` comment markers so a plain
string-builder can clone blocks if a template engine is undesirable. Use one
approach, not both. The full `context` object (every variable and loop) is
documented in the header comment of `scorecard-template.html`.

---

## 5. Reuse / brand-asset note

Save a winning rendered PDF back as a reference exemplar so future runs and other
ARA work stay visually consistent. To rebrand for a different client, change only
the `:root` tokens (and optionally the font stack); structure and class contract
stay fixed. Original ARA design — no third-party artwork.
