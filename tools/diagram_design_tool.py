"""
title: Diagram Design
author: diagram-design maintainers
description: Design-system briefs, validation and export for editorial diagrams (40 types).
"""

import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

# OpenWebUI imports this module inside its own backend process, where pydantic v2
# is always present. The guard exists so the file still loads in a bare
# interpreter (this repo's CI, `python3 -I -S` probes), which keeps PKG-01
# literally true. The stand-in is deliberately dumb — an attribute bag with no
# validation — so no code below may ever call a pydantic-only serialization or
# introspection API; both import branches must behave identically.
try:
    from pydantic import BaseModel, Field
except ImportError:

    class BaseModel:
        def __init__(self, **data):
            for key, value in data.items():
                setattr(self, key, value)

    def _field_stub(default=None, description=None, **_ignored):
        return default

    # Same name real pydantic binds, so Phase 4+ valve fields are written
    # identically under both branches. (The indented `_field_stub` def keeps
    # every 4-space-indented `def` in this file `_`-prefixed or public API.)
    Field = _field_stub


# ── Diagram validator (VAL-01/VAL-03) ────────────────────────────────────────
#
# Distilled stdlib-only validator behind validate_diagram(): the token/egress
# regexes, palette, font allowlist and two-stack SVG parser are pasted ports of
# scripts/lint-skin.py (D-07/VAL-03), and the harness parity case re-proves the
# constants against lint-skin on every run. Deviations: IMPORT_ANY_RE/URL_ANY_RE
# ban every @import/url(), not only remote ones, and the palette/font list is
# hardcoded rather than derived from _DESIGN_DIGEST (measured: derivation
# yields 12 of the 31 hex values).
#
# Call-time contract (D-15): pure regex + tokenizer over the `html` argument —
# no file/network/subprocess access, no event emissions; script bodies are read
# as text and hashed, never executed.

HEX_RE = re.compile(
    r"(?<![\w-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9a-fA-F])"
)
RGBA_RE = re.compile(
    r"rgba\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*([^)]+)\)",
    re.IGNORECASE,
)
BLACK_RGB_RE = re.compile(r"rgb\(\s*0\s*,\s*0\s*,\s*0\s*\)", re.IGNORECASE)
FONT_CSS_RE = re.compile(r"font-family\s*:\s*([^;}]+)", re.IGNORECASE)
FONT_ATTR_RE = re.compile(
    r"\bfont-family\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL
)
SRC_HTTP_RE = re.compile(r"\bsrc\s*=\s*(['\"])\s*(?:https?:)?//", re.IGNORECASE)
IMPORT_ANY_RE = re.compile(r"@import\b", re.IGNORECASE)
URL_ANY_RE = re.compile(r"url\(\s*([^)]+?)\s*\)", re.IGNORECASE)
LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"\bhref\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
REL_RE = re.compile(r"\brel\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
SCRIPT_OPEN_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
SCRIPT_BLOCK_RE = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Style-guide palette hardcoded from lint-skin's allowed_colors() (31 values).
_PALETTE_HEX_STR = (
    "#0a0a0a #141414 #1b1b1b #2b2b2b #2d3142 #2e5aa8 #393e53 #4f5d75 "
    "#5c5c5c #5e7a9b #6a95d8 #6e6479 #7a8399 #7c8f6f #82a0c0 #8d8298 "
    "#8e98ac #9a9a9a #9c6b50 #9caf8f #b88670 #b8915a #bfc0c0 #d3ad7a "
    "#eb6c36 #ececec #f08a59 #f5f5f5 #ff5a36 #fff #ffffff"
)
def _palette_rgb(palette):
    """The rgba() membership set: an RGB triple per 6-digit hex in a palette.

    Derived rather than hardcoded so a substituted brand palette gets its own
    set (B3) — the default's _PALETTE_RGB and a profile's are built by the same
    one derivation, never by two that could drift.
    """
    return frozenset(
        tuple(int(c[i : i + 2], 16) for i in (1, 3, 5)) for c in palette if len(c) == 7
    )


_PALETTE = frozenset(_PALETTE_HEX_STR.split())
_PALETTE_RGB = _palette_rgb(_PALETTE)

# D-01/D-02: the ten semantic roles a brand profile may set, in the order the
# style guide introduces them. This tuple is the single source of truth for
# both the store schema (what a profile file may contain) and the D-02
# reject-whole envelope (the valid-role list a rejected payload is shown).
_PROFILE_ROLES = (
    "paper",
    "paper-2",
    "ink",
    "muted",
    "soft",
    "rule",
    "rule-solid",
    "accent",
    "accent-tint",
    "link",
)
# A role value must be a hex colour: `#abc` or `#aabbcc`, nothing else (D-02).
_PROFILE_HEX_RE = re.compile(r"^#[0-9a-f]{3}(?:[0-9a-f]{3})?$")

_ALLOWED_FONTS = frozenset((
    "instrument serif", "geist", "geist mono", "hiragino sans", "noto sans jp",
    "yu gothic", "noto sans mono cjk jp", "apple sd gothic neo", "noto sans kr",
    "noto serif kr", "malgun gothic", "noto sans mono cjk kr", "pingfang sc",
    "noto sans sc", "microsoft yahei", "noto sans mono cjk sc", "pingfang tc",
    "noto sans tc", "noto serif tc", "microsoft jhenghei",
    "noto sans mono cjk tc", "system-ui", "sans-serif",
    "serif", "monospace", "ui-monospace",
))
_CSS_FONT_KEYWORDS = frozenset(("inherit", "initial", "revert", "revert-layer", "unset"))

# Script policy: `<script>` is banned except one exact-match canonical motion
# controller — the skill's documented animation convention (template-motion.html)
# that shipped diagrams embed and models imitate. Repo-derived and suite-verified
# (the harness recomputes it via lint-skin's canonical_controller_digest());
# embedded here, never read from the repo at call time (D-15).
_CANONICAL_CONTROLLER_SHA256 = (
    "fd49c28b3bbc62d5304c343d267fb3fe93dd5cb33db49b953b1c86e01faf0663"
)


def _normalize_hex(value):
    value = value.lower()
    if len(value) == 4:
        return "#" + "".join(character * 2 for character in value[1:])
    return value


def _now_iso():
    """Profile timestamp, UTC. Informational only — nothing parses it back (A3)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize_role_hex(value):
    """`#ABC`/`#aabbcc` in, canonical lowercase 6-digit hex out; None otherwise.

    Reuses _normalize_hex so a profile value normalizes exactly the way
    _check_skin normalizes authored hex (Pitfall 9) — one discipline, two calls.
    """
    if not isinstance(value, str):
        return None
    normalized = _normalize_hex(value.strip())
    return normalized if _PROFILE_HEX_RE.match(normalized) else None


def _profile_palette(tokens):
    """The substituted skin set for a stored token map, or None if none survives.

    Values are re-normalized through _normalize_hex, so `#FFF` stored by an
    older writer passes a diagram authored `#ffffff` (Pitfall 9). An empty or
    unusable map means "no profile", never an empty palette that would flag
    every colour. D-09's universal pure-black ban needs no special case here:
    _check_skin's black branch fires before membership, so a #000000 role is
    still an error in every verdict.
    """
    if not isinstance(tokens, dict):
        return None
    # CR-01 (code review): _normalize_hex alone accepts junk like "blue"
    # (-> "#lluuee") which then crashes _palette_rgb's int(..., 16). Route
    # through _normalize_role_hex so a non-hex value is dropped (junk-only
    # degrades to no-profile) instead of poisoning the palette.
    palette = frozenset(
        hex_value
        for hex_value in (
            _normalize_role_hex(value.strip().lower())
            for value in tokens.values()
            if isinstance(value, str) and value.strip()
        )
        if hex_value is not None
    )
    return palette or None


def _google_fonts_families(url):
    """Approved families from a fonts.googleapis.com/css2 stylesheet URL."""
    candidate = url.strip()
    # Casefold scheme+host as lint-skin's urlparse does, so an uppercase host
    # stays allowed while the anchored regex stays byte-identical (parity case).
    head = re.match(
        r"^(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<host>[^/?#]*)", candidate, re.IGNORECASE
    )
    if head:
        candidate = (
            head.group("scheme").casefold()
            + head.group("host").casefold()
            + candidate[head.end():]
        )
    match = re.match(r"https://fonts\.googleapis\.com(?::443)?/css2\?([^#]*)$", candidate)
    if not match:
        return set()
    families = set()
    for pair in match.group(1).split("&"):
        key, _, value = pair.partition("=")
        if key != "family":
            continue
        family = value.split(":", 1)[0].replace("+", " ").strip().casefold()
        if family:
            families.add(family)
    return families


def _named_families(value, allowed):
    families = []
    for raw in value.split(","):
        family = raw.strip().strip("'\"").strip()
        lowered = family.casefold()
        if not family or lowered in _CSS_FONT_KEYWORDS or lowered.startswith("var("):
            continue
        if lowered not in allowed:
            families.append(family)
    return families


def _normalized_controller(body):
    """Platform-stable form of an inline motion controller (lint-skin port)."""
    return body.replace("\r\n", "\n").replace("\r", "\n").strip()


class _DiagramParser(HTMLParser):
    """Distilled AccessibleSvgParser — names, ids and viewBox-scoped geometry.

    Tokenizes untrusted text only. Two stacks (open <svg> entries plus tag
    names) because void elements (<link>, <img>) never get an end tag: a depth
    check against the element stack stays correct where a parent lookup would
    not.
    """

    GEOMETRY = {
        "rect": ("x", "y", "width", "height"),
        "circle": ("cx", "cy", "r"),
        "ellipse": ("cx", "cy", "rx", "ry"),
        "line": ("x1", "y1", "x2", "y2"),
        "text": ("x", "y"),
        "image": ("x", "y", "width", "height"),
    }
    REMOTE_ATTRS = {
        "image": ("href", "xlink:href"), "use": ("href", "xlink:href"),
        "feimage": ("href", "xlink:href"),  # CR-02
        "iframe": ("src",), "object": ("data",), "embed": ("src",),
        "img": ("src", "srcset"),
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.svgs = []
        self.ids = {}
        self.open_text = []
        self.geometry = []
        self.remote = []
        self._svg_stack = []
        self._element_stack = []
        self._viewbox_stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        line = self.getpos()[0]
        amap = {name.casefold(): value or "" for name, value in attrs}
        element_id = amap.get("id")

        if tag == "svg":
            entry = {"line": line, "attrs": amap, "titles": [], "descs": [],
                     "first_child": None, "content_depth": len(self._element_stack) + 1,
                     "ids": set(), "bare_ids": 0,
                     "viewbox_raw": amap.get("viewbox")}
            entry["viewbox"] = self._viewbox(entry["viewbox_raw"])
            self.svgs.append(entry)
            self._svg_stack.append(entry)
            self._viewbox_stack.append(entry["viewbox"])
        elif self._svg_stack:
            owner = self._svg_stack[-1]
            for attr in self.REMOTE_ATTRS.get(tag, ()):
                value = amap.get(attr, "").strip()
                if value and re.match(r"^(?:[a-z][a-z0-9+.-]*:)?//", value, re.IGNORECASE):
                    self.remote.append((line, tag, attr, value))
            if len(self._element_stack) == owner["content_depth"]:
                if owner["first_child"] is None:
                    owner["first_child"] = tag
                if tag in ("title", "desc"):
                    element = {"tag": tag, "id": element_id, "line": line, "text": []}
                    owner["titles" if tag == "title" else "descs"].append(element)
                    self.open_text.append(element)

        if element_id:
            prior = self.ids.get(element_id)
            self.ids[element_id] = [line, (prior[1] + 1) if prior else 1]
            for owner in self._svg_stack:
                owner["ids"].add(element_id)
                if element_id in {"title", "desc"}:
                    owner["bare_ids"] += 1

        if tag in self.GEOMETRY and self._viewbox_stack:
            values = {}
            for name in self.GEOMETRY[tag]:
                raw = amap.get(name)
                try:
                    values[name] = float(raw) if raw is not None else None
                except ValueError:
                    values[name] = None  # "100%" and other unit forms: unchecked
            self.geometry.append((line, tag, values, self._viewbox_stack[-1]))

        self._element_stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.casefold()
        for index in range(len(self.open_text) - 1, -1, -1):
            if self.open_text[index]["tag"] == tag:
                del self.open_text[index]
                break
        if tag == "svg" and self._svg_stack:
            self._svg_stack.pop()
            self._viewbox_stack.pop()
        for index in range(len(self._element_stack) - 1, -1, -1):
            if self._element_stack[index] == tag:
                del self._element_stack[index:]
                break

    def handle_data(self, data):
        for element in self.open_text:
            element["text"].append(data)

    @staticmethod
    def _viewbox(raw):
        if not raw:
            return None
        parts = re.split(r"[\s,]+", raw.strip())
        if len(parts) != 4:
            return None
        try:
            nums = [float(part) for part in parts]
        except ValueError:
            return None
        if not all(math.isfinite(n) for n in nums) or nums[2] <= 0 or nums[3] <= 0:
            return None
        return nums


def _check_egress(text):
    """Network-egress findings plus the fonts a stylesheet <link> approves.

    Rules verbatim from lint_text(); the one permitted external resource is a
    fonts.googleapis.com/css2 stylesheet. Script policy lives here too: at most
    one controller, with exactly the canonical attribute and body.
    """
    findings = []
    approved_fonts = set()

    def add(offset, message):
        findings.append(("egress", "error", text.count("\n", 0, offset) + 1, message))

    for match in SRC_HTTP_RE.finditer(text):
        add(match.start(), "external HTTP(S) src is not allowed")
    for match in IMPORT_ANY_RE.finditer(text):
        add(match.start(), "CSS @import is not allowed")
    for match in URL_ANY_RE.finditer(text):
        value = match.group(1).strip().strip("'\"").strip()
        if value.startswith("#"):
            continue
        add(match.start(), "non-fragment CSS url() is not allowed")
    for link_match in LINK_RE.finditer(text):
        href_match = HREF_RE.search(link_match.group())
        if not href_match:
            continue
        href = href_match.group(2).strip()
        rel_match = REL_RE.search(link_match.group())
        rel_values = rel_match.group(2).casefold().split() if rel_match else []
        families = _google_fonts_families(href) if "stylesheet" in rel_values else set()
        if families:
            approved_fonts.update(families)
            continue
        if re.match(r"^(https?:)?//", href, re.IGNORECASE):
            add(link_match.start(), "external HTTP(S) <link> is not allowed")

    script_blocks = {match.start(): match for match in SCRIPT_BLOCK_RE.finditer(text)}
    script_openings = list(SCRIPT_OPEN_RE.finditer(text))
    if len(script_openings) > 1:
        add(script_openings[1].start(), "only one canonical motion controller is allowed")
    for match in script_openings:
        attrs = match.group("attrs")
        block = script_blocks.get(match.start())
        if attrs.strip().casefold() != "data-diagram-controls":
            add(match.start(), "only the canonical data-diagram-controls attribute is allowed")
        elif block is None:
            add(match.start(), "motion controller must have a closing script tag")
        else:
            digest = hashlib.sha256(
                _normalized_controller(block.group("body")).encode("utf-8")
            ).hexdigest()
            if digest != _CANONICAL_CONTROLLER_SHA256:
                add(
                    match.start(),
                    "motion controller must exactly match the canonical motion template",
                )
    return findings, approved_fonts


def _check_skin(text, allowed_fonts, palette=None, palette_rgb=None):
    """Skin-token findings. Regexes and rules verbatim from lint_text().

    M1-A: the palette arrives as a parameter and is never a module-global write
    (T-5-08) — None resolves to the style-guide default exactly as before, so a
    no-profile call IS the old call; a resolved profile supplies its own hex set
    and its own rgb set, so BOTH membership tests below are substituted (B3).
    """
    if palette is None:
        palette, palette_rgb = _PALETTE, _PALETTE_RGB
    findings = []

    def add(offset, severity, message):
        findings.append(("skin", severity, text.count("\n", 0, offset) + 1, message))

    for match in HEX_RE.finditer(text):
        value = match.group()
        normalized = _normalize_hex(value)
        if normalized == "#000000":
            add(match.start(), "error", f"pure black {value} is not allowed — use the ink token")
        elif normalized not in palette:
            add(match.start(), "error", f"{value} is not in the style-guide palette")
    for match in RGBA_RE.finditer(text):
        rgb = tuple(int(match.group(i)) for i in (1, 2, 3))
        if rgb not in palette_rgb:
            add(match.start(), "error", f"{match.group()} is not derived from an allowed palette color")
    for match in BLACK_RGB_RE.finditer(text):
        add(match.start(), "error", "pure black rgb(0,0,0) is not allowed")
    for match in FONT_CSS_RE.finditer(text):
        unsupported = _named_families(match.group(1), allowed_fonts)
        if unsupported:
            add(match.start(), "warning", "unsupported font family: " + ", ".join(unsupported))
    for match in FONT_ATTR_RE.finditer(text):
        unsupported = _named_families(match.group(2), allowed_fonts)
        if unsupported:
            add(match.start(), "warning", "unsupported font family: " + ", ".join(unsupported))
    return findings


def _check_a11y(parser):
    """Accessibility-minimum findings, distilled from lint_accessible_svgs().

    Dropped (no filename/template context model-side): the file-slug id match
    and the template-placeholder allowlist.
    """
    findings = []
    root = parser.svgs[0] if parser.svgs else None  # CR-01: root never aria-hidden-exempt
    naming_ids = {
        element["id"]
        for svg in parser.svgs
        if svg is root
        or (svg["attrs"].get("aria-hidden") or "").casefold() != "true"
        for element in svg["titles"] + svg["descs"]
        if element["id"]
    }
    for element_id in sorted(naming_ids):
        line, occurrences = parser.ids[element_id]
        if occurrences > 1:
            findings.append(("a11y", "error", line, f'duplicate accessible-name id="{element_id}" is not allowed'))
    for svg in parser.svgs:
        if svg is not root and (svg["attrs"].get("aria-hidden") or "").casefold() == "true":
            continue
        line = svg["line"]
        if (svg["attrs"].get("role") or "").casefold() != "img":
            findings.append(("a11y", "error", line, 'diagram <svg> must carry role="img"'))
        labelled_by = (svg["attrs"].get("aria-labelledby") or "").split()
        if not labelled_by:
            findings.append(("a11y", "error", line, "aria-labelledby must name the <title> and <desc>"))
        else:
            missing = [i for i in labelled_by if i not in svg["ids"]]
            if missing:
                findings.append(("a11y", "error", line, "aria-labelledby references missing id(s): " + ", ".join(missing)))
        title = svg["titles"][0] if svg["titles"] else None
        desc = svg["descs"][0] if svg["descs"] else None
        if title is None:
            findings.append(("a11y", "error", line, "diagram <svg> must contain a <title>"))
        else:
            if svg["first_child"] != "title":
                findings.append(("a11y", "warning", title["line"], "<title> should be the first child of <svg>"))
            title_text = "".join(title["text"]).strip()
            if not title_text:
                findings.append(("a11y", "error", title["line"], "<title> must not be empty"))
            elif len(title_text) > 60:
                findings.append(("a11y", "warning", title["line"], f"<title> is {len(title_text)} characters (60 max)"))
        if desc is None:
            findings.append(("a11y", "warning", line, "diagram <svg> should contain a <desc>"))
        elif not "".join(desc["text"]).strip():
            findings.append(("a11y", "warning", desc["line"], "<desc> must not be empty"))
        if svg["bare_ids"]:
            findings.append(("a11y", "error", line, 'bare id="title" and id="desc" are not allowed'))
        if labelled_by and title and desc and (title["id"] not in labelled_by or desc["id"] not in labelled_by):
            findings.append(("a11y", "error", line, "aria-labelledby must name the <title> and <desc> IDs"))
    return findings


def _check_geometry(parser):
    """Geometry-sanity findings: viewBox validity, negative sizes, non-finite
    numbers, and coordinates outside their own (innermost) viewBox.

    Provenance: lint-skin's lint_accessible_svgs() path, NOT verify-geometry.py
    — that file's label-mask-vs-node paint-order heuristic is deferred.
    Unparseable/%-bearing coordinates are "not checkable", never an error;
    bounds keep a 4 px tolerance for deliberate bleeds.
    """
    findings = []
    root = parser.svgs[0] if parser.svgs else None  # CR-01: as in _check_a11y
    for svg in parser.svgs:
        if svg is not root and (svg["attrs"].get("aria-hidden") or "").casefold() == "true":
            continue
        raw_viewbox = svg["viewbox_raw"]
        if not raw_viewbox:
            findings.append(("geometry", "error", svg["line"], "diagram <svg> must have a viewBox attribute"))
        elif svg["viewbox"] is None:
            findings.append(("geometry", "error", svg["line"],
                             f'viewBox "{raw_viewbox}" is not valid (expected "min-x min-y width height" with positive size)'))
    for line, tag, values, vb in parser.geometry:
        for name, value in values.items():
            if value is not None and not math.isfinite(value):
                findings.append(("geometry", "error", line, f"<{tag}> {name} is not a finite number"))
        for name in ("width", "height", "r", "rx", "ry"):
            value = values.get(name)
            if value is not None and value < 0:
                findings.append(("geometry", "error", line, f"<{tag}> negative {name}={value:g} is not allowed"))
        if vb is None:
            continue
        min_x, min_y, w, h = vb
        for names, base, size in ((("x", "x1", "x2", "cx"), min_x, w), (("y", "y1", "y2", "cy"), min_y, h)):
            for name in names:
                value = values.get(name)
                if value is None or not math.isfinite(value):
                    continue
                if value < base - 4 or value > base + size + 4:
                    findings.append(("geometry", "warning", line, f"<{tag}> {name}={value:g} lies outside the viewBox"))
                    break
    return findings


MAX_HTML_INPUT_BYTES = 512_000
_FAMILY_HEADINGS = (("skin", "Skin tokens"), ("geometry", "Geometry"),
                    ("a11y", "Accessibility"), ("egress", "Network egress"))
_MAX_PER_FAMILY = 8
_RETRY_LINE = "Fix the issues above and call validate_diagram again with the corrected HTML."


def _render(findings, profile_name=""):
    """Verdict-first markdown (D-01..D-04), capped per family (D-13).

    D-09: the first line names the active brand profile — `— brand profile:
    <slug>` — and only then; an empty name renders the plain PASS/FAIL line
    every no-profile caller has always seen.
    """
    errors = [f for f in findings if f[1] == "error"]
    warnings = [f for f in findings if f[1] == "warning"]
    suffix = f" — brand profile: {profile_name}" if profile_name else ""
    lines = [
        f"FAIL: {len(errors)} issue(s){suffix}" if errors else f"PASS: 0 issues{suffix}"
    ]
    if not errors and not warnings:
        return lines[0] + "\n\nNo design-system issues found — the diagram is ready to export.\n"
    for family, heading in _FAMILY_HEADINGS:
        family_findings = [f for f in findings if f[0] == family]
        if not family_findings:
            continue
        lines += ["", f"## {heading}"]
        for _family, severity, line, message in family_findings[:_MAX_PER_FAMILY]:
            lines.append(f"- **{severity}** (line {line}): {message}")
        hidden = len(family_findings) - _MAX_PER_FAMILY
        if hidden > 0:
            lines.append(f"- +{hidden} more in this family")
    if errors:
        lines += ["", _RETRY_LINE]
    return "\n".join(lines) + "\n"


def validate_html(html, palette=None, palette_rgb=None, profile_name=""):
    """Pure in-memory validation: regex + tokenizer only, no file/network I/O.

    The skin palette is handed in by the caller (M1-A): None means the
    style-guide default, a resolved brand profile means its own hex set —
    derived here in-body when the caller did not supply one (B3). The profile
    read itself lives in Tools._validate_impl, so this function stays I/O-free.
    """
    if not isinstance(html, str):
        return "Error: html must be a string containing the diagram markup."
    if not html.strip():
        return "Error: html is empty — pass the diagram markup to validate."
    if len(html.encode("utf-8")) > MAX_HTML_INPUT_BYTES:
        return (f"Error: html is larger than {MAX_HTML_INPUT_BYTES // 1000} KB "
                "— simplify or split the diagram before validating.")
    if "<svg" not in html.lower():
        return "Error: html contains no SVG diagram to validate."
    if palette is None:
        palette, palette_rgb = _PALETTE, _PALETTE_RGB
    elif palette_rgb is None:
        palette_rgb = _palette_rgb(palette)
    parser = _DiagramParser()
    parser.feed(html)
    parser.close()
    if not parser.svgs:
        # The substring gate also matches truncated markup (a bare `<svg>`, an
        # unclosed tag) and comment-only references, which never fire
        # handle_starttag and would render a vacuous PASS (D-14 forbids that).
        # A well-formed fragment missing </svg> still parses and keeps validating.
        return "Error: html contains no SVG diagram to validate."
    egress, approved_fonts = _check_egress(html)
    findings = _check_skin(html, _ALLOWED_FONTS | approved_fonts, palette, palette_rgb)
    findings += egress
    for line, tag, attr, value in parser.remote:
        findings.append(("egress", "error", line, f"external resource in <{tag}> {attr} is not allowed"))
    findings += _check_a11y(parser)
    findings += _check_geometry(parser)
    return _render(findings, profile_name)


# ── Export helpers (D-03/D-04/D-06) ──────────────────────────────────────────
#
# Pure string/regex contracts behind export_diagram(): the strict-slug filename
# sanitizer (D-04), the verbatim root-<svg> span extractor (D-06), and the
# svg-tier report state that both the success report and the PNG-absent notice
# render from (D-03). Call-time contract: pure work over the `html`/`name`
# arguments — no filesystem, network or subprocess access in this block at all.
# The only filesystem access in an export happens after the envelope ladder has
# passed (D-07), and the only network-adjacent step is the optional PNG render
# (D-09), which lives in the class body, not here.

EXPORT_FORMATS = ("html", "svg", "png")
_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_MAX_SLUG_CHARS = 80
_SVG_OPEN_RE = re.compile(r"<svg\b", re.IGNORECASE)
_SVG_CLOSE_RE = re.compile(r"</svg\s*>", re.IGNORECASE)
_SVG_TAG_RE = re.compile(r"</?svg\b", re.IGNORECASE)

# --- D-07: the url= fetch gate -------------------------------------------------
# `url=` is the project's first inbound-network surface: a model-supplied string
# reaches real network code. The gate is urlsplit FIRST, then resolve, then open
# — in that order, so nothing is opened until the authority is proven public.
# Host parsing is urllib.parse.urlsplit and nothing else: a hand-rolled regex is
# a named, defeated adversary (the prototype's regex allowed
# `https://example.com@127.0.0.1/` straight through and mangled `[::1]`).
_MAX_URL_BODY = 262144  # 256 KiB cap on the streamed read (T-5-SSRF5)
_URL_TIMEOUT = 5  # seconds (T-5-SSRF6)


class _BrandFetchError(Exception):
    """A url= refusal whose message is already the exact envelope to return.

    Raising (rather than returning a sentinel) keeps every branch of the gate a
    single early exit, and the REL-01 wrapper in apply_brand_profile turns it
    into a normal returned string — nothing propagates to the host.
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every 3xx instead of following it (T-5-SSRF4).

    Returning None is the documented stdlib interception point: urllib then
    raises HTTPError for the redirect status, which the gate turns into the
    redirect-refusal envelope. A redirect must never be followed — 30x is how
    a public-looking host pivots to an internal one.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _refuse_url_reason(candidate):
    """Why this url is refused, or None when it may be fetched.

    Pure string/parse work plus one getaddrinfo — no socket is ever connected.
    """
    if not isinstance(candidate, str) or not candidate.strip():
        return (
            "Error: url must be a non-empty string — pass a https token-file "
            "URL, or omit url and pass the tokens inline as tokens_json."
        )
    try:
        split = urllib.parse.urlsplit(candidate.strip())
        port = split.port  # raises ValueError on a non-numeric/out-of-range port
    except ValueError:
        return "Error: url rejected — the port is not a valid port number."
    if split.scheme.lower() != "https" or not split.hostname:
        return (
            "Error: url rejected — only https:// urls with a hostname are "
            "fetched (no http, no ftp, no hostless url)."
        )
    if split.username is not None or split.password is not None:
        return (
            "Error: url rejected — userinfo (user:pass@host) is not allowed; "
            "pass the bare https hostname."
        )
    try:
        infos = socket.getaddrinfo(
            split.hostname, port or 443, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as exc:
        return (
            "Error: url rejected — the hostname did not resolve "
            f"({type(exc).__name__})."
        )
    except OSError as exc:
        return (
            "Error: url rejected — the hostname could not be resolved "
            f"({type(exc).__name__}: {exc})."
        )
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        # The inverted is_global form is deliberate: a flag list (is_private/
        # is_loopback/is_link_local/...) misses CGNAT/shared space 100.64.0.0/10
        # (RFC 6598 — AWS VPCs, Tailscale, K8s), where all of those flags are
        # False. is_global is False for CGNAT, private, loopback, link-local,
        # reserved and unspecified space, so `not is_global` covers all of them
        # in one predicate; multicast is added explicitly.
        if not address.is_global or address.is_multicast:
            return (
                f"Error: url rejected — {split.hostname!r} resolves to a "
                f"non-public address ({address}) and is refused."
            )
    return None


def _read_body_capped(response, limit=_MAX_URL_BODY):
    """Stream at most `limit` bytes off an untrusted response (T-5-SSRF5).

    Never response.read(): that buffers the whole body before a cap can apply.
    """
    chunks = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _BrandFetchError(
                f"Error: url rejected — the response exceeds the "
                f"{limit}-byte cap; nothing was fetched or stored."
            )
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _fetch_brand_url(url, _opener=None):
    """Fetch https token JSON behind the full D-07 battery.

    `_opener` exists only so the harness can prove the failure translations
    (3xx, 404, URLError, OSError, non-JSON body) without any real network I/O.
    """
    reason = _refuse_url_reason(url)
    if reason is not None:
        raise _BrandFetchError(reason)
    split = urllib.parse.urlsplit(url.strip())
    request = urllib.request.Request(
        url.strip(), headers={"Accept": "application/json"}
    )
    try:
        if _opener is not None:
            response = _opener.open(request, timeout=_URL_TIMEOUT)
        else:
            # build_opener(_NoRedirect()) is what turns every 3xx into a
            # catchable HTTPError; urlopen() has no opener kwarg on 3.11.
            response = urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=_URL_TIMEOUT
            )
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise _BrandFetchError(
                "Error: url rejected — the server answered with a redirect "
                f"({exc.code}); redirects are not followed."
            ) from exc
        raise _BrandFetchError(
            f"Error: url rejected — the server returned HTTP {exc.code} "
            f"({exc.reason}); nothing was stored."
        ) from exc
    except urllib.error.URLError as exc:
        raise _BrandFetchError(
            "Error: url rejected — the request failed before an answer "
            f"({exc.reason}); nothing was stored."
        ) from exc
    except OSError as exc:
        raise _BrandFetchError(
            "Error: url rejected — the request failed "
            f"({type(exc).__name__}: {exc}); nothing was stored."
        ) from exc
    with response:
        body = _read_body_capped(response)
    try:
        json.loads(body)
    except ValueError as exc:
        raise _BrandFetchError(
            "Error: url rejected — the response body is not valid JSON "
            f"({exc}); nothing was stored."
        ) from exc
    return body


def _slug_name(name):
    # D-04: separator-free strict slug — lowercased, every run of characters
    # outside [a-z0-9-] collapsed to one dash, dashes stripped from both ends,
    # capped at 80 then right-stripped again. Non-string input coerces to ""
    # and falls back to "diagram". No dot and no slash survives, so path
    # traversal is impossible by construction.
    slug = _SLUG_RE.sub("-", (name if isinstance(name, str) else "").lower()).strip("-")
    return slug[:_MAX_SLUG_CHARS].rstrip("-") or "diagram"


def _profile_path(directory, user_id):
    """The one profile file for a user, slugged BEFORE the join (D-04/T-5-01):
    a hostile id such as "../../etc/passwd" becomes "etc-passwd.json" inside the
    directory — no dot and no slash survives _slug_name, so traversal is
    impossible by construction."""
    return os.path.join(directory, f"{_slug_name(user_id)}.json")


def _extract_root_svg(text):
    """Verbatim root <svg>…</svg> span, or None (D-06).

    The close is the LAST one, never the first: measured on the repo's corpus,
    searching for the first close truncates 9/174 files — every nested-<svg>
    diagram. The depth walk then rejects sibling roots and unbalanced spans, so
    only a single well-formed root is ever handed to the writer.
    """
    opening = _SVG_OPEN_RE.search(text)
    if opening is None:
        return None
    closes = list(_SVG_CLOSE_RE.finditer(text, opening.end()))
    if not closes:
        return None
    span = text[opening.start():closes[-1].end()]  # LAST close: nested svgs stay whole
    depth = roots = 0
    for match in _SVG_TAG_RE.finditer(span):
        if match.group()[1] == "/":
            depth -= 1
        else:
            depth += 1
            roots += depth == 1
    return span if roots == 1 and depth == 0 else None  # reject sibling roots / imbalance


def _svg_report_state(html):
    """("extractable", span) or ("absent", "") — one source of truth for the
    svg tier line, so the success report and the PNG-absent notice cannot
    diverge (D-03)."""
    span = _extract_root_svg(html)
    return ("extractable", span) if span else ("absent", "")


class Tools:
    """OpenWebUI tool surface for the diagram-design skill's 40 editorial diagram types.

    Only the four public async methods are model-callable: OpenWebUI exposes
    every public callable of a tool instance as a separate tool with its own
    schema, so every helper below is `_`-prefixed on purpose. Each public method
    delegates to a `_impl` seam and wraps it in the same never-crash handler
    (REL-01) — an exception is returned to the chat as an ``Error:`` markdown
    string, never propagated to the host. ``asyncio.CancelledError`` inherits
    from ``BaseException`` and is deliberately not caught, so OpenWebUI can
    still cancel an in-flight call.
    """

    class Valves(BaseModel):
        """Admin-level configuration.

        D-12 set the growth rule — each feature adds exactly the valves its own
        behaviour needs. Export executed it first (D-05): an output directory
        and an input size cap. The brand-profile store follows (D-03) with the
        one valve its own behaviour needs: where per-user profiles live.
        """

        # D-05: the only filesystem destination export may use, admin-owned.
        export_dir: str = Field(
            default="exports",
            description=(
                "The export_dir valve names the directory artifact files are "
                "written to. A relative path resolves against the OpenWebUI "
                "data dir (DATA_DIR) when that is resolvable, else the working "
                "directory; the directory is created on first export."
            ),
        )
        # D-05 / T-4-06: caps the accepted `html` input before any parse or I/O,
        # mirroring the validator's input cap. Read at call time, never cached.
        max_export_bytes: int = Field(
            default=2_000_000,
            description=(
                "Maximum accepted diagram markup size in bytes, mirroring the "
                "validator's input cap; larger markup returns an error envelope "
                "instead of being written to disk."
            ),
        )

        # D-03/BRND-01: the only filesystem destination the brand-profile store
        # may use, admin-owned. Read at call time, never cached in __init__.
        profiles_dir: str = Field(
            default="brand-profiles",
            description=(
                "The profiles_dir valve names the directory brand profiles are "
                "stored in, one JSON file per user. A relative path resolves "
                "against the OpenWebUI data dir (DATA_DIR) when that is "
                "resolvable, else the working directory; the directory is "
                "created on first store."
            ),
        )

    class UserValves(BaseModel):
        """Per-user configuration. Empty by decision (D-05): export shipped
        without needing a per-user preference, and D-13's "arrive later"
        framing is retired — no user-level valve is planned."""

    def __init__(self):
        self.valves = self.Valves()
        # D-14 — `citation` is the documented OpenWebUI tool flag for surfacing
        # which reference informed a response. It is inert in current releases,
        # but it stays set because briefs serve distilled reference content.
        self.citation = True
        # D-14 — `file_handler` is the documented flag telling OpenWebUI this
        # tool produces files. Export now writes an artifact per call, so it is
        # set. (Research A7 rates the dispatcher effect MEDIUM confidence; the
        # revert is this one line.)
        self.file_handler = True

    async def get_design_brief(self, diagram_type: str, __event_emitter__=None) -> str:
        """Serve the design-system brief for one diagram type as markdown.

        Returns that type's layout conventions, anti-patterns and example note,
        followed by the shared style-guide token, typography and spacing tables.
        Example diagram_type values are "sankey", "flowchart" and "treemap". An
        unknown key returns a message listing every valid key.

        :param diagram_type: one of the 40 diagram type keys, e.g. "sankey".
        """
        try:
            if not isinstance(diagram_type, str):
                return (
                    "Error: diagram_type must be a string — pass one of the keys "
                    "listed in the tool description."
                )
            return self._brief_impl(diagram_type)
        except Exception as exc:  # noqa: BLE001 — REL-01: never propagate to OpenWebUI
            return f"Error: internal failure — {type(exc).__name__}: {exc}"

    async def validate_diagram(
        self, html: str, __user__=None, __event_emitter__=None
    ) -> str:
        """Validate authored diagram HTML against the design-system rules.

        Checks four families — skin tokens (palette hex/rgba, pure black, font
        stack), geometry (viewBox, negative sizes, bounds), accessibility
        (svg role, title/desc, aria wiring) and network egress (external
        src/href, @import, url() and the script policy) — and returns a
        markdown report whose first line is the verdict, "PASS: 0 issues" or
        "FAIL: N issue(s)", with findings grouped under family headings and a
        retry line on any FAIL. When you have stored a brand profile with
        apply_brand_profile, the skin family is checked against your palette
        instead of the style-guide default and the first line names it.
        Pass the diagram markup itself: a full "<html>…</html>" document or a
        bare "<svg>…</svg>" fragment both work. Degenerate input returns a
        targeted "Error: …" message, never a PASS.

        :param html: the diagram markup to validate, e.g. an SVG document.
        """
        try:
            return self._validate_impl(html, __user__)
        except Exception as exc:  # noqa: BLE001 — REL-01: never propagate to OpenWebUI
            return f"Error: internal failure — {type(exc).__name__}: {exc}"

    async def export_diagram(
        self, html: str, format: str = "html", name: str = "diagram", __event_emitter__=None
    ) -> str:
        """Export a diagram as a working artifact file.

        Three tiers, one artifact per call. format="html" (the default) writes
        the markup byte-identically as a standalone .html page that opens in any
        browser. format="svg" writes only the root <svg>…</svg> element,
        verbatim, as a standalone .svg. format="png" renders the markup in a
        headless browser, which needs playwright on the host — without it the
        call still answers, with a notice and nothing written. The reply is a
        short markdown report naming the written file, its absolute path and its
        byte size, followed by a line for each tier saying how to get that tier,
        so a tier you did not request is one call away rather than a mystery.
        `name` becomes the filename and is reduced to a strict slug of
        [a-z0-9-], so it cannot escape the export directory; an existing file of
        the same name is overwritten. Export writes the markup as authored, so
        call validate_diagram first and fix what it reports.

        :param html: the diagram markup to export, e.g. a full HTML document.
        :param format: "html" (default), "svg" or "png".
        :param name: filename to write, e.g. "q3-pipeline" — slugified.
        """
        try:
            return await self._export_impl(html, format, name, __event_emitter__)
        except Exception as exc:  # noqa: BLE001 — REL-01: never propagate to OpenWebUI
            return f"Error: internal failure — {type(exc).__name__}: {exc}"

    async def apply_brand_profile(
        self, tokens_json: str = "", url: str = "", __user__=None, __event_emitter__=None
    ) -> str:
        """Apply a brand token profile so diagrams match a product's palette.

        Store a profile by passing tokens_json — a JSON object mapping any of
        the ten palette roles (paper, paper-2, ink, muted, soft, rule,
        rule-solid, accent, accent-tint, link) to hex values. The profile is
        saved for your user account and validate_diagram then checks diagrams
        against your palette instead of the style-guide default, so your brand
        colours pass and off-brand colours still fail. Call with no arguments
        to read the active profile, or pass tokens_json set to "{}" to clear it
        and return to the default palette. A payload with an unknown role or a
        malformed hex is rejected whole: nothing is stored and your previous
        profile stays active.

        :param tokens_json: JSON object of role to hex, "{}" to clear, omitted to read.
        :param url: https URL of a token file to fetch instead. Only https is
            fetched, redirects are refused, and the response is capped at 256 KiB.
        """
        try:
            return self._brand_impl(tokens_json, url, self._user_id(__user__))
        except Exception as exc:  # noqa: BLE001 — REL-01: never propagate to OpenWebUI
            return f"Error: internal failure — {type(exc).__name__}: {exc}"

    @staticmethod
    def _user_id(user):
        """The per-user profile key: `__user__["id"]` when the host passed a
        string, else "default". Only a str is trusted — a malformed context
        must degrade to the shared default profile, never raise (D-04), and the
        value is slugged before any path join (see _profile_path)."""
        if isinstance(user, dict) and isinstance(user.get("id"), str):
            return user["id"]
        return "default"

    def _brief_impl(self, diagram_type: str) -> str:
        """Digest lookup only — no filesystem or network access at call time."""
        types = self._DESIGN_DIGEST["types"]
        entry = types.get(diagram_type)
        if entry is None:
            valid = ", ".join(sorted(types))
            return (
                f"Error: unknown diagram_type {diagram_type!r}. "
                f"Valid types: {valid}."
            )
        return self._render_brief(entry)

    def _validate_impl(self, html: str, __user__: dict = None) -> str:
        """Skin/geometry/a11y/egress validation plus AT MOST ONE profile-file
        read per call (D-15 as narrowed in 05-02, a recorded deviation from
        D-08's literal "validate_diagram remains pure"). The read is gated by an
        existence pre-check, so a call with no profile active costs zero
        denylisted audit events (a stat call occurs — os.path.exists — but stat
        is not in the denylist); one sanctioned open read happens when a profile
        is active. The palette travels to validate_html as a parameter (M1-A),
        never a module-global write (T-5-08), and is resolved at call time from
        the caller's own file — never cached on the instance or the module
        (BRND-02/T-5-12)."""
        user = self._user_id(__user__)
        palette = palette_rgb = None
        profile_name = ""
        if os.path.exists(_profile_path(self._resolve_profiles_dir(), user)):
            tokens, _updated = self._load_profile(user)
            palette = _profile_palette(tokens)
            if palette:
                profile_name = _slug_name(user)
                palette_rgb = _palette_rgb(palette)
        return validate_html(html, palette, palette_rgb, profile_name)

    async def _export_impl(self, html, format, name, emit) -> str:
        """Envelope ladder, one write, then emit, then the tier report (D-03)."""
        if not isinstance(html, str):
            return "Error: html must be a string containing the diagram markup to export."
        if not html.strip():
            return "Error: html is empty — there is no diagram to export."
        # T-4-06: the cap is read at call time, before any parse or I/O, so an
        # oversized payload is refused as an envelope rather than buffered. The
        # guard is the same one `_resolve_export_dir` needs: the stdlib pydantic
        # stand-in performs no validation, so a non-int valve must fall back to
        # the default instead of raising on the comparison below.
        max_bytes = getattr(self.valves, "max_export_bytes", 2_000_000)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            max_bytes = 2_000_000
        if len(html.encode("utf-8")) > max_bytes:
            return (f"Error: html is larger than {max_bytes / 1_000_000:g} MB — "
                    "simplify the diagram or raise the max_export_bytes valve.")
        # Normalized before the allowlist check, so "HTML" and "svg " work; a
        # rejected value is reported as authored. "pdf" lands here too — there
        # is no fourth tier to grow into.
        raw_format = format
        fmt = (format if isinstance(format, str) else "").strip().lower()
        if fmt not in EXPORT_FORMATS:
            return f'Error: format must be one of "html", "svg", "png" — got {raw_format!r}.'
        if not isinstance(name, str):
            return 'Error: name must be a string, e.g. "q3-pipeline".'
        if "<svg" not in html.lower():
            # The validator's no-vacuous-PASS discipline: a comment-only or
            # attribute-embedded "<svg" would otherwise reach the writer. The
            # distinct extractability envelope below covers markup that does
            # contain an <svg but has no well-formed single root.
            return "Error: html contains no SVG diagram to export."
        # Extraction happens exactly once per call: `state` renders the svg tier
        # line and `span` is the svg artifact.
        state, span = _svg_report_state(html)
        if fmt == "svg" and state != "extractable":
            return ('Error: no extractable root <svg>…</svg> in the html — '
                    'export format="html" instead.')

        markup = span if fmt == "svg" else html
        directory = self._resolve_export_dir()
        # First side effect of the call, deliberately after the ladder (Pitfall
        # 7): every rejected input above leaves no directory and no file behind.
        os.makedirs(directory, exist_ok=True)
        filename = f"{_slug_name(name)}.{fmt}"
        path = os.path.join(directory, filename)

        if fmt == "png":
            rendered = await self._render_png(html, path)
            if not isinstance(rendered, int):
                # A2: report-shaped, never an Error: envelope — nothing was
                # written, the html tier is still one call away, and the png
                # line carries the reason (missing package or failed launch).
                first = ("PNG export unavailable — playwright is not installed on this host "
                         "(admin step: pip install playwright && playwright install chromium)")
                lines = [first if rendered is None else f"PNG export unavailable — {rendered}",
                         "Nothing was written.", ""]
                lines += self._tier_lines(fmt, path, None, state, f"- png: {self._png_note(rendered)}")
                return "\n".join(lines) + "\n"
            written = rendered
        else:
            # D-02: written as authored, byte for byte, fonts link included.
            # newline="\n" pins LF on Windows too — text mode would translate
            # \n to \r\n and the file would no longer be byte-identical.
            # Collisions overwrite (D-04) — the self-correct loop re-exports.
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(markup)
            written = len(markup.encode("utf-8"))

        # Best-effort and strictly after the write (D-08): the report is the
        # guaranteed delivery, so an emitter failure cannot fail an export whose
        # file is already on disk.
        await self._emit_files(emit, path, filename)

        # D-07: path + byte size, never the markup itself.
        lines = [f"Exported {filename} ({written:,} bytes) to {directory}.", ""]
        png_line = '- png: not requested — call again with format="png" to render one'
        lines += self._tier_lines(fmt, path, written, state, png_line)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _tier_lines(fmt, path, written, state, png_line):
        """The three tier lines every export report ends with (D-03): the
        requested tier's outcome, then an actionable line for each of the other
        two. `written` is None when the requested tier wrote nothing, which is
        how the PNG-absent report reaches its own png line."""
        lines = []
        for tier in EXPORT_FORMATS:
            if tier == fmt and written is not None:
                lines.append(f"- {tier}: {path} (written, {written:,} bytes)")
            elif tier == "html":
                lines.append('- html: not written — call again with format="html" for a standalone file')
            elif tier == "svg":
                lines.append(
                    '- svg: extractable — call again with format="svg" for a standalone .svg file'
                    if state == "extractable"
                    else "- svg: not extractable — the html has no well-formed root <svg>…</svg>"
                )
            else:
                lines.append(png_line)
        return lines

    def _resolve_export_dir(self) -> str:
        """D-05: absolute valve used as-is, else DATA_DIR, else CWD. Read at
        call time — OpenWebUI may re-apply stored valve config per dispatch or
        once at load, so caching it in __init__ would freeze a stale value."""
        # The isinstance guard: the stdlib pydantic stand-in performs no
        # validation, so a None or non-string valve must fall back to the
        # default rather than raise AttributeError at .strip().
        raw = getattr(self.valves, "export_dir", "")
        configured = (raw if isinstance(raw, str) else "").strip() or "exports"
        if os.path.isabs(configured):
            return configured
        return os.path.join(os.environ.get("DATA_DIR") or os.getcwd(), configured)

    def _resolve_profiles_dir(self) -> str:
        """D-03: absolute valve used as-is, else DATA_DIR, else CWD. Read at
        call time — OpenWebUI may re-apply stored valve config per dispatch or
        once at load, so caching it in __init__ would freeze a stale value, and
        BRND-02 forbids holding profile state on the instance at all."""
        # The isinstance guard: the stdlib pydantic stand-in performs no
        # validation, so a None or non-string valve must fall back to the
        # default rather than raise AttributeError at .strip().
        raw = getattr(self.valves, "profiles_dir", "")
        configured = (raw if isinstance(raw, str) else "").strip() or "brand-profiles"
        if os.path.isabs(configured):
            return configured
        return os.path.join(os.environ.get("DATA_DIR") or os.getcwd(), configured)

    @staticmethod
    async def _render_png(markup, path):
        """Render markup to PNG: None when playwright is absent, a message when
        the render fails, else the written byte count (D-09).

        Deliberate deviation from D-09's literal "sync API" wording, recorded
        here: the async API is imported inside the method, because playwright's
        sync API raises inside a running event loop (playwright-python #462) and
        OpenWebUI awaits tool methods. The import is call-time only — never
        module scope, never a docstring `requirements:` key — so a host without
        playwright loads this file unchanged and only the PNG tier degrades.
        """
        try:
            from playwright.async_api import async_playwright  # call time only (D-09)
        except ImportError:
            return None
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page(
                        viewport={"width": 1200, "height": 800}, device_scale_factor=2
                    )
                    # "load", never "networkidle" (Pitfall 5): the markup keeps
                    # its fonts link (D-02), which must not stall a firewalled
                    # host — fonts load opportunistically or not at all.
                    await page.set_content(markup, wait_until="load")
                    await page.screenshot(path=path, full_page=True)
                finally:
                    await browser.close()
            with open(path, "rb") as handle:
                return len(handle.read())
        except Exception as exc:  # noqa: BLE001 — D-09: a launch/render failure is the same notice
            # CR-01 (code review): a screenshot-then-raise leaves a partial file
            # behind — remove it so "Nothing was written." stays a true claim.
            try:
                os.unlink(path)
            except OSError:
                pass
            return f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _png_note(result):
        """Map a _render_png result onto the png tier line (D-03/A2)."""
        if result is None:
            return ("unavailable — playwright is not installed on this host (admin step: "
                    "pip install playwright && playwright install chromium)")
        if isinstance(result, int):
            return f"written, {result:,} bytes"
        return f"unavailable — {result}"

    @staticmethod
    async def _emit_files(emit, path: str, filename: str) -> None:
        """One best-effort `files` event per written file (D-08)."""
        if emit is None:
            return
        try:
            await emit({"type": "files",
                        "data": {"files": [{"type": "file", "name": filename, "url": path}]}})
        except Exception:  # noqa: BLE001 — D-08: report is the guaranteed delivery
            pass

    def _brand_impl(self, tokens_json: str, url: str, user_id: str = "default") -> str:
        """D-05's call grammar, in order: read, then clear, then store.

        Every rejection below happens before the first filesystem touch (the
        export ladder's discipline), so a rejected payload leaves no directory
        and no file behind, and the caller's last good profile stays active
        (D-02's reject-whole). The user id arrives already extracted by
        _user_id; it is slugged by _profile_path before any join.
        """
        # Arg checks first: non-string input is a targeted envelope, never a
        # traceback (REL-01) and never a silently ignored value.
        if not isinstance(tokens_json, str):
            return (
                "Error: tokens_json must be a string of JSON — pass an object mapping "
                f"role names to hex values, got {type(tokens_json).__name__}."
            )
        # url=None means "not provided" (the host omits it; case 40's purity
        # probe passes it explicitly), so only a genuinely wrong type is an
        # envelope — None falls through to the inline-tokens path below.
        if url is not None and not isinstance(url, str):
            return (
                "Error: url must be a string — pass a https token-file URL or omit it, "
                f"got {type(url).__name__}."
            )
        # D-07: a url call resolves through the full SSRF gate before its body
        # is allowed anywhere near the store. Every gate failure arrives as a
        # _BrandFetchError whose message is the envelope, so a refused url
        # touches no filesystem and leaves the caller's profile untouched.
        if url is not None and url.strip():
            try:
                body = _fetch_brand_url(url)
            except _BrandFetchError as exc:
                return str(exc)
            palette, ignored, rejected = self._parse_tokens(body)
            if rejected or not palette:
                detail = ", ".join(repr(key) for key in rejected) or "no palette role"
                return (
                    f"Error: the token file at {url.strip()} was rejected "
                    f"({detail}) — nothing was stored. Valid roles: "
                    f"{', '.join(_PROFILE_ROLES)}."
                )
            return self._store_profile(user_id, palette, ignored)
        requested = tokens_json.strip()
        if not requested:
            return self._read_profile(user_id)
        if requested == "{}":
            return self._clear_profile(user_id)
        palette, ignored, rejected = self._parse_tokens(requested)
        if rejected or not palette:
            detail = ", ".join(repr(key) for key in rejected) or "no palette role"
            return (
                f"Error: tokens_json rejected ({detail}) — nothing was stored. "
                f"Valid roles: {', '.join(_PROFILE_ROLES)}."
            )
        return self._store_profile(user_id, palette, ignored)

    def _parse_tokens(self, requested: str):
        """(palette, ignored, rejected) from one tokens_json payload.

        D-01 vs D-02's key classification: a non-scalar under an unknown key is
        a token group (typography, spacing, …) — tolerated and ignored, never
        stored, so the file stays a role → hex map. An unknown SCALAR key, and
        any malformed value under a real role name, lands in `rejected` and
        rejects the whole payload (D-02). Nothing here touches the filesystem.
        """
        try:
            parsed = json.loads(requested)
        except ValueError:
            return {}, [], ["not valid JSON"]
        if not isinstance(parsed, dict):
            return {}, [], [f"a JSON {type(parsed).__name__}"]
        palette: dict = {}
        ignored: list = []
        rejected: list = []
        for key, value in parsed.items():
            if key in _PROFILE_ROLES:
                normalized = _normalize_role_hex(value)
                if normalized is None:
                    rejected.append(key)
                else:
                    palette[key] = normalized
            elif isinstance(value, (dict, list)):
                ignored.append(key)
            else:
                rejected.append(key)
        return palette, ignored, rejected

    def _read_profile(self, user_id: str) -> str:
        """The read leg: the active profile as markdown, or the no-profile note (D-05/D-10)."""
        tokens, updated_at = self._load_profile(user_id)
        slug = _slug_name(user_id)
        if not tokens:
            return (
                "## Brand profile\n"
                "\n"
                "no profile active — validate_diagram is using the default style-guide "
                "palette. Pass tokens_json to store one, for example "
                '{\"paper\": \"#f5f5f5\", \"ink\": \"#0a0a0a\"}.\n'
            )
        lines = [
            f"## Brand profile (read-only) — `{slug}.json`",
            "",
            f"{len(tokens)} role(s) on file in the profiles_dir valve's directory:",
            "",
        ]
        lines.extend(f"- {role}: `{tokens[role]}`" for role in _PROFILE_ROLES if role in tokens)
        lines.extend(
            (
                "",
                f"- updated: {updated_at}",
                "- validate_diagram checks against this palette; hex values outside it still fail.",
            )
        )
        return "\n".join(lines) + "\n"

    def _store_profile(self, user_id: str, palette: dict, ignored: list) -> str:
        """Write `{tokens, updated_at}` atomically and report it (D-03/D-06/D-10)."""
        directory = self._resolve_profiles_dir()
        # First side effect of the call, deliberately after every rejection.
        os.makedirs(directory, exist_ok=True)
        path = _profile_path(directory, user_id)
        payload = json.dumps({"tokens": palette, "updated_at": _now_iso()}, ensure_ascii=False)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(tmp, path)  # D-06: atomic — a concurrent reader never sees a torn file
        stored = ", ".join(f"{role} `{palette[role]}`" for role in _PROFILE_ROLES if role in palette)
        lines = [
            f"## Brand profile stored to `{directory}/{_slug_name(user_id)}.json`",
            "",
            f"- {len(palette)} role(s): {stored}",
        ]
        if ignored:
            lines.append(
                "- ignored (not palette roles): "
                + ", ".join(f"`{key}`" for key in sorted(ignored))
                + " — only the ten roles above drive validation."
            )
        lines.append(
            "- validate_diagram now checks against this palette; off-palette hex still fails "
            "and pure #000000 stays banned."
        )
        return "\n".join(lines) + "\n"

    def _clear_profile(self, user_id: str) -> str:
        """The clear leg: remove this user's file and confirm (D-05/D-10)."""
        path = _profile_path(self._resolve_profiles_dir(), user_id)
        slug = _slug_name(user_id)
        try:
            os.remove(path)
        except OSError:
            return (
                f"No brand profile file to remove (`{slug}.json`) — validate_diagram is "
                "using the default style-guide palette."
            )
        return (
            "## Brand profile cleared\n"
            "\n"
            f"- removed `{slug}.json` from the profiles_dir valve's directory.\n"
            "- validate_diagram is back on the default style-guide palette.\n"
        )

    def _load_profile(self, user_id: str):
        """(tokens, updated_at), or (None, None) when there is no usable profile.

        A corrupt OWN file means "no profile active" — never an error, never
        another user's data (D-03/T-5-04): a missing directory, an unreadable
        file, undecodable JSON, a wrong top-level type, or a non-dict/empty
        tokens map all degrade to the default palette.
        """
        try:
            with open(
                _profile_path(self._resolve_profiles_dir(), user_id), encoding="utf-8"
            ) as handle:
                payload = json.load(handle)
            tokens = payload.get("tokens") if isinstance(payload, dict) else None
            if isinstance(tokens, dict) and tokens:
                return tokens, payload.get("updated_at")
        except (OSError, ValueError):
            pass
        return None, None

    def _render_brief(self, entry: dict) -> str:
        """Render one digest entry as markdown, style guide appended (D-01/D-02).

        Digest content is already markdown, so bullets are emitted verbatim —
        no escaping and no re-splitting of a bullet's own sub-item separators.
        """
        lines = [f"# {entry['name']}", "", f"**Best for:** {entry['best_for']}"]
        lines += ["", "## Layout conventions"]
        lines += [f"- {bullet}" for bullet in entry["layout_conventions"]]
        # Anti-patterns is omitted entirely when the entry has none; the corpus
        # minimum is zero, so an empty heading would be a lie.
        if entry["anti_patterns"]:
            lines += ["", "## Anti-patterns"]
            lines += [f"- {bullet}" for bullet in entry["anti_patterns"]]
        lines += ["", "## Examples", "", entry["examples"]]
        # D-02 — the shared style guide is appended to every brief, so one call
        # carries complete authoring context. No opt-out parameter.
        lines += ["", "## Style guide", "", self._render_style_guide()]
        return "\n".join(lines) + "\n"

    def _render_style_guide(self) -> str:
        """Render the shared style-guide block as three markdown tables."""
        style = self._DESIGN_DIGEST["style_guide"]
        tokens = self._md_table(
            [
                (role, spec["purpose"], spec["light"], spec["dark"])
                for role, spec in style["tokens"].items()
            ],
            ("token", "purpose", "light", "dark"),
        )
        typography = self._md_table(
            [
                (role, spec["family"], spec["size"], spec["weight"], spec["usage"])
                for role, spec in style["typography"].items()
            ],
            ("role", "family", "size", "weight", "usage"),
        )
        spacing = self._md_table(
            [
                (role, spec["value"], spec["use"])
                for role, spec in style["spacing"].items()
            ],
            ("token", "value", "use"),
        )
        return "\n\n".join(
            (
                "### Design tokens\n\n" + tokens,
                "### Typography\n\n" + typography,
                "### Spacing\n\n" + spacing,
            )
        )

    @staticmethod
    def _md_table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> str:
        """Render rows as a GitHub-flavoured markdown pipe table."""

        def cell(text: str) -> str:
            # The only escaping in this file: an unescaped pipe would be parsed
            # as a cell boundary. Bold lead-ins and backticks in cell content
            # are intentional markdown and stay untouched.
            return text.replace("|", "\\|")

        lines = [
            "| " + " | ".join(headers) + " |",
            "|" + "|".join([" --- "] * len(headers)) + "|",
        ]
        lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
        return "\n".join(lines)

    # `_DESIGN_DIGEST` is a CLASS attribute, and OpenWebUI keeps one Tools
    # instance for the whole process lifetime, so every user's call reads this
    # same object. It is generated content (see the markers below) and must be
    # treated as immutable: a runtime mutation would leak across users. It is
    # written only by scripts/build-owui-context.py, never by this class.
    # >>> GENERATED DESIGN DIGEST (scripts/build-owui-context.py) >>>
    _DESIGN_DIGEST: dict = json.loads(zlib.decompress(base64.b85decode(
        'c%0n53wIk=k|y|9@~}>=0T=+EqGhsFGqN7Gx@1W$%Jt|{A0YuG$ut2ZDl;Jp?&<#R8{ZcZ_ud34yJlurJ7<rpL=woyeZ-A-eDNQ9O}kwbAFej#yx2SYkG*D{&&t&me!Z&8Ieu;$`DX9g$D-b5v#P4+!>lQPDTZ0TnrBz}IxCxOUVbc_a=Iw8={7sbde?b9&+5&h=<f~pKIV%J9&xhwpToU6pO>3vw780oep0R4yj&G^Gt6pvd6V4pFu7-$4Chrfmv>d4iuB^s<l<%-FTPmji$&JvohNfNO<Pwt#b{n!=9@(u@A$E5+DyMp?>S8F!H-oki)YLAb>7HY+;iLbD7mq{E?4okclp&w=8;vKb|GsZORy;N%WNg{OCNDCmX(lawDN>igWL1jtXOf#^Sr*1OMZ8G@GyVyt=zOIudZ8sQcO>0$ER5@Z>wcF8@2iBs*n}tuB<opy5bO?SF?PP?PrJYH%AAD$4A*R|CFtZT6Q(Rs`Dkru5TN)W#^Uk)im!Nog54gj~@;nemguEA0G6R0qFO~4~B;a4~Gv<<okn%JMS)KXD+KcvlmQ-zKXh-+uq8X%hgS1k`M4-*1NqfTiJxAtVjCB-yO}54^NJ=-p_?RY?04yy6<~am&;rTxh+1m!)$GT>CWJ7i=Ee<=HEV;KkU4qc#!80I}iQg-McsYkKeq=WE~f>gYxX6Udfu~bzR+Z7Ryar%+m)?FJ}j{gRIw-3prVxDV$tBn13TvoXXaZnnm93?AJvxt5)+k3!`3rTQZk*E}NLX^WL=yul>x*CTo{p$(fVeuIg&Dn#UJ|ShGvn$?(?EQTpEFZ;y)y#~s3);lIw)pDM8li!pAHO|OL0gt_|j!By^(;De$6Uuf{?4h_oZC*KZFPs6v#=H+FLlb0#!HU5rKQ!UE*?wbz3Jsci>d&K`AjE^34R`BkgCYwfffm!KmhO&}P!6Q%JP+fM;^26fWhxsgh_&52(<8Qz1K64|xI4wYOQ|t(qNk33tv0YcPr0eS~`30zB#INz^WxgyI^2OfwMJc@Q$7)r{v%%QGb=RNdk4+;RE`L2HUFEA))#hzkt-zOV3mezT!ND+VYoQ7`v~s>r3-Sw`1Yv2NH@#R1ZEluw%(Dw&Lzmg0Z1Y7q8^jkMo^;;)Y+kliUE(A)BA}YATFH~TZ^zq;Z84QG{^q>CQ4ZIg-n;F(pl}e0_DT4+tzhz&2i-XyJ}j2QY_k?RpUEC#05}1oRlY3l8A7kgS4}^e<5B1JKWvuyYE&2bJcs<e`}R|vV`H2Bo0I&u^Y}MaO-5I>)vQ_!v!C9+9LiZL7V}K#qqt`=+p=90zq)bBT*lu#sAb2K_kl_LaCqmrMv#Tdu6hP0Gix_B-;}S~^20iB!8YZBzniuJxmd1*Bh*3xz4v<}IU8sj<#H{vlAE%&s^0JQGj8iic3l=V9t}NZvB=j=(Trsv-<9*SmX`_XI#14SuZxvXQ;~(Q@Zf6tv8*;tc3D@-h=jl52``JQLX@4$MI{GdbtQuWIcDv3;WAPlTjb-te;@8mi>Cb`5=|!FY@4=NW|d6lV_Dn^fz9(aAIW2G{XY3jL~0?watuouG(NekbD7(^T2xnC450XwFV_n!-d=NEt{XWI*|J=T^jeI_Aw+!p-(Vr~=jCRx7!~0Feaz*@ss&{&@~s@r4>G@xvZXxGvgF?jQ(w!ZWo=jTaRW`Rl?Ba2ILqz}nFSuxll757%ZkXt3n!#dNdCku&$G1<Y&rAKMAUwSB*Sc62y-Fo%}UN(aoOsHHD`l&R%`uaUfr(7*_(29!wrMhd0h$lmfV`Bb$)AdHk%f*?xq%IO_lvJBCnN^$aG}7E8S^aC_30YnJk_Z)E}j{OKL^b!_K(Vt}LYcwXWo^Vdra1UWBj=WXNgAS2u-xT+Bp6mRs;d4m@wJspr_Df(%7JlT8*XkR7axD~`4I=QmF%2RC)uZZpW<_0V?Y7a4v3+<1m)<+5<AEYDN~%NeMkNiFl{hBU~rL_r~<hXku^OSpV;CgreB^ZMSS`6HINz0OzJL)rJb$!^Q`TIfkfhSSj#NiBm1H_M?M)Iyb6Bde*q|4w#%CZsUWwj(m!o(L9MvApKll3NbKiJq5ON!dLNeVJda%620M4;v@++*B((@G(_KOnn)~c8IZoXB6NhP`R%<oa6mo_DF{2UvOZ@qo=$}4!x`{_R2=Fs<P%f7dn))DZkH)b$g8)p2#e&svvZ;8Uhk49)@x9;Qi*{V0s{@p)M|E3z}<rzh5upA%B*?K;v0tGclT4u$ES2!7eMY8Ova0NoIA?7TG^G*c&;b!VPA1)d-2}c<>1*uUr-+)>@2+#^=%qoyuZ;EEd&F=xHc}UA5N>(4TOFjS%f+k%I;@neZA!K9uF=sAobta#XUPnnL+!k<YIErQ1xbAECXb1Px~^5f`<be%dcQv)IA7Z*m0N+d`f}5-OX{5n2`vq=^r?s=*SK+isRqS-QZ^<T1iSpmFE3RwmKjR{BaI%WF}RWVJ43*^AYz@R!)bzYtd)3|`BAtW<G28w|2P7eeOXESz{x4(Vno>H*}3>(k@xhe#E?3}5DJ5#q8!vfR4*vmNTgMGMg}vG+0**^iG!LW&O)^1$Q=gExz+rQ8C;xe~&|_`r#?hX?CVS<jBAKE4kfvlTlsJLQ+3cD@vLot?-xFyXOs_}w?q#ATjM4zh#n@ZjJe6P0R`K`4G#ebUi<Cnt{-CgWUVBqye<aS6s*%6CkrkmG>FE10WgH7DIS>qXh>ryvI&t0unVww$-uc)mR3==6b%<o8*1Y2=B`$kt^mblt-zaBs+w-H5nZfs+WK&5A;aS%%dgXQDk~JJ0m4M~8=81NrXJH=^h1YyYNe1&0b1{$zHiDF@(}V1UB=HwzhSIKAWSf@WPd*)?axpq^wss!Nw*Hnp-92V+ri$Jt-?t!`%Exp(ds17<SJ4tEBsn~G6#FGr%JU#(=;QQOSq4H<3PA)fn5CY$x}8Og_%bM5ri*Qft=_vv8UYV6|_D?5>u8`_JYH)n@3I3c+MItc>FLuT2Y{+S#mK6Gc0`BX$-c=R|^X9w6kZvkUb*TZ=x(x3n{TItO6@!@(rTcrxzBjGmMrTc1hkF&?Zn&M+7d`|Y?VKY3K@XG9K`*dQ4dUxSvpYR@tC1KIVDJFlw=YO2&N<TbM^>q8_<UmdxejXj9f*eG?pFL4pegf);I1ifVy+R$#AfFY`t3c$-wP+eM;j@jN6CUDAIly%#ynS93&FZUG)C>%;T!SNFJ!D43n*OVgsvF}8_Art0(90-4p;a46#6u0$jytlD6{9{EUk$5|4^*9u!fwQ&$er`*@-n+BS_mILK%WeELe%$@dE|@ReA^_)Zgg4JjVf_*(a&9w*$Lq9Q_*qt=@S}B6I~BbCS4%>FnLw-VYX>>O(wxqw7Ywvb6u~v(PnW7S9$F-UPBH}{&0OL+b@r3GJCTQ<37y?3yF6O`I~MD_2%xH>QX)mmkVL~_4X@o7=qeu&buSWy!neBzWD4#?wi^8a{Kgy)tpVtXb;P9Em|^Un1~7?Pcczzs#`6L*QPLnnNX{pP@k<KRH*RkWQs)r8#N)>s}(e0IhMLkHkMnFNBXoUqC{8Apef%W6*Vg`C^yDNbFpIzu}7ZZ9(_BPPsM|1SE9T%IG3eRWHG;THuVx~0{3adv5!QYFUn$`yo}%CiBws25%p=JmWcUKsm-Bl(#YKMh0IJIv&HKu%}x$^+c+DIMmq}R$FkW79|u(n?cb`IvXr4fD-t<+bq&LzT&t`Ym_}LESNW=Ja<iSWQ%xALXjpRYMFSNEAX|k$WvgbQUxhzm0*aZ_Xs`eu35_=!Fz@UgI7Ggxl=eX&q>B;<c`0g8sYjdxT`%%h=CRCrkLRM1xdQNPC2C6nZfhT0%%E(E00^Hv(f8G&V!=>}a@U4RicghVP$`yOc2gd$dnqQ9JfX!L1_R6jEKn{NhAjW1aFK7Xiorm4=Vusacu;z_Q(N;3Xr2?(uH=lr--F)uANIkA+y9KeU;pR(y@^m4sCuq~WwQFfHJNbF<S&p^18ptOV9n&;9AOGS=o*O>E7#O5wvP_R<Kxqb*rVz$Nx#yMK5Re8K^l+4+eLMmq+iKkWoy39?4{X2RM82KzIuds)bPX}hWKIjsrPd~8;K4#B;o5rdN+M}1oR?!sj`VEoEZ1~o6{5W&1X@($xg4h(`E@qgKwS4Lzw14a`6dfZVU#prE-Lo|B!d~CS1I~|NQS`etHH|y|6jldmHD}BX@27UjEa!sUHik+b)VT(?;Zm>?Ig46f@GhdjsGFkR8TU-2DLp4H7eGF3$BfD;LN9wS7@6%eJM`MsG{LvTfm`d+u{#@DF!hP>zluJB5F2Xya3$$F-iDtd?W=uX5op@H$QEM-M0Abh+eJh8+)6M%LXJ_sfp69u$$eoD!JtumFPAu+B^9>yLF3%C2R|#`>q*<e?^HIQq?$0#{Ags#*kROPg<1PlUFf*YgFKiD<r9kGqd@j%+&GQ!usX(8i#eO-Ie_x>)A-s%CI-Va*B)z*4P~_vqg(oyI|g=yQTo61-H_qrOt-t?0s=7FTVi>Y7w;C3e~S_w%nOu&&pb^z%QNDbj+&nd6M!*tRmKES(e>h7G8#{5Km5ZvCpsp1mECwLg0sJsZQlAIpY<=;B{r(hJ?hy9$1QkzeNvH_PB`<iYzX5M?HVq<BqloxFaPJ$?4_*}G>G5PANw6d7GQr-q9|7*D~E7FBhFAqWkVuqybtVL*w&2!<TwFix3RrR74$qTISxZDv)K4K?@|!bLurd!t>_eJX;ljr&&e0+Ha?^uy&UXR5HsOYXQ+ESz3jU8qlZhOEG>ShpJ}Iq><I0i-8<eZ6fcVw4v2o6^18B!pdW65cWJ`vy)mwvZzH>{*D%<BzaS_V+9-mVd(OxtIrA5cLFZ&m9uNkRrk4D`hzP$vArfg2B+9K7A<*A}qz+h-V@T&Ev`!Vi$ST^ySbHVCCxD<@_lx7in(W<gWx_nM?L_ltP^wPTeo&A2K;Qm9a-<aH3l><~E3sEXG&kY@#0Zu?7$)eQa`SDIbZst3ekS%z^)#W0?+x)?!^EH|kH1j>PtV_4*wZzr~_YwFfIVdH49cm(PT0J(fdg(b1+5(uSfZimmDi>P?<+O_kO)?kdoi<tI6*M<+5c9HVO;TvItgfOXHnp;j}o$KWHKmov<HJI<bg@5^0hzI`C<`=+cn%X{E^7zSdEm(myTtz7Arceq1cRlUh(Q_f{ACXny=Y2M4;;nDD@&o4?jRIN}$`^%)SbT-@N81B)Oby<Q^8fKm7Iy1hwGopv<PiNWW&7bi6=lu7lf5pGAUSZ`IOd$}<8VeVL?%AYV-O!_OAWu~$rK?VJtdZ4R<<oK@JVN*7wXh$Jv!vD^HQn4e1{}AvAI%vV`O|bG92R!dg1&E67-aky6ZQ1ZEHM2T+2rf&Rp-GnuUBd&l?yooFkCDa5XR!cgeKpD5}QhAafiPqc9U?rwb>_^<;qlTyDX3`C43&pc^G{Sc1m{KMv0yJ;?>h<e|>f_$;@CK2G*`jJuuvz#pq4S`KJ$hSU=3DxD@pVCA7^yK8XD>-^|+PLvypC+J>8hr^6WPXN7}G?o_r^x@BC(@s6qi?kKhqbqpvR#TshwYPS7VjU&(Eg&7kGqJ>1O3?wjvR9awd+qx)_`#@KTtU8vT10*A+{yf{xgq?=y4itlTxGPZ&o>%oPlE+lGbNb}RvLpQ531nJeh$ciCF_XRI^Qo?Kr>8i8%6D~}We9aH=ZBk;===Q8+(7CiIOk^;HM-SJxG7XV=u&vZ-gKyh3fC2kURi>Mw%%*^upp)CQYb%FpYYjQPPZ73`%58+MX}!?T0ARKy++@N<)#LgYzqSO4B6>0sP4)uRK45Lm_k}oxWA0*lWY`2UhwIN)RpIM>6Tq7-@=0v`u1c5A@YyM<D+95hSP7d)p`m3h@sFET#EcGXBw&fL6|M2CRVlUuj0O$4^ipIHCP+n8qr_h?Bq<q#bUB{u|3D_%onl;f)Nz0>q)vcvd3iKtD>axK@SVJ31*Gc)MdUJl?YCy8m5yT8GPa<5T5%LI1Me+Ji8K0bXJ)9(OGJ$ajO>m2>y|BcI_$$G=iEBY<Q0GF=SZriM|X*Z|McWR>+CjRp>~e`Yx)j_<QJKL8Gq*u|7~(!@#tWSrQm5X5}RmLUkxL@Dd?_LTw7nelWnR;kRclP@p~DQG62V-85%eFYmigjLSY4boW|0gFK~|BdQ89oX|7(wgKCK5~^>e%2Z!r^G){lrhSfw+dLSE>UbG8G#AdD*MVrssBDTw^dUgzdC)M9N-4v90c1N3WiL5T!nFPluJ!LdY|)6@v7Gn)RG&k3LC295A?+i!n&|zqVKe2fda8{-Rl2bwS&}uDM27vvRM5A=>YzX6TRBl<*&1ZmYCeIiViKStpVU-~ZMtiWh@a`#2coU6z&&jJd^sa^_Hj-qtEx4U2n#Sl+}mg%2tk@u2q^zY8VEl$s^$%(WYHngL4xX1R^o$pr6(CT7(`8fWe~1VGC3WrG<>*zd23oYsTK-RhJ@!hGdwUzHgzwU$dWY==|XpeRrWTlDXhxFBg9Q}=r7oQ-3T`~>4C%(>4}FSZely@sblW6e@2D#6hS@4X>UtJ0v?>oud+548+o2^H*9y}Tbt!I^9SxIM7$W}fHjeg8>#L*tLjz3_`HR9J6bOzK42v7bzZu+$k!EgUqS^(lo)EzW6f6q5pUyDIW}sXIu2;u?z~Vz<e>+VU81Z*rKHo=JeH^QM`fq#b&kvlt9mUH5l-qn-#D@fps$*Q&8g~ju5LxA>tcb>M&-grpQ{pYFkqUxoa<bj0^oW&pa+?L1txEv>TsEC;wXEuGGXo7L82DmYG&z%kfIm`A)qKXKXXaH4<1_2N1&&z-x^Jb<4O7ies+A|57G#@KWhKUqT0-pLE=3xk*e;Rm5}*W#T9X(*?rlsM?e`3;E9k$dMxt2YSdo<{yV}WouSyN%CB;8+(e;+2O$X5!2gtcSV-D-RZfZdOKl9u%wsy%<iYR+3OdhdJrW9i^nPzsFM7Yz{8_!N|9(#{f;Y23L?BnwjOSa=y3E1eNLrJFEBZ@^H_&#(3G(O*b5qLsd|`eJz06#-egfJMe07(KhJIW<LFhysy*tNu^5kz%#H6>wBK#M}*lRZ6T;;)lxYyI!`gX_*HexuK!&8N7S$kI3szcv_xciB7*TUpRQ&p`*VAe`_*|I4XA5AmC7e}_zZh|6_L3r-UZ@fh|wMYD#G*pj+t}%3Zioe}2guZR=>!iRTFxW_I*a0k2XWhhnO%Hv!y3a(SQ8Y7uCEVss`6+OrAHl%EM4|_T18$(Ukl~T@(O44uZ%LCvlqwRKJ1i_3T`Z=M8a;Z-`@#KCU0!N30oQhzooc`fc_})Jk!Jhw9Z)P6@@jK3x=*GztxH*j{p*VH8wy;gVxoQtmqaRv$Q}4+>`3-Bmqy(&;5B_ye;LPHW3c22)c3%|MReg4H44K@z+%Ek5~5$>2<j2`^Z_MdN{QK`T(8}>N1;S5oDNAdb4#;K%;30B-(#Q54qjB-rIpd#v4h_hbF+l$_^7T*jl0A=Nj-Dd!m88>Fs&ANcePTD)S^f{8X^C)jOVVWHURE-djJ`o!ze220y<=lg@@ibp~jhxKs3)uYCiv4bSY!08Hm)-Z_Lkhh8<Etmqorxtl^xakiA3BZ{c3nV)e0<L&wP=;X%l)5`EJj5~VTKS=BL1B;N5H)NOew+P1mM+>*+J+1ch-%7Xj@n%&z@inSU;=diUmE4vPQlsr;(gWJ4mwREUscVib<Jny!gsR14{7K2idhwD^BLU;heZzMCC!%Zxc69s>>R)tJGL7D|3R4_ND<{S_TCDjZHv|x9M#Hql?EwEph%8;1rzQRJ1*UdP4V=4S(O~_&mT+8AkI-5w-zHYiRF@Ye7D}di8`??!8FpU?XZCWgNv3jjMkr_t9oJD{98TXYG6bMY|(OpH+l?=w;ziZ37-yTIVx8xXp>j2r}!S#nF6%P03ALXBQHMj7C930V?=F!0AA)LE1c|RSG!dNl##+>I)2b%fHr$@TWjiwuxeHGO65noK$L%wQmnVp~px@h1xv^4P7`@QpYvvw{;uCfrRJ$RL+oTBtUlc@}yY3et3Pp3Td4p$k!-vggY&lv1t@GXlCjqYJ!?i+|AzO&in(Vd0kC*h!{&YQ%f{&m)NE>HYlX2v1&7bj5E^rd{%S^2gc8Q=e6CilN-C_nnJe}lb>n1{fGYKkK$?<_W>TRAkNLwp8zfm%<C9G()*Sb!Dwa57YlD-9eBT^$P_9*NrTD)5P@z<AV-3fYrkeIq|FnOlx^(8)FZ#9QuFQsyf+VcnGEEx`(g{Yd|yn~(oQ*@9~LD&LqaO|Cu|(4`lb(z6mQb!)Z5j+-hZrmd{VG^5H8UbPH43}@N&%FP&Z1^>eyZhzVH>@U8p?@^1(Iu9ONewF5X>1qD+k7Qode0Nw^w14q>m5;7T-gN<7{OQHZm-Iw2(^tI!|5OZcFc1kLBl9>QoB#~JOy0bHIuTl%{P{=z`0??p$KOBGkDfezHE}1~&VE{QVk+~~(37$^J%K!+^u6R-M+(AI9|FRLBXB^?EWuz;?h^BZgqYQ=9I`yfIfWe4t0IxRpA94SFab|>?u*da+!NqR)RnB)Nsb<78l(MqG(H(0>RB|8%1Re>*XIqjrANy!M4?tZXjl2Vug<FO9WHRNdb4V@sI-a(epI2784{;)HjRJD2nK_<9YqF~wdLw;R@C@j$%EE(O$Wcvucmyf_oX-abj<KGsS{@#mf5c7$B6?k{OYdH=zqr5qvjYhe4~E{src(O&*%LfpGUnFDvudAZW{2(BJt=63a4eqK@=?463GZKWz>*|R~m$)Bsv(p>L_Qju&--TeKh0P3^O;#l>gj$;ZU!`mWh>=2eob_matVMs%3~o@_U72e>aM`zM~@Ei8#yNp<4ao?BwJag5%xGi?atO$45%An2lv@z~m!6bTv8Igb8ZoCnHp+pOb6*J@60&Hjle_EAG&PDQ{@DtC@zFNS9hehb2^&p7>y}$kCw33ctJ8pbx%LO*5mZII1p3mwCAW(=~s>s<>TjNAex@SD6205o2uXYl)}rNbENy-k6|j$4YAf-#)zv)k?ht8-d(u*!6kzL;2J0tUXPbX2di+$(>RC9sAX7Rama;QBsEZnUwyEYE#d&7HsBOuvuE9<yEqC7fob3YRez9_O4uk-L%EBQR^$z<rSzBQ?=^$8fm(To6Uk(np4}+hh-#)0*b8oh|-Ho)+ycQn#YHs$ha(R0YnE5%q&Odo4ns^bV725;q}_n8WT+ne!mwbjD~q`5m57T6F(tXZ9gUFnb#9Tn@y`aMdMsHjP9knu@y~;io!fV>M8OsBn|w!a?@h;s(fA|W&R2!U!xz()r;4|Y+E2)F#4CxdMmrrPZc4a5F2)=S&ntg=P@AwrkW*wl1OAj^4e;nhbpbaAeD7F%4VJ<$<#=hj}}bKHX7edN_@><cY>0uI~5s6UTzVt->n|7=ww?Ntz2yo_kO<@H^p~ujj%U}LsqVV<@3j*Oy<~_r%|)nQ7OMHlo1P=?l8>5f1&~)4E3HG05X#g`DZEto>vt_1c(NCo>~iG5mAmBTMP+5%pN~=UGH(JsWsT7AUM=BxmhtE)WVsnd59v&XrufC8Z2NsGM+7uSLMzDHOydPKyHK(PEx(x885sMb4v-_A**Bw{E90#y7kGrIp(n*XP)-Ks2?A-EE{22b<vPmNQEprUJAFcP|}-gX#KP6;$w|sWsA+3P8f&KvKUkI39w<4>%ayk_m*NrXJmXx;-I{@B;x{^&`cF*JngTl&$B=<pp%%H`zr+-_mod)PENui?B(Wk>C`9$MHiO1@~lh~GT@8Yz+2?*KIh9>v#CY3E9?T<6R()SRY9&Z7urZOzmb9du$hkJx>2)+0_>?SbxyA+{L`xth8B7HG#w^tJwXNMOl&Wg#r~Rm3E%PmS0xIEM+XPHwF&ADnuHJK;${A+#|+0yo`MfnnEG}0@T5<2dgEI`{t9zQl+x3KY>PU@+efG0o@UpNWI;b%Pt>F|JGGN4@3dU4&HA)b20Zh__Uq$`B16#XVS=K*=j324*7U)|3t>B7qV{z>K0G`XbK4EpO5~D!z)&qy&Xr>JBXQ8$Y%8y&_|^<~9MOjlGcDen_`Z&D^#mCuROmJIWCxndfjW*hSdvHMLlGEt0|=%Cs)O6>rAWpKk@T(5Rlf4%`Uxj~J<)t;^`(IxCpYD4ju6G|WT@Kvr;%26>N!P*BD{WUdf{wUITDuQx)3@V9~p=DdWWM>>yV19$=&)0%6o=wWc*j<N<liv-Etq^G#3^SPrugES?z?Z)YVlX-}hAU8YaaL$s69)<*IVtA2x^Y?L5vD!-)n^!TpPoL0wo)gmG`R3}SM$Cf)?Sz{r{}z@|;|O?!=q<#wSqNF@uzZ*8i29GF@ig;$vKKT$t%^xO3lPa+fkXKN@B1WS1L|4jYJZrQ|}lnI+6?9do;Ze=4seMTAXR+<`F$qs*Bry&Z*Ko(#i3RJ13xKJOEdm@7q9p><^4&%wtQBtim|1rKthsih)N|#N1oktK83Q5_A?reo2c*&Y$$CxTm7X_K!MCGXgAS7~gQ9;N5DD#`b1Ef|Qn059)vicVPK-K*p2)7jzRof?g$b=4sOXMZ-4Wj0)AvET!ghC{LR~aLeTRkn?ntp#7e^BY6IYXI8piljL;ESq8voJF8jWHPN4@cyNrNlTP<Loc6H~dL7KKxnNX@+6TY@=3VoTEa~`F<%ZciUBM#TC&FS@GZ?xmO4L6N8ZxtjY(ngNa&CX=xbd`+o1;cTZCTgttr>R%^0>#lV|J^VDdyAPU`hoNKeXSyi{I&s0t$C1phfn<-L1tC`mFlr4kMq^)y{C0Q{W66%-J@@fMpqXv*9yQ=-ERdR+A?_efyMWBtC%6korD!nKB*aU@Erp3TW-_Nq5BVg4|J?DG()zste8WcJJWR+D*BsunwY6H*RJ}=+2_^q1a&6eISe_vs1SID-!X9P&=mGhKtWwP0@S$ru6V*d@2JyEeGCk%n)REN#mG$l3<=aJsUe{6^{;>IHNd3zbcB|-@Bd=6A*va%JP!Fd%EclkzwBHgEf`f!<=$fLBtn&?q-u50`$oeNLo{TgacND9-iY+RPuA1;m0?)|CQ3M<KPh8Yc8p~u-xA&RQX#hJW8G~!`qcCM(V!%XdAyzb~!{UC}Nqvw@xvU!MArEnfSYN;&qMHVu$C7NNz4@0@H{T-`)?SoM48{TPkBm~lE%&o@5h4aga#-pfMHCAD~pFIbI+|S<b{{{boL+)o6&t5)z@=ktvSzYm>SAMwr?rOJadw{C&fESZ!b`C!oHmd#Y#4mFsU3-F{3M8S_%SDcS(fg#3pV0Sv-31_Vy?*ubZ`nl0Jc&g+`PCIl79oRe(_(alULn(L^fmAI`u_)4EGI?DS?|PHlveu!@h0kxrnY^1bl_h<I6CMjh0Me_q<$@rx_<QFXaZ%&iT~PCy|GIQkO@zpyOo4zj#wo%iFfP^ryK{e4jDvmvBXm((a0v*Gc<iq(=y-z`C4upT^SzRys&_8Yp`K$t_A@SvuwzRCkN3cI1$UpO0AAh$Jqo|yN&cn_~L=Qptt%E7YEwk;n87Vllb}l@##d%B7Hh)AaVX4iQ!n&sEJ!z_%c*Oh^db}yKY#47s_BTh7T~rKFK@7AB-)_h{PV8MD<&bXJ-VvQB1jB{yiOfq9L^uo;^)(Q9I*=JdxR5(F1XRku~3jz~?VTUG*J3hM)SoJ&{n2nZxl3QZ>{wY~WYeA(^Xe!-uWbrXN0>oQt`znTm~2US{TE>ECzh@qumxtJN!0owZJ%73^n|AohJeXL5X?fzWyeQFT-pu1U{5O_TR~frZKS{b(daR}5eMPgn@Ju&-ImNm5tz)SD*kaiNPpf1hA0MzDYs4`5;_Cb!r_DuS-$hLjL7y~JSKty6^11M&||;{<Ntibuy*B{>4vkHBqqFC$mETf_Y(qVk#P0VvY@QS(Ag)yiGkSQ^sx^FOI;ie5E+=e+u*ScRCiaLbG(Phwv!WN%Qw95cRNtiZ5E2-2L(>p5!RSR!n2tsxMl>Gzj0&%G`dT2w0xon|RuK@s(u&(#YmN{z5{<0Tp_4{1%B5$)e?Ll#mAwzj%cp^}s^Lk;i*4uc_?6T}t}%5oIc|B((3$eF4a>ZLJn;!dqEQ?xONK@rgLpz+6r8GIpeM#aN@vrsi4C0qxQ#YC33m;Vk1XK7si*{gRi-u-QY^PkjvhiKlIcYasRx42KiTw%6!!+m;maApYgn7#QEK?qrrtCamaIbP52e5#U?T<2}N2WJKf5Xs5bm$#*q=co+@95176!W5NP_y^7}{5%|w2b105J_K@_C2JiXSlTK=^Fed5#emNPYw+4-OiC*zse14C#Nd0s2i3@x_Lg9=0d>q$i4_QQXaJ~u6cSi`cC?<)!y#-c%_HiNCk=*>(QP>HRO=PdQ$;Dxi@h_bV8ZGvCj4k&Sg7x}V``rE$aJ2)6-uNI@FTtg$GI$<>#1z@Z#q66WK(9b*ce;{h^F{4Z#=?!3Ux+^5UI7QuR2O!_7?;avdIt69zUIgn5OG&D*oE)s?aK|70S96<+MPi-(owA$b3i^bO4HIa{4UHV7u5%N3(p>crt5U(2%v5tg6<U&+y(tqwn`PJy*jUe?SNTzEARf<v=zYBm)SmYtnKxW-rbyrxONH9%C+#lNUfAi0|O)JzhC*nydchj<>*bWQ9M@lYCoQr^2Sj7syIkZYZ#AWyJ#gM2$p>rdk%t^|ZppYfTj7>TYo*DU8F!DB*#)tx~AaoRGOKgAtcyy@WV5^bTC<&L*ls!mCk<H`nJSY5py*^_+d~qXz8n=JAUOIUh|6MKis$_<RI+=J8-Mq-;;>R8t$?>BQ{XV0X)bv5>!R25=wCl?9}E_(4Y3h>WZ>Hllt;<;>+$aj)*N<Rg}Wy|`=)i;&bQFQCSl6_=L=6ND|EUDv|X9KLxlc$0g{xRUS!sqVAv^heRhmN2%(lKU}V?f;lVS1shH9AObN`#;KFS9AH1H!?TS?*6lwDri<`hwe&Ufme_i$u>vxkD8N=<fELa@+tup%QMlu2-_;g>g<Lh<u)(XomRmzHG@TF!IT>F*K_7f`B`7XAv(7#1F`aXdf-FZdpt)*|N7f+vwTt10N~})i{yzF)b#Iw`SOWML3k|BzSUvh0$QOcR?&&kY-Re@a!Be7%Y*=$40OimXE)J9b1{U_w|5Y`Jy~zgV`Cg77aH7(ilvtN&M1dY2{hQu4Q?D@n6L<76*7$)c!eG0T%Bhq?h~u7V44@fgyIy|B6db{w81u$#984FR@k_UT84_`L0Gf6-^F|vrVPs2f{@2wECKdn4YA9Ux`TiU-~_RUhhj^K@bBLb5N;Ku0V9**6ZZGt<i-{r7K;LTVjZ<^Sn=lPMe1k~2p7qZ`b2#^FH8#X?_^u@G~G?<VhNbh$H*Q};#Hx;sF^|0l1IvWBZzeAsjWei2*6lcS*}{zU~qb%sM4r%va;1RK`nAa4WXq$;8SL~8b)$A4yRFu)*NKyY@P&IwoTT7TJ^<2!CMWr&T93&ug-`uDIV2c@D7lnXhNy2<mbU^c7;9`=7ONp330TH?Dvk3peIgH9DuT?8fY%@P)#l3t7~QTNj_Q7(dw$b9_qFCs|xo(c{BzMu+HtkRBN@TTu;N$c5p{S`4;DLZ?@rK)?0>xu+Nux^%ZWA3+ix>c&U?tsdbTDx0V%WiCAAD*;aH8%m5N?2^?IooG?UT0LW|=($YtPYT~Nsx+C~97@RX+=eSL7<C7YKfb3R}<eBvXL3o2oQ0@q>g+p`maQ21n;kF^O{QPR{*7~u4IjQ7#8Et$`HJU^TI#W6*Ej7{HH^Wfz7ag~v1#4V4N$`s`tla`qThJ&8*$8U8Fsx{r+qXH=`0qSyxcjU=^7D}W?RZ$R78!g=t+{tZM#;-MV3<UCHT6CT$#^^$NT0~y-;Qo}ZvF_}+$jsuWDRs>Km(TGsou){P{q4%XVK{2937fLFME?<7nu*1%*y`=w99XHXwGBp>6QdH1itA`klFb39sy2AZPnNjVhGy}S7Onz&+B0RscU2=1d0e_Vs1Tmn%>V|h%CIxZ}Y4dSGrI8OSmgR#_u)2Tx(r;#vlZ<rn{{|1$d9B4|>d@r_zh|)C+E8`<DPUQT)=c5u$Fxaj&T>@C?e6WzH~O?T#j?C?m{<Jy3ZjOs$;|lV3pZ%rGW|G%P=?N0)34XZLen3zne1-@8zd^cNa-`EO!p!~HO==oPqf;78~~YbH6jq99zcbh!9Z*+v&!W0!GQcNJ3H7IntF{+Kh-k4fwYlOVX%)xJ-TKVn+F_3H48Pt$9QwOpF3p9fbz)!m4Z#IVj<H+yodTiWN<+N>@p+BZTKt-eOYYIqCHFiW~s=a+A)P{hyjG2D5fy~<86mFJo+VpKBB;zDTxMBAKYe~XwH<iSIPx)i6~6PGp~6)?Wx_5bSNjHsBTLSzZ19uQb!Tg`$VRQ13=w#;NJ)mo`B)|Lb<vPJ8t@&TsNzotu6ZCp#JU_AgOjuz3n-jdfd(SVn0JcCYVovjx;gH<}lQ->vl<1T0aN+|CBbDu=KkA!bE&|K*MX!N9l)rsA=vh-}vN6=;lNW`-wY4}&j4pKi3T--t*3oW;56h(^Jdr9vVD}OLv5~`m=nu(#+acF>>7i}b;&ovE6+j}&PuKjs{?~YBtnB^p<;YYQ^tq%nA*Kk<&aa87t7id^=0dl{$b`T}|%6dtVIaVkbXO*65%<yw-5*D1%9$q!okxqbxH`9orh&B|0C~C9N=AAmzbzS`o;i0>b6P`AoqXSb=rMNa~VS5&(Mcp7=eTKte`o-P*EktS9X0ZJANa#)tclXumS%K%(l0wxrVO??@Y3hraY|M4EI3NS~5bt1=>Z(QVX73QKL(moj8?b|n*Xidl&NOw{tMQst=SG>KRS-652M4aR;f1)%1`Q^BAU2Y3Ph}+EoZ3jx28Z-@pCPFadmF$h*_s1T2{=;Nf47f>daK!N!(yrHM})Ktj$|Wa;xVJf2C15B^&<A;XgP>`I#kFpyxC_Lp0F6L&gSb<L)C08GR(G*9-L-98tu9!ie^SyhyHvYUT4Lu)$OCxlQdu3p#aWU^e3zc2*=Dh)FeErt8VJTmy;H8u<4YS8*^Jo@n1CzOAmv^-7Nv#trS!c3QIP-)70dsA9Uk8{TmPZUF4YO@x*u9bllsbLz#p&<XV(~86&H#V{(NS=E&3fX*-WI!+NG7rwu)Z_E87Lrkdwlo;5$S3M7M3g@|}?T3Ef4+9In6Pijt?O-)QP(1)<db=8#Wi;lABdx)Rd<0B^3-z$=yXqLWZAB13XTJYwJV@WIPCZw}CG-xG^W97Z{jMW)XRml4#28q=KczW+Q+3ya6_A;%JzHW|R9nL6kMyk8N-(xIMI3Q1FXqe;=)WYA&9jO`+?I{o41{gM2=B0YY%w4CR9mbiv@-!xdQX{u9GRJBE;+TGK=HVM|kLIOaZ>D{Q8JQqzDY6b69PpQZ2>}Ck6ZJ%L*8{x;oO_~^S{Uwt{M{6377Oh96i&>hPo>AJJe2l#FNr05VZSr&LBUPU7o99?${mW&wL!tsV$@KP#(M7En+N~_(-d>d>;2D^9|eWE@Lco<yo>h2MsYm9%hT*O1(DJH*Y$kw9m1gbwqBc|>D)~~Fwu2Ra_a$NLnC!V0Hr@3r*n^UkUhRH6HjP#)K(*t|GA~uhc1s{fmc^>xK^QCg_Dl`DYCo{m|S!*%UTA@*7|k)lDCP@o77CRmc={e#`z1a_Wrb)3iTD#OrAZtfabo04uy24sX7@=Tby+`!Z1Zeh;R<w#btk{P*J2A=LX8?3=YLmUPnwa{J2Xdn(V<S$_{OaK2xLV;p_CJ!|cKmEr!|aGzaucWu{s4b2~|F)i9oYnrME6`Ac<b!b2zcCKfehUx&n_Z=jA?m}>`9w!0IKxns_ZkOJjbO?Uz)Nw1W(*4LeIC$xeS)ik*E9o5gfOfc&mp?IY4Der?pd3gziV70xwV=UO8EpnF9iG8&NaZS|yB4YHzZT2Yp_Vi?Oj;c-s^LM^O;mf^wR$9(6vBGM3vVXwD=%#360(5LR?P*8Uf2_a*513d;)JOeZj1iu>_j@~qBHA4=U`x4MEJ}gTRCILF%-{x0VBsbv?f>7YN{Mf4mYS&b&dt?ufY|i1WD|-$3oROgJ6N5FN$QAz`p4{e2eyxKdU=2$%?()%XkJL$|FA>*WRO<iG5G@&r$1WL81GoNQ?lo!AA^B9=8W1np2;7ed&)gt0-~!3{qhbqLQG(0hi8YVT2=!sx_~EKR+-48vpVTY_wJK8ay%!CV%78aHT~?-BZLfkurHB~gWvhg8E{V~-Rv@}OnZR*OP;U~Tclkg-|r9#tub-rmU(SCr74jf{1AtyTSrJ+b8}9F&N?N1M2dX(xuPDM<MH^UZ-E0ZF^ZDoYelpAnzq_`x?jZmyW93EXOf;587K9cL4JNNKhP=G!vjEjEAEu}ew+19MgYO6%lUcCI@~GyKS$frO6S8Af^H8rbn|mds5wvixAOOi1s6s~pB9rlHAXB@e@P`;RO(Vh7~*%INv_%HGUrJ&uk!YyW!6e8_{VtzarQKt+`6EDSzP6_E!uSFi@xEL7PsmbmZ?q4Ia@Axv|9_8?+cH3>l~}NAScqy3?Mjq{X+|UpcVst14D<7Z#Os)L#;%ss;K6!8|zkUF)oAPF=QJ}cV@$cBoJf<Y*e9$$bp0HOOsq%uCxY1EHH*iA`T{VR5c(Y`kT-e8tAQ}H%=R>dDCNSDMYh7bn;B!G6b;Zs41>MI~a)FYo%5;{Q+G&gS(ILK1xLmCt5ipijLZ^Vk!si^>`d{FQ*#?&`G^q3hwET=j^Xb$J*4SN2rx^D2*$nnb@4%`l`(Aa&;$(vLQw`Z8C!rEx6$Ba}d_bl{6T0Bmuyu>RVxnmJ~YQ;LJ^)e#dam<2P^k^VwhDP3-3L7*4bR9rTkU9#t<Q(1&O5(pSP>!6C)@U2BVn+)L2m7Qpx{R_r0WU>@w@f#dgkNqxA*Q0^=F$E?&9kQDcyYuo~AgjbV=-V=rlz;bkz#wyo31@_Z?2NYoEeZ1(=juh_J?L(tIS9RS8>YpmtROK#&z6xP5BHJTWS{DKYcrZc^kAHgigH|HA_a?e?l@armT|@$ul|tE_(Oo+b1%8n5b5-uyW~BAU@C}PR;T^jLtG1_YIyE?V%^&v1?C{izj1$Qr6Z|8*+cH7|UdakSEyR*#ZqxdFw@R&B25FFnRcyE1I23r=2?>nPTgiL5Ud%C2t-<Ag5ZUZ2qyVi{J~iaPF{YJ*DHVmf9laJ9V`6n^GFl{6Q@vS5=%Q{VvLvTt9XBbWjS#X{8hVzToG%7ou*x139?6GDcqNTkRO|iT@1_qQoIE%{F#y0DM(VKC^zq*)Gb2|tO%1pTis5o;x$5=Gty46nu9`f6aGRzgVl8{|PU&QZ*jthzrS>N>9gEPbrT3&-IP{ST$2VGGXUUTI<Y_5d!_pWewbkb}MLx>2z5|#Re>1(G7hG(_^a+;sV->%_(0*GWAPIz3#ZZcC&i%VWIwV?ZjHb+NL&w=sb$QuD?4{n5i$BXNpM9DYC>VRT%*zEk@UFMkuV7slqfk+YV}J=63amB4M9-R)sZ5_c;AaUF)|x_=4Kk3;qsAt`;YDFv(0jVFBtE7ye<`!sJiuRT6I}V(cso5Sc_F%RfLPiFC#NW{M<?>e<5T(P!BPL5ou=HWo*f?<>cP=?e4^NV=jw{SHq`!zNEi^WJML$A2iwNk)~5~vq2WK!Ng1M7LHRLDJXVIG?nb-%us7~l3wz{Gh%fvETfk2fK$MB*D!~K8!Qb&gb@|D8XfnAa^$63hyoqcuOX;2dCj0#k0u`!~V9Dj8jFNYc4zUycbULA2hsgyo6As=_<?DScn&}K0|02YLEh{g?((<xIi8=pOWi{#Z$bq2I<GGp9uz>YyJu2IPMXu&()RStD$7^W-v+Z|OQZ%!0Mb9+7dsMw1_-UdnYRxKlOpbg35S5M^7&GuRb5VpB@%ubLBaciH*zBPI<@W2+5IZjqwys~i(^Dcv?ACf0O+R`=(j@^<=e`kjD>V8PCF@)by&hEolI9tGEHbIS;&X=9?a<#62Qq^}u2#PHMw_3t|IP^`fb$frzHrlJP^$dA!oS=krB`10cQn9o#M+uq&O0FCz>i?=gDO!g_dzD5WGK@n#h%?44*PnyqD*raJN0C=>($p{RdbhBeaFlhCOxb@kDdLxJ@N(G`6ZysG(&IR-MyNh4Lgm&{re{EKAJ!mA+D;DW#XNfCOdX8@(c8}40iB9$wF%z18Voy(O}`}T)*qOvY0YR^?0VysXxdk%^!6;O11y&oN9I<`L>p*inK7{aK?hJ#mB<Vx5#do(NG3&s7$KS$3`o1+X#w<)=i%8;F#eMQZ6xn|Gy3&WWV1v-9_23pqUx%V!;0_3~N=~-{=ewDj9PesDI3$Cs>VyjTTLvv&#Yu#E$}13gFgh+b2zr<uy!jl5f5;bXx85;~)V^%JyJjdJR}moyB1GU~m_yfn(5w*sl6(GiyB0a`ul+snS!E8H!d#alg}kXodt(9T^Rj%Z(8ALZ7PfN(->R(jJJ(`XGL-eVe>E>);@3W-`|PIJ*d!X^khec@~7l`iYN)0i-iT*~C%_L)p}bM!6zx#j*edg%ubqtDj4@R17as%Hy*9WVtEnv$+C^2dLl^w5kTUsf+R#*~oQWqE3KJdNBun9?;sD90aHkH!R#WC(NP|q1|}G5S9wapBi~|JoO!VY8)d5B{o_>Aopp;jy}%|L{Qym$24R$ar2ENz2OWU<0Hig7^a^FemfXUZfllp@H|bTPzmvPuG|k%G{H<9y0Vi5=tGb$MdnNMM7`A}HPhy%Ty&~+N=wmMDFOq?!3w_!h~%9J6kmkRg|z*YtqqaG!-DcJqk1l40?hH1bvo{XO;R7`n?BPB*BS%s+cf_&fw0q~XLT6GC%LNBbY&1%#nP++nZqEIJO-TR9Wn@zerfTOc{9@nF7TSc#xyfelRb8Uf868U*16?eG?k-icDPa;L@YPvq=vu6htdk;FH;XfR;w@z2`<b7jj?(D!7w{Ih;6Nyg>$u0EcxCt{}kAz<9=A4(y#UR+(j(!O)lO&{{C5ZI57;7%szN9=|?<rl?<Q5l1{tuYH8h0&8f9K)~biFNmGVVHRJn|x$$iYou!|mhO@7i;$p8C6}v#><|u^r!uS^xEOjZzqIRVn+(OQM;EoCxzJXqX>>9C6@|o7O*Ehxv$WkXiy!ieHY6`EO{q*kb<CpyX<?Fu$H_2|#A;;qm{dOm!o=r3@;}m<>$piwZGM1OTdT8qI=NK84R8x~e7MYe4HOmT1?%6bl;|ECRsuo+SESe8%Y6pWw^`i$9B-xrW$kV~ItndxZO3@TeKBCSLlFX%>Ely8YN7T3<pFJJ<@4is>G1h9ROBS^H2=IE1PN<=COGh7N-k`TSE23nxu1z`_4jr`~mHW;SixI7njdazT;G@0Vz!LtGjz&(M&aoW0*+I=%;NNZWte~^U(Hk<QTTnBF+YTedH{Hsc&mPK7>wvUyG8IjlrsnvH&Cc62ebzN^!>U8_YB6VnwaMcr?_RuqbrGod)-rDBGPdaF&cQY`3PwfL%jzV9a`ef&*FQb^;mzw8uinL{sv1vlPp`hzXb=ifDd~F!97aPp7y)xIYYe(*xLbu3V6;c5H4f=y?C_R3rbKR<6Bv4R%d8GAQ*6?Om4Y3uPA(wQSnt73ujI%c6vMWY2SmnW=#8PSG~F78z1=d-H4T@<sfI4T?%&mWB2_U*gu+*o+vB#*>k4oXp2V%8@sasCtUe@mHZW3W#@>V2+0KE+kB7&sRI{FBSiMU&gRt9Bq+`~xGuAxLMYpmc?#b{oB&|d&aw2q=mA#=qFZ)sC)lu4pOf5Px1aIc7Cp)AgU$KBRwM{+)K&Y1RAnBA7oe&PRqHZmE?AInUt`^nQ%Br&7=O9(sTG9*vWc#Lo5GbwapBXQ^5#wYSE3MN55l<<@b8)UComN2d8wQ{OI%eH5<lUldfsnFTYK5F1i!-a7T8G>YExI3iD7~AJeVx2hEr~SG@+WYFe=*JT$9xUL@+MyiL;GA@*v-FgvOPdDOiHmd#=N*RR@EH3ObRSY^-l34A;DsC8S}@d?i4dsuDLCji(D(%U0Y04xYVRP(P5|<Hbn$O3Xl{Agh*^ZUhDXRi=qv;h~ZaPI5d}+Niu`J>$TRB&J!y~V~(z;SM`;deYd6x=b5f2Oo)}om`6Sn&pvex`7bZt2<0HX#bcTLi&*aY!Y~%ozK`B#Q&lLN0UJ1I1=GMZf<Z7zxF>Uau61~3M`X4y!91u7YU!L+|FP}uKQ_gt2vlpO$crGR45lVDuQ*FBI0R5p`@1q8Y<*0K=P+j~sSMdEc&h_KpNzWD-P#3$FrTVeylgltTS9|X$ppq~`jsKx!^|cpIgcwvrVlen*`h*bvr|;rm@_!?q03mBX|NgZ_ns1t${)uyj1Wu2HiC~>%c0jyP1Q!sCNp8yee^9gO(3)&SGFxWAFmG)JK@+<b38xQz%AeXv*v_qPC@7wj=<5Pa-dQX2HuAmlqTFJ&{=FY7k$pwZU^!vV2wG^jUbX@sSFKjtpI-Soy}2<7*<}WZqs=5++3VYeNa@Id4at_NbPC^tz*ZW&8e*lS&;DA*R13zx<Z#@BCildWv3MnNp?OAe=n<#<dpt*({5%r4oL1mOW_PM99d{xAe6ge9<v;CW>vJzEonB*ARwT~sXL2@UYQ{xDacmqWQ_r<e}NX??`$xh=yj{m`rb+oo(3#eV1zyV+jt)zGSN`lg|rNl*ofyboR{Bcpko^}2Ed0qKk8AUG55c)c7`!m1E^T`(EfP0znU`efCA>2XqIOOF0->Jas`inDEEZ=3`OrZIQ|zg+JB<*{~$@wGe#}cr*^w3iASiZ5senL`);xGwCkZvYJ|(C)2Ymy|9VxDfMlSJw3vwx914=`;(x|84|XY3S5(h<gvvf13_#@F_EkH*w07EPf#D{Nw48xYR{K0EJCIk@5JL*P^6|;aIu)oKp-;afLPF~RD`Z7SR6KiZZM@4}vFYe3bWJ4{LT2+v?T&ixXFUmKzG)qR2~e*C5aSR6#M|zEwntggm@It+2vJVyA-a@%h!-wU28g;1%!j&*f#48B>uOh-*{Nq1bpA~mnBQ&l6&h5#jNyXe)J@*Kc=D%b??x}4W{Q@nM%Kif@RP@Xempvo?X)jfxg7jS|DP_6cX?YbVn;b!enZ@LiroELdqstUd5H5K7Vu2N+f3b|h2}N?IrDr?0k8`!6vf|t?){z0`a~`MlQOFBz^I@SGw-RnS0#SQuY!AnjLtnA-tLAQ+S}D;Q=hKZm)>a8-_>S0Ey$*O#M6~uUY-PEczMjNefc1$7%1V)HDH`XzO@rd9zAZBV&;aEx8ebQjwNjDf-dSKw0<yZG{UJHtJX>+^y`Zk3hFk@o?KkWMm4h>1kSWo+2HWLA!B)3TOw3CZCDV+;(RWp=o-ZmiDBW6FU0kepn<x2yoH^gNno_o`Ks%pC{K8ap7p3VssHAaIVkw82+R{TEl+KpnkB~QZnDh65cINd=#??>x$RotpFQ+S(1$coLvJ}$A%J@rqMhfLLlRp`bfbL$lL}EWXAj|=d?EZ0JP6I6%sju$HRVA>z)N;hJNO3wK7RA&<%=he;a!^a@AHjXm1-<<9i!tIQd%)X94>iB!~K%UsLMnosydN1f2H%(Lt8U4P}bHS<8c?xz4oa}cgL+yy*s#XJ+vo}A{S&CmRj(BwsdD?UF4cXoa`2A9`z^V>@^{SnsOnBq7Z(-n;_uP?$V$C9Q^JqD@ulxQWPVrO-1@P;OLp41JrY7I~?`1*Kb+6rk|TaX9Jp-A4~OshlD>3+$`pXnfElL*TOe2qY$(udim`6yWnR`EAB|`LKEtK+N}2hsbJdN*Xcr9t@|WBx+E(CV6JgyJPGaCaCaRap^HJR$EX)dPHag+ySaYqS%E`mQHb_@Gc>Qsc2ox@WdK#O5-Q#2Wi#6}%}!ORlHmm=O0Hgh23s;kBxnjMBRwk@U2dA|sIsEtUci6kR9{BVqPi9Wn02mA;^&I6h@3laruEbEGJIsMDind$_0i{F%=ht%RpFu6koU^zE*PU_$sUXva>`Ib7y<<B2L~J#Qb^UN(O8|NKb37}3m~%i?5!SwiV1diF1K^Oe^Z15n{YuzzXawQn(|<xVWdJXfXzdc_-a+QxWKw4CuvQbhwNPpmun%Sr3|J0)vfnCqWX6LdHv?LR%l=bw@|5`Kr0e^qa^S1P_S5_o3&kz;$)FUHS&WuYy{3q_95{B0XI=;kC;Vq4=6%6y`5dq!R7#zR|IBbxtQj~^QMbpy*K&Qm%~1I6GCmu6sYTx6GOrb-I03AKadW0!rzq35>Oyig_4y_*zQWiQTE{AV3JBCc^79)BmxJ)wf+s}v439Nsz22m5tYRM7S6Eb;y~d)&)jTed1=?Vhe&ocH*eivM-EKTMTlchFx~k@te4QB4gvtWnO$pU*oky1H~^hCHZ?m3DDGi!A(`Z9;-seCF9W8H#ms?^^5HnUfTExt`c6Cp$}Ihf=Kc0ZPyXP?`i~BU4vzUK;G;XDyB*+(GFjDB<f_B*L%C>Z7ytlEKO7(7HogYc&$2i;<gD*c9QT*$Qx&>rXqY~t;=HDq({B}3SzFtvy9`9ectj|u?Z>%XRV$R$EviP#{Y0=KVR-xK1RcD4hNR+blZf(SM?dtSuI`zqmvu(jEfi2vFY=XnCQCq-h~CyFIWZ%ffedbAjsX3{SM^Z{HQJmTe5BWC-*Wr?DEa+3`TfMYRxm|xbhQC`;&LulEOYT~;Ol%r#bP(mV1nhQT&FP+jP}=~Awp)F9Qa5qQU&0Eolay7?e@S3hJva_WC_jfQ6KO+Y;Q-dVxt3C!=kFz_lnhbw@8SYhK#Xy3aq<9mte)4pgBLo0nomn)?83qINm9ZxL~C`I|RmO!hI|wxDo2RiCmRlGVo&L@hFSdNLgKo`L*&86N$*a0rXpjAUzV~XE%)@0BK|_2X-+_M-&Hp$2dwxlhKgVsxvr4857WgxoQmzX_klQfH1Pgvg%(tK0VlfMUGX*uA6!2?E?tN4&sSC{2v<;JG?01Q>%O&?)^y6NB~29EwmIGI@d`Ay}2{!=u8fY&TitkPsXF~4oHw^v$EDYGQ6yV$uDz9+a}Ko=8(<Zu5Rogw(*QbH`}@C_&7mEK3sx<`A~t2$h-oDreKSx>@Cne<6B+nMdHwEfvQ3Mj^kt1uR0Q{q<FRQjZfAnido7ZQCkrD&Y_q8sVLS7`i9*Nssa927o_9iorhZyAF(QxxMl9QbD#J)&6jj=2HDqnA$A@O%E#GkJ6l-Jg(e6DpM(;8GLhU0vI6`Ubbcr*hJ|XILDEdFS<O9cRA|YZmz?jaV|78iags5IC0f<x0^VWfFI*PIoZKF4!*YM)!Qh9#(iFi$@Uq&MA>66O^XmRGl^f&|IgR%Y<CI>?8}6W4DMCnL@ZrlU9D3vopJ{w8pML~M6>;=uH@A!zsmseLjSkI)Av~cdSlNl_v>Qd|(FlUeIxE6kvXRruGQYnBe4tUMhDC)I9+-<ktx8$C@J&*Sp0Gnv_)1N-8hv{(FRmu%G+j-%*aVg~Ay1oFY#}ULbXh6%Ngbi~EM6iHpZ}a&-A7UZmrWjLZ{<I#oD;;0L-7DO&gAXnJW`1hehRS=U(J_V5MJy5=&SaV8!P_^i4nF(Z+3~nUbWK#T6E|3uMogkc}q<m9m<1=zVL%E>4_tHI(LiUOK%N8q*K41TJCcBxpRtnrO=N|^{hfw1-XK9AI<c!dEEm8`*o5=<%0ptu$9NE>=Z&%wA4$>i@mTuy_%F1TmM+mURrWM{Blv-#0X>cg}bUx69<inEwEYd!(dir6UOe?r(7ucMNQ+O=iC^^oEkMjKBM~s#N}>=J=g%0mt`>*+Wma%`TM|?+LEymPY1uR+a?nU3T=eph&ZfeVE)vvciddeLv0j^N;LeD2adb1PV}0~>Ppohf;}2W3e@9}J-?qm!z`xn$~4%w77evh!Buy2<H9$M*CbQHat9s^GX<ez{F&cbJ2wOfgUMzokbwh=l1Jz%?KG~+yp;u>8^UGuW_Oy`?{s$B9q+F<*gj8-udy1x)<WBIyR0#cI&N}>pysV!2a>LqTn&_&W>$+@mJKKckj-{2rs^Q;L5y0vppQu#8tzM9xejB(lp}lzd;BR}O|@YH5PqqdN#l$9e($a5%z$Px?fB>vtqHUm(1x%5FhYl;qz5EO3HRf5Up3l_C)6XW9yT%A<o&noDRJKFThdig6kW^fK<zl_FtaDXnTPOn))P4)re_TsNNZa)3tS6ktS7Im`Vf64%4OOO@h@qPc??Sj9oC$c3HKFMO^ra=TY3g;Z@W})?oKR>SDG*dLS{8Wj84A>ABz*ef%<2zg|_knXwcy`;5==4V^V0hA`4Bq#-f*?lD4R)L?M3i_~nzIUOs;JEc?H*i=Te{@$uUi|Mm<$f3%j98prjP9j=+gS+Lyp{odp6zkmDed$~`xKJ=-%TW9_o<<bVlG~!mW^uQ6nyfJ%hw8?9zP>Mn<cgvCwApNzmsWo_H3hSU_9^kR4Lv&UaLLRXEAdxYUUx?j}vfAykWP3DZ)yX&egt*2BNe{>=^a0HtFekVPg&BCNu|H~o^Iyj^O)yEV?}Q@qvrT9SundOyvT`M_Z21Ay6ivXbs)eInEOUH`KL+9O%bF=$We4em1$?QtUjN%&Dj(>0MT0mpo@cV)@Zcmir#yKW@sCI>OsIqQ*u$S6KJ`jrdJhSPiMGa*LwUB{eEbmIAT`oX{ea9c$(Ydi^3IcnnLJ{co#2s{HFJ0Xe2?N&3ls+EK_MpqkGT)dD~kH_ZUQ>&T-Gc*Dt_fSQg}?tO=eBf(Qvkzmb2_TOsx3!7p_~V!7#j+p!9NC&n>H?N!DtXES9Zx^3B(W_MJvyg|9S<nR+l*8#672<uveM`Q?L{u{R$+4YLQlc~ylf+3zf^76OEgs^MGnl6S4Z1(FnZzn}dvJ3y~d%1LeKdGZZXBtoe3*Fc^^_4sGoGGe0R?3Kf18&Z+McI_%Jz_<#my7?Sn7YXuizxJR<a4IZAZm&~>{S=lG*8~)^IMS~gXBYtl$i<)bHFx3&@UG{^kTlDD8>5PP0Al!tqh`X83-)tM+@sqTP0zqHc6Ht#gRU!zuLA7<MmX#=RD~ox#1hYkjQv*L&e^}H-OufrFCogR&+s~nmG@3v6^$GHH%LO)_CTr#dyR%IbR9ciX>!>*k9bZowIL}@6=STNE;POr6vq&l433`e*y57{kom5ABVOXQHzp6#0=Q;YtqVt<EMcTPdHMLKr_X>67BVGTpqMD;!%*mEp<<}f_5xG2Vbw=vvFAr3{VVM}qUPlz4UIT1%@>jz$7;7dxa%csG==Qp1X`>%aL|^DF~y`L5~5Q`@5o{%Os9+9Y~<J37pG~G6x2cBsn3gf6e>x5j5g$LQ+$kWo|@OjX{mhg8}oc1-b3GeJ#r81!T9i`SoX*6odfJb0<GbeM}P&3+#Iy}6<ZY~iGCLv^bXW>W-W-6ur(6Y)69eMcz7^Aw!Dvn@yUZe*3)xe4gG|?VMtSDbu&~bJQ|xiebNW5|4>WI)CBdNZPtu2Sa}=+6FM3`PVPL*lA#<NK1jq(^7&EndE!#=&`}rz-X1z!5`l&Y4p)oq8f7=2{JAV>y)s_h_fucQ?Y-9aN39(#vpvXn6b<AB63(f?W*4HYTza#fMv=`Xp<b}r4_VBk*aZNnG$H@sP}lwlD2Qp?<CW6%lX&34Nvry5^jmFH@|X!0L-`Gb1f8k>*cigDXoc_hpjEPj7mDP66IK4gZVu+BC(5433WWF_6ZyOquIK6*dJ}6H_C=M~cq59H=L(s%fP+PB%r*gYy96SU45dQx6}U{k&?fa_2|1pYaI=M>CqWA!oAuIs=2-q*P0biys|!IJyj1hTJ?FZo+H%ja`smC7^oY8-shV3<Sfw&!xI9{-Qftg4|0q||)5=7l(Y9zuih0HfTc*E=u?0(>Q2fZH>coLUIhs3Rnu^n<Cn3q4ds~PIr2M*Az@LFs>4kO)Fo>TTa%X+rcw0N7gN95@00tuUb!&&^V>OG*$O9`TvdTxs`+^;b9Y<7i8~_7C$2|+)FOp8wcjOl<)Y~G5HLY}De^^JGbVrxmW*DfEsD|DH$G)XAZ<VC)CGPrN<LYK5yq89kJWk1b&;fN|QNuc~O_nk(;JTzo%=#IAAtCpc-E40alR7yEWU#{7%_Yn&P)9zvZvwj;kWuu+VU6wBSsU0{QNsI^FQ?@dqhN?K1S98A4~Su>I<lB7_qu{RggMoUaqA&Z-xXCl{b->;AQ7xcH&X2@yy+-;lL(kKWc4&`xOx%7$@ek<%NSawQLPStU<+ctTum(*w!J5TwO{5p>g6o5D{Kc$42+A58l#rcn|GG99H?a%w3eYmE=fpyx``m<>1`xMl2r{P*jH6(PpGX6gJz)zR$W=z$<+!1on70}rZ)*O5V1Qf$?}!vKDu6sLpO5RPadfK4lC~Dc3TqV4~Vu^<P0~-vr=wjgfP@Dz}EkoRAHFQUG>M)DZy^z15ML9L4JOd9CQ%mJb;@7cIGUXerX*{<hF~*RIQlgGfXu-^dfJ(mSvo|w-My*xSJXtR7Cb}0>_(8hhOcZ_PDM-TG>{Uc6X#k;T`;NrVT+7Ofmp>Dij4ctjecE_`jA9ne!lTdF$td;O$;Wxvs?exmO8zTCL=z%!MF`$d-%8d^y$RJ<r7zHE7NTW8Em}b62lm5O@W;+e1sFkTA_f)EgeLka5RDj@a&sQ?{z{h{c)UDuu|mC(%S#9p@F(lHS#u)l3sOWI3s&0D_zhceC{JRZNb$Ky2LGz){J1Km{a7I6(~|zcMN)Yfsn#&}2w32)gvU+5~T&dA__pP&QCs+ZB1>FaOwJQ(A?iv^g3;%87?RlODr5r5NcAjV<E9zK|gL%;3-EhA?o3TVuXkG7r*!x|rP=;VB_#4*HX2=B8S>RmLtx=7C|M1D6CY9^6Ifh_E$CgELbGnA!)whxbkXImJH@@edwwcyRE0+^e}TZVxE<ty84>MBH#xoB{QT0cHz#+j?eJRqcObdG$3rE9`rOds7Q@v(}u(ox{+Hv_>1v$GtKzDJ3}7<XE;9S|I;fcprGtAxdKz!90T+{^H`v(1DT`YybfdSi#)Vp*8Ii%SgBvETl}+C5#T6B$lPVb&>*iub!5|vY^^CJ&{e?X$2W%tlg`7M#j>iCg(C;&N8&t?Z&jY>|F){1OG}Nq28<^dM)N`vIwj#a)u$2rkDz#qp;*Di_`|)p-A_^XgkNQlBZXeIGG#T0F;BbB1mhhSYcQ=GA+5xOhbZtxIMuFK6)XCA=_!{ojdRMN=W7nn-ZlX{SMH{kn>$u1TQ74*6e(x!r~2CW=N4UYwa^J<~P{0Yjprw1N1^{^(dj)2O-n$n4WYEm9U(ZYfXh^?m@AJ7bE{@eo1+TJ+8N^w0KH>!WrphTwTof-9TPM31oWAH6ENcCrD8+r)*V9O{&&b(bo8RVckpB6X+UX64s?z>v@fr?Y<sLHAI+#NT-YIZ=*S!@Hz2cWXwr5qlTpc`sr;RsiCWH-H|_c5d_kdEjGK13VagIMbU+Z>64a@-eoH&Cx#Zb3ivUc+3unSHvrdEZSTu1BMjVMOPyW#rbH=BmsJ8R^3QY%g8rysxvNdtT&qG(r8xqMMeW)U*9_ei{;ZmLa}lH5aJ~}CROs<>W;t{TV)_3gf%-6*U_bv>sQx7ux23lTGjL?twC>fNRE5>et%{hLI7h(QAeo`Yc%tZI?^ms1ibLLJ#|TSFTm~&?<Y}B&V&@k`Feen|4oxAMS5GN#sp=TUy*4y1%Mf#|FeTf=qoiIN>MY~D*yRMyh<FBf^99)_<h}rKQY8@rsm8Y~-!(E@l4`S$!lcH{pwF26aK6FfAE-x*;Z%*BqJl)0Zs7R4V6|jljTOw2p)qKxg~Eua0->%h6idqCWM!M^H&9myGBK$sW>2&ic->Xj^NpwmXu*SR^GG$*Q|f|kuZ9MK@3V_#@QKGGX`KbTU7`6bgdW(z|M@{&jM6I5Ljw5(0mJd}>EyiQNV2c=qYv8;a#qLV@PbL!o1|aKc#coejpc*-EeE0oPLgU>o@K@#v115d{*11{4-SSrS^ALP4O-PajFI7k0}TwHo@fiEC@ejg<`^V_HdG@r=?d8tq~PbHz9gdFglFvUKmYrfpQ4Y^rm#8OdmHD}Bm8ai_wt_GSWGYqT}+I_GhxnJCGnO~xgsK+{vn$l+BNk(+NqG0_5i<GU7K>TwR1#vWuJWj?lfZ3D^y7IMW>2VYr6I3RQ@h|i$_o`Z!xqxx*I*!;${lV5fp=trr>TWMv`4L-Y2#9y@(<4&otxv@V1$oVeQ3!Bf5NyNakn4%V3OXm8sJ;oVCIw*EEGnH?hQdrpU(3`EZ=>5W-D5NGk;AeUHjspf#AnvNTp04(MqnH-i$tYE*q1!YaA%(vTbNSMJ7t4I`5Z>9)?7<^AE`Z@%`5<)jC5kk`vK5m-7!kZ<&9RIB0G=+wb8*`=o;S#(J^fC%*`XisNRO<1U?<<Iio%lyWZh&>)h1zf#%6aZ>ibjwXOpMxlhtTdgM3p$Yq<*=8p3$RHU84!F_u;1yW9KG0;fm9J)7v1B%NkpBmP${Supe<x$y~ow`Qz+tQ7!vK=XEn33+b`8Aey0TeZaKT?kL%(Wv`U(8%0(N1dM^I_eWy-~;{U|!_B1|!A8J6>OY7OWJActV6~zj*GKIMr78ZBpb^)aeo71z^>}v;YmSR~*qo-?%#6NmmE#|}EnIS)e(1uPzt_K@o_1Hq9cA&vZQ{3T$1zXJx#4lN$>%2K*7kfBz^+*Gfr3j6ZgO4CCVYigs+^UBsUdld7D%pXV!5Tyyh9W$Rir=r6vfE>b0uX|iz_PaDdT(9eR)V)@$ezRHsdRb9bA;X>SzG``yY5Zi6Sn^3;P)o7gaw>=r8b-dw2+})rv>YyqtBGAXFW~S17r2#^%xG{DZKAi<)54PWvAj^IVzn3H=Z$V$N%-KXSRC~E`!0DQk5KJqpQv#wJR@eRfe73$+7yp6THUSj;xrT^@+byE)PCaCx!B1&10o}!kUeP?q|<A8t`c0Z*$=__lK3ASDQME4jo>&V_8SZr)A*=sqT4ERL4Pd&|f_zT5-^B3*o;A)LMYOST?FgX}?xXT<!W^j9&R5`dWUbZdqPRkpH_q>pMKP_hPw5wyx#<J-d{vT4X)gsF^JCmXh8sd0uM|`7N)i+e^`QDpSyp^qM0uol-e+t%LX?TsOQPZc$Mv!g`$r%m|0(j-BFSjft55yn|}QS@tj0v>Dlm9Q4jS5eU!(4M<5*?6XAoWr_u=Xm7}~0*)yu6b%H8JfUrBq%8h=5Pv;pLQiFpw(CNBO7f%kdw>X*D__3;i%8XFCG^Mof?XscPYzSaqG3R*J{TW91cq72juuuW7Ql@qDcYO@8JqcEw!vN7X_pshbDUiy2DLt?SaG#`|K+HkDW*!Bl%5dP6;eXIX~?-&tD>$%6nSJf)RJ#2$4Erdo$R2dD0cew$l(r6wRxZ%c*FCSP<(9#*8M>CT9b$S!x;|=Ne`3qnfx~2rr4G?G^Mh5UhgPN@Au}Fs+cGh0GfR-CSoInN`Ye(=whiY!~W~=h?fe3_CCTP(>$z2-lp8`U!9W)SF7Q={mo(<FThrR1|qls;u!CCj<CcRg{*o{{sKkaARA;(Unif6>WB<_MVxEtPhf3=aGttsm^c}FGCuyKF|aa)M_O3`hQ4jcT=-{V2MH#s(=PMR`oTe?rkQa&#9$w*qs%|24rI5z(jAGe94=XA%2;C~Mw>?<I|e91hucthyiViain<yF=n<eP-z3gALX~(0oyH-x7O%}dK6=3A5mpKJqzjc9j0&W=GT_7e8Syz_x8&q1twkb*Yt6}5)?caKwATnkS!uL+(Cz^zQC4S=;vOVYpg0^AVXN~HHTJ&mC!U#T0X{1Lq?-+aTf>8$BIr2V)Cf8105qOJ73Qczc@DE0Xe3xJ3N3OpB7<U7AF!vwFly)Guf!7S#9H!CIxQO2ATU=_m!cS3iD(*k4a8xQR*?9`T5J*{Y>I6AeJd{p|1Cdpfxrl?DJWg1Bq4VruBy>xWo=Lrz<Fr8=&!Z|G)EZ!1W#EsIWqOKx!b5G@MOn;cq>ahl@&5*1H9%Ls^~*>4;^A1Bw;YoC||?oGd2qajF%OAkXZvnUH-()seL&S_FnNPZf3Lswx~I-Ejn};JBB-pPHav)A&_Jdr0_*&FFl}Sb)PDalMM@J*>>(pzP48HK*5oNSkSPn=9^io7O^(`D{y?Z<aMZ83)C~F4{2n}umS&~IXHLNmmkVn)JQ9Shipt6Cx{Fi8c&IB+sR9acFE=+YXxXC<3Fi~`C>RVuk?uYeEIvNoo=98N!zo|l|PPNi2|V&;h)MTNe33;I{KFW!|4I^8EqsOtCGQ#+3W0qiyW0{vN`I!xFabB1Jd|&(gP~RSH!E2!SDgwsa)2MuRb{#AIV}Ij8FLQDgS*a{~nG-+cFi#bH0(d$!-8_bU07>e}GO3Dv!p;3Y%~8Quvj?*e_u(+}|H#n1s2*B_nzdapBPc8YLnBmZi5^7FStuN@TA6M0M5LWcWp4;?LZrXSBtaeI(?}_@0*qfEPBkqUMH*pT?IvRgMW(V|p;5N?4WLOI7CnxPzK`*g?${qZDS10(RaX_-8G2XQdgY%iaYKC7@v7Pd2#KNALHvLGt^($+_Vs@wq^Z!QrG|{W5-tRwBB17fiiqO-dUO=TUZ&on{ZS!vnN!NuYTK19cAegt+_O)5@(qYv8NUlBsaz3W?&@hMtGYPcQSP9p3>#y4(50`qE8i1cE|`yHg*%q1Y&$VsApzMl>$5lW3xuvP+SKqQFxd6i<g$FbNszJsg}PV$On}ph=fM8GttTK@|nrZLSO3^XX=}z8CWdp8We!j|Kx&pglU7xVgOxox@SXP3^+Q(~D|vYB)lxRqf0G$rwPT+g+Gu)*kWvqNFt4o{&2z-|eV5rbF#wvSMZ<$E%%&wzEm9j;HR&Bis4Ngt;@rNSprAYInvxDtu74bH#oV@(ie}yW0~#`xv)k7ozol2JR^9-P8S{A3%`k?+iFdDS9Wd&P@&)_1eaLjZF*f-3W}*cZAt%%Q@N7!7&!Q=;WRr65}j5kF0GegRn+t${hkhd6pynaC@&ktys8}J8#MqFd#bR7cBHos{S<+8;WI2ppS(mb=*#>_wI{X1#cb9GvSgqpKUVuwwz9@mE|2Si^5hvuMO{Tk!v^cce}|FnuH@352V0V(Z)J|t^FTbpC4-#uEmSmycj8dt*9x!3s!+FZ2!zUQhBLk)OS;ZJNpEz=&5!;t;>%l<Z?rDN+bU*G2c~aA*%pE6bGqh7W2};9yR5=4fyz8e2&pKO`%3rz-Y*Ax~4yFty6=!V}RIG&E4$m0A*-s&03#&3xm;-9!zi8m*vivrnZJ*MmcZ;grqks&Z}IdgAGe=I26#G38maRh6oU-LB)=|qXndTz$iXEfpci#|FTu&ZRA_Q>uX@9?0~>@01wXEl*%?hUL+F(wgf;o%~=(lJ7sO}CEj`%*;4bJS&6yU63w)`E~%4xop-YE0)G@85;UxgNC@^)1*Ay!g^$r>JPUN72=b~fJT-#0427*WV`QL|5?(t2UKCcUK{g7`bS9j=sv5PWN0;cxH(zX>N>X^vP%lo*2TfkDtoe03lmRbmnWQ((XmY(RG&svadI}cFSPXWpv{&Lnr;H170Pg~1JhBXjYOa<pIytCB>W%^%yz<2Ell3RO{>Q>Fx0Yssuf$-br$j7PrP`PY9Q%znNG69%TCF~`R4E*@0Ud%7KB>*VBnvFHAwBq}<DN%wMQ}xJb!PBzjrPED@SB}WsRb;&O^SOS+71ku@|TWdGC@JO-e^~?nk!@3VcZ~FVQq5Nlq?C>AcXaOGi#E%FK!5&BlL8EWx^mVTL@c+3X5{=dq6`OroQd7@7IzsW;|#H&}_F&r@m}xui|O@n0t1yTr#^suFz0>$=q%6m|)f1DnIgmHa3owHFCA5HMC7vYzW_5T2E4CovuoHUe$;=OWN4lTnvr@Nc38+l^9dj`kH8Go*(OHLdJe=u4(Hh6#Ra0gtpblw2TfkqwYFJR@(tc+ghjN6hRt5L<139?2S8_9Q+hcc(Zh-WF5;DMm)^Y#x62Z4cZ<<V~SPKxAjtZGk=mvoUe3EbkdNKwjCX4Us0nNwT?1HG#eqy+NsO>xClckXLq`|c$D_moD~D#=Yd$?1)o>8PRz^+R%$lSi&t(KUq#b)T`gPuCogD|!|AEri{LUL?T-SK!bkA6!Vmn6cvuP{p&)0d2bq~yHdV_h;HjT*?Br5|L7?{Ni$n(7;M`kWS@xSRADvf>Hml1v6lOGvljUaxWn2~k{L-CIJeBF#`mkB!6Iea>z&nXW4)Ma!&mw6CtrFIrfufbJpB}Bb5#va}JYn5?*;%O0LO*rdTZ&tI2sl**iHqoC+Dg}uN`FvctXL|Y0KWxIl&t`aPn9(W+=iia-7Z8%0?Zb~>d}E#0T`gkjA$<<JdAf5mTYcWXNve)tiz#zyjJw*b9Z|e-i5O*iB?fsytXq=ccJP6Jzsc3L(}k##|+HnuZuAE*kW))n!{-F2UL>&aY8AC13;&RV$!Ley$eZ_g%*g$Ym85{8o37Vbgc|9n(f{1&)pXDw?x~XtvKrqyCVZ-8?|QoK0eFYt7C)d2u7X+HW+*-;)q>wsO~$OHX?z>W)tf$DE|Ri=UM?~L-$<`nd*&OcfA#=5l3b4ESo&R!~Qzl9@x)A8!vvH#AZ%RXN~UEX-H<xg5?2^O<IYGZB$}gFE&?Z5NI+R%5VUURgA!w-k38-eGY8%<*VTB6*(l?iBCb5>LnwOBTfh3k02M+`aL}Cz@9@7otCJlYpslI4r`oPvz`fSc#0rR8iguvgH>oH(`p@tyzw?wz{84J?U>5SV(xq*@d6I{K8U}h=K)cjk>!mrA60zhX^6<S3Lou7V_CtmqgRB@9R@sp`2{r-nlm8ec=4wfFJA(-!&(7BYKF?xQLzx^lir;$LeuSC9mwk<sm+7iMbB<be0MlR1EbVz`^2IPqSu^u21^<7SW*R?cM@dV6yUqgw7ZJ1hV&ruqU7+DNc6lSFe<W@`W^g8fA~>+IMj&b&Sk#Ueyht)#f8y|V+pIDGirCL1H!bwr1*3Wg8mc@wDhHA53@(&N9}8_AeU9W4XIE0PV+!}t3Ew7_W&xPjFW>_22JgN%ihiLwF(H`glg4M@eP`2{W;^L>#x}bYILoGgHR<*zlOeR7>-eJDmByGg9cFkQbY?-W2d#IL(k1ZEe*xqTWQ(Mb;&g?>dGJ|p+e3}{{Y9J-_^G@xAoW6WH%;aAr?8W7LX_UvMo2S=~}L6#;|L*b`a4Q>VR6Bk?Qpy9vRSbk<WBbW6P`{cXxV)mAkMpou<WaLWy1J(;7Jh&zZ09p}9hmn1XP`;clcrcrC*OcGr>K)%CKdZe=|6&J#kc0d3o2=ORTPb*~S)RPteiF5JSdr{%6IMf9~_c0DO!xX1t8BpLN!j{<saT$FYFSr{lD=B}lVNQJo2Smas9|D*#0ETfAMezI%|@&!xZaUI~c@(k*RnIE=7p*{DndakILxbtwa7^4^GS8dY9Fi(7Tv>7}F^MBt-cbWLD@&#_)hVkl1)paVWGX0xox$~@9uXQnmR~y+-bSWbWirO_ySfIL9n!LBIEjw*tHDlCEutK^|GSbwOX9#^fgu4NykmeOKz>-cIO6e(^vNe3aC$`>`*FV1b>D{yJyPuwZ|LmO@JMW%dyvr`$J^ucg{PFsycX9!6FBRL#ke*E35{L$%b(+I+f5haoUF>W={%Me&&cp2UHdZTxiL*&KF6cFVG<;I8AhY!ICH#wl1zHze_D<D&wtkGU%z3Dq&IjGl%>Pob<epf`FebnOC=vEiNR>Nezp)nCV#HkSxnn8JvQcZ7Jlx(v>Z1RE+$MLF&lIgShaK!f1;Hhl2+)s1<i?D|dqOu&Y*S03aS`vHyrIEACOzgY2$Pdx5n0-}QB-|un%^-<Vt$c*qT!GJ)no>PB{POcegj1B_`r>o2ujVoIl60pMpaF{kMy>nx&+6s_5rY#*=|ii%NepHEh~dT23*6Uxy|dP8~vVQ?h%YNS2yvr>YFVI>v>hPRSpbV<@|TN&yj=JykiO8+$%U(4MNK+re_-R6hG}LGm0@*;sQ9_7V_Dd1hr`wijcWlY_;ZTKDw@E!i}ahC6F-Z3bmoKsT)%VCX|CKnB2a06M?;lOFrKcL9GKefq|owkfv2W6P_1VK)7i=H~T%Vf_1Y}Pmb+@j_-3jP^v+Y`=%Lcg)G69xBgY!1qI7$_CEs2Y&+7&j@Mh!6ga3dc<6>WRUBT7*?J>s=FHJCCyLLOkC)Q~dBG+9)b%v}Mi;M;KW@`L*ZdrNz-R7OPb&zwL~0hm8FKS7+y~S#t7`U#_C{LprOTP#c*{BZU}(jM)*)Q#>4N={E7qswR{-a=q-aJNcA9nhD~NFeU6&X(YD4scwh$j|=gt~+QAOgjkY?r%zEH?*M_a43NW?0`uf-DGhfyG_56jIU*#H08@=L8Jyv15e1Qxi{@{47=0Vw^><|xixQ*>K^O`$1jCg&V*zAd2&+G-QB=e3p7UlsiJ1PM%`kJK>x8$SN@=;6UQgG-60cc+uFclGk*$n1M>^A&r1Q9rZ{Gp$sM?Y)wfU|yYez0&-FKTfjx(F1LK?XYdSHnQT6PWgpmHd^ng&?`Qe-R!~OeW>t-*2MgG=1=L@=<49De?wylH;$+_szmB`=Gs|?6>w|0&KkAgF>(M*zJ<yz)m{tf20hgghph#UgG?RUi_g{1voKHRyC`;cdxcrgGx7{MKi)P}hDB9!WM!k4YILV}Cow7w4B=#W>^3?Y)Nn3x8ZifGHLkBSn{*!3ZWW2h{9*?}BneGFmet0#*>l^IlBdoL^c33&l!m+{Kn}5}2cO6vm>H3H&3E(tm7V#gce`&79W6M~TT~ckIR4aD%p$*ET~Y34l@i<}$kxGT4S-+Nm#NFPQGAp%{G|dKc#77iBlrHu8XcoMnk57QNmVO=X&70`{Gz<nBz4bCx9SZdBb2h?TR-#GD#Rf%6^2{EHd5`zAK0J_)JvkEbP5mbr@)w#@^W8>RZPHp>4mC`0uCI*n9{~H#@1Cn4+g7>*^Rk+u$ZT-g&zb6o#YF4NLOyjKYE{m;@2WOe~^NSRCP3o6TgM6Oh_7+oj=Pak6*rHq0Z|!`t#-M*Ka1BLJzvqbQEc1^?6N{3GD|#paeyA2tj5Ocpb27pK^jz7CoPk+Rb(~L*kd^yj2Vnu=-c#wlXJYUyJIBRrhA=%@*F<^A<jIH<<zbb*Ngu(|3xfeESCX5Mq8JLE1_TIlwV7B!f^k+Z!mLP695JbPgS6kKeo?TpaCG+$1Uv#XJ+MRtiX|dD{7=z20Z)x_l?((_9-KK9^D#-Sq4)An+d>o=T$~bJS1QNdUi1(xOc;_-R4aq%o@ymD6JcF>T2rWX2jn;S!oU8!>i_n>1(Xdkse*RC)R<&y}T^^S4^7n1{F5<avj+V1Sw#k*4rXZXZ3I7!R~4Au_8&X{m~bzS;_b*T6UJpmRZd>ygljvG0lpTgKW+SgYLipo2cNQJ@%Tqh*-2LfHN^qV%7$tC>-^!8ZqYs*mN|j(q5mt+}DPKX1DS<4hQk?orIK{hH~(I3e<xg%Wzoe{t|oeoF*oU83_~5UU+C(*!2jD^U-<MIpW53)gmmjzD)akIuDB$ST=0;D_jPZnWD6-2OQ68o5<uR0a`T$lQpk(A+KEcO+APPB_UHvFA%{5u&s%&;e>YWqc1fM6D}O-YF{d$YQ|1tsv0M)rFV)1J%>TigmXX=*1VS&bTIWvV}loP9Jfq^j98xxT;%N<q6JZu@+?$*-S`$7|g1r=&j5yR1T{0BZ^WRWXYqvBZEvr?Z~2ztasTCIS-J>U-3!EY}Ax%)IVWuIIXVsD0r318(6t)%gMfV%N$%xg~xjlA4Ce!9=!lmNS`}r?SiriO*`ikcHsGWbYm5}oZ?XJ_Y65EqzhpUKtSoIi4g`@LVrbkX>T{hs5H2th>nFk%)uSWaVh+^_3nI&>Z+VYkS#^V$=i*wdA({FiZCi=WNdUeWUCip`oFN%5^%2x&uS0&LhS!Sx07(bzXs6nOXx~Q$hN9m@K_VKC{~_V#HN3AI_V=2|7<nK1>go+x5~cq0g;7vFHWT<0@`!ry9bIW@GfrInPvqzEZChPsdg(%G%0v0{(iNQmHl`26~Y5Vt*TI!+7u?fyJchjz?rqJq^2OKlUY4!=n4r{q%IY_%qu+t%ADdlg-CM`wuh2vz+31@to}LNhuTlmk#b@q(>U`xyibWQNg*=WVL2S|-?;N57U#lc$wOiUDZjnn6LGUvaG6Dc_q^ZhFbTfnM?J>MDXMz=e;0SH&21#t^{-6H<t#uAAd(^_Eg2_8J-n+#E6G|~&Q_Ev42S_SD-b|o07-D!|2=)qxwpFqq--aZQa)_eiU8)(kK6Zg?m2^->Tda7R^CnUJdE8&;KcCl=plwb7A`ekF?}9(kNgdU&<=;ePcj4zeq}k8Y`5kGgf@~fj(5(!vdf)&H~`LunlOu(Gl^@Cm;OkWEA@zR9r1lS?Yy=KCVn6Q(3`;S)f5ft4M6$A@GWhVWhA1PzW|g@U4fz8N)EW_z`+k;Q&5)-kNM%=#e{h78OvhXlb%Dwt)7}w<-H$77lFm?s@Au0^@O9yMBLIGpiiT=Dm7!x${f9)=WG+hiV)Y6_#@-Uj(7g7c+}BP@82@2c=zJfi;JIM6ygVfFxk6NO5BjY|GXIe;UPc%!%6m81y7<zo<k|UHG(-l((*=Z==go`WwykKSO<OH3;qy)zS7t|RfBnWtdi*oCD+jJO<V+t_rriR^(P1Fdzn1d-U)dz*i185NcJHFr$`)sxTl*pUmS62l&!6IpJF5pWQ=vaQo$czDGlh^YCZkB3_hC!Z>?EbyWC6RT(UTxw#q+yg!{2tj4Q-#a8%;P@rDVN?~?I~7q3|0YB;Jibzi+f{(N7(!L){BdM*Bd{UZ~j_iy!dTD%PgL%O1Ct>JvP1Di$KbctG2d;8vYCuUI9$lwEgmVKc2#qKsUKv2PW9e2HRjV;>l-ab#(f**D`gamUrILJ&|7@+2ERJVEH7N(z>74rg)85pK7?=XDdEVKL6tX)R?X!`Zkw-B1RS?uAV7Ts|y@o1kB;hVCW%v$gGkZE9vPnASqGRJs$>OjnHim`9>Quz6q6+`(DyRV`!QYU8VO&a@2$fTO+j3KV!(;HnpyL|cn<>fEM(a3}$i}W|R8^tjYo!8dv=#i&dsJV6iRLxn`Z-cK}V<C4)W%pnq9`W*>N*0W`?hi;5cz|J416&+JjNQoyYDOrYk__SV48Ep~A3?1)dgcD;3~6MRYd1sj*5?i!%J@j&r7z9d_V1xI3$1je_o@5P78J4fA`)|*GZSRTTNswic8Kh5CS#CkD-dfT4lA1QYycfysRd0auUJ;Cr{wR>uu~}E^eOYhr}VR@AG3(ubl;<YmzF>k4(QM>CV4@>n!~T5XPg!?PqPImsURN|6^HO{rCGDRi9C5?6UhfKPoi1pLoVxnX1^CV;NMyLCQ`TuID*7Ph|TK<I?R)Dj5P+odwV%J0yqLK?T?FxtIf#h(bS59*bEcM31;53=!r&Jn=*QFIA$Cs6s9k%M5Mp1Q#__n95@Cs5^8G}y~80h&z3J5os<}&ytTX^c?cK_(q87$i{`7rbM7XQVg`hqhIot8g33%bTXW+zb2H8A2;=JpS6JPyj<rd3>l_4iQ;|W@sDyoZtT3UHl%E&$c5`NsY=vi=BUWnI&)Z}Fa|mE>nDO7#(V*ZZ!Wy>)XS)OCl<+uvn?y4ox5BeOUJ`zZCzeqs0e8f<3WZ`|MJHWzbjY0>`X9uI6up#F^OYde-9M8RQMaul8jVXd+fAjhZtHW@MZ-c5ClfRl6L2H{j!nS8Cm@e${s;psZ`@QkoMHRUnC2DB1in_1{KbVjptQscvB<^+e!sUe>N-iAsNmuB+<=cRz*kA+x(U8+W|6$7_9Oxty4z(+EJ00T+2VL{w)9)|aPkmOw~I`nTYSuA@$TZ;ORIfYqB|J&2{L8kDvnuhQHRVVgC5C>zg0Hk$F#z1ESKw)SvF6&pJns>q+6~@deSL>@er}-EwlE#CP7oT4;XV6RDQ~c3A@yT91WvKwV9F}Qp}qe{ascH;lnuX7$Y_-?K>;cxu-R#f;~00vL9`Ff-TmkF+IQwCe6sc7}00eLmg#rM0TDLsN;#&mdvzlg7Z}i;ezb-WP4>1k#%HmS5lZ?J6jfOV$7YWIBN8(*cQs1uE=SIRCSdd=k7U>4`^?5dW{a-5`hVZ`p#`hUY97n$Yf87tMN#C3MY*cgSV&#4RqAAXk)u*EaYg~5I`v73`mvx7|9gGC24&w9>F*P?(J%m4ux=KMKNJ({>o~20|Eqzdd7;8zdXg%s)<+%RgFU6pvtnbgZ)43%*4RyRAFucQ_nBxu*qd%tyz}LgJDFuK~-%^XT%j8<=tV6!kuPp3}fe0-_@Zu8IK-~Vn?s)9_hlv_nj@zvWbFHB)+_i`&Z<DR%(u3U)BO{KI;-{S)Yt2b554XnAJ^<VY-pql@41e5V2f<)vxHojnjT+ThV@MiPPeK7?`;q>#@n}5+kOb@GfKR8Q=#IQ9@Gyx()7w(gerS1Gecy@Qc_s#-Ze*KDM<8R1TLXhhrPu<R~XEavP?rb`%@|v653xESH4ZbT;>;Z)j}pb$$27z69a(>3x^koj{T<XUtlm@^YJII#EljNidk%&&?d|@nKo-c#etYH<#!*j@-Z17tu;rd-`#S1@UL2&31WX(~#@Te+c{+6R8izk~sH5w{o?HQsBkw=dg-(HN7Q174<nw2>Ah48;mWPu)|+A)t9i&Z+2wDsr|2!wiL{hAKCX(g6W&Z=o+LVVDrscw#(w>^Fi?1!OcyG;~2bFRI|8=$UVLVFf;n_<%2g|@$^0xE_TCq^r&K>;<quEl_aYv{&UpUH<;-wr<ZA=p88+CyZ+p_#;M{`7y8PhDc6(IMlL3(-UC{8(tbn)Eo>*lzs)x?mlTAQ_+uHx1H&F$kYY_nrALi5eGmRg_C?Q7Ws92pKphUy{W{H~QOpW>(~43l_fw&9FKCP%qy>4m!|xoxEv%ohSDe^iPmyP^%zH<PC6iz5smqPNCWW05T{<H_a*S`ad5oOhir*ddfN|zj`|6Cg-&)t|ZtaU|0pQkC@Va+Rb|WyEi2!J0U9H26AUdJmUWH8=zB>s&3~z_TlnPT;I7bjmQqY7FBDBrc;+uDBGa~s5vxBx`C&m<FqMbigU$DJ5v$Nu)=y4I;FCJ2<y#-@p_!ygcq8%u^b5@*oEQxoT0S$kqg%oZdIjLz~iq}gu$2o*4m(a1;_?C1bt?+~5N9VS8ll`^I&rg}Gr_?ok-;uh5$U{(fNZ!7;RXJAlgCy-xasNU|>WL@|bX@Ujf>?(cAuns{`kFMs@K?LkV;#$|=})EsAD3aPZs%d~&WfEY@*j$#s4*W#4-oEE@%-irH)i<d!cJ0ac~)o(xOBTb2l;qd5%ZlIwt0d!*QoL2$Z6t%t{M@eES4K1IB!<9o2{(C!oFT`Bq#8Ejlz*3!xPCMfgjdEh!xo?zIIv>%6m<q0JYq;4z~X2ysDAxCj4Yz*#Ly^4pQs##4ys$PH58;3n##pXTZ-{USRLB1i950RUv2m39aK}(?nxef{^rv^>3JBW{<0o>K*!+xU_PSxA2q?2hRomtHSvQmyavQ6E7Gl<T<PvD#K^rA{r0NVX&MIRB}i<MF&C}e^NI=8-p#n^H~PCRZKtLLT|O$3IZ;yh-u^E)9jok_+}$fMNpHkwsX^yqI;YZPDEIG^oO)i{cNnQDO^k1r&I;v@7i0U;ard~Q?WNYta!wpH`}Y(zp}LTZSm$r@O~ME)5dWf-=97zi_tFs6Y$v<R##tvmgm)D*4W}fu%7KUvm0XF(0HiWWijzgU+D=K6Ec$P!PYtFB(ZWk19`A9p#+u|7K*}G>}U^0eJlFC(e8IAkCcmk@xzPP&(#Ng@)!WLkM*wAKc74q871b=X~%2CfFpVuuGSVcKnKIJI1X>zKchfuhO+}kV`rUB!0cIL16Qx`vFih|NIG?Bu;@}zjh@7?xPULU5x1N{DH#$Rd0WaJd%@===gxVi<+AONNOABqo=V$M`_?&(m_kgc2}4ta&&}!+>Vpu+=uoU>?OE;p)>tntYB~&VMR?gKWvbMUYu3<<JI))wXVjg}YP)fB#UB53gwcw!%@fP&vo!?ae8O0Jt|>bj^vEaHBydJe5a=v2C(|yfl}C=uJd@pkMHx{k0mT&)y{R;nDpMG!Ve#|A353uZ<kc&{cyAcRhUwwTzM9o0O&CrjWLe!jf+2#RH5;|CyRi$;E7VAD)Kjw4NIbz%B42&_*c7XT9>^3zyoKWeIsVm()p|SfX0Z|L^!wkYQLPHg08sZR{OnX8o~OPdJ<hCwN9^j7N;{DDuhV67h19YMo0~=@?qu!lRWYqU`}5+4^bNthLR;K1A~S`5W)8UpbdlrX<KL>Z7d$&8%8=w4*O7jM%dO1L5zezM?cfuRYffl35eeB7JJxP5D-<)|y|^fTSG;}m_K&YFE?>TRjcU$*CzyF{Xa%#LF+@j37h}{9f~yQ1LZc$1vp<BReB}kCFpH1e3Lg#+PcY?XFB7$=Wv{pEPn4^_UIz=ud`0w#xZ0NU@G=89@e?ztt?HNNd~l+2&96?;9cn<j+ro(+x0@yCEfU&k4(5`U97G%2s*DUJD;R!r5RT;?){BbI*8hh4f*#1)Z<o{Q0AnEPl|}YGl$1bGlXLrtP1UWmA_1$a2`2~vUnB&z>Pr~wVev8vc{&G)y|}`R(t62hK1riPO1m}i5>k)VZ{XKBd6b_){MyjwQLIDDdE~>?$e*dF91Y+|1_k>b=7Mc)3+q71JKDpiDnl<9rkmV~WRI%@op4+&BDYnLjxnAPR(#;JU1*HCMz|L3r0&#yY}CGYz<=Wx-qj9|g5_4!)zVF{JJmsV#Xc(9x^pcT)mM2X5^qftM94&h!jeba7t6X253{Oe>_3&+*;^PJ9(k296vwjN9W7I;_QvEtehvN}a9$|VCxvvgNEAvq63+IJ5Bs7}WmH1nkK-+)$rdc7G%sg};uug;jIO4p{hw>dRB>-a$eJ<)y7GN9e!bDj@bQzUUMz-{MpA9Lo=-*)UPNhfdvoU{29~9WMbC$Wxvbm`;hEzxgfq>@ZA8VQlB+_jFo^>vbF^!qfkqmPVe%r}+V?86CzTBsK*>mclm;7iJSler!oO13(z45Vp{teJJVHmQZ%zA!R~VIvyA$93PPjN(ZH`)Sop?9%bCM@HES@ss!?X<6Si^cUlBi`tOsZN627D)k1d5E@Or*1f5XVUGo8RM{Z~LX54A}3CcU6+u+NxrPp7*e{`|fX_8T1NTZxAp)hYdZ&yBb6;!M0dayc0)kmh=p<<)_UiqZz|yklHV0h)8KbS$J1g$L!}s7k;E$nj&Yxkan8~k5^6;5;f|4;e3t!#3sMZFuVs|4LEQ|H+mxjwNq59Ncd{g&mU_N!5xxuX{2Wo@?bTvOq!U5(_~n03a#A09OibmhH<LQ=s1Z&>#dTPK8c)YL!@Cy&WqSkDYBUxW@gvBn6nFm)6#EtCPckW!{S~ZF{nKW-c<0hte6(OAl)1m#}w@i&Krr}amsMBX?h&;<>9}XczZJ>6KrQLQ3;W-N!FCDwhKE_yi><Ik&*yHID|m=dcDM09URb4x#J)Ue?RdfjJ@{0tdxlq#^PjndUiAn_K%PCnV{-TI6p!ACBcD6x?sUj?u8olB!=xXvVxx>`ysqauypuy^1($K%yAy}fs}3_5xl57`9$@^=%68J`}FX)1lil?(-5|_ak%L7>$M4<g&n~TIFFnI@J|N74-G7z>5T8*E+I1R%+u7F7YTBgXd-Nv({SwBY_-M1;mMP;qr@(gd>7K)gl}A20AD08m`4@ZtSaZ$Pb4HPMlbQO3cY*c8S%(j0W%_v%FE{2@mv}N)6w^~J}_Ex;kfk{b`~Z}zaTprjoi=`9F?w%mm2RZ2?+pawl?*?Zfv67Mf@%+5o>Dbb{JqGI^`}3fz$Gp^6xhmjJVD~wwliiwYzURj*?|s5^uGOF;}i>x?E`^@hG1ecE|e%SV?k%72j8|anY4RuhDvw5m?i63;SR$x+#wt=wTzTNr|9IK6nD^II&LQd}Xu+a92EAq#!|2(i(m%Qd+l<Dm=howG2ZB1Xef5DYexSp<A3y5`My;o>$FHXF6p{lD%>MHx%29|CSSQdzY*kkj8`)^Dk|-ZcWk{m`b@EH?a}hS!F&Oe!;l^2qM`g+YXe0A9xdDxw{Gb8^M{jH}gSwv9`T4Q~yJ~iQ4I{{Xe=CU~*e)CvKLhc{tpB_Z>?F4QLgJOc~d+aMTfN#VpWfW8@Sf7!Lm?2S;rTy_?xGSfcICO7ERSvha`{Dtxr}UT+g!UgZE|48JNLsRGEoe0>e6Wd559@5~7cCsB>Nv7`Bc;eIzuK38Q@Usze;&p9zWbHBslXV4KYOIP%fZJMjYhgV_IqHUK~jd73&p%bgWz*8xOw97ll_ut)_M%Fczn8RCy|8FG!OH;bMb7m-#_TrtY2jRy{qVcb{c+s0WQ%IvjQg(IkIZ{(JDnF>kEeUL56TV0k*_iv39l@(=V&u01`4|WE9T<Nr0;t(}yRM~QT`b4Kx`b7oR46b8usLptcGzF}KW!Ww@$Y}W3GX`JS#i7&5p!)k>f!BMxnHiNtVupN$VO1<#Eo@Ty16_!!1ztNj3S@H;WCC?)DB5lh1^ap+KtD7W`E}<tNtR@Yjg9nKE~8GLA%bW&mPSXjM)l!ke9vF<HY*IUF`?vdf_&MV61M!xgFVT1gwPH5_QXHYROSHx0U)0(*A<}b05%aKRB>3VZ8i;v5xg@A@F#L<mg#P;wfjlwLB`$kZgJ(Ns<0#>lQk6+{L~|Iwx~j>*6rkE*iFHI^p?|`vwUPtH;0HtWXw;Ky%FY!GY7(9UKgci$&UjksJIL(tSiM#5jJF!T1>YhPd5T!H6x2l9mZq_f2ij*w&jiegO}AAY!xdD92PXbEBQ1_i;q>^6=rxy@Z3B<orMjF5!J%`mDGNPc<4A?|}?*{W`cdExt1<R@-s7ZSd#@BV+_Wu@`aln-OUj2&EYYcQQQDo46_d_fMq5rx#9@{#l`>`0!|N5N=obGT~mJQL*Zp2F)6qZfa>_k#Lfb_%wBH>f@o;!YNvrDa~%70=IwbLEQ3A@v_cZoMZ2I%Lp=c<!wGM2L`L3HuZe+KubV1<6wQrwwvq72q+jeCUCzb6otx-s#nZGc_r9=m;^QGDg5Z)NkOZjQpP)Cfuy({Spj>=Psa`SD0c_UnJTpFBteJ8OOl&1Rnf7Dka2e@Xq8fooj=j<h9j#nXKxSP-Xm;W9#5qTP@k>X#qr)(TdS|G48TI($_z{X)2recU;nv2g#Q(6+~Q8xD>b1DC>LjH?Ojwz;%-==U}y=4kF~b&4^D<b_cA1biAUHmV*hB`bTH5@6IhlqlMesm4`>t!-Vs63e3yY*dXb~A3tuy*rjEDsx9J&)f~2P4Q1$O3@rm-whIe$*g#_y3&ra^=&l;XF5<)2?pWGsY<ECs==tqS`#0rxos+>T?AxAgK_iF0!+SJ!e7~ztYaoq62$jP66xK|d<$0mdcj-RJEco?C&ZB*|xE`(_ah@0%+pU4Q;|8~S}qU-?UXz|d;k8RkWmd&IHW7^Ar6diU2ii}1!_<mY%HbGtddudD`NRVQg_CkzpY$~V?OgH%jNHJb8HUJ@}M~RzE4CfXN*WpQj#4U;7y+voTzVi6{^81J7(a~dk=hVI<CadC-3}tkewye+wd(XU8v)Es(!pcmV=FB|W?taKB*{oLoxjygeMRdPz9w6o`+d&;zt%2i-9NELvsx%QrxWjB&??6tIthp?U*Dv0~%Zv;GvpU+!yDJ9KPGg#2x5~c{1>_UG%(v72qh*zi?#S-L4-;9hFsP~wxL}y)=;yM4ro|OA$<4y}h6!Kx6y`cwbgFssA>O=xVdqQ73S{BmhkNa32+*~w<>te^;H%3s7xdZ~os|#+gl^uf-d3C0U~CvMeNEz%q$SmAJbrju9vwa@Pftp8vGqs!ve%2ty#{!#p(iW*5PaE_{3LlF4h_iag$I12vB6OS;+gqZ#q}<6iuoe=V%&nQw(^!O&D8&u@(*g~hWw$jE0&{66RS0|2bqR8eEy)g3PIxXW-tzsB+Qbl4nL4C-voVn9-v3IUsNY3m!L(znlIhQ3TMcI)z1(29t}@F+>01&Dyb@mRq-LLJ3afAyH^<y8qzR(f>V|RQ$jjK|B(IU4<yO%X_Neq_R~^C*nDDYbSsR~H6q+@UT^rIpf+mO_RRLMiueVMP>yG_WQDIg?hN(-#%P^TD2^rY5%gRmi5|X3PerAHq;W_HBzM`JVoFJ|i-3r*DJ#pWT%mfafZY8U2zZ08V~+$aCdnw9<6LN@s6cNfA!*?N57z&Bx0i}gbe!D^zMl0M(oya9_J7Ah5ctl*I-}?LDTHg|EpoDDSSDnxxQB!j^NHc@r_<(2eNjfc@S53nf8m{&2f~ZJ$4VGz7C)sxq-l+cJGit2Gx~P4q7Is~OUV@3CvlxS^Z&32_pG7XEM-%8yg-RsD%=<<GC{2ROXL7aDB0t^DbA@XHlCNo{DpnELiO#V@ikZz1Veu2Ekx8H)Y6n{->CPA*WyaeyM@a@j8KdSR~R4-GfJ2^S`II+in7d*%GL#|zRiwc&gh@<gMS$n9)k<7>P3qXg}+i%lKChKQ*YrdEw_<a{%!NO!T{yF+EcYy6!j-y7eZ{|8>wmQN#9s}S(7r_{^VZsxs*p*YSoq5KX+N4so}$J`3a^9dld0>@Dh=E*^qO^*2dlYEALG}&C_j_E<0oPA-<?VvSB1`kCib!w64=TIfp-SL^9VfH_?<BjmNigH-FL6u{X^oiWQCbw>Q@);}I-l7x(Dp!nWI}+fVGhoEvX9s#=lAAvE?yxwjIh^J?)i!ehQL&owdf+5{$Grl-3fP<&ddnq5Z^B<W*>6~Kl;xF{VBa?`XqOqw>_Z2!?2o7xWojLh$G0PE28w=f|hRzQ4P#the4G5W#I=D!rr^0WCb#ala@|AIW17Qx8K46??1IH}g2Z~8vzal7sK_S<;^v7`dI^FoC8I{Sxo8lnQq|7_@<#f^b5UWpO9jTSMoL9UFz4i;oa29or0GJ+L%j9=Zsjd};A0v%iaJ*Cqo%gxsXRG4S`nll}#PMm~+_NU3|48VjYA&_7@W(!U_{jzD(%^9~Vs(sIGe4clmAs;2e5&gDgT8Mq(zBfq0m$_Ftk3#xu$5TQo?s8U>Owb&D5-?nCEzh&3e`T^)>is{0R`z02G3A_f2(dvZb#d=`ccSqZdsuv`;4SPPd`ZX`FCycVJZRd33<806WVL!pqYM_BN1OpuAW4Y_K##~q9XXa%xM4pDy;4dSCbrouk`UrS5(`;vHNAwN_B!En_J=ga-j%{rO!S$a16J#je97n%@=4Aj;w)|2Q*vmGlw%!)CkQKayPo%c!(Uh{N1t*(9cm3*fMHmvyqB?D5<*6IoOKoXpi$LkxsY@j9R}Hnv#i?u9lX02_EOMR5xawB5V_=?kk@l<yj(w`7Z=Q}s1~X(*biF9**c_yBXm`Jxh-D`SC#qN%H`8h61a$jq8DK_W4B?Nm;Vd*yT6Y<uq6`pci;tlxOdC@4}(OtXgOR$fRCQN`RS)O&tG1?{Q2eUA4W*JYN2shND>=SwChaaCl`6IRx1`ju^o@V>{Am9zG1I0hUsPe)66^JOS#&*teR<XryN%rUh7|rA5Fa*-%c$PjymE2Ka0Ir*Gp%(gZJVaSjN*AQ~WJbfMT)9>SN(fZWqyIg{*!8z~biz%$**@vYSSq<@z|s@Tx?h+8aInrTD``)Rrpn%VDMTM(<y|ejY;qS8tyE`{3p4QNLRis(cP^3@aYznW9OANlhlsF541FzkftN@>n@oX5}?3+;qq<kpuWOJ2KckBAxYoka`^pd)zN=B`l`R`X*M9!(RVf?PBtWkHnr?ZC3SzN4l!YIFCB!7JC()S4Lvb%ZLgBoY?G{Tv5}`qm>*z!tkz|x63H?uIadC>a8JmSo|rN;S6D)8Qic)Rnd(HR5<7Cdjrdn3}>60`A7~j+=|}G@nLy%`lxS?LA;q0Dvx@P!Z(hO55qTDEbTpd68?R9+#j7s!u@!5<x>Y<q;W<J5LaPuwENxBBkM;wdd%Xb^$1DQ)+P7&gkOGla&k0$tZ$z@IUb(cgW!YH&YuySW3hdz6QrS<b23<1*|MhrRZ*uG!Sfhig?frTT=n>g_3#S0p8$)t8dpoUXOh{?6mR6780zpMcZ<}=VeJcNT@S+U%u-`lglu_rwOvKUYL23F2Ep?h(DNEhmU-MbES?cEM{Jwi=Bd-LGt+t%5u%k78%7h(+f!|MVYPU`BJsMVl~8Qkc20N!r=r~x+kInUyi_)*+G&8b+hrp^l9cv{v@G3qY<sLX%F1AoDX$AH??>d%-r<xQUa`jZ@6fX3AzO)c7$3BqFE<INB1Hv1jvb~)2I;iB_mO&kn+mEdzDE000MK!lZdj6yaav5lw)Aw&C1n}YKWX1PiPPtb71{axE6LJ}B9dc6mN26-Tko6-Hqk!VAtroSOvtDfNLcN{il7;eH<*R<)SJ5TJmQ)=g%TIxGRv^o41{2Le>f26x`eLNT$Rh-^%^y)?J5N2YceJS7PE{q6k8M~^%WZLK&1HNHsXMZY@1b(HBYH1=|8I}C+lGe7k&2r`MEON77nq8ti~z3_A{SH2A&iPql}1|SgeI>6Eg>%#odH6=!&te$yQl%j$j>@<P3+AAa38aQh;*O0G=9S8|$!EkYLN9X@<2UL=>^7Yva!61L{g)L5O9`?y<cYk5ORl2^$lph2R*YoOtdZtM0kumC=oVQN;D7rZp4@R-78XN3)pD>7Qv{#_I;`0((h6CUCZ6t|A3DpR4)Dgt<aCApD$ya%#A?v_+#POMJUMcg+{iDT;U&#6>Lu#Q2s{&wP@Ki`o0fz}2l~{9P-uqsW-1j&#GZk(w!XRv#QZ96T?rv${=M=z2}lVDIeROdAg)nD(+Z1Xj_08W6}rya_U6RsN*0ixXknB5?yyn4PP#HhF6k{>uD%YrD1w?fw3j<I_Z0WQ}a$kB<=jFZw0nh5`~y>iH%-b&n51cWl|JPw((>d{oSuwK3p_f3Tj6JNHWy#w8a^IAlIPK*F*t-u}}oPT#(;*w)<<Gb|CGb>=H6%%8W4F#nL#6NI_bn~9FoucmhUC{*v9#~IXD0wo51glsYbLLc(Nq#?ga22n)nTy!{7_IchI0~j5*O_zeXX0cC7dzP=6oE5}Yh(;<GT(GTJrd5TFo*ipx&u2X;KZG~3z1L}5kMd|mZ#T%e#rhq&w4!H7gI)Ug9}rOcTA#ax3<_(iq`J#(Hl&b~oUAY5(KLYHKRPUno%meBKZBdBmwAexc~A92>!~(rCJmDm`*?!F6XRc1-E_^aj)f~|`MMiZ<Pk7<Hk&s!`p~$fWBWC(BD(R6ImC9b(|6qJyzX~JW@%ZYc9Q6n(tFBReo(BM&D9L=%j&!V3;D{#7mNCqUn)7QXnlTk$_@;3wD}>_Bfsc(Q}s`a^6dLZIxLP(P>bhRi)c@%DR*>qY_y?1nAqcG+}8A2{LJjEZ<4jf73}*v*+=-CnW)AlJ9Fntv~#m)tc4o2?jnj39h5w@LHHGoyh4e*+ofy3+zH2um~Q0OH|x$4NPW&&PIO+vs&73v$st8^Ae0(u#~7lUo5SPWzUk-RGx{ewW%y3EQlfAd587-~ce%Y{+U@q+m}^HH!jR9PLe=Egif#{v#7Sn&Lo|Sqk@dt^GQw3nmnqMWt0;m*!{MJLlYI*ja|*OexX+Oq2=m@1M<ax9`+tYSlS2yGtv~j2hsiyl@7io6<oI=)KP=hPlEP~+t`$ZQ(GLi@BF2YjM+uw=?K~w~-StoW-f~vw)%wn%#-@eT1@JWzD1<XO;O|D}=SYNN5W!0nnRV&auERr-!FW4e3mwoSxP7=G5~e)hM|7)4zL5|W>+4+n;rnLfwhNsirTYcW3^CvN`7*jcdN3M{EFGh(z>1A-H?72Tg$*k@TQ*9uQ}7fGU0ljrQprVGUGE;S&8|0;!xrpfg{UDfz;0%B*O67iB8xj%97Csxz||=R(4>Z#gg^`1s4Y9e;u$loZkjpxtDUho2gc+uts}y;?&g1gUi-DhutZq4<3qT%^+O(W-Cw5$xx&G$F(=pF5$a^D1D`~e7aly!A#+iY;MaQVE$mf*qda&FiF!h3viD#bheKGwxO4h(nrwZL{UF6cP97wNBz+fDM^5?B#@rwec&zcxL1DianNqtEr6%cfZXxNO*zo71lsS>SUN4da)w|lYWI%7=LqVDsb3vI%G#|@i7&p9ao+I&7o9E`qQF6Zeo;hKxnx79TY9?@kMH@C9UHkBM)}CRCod&uKC+woLj{9H9twhkVHMA<(^%Bj-EC$776-I|qFfMbRPp&1o^1wMnaNc9stqW*?-lu!rA$uRDBzad&+<#m{S#?sU$SSGT79;%A7+Ri;`seZN^dG03C4Ow4Y4&^j%l+c$7`7}w5tK7_)rEK{H*J>U)%)U1{RGy@=dzZ%wHIh3s}9GqH@g3b6)ZjVLq4^cUH#6DuLdqD>pq~MGzKqGK{zM8`@p^+q6+V{B0op+2d?Aw?%p{@S?@l*^F{5sPG8ZHJ)%G|xJIp@zSrL&>cMVnARWWU$E86LCVp!P17sI?3}pe8+T9oGgcjl&68JI~q`=@v==kI?!ZnW!EgI}XPI{q5ujLJuKAr$(6~CSjv_)iTLAruf$_FZ4gPs4e-YNYX_b;0G&a!}mORS8Gtu&?MP9D&Mv}c&`dw>1wU;i7lLQek'
    )).decode('utf-8'))
    # <<< END GENERATED DESIGN DIGEST <<<
