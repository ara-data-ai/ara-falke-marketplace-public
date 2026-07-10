# Falke Bid Tools (Cowork plugin) — PROTOTYPE, Phase 2 de-pathed

One plugin, two skills: **create-matrix** (contractor bid PDFs -> leveled
comparison matrix `.xlsx`) then **build-scorecard** (matrix -> board-ready
Falke-branded scorecard PDF + plain-English summary).

> **Status: de-pathed (Phase 2 complete), pending fresh-Cowork validation +
> go-live review.** The two skills and both Python engines are bundled here. The
> absolute home-dir paths and FALKE-tree references have been replaced with
> portable plugin conventions (`${CLAUDE_PLUGIN_ROOT}`,
> `${CLAUDE_PLUGIN_DATA}/venv`, the session upload dir, and a user-chosen
> `--out`); the matrix pipeline is arg-driven and a blank matrix template + a
> synthetic eval fixture are bundled. Project-agnostic: all engines, examples,
> tests, and docs use synthetic sample data — no client project data ships.
> Requires Cowork (sub-agents and hooks run only in the Cowork desktop app, not
> web chat). Still independently reviewed before any ship.

## Layout (verified against code.claude.com/docs/en/plugins-reference, 2026-05-30)

```
falke-bid-tools/
├── .claude-plugin/plugin.json    # manifest (name = falke-bid-tools, kebab-case)
├── skills/
│   ├── create-matrix/SKILL.md            # COPY of source skill (de-path target)
│   └── build-scorecard/SKILL.md + reference/ + eval/   # COPY of scorecard skill
├── agents/
│   └── bid-extractor.md          # per-PDF extraction agent (was inline in skill)
├── engines/                      # bundled Python, referenced via ${CLAUDE_PLUGIN_ROOT}
│   ├── matrix/    = the matrix engine {src,tests}
│   ├── scorecard/ = the scorecard engine {scorecard,config,templates,tests,examples,...}
│   └── requirements.txt          # UNION of both engines' runtime deps
├── hooks/hooks.json              # SessionStart -> scripts/bootstrap.sh
├── scripts/bootstrap.sh          # pip-install into ${CLAUDE_PLUGIN_DATA}/venv + chromium
└── assets/                       # (reserved; fonts are inlined in templates)
```

Per the plugin reference, installed plugins **cannot reference files outside
their own root** (`../` traversal is dropped from the cache) — which is exactly
why both engines are bundled in `engines/`, not referenced from the FALKE tree.

## Dependency bootstrap

`hooks/hooks.json` fires `scripts/bootstrap.sh` on `SessionStart`. The script
creates a persistent venv at `${CLAUDE_PLUGIN_DATA}/venv`, `pip install`s
`engines/requirements.txt`, then `playwright install chromium`. It reinstalls
only when `requirements.txt` changes (diff-stamp pattern). Skills then invoke
the engines with `"${CLAUDE_PLUGIN_DATA}/venv/bin/python"` and
`PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/engines/<engine>"`.

## PDF render fallback (switchable)

Chromium render is proven in principle (a local prototype: headless launch +
valid PDF). The open unknown is whether `playwright install chromium` (~150 MB +
system libs) succeeds in Cowork's Linux sandbox. bootstrap.sh handles failure
**gracefully**: if Chromium install fails it writes `${CLAUDE_PLUGIN_DATA}/render-mode`
= `html-only`; on success it writes `chromium`. The build-scorecard skill reads
that marker and passes `--html-only` when set, so a Chromium failure degrades to
HTML output instead of breaking the run. **Must be confirmed on a fresh Cowork
account** (see the handoff note, "Open prototype validation").
