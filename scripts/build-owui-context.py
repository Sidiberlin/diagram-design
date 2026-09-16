"""Distill the diagram-design skill's reference docs into a single JSON digest.

Reads:  skills/diagram-design/references/type-*.md (40 files)
        skills/diagram-design/references/style-guide.md
Emits:  scripts/owui-context-digest.json
        tools/diagram_design_tool.py (its GENERATED DESIGN DIGEST block re-spliced)

Run:    python3 scripts/build-owui-context.py
Reqs:   stdlib only (base64, json, re, sys, zlib, pathlib)

The digest is a deterministic, size-budgeted distillation of the 40 diagram
types' authoring guidance (name, best_for, layout_conventions, anti_patterns,
examples) plus a shared style-guide block (tokens, typography, spacing). It
is committed generated output — Phase 2 splices its content into
tools/diagram_design_tool.py between GENERATED markers so the shipped tool
never reads this repo at runtime (PKG-03). Re-run this script any time a
type-*.md or style-guide.md file changes, and commit the regenerated digest.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TYPES_DIR = ROOT / "skills/diagram-design/references"
STYLE_GUIDE = ROOT / "skills/diagram-design/references/style-guide.md"
DIGEST_OUT = ROOT / "scripts/owui-context-digest.json"
# The shipped OpenWebUI tool file whose GENERATED DESIGN DIGEST block this script
# splices the payload into (D-05): one command regenerates both committed artifacts.
TOOL_OUT = ROOT / "tools/diagram_design_tool.py"
# Authoritative PKG-02 gate from Phase 2 on (D-06): the *combined* hand-written
# code + spliced digest block is what OpenWebUI has to load, so the whole file is
# what gets measured — not the digest in isolation. Gate runs before any write.
MAX_TOOL_FILE_BYTES = 120_000

# D-01 (Phase 4 design addendum): the digest is compressed at build time with
# zlib level 6, then base85-encoded so it drops into the tool file as plain
# ASCII with no escaping layer (base85's RFC1924 alphabet contains no quote and
# no backslash). Level 6 is the measured sweet spot on this corpus: the
# 105,435-byte payload (40-type corpus, full lists, 2026-09-11) compresses to
# 38,363 bytes / 47,954 base85 chars, and level 9 saves only 107 chars of file
# bytes — not worth leaving the default.
DIGEST_COMPRESS_LEVEL = 6
# Gates the *shipped* compressed block (measured 48,172 bytes: 47,954 blob +
# 218 bytes of decode wrapper and markers), fail-loud before any write. The
# uncompressed *guidance volume* keeps its own gate in INTERIM_SIZE_BUDGET_BYTES
# below — the two measure different things and both must hold.
MAX_EMBEDDED_DIGEST_BYTES = 55_000

# No per-type truncation (2026-09-11, quick task 260911-dsc). The earlier
# D-09 policy kept the first PER_TYPE_CAP=6 bullets and appended a
# "+N more — see skills/diagram-design/references/type-X.md" trailer — a repo
# path that does not exist in the OpenWebUI world, where the chat model has no
# filesystem. Live-instance symptom (OWUI .101, v0.11.1): on medium-complexity
# tasks the model burned its thinking budget and answered "the reference files
# are not available" — the tool's own trailer echoed back. The digest now
# ships every source bullet: restoring the 135 bullets the cap omitted (across
# 46 of the 80 lists, 32 of 40 types) measures 105,435 bytes combined, which
# fits the raised INTERIM_SIZE_BUDGET_BYTES below with the embedded-block
# (48,172 of 55,000) and tool-file (119,423 of 120,000) gates unchanged. If a
# future corpus outgrows a gate, the build fails loud and the budget decision
# is made deliberately (see PROJECT.md § Active) — never by silently dropping
# guidance the model has no other way to reach. Corpus range for reference:
# layout-conventions bullets 3-14, anti-patterns bullets 0-11.

# A build-time error log: a section whose heading cannot be located, or one
# that is located but yields nothing, is a real bug — not a silent []/"".
# See the guards in build_types() and extract_style_tokens().
EXTRACTION_FAILURES: list[str] = []


def find_heading_span(text: str, heading_names: list[str]) -> str | None:
    """Locate the first matching heading (numbered or not) and return its body,
    bounded by the next top-level '## ' heading or a nested-variant boundary
    (a sub-heading immediately followed, after blank lines, by '**Best for:**').
    """
    for name in heading_names:
        pat = r"^##\s+(?:\d+\.\s+)?" + re.escape(name) + r"[^\n]*\n"
        m = re.search(pat, text, re.MULTILINE)
        if not m:
            continue
        rest = text[m.end():]
        lines = rest.split("\n")
        offset = 0
        for i, line in enumerate(lines):
            if re.match(r"^##\s", line):  # next top-level heading
                return rest[:offset]
            if re.match(r"^#{3,}\s", line):  # a sub-heading — check for nested variant
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                if j < len(lines) and lines[j].startswith("**Best for:**"):
                    return rest[:offset]  # nested variant — stop BEFORE it
                # else: a legitimate continuation subsection (e.g. "### Table
                # box") — keep going, do not stop here
            offset += len(line) + 1
        return rest
    return None


def _split_pipe_row(row: str) -> list[str]:
    """Split one table row into cells, splitting on unescaped pipes only.

    A markdown-escaped ``\\|`` inside a cell is content, not a cell boundary:
    splitting on every pipe would cut the cell in two and shift every later
    cell left. The escape is then resolved so the cell text matches what
    markdown actually renders.
    """
    return [
        c.replace("\\|", "|").replace("`", "").strip() for c in re.split(r"(?<!\\)\|", row)
    ]


def parse_pipe_table(span_text: str) -> list[list[str]]:
    """Parse markdown pipe-table rows into a list of cell-lists, in source
    order, with the header row and any separator row dropped.

    Trailing whitespace after the closing pipe is tolerated — a row is
    dropped as "not a row" only if it does not start and end with a pipe.

    Strips ALL backtick characters from each cell (not just a leading/
    trailing pair) — a cell can wrap only part of its content in backticks
    (e.g. style-guide.md's `` `#f5f5f5` (white-smoke) ``, where the backticks
    surround only the hex code), so a leading/trailing-only strip would leave
    a stray backtick behind.
    """
    rows = re.findall(r"^\|(.*)\|[ \t]*$", span_text, re.MULTILINE)
    # A line that LOOKS like a table row (starts or ends with a pipe) but
    # fails the strict `| ... |` form is a real extraction loss, not noise:
    # the strict regex silently skips it and a token/typography/spacing
    # entry quietly vanishes from the digest (code review CR-02, Phase 2).
    # Prose containing interior pipes (e.g. "a | b | c") does not start or
    # end with a pipe and is not flagged.
    rejected = [
        line
        for line in span_text.split("\n")
        if line.strip()
        and (line.lstrip().startswith("|") or line.rstrip().endswith("|"))
        and not re.match(r"^\|.*\|[ \t]*$", line)
    ]
    if rejected:
        EXTRACTION_FAILURES.append(
            f"table line(s) that look like rows but are not in `| ... |` "
            f"form were skipped: {rejected!r}"
        )
    parsed = [_split_pipe_row(row) for row in rows]
    if not parsed:
        return []
    # Drop header row.
    body = parsed[1:]
    # Drop any separator row (cells consist only of -/: characters).
    body = [row for row in body if not all(re.match(r"^[-:]*$", c) for c in row)]
    return body


def extract_bullets(section_text: str | None) -> list[str]:
    """3-way fallback bullet extraction: unordered -> numbered -> pipe-table
    rows (joined as "{cell0} — {cell1}"). Collapses internal whitespace.
    """
    if not section_text:
        return []
    items = re.findall(
        r"^-\s+(.*(?:\n(?!^-\s|\n^#{2,}\s|^\d+\.\s).*)*)", section_text, re.MULTILINE
    )
    if not items:
        items = re.findall(
            r"^\d+\.\s+(.*(?:\n(?!^\d+\.\s|\n^#{2,}\s|^-\s).*)*)", section_text, re.MULTILINE
        )
    if not items:
        rows = parse_pipe_table(section_text)
        items = [f"{row[0]} — {row[1]}" for row in rows if len(row) >= 2]
    return [re.sub(r"\s+", " ", it).strip() for it in items]


def layout_conventions_for(text: str) -> tuple[list[str], str, str | None]:
    """Returns (bullets, source_field, span) — falls back to Reproducibility
    checklist for the 8 parametric types with no Layout conventions heading.

    The located span is returned alongside the bullets so a caller can tell
    "heading not located" (span is None, bullets necessarily []) apart from
    "located but yielded no bullets" — the two failure shapes need different
    diagnostics, and neither may pass silently.
    """
    span = find_heading_span(text, ["Layout conventions"])
    if span is not None:
        return extract_bullets(span), "layout_conventions", span
    span = find_heading_span(text, ["Reproducibility checklist"])
    return extract_bullets(span), "reproducibility_checklist_fallback", span


def extract_title(text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def extract_best_for(text: str) -> str:
    m = re.search(r"^\*\*Best for:\*\*(.*)$", text, re.MULTILINE)
    if not m:
        return ""
    value = m.group(1).strip()
    return re.sub(r"\s+", " ", value).strip()


def build_types() -> dict[str, dict]:
    """Build the per-type digest entries for all 40 type-*.md files (excludes
    primitive-*.md and semantic-patterns.md via the "type-*.md" glob itself).
    """
    types: dict[str, dict] = {}
    paths = sorted(TYPES_DIR.glob("type-*.md"))
    if not TYPES_DIR.is_dir() or not paths:
        # A corpus that yields zero files must not quietly publish a
        # zero-type tool (code review CR-01, Phase 2): the references
        # directory is missing/renamed or the glob pattern is wrong.
        EXTRACTION_FAILURES.append(
            f"no type-*.md files found under {TYPES_DIR} — the corpus is "
            "missing or the glob pattern is wrong; refusing to publish a "
            "digest with zero types"
        )
        return types
    if len(paths) < 30:
        # Partial-corpus floor: 40 types ship today; a checkout that lost
        # most of them should fail loudly, not publish a thin digest.
        EXTRACTION_FAILURES.append(
            f"only {len(paths)} type-*.md files found under {TYPES_DIR} "
            "(expected the full ~40-type corpus) — partial checkout or "
            "corpus moved; refusing to publish"
        )
    for path in paths:
        slug = path.stem[len("type-"):]
        text = path.read_text(encoding="utf-8")

        name = extract_title(text)
        best_for = extract_best_for(text)

        layout_bullets, layout_source_field, layout_span = layout_conventions_for(text)
        if layout_span is None:
            # Heading renamed/removed: extract_bullets() would have returned []
            # with no other signal, so fail loudly here instead.
            EXTRACTION_FAILURES.append(
                f"{slug}: neither 'Layout conventions' nor the fallback heading was "
                "located — layout_conventions would be silently empty"
            )
        elif not layout_bullets:
            EXTRACTION_FAILURES.append(
                f"{slug}: layout_conventions section located but extract_bullets "
                "returned zero items"
            )

        anti_span = find_heading_span(text, ["Anti-patterns"])
        anti_bullets = extract_bullets(anti_span)
        # Span-based guard, deliberately not "must be non-empty": the corpus's
        # documented anti-patterns range starts at 0 (see the no-truncation
        # policy comment above PER_TYPE_CAP used to sit).
        if anti_span is not None and not anti_bullets:
            EXTRACTION_FAILURES.append(
                f"{slug}: anti_patterns section located but extract_bullets "
                "returned zero items"
            )

        if not name:
            EXTRACTION_FAILURES.append(f"{slug}: no '# ' title found")
        if not best_for:
            EXTRACTION_FAILURES.append(f"{slug}: no '**Best for:**' line found")

        types[slug] = {
            "name": name,
            "best_for": best_for,
            "layout_conventions": layout_bullets,
            "anti_patterns": anti_bullets,
            "examples": "ships as minimal-light / minimal-dark / full-editorial variants",
        }

        print(
            f"{slug}: layout={len(layout_bullets)} ({layout_source_field}) "
            f"anti_patterns={len(anti_bullets)}",
            file=sys.stderr,
        )

    return types


def bounded_span(text: str, heading_line_pattern: str) -> str | None:
    """Locate a heading line matching heading_line_pattern (MULTILINE) and
    return the text between the end of that heading line and the next line
    that either starts with "#" or is exactly "---" (style-guide.md's
    sections are separated by "---" rules, not only headings).

    A "#" line inside a fenced code block is content, not a boundary, so
    fence delimiters are tracked and such lines are skipped — otherwise a
    fenced comment silently truncates the section and the loss goes
    unrecorded.
    """
    m = re.search(heading_line_pattern, text, re.MULTILINE)
    if not m:
        return None
    rest = text[m.end():]
    # Consume the newline right after the heading line, if present.
    if rest.startswith("\n"):
        rest = rest[1:]
    lines = rest.split("\n")
    offset = 0
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and (line.startswith("#") or line == "---"):
            return rest[:offset]
        offset += len(line) + 1
    return rest


def extract_style_tokens(style_source: str) -> dict:
    """Distill style-guide.md's Semantic roles / Typography / Stroke, radius,
    spacing tables only (per D-04/D-05) — nothing else from that file.
    """
    tokens_span = bounded_span(style_source, r"^### Semantic roles$")
    tokens_rows = parse_pipe_table(tokens_span or "")
    tokens = {
        row[0]: {"purpose": row[1], "light": row[2], "dark": row[3]}
        for row in tokens_rows
        if len(row) >= 4
    }

    typography_span = bounded_span(style_source, r"^## Typography$")
    typography_rows = parse_pipe_table(typography_span or "")
    typography = {
        row[0]: {"family": row[1], "size": row[2], "weight": row[3], "usage": row[4]}
        for row in typography_rows
        if len(row) >= 5
    }

    spacing_span = bounded_span(style_source, r"^## Stroke, radius, spacing$")
    spacing_rows = parse_pipe_table(spacing_span or "")
    spacing = {
        row[0]: {"value": row[1], "use": row[2]}
        for row in spacing_rows
        if len(row) >= 3
    }

    # A build-time error log: a renamed heading, a truncated/empty table, or a
    # row that was parsed but could not be keyed is a real bug, not a silent
    # {} — same "no silent []" discipline build_types() applies to the per-type
    # sections above. Comparing parsed rows against built keys also catches a
    # duplicate first cell silently collapsing two rows into one.
    for field_name, span, rows, table in (
        ("tokens", tokens_span, tokens_rows, tokens),
        ("typography", typography_span, typography_rows, typography),
        ("spacing", spacing_span, spacing_rows, spacing),
    ):
        if span is None:
            EXTRACTION_FAILURES.append(f"style_guide.{field_name}: heading not found")
        elif not table:
            EXTRACTION_FAILURES.append(
                f"style_guide.{field_name}: section located but table extraction returned zero rows"
            )
        elif len(rows) != len(table):
            EXTRACTION_FAILURES.append(
                f"style_guide.{field_name}: parsed {len(rows)} rows but built "
                f"{len(table)} keys (dropped row or duplicate key)"
            )

    return {"tokens": tokens, "typography": typography, "spacing": spacing}


def build_digest() -> dict:
    """Pure function: {"types": {...40}, "style_guide": {...}}. D-06: style_guide
    is a single top-level key, not duplicated inside each of the 40 type entries.
    """
    EXTRACTION_FAILURES.clear()
    return {
        "types": build_types(),
        "style_guide": extract_style_tokens(STYLE_GUIDE.read_text(encoding="utf-8")),
    }


# Sentinel markers Phase 2 will search for when splicing the digest into
# tools/diagram_design_tool.py (RESEARCH.md Pattern 2).
DIGEST_START = "    # >>> GENERATED DESIGN DIGEST (scripts/build-owui-context.py) >>>"
DIGEST_END = "    # <<< END GENERATED DESIGN DIGEST <<<"


def encode_digest_payload(payload: str) -> str:
    """Compress a plain-JSON digest payload into its embeddable base85 form (D-01).

    Deterministic in-process: the same input yields the same bytes (witnessed by
    the context suite's codec-determinism case). RFC 1950/1951 does not extend
    that guarantee across zlib builds, which is why decode_digest_block() exists
    to explain a platform mismatch instead of printing a bare byte diff.
    """
    return base64.b85encode(
        zlib.compress(payload.encode("utf-8"), DIGEST_COMPRESS_LEVEL)
    ).decode("ascii")


def digest_block(encoded_blob: str) -> str:
    """Return the full marker-to-marker generated block for one base85 blob.

    The blob sits on a single physical line inside a single-quoted repr:
    base85's RFC1924 alphabet contains no quote and no backslash, so no
    escaping layer is needed and none is added. Splitting it into ~120-char
    adjacent literals would cost ~380 newlines and buy nothing — D-01 already
    accepts the generated region's review opacity, and the round-trip parity
    suite guards it.
    """
    return (
        f"{DIGEST_START}\n"
        "    _DESIGN_DIGEST: dict = json.loads(zlib.decompress(base64.b85decode(\n"
        f"        {encoded_blob!r}\n"
        "    )).decode('utf-8'))\n"
        f"{DIGEST_END}"
    )


def splice(tool_source: str, digest_json: str) -> str:
    """Replace the span between DIGEST_START/DIGEST_END with a block embedding
    the digest as a zlib(6)+base85 blob decoded once at class-body exec:
    `_DESIGN_DIGEST: dict = json.loads(zlib.decompress(base64.b85decode(...)))`.

    The payload argument stays the *uncompressed* plain-JSON text and is
    compressed internally here, and DIGEST_OUT keeps being written as plain
    JSON — that pairing is what lets every existing consumer keep reading the
    committed digest artifact rather than the embed.

    main() calls this against the real tools/diagram_design_tool.py, so the
    committed file must be a fixed point of splice() with the committed payload:
    running the build twice leaves it byte-identical. Raises ValueError (via
    .index()) when either marker is missing — callers wrap it for remediation.
    """
    block = digest_block(encode_digest_payload(digest_json))
    start = tool_source.index(DIGEST_START)
    end = tool_source.index(DIGEST_END) + len(DIGEST_END)
    return tool_source[:start] + block + tool_source[end:]


def decode_digest_block(tool_source: str) -> dict:
    """Decode a tool source's generated block back into the digest dict.

    The fixed-point and budget diagnostics use this to compare *decoded*
    digests: when the decoded digests match but the file bytes do not, the cause
    is a zlib encoder that differs from the one that produced the committed
    artifact (Pitfall 4) — not real digest drift. Raises ValueError when the
    marker span or the base85 blob cannot be located.
    """
    start = tool_source.index(DIGEST_START)
    end = tool_source.index(DIGEST_END)
    span = tool_source[start:end]
    match = re.search(r"b85decode\(\s*'([^']*)'", span, re.DOTALL)
    if match is None:
        raise ValueError(
            "no base85 blob found between DIGEST_START/DIGEST_END — the generated "
            "block does not carry the compressed embed format"
        )
    decoded = zlib.decompress(base64.b85decode(match.group(1)))
    return json.loads(decoded.decode("utf-8"))


# Secondary, digest-only check (kept per D-06's implementer discretion so Phase 1's
# parity-test cases 6/6b keep passing). Measures the *uncompressed guidance
# volume* the tool serves, not the shipped bytes — the compressed embed has its
# own gate in MAX_EMBEDDED_DIGEST_BYTES above. The authoritative budget is the
# combined tools/diagram_design_tool.py size against MAX_TOOL_FILE_BYTES above.
# Raised 90,000 -> 110,000 (2026-09-11) when truncation was removed: the full
# 40-type corpus measures 105,435 bytes, so the old gate held only with the
# cap dropping 135 bullets. 110,000 is the smallest clean number that holds
# the full-lists digest (~4.6 KB headroom).
INTERIM_SIZE_BUDGET_BYTES = 110_000


def main() -> int:
    digest = build_digest()
    payload = json.dumps(digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    size = len(payload.encode("utf-8"))
    print(f"digest size: {size} bytes", file=sys.stderr)
    if EXTRACTION_FAILURES:
        print(f"EXTRACTION_FAILURES: {EXTRACTION_FAILURES}", file=sys.stderr)

    failed = False
    if EXTRACTION_FAILURES:
        failed = True
        for failure in EXTRACTION_FAILURES:
            print(f"FAIL: {failure}", file=sys.stderr)
    if size > INTERIM_SIZE_BUDGET_BYTES:
        failed = True
        print(
            f"FAIL: digest is {size} bytes, exceeds interim budget of "
            f"{INTERIM_SIZE_BUDGET_BYTES}",
            file=sys.stderr,
        )

    # D-01: the shipped generated block is compressed, so it gets its own
    # byte budget — measured on the exact block that would be spliced, before
    # anything is written.
    embedded_block = digest_block(encode_digest_payload(payload))
    embedded_size = len(embedded_block.encode("utf-8"))
    if embedded_size > MAX_EMBEDDED_DIGEST_BYTES:
        failed = True
        print(
            f"FAIL: embedded digest block is {embedded_size} bytes, exceeds embedded "
            f"digest budget of {MAX_EMBEDDED_DIGEST_BYTES} bytes",
            file=sys.stderr,
        )

    # D-05: splice the payload into the tool file's GENERATED block, and D-06:
    # gate the *combined* file size before anything is written. Measured on the
    # in-memory spliced string via .encode("utf-8") — never on read_text()
    # length, which universal-newline translation can shift.
    spliced = ""
    total = 0
    if not TOOL_OUT.exists():
        failed = True
        print(
            f"FAIL: {TOOL_OUT} not found — create skeleton before building "
            "(it must carry the DIGEST_START/DIGEST_END markers)",
            file=sys.stderr,
        )
    else:
        tool_source = TOOL_OUT.read_text(encoding="utf-8")
        # D-01 / T-4-01: the generated block calls `zlib` and `base64` from
        # inside `class Tools:`, so those imports are hand-written code *outside*
        # the markers. A missing one still byte-compiles — and then dies with
        # `NameError` at class-body exec on every load — so refuse to splice.
        missing_imports = [
            name for name in ("import base64", "import zlib") if name not in tool_source
        ]
        if missing_imports:
            failed = True
            print(
                f"FAIL: {TOOL_OUT} is missing the compressed digest block's required "
                f"module-level import(s): {', '.join(missing_imports)} (D-01) — "
                "the spliced block resolves them at class-body exec",
                file=sys.stderr,
            )
        try:
            spliced = splice(tool_source, payload)
        except ValueError:
            failed = True
            print(
                f"FAIL: {TOOL_OUT} is missing DIGEST_START/DIGEST_END marker — "
                "both marker lines must exist verbatim, at class-body indent "
                "(4 spaces) inside `class Tools:`",
                file=sys.stderr,
            )
        else:
            total = len(spliced.encode("utf-8"))
            if total > MAX_TOOL_FILE_BYTES:
                failed = True
                print(
                    f"FAIL: spliced tool file is {total} bytes, exceeds tool file "
                    f"budget of {MAX_TOOL_FILE_BYTES} bytes",
                    file=sys.stderr,
                )
    # Decode-aware notice for cross-platform zlib encoder drift (Pitfall 4 /
    # T-4-04). CI's regenerate-and-diff gate fails on the byte diff BEFORE the
    # test suites ever run, so a harness-only diagnostic is unreachable exactly
    # when it matters — the build always runs first in that step. If the spliced
    # output differs from the committed file but both embeds decode to the same
    # digest, the content is fine and the encoder is the cause: say so instead
    # of leaving a bare byte diff.
    if not failed and TOOL_OUT.exists() and spliced != TOOL_OUT.read_text(encoding="utf-8"):
        try:
            on_disk_digest = decode_digest_block(TOOL_OUT.read_text(encoding="utf-8"))
            rebuilt_digest = decode_digest_block(spliced)
        except ValueError:  # noqa: BLE001 — unreadable embed; the suites report the drift
            on_disk_digest = rebuilt_digest = None
        if on_disk_digest is not None and on_disk_digest == rebuilt_digest:
            print(
                "NOTE: tools/diagram_design_tool.py's embedded digest block differs "
                "byte-for-byte from the freshly built one, but both decode to the "
                "identical digest: your platform's zlib encoder differs from the one "
                "that produced the committed artifact. Regenerate on ubuntu + python "
                "3.12 for a byte-identical artifact, or pin CI's regenerate-and-diff "
                "step to one matrix leg.",
                file=sys.stderr,
            )

    if failed:
        return 1

    DIGEST_OUT.write_text(payload, encoding="utf-8", newline="\n")
    TOOL_OUT.write_text(spliced, encoding="utf-8", newline="\n")
    print(
        f"spliced tool file: {total} bytes (embedded digest block: {embedded_size} "
        f"bytes, {len(encode_digest_payload(payload))} base85 chars at "
        f"level {DIGEST_COMPRESS_LEVEL})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
