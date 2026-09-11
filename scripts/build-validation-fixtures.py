#!/usr/bin/env python3
"""Emit the VAL-02/TEST-02 measurement corpus for the tool's diagram validator.

Deterministically writes, all committed and regenerate-and-diff safe:

    scripts/fixtures/validation/clean-base.html   one honest current-skin diagram
    scripts/fixtures/validation/defect-*.html     31 isolated single-defect mutants
    scripts/fixtures/validation/manifest.json     machine-readable expectations

Invariant (D-10): one defect class per fixture, every fixture a mutation of the
one clean base, isolated by family, byte-identical on re-run. Before a byte is
written the generator exec-loads the committed tool source the way an OpenWebUI
host does (read text -> exec(compile(...)) into a non-__main__ namespace) and
proves, per fixture, that the expected family reports the expected severity with
the expected message substring and that no other family reports an error. Any
miss is a loud exit 1 — a thin or drifted corpus must never publish
(CR-01/CR-02 lineage). Verification drives the public string in/out and parses
only the report markdown; it never imports the tool's internals, so it stays
honest about what the model actually receives.

Not wired into CI here: the suite cases that consume this corpus are Plan 03's
(VAL-02/VAL-03/D-15); CI/policy wiring for the generator itself is Phase 6
(TEST-04).
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = ROOT / "tools" / "diagram_design_tool.py"
OUT_DIR = ROOT / "scripts" / "fixtures" / "validation"
BASE_NAME = "clean-base.html"
MANIFEST_NAME = "manifest.json"
EXPECTED_CLASS_COUNT = 31

# Every check failure accumulates here with the offending class named; main()
# prints them and exits 1 before writing anything.
FAILURES: list[str] = []

# Report heading -> manifest family key. Headings are part of the validator's
# locked public shape (D-03), so keying on them keeps the verifier honest.
HEADING_TO_FAMILY = {
    "skin tokens": "skin",
    "geometry": "geometry",
    "accessibility": "a11y",
    "network egress": "egress",
}
FAMILY_TO_HEADING = {family: heading for heading, family in HEADING_TO_FAMILY.items()}
BULLET_RE = re.compile(r"^- \*\*(error|warning)\*\* \(line \d+\): (.*)$")

# The single clean base every defect fixture mutates (D-10 rule 1). It is an
# honest current-skin diagram, not a strawman: the design system's real font
# <link>, real palette hex + palette-derived rgba(), real aria wiring, and both
# font-family spellings the skin check scans (CSS declaration and attribute).
# It must report "PASS: 0 issues" — that is what makes "zero error findings on
# clean fixtures" a meaningful claim (D-09/T-03-12). The stale old-skin shipped
# assets stay excluded from the clean set via lint-skin's own baseline file,
# consumed by the suite, never by weakening a check here.
BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Median latency by region</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist+Mono&display=swap">
<style>
  .d1-note { font-family: Geist Mono, monospace; }
</style>
</head>
<body>
<svg role="img" aria-labelledby="d1-title d1-desc" viewBox="0 0 400 240">
  <title id="d1-title">Median latency by region</title>
  <desc id="d1-desc">Horizontal bars comparing median latency for four regions.</desc>
  <rect x="24" y="60" width="88" height="140" fill="#2e5aa8" stroke="#0a0a0a"/>
  <circle cx="220" cy="120" r="46" fill="rgba(46, 90, 168, 0.35)"/>
  <text class="d1-note" x="300" y="40" font-family="Geist Mono, monospace">p95 ms</text>
</svg>
</body>
</html>
"""

# The 31 defect classes, manifest-as-code (build-icons.py's ICONS style). Each
# entry is (name, family, severity, expect, instruction):
#   name        output filename stem, always "defect-<family>-<class>" (D-10 rule 4)
#   family      one of skin | egress | a11y | geometry
#   severity    error | warning per D-08 (errors fail the verdict, warnings pass)
#   expect      substring of the intended finding message — wording is D-03
#               discretion, so a wording drift fails generation and `expect` is
#               then corrected to the real substring; the family+severity pair
#               is the contract
#   instruction a single deterministic transformation of BASE_HTML, encoded as
#               an ordered tuple of (old, new) literal replacements; each `old`
#               must occur in BASE_HTML exactly once or generation fails
#
# Construction rules measured by failure and encoded below (D-10 rules 1-3):
#   1. one class per fixture, all mutating the same clean base;
#   2. isolate the intended severity — the missing-<desc> fixture also drops the
#      "d1-desc" entry from aria-labelledby and the missing-<title> fixture
#      drops "d1-title", so the broken-reference *error* cannot mask the
#      intended warning;
#   3. egress defects are injected before <svg> (or in <head>), never between
#      <svg> and its <title> — a void element there desyncs naive parsers. The
#      one in-svg exception is defect-egress-image-href, which must be an svg
#      child; it is appended after the accessible-name elements so first-child
#      detection is untouched.
DEFECT_CLASSES: tuple[tuple[str, str, str, str, tuple[tuple[str, str], ...]], ...] = (
    # ── skin (6) ──────────────────────────────────────────────────────────────
    (
        "defect-skin-off-palette-hex", "skin", "error",
        "#cc3388 is not in the style-guide palette",
        (('fill="#2e5aa8"', 'fill="#cc3388"'),),
    ),
    (
        "defect-skin-pure-black-hex", "skin", "error",
        "pure black #000000 is not allowed",
        (('fill="#2e5aa8"', 'fill="#000000"'),),
    ),
    (
        "defect-skin-pure-black-rgb", "skin", "error",
        "pure black rgb(0,0,0) is not allowed",
        (('stroke="#0a0a0a"', 'stroke="rgb(0, 0, 0)"'),),
    ),
    (
        "defect-skin-non-palette-rgba", "skin", "error",
        "rgba(12, 34, 200, 0.4) is not derived from an allowed palette color",
        (('fill="rgba(46, 90, 168, 0.35)"', 'fill="rgba(12, 34, 200, 0.4)"'),),
    ),
    (
        "defect-skin-offstack-font-attr", "skin", "warning",
        "unsupported font family: Helvetica",
        (
            ('font-family="Geist Mono, monospace">p95 ms',
             'font-family="Helvetica, sans-serif">p95 ms'),
        ),
    ),
    (
        "defect-skin-offstack-font-css", "skin", "warning",
        "unsupported font family: Helvetica",
        (
            (".d1-note { font-family: Geist Mono, monospace; }",
             ".d1-note { font-family: Helvetica, sans-serif; }"),
        ),
    ),
    # ── egress (8) — the last three close research assumption A4 (T-03-09):
    # the distilled fonts allowlist regex must reject a lookalike host, a bad
    # path and a protocol-relative URL, each of which then falls through to the
    # generic external-<link> error ───────────────────────────────────────────
    (
        "defect-egress-script-src", "egress", "error",
        "external HTTP(S) src is not allowed",
        (
            (
                '<body>\n<svg role="img"',
                '<body>\n<script src="https://cdn.example.com/tracker.js"></script>'
                "\n<svg role=\"img\"",
            ),
        ),
    ),
    (
        "defect-egress-css-import", "egress", "error",
        "CSS @import is not allowed",
        (
            (
                "<style>\n  .d1-note { font-family: Geist Mono, monospace; }\n</style>",
                "<style>\n  @import url(https://cdn.example.com/fonts.css);\n"
                "  .d1-note { font-family: Geist Mono, monospace; }\n</style>",
            ),
        ),
    ),
    (
        "defect-egress-css-url", "egress", "error",
        "non-fragment CSS url() is not allowed",
        (
            (
                "<style>\n  .d1-note { font-family: Geist Mono, monospace; }\n</style>",
                "<style>\n  .d1-note { background: "
                "url(https://cdn.example.com/pattern.svg); "
                "font-family: Geist Mono, monospace; }\n</style>",
            ),
        ),
    ),
    (
        "defect-egress-link-stylesheet", "egress", "error",
        "external HTTP(S) <link> is not allowed",
        (
            (
                '<link rel="stylesheet" '
                'href="https://fonts.googleapis.com/css2?family=Geist+Mono&display=swap">',
                '<link rel="stylesheet" '
                'href="https://fonts.googleapis.com/css2?family=Geist+Mono&display=swap">\n'
                '<link rel="stylesheet" href="https://cdn.example.com/theme.css">',
            ),
        ),
    ),
    (
        "defect-egress-image-href", "egress", "error",
        "external resource in <image> href is not allowed",
        (
            (
                "</svg>",
                '  <image x="200" y="90" width="60" height="60" '
                'href="https://cdn.example.com/pic.png"/>\n</svg>',
            ),
        ),
    ),
    (
        "defect-egress-fonts-lookalike-host", "egress", "error",
        "external HTTP(S) <link> is not allowed",
        (
            (
                'href="https://fonts.googleapis.com/css2?family=Geist+Mono&display=swap"',
                'href="https://fonts.googleapis.com.evil.test/css2?family=Geist+Mono'
                '&display=swap"',
            ),
        ),
    ),
    (
        "defect-egress-fonts-bad-path", "egress", "error",
        "external HTTP(S) <link> is not allowed",
        (
            (
                'href="https://fonts.googleapis.com/css2?family=Geist+Mono&display=swap"',
                'href="https://fonts.googleapis.com/css2.evil?family=Geist+Mono'
                '&display=swap"',
            ),
        ),
    ),
    (
        "defect-egress-fonts-protocol-relative", "egress", "error",
        "external HTTP(S) <link> is not allowed",
        (
            (
                'href="https://fonts.googleapis.com/css2?family=Geist+Mono&display=swap"',
                'href="//fonts.googleapis.com/css2?family=Geist+Mono&display=swap"',
            ),
        ),
    ),
    # ── a11y (10) ─────────────────────────────────────────────────────────────
    (
        "defect-a11y-missing-role", "a11y", "error",
        'must carry role="img"',
        (('<svg role="img" aria-labelledby=', '<svg aria-labelledby='),),
    ),
    (
        "defect-a11y-missing-title", "a11y", "error",
        "must contain a <title>",
        (
            ('  <title id="d1-title">Median latency by region</title>\n', ""),
            ('aria-labelledby="d1-title d1-desc"', 'aria-labelledby="d1-desc"'),
        ),
    ),
    (
        "defect-a11y-missing-desc", "a11y", "warning",
        "should contain a <desc>",
        (
            (
                "  <desc id=\"d1-desc\">Horizontal bars comparing median latency "
                "for four regions.</desc>\n",
                "",
            ),
            ('aria-labelledby="d1-title d1-desc"', 'aria-labelledby="d1-title"'),
        ),
    ),
    (
        "defect-a11y-empty-title", "a11y", "error",
        "<title> must not be empty",
        (
            ("<title id=\"d1-title\">Median latency by region</title>",
             '<title id="d1-title"></title>'),
        ),
    ),
    (
        "defect-a11y-empty-desc", "a11y", "warning",
        "<desc> must not be empty",
        (
            ("<desc id=\"d1-desc\">Horizontal bars comparing median latency "
             "for four regions.</desc>",
             '<desc id="d1-desc"></desc>'),
        ),
    ),
    (
        "defect-a11y-bare-id", "a11y", "error",
        'bare id="title" and id="desc" are not allowed',
        (
            ('<rect x="24" y="60" width="88" height="140"',
             '<rect id="title" x="24" y="60" width="88" height="140"'),
        ),
    ),
    (
        "defect-a11y-duplicate-name-id", "a11y", "error",
        'duplicate accessible-name id="d1-title" is not allowed',
        (
            ('<circle cx="220" cy="120" r="46"',
             '<circle id="d1-title" cx="220" cy="120" r="46"'),
        ),
    ),
    (
        "defect-a11y-labelledby-missing-id", "a11y", "error",
        "aria-labelledby references missing id(s): d1-missing",
        (
            ('aria-labelledby="d1-title d1-desc"',
             'aria-labelledby="d1-title d1-desc d1-missing"'),
        ),
    ),
    (
        "defect-a11y-labelledby-not-names", "a11y", "error",
        "aria-labelledby must name the <title> and <desc> IDs",
        (
            ('aria-labelledby="d1-title d1-desc"',
             'aria-labelledby="d1-rect d1-circle"'),
            ('<rect x="24" y="60"', '<rect id="d1-rect" x="24" y="60"'),
            ('<circle cx="220" cy="120"', '<circle id="d1-circle" cx="220" cy="120"'),
        ),
    ),
    (
        "defect-a11y-title-not-first", "a11y", "warning",
        "should be the first child of <svg>",
        (
            (
                '  <title id="d1-title">Median latency by region</title>\n'
                "  <desc id=\"d1-desc\">Horizontal bars comparing median latency "
                "for four regions.</desc>\n",
                '  <desc id="d1-desc">Horizontal bars comparing median latency '
                "for four regions.</desc>\n"
                '  <title id="d1-title">Median latency by region</title>\n',
            ),
        ),
    ),
    # ── geometry (7) ──────────────────────────────────────────────────────────
    (
        "defect-geometry-missing-viewbox", "geometry", "error",
        "must have a viewBox attribute",
        ((' viewBox="0 0 400 240"', ""),),
    ),
    (
        "defect-geometry-viewbox-negative-width", "geometry", "error",
        'viewBox "0 0 -400 240" is not valid',
        (('viewBox="0 0 400 240"', 'viewBox="0 0 -400 240"'),),
    ),
    (
        "defect-geometry-viewbox-zero-height", "geometry", "error",
        'viewBox "0 0 400 0" is not valid',
        (('viewBox="0 0 400 240"', 'viewBox="0 0 400 0"'),),
    ),
    (
        "defect-geometry-viewbox-malformed", "geometry", "error",
        'viewBox "0 0 400px 240" is not valid',
        (('viewBox="0 0 400 240"', 'viewBox="0 0 400px 240"'),),
    ),
    (
        "defect-geometry-negative-rect-height", "geometry", "error",
        "negative height=-40 is not allowed",
        (('height="140"', 'height="-40"'),),
    ),
    (
        "defect-geometry-non-finite-coord", "geometry", "error",
        "<rect> x is not a finite number",
        (('<rect x="24" y="60"', '<rect x="inf" y="60"'),),
    ),
    (
        "defect-geometry-out-of-bounds", "geometry", "warning",
        "lies outside the viewBox",
        (('<circle cx="220" cy="120"', '<circle cx="500" cy="120"'),),
    ),
)


def transform_base(name, instruction):
    """Apply one fixture's (old, new) replacements to BASE_HTML."""
    payload = BASE_HTML
    for old, new in instruction:
        occurrences = payload.count(old)
        if occurrences != 1:
            # An anchor that is absent or ambiguous would mutate nothing or the
            # wrong span — both silently produce a fixture of the wrong class.
            FAILURES.append(
                f"{name}: anchor {old[:60]!r} occurs {occurrences} times in "
                "BASE_HTML (expected exactly 1)"
            )
            continue
        payload = payload.replace(old, new)
    return payload


def load_validator():
    """Exec-load the committed tool source the way a host does; return validate_html."""
    if not TOOL_PATH.exists():
        FAILURES.append(
            f"{TOOL_PATH} not found — the corpus cannot be verified against the "
            "validator it measures; refusing to publish unverified fixtures"
        )
        return None
    namespace = {"__name__": "diagram_design_tool_fixture_probe", "__file__": str(TOOL_PATH)}
    try:
        exec(compile(TOOL_PATH.read_text(encoding="utf-8"), str(TOOL_PATH), "exec"), namespace)
    except Exception as error:  # noqa: BLE001 — any import-time break is a floor breach
        FAILURES.append(f"{TOOL_PATH} did not exec-load: {type(error).__name__}: {error}")
        return None
    entry = namespace.get("validate_html")
    if not callable(entry):
        FAILURES.append(f"{TOOL_PATH} exposes no callable validate_html entry point")
        return None
    return entry


def parse_report(name, report):
    """Report markdown -> {family: [(severity, message), ...]}, or None on a bad shape."""
    grouped: dict[str, list[tuple[str, str]]] = {}
    family = None
    for line in report.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip().casefold()
            family = HEADING_TO_FAMILY.get(heading)
            if family is None:
                FAILURES.append(f"{name}: unmapped report heading {line.strip()!r}")
                return None
            grouped.setdefault(family, [])
            continue
        bullet = BULLET_RE.match(line)
        if bullet and family is not None:
            grouped[family].append((bullet.group(1), bullet.group(2)))
    return grouped


def measured(findings):
    return "; ".join(
        f"{family}=[{', '.join(f'{sev}: {msg[:60]}' for sev, msg in items)}]"
        for family, items in sorted(findings.items())
    ) or "no findings"


def verify_base(validate):
    """T-03-12: an honest current-skin diagram must validate clean."""
    report = validate(BASE_HTML)
    if not report.startswith("PASS: 0 issues"):
        FAILURES.append(
            "clean-base: expected 'PASS: 0 issues' for the un-mutated base — "
            f"got: {report.splitlines()[0] if report else '<empty>'} "
            f"[{measured(parse_report('clean-base', report) or {})}]"
        )


def verify_class(validate, name, family, severity, expect, payload):
    """Family+severity+expect must be present; cross-family errors are forbidden."""
    report = validate(payload)
    if report.startswith("Error:"):
        FAILURES.append(
            f"{name}: validator returned an envelope instead of a report: "
            f"{report.strip()[:120]}"
        )
        return
    findings = parse_report(name, report)
    if findings is None:
        return
    hits = findings.get(family, [])
    if not any(found_severity == severity and expect in message
               for found_severity, message in hits):
        FAILURES.append(
            f"{name}: no {severity} finding under '## {FAMILY_TO_HEADING[family]}' "
            f"containing {expect!r} — measured [{measured(findings)}]"
        )
    for other_family, other_findings in sorted(findings.items()):
        if other_family == family:
            continue
        for other_severity, message in other_findings:
            if other_severity == "error":
                # Warnings elsewhere are tolerated (measured); an error in an
                # unintended family means the fixture is not isolated.
                FAILURES.append(
                    f"{name}: cross-family error under "
                    f"'## {FAMILY_TO_HEADING[other_family]}': {message}"
                )


def build_corpus():
    """In-memory (filename, payload) list, fixtures sorted by filename."""
    payloads = {
        f"{name}.html": transform_base(name, instruction)
        for name, _family, _severity, _expect, instruction in DEFECT_CLASSES
    }
    corpus = [(BASE_NAME, BASE_HTML)]
    corpus += sorted(payloads.items())
    manifest = {
        "base": BASE_NAME,
        "defects": [
            {"expect": expect, "family": family, "name": name, "severity": severity}
            for name, family, severity, expect, _instruction in DEFECT_CLASSES
        ],
    }
    corpus.append(
        (
            MANIFEST_NAME,
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )
    )
    return corpus


def main() -> int:
    # ── floors (CR-01/CR-02 lineage): refuse to publish a thin or drifted corpus
    validate = load_validator()
    if len(DEFECT_CLASSES) != EXPECTED_CLASS_COUNT:
        FAILURES.append(
            f"DEFECT_CLASSES holds {len(DEFECT_CLASSES)} entries, expected "
            f"{EXPECTED_CLASS_COUNT} — the class table drifted from the plan"
        )
    names = [name for name, *_rest in DEFECT_CLASSES]
    if len(set(names)) != len(names):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        FAILURES.append(f"duplicate fixture name(s): {duplicates}")
    for name, family, severity, expect, _instruction in DEFECT_CLASSES:
        if family not in HEADING_TO_FAMILY.values():
            FAILURES.append(f"{name}: unknown family {family!r}")
        if not name.startswith(f"defect-{family}-"):
            FAILURES.append(f"{name}: name does not start with 'defect-{family}-'")
        if severity not in ("error", "warning"):
            FAILURES.append(f"{name}: unknown severity {severity!r}")
        if not expect:
            FAILURES.append(f"{name}: empty expect substring")
        if not _instruction:
            FAILURES.append(f"{name}: empty instruction — it would emit the clean base")
    for marker in ("aria-labelledby", 'role="img"', "viewBox", "<title id=",
                   "<desc id=", "fonts.googleapis.com/css2"):
        if marker not in BASE_HTML:
            FAILURES.append(f"BASE_HTML lost required marker {marker!r}")
    lowered = BASE_HTML.casefold()
    for banned in ("<script", "url(", "@import", "src="):
        if banned in lowered:
            FAILURES.append(
                f"BASE_HTML carries {banned!r} — the clean base must be an honest "
                "current-skin diagram with no egress surface (T-03-12)"
            )

    corpus = build_corpus()
    payloads = dict(corpus)

    # ── self-verification against the real validator, before any write
    if validate is not None:
        verify_base(validate)
        for name, family, severity, expect, _instruction in DEFECT_CLASSES:
            verify_class(validate, name, family, severity, expect, payloads[f"{name}.html"])

    # ── pre-write hygiene: never publish alongside an orphan fixture a renamed
    # class left behind (it would sit committed and unverified — T-03-11)
    expected_names = {filename for filename, _payload in corpus}
    if OUT_DIR.is_dir():
        unexpected = sorted(
            path.name for path in OUT_DIR.iterdir() if path.name not in expected_names
        )
        if unexpected:
            FAILURES.append(
                f"{OUT_DIR} holds file(s) the generator does not emit: {unexpected} "
                "— remove them or restore their class definitions"
            )

    if FAILURES:
        for failure in FAILURES:
            print(f"FAIL: {failure}", file=sys.stderr)
        print(
            f"FAIL: {len(FAILURES)} problem(s) — nothing written to {OUT_DIR}",
            file=sys.stderr,
        )
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, payload in corpus:
        path = OUT_DIR / filename
        path.write_text(payload, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT)} ({len(payload.encode('utf-8'))} bytes)",
              file=sys.stderr)

    # ── determinism: re-read and compare against the in-memory payloads
    drift = []
    for filename, payload in corpus:
        reread = (OUT_DIR / filename).read_text(encoding="utf-8")
        if reread != payload:
            drift.append(filename)
    if drift:
        print(
            f"FAIL: re-read of {drift} differs from the generated payload — "
            "write is not deterministic",
            file=sys.stderr,
        )
        return 1

    defect_count = len(DEFECT_CLASSES)
    total = sum(len(payload.encode("utf-8")) for _filename, payload in corpus)
    by_family: dict[str, int] = {}
    for _name, family, *_rest in DEFECT_CLASSES:
        by_family[family] = by_family.get(family, 0) + 1
    print(
        f"wrote {len(corpus)} files ({total} bytes total) — {defect_count} defect "
        f"classes {sorted(by_family.items())} verified against {TOOL_PATH.relative_to(ROOT)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
