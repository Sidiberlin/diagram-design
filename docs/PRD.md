# PRD — diagram-design OpenWebUI Tool

## Background

This repo is a public fork of `cathrynlavery/diagram-design` (MIT). The upstream project is an
agent skill (`skills/diagram-design/SKILL.md` + `references/` + `scripts/verify-*.py`) that guides
LLMs to author 39 editorial diagram types as self-contained HTML/SVG, with an opinionated design
system (style-guide tokens, density rules, lint/verify scripts).

The fork's addition: expose this capability to **OpenWebUI** as a Tool so any chat model in an
OpenWebUI instance can produce validated, branded diagram artifacts instead of ad-hoc Mermaid slop.

## Problem

OpenWebUI users have no bridge to this design system: model-authored diagrams drift from the
tokens, nothing validates the output, and there is no file-artifact delivery.

## Goals

1. **G1 — OpenWebUI Tool artifact**: a single importable Python file (`tools/diagram_design_tool.py`,
   class `Tools`, valves via class attributes) installable via OpenWebUI Admin → Tools → import,
   plus a `--user-valves`-friendly default configuration.
2. **G2 — Function-calling surface**: the tool exposes callable methods for:
   - `get_design_brief(diagram_type)` → returns the distilled design-system context for the
     requested type (loaded from a packaged, size-capped digest of `references/`)
   - `validate_diagram(html)` → runs distilled lint checks (skin tokens, geometry sanity,
     accessibility minimums) and returns structured findings the model can self-correct from
   - `export_diagram(html, format, name)` → writes standalone HTML (always), inline-SVG
     (when extractable), PNG (only when playwright is importable; otherwise a graceful notice)
   - `apply_brand_profile(tokens_json | url)` → store/update a brand profile used by validation
     (ports the `profile` command concept; valves hold the active profile path)
3. **G3 — Delivery**: exported files land under the OpenWebUI data dir (valve-configurable),
   surfaced via the event emitter as downloadable artifacts; when emitters are unavailable
   (tests/CLI), fall back to returning content + path inline.
4. **G4 — Tests**: extend the repo's `scripts/test-verify-*.py` convention with a
   `scripts/test-verify-owui-tool.py` suite: tool-contract checks (class shape, valve schema,
   emitter mock), validation true/false positives seeded from existing fixtures, export
   round-trips. Headless, offline, stdlib + pytest only.
5. **G5 — Docs**: `docs/owui-tool.md` — install, valve reference, example chat transcript,
   limitations; README fork note linking it.

## Non-goals

- No template engine for all 39 types — the chat model authors diagrams; the tool supplies
  context, validation, and export only.
- No OpenWebUI Tool Server / MCP endpoint (future work).
- No changes to upstream skill behavior (`skills/`, `commands/`, `prompts/` untouched except
  additive packaging helpers if required).

## Locked decisions

- **D1 — Artifact form**: OpenWebUI *Tool* (single-file `Tools` class), not a Tool Server or Pipe.
  Matches the request; zero infra beyond the import.
- **D2 — Reference packaging**: the design-system digest is generated at build time by a repo
  script (`scripts/build-owui-context.py`) into a JSON bundle the tool ships/embeds; the tool
  never reads the repo at runtime.
- **D3 — Validation scope**: port a distilled subset of `lint-skin`-style checks (token presence,
  contrast floor, font stack) + geometry sanity + a11y minimums. Pure stdlib (`html.parser`,
  `re`, `json`).
- **D4 — PNG export**: optional dependency (playwright). Absent → HTML/SVG only with explicit
  notice in the tool response. Never a hard failure.
- **D5 — Compatibility target**: OpenWebUI tool API as of 0.6.x (`Tools` class, `__init__(self)`,
  `self.clients`/`self.user_valves` conventions, event-emitter first-arg call signature). The
   suite mocks the API surface; live-instance import is the release-gate UAT (documented, non-blocking).

## Acceptance criteria

- **AC1**: `tools/diagram_design_tool.py` imports with zero third-party dependencies; contract
  test proves class/valve/method shape.
- **AC2**: `get_design_brief` returns non-empty, type-keyed context for all 39 upstream types.
- **AC3**: `validate_diagram` catches ≥90% of seeded defect fixtures and passes all clean
  fixtures (seeded from `scripts/fixtures/` where applicable).
- **AC4**: `export_diagram` produces a standalone HTML file (no external refs) in tests; PNG path
  is skipped-with-notice when playwright is absent.
- **AC5**: full suite green: `pytest scripts/ -q` including new tests; no regressions in the
  upstream verify suite.
- **AC6**: docs complete; manual live-import into an OpenWebUI instance listed as release-gate
  UAT for the maintainer (non-blocking for milestone completion).

## Constraints

- Python 3.11+ stdlib-first; pytest for tests; no network in tests.
- Keep the tool file under ~120 KB (OpenWebUI import practicality); digest generation must be
  deterministic and committed.
- All work on branch `owui-tool`; merge to `main` after quality gates.
