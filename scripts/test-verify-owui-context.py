#!/usr/bin/env python3
"""Round-trip parity test for scripts/build-owui-context.py's digest.

Proves scripts/owui-context-digest.json matches its markdown sources, is
deterministic, and stays within its size budget (per D-12-D-15): re-derives
a fresh digest from current source using build-owui-context.py's own
functions (never a re-implemented parser) and diffs it against the
committed artifact, then walks per-type/style-guide/size-budget/regression-
guard/splice-idempotency cases on top.

Usage: python3 scripts/test-verify-owui-context.py
Exit: 0 all pass, 1 a case failed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = ROOT / "scripts" / "build-owui-context.py"
DIGEST = ROOT / "scripts" / "owui-context-digest.json"
EXPECTED_TYPE_COUNT = 40
# Corpus-documented lower bound for per-type layout guidance — see the
# no-truncation policy comment in build-owui-context.py ("layout-conventions
# bullets 3-14"). A floor rather than an exact count, so a legitimate
# corpus edit is not reported as an extraction bug, while a span that silently
# collapses to a couple of bullets still cannot ship.
MIN_LAYOUT_BULLETS = 3
# The false-affordance class the 260911-dsc trailer-policy gate refuses: a repo
# path inside the digest. The OpenWebUI chat model consuming this digest has no
# filesystem — before the fix, 46 truncated lists ended with "+N more — see
# <this path>" and the live model echoed it back as "the reference files are
# not available".
BANNED_DIGEST_SUBSTRING = "skills/diagram-design/references/"


def load_build_module():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("build_owui_context", BUILD_SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load build-owui-context.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    failures: list[str] = []
    build = load_build_module()

    # 1. Re-derive a fresh digest from CURRENT source files using the build
    #    script's OWN extraction functions (not a re-implementation) and diff
    #    against the committed artifact — this is the backbone check: it
    #    proves determinism (same output twice) AND non-drift (matches
    #    source) in one assertion.
    fresh_digest = build.build_digest()
    fresh_payload = json.dumps(
        fresh_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    committed_payload = DIGEST.read_text(encoding="utf-8")
    if fresh_payload != committed_payload:
        failures.append(
            "committed scripts/owui-context-digest.json is stale — "
            "re-run python3 scripts/build-owui-context.py and commit the result"
        )
    else:
        print("OK: committed digest matches a fresh re-derivation from source")

    # 2. Explicit determinism check: build it twice in-process, compare.
    again_payload = json.dumps(
        build.build_digest(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    if again_payload != fresh_payload:
        failures.append("build_digest() is not deterministic across repeat calls")
    else:
        print("OK: build_digest() is byte-identical across repeat runs")

    # 3. All 40 keys present, no extras.
    types = fresh_digest["types"]
    if len(types) != EXPECTED_TYPE_COUNT:
        failures.append(f"expected {EXPECTED_TYPE_COUNT} types, found {len(types)}")
    else:
        print(f"OK: all {EXPECTED_TYPE_COUNT} diagram types present")

    # 4. Per-type field parity: full-list byte match, re-derived from source
    #    via the build script's own functions (not a re-implementation). Under
    #    the 260911-dsc no-truncation policy "parity" means the digest carries
    #    EVERY source bullet, in source order — there is no cap and no trailer
    #    branch, so a reintroduced truncation shows up here as a byte mismatch
    #    and again in case 4d as a banned-substring hit.
    field_parity_ok = True
    for slug, entry in types.items():
        source_path = ROOT / "skills/diagram-design/references" / f"type-{slug}.md"
        source_text = source_path.read_text(encoding="utf-8")
        raw_layout, _, _ = build.layout_conventions_for(source_text)
        raw_anti = build.extract_bullets(
            build.find_heading_span(source_text, ["Anti-patterns"])
        )
        for field_name, raw_bullets, digest_bullets in (
            ("layout_conventions", raw_layout, entry["layout_conventions"]),
            ("anti_patterns", raw_anti, entry["anti_patterns"]),
        ):
            if digest_bullets != raw_bullets:
                field_parity_ok = False
                failures.append(
                    f"{slug}.{field_name}: digest bullets do not byte-match the full "
                    f"source list ({len(digest_bullets)} shipped vs {len(raw_bullets)} "
                    "in source) — content/order drift, or truncation reintroduced"
                )
    if field_parity_ok:
        print("OK: per-type field parity (full-list byte-match) holds for all types")

    # 4b. Per-type name/best_for parity — calling the build script's own
    #     extraction functions, not re-deriving the regex independently, so a
    #     bug isolated to name/best_for extraction surfaces as a specific,
    #     named failure rather than only showing up as a generic whole-digest
    #     byte-mismatch in case 1.
    name_best_for_ok = True
    for slug, entry in types.items():
        source_path = ROOT / "skills/diagram-design/references" / f"type-{slug}.md"
        source_text = source_path.read_text(encoding="utf-8")
        expected_name = build.extract_title(source_text)
        expected_best_for = build.extract_best_for(source_text)
        if entry["name"] != expected_name:
            name_best_for_ok = False
            failures.append(
                f"{slug}.name: {entry['name']!r} does not match source title {expected_name!r}"
            )
        if entry["best_for"] != expected_best_for:
            name_best_for_ok = False
            failures.append(
                f"{slug}.best_for: {entry['best_for']!r} does not match source "
                f"{expected_best_for!r}"
            )
    if name_best_for_ok:
        print("OK: per-type name/best_for parity holds for all types")

    # 4c. Non-self-referential shape floor: unlike cases 4/4b this does NOT call
    #     the build script's extraction functions, so it stays blind to nothing —
    #     an extraction that silently returns []/"" for a type would still make
    #     4/4b pass (both sides call the same function) and only show up as a
    #     whole-digest byte-mismatch in case 1, whose remediation message would
    #     otherwise instruct a contributor to commit the corruption. anti_patterns
    #     is deliberately NOT floored here: the corpus's documented range starts
    #     at 0 (see PER_TYPE_CAP in build-owui-context.py).
    shape_floor_ok = True
    for slug, entry in types.items():
        if not entry["layout_conventions"]:
            shape_floor_ok = False
            failures.append(f"{slug}.layout_conventions is empty")
        if not entry["name"] or not entry["best_for"]:
            shape_floor_ok = False
            failures.append(f"{slug}.name/best_for is empty")
    if shape_floor_ok:
        print("OK: no type ships an empty layout_conventions or empty name/best_for")

    # 4d. Trailer-policy gate (260911-dsc): the digest's only consumer is an
    #     OpenWebUI chat model with NO filesystem, so the payload must never
    #     point at repo files. The pre-fix digest ended 46 truncated lists with
    #     "+N more — see skills/diagram-design/references/type-X.md" and the
    #     live model echoed the trailer back as "the reference files are not
    #     available". A substring check over the WHOLE committed payload covers
    #     every field at once (types, bullets, style_guide) and refuses the
    #     entire false-affordance class however it re-enters — a truncation
    #     trailer is just the shape it arrived in last time.
    if BANNED_DIGEST_SUBSTRING in committed_payload:
        offenders: list[str] = []
        for slug, entry in types.items():
            for field, value in entry.items():
                values = value if isinstance(value, list) else [value]
                if any(
                    isinstance(v, str) and BANNED_DIGEST_SUBSTRING in v for v in values
                ):
                    offenders.append(f"{slug}.{field}")
        for field, table in fresh_digest["style_guide"].items():
            if BANNED_DIGEST_SUBSTRING in json.dumps(table):
                offenders.append(f"style_guide.{field}")
        failures.append(
            f"digest contains the repo path {BANNED_DIGEST_SUBSTRING!r} — briefs must "
            "be self-contained (the OWUI model has no filesystem); offending "
            f"entries: {offenders or 'outside the scanned fields — inspect the payload'}"
        )
    else:
        print("OK: digest is self-contained — no repo-path pointers in the payload")

    # 5. Style-guide token values match style-guide.md's own tables.
    style_source = (
        ROOT / "skills/diagram-design/references/style-guide.md"
    ).read_text(encoding="utf-8")
    expected_style_guide = build.extract_style_tokens(style_source)
    if fresh_digest["style_guide"] != expected_style_guide:
        failures.append("style_guide does not match style-guide.md's own tables")
    else:
        print("OK: style-guide token parity holds")

    # 5b. Independent shape check — does NOT call extract_style_tokens again, so
    # it can't be fooled by a self-consistent-but-wrong extraction (a renamed
    # heading or truncated table producing a silently empty/partial dict would
    # make case 5 above pass since both sides call the same buggy function).
    expected_counts = {"tokens": 10, "typography": 6, "spacing": 7}
    shape_ok = True
    for field_name, expected_count in expected_counts.items():
        actual_count = len(fresh_digest["style_guide"][field_name])
        if actual_count != expected_count:
            shape_ok = False
            failures.append(
                f"style_guide.{field_name} has {actual_count} entries, expected "
                f"{expected_count} — possible silent extraction failure"
            )
    if shape_ok:
        print("OK: style-guide section counts match expected shape")

    # 5c. Per-type floor on layout guidance — the per-TYPE analogue of 5b's
    #     independent shape check. Cases 4/4c re-derive their expectations with
    #     the build script's own extraction functions, so a span that silently
    #     lost *some* (not all) of its bullets passes both sides; this floor is
    #     stated independently of the extractor. anti_patterns is deliberately
    #     not floored: its documented range starts at 0 (PER_TYPE_CAP comment).
    floor_ok = True
    for slug, entry in types.items():
        actual = len(entry["layout_conventions"])
        if actual < MIN_LAYOUT_BULLETS:
            floor_ok = False
            failures.append(
                f"{slug}.layout_conventions has {actual} bullets, below the corpus "
                f"minimum of {MIN_LAYOUT_BULLETS} — possible partial extraction loss "
                "(if the corpus change is intentional, raise MIN_LAYOUT_BULLETS here)"
            )
    if floor_ok:
        print(
            f"OK: every type's layout_conventions holds at least {MIN_LAYOUT_BULLETS} bullets"
        )

    # 6. Size budget.
    size = len(committed_payload.encode("utf-8"))
    if size > build.INTERIM_SIZE_BUDGET_BYTES:
        failures.append(
            f"digest is {size} bytes, exceeds interim budget of "
            f"{build.INTERIM_SIZE_BUDGET_BYTES}"
        )
    else:
        print(f"OK: digest is {size} bytes (interim budget: {build.INTERIM_SIZE_BUDGET_BYTES})")

    # 6b. The budget gate must actually refuse to write. Case 6 only proves the
    #     committed artifact is under budget — a refactor that moved main()'s
    #     DIGEST_OUT.write_text() above the budget check would pass it with zero
    #     signal. Drive the build script in a fresh subprocess with the constant
    #     monkeypatched low (never hand-edit the committed constant) and assert
    #     the non-zero exit, the diagnostic, AND that nothing was written. The
    #     override points at a temp path, so the committed digest is untouched.
    budget_gate_ok = True
    with tempfile.TemporaryDirectory(prefix="owui-budget-gate-") as budget_tmp:
        out_tmp = Path(budget_tmp) / "owui-context-digest.json"
        probe = (
            "import importlib.util, pathlib, sys\n"
            "spec = importlib.util.spec_from_file_location('b', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "module.INTERIM_SIZE_BUDGET_BYTES = 1\n"
            "module.DIGEST_OUT = pathlib.Path(sys.argv[2])\n"
            "sys.exit(module.main())\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe, str(BUILD_SCRIPT), str(out_tmp)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            budget_gate_ok = False
            failures.append("size-budget gate exited 0 on an over-budget digest")
        if out_tmp.exists():
            budget_gate_ok = False
            failures.append("size-budget gate wrote the digest despite exceeding the budget")
        if "exceeds interim budget" not in proc.stderr:
            budget_gate_ok = False
            failures.append(
                "size-budget gate failure message missing from stderr — got "
                f"{proc.stderr.strip()!r}"
            )
    if budget_gate_ok:
        print("OK: size-budget gate refuses to write an over-budget digest")

    # 7. Scatter-contamination regression guard (Pitfall 2): a permanent
    #    regression guard, independent of Plan 01-01's own one-time check, so
    #    a future un-wrapped variant added to any other type file cannot
    #    silently reintroduce this bug class.
    #
    #    Note: a naive "no bullet contains the substring 'bubble'" check (as
    #    literally specified in this plan) produces a false positive — the
    #    parent scatter type's own legitimate, un-contaminated Anti-patterns
    #    section correctly references "the **bubble variant** below" as
    #    vocabulary (see type-scatter.md line 32). Plan 01-01's SUMMARY
    #    documented this exact over-generalization as a deviation. The real
    #    contamination signal is the nested Bubble variant's OWN distinct
    #    "#### Anti-patterns" bullets (radius-proportional sizing, moving a
    #    bubble to avoid overlap, ink-ramp-vs-hue, etc.) leaking into the
    #    parent's digest entry — checked here directly against source, not
    #    hardcoded, so it stays correct if either section's wording changes.
    scatter_anti = types.get("scatter", {}).get("anti_patterns", [])
    scatter_source = (
        ROOT / "skills/diagram-design/references/type-scatter.md"
    ).read_text(encoding="utf-8")
    nested_match = re.search(
        r"^#### Anti-patterns\n(.*?)(?=\n#{1,6}\s|\Z)", scatter_source, re.MULTILINE | re.DOTALL
    )
    nested_bubble_anti = build.extract_bullets(nested_match.group(1)) if nested_match else []
    contamination = sorted(set(scatter_anti) & set(nested_bubble_anti))
    if len(scatter_anti) > 8:
        failures.append(
            f"scatter.anti_patterns has {len(scatter_anti)} entries, "
            "exceeds regression-guard ceiling of 8"
        )
    elif contamination:
        failures.append(
            "scatter.anti_patterns contains bullet(s) from the nested Bubble variant's "
            f"own anti-patterns section: {contamination}"
        )
    elif not nested_bubble_anti:
        failures.append(
            "could not locate the nested Bubble variant's '#### Anti-patterns' section in "
            "type-scatter.md — regression guard cannot verify non-contamination"
        )
    else:
        print("OK: scatter anti_patterns shows no bubble-variant contamination")

    # 8. splice() idempotency (RESEARCH.md Pattern 2): a synthetic fixture
    #    string stands in for tools/diagram_design_tool.py's eventual shape
    #    (which does not exist yet in this phase).
    fixture = (
        f"class Tools:\n{build.DIGEST_START}\n"
        f"    _DESIGN_DIGEST: dict = {{}}\n{build.DIGEST_END}\n"
    )
    once = build.splice(fixture, fresh_payload)
    twice = build.splice(once, fresh_payload)
    if once != twice:
        failures.append("splice() is not idempotent")
    else:
        print("OK: splice() is idempotent")

    # 9. parse_pipe_table's contract on synthetic input — the current corpus
    #    exercises neither escaped pipes nor trailing row whitespace, so the
    #    parity cases above cannot notice a regression back to a naive
    #    row.split("|") (which cuts an escaped-pipe cell in two and shifts every
    #    later cell left) or to a row regex that silently drops
    #    trailing-whitespace rows.
    table_ok = True
    escaped = build.parse_pipe_table(
        "| token | purpose | light |\n"
        "| --- | --- | --- |\n"
        "| `paper` | the `value \\| escaped` surface | `#f5f5f5` |"
    )
    if len(escaped) != 1 or len(escaped[0]) != 3:
        table_ok = False
        failures.append(f"parse_pipe_table split an escaped-pipe cell: {escaped}")
    elif escaped[0][1] != "the value | escaped surface":
        table_ok = False
        failures.append(
            f"parse_pipe_table did not resolve a markdown-escaped pipe: {escaped[0][1]!r}"
        )
    trailing_ws = build.parse_pipe_table(
        "| a | b |\n| --- | --- |\n| one | two |   \n| three | four |"
    )
    if len(trailing_ws) != 2:
        table_ok = False
        failures.append(
            f"parse_pipe_table dropped a row with trailing whitespace: {trailing_ws}"
        )
    if build.parse_pipe_table("") != []:
        table_ok = False
        failures.append("parse_pipe_table did not return [] for an empty section")
    if table_ok:
        print("OK: parse_pipe_table handles escaped pipes and trailing whitespace")

    # 10. The compressed-embed size gate must actually refuse to write (D-01).
    #     Case 6b proves the *uncompressed* guidance-volume gate; a refactor
    #     that dropped the MAX_EMBEDDED_DIGEST_BYTES check from main() would
    #     pass this whole suite with zero signal. Same subprocess-probe
    #     discipline as case 6b, plus the tool suite's two refinements:
    #     `sys.dont_write_bytecode` in the probe, and comparing bytes against a
    #     pre-captured copy of the tool artifact instead of mere absence.
    embed_gate_ok = True
    tool_path = ROOT / "tools" / "diagram_design_tool.py"
    tool_bytes = tool_path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="owui-embed-gate-") as embed_tmp:
        digest_tmp = Path(embed_tmp) / "owui-context-digest.json"
        tool_tmp = Path(embed_tmp) / "diagram_design_tool.py"
        tool_tmp.write_bytes(tool_bytes)
        probe = (
            "import importlib.util, pathlib, sys\n"
            "sys.dont_write_bytecode = True\n"
            "spec = importlib.util.spec_from_file_location('b', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "module.MAX_EMBEDDED_DIGEST_BYTES = 1\n"
            "module.DIGEST_OUT = pathlib.Path(sys.argv[2])\n"
            "module.TOOL_OUT = pathlib.Path(sys.argv[3])\n"
            "sys.exit(module.main())\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe, str(BUILD_SCRIPT), str(digest_tmp), str(tool_tmp)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            embed_gate_ok = False
            failures.append("embedded-digest gate exited 0 on an over-budget embed")
        if tool_tmp.read_bytes() != tool_bytes:
            embed_gate_ok = False
            failures.append("embedded-digest gate rewrote the tool file despite exceeding the budget")
        if digest_tmp.exists():
            embed_gate_ok = False
            failures.append("embedded-digest gate wrote the digest despite exceeding the budget")
        if "exceeds embedded digest budget of" not in proc.stderr:
            embed_gate_ok = False
            failures.append(
                "embedded-digest gate diagnostic missing from stderr — got "
                f"{proc.stderr.strip()[-400:]!r}"
            )
    if embed_gate_ok:
        print("OK: embedded-digest gate refuses to write an over-budget embed")

    # 11. Codec determinism (D-01's "same input → same compressed bytes"):
    #     encode the committed payload twice in-process and require identical,
    #     non-empty output, then round-trip a synthetic block through
    #     decode_digest_block() and require it to yield the committed digest.
    #     This is the in-suite witness that a byte-identical regenerated
    #     artifact carries the same content.
    codec_ok = True
    first_blob = build.encode_digest_payload(committed_payload)
    second_blob = build.encode_digest_payload(committed_payload)
    if not first_blob:
        codec_ok = False
        failures.append("encode_digest_payload returned an empty blob")
    elif first_blob != second_blob:
        codec_ok = False
        failures.append("encode_digest_payload is not deterministic for the committed payload")
    else:
        synthetic = "class Tools:\n" + build.digest_block(first_blob) + "\n"
        try:
            round_tripped = build.decode_digest_block(synthetic)
        except ValueError as exc:
            codec_ok = False
            failures.append(f"decode_digest_block could not read digest_block() output: {exc}")
        else:
            if round_tripped != json.loads(committed_payload):
                codec_ok = False
                failures.append("decode_digest_block(digest_block(blob)) != the committed digest")
    if codec_ok:
        print("OK: codec round-trips deterministically for the committed payload")

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        print(f"\n{len(failures)} case(s) failed.")
        return 1
    print("\nAll digest parity cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
