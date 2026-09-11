#!/usr/bin/env python3
"""Docs-parity gate for docs/owui-tool.md.

Proves the maintainer reference keeps saying what tools/diagram_design_tool.py
actually says (D-04) and keeps the claims a reader must not lose (D-03,
DOC-02): every envelope and verdict line the worked transcript shows, the
three admin valve defaults, the four model-callable method names, the
security-relevant claims (SSRF refusal, fetch cap, export cap, slug-only
filename rule), the seven-section shape with the nine release-gate UAT rows,
and the README fork note that points at this doc (DOC-03).

Assertions are substring/structure checks over file text only — never a
full-output diff — so rewording surrounding prose cannot red a build while
dropping a quoted envelope always does. Text the doc must never carry is
asserted negatively: a `requirements: playwright` recommendation (research
Pitfall 8) and a promised citation chip (Pitfall 7 — `self.citation` is inert
in current releases).

Assertion set I (README parity) is EXPECTED to stay red until 06-03 Task 1
lands the README fork note. This gate is authored contract-first (06-02
Task 1), before docs/owui-tool.md exists and before the README links to it,
so between 06-02 and 06-03 the shared owui_digest step reports exactly the
two named README FAIL lines below. That single-assertion red is accepted and
is closed by 06-03, not a defect in this gate.

Set J is the doc-to-behavior half the grounding check cannot be: it exec-loads
the tool, reads the digest the tool actually embeds, and asserts every
diagram_type example the doc names is a key that digest really has. Source-text
grounding (step 1 / set A) is structurally blind to a false example that
originates in the tool's own prose, so only the embedded key space — the thing
get_design_brief actually dispatches on — can catch that drift class. Set J's
scope is deliberately narrow: the paragraph carrying the diagram_type example
sentence, and nothing else, because quoted tokens elsewhere in the doc (event
names, the transcript's `not-a-type` probe) are not diagram_type examples.

Usage: python3 scripts/test-verify-owui-docs.py
Exit: 0 all pass, 1 a case failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "owui-tool.md"
README = ROOT / "README.md"
TOOL_SOURCE = ROOT / "tools" / "diagram_design_tool.py"

# Set A — verbatim tool output the worked transcript must contain. Every
# string is single-sourced in tools/diagram_design_tool.py (checked below), so
# the doc can only satisfy this set by quoting real tool behavior.
EXPECTED_SUBSTRINGS = [
    "PASS: 0 issues",
    "No design-system issues found — the diagram is ready to export.",
    "Fix the issues above and call validate_diagram again with the corrected HTML.",
    "png: not requested",
    "PNG export unavailable",
    "Error: unknown diagram_type",
    "unavailable — playwright is not installed on this host",
]

# Set B — the doc must show a real FAIL verdict, not only the PASS leg.
FAIL_VERDICT_RE = r"^FAIL: \d+ issue\(s\)"

# Set C — the three admin valve defaults, verbatim as the tool declares them.
VALVE_DEFAULTS = ["exports", "2_000_000", "brand-profiles"]

# Set D — all four model-callable methods the doc documents.
METHOD_NAMES = [
    "get_design_brief",
    "validate_diagram",
    "export_diagram",
    "apply_brand_profile",
]

# Set E — security-relevant claims the doc must keep (each is real tool text;
# silently weakening either side of these is what T-6-09 exists to catch).
SECURITY_CLAIMS = [
    "non-public address",
    "256 KiB",
    "2_000_000",
    "a-z0-9-",
]

# Set J — every diagram_type example the doc names must be a key the tool
# actually accepts. Anchored to the one sentence that declares the key space,
# so quoted tokens elsewhere in the doc (event names, the `not-a-type` probe)
# can never be mistaken for examples. The keys are read from the digest the
# tool embeds, never hardcoded here and never grounded against tool prose: a
# false example that originates in the tool's own docstring is exactly what
# source-text grounding cannot see.
DIAGRAM_TYPE_ANCHOR = "is one of the 40 upstream keys"
EXAMPLE_KEY_RE = r'"([a-z][a-z0-9-]*)"'
MIN_EXAMPLE_KEYS = 2

# Set H — section shape: seven `## ` sections (D-03) and the nine release-gate
# UAT rows (D-06), each row carrying its own topic token so an id cannot be
# reused for the wrong behavior.
MIN_LEVEL2_HEADINGS = 7
UAT_ROW_TOPICS = {
    1: "import",
    2: "get_design_brief",
    3: "unknown diagram_type",
    4: "validate_diagram",
    5: "readability",
    6: "playwright",
    7: "files",
    8: "url=",
    9: "__user__",
}

# Set F and set G — text the doc must never carry, and the one word it must.
BANNED_PATTERNS = [
    (
        r"requirements:\s*playwright",
        "recommends `requirements:` frontmatter for playwright (Pitfall 8: an "
        "in-process pip install that blocks the UI and does not survive restarts)",
    ),
    (
        r"citation chip",
        "promises a citation chip (Pitfall 7: self.citation is inert in current "
        "releases)",
    ),
]
REQUIRED_WORDS = ["inert"]


def contract_strings() -> list[str]:
    """Every string the doc is required to quote, deduplicated, order kept."""
    return list(
        dict.fromkeys(EXPECTED_SUBSTRINGS + VALVE_DEFAULTS + METHOD_NAMES + SECURITY_CLAIMS)
    )


def _load_tool() -> dict:
    """Load the tool file the way OpenWebUI does: as text, through exec().

    Mirrors scripts/test-verify-owui-tool.py's load_tool_source(): a
    non-`__main__` namespace so unguarded top-level code cannot run, `__file__`
    pointing at the real path as a debugging aid. `sys.dont_write_bytecode` is
    set first so a test run never leaves `__pycache__` in the tree. A plain
    `import tools.diagram_design_tool` would miss both the frontmatter contract
    and the top-level-statement failures this load surfaces.
    """
    sys.dont_write_bytecode = True
    source = TOOL_SOURCE.read_text(encoding="utf-8")
    namespace: dict = {"__name__": "tool_diagram_design_docs", "__file__": str(TOOL_SOURCE)}
    exec(compile(source, str(TOOL_SOURCE), "exec"), namespace)
    return namespace


def _diagram_type_keys() -> set[str]:
    """The diagram_type key space the tool actually dispatches on.

    `_DESIGN_DIGEST` is a class attribute (declared outside `__init__`), so it
    is read off `Tools` without instantiating it — no pydantic, no valves, no
    event emitter. This is behavior, not prose: it is the mapping
    `get_design_brief` looks types up in.
    """
    namespace = _load_tool()
    return set(namespace["Tools"]._DESIGN_DIGEST["types"])


def _doc_example_region(text: str) -> str:
    """The paragraph carrying the diagram_type example sentence, or ''.

    Scoped to that paragraph only: elsewhere in the doc, quoted backticked
    tokens name event types and the transcript's deliberate unknown-type probe,
    and none of those is a diagram_type example. Losing the anchor therefore
    disarms the check, which main() reports as its own failure rather than
    letting the gate pass vacuously.
    """
    anchor_at = text.find(DIAGRAM_TYPE_ANCHOR)
    if anchor_at < 0:
        return ""
    line_start = text.rfind("\n", 0, anchor_at) + 1
    paragraph_end = text.find("\n\n", anchor_at)
    if paragraph_end < 0:
        paragraph_end = len(text)
    return text[line_start:paragraph_end]


def main() -> int:
    failures: list[str] = []

    # 1. The contract's own grounding: each asserted string must still exist in
    #    the tool source, so a tool-side edit that drops a claim reds this gate
    #    instead of leaving the doc quoting behavior that no longer exists.
    tool_text = TOOL_SOURCE.read_text(encoding="utf-8") if TOOL_SOURCE.is_file() else ""
    stale = [s for s in contract_strings() if s not in tool_text]
    if not TOOL_SOURCE.is_file():
        failures.append("tools/diagram_design_tool.py is missing — the contract has no source")
    elif stale:
        for needle in stale:
            failures.append(
                f"tools/diagram_design_tool.py no longer contains {needle!r} — "
                "re-derive this gate's contract from the tool before trusting the doc check"
            )
    else:
        print("OK: every asserted string still exists in tools/diagram_design_tool.py")

    # 2. Sets A-G over the doc body.
    text = ""
    if not DOC.is_file():
        failures.append(
            "docs/owui-tool.md does not exist yet — this gate is authored "
            "contract-first and stays red until the doc lands"
        )
    else:
        text = DOC.read_text(encoding="utf-8")

    if text:
        for needle in EXPECTED_SUBSTRINGS:
            if needle not in text:
                failures.append(f"docs/owui-tool.md is missing verbatim tool output {needle!r}")
        if len([n for n in EXPECTED_SUBSTRINGS if n in text]) == len(EXPECTED_SUBSTRINGS):
            print("OK: transcript quotes the tool's real output verbatim (set A)")

        if re.search(FAIL_VERDICT_RE, text, re.MULTILINE) is None:
            failures.append(
                "docs/owui-tool.md shows no real FAIL verdict "
                f"(expected a line matching /{FAIL_VERDICT_RE}/)"
            )
        else:
            print("OK: transcript shows a real FAIL verdict line (set B)")

        for needle in VALVE_DEFAULTS:
            if needle not in text:
                failures.append(f"docs/owui-tool.md is missing valve default {needle!r}")
        if all(n in text for n in VALVE_DEFAULTS):
            print("OK: valve defaults exports / 2_000_000 / brand-profiles stated (set C)")

        for needle in METHOD_NAMES:
            if needle not in text:
                failures.append(f"docs/owui-tool.md does not document {needle!r}")
        if all(n in text for n in METHOD_NAMES):
            print("OK: all four model-callable methods documented (set D)")

        for needle in SECURITY_CLAIMS:
            if needle not in text:
                failures.append(f"docs/owui-tool.md is missing security claim {needle!r}")
        if all(n in text for n in SECURITY_CLAIMS):
            print("OK: security claims (refusal, caps, slug rule) present (set E)")

        for pattern, reason in BANNED_PATTERNS:
            if re.search(pattern, text) is not None:
                failures.append(f"docs/owui-tool.md {reason} (matched /{pattern}/)")
        if all(re.search(p, text) is None for p, _reason in BANNED_PATTERNS):
            print("OK: no requirements: frontmatter advice and no citation-chip claim (set F)")
        for word in REQUIRED_WORDS:
            if word not in text:
                failures.append(
                    f"docs/owui-tool.md never says {word!r} — the doc must state that "
                    "self.citation is inert rather than omit the flag's story"
                )
        if all(w in text for w in REQUIRED_WORDS):
            print("OK: self.citation described as inert (set G)")

        # 3. Set H — section shape.
        headings = len(re.findall(r"^## ", text, re.MULTILINE))
        if headings < MIN_LEVEL2_HEADINGS:
            failures.append(
                f"docs/owui-tool.md has {headings} '## ' headings, expected at least "
                f"{MIN_LEVEL2_HEADINGS} (D-03's seven-section reference)"
            )
        else:
            print(f"OK: {headings} '## ' sections (set H)")

        rows = re.findall(r"^\|\s*UAT-\d+\s*\|.*$", text, re.MULTILINE)
        if len(rows) != len(UAT_ROW_TOPICS):
            failures.append(
                f"docs/owui-tool.md UAT table has {len(rows)} rows, expected exactly "
                f"{len(UAT_ROW_TOPICS)} (UAT-1..UAT-9)"
            )
        for index in sorted(UAT_ROW_TOPICS):
            topic = UAT_ROW_TOPICS[index]
            row = next(
                (r for r in rows if re.match(rf"^\|\s*UAT-{index}\s*\|", r)), None
            )
            if row is None:
                failures.append(f"docs/owui-tool.md UAT table has no UAT-{index} row")
            elif topic not in row:
                failures.append(
                    f"docs/owui-tool.md UAT-{index} row does not mention {topic!r}"
                )
        if len(rows) == len(UAT_ROW_TOPICS) and all(
            any(
                re.match(rf"^\|\s*UAT-{index}\s*\|", row) and UAT_ROW_TOPICS[index] in row
                for row in rows
            )
            for index in UAT_ROW_TOPICS
        ):
            print("OK: nine release-gate UAT rows present with their topic tokens (set H)")

        # 3b. Set J — every diagram_type example the doc names must be a key the
        #     tool accepts. The keys come from the digest the tool embeds, so this
        #     is doc-to-behavior; source-text grounding cannot see a false example
        #     that originates in the tool's own prose.
        try:
            digest_keys = _diagram_type_keys()
        except Exception as exc:  # the gate reports, it never tracebacks out of main()
            failures.append(
                "could not read the tool's embedded digest "
                f"({type(exc).__name__}: {exc}) — set J has no behavior to check the doc against"
            )
        else:
            region = _doc_example_region(text)
            if not region:
                failures.append(
                    "docs/owui-tool.md names no diagram_type examples — the sentence carrying "
                    f"{DIAGRAM_TYPE_ANCHOR!r} could not be located, so set J is disarmed rather "
                    "than passing vacuously"
                )
            else:
                examples = re.findall(EXAMPLE_KEY_RE, region)
                if len(examples) < MIN_EXAMPLE_KEYS:
                    failures.append(
                        f"docs/owui-tool.md names {len(examples)} diagram_type example(s) in its "
                        f"example sentence, expected at least {MIN_EXAMPLE_KEYS} — keep the "
                        "example list populated or set J is disarmed"
                    )
                else:
                    unknown = [key for key in examples if key not in digest_keys]
                    for key in unknown:
                        failures.append(
                            f"docs/owui-tool.md names {key!r} as a diagram_type example, but the "
                            f"tool's embedded digest has no such key ({len(digest_keys)} types) "
                            "— drive get_design_brief to confirm"
                        )
                    if not unknown:
                        print(
                            f"OK: all {len(examples)} diagram_type examples the doc names are "
                            "real digest keys (set J)"
                        )

    # 4. Set I — README parity (DOC-03). Red by design until 06-03 Task 1.
    readme_text = README.read_text(encoding="utf-8") if README.is_file() else ""
    if "](docs/owui-tool.md)" not in readme_text:
        failures.append(
            "README.md does not link to docs/owui-tool.md (EXPECTED until 06-03 Task 1 "
            "lands the fork note — the accepted interim red this gate documents)"
        )
    else:
        print("OK: README.md links to docs/owui-tool.md (set I)")
    if "OpenWebUI" not in readme_text:
        failures.append(
            'README.md does not mention "OpenWebUI" (EXPECTED until 06-03 Task 1 lands '
            "the fork note)"
        )
    else:
        print('OK: README.md mentions "OpenWebUI" (set I)')

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        print(f"\n{len(failures)} case(s) failed.")
        return 1
    print("\nAll docs-parity cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
