# OpenWebUI Tool — `tools/diagram_design_tool.py`

Reference for the fork's OpenWebUI Tool: what the four model-callable methods do, how to install
the file, the three admin valves, and a worked transcript generated from the real tool. Project
background lives in `docs/PRD.md`; the release-time checklist is the UAT table further down.

## What it does

`get_design_brief(diagram_type)` serves the design-system brief for one diagram type as markdown —
that type's layout conventions, anti-patterns and example note, followed by the shared style-guide
token, typography and spacing tables. `diagram_type` is one of the 40 upstream keys, e.g.
`"sankey"`, `"flowchart"`, `"treemap"`, `"waterfall"`; an unknown key returns an error message
listing every valid key.

`validate_diagram(html)` checks authored markup against the design system and returns a markdown
report whose first line is the verdict: `PASS: 0 issues` or `FAIL: N issue(s)`. Four check
families are covered — skin tokens (palette hex/rgba, pure black, font stack), geometry (viewBox,
negative sizes, bounds), accessibility (`<svg role="img">`, `<title>`/`<desc>`, aria wiring) and
network egress (external `src`/`href`, `@import`, `url()` and the script policy). Findings are
grouped under family headings, capped at eight per family, and a FAIL ends with a retry line. Pass
the markup itself: a full `<html>…</html>` document or a bare `<svg>…</svg>` fragment both work.

`export_diagram(html, format, name)` writes one artifact per call in one of three tiers: `html`
(the default — markup written byte-identically as a standalone page, with LF line endings on
every platform), `svg` (the root
`<svg>…</svg>` element verbatim) and `png` (rendered in a headless browser, which needs playwright
on the host; without it the call still answers, with a notice and nothing written). `name` becomes
the filename and is reduced to a strict slug of `a-z0-9-`, so it cannot escape the export
directory. Export writes the markup as authored, so call validate_diagram first and fix what it
reports.

`apply_brand_profile(tokens_json, url)` stores a brand token profile for the calling user from a
JSON object mapping any of the ten palette roles (`paper`, `paper-2`, `ink`, `muted`, `soft`,
`rule`, `rule-solid`, `accent`, `accent-tint`, `link`) to hex values; after a store,
validate_diagram checks that user's diagrams against the stored palette instead of the style-guide
default, so brand colours pass and off-brand colours still fail. Call it with no arguments to read
the active profile, or with `tokens_json` set to `"{}"` to clear it and return to the default
palette. A payload with an unknown role or a malformed hex is rejected whole. The `url=` form
fetches a token file instead of taking inline JSON: only https is fetched, redirects are refused,
the response is capped at 256 KiB, and a host resolving to a non-public address is refused before
anything is opened.

The intended loop is closed and self-correcting: brief → author → validate (FAIL) → fix →
validate (PASS) → export.

## Self-contained briefs

Every brief is complete in itself: `get_design_brief` returns *all* of a type's layout
conventions and anti-patterns, and nothing in any tool output points at a repository file. The
chat model consuming the tool has no filesystem, so a path reference would be an instruction it
cannot follow — an earlier digest build capped each list and ended the cut with "see
`skills/diagram-design/references/type-X.md`", and on a live instance the model dutifully
reported "the reference files are not available". The build now ships the full lists, and the
trailer-policy gate in `scripts/test-verify-owui-context.py` refuses any repo path re-entering
the digest.

## Install

Import `tools/diagram_design_tool.py` into an OpenWebUI 0.6.x instance as a single file through
the admin Tools surface. The file imports with zero third-party dependencies: everything it needs
beyond the standard library is pydantic, which is already present in the OpenWebUI backend process
that loads tools. There is no `requirements.txt` to add, and the file's frontmatter deliberately
carries no `requirements:` key — a frontmatter dependency triggers a blocking, non-surviving
in-process pip install, which is why PNG export is a manual host or image step instead:

```
pip install "playwright==1.62.0" && playwright install --with-deps chromium
```

`1.62.0` is the same pin this repository's own CI uses (`PLAYWRIGHT_VERSION` in
`.github/workflows/ci.yml`), so the repo and this doc tell one story. Without playwright the tool
still loads and every html and svg tier still works; only the png tier degrades, to a notice.

Import is an administrator action. Upstream warns that Workspace Tools execute arbitrary Python
with the server's privileges and should be imported by administrators only; where install
permission matters, `USER_PERMISSIONS_WORKSPACE_TOOLS_IMPORT` gates it. The exact click-path for
importing a `.py` file varies by release and is not documented upstream, so it is deliberately not
described here — confirming it on a live instance is release-gate item UAT-1.

Two flags set in `__init__` are worth knowing. `self.citation` is set but is inert in current
OpenWebUI releases — nothing acts on it any more — so no citation appears in chat.
`self.file_handler` is set so the host knows the tool produces files.

## Valve reference

All three valves are admin-level (`Valves`). `UserValves` is deliberately empty: no per-user
configuration is planned. Valves are read at call time, never cached in `__init__`, so a value
change takes effect on the next call without reloading the tool.

### `export_dir` — where artifacts are written

Type `str`, default `exports`. The only filesystem destination export may use. A relative value
resolves against the OpenWebUI data dir (`DATA_DIR`) when that is resolvable, else against the
working directory; an absolute value is used as-is. The directory is created on first export.
Example: `/data/open-webui/exports`.

### `max_export_bytes` — the accepted markup cap

Type `int`, default `2_000_000`. Caps the accepted `html` input in bytes before any parse or I/O;
larger markup returns an error envelope instead of being written to disk. Example: `4000000`.

### `profiles_dir` — where brand profiles live

Type `str`, default `brand-profiles`. The only filesystem destination the brand-profile store may
use, one JSON file per user keyed by the slugged user id. Same resolution rule as `export_dir`:
absolute used as-is, else joined to `DATA_DIR`, else the working directory; created on first
store. Example: `/data/open-webui/brand-profiles`.

A stored profile changes validation verdicts: a diagram that passes under the default palette can
legitimately fail under a stored profile, and the verdict's first line then names that profile.

## Worked transcript

Generated by loading `tools/diagram_design_tool.py` the way OpenWebUI does — file text `exec()`ed
into a non-`__main__` namespace, both directory valves pointed at temp paths — and then driving
the loop below. Every block is verbatim tool output, with one deliberate exception stated once
here: the `get_design_brief` body is shown as a real prefix ending at a section boundary followed
by an explicit marker, because the full brief is 8,092 characters; regenerate it the way
`scripts/test-verify-owui-docs.py`'s set-J check does — its `_load_tool()` helper exec-loads the
tool — by printing `get_design_brief("sankey")`. User id and paths are the sanitized test values
(`doc-author`, `/tmp/p6-transcript/...`), never a real account or a real host path.

### 1. `get_design_brief("sankey")` — the brief (prefix)

```text
# Sankey / Flow-Quantity

**Best for:** showing where a *quantity* goes as it splits and merges across a small number of stages — CI compute budgets, funnel-adjacent volume flows, cost or headcount allocation. This is the one type where band **thickness carries data**; if the reader doesn't need to compare magnitudes, use process or pyramid instead.

… elided — 8,092 chars total, regenerate via scripts/test-verify-owui-docs.py's set-J recipe (_load_tool + get_design_brief("sankey"))
```

### 2. `get_design_brief("not-a-type")` — the failure path

```text
Error: unknown diagram_type 'not-a-type'. Valid types: architecture, bar, data-flow, db-schema, dependency, deployment, dp-integration, dp-security-matrix, er, fishbone, flowchart, gantt, high-level, it-state, journey, kanban, layers, line, loop, medallion, nested, org-chart, polar, process, pyramid, quadrant, radar, sankey, scatter, sequence, state, story-map, swimlane, timeline, tree, treemap, uml-class, venn, wardley, waterfall.
```

### 3. `validate_diagram(...)` on markup with an off-palette hex — FAIL

```text
FAIL: 1 issue(s)

## Skin tokens
- **error** (line 15): #cc3388 is not in the style-guide palette

Fix the issues above and call validate_diagram again with the corrected HTML.
```

### 4. Fix, then `validate_diagram(...)` again — PASS

Replacing `#cc3388` with a palette hex (`#2e5aa8`) clears the finding:

```text
PASS: 0 issues

No design-system issues found — the diagram is ready to export.
```

### 5. `export_diagram(..., format="html", name="q3-flow")` — the html tier

```text
Exported q3-flow.html (759 bytes) to /tmp/p6-transcript/out.

- html: /tmp/p6-transcript/out/q3-flow.html (written, 759 bytes)
- svg: extractable — call again with format="svg" for a standalone .svg file
- png: not requested — call again with format="png" to render one
```

### 6. `export_diagram(..., format="png", name="q3-flow")` — png without playwright

```text
PNG export unavailable — playwright is not installed on this host (admin step: pip install playwright && playwright install chromium)
Nothing was written.

- html: not written — call again with format="html" for a standalone file
- svg: extractable — call again with format="svg" for a standalone .svg file
- png: unavailable — playwright is not installed on this host (admin step: pip install playwright && playwright install chromium)
```

### 7. `apply_brand_profile(tokens_json='{"ink":"#1a1a1a","accent":"#0ea5e9"}', __user__={"id": "doc-author"})` — store

```text
## Brand profile stored to `/tmp/p6-transcript/prof/doc-author.json`

- 2 role(s): ink `#1a1a1a`, accent `#0ea5e9`
- validate_diagram now checks against this palette; off-palette hex still fails and pure #000000 stays banned.
```

### 8. `validate_diagram(..., __user__={"id": "doc-author"})` — the same clean diagram, branded

Note the `__user__` argument on the call below. Profile application is per-user, so a reader who
reproduces this call *without* it sees `PASS: 0 issues` instead — the profile is simply not theirs.

```text
FAIL: 3 issue(s) — brand profile: doc-author

## Skin tokens
- **error** (line 15): #2e5aa8 is not in the style-guide palette
- **error** (line 15): #0a0a0a is not in the style-guide palette
- **error** (line 16): rgba(46, 90, 168, 0.35) is not derived from an allowed palette color

Fix the issues above and call validate_diagram again with the corrected HTML.
```

### 9. `apply_brand_profile(tokens_json="{}", __user__={"id": "doc-author"})` — clear

```text
## Brand profile cleared

- removed `doc-author.json` from the profiles_dir valve's directory.
- validate_diagram is back on the default style-guide palette.
```

## Limitations

**The `files` event's download UX is a release-gate item, not a proven path.** The event's
envelope and its persistence semantics are documented upstream: a `{type: "files", data: {files:
[...]}}` emit is merged into the message's `files` array, newest first, and persisted, and only the
short `"files"` type persists (`"chat:message:files"` is a non-persisted frontend alias). What
upstream does not document is the File Object shape — the official docs leave a placeholder
comment where the example belongs — or the registration and download path a client follows. The
payload this tool emits per written file, `{"type": "file", "name": <filename>, "url": <path>}`, is
therefore an unverified guess at that shape. That is release-gate item UAT-7, and no unit test can
close it out: the chip-and-download behaviour is only observable on a live OpenWebUI instance, and
CI is offline by design. The export report naming the written path is the guaranteed delivery
either way.

**Contrast-ratio checking is deferred.** `validate_diagram` does not compute WCAG contrast ratios.
It checks palette membership, the rgba opacity floors, pure black and the font stack; a contrast
floor across a chosen palette is a v2 candidate. Do not describe validate_diagram as a contrast
gate.

**PNG export without playwright writes nothing.** The call still answers — a report-shaped notice,
never an `Error:` envelope — and nothing is written (transcript block 6 above). The playwright
install stays a manual host or image step; the tool carries no frontmatter dependency for it.

**The pydantic guard is a dumb attribute bag, not a validator.** In a bare interpreter (this
repo's CI) the tool binds stand-in `BaseModel`/`Field` objects that perform no validation. That is
what keeps the file loadable with zero third-party dependencies, and it holds only if tool code
never calls a pydantic-v2-only API: no `model_dump`, no `model_validate`, no field introspection.
Any such call would work on an OpenWebUI host and break the bare-environment load, so it is a hard
constraint on future contributors.

**Two `url=` residuals are documented, not fixed.** WR-04: the public-address predicate does not
refuse NAT64-prefixed IPv6 literals (`64:ff9b::/96`) encoding an arbitrary IPv4 target, so on a
network with a NAT64 gateway a crafted token URL can reach internal IPv4 space. WR-07: the
transport-error mapping is incomplete — a transport failure outside `HTTPError`/`URLError`/
`OSError` surfaces as the generic internal-failure envelope — and `https://host:0/` is
resolution-checked against port 443 while the fetch connects to port 0. Both are recorded in
`.planning/phases/05-brand-profile-cross-call-state/05-REVIEW.md`; this phase changes no tool
code, so they are documented rather than patched.

**The SSRF gate has an accepted TOCTOU residual.** The gate resolves the host, checks the resolved
address, and then lets urllib resolve again on connect. A DNS rebinding between those two
resolutions is not prevented; pinning the resolved address needs a custom opener and was accepted
as out of scope in Phase 5 (A4 / T-5-SSRF8).

**Upstream 2.6.x additions — what the tool serves and what it does not.** Upstream now ships
Excalidraw import (`skills/diagram-design/scripts/excalidraw_extract.py` +
`references/import-excalidraw.md`), an export-block registry
(`references/export-registry.md`) and CJK (Korean / Traditional Chinese) label guidance. Of these,
the OpenWebUI tool serves the 40-type briefs, the token/typography/spacing tables and validator
acceptance of the new CJK font families. Excalidraw import and the export-block registry remain
skill-host capabilities: both need the agent-skill runtime the tool does not have, and the tool
offers no method for them.

## Release-gate UAT checklist

The nine live-instance items this fork could not close offline. CI is offline by design, so none
of them is unit-provable; each stays `pending` until it has been run once against a real OpenWebUI
0.6.x instance.

| id | Behavior under test | Verification step | Status |
| --- | --- | --- | --- |
| UAT-1 | Live single-file import of the tool | Import `tools/diagram_design_tool.py` through the admin Tools surface on a live instance; confirm it loads with exactly the four callable tools and no error | pending |
| UAT-2 | A model discovers and calls `get_design_brief` | In a chat, ask for a sankey brief; confirm the model calls `get_design_brief("sankey")` and the rendered markdown brief appears | pending |
| UAT-3 | The failure path surfaces the readable error envelope | Ask for a nonsense type; confirm the `Error: unknown diagram_type ...` message lists every valid key, with no traceback | pending |
| UAT-4 | Live model-in-the-loop FAIL → fix → PASS validation | Ask a model to author and check a diagram; confirm it calls `validate_diagram`, fixes the findings it is given, and re-validates to PASS | pending |
| UAT-5 | FAIL-report readability in the chat surface | Trigger a report with more than eight findings in one family; confirm it renders as bounded, readable markdown rather than a wall of noise | pending |
| UAT-6 | Live Chromium PNG render | On a host with playwright and chromium installed, export `format="png"`; confirm a real `.png` lands and the report names it with a byte count | pending |
| UAT-7 | `files`-event download UX in a live chat | Export in a chat; confirm the file chip appears and downloads, and record the payload shape this OWUI release actually accepts | pending |
| UAT-8 | Real `url=` fetch against an https endpoint | `apply_brand_profile(url="https://<endpoint serving the role JSON>")` from an online host; confirm a store report naming the roles and a subsequent branded validation | pending |
| UAT-9 | Live `__user__` per-user isolation | As two different users on one instance, each store a distinct profile; confirm two isolated `<slug>.json` files and isolated verdicts | pending |

The `.planning/phases/0{2,3,4,5}-*/*-HUMAN-UAT.md` files remain the audit trail for these rows;
this table is the maintainer's working checklist at release time (D-06), updated in place as rows
close.

## Fork maintenance

Three committed artifacts are generated, and four gates catch drift between them and their
sources. Regenerate with:

```
python3 scripts/build-owui-context.py         # the embedded design digest
python3 scripts/build-validation-fixtures.py  # the validation fixture corpus
python3 scripts/bump-plugin-version.py        # plugin manifests, patch bump only
```

- **Digest and tool-file parity:** `python3 scripts/build-owui-context.py`, then
  `git diff --ignore-space-at-eol --exit-code -- scripts/owui-context-digest.json
  tools/diagram_design_tool.py`. That byte-diff runs on all six matrix legs and is the gate that
  carries the zlib+base85 embedded blob, so a platform encoder difference would surface here.
  `scripts/test-verify-owui-context.py` re-derives the digest from its markdown sources as a
  second opinion.
- **Fixture bytes:** `python3 scripts/build-validation-fixtures.py`, then
  `git diff --ignore-space-at-eol --exit-code -- scripts/fixtures/validation/`, pinned to the
  ubuntu + Python 3.12 leg as belt-and-braces. The corpus is plain LF text with no compression, so
  one platform is enough to own the byte oracle; the other legs still run the full suites.
- **This doc:** `python3 scripts/test-verify-owui-docs.py`, the docs-parity gate added alongside
  it. It asserts the verbatim envelopes above, the valve defaults, the security claims and the
  nine-row UAT table, so an edit that stops the doc quoting real tool output fails CI.
- **Palette drift:** `python3 scripts/lint-skin.py --all --baseline`, the D-07 parity test that
  owns the style-guide palette the tool distils.

Plugin manifests (`.claude-plugin`, `.codex-plugin`, `.factory-plugin`) are bumped only with
`python3 scripts/bump-plugin-version.py`. A patch bump keeps `verify-plugin-package.py` green and
leaves `SKILL.md`'s `metadata.version: "2.6"` valid; never hand-edit the manifests.

Two CI legs stay CI-only: `lint-render.py --self-test` and `lint-render.py --all` need playwright
and run only on the ubuntu/3.12 leg. Locally those two commands fail with
`ModuleNotFoundError: No module named 'playwright'` and nothing else does.

