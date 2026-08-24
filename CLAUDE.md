<!-- GSD:project-start source:PROJECT.md -->
## Project

**diagram-design OpenWebUI Tool**

A fork of `cathrynlavery/diagram-design` — an agent skill that guides LLMs to author 39 editorial
diagram types as self-contained HTML/SVG with an opinionated design system — that adds a bridge
into **OpenWebUI**. The addition is a single-file OpenWebUI Tool (`tools/diagram_design_tool.py`)
so any chat model running inside an OpenWebUI instance can produce validated, on-brand diagram
artifacts, instead of falling back to ad-hoc Mermaid output.

**Core Value:** An OpenWebUI chat model can call the tool to get design-system context, validate the diagram HTML
it authors against distilled lint checks, and export a working artifact file — without OpenWebUI
users needing Claude Code, Codex, or any other skill-capable agent runtime.

### Constraints

- **Language/runtime**: Python 3.11+, stdlib-first; tests are plain `main() -> int` scripts run
  directly via `python3` (no pytest — this repo has no pytest wiring anywhere; hyphenated
  `test-*.py` filenames don't match pytest's default discovery glob); no network access during
  tests — matches the existing `scripts/test-*.py` suite's conventions.
- **Dependency budget**: `tools/diagram_design_tool.py` must import with zero third-party
  dependencies. PNG export is the sole optional dependency (`playwright`), and its absence must
  degrade gracefully (HTML/SVG only + notice), never hard-fail.
- **File size**: keep the tool file under ~120 KB for OpenWebUI import practicality; the design
  digest it embeds must be generated deterministically by a committed build script
  (`scripts/build-owui-context.py`), never read from the repo at runtime.
- **Compatibility target**: OpenWebUI Tool API as of 0.6.x (`Tools` class, `__init__(self)`,
  `self.clients` / `self.user_valves` conventions, event-emitter first-arg call signature). The
  test suite mocks this API surface; a live-instance import into a real OpenWebUI instance is the
  release-gate UAT, documented but non-blocking for milestone completion.
- **No upstream regressions**: every `scripts/test-verify-*.py` (existing and new) must stay
  green, run the way this repo's CI actually runs them — direct `python3 scripts/test-X.py`
  invocation per script, not `pytest`.
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Recommended Stack
### Core Technologies
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|------------------|
| Python | 3.11+ (host determines actual interpreter; write compatible with 3.11–3.13) | Runtime | Matches repo constraint and OpenWebUI's own backend runtime (OWUI backend targets 3.11+ as of the 0.6.x line). No language-level 3.12+-only syntax should be used — OWUI's bundled interpreter version is admin-controlled and not guaranteed to be the newest. |
| stdlib `html.parser.HTMLParser` | stdlib | Structural HTML/SVG validation (`validate_diagram`) | Same tool the repo's own `scripts/lint-skin.py` already uses (`from html.parser import HTMLParser`) — reuse the proven pattern rather than introducing a second parsing strategy. Confirmed by reading `scripts/lint-skin.py` in this repo. HIGH confidence. |
| stdlib `re` | stdlib | Token/pattern checks (hex colors, `rgba()`, font-family, `@import`/`url()` egress checks) | Repo's `lint-skin.py` already encodes the exact regexes needed for token/skin checks (`HEX_RE`, `RGBA_RE`, `FONT_CSS_RE`, etc.) — port these directly rather than re-deriving them. HIGH confidence. |
| stdlib `json` | stdlib | Digest loading, brand-profile storage, structured `validate_diagram` findings | No alternative needed; OpenWebUI tool return values are typically `str`/`dict`/`list`, all JSON-native. HIGH confidence. |
| `pydantic` (BaseModel) | Whatever version ships inside the target OpenWebUI instance (OWUI 0.6.x backend pins **Pydantic v2**) | `Valves` / `UserValves` schema | This is **not an added dependency** — OpenWebUI's own backend already has pydantic v2 installed and imports every uploaded Tool module inside that same Python process. `from pydantic import BaseModel, Field` is safe and expected; every official example and every community tool defines `Valves`/`UserValves` this way. Do NOT vendor or pip-install pydantic yourself — assume host-provided. HIGH confidence (per official docs + every community example reviewed). |
### Supporting Libraries (optional / conditional)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `playwright` (sync or async API) | Whatever the OpenWebUI host has installed, if any — do not pin a version inside the tool file since you don't control the host's install | PNG rendering of exported HTML/SVG | **Only** inside `export_diagram(..., format="png", ...)`, guarded by `try: import playwright.sync_api \| except ImportError`. Never imported at module top-level — a top-level `import playwright` would make the *entire tool* fail to load in any OWUI instance that lacks it, which violates the zero-hard-dependency requirement. MEDIUM confidence on exact API shape (sync vs async — see note below), HIGH confidence on the guard placement being mandatory. |
| `urllib.request` (stdlib) | stdlib | Fetching `apply_brand_profile(url=...)` when a URL is passed instead of inline JSON | No `requests` dependency needed for a single GET of a JSON/token file; stdlib `urllib.request.urlopen` with a timeout is sufficient and keeps the zero-dependency budget intact. MEDIUM confidence — reasonable engineering default, not something OpenWebUI's docs prescribe either way. |
### Development / Test Tools
| Tool | Purpose | Notes |
|------|---------|-------|
| Repo's existing `Harness`/`main()`-returns-int pattern | Test runner for everything, including the new tool-contract suite | This is the repo's real, load-bearing test convention (see `scripts/test-verify-bubble.py`), invoked in CI via direct `python scripts/test-X.py`. **Decided: no pytest** — this repo has no pytest wiring anywhere (no `pytest.ini`, no CI invocation), and hyphenated `test-*.py` filenames don't match pytest's default `test_*.py` discovery glob (`pytest scripts/ -q` collects 0 tests, exit 5, against this repo as-is). New tests follow the existing plain-script shape for consistency — see `REQUIREMENTS.md` TEST-01 and `PROJECT.md` Key Decisions. |
## OpenWebUI Tool API — 0.6.x conventions (verified against official docs, 2026-08-24)
### Class shape
- Methods intended to be model-callable **must be `async def`** with full type hints and a
- Return values should be plain **strings** (or JSON-serializable structures serialized to a
- `self.valves = self.Valves()` in `__init__` is mandatory boilerplate if `Valves` is declared —
- `self.citation` and `self.file_handler` are real, functioning boolean flags on the `Tools`
### Injected special parameters (declare only the ones you use, as keyword params with defaults)
| Parameter | Type | Purpose | Use in this tool |
|---|---|---|---|
| `__event_emitter__` | `Callable[[dict], Awaitable[None]]` or `None` | Fire-and-forget UI events | `status` during validation/export; `files` after a successful export; `notification` for the playwright-missing graceful notice |
| `__event_call__` | `Callable[[dict], Awaitable[Any]]` or `None` | Blocking, interactive events (confirmation/input) | Not needed for this milestone — no interactive prompts in scope |
| `__user__` | `dict` (contains `id`, `email`, `valves` = a `UserValves` instance) | Per-user context | Read `__user__["valves"]` for user-level export-format preference; access via attribute (`__user__["valves"].preferred_format`), **not** `__user__["valves"]["preferred_format"]` — UserValves is a pydantic model, not subscriptable (documented gotcha, confirmed in official Valves docs) |
| `__files__` | `list` | Files attached to the current chat turn | Not needed — this tool's input is HTML text via a string parameter, not chat attachments |
| `__metadata__` | `dict` | Chat/session metadata | Not needed for this milestone |
| `__request__` | `starlette.Request` | Raw request (headers, auth) | Only relevant if you decide to call OWUI's own `/api/v1/files/` upload endpoint to get a "real" downloadable file id (see Event Emitter section) — **not required** for the PRD's chosen approach of writing to the OWUI data dir + returning path/content inline |
### Event emitter payloads (HIGH confidence, verified against current official docs)
# Progress / status — always pair a False with an eventual True, or the UI shimmer never stops
# Non-blocking toast for the "playwright missing" graceful-degradation notice
# Citation/source, if surfacing which design-system reference informed a brief
### File/artifact delivery — the real mechanism, and why the PRD's simpler fallback is the right default
## Community Tool Structure Patterns to Imitate
- **One `Tools` class per file**, `Valves`/`UserValves` declared as *nested* classes right above
- **Every public method starts with a `status` event and ends with a `done: True` status event**,
- **Defensive `if __event_emitter__:` guards before every emit call** — since the same class is
- **Valves hold configuration, not secrets-in-code** — file paths, size caps, feature toggles
## Validating HTML/SVG with stdlib only — realistic scope
| Check | Approach | Confidence |
|---|---|---|
| Token/skin conformance (allowed hex palette, `rgba()` opacity floors, banned pure-black `rgb(0,0,0)`, allowed `font-family` stack) | `re` patterns exactly like `HEX_RE`/`RGBA_RE`/`FONT_CSS_RE`/`BLACK_RGB_RE` in `lint-skin.py` — port directly | HIGH |
| No external network egress (`<script src="http://...">`, `@import url(http://...)`, `<link rel="stylesheet" href="http://...">`) | `re` patterns exactly like `SRC_HTTP_RE`/`IMPORT_HTTP_RE`/`URL_HTTP_RE`/`LINK_RE` in `lint-skin.py` — port directly | HIGH |
| Well-formedness / structural sanity (balanced tags, presence of required elements like `<svg role="img">`, `<title>`, `<desc>`) | Subclass `html.parser.HTMLParser`, override `handle_starttag`/`handle_endtag`, track a tag stack | HIGH — this is exactly what `HTMLParser` is designed for; do not reach for `xml.etree.ElementTree` for HTML since real-world HTML is not always well-formed XML |
| Contrast ratio floor (WCAG 2.x relative luminance formula) | Pure math on parsed hex/rgb values — `re` to extract colors already found by the token check, then compute relative luminance `L = 0.2126*R + 0.7152*G + 0.0722*B` (linearized channels) and contrast ratio `(L1+0.05)/(L2+0.05)`, threshold ≥4.5:1 for normal text / ≥3:1 for large text/graphical objects per WCAG 2.1 SC 1.4.3 / 1.4.11 | HIGH — this is a fixed, stable, well-documented public formula (unchanged across WCAG 2.0/2.1/2.2), not an API that could have silently changed; safe to state as fact without re-verification |
| Geometry sanity (no negative/NaN width-height, viewBox consistency, coordinates within declared viewBox bounds) | `re` to pull numeric attributes (`cx`, `cy`, `r`, `x`, `y`, `width`, `height`, `viewBox`), plain arithmetic checks | HIGH — same category of check the repo's own `verify-geometry.py`/`verify-bubble.py` already do for specific diagram types; this tool's version is a generic subset |
| A11y minimums (`<svg role="img">`, `aria-labelledby`/`aria-describedby` wired to real `<title>`/`<desc>` ids present in the doc, no empty `alt=""` on meaningful `<img>`) | `HTMLParser` tag/attr tracking + `re` for id cross-reference | HIGH — mirrors `scripts/test-lint-a11y.py`'s existing "accessible SVG contract" already enforced in this repo's CI; port the contract, don't reinvent it |
## Installation
# Runtime: none. tools/diagram_design_tool.py must import cleanly with zero
# `pip install` steps beyond what any OpenWebUI 0.6.x host already provides
# (pydantic v2, stdlib). Do not add a requirements.txt for the tool itself.
# Optional, host-side, only if the maintainer wants PNG export enabled on
# their own instance (documented in docs/owui-tool.md, never required):
# Dev/test only (repo-root, not shipped with the tool file):
## Alternatives Considered
| Recommended | Alternative | When to Use Alternative |
|---|---|---|
| stdlib `html.parser` for validation | `lxml` / `BeautifulSoup4` | Never for this tool — explicitly forbidden by the zero-third-party-dependency constraint, and unnecessary: `html.parser` + `re` already covers everything the repo's own `lint-skin.py` needs for the same class of checks |
| Optional `playwright` guarded by `try/except ImportError` | `wkhtmltoimage`/`imgkit`, headless Chrome via `subprocess` + system Chrome binary, `weasyprint` | Only if a target OWUI deployment already has one of these and specifically wants to avoid the Playwright browser-binary download (~300MB Chromium download on `playwright install`). Not recommended as the primary optional path — Playwright is what the repo's own `lint-render.py` already standardizes on (pinned `1.62.0` in CI), so reusing it keeps one browser-automation story in the whole repo instead of two |
| Data-dir disk write + inline string return as primary delivery | OWUI Files-API upload (`POST /api/v1/files/`) + `files` event as primary delivery | Only pursue this as a later enhancement once a maintainer confirms it's reliable on their specific OWUI version and is willing to accept the auth-token complexity (`__request__` header extraction or a valve-stored API key) — not worth it as the *primary* path given the reliability gap documented above |
| Plain `main() -> int` script, direct `python3` invocation | `pytest.ini` + thin `test_*` wrapper to make `pytest scripts/ -q` pass | **Rejected.** Adding pytest wiring would introduce a second, inconsistent test-running convention alongside ~30 existing plain-script tests, for no benefit — the repo's CI never invokes pytest today. Follow the existing convention instead (decided; see `REQUIREMENTS.md` TEST-01) |
## What NOT to Use
| Avoid | Why | Use Instead |
|---|---|---|
| Top-level `import playwright` (or any third-party import) at module scope in `tools/diagram_design_tool.py` | Breaks the file's ability to import cleanly on any OWUI host without that package — violates AC1 and the zero-dependency requirement outright, not just in spirit | `try: import playwright.sync_api as pw \| except ImportError: pw = None`, checked only inside `export_diagram`'s PNG branch |
| `requests` for the optional `apply_brand_profile(url=...)` fetch | Third-party dependency for a single GET — unnecessary | stdlib `urllib.request.urlopen(url, timeout=...)` |
| `pytest` for any test in this repo, or assuming `pytest scripts/ -q` passes on `main` | False — empirically verified exit code 5 ("no tests collected") against this repo, because hyphenated `test-*.py` filenames don't match pytest's default discovery glob; decided against adding pytest wiring to fix this (see Development / Test Tools above) | Plain `main() -> int` script, invoked directly via `python3 scripts/test-X.py`, matching every existing `scripts/test-verify-*.py` |
| `xml.etree.ElementTree` for parsing model-authored HTML | Real-world/model-generated HTML is not guaranteed well-formed XML (unescaped `&`, optional closing tags, etc.) — `ElementTree.fromstring` will raise on inputs `html.parser.HTMLParser` tolerates gracefully | `html.parser.HTMLParser` subclass, exactly as `scripts/lint-skin.py` already does |
| Relying on `__event_emitter__({"type": "files", ...})` as the *sole* delivery mechanism | Requires an authenticated round-trip to OWUI's own Files API to get a resolvable `file_id`/URL first (not just emitting the event with an arbitrary URL); documented frontend display bugs in some releases | Disk write under the valve-configured export dir + inline string return as primary; `files` event only as a best-effort, failure-tolerant enhancement |
## Stack Patterns by Variant
- Document `pip install playwright && playwright install chromium` as a manual host-side step in
- Because OWUI's own Playwright integration (used for its web-page-loader feature) runs as a
- All public methods must still return a fully useful string (content + written path) with no
- Because this is both a testability requirement (per PRD G4/AC1) and a real production case — the
## Version Compatibility
| Package/Surface | Compatible With | Notes |
|---|---|---|
| `Tools` class contract described here | OpenWebUI 0.6.x line | Docs and community examples reviewed are current as of research date; OWUI ships frequent point releases (0.6.41 referenced in a GitHub issue found during research) — re-verify the `Tools`/event-emitter contract against `docs.openwebui.com` if the maintainer's live-instance UAT (AC6) surfaces any signature mismatch |
| `pydantic` v2 (`BaseModel`, `Field`) | Host-provided by OWUI backend | Do not write v1-style `class Config:` — OWUI 0.6.x's backend is on pydantic v2; use `model_config = {...}` idiom only if config is ever needed (not expected for this tool) |
| `playwright` (optional) | Pin to whatever the repo's `lint-render.py` CI step already pins (`1.62.0` as of this repo's current CI config) if bundling install instructions, to keep one Playwright version story across the repo | Not a hard requirement — the tool's own code should not hard-pin a version since it never installs Playwright itself, only detects it |
## Sources
- https://docs.openwebui.com/features/extensibility/plugin/tools/development/ — Tools class shape, `__init__`, injected special parameters, async method requirement (fetched 2026-08-24)
- https://docs.openwebui.com/features/extensibility/plugin/development/valves/ — Valves/UserValves as pydantic BaseModel, `__user__["valves"]` non-subscriptable gotcha (fetched 2026-08-24)
- https://docs.openwebui.com/features/extensibility/plugin/development/events/ — full event-type payload reference (`status`, `files`, `citation`/`source`, `notification`, `confirmation`, `input`, `execute`), persistence table, `__event_emitter__` vs `__event_call__` distinction (fetched 2026-08-24)
- https://docs.openwebui.com/features/extensibility/plugin/tools/ — `self.citation`/`self.file_handler` context, tool result image handling (fetched 2026-08-24)
- https://github.com/open-webui/open-webui/discussions/11815 — real-world mechanism for uploading files from a tool call (`POST /api/v1/files/`, bearer token via `__request__` headers, known frontend display bugs), MEDIUM confidence (community discussion, not spec)
- https://github.com/open-webui/open-webui/discussions/7347 — Valves/UserValves scoping to Tools vs Functions
- https://github.com/open-webui/open-webui/issues/19806 — evidence OWUI's own Playwright integration is a separate `PLAYWRIGHT_WS_URI` microservice, not an in-process pip dependency
- https://github.com/open-webui/open-webui/blob/main/docker-compose.playwright.yaml — confirms the separate-Playwright-service architecture
- `/root/diagram-design/scripts/lint-skin.py` (read directly) — proof of the exact stdlib `html.parser`/`re` validation patterns already proven in this repo; ported patterns cited above are HIGH confidence because they are the repo's own working code, not external claims
- `/root/diagram-design/scripts/test-verify-bubble.py` (read directly) — proof of the `Harness`/`main()` test convention actually in use
- `/root/diagram-design/.github/workflows/ci.yml` (read directly) — proof every existing test script runs via direct `python script.py`, never `pytest`
- Empirical verification performed in this session: `pytest scripts/ -q` against the live repo checkout (pytest 9.0.3) → exit code 5, "no tests ran"; minimal reproduction confirming `python_files` glob mismatch is the root cause, and confirming a `pytest.ini` override + `def test_*()` function fixes it — HIGH confidence, directly reproduced, not sourced from documentation
- Community tool structure cross-reference: `Haervwe/open-webui-tools`, `MartianInGreen/OpenWebUI-Tools`, `pahautelman/open-webui-tool-skeleton` (via WebSearch summaries — MEDIUM confidence, not individually fetched line-by-line)
- WCAG 2.x contrast formula (relative luminance / contrast ratio) — stated from stable, unchanged public specification; not independently re-fetched this session but HIGH confidence as it is fixed math, not a version-dependent API
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
