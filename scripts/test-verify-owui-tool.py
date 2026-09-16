#!/usr/bin/env python3
"""Loader-fidelity contract harness for tools/diagram_design_tool.py.

OpenWebUI does not `import` a tool file: it reads the stored source text,
blindly rewrites a few substrings, parses a naive frontmatter block, then
exec()s the result inside a module whose __name__ is `tool_<id>`. Most of what
makes this plugin correct is therefore a property of the *boundary* between
this repo and that host — invisible to a plain `import`. So this harness loads
the tool the way the host does and proves the packaging (PKG-01/PKG-02),
context (CTX-01/CTX-02), reliability (REL-01), public-surface and valve-shape
contracts on top of it.

Two loading idioms coexist here deliberately, and the distinction is the point:

* `load_build_module()` — importlib, for the repo-side build script only
  (Phase 1's tested idiom; importlib is fine for scripts this repo owns).
* `load_tool_source()` — read the tool file as TEXT and exec() it, never
  `import`, never importlib. OWUI's loader is `types.ModuleType(...)` + `exec`
  with a non-`__main__` name; a plain import would miss the frontmatter
  contract, top-level-statement failures, unguarded main-ish code, and
  relative-import/package assumptions.

Usage: python3 scripts/test-verify-owui-tool.py
Exit: 0 all pass, 1 a case failed.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import urllib.error
from pathlib import Path

# Before any dynamic load (the tool file, the build script, both subprocess
# probes): no `__pycache__` may land in the tree as a side effect of testing.
sys.dont_write_bytecode = True

# Windows CI (run 35117831059): run every audited call on the Selector loop.
# This is noise reduction only — the Selector loop still builds a socketpair
# wakeup on Windows, so the narrow selfpipe filter in _denylist_hook below
# remains the actual guarantee that host-runtime sockets are not mistaken for
# tool I/O (and that real tool sockets are not excused).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = ROOT / "scripts" / "build-owui-context.py"
DIGEST = ROOT / "scripts" / "owui-context-digest.json"
TOOL_OUT = ROOT / "tools" / "diagram_design_tool.py"
# Plan 02's committed validation corpus (the clean base case 20 mutates, the 31
# defect fixtures + manifest the VAL-02 case drives, all emitted by
# scripts/build-validation-fixtures.py and never hand-edited).
FIXTURE_DIR = ROOT / "scripts" / "fixtures" / "validation"
EXPECTED_TYPE_COUNT = 40
# Context-budget ceiling for ONE rendered brief — the 120,000-byte *file* gate
# (MAX_TOOL_FILE_BYTES) measures the shipped file and cannot see response size.
# Largest measured brief is ~6.9 KB (a 5.3 KB worst-case type entry + the
# ~2.3 KB style-guide append + scaffolding). A ceiling rather than an exact
# size, so legitimate corpus edits do not fail the suite, while a silent
# blow-up (a duplicated style-guide append, an unbounded loop) cannot ship.
MAX_BRIEF_BYTES = 9_000
# The four model-callable methods this release ships. Pitfall 3: OpenWebUI
# exposes every public callable of a tool instance as a separate model-callable
# tool with its own schema, so the public surface must stay exactly this set.
PUBLIC_METHODS = (
    "apply_brand_profile",
    "export_diagram",
    "get_design_brief",
    "validate_diagram",
)
# The only frontmatter keys a tool may declare. Any other `word:` line inside
# the module docstring is prose that OpenWebUI's naive parser would promote to
# metadata (Pitfall 2) — and a `requirements:` key would trigger a runtime pip
# install on a real instance.
ALLOWED_FRONTMATTER_KEYS = frozenset({"title", "author", "description"})
# OpenWebUI's replace_imports() blindly str.replace()s these substrings in
# *stored* tool source — comments included — so a comment mentioning one would
# be silently rewritten into broken code on a real instance.
REPLACE_IMPORTS_TRIGGERS = ("from utils", "from apps", "from main", "from config")
# Audit events that mean real filesystem/network/process I/O. A denylist, never
# "zero events": asyncio.run() itself emits benign object-construction events
# (`socket.__new__`, asyncgen finalizer hooks) that are not I/O. Audit hooks are
# used instead of patching `builtins.open` because `pathlib.Path.read_text`
# demonstrably bypasses that patch, while audit events fire from inside the
# interpreter for `builtins.open`, `io.open`, `os.open`, sockets and subprocess
# alike — attribute rebinding cannot dodge them.
IO_EVENT_DENYLIST = frozenset(
    {
        "open",
        "os.remove",
        "os.rename",
        "os.truncate",
        "os.chmod",
        "shutil.copyfile",
        "mmap.__new__",
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "socket.sendto",
        "socket.sendmsg",
        "urllib.Request",
        "http.client.connect",
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "_posixsubprocess.fork_exec",
    }
)
# Appended to by the process-global audit hook installed in case 18. An audit
# hook is irremovable, so every later file operation in this run (Temporary-
# Directory cleanup, reading the committed digest) keeps appending to it — which
# is exactly why the no-I/O assertion is a DELTA SLICE over the audited window,
# never a whole-list emptiness check.
AUDIT_HITS: list[str] = []
# asyncio's Windows wakeup is host runtime, not tool I/O. On Windows the event
# loop's self-wakeup is a loopback socket PAIR built lazily at the first await
# inside an audited window: bind(('127.0.0.1', 0)) asks the OS for an ephemeral
# port, then connect() targets that port. Linux/macOS use os.pipe() for the
# same wakeup and emit nothing denylisted — which is why these events only
# ever red the Windows CI legs (run 35117831059: 7 audit failures, every one a
# loopback bind-to-port-0 / connect-to-that-port pair, plus the `_socket`
# import that machinery pulls in). The hook licenses the pattern NARROWLY:
# each bind to a loopback port 0 issues exactly one license and each loopback
# connect consumes one — a tool connecting to a real loopback service (without
# first binding port 0 of its own) still lands in AUDIT_HITS, as does any
# non-loopback socket event. Filtered events land in SELFPIPE_HITS instead,
# sliced per window like AUDIT_HITS, because seeing the wakeup machinery run
# is also what licenses dropping its `_socket` import from the strict
# call-time-import assertions (cases 18/21).
SELFPIPE_HITS: list[str] = []
_SELFPIPE_LICENSES = 0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _reset_selfpipe_licenses() -> None:
    """Zero the license balance so each audit window re-arms cleanly — a
    window whose bind went unconsumed cannot silence a later window's real
    loopback connect."""
    global _SELFPIPE_LICENSES
    _SELFPIPE_LICENSES = 0


def _is_selfpipe_wakeup(event: str, args) -> bool:
    """Narrow loopback-socketpair-wakeup predicate (see SELFPIPE_HITS above).

    bind: the address is (loopback host, 0) — an ephemeral-port request, the
    socketpair bootstrap signature. connect: a loopback host AND a license
    issued by an in-window bind is still unspent. Anything else is False.
    """
    try:
        address = args[1]
    except (TypeError, IndexError, KeyError):
        return False
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    if address[0] not in _LOOPBACK_HOSTS:
        return False
    if event == "socket.bind":
        return address[1] == 0
    return _SELFPIPE_LICENSES > 0


def _flagged_call_time_imports(names: list[str], selfpipe_seen: bool) -> list[str]:
    """Call-time import names minus the wakeup machinery's own `_socket` pull
    — and ONLY that: `_socket` is dropped only when the same window actually
    ran the socketpair machinery; imported with no wakeup events in the
    window, it stays a flagged anomaly."""
    if selfpipe_seen:
        return [name for name in names if name != "_socket"]
    return list(names)


def _denylist_hook(event: str, args) -> None:
    global _SELFPIPE_LICENSES
    if event not in IO_EVENT_DENYLIST:
        return
    if event in ("socket.bind", "socket.connect") and _is_selfpipe_wakeup(event, args):
        if event == "socket.bind":
            _SELFPIPE_LICENSES += 1
        else:
            _SELFPIPE_LICENSES -= 1
        SELFPIPE_HITS.append(f"{event}: {args!r}")
        return
    AUDIT_HITS.append(f"{event}: {args!r}")


class RecordingEmitter:
    """Stands in for OWUI's injected __event_emitter__ — a plain async callable.

    OpenWebUI passes an async callable as `__event_emitter__` (or omits the
    parameter entirely), so the mock must be callable-and-awaitable, not a
    method-bearing object.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, payload: dict) -> None:
        self.events.append(payload)


def load_build_module():
    """importlib-load the repo-side build script (Phase 1's tested idiom).

    importlib is correct for scripts this repo owns, and wrong for the tool
    file — see load_tool_source().
    """
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("build_owui_context", BUILD_SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load build-owui-context.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_tool_source(path: Path) -> dict:
    """Load the tool file the way OpenWebUI does: as text, through exec().

    Mirrors backend/open_webui/utils/plugin.py::load_tool_module_by_id —
    `types.ModuleType(f'tool_{id}')` + `exec(content, module.__dict__)` with a
    non-`__main__` name, so unguarded top-level code behaves here exactly as it
    would on a real instance. `__file__` points at the real path purely as a
    debugging aid (the loader points it at a temp copy).
    """
    source = path.read_text(encoding="utf-8")
    namespace = {"__name__": "tool_diagram_design", "__file__": str(path)}
    exec(compile(source, str(path), "exec"), namespace)
    return namespace


def frontmatter_keys(source: str) -> tuple[bool, set[str]]:
    """Replicate OWUI's frontmatter parser; return (line1_ok, keys).

    The loader requires line 1 to be exactly a triple double-quote (the module
    docstring delimiter — anything else, a shebang or a `from __future__` line,
    makes it return {} and the metadata silently vanishes from the Admin UI),
    then scans lines[1:] to the first line containing that delimiter, accepting
    every `^\\s*([a-z_]+):` match as a key.
    """
    lines = source.split("\n")
    if not lines or lines[0] != '"""':
        return False, set()
    keys: set[str] = set()
    for line in lines[1:]:
        if '"""' in line:
            break
        match = re.match(r"^\s*([a-z_]+):", line, re.IGNORECASE)
        if match:
            keys.add(match.group(1))
    return True, keys


def main() -> int:
    failures: list[str] = []
    build = load_build_module()
    tool_source = TOOL_OUT.read_text(encoding="utf-8")
    committed_payload = DIGEST.read_text(encoding="utf-8")

    # 1. Frontmatter hygiene (PKG-01, Pitfall 2): line 1 must be exactly `"""`
    #    (else the loader drops the metadata), the parsed key set must be the
    #    allowed metadata keys and nothing else, and `requirements` must be
    #    absent — a present key would trigger a runtime pip install on a real
    #    instance.
    line1_ok, keys = frontmatter_keys(tool_source)
    if not line1_ok:
        failures.append(
            'tool file line 1 is not exactly """ — the loader would drop all frontmatter'
        )
    unexpected = keys - ALLOWED_FRONTMATTER_KEYS
    if unexpected:
        failures.append(
            "unexpected frontmatter key(s) parsed from the module docstring: "
            + ", ".join(sorted(unexpected))
            + " — a prose line matched OWUI's `^[a-z_]+:` parser (Pitfall 2)"
        )
    if "requirements" in keys:
        failures.append(
            "frontmatter declares `requirements:` — a real instance would pip "
            "install it at load time"
        )
    if line1_ok and not unexpected and "requirements" not in keys:
        print(f"OK: frontmatter is metadata-only ({', '.join(sorted(keys))})")

    # 2. replace_imports() trigger substrings (PKG-01): OpenWebUI blind-rewrites
    #    these four substrings anywhere in stored source, comments included.
    triggers_found = [t for t in REPLACE_IMPORTS_TRIGGERS if t in tool_source]
    if triggers_found:
        failures.append(
            "tool source contains replace_imports() trigger substring(s) "
            + ", ".join(repr(t) for t in triggers_found)
            + " — a real instance would rewrite them into broken code"
        )
    else:
        print("OK: no replace_imports() trigger substring appears in the tool source")

    # 3. Exec-load fidelity: the file must load the way the host loads it, and
    #    the spliced digest must carry every type. Only the COUNT is asserted
    #    against a literal; the key list itself is always derived from the
    #    digest (never hardcoded here).
    namespace = load_tool_source(TOOL_OUT)
    Tools = namespace.get("Tools")
    if Tools is None:
        failures.append("exec-loaded tool namespace exposes no `Tools` class")
        # Every later case needs the class; report what was found instead of
        # crashing with a traceback that would bury the diagnostics above.
        for failure in failures:
            print(f"FAIL: {failure}")
        print(f"\n{len(failures)} case(s) failed.")
        return 1
    # Isolation helper for the brand-profile store (BRND-01/BRND-02, research
    # Pitfall 4): profiles are read from disk at call time, so ANY case that
    # calls validate_diagram must run against an EMPTY profiles dir — a stray
    # brand-profiles/*.json from an earlier case would flip the default-palette
    # verdict the first-line assertions depend on. The TemporaryDirectory is
    # held in a list rather than a `with` block because the helper returns
    # before the call site is done with it; its finalizer removes the directory
    # at interpreter exit, so the repo tree is never polluted.
    profile_tmp_dirs: list = []

    def _isolated_profiles():
        holder = tempfile.TemporaryDirectory(prefix="owui-profiles-")
        profile_tmp_dirs.append(holder)
        tool = Tools()
        tool.valves.profiles_dir = holder.name
        return tool

    digest_types = Tools._DESIGN_DIGEST["types"]
    if len(digest_types) != EXPECTED_TYPE_COUNT:
        failures.append(
            f"Tools._DESIGN_DIGEST carries {len(digest_types)} types, "
            f"expected {EXPECTED_TYPE_COUNT}"
        )
    else:
        print(f"OK: tool exec-loads and carries {EXPECTED_TYPE_COUNT} diagram types")

    # 4. Staleness backbone: the digest embedded in the committed tool file must
    #    equal the committed digest artifact — one drifts, both are stale.
    if Tools._DESIGN_DIGEST != json.loads(DIGEST.read_text(encoding="utf-8")):
        failures.append(
            "Tools._DESIGN_DIGEST does not match scripts/owui-context-digest.json — "
            "re-run python3 scripts/build-owui-context.py and commit the result"
        )
    else:
        print("OK: embedded digest matches the committed digest artifact")

    # 5. Splice fixed point: re-splicing the committed tool file with the
    #    committed payload must reproduce that same file byte-for-byte. This is
    #    the in-test counterpart of the CI regenerate-and-diff gate.
    #
    #    The compressed embed (D-01) makes the failure path ambiguous: RFC
    #    1950/1951 guarantees decompression interoperability, NOT encoder byte
    #    identity, so a platform whose zlib encoder differs from the one that
    #    produced the committed artifact reds this case with no digest drift at
    #    all. When the bytes differ, decode BOTH embeds and compare digests —
    #    equal digests mean the codec, not the content, so the reader gets the
    #    regeneration recipe instead of a bare diff (research Pitfall 4).
    spliced = build.splice(tool_source, committed_payload)
    if spliced != tool_source:
        try:
            committed_digest = build.decode_digest_block(tool_source)
            rebuilt_digest = json.loads(committed_payload)
        except Exception:  # noqa: BLE001 — the embed itself is unreadable; report drift
            committed_digest = None
            rebuilt_digest = None
        if committed_digest is not None and committed_digest == rebuilt_digest:
            failures.append(
                "tools/diagram_design_tool.py is not a fixed point of splice() with the "
                "committed payload, but the decoded digest matches: your platform's "
                "zlib encoder differs from the one that produced the committed "
                "artifact — the content is fine, the bytes are not. Regenerate on "
                "ubuntu + python 3.12 (python3 scripts/build-owui-context.py) and "
                "commit, or pin CI's regenerate-and-diff step to one matrix leg"
            )
        else:
            failures.append(
                "tools/diagram_design_tool.py is not a fixed point of splice() with the "
                "committed payload and the embedded digest does not decode to the "
                "committed digest — real digest/embed drift. Re-run python3 "
                "scripts/build-owui-context.py and commit the result"
            )
    else:
        print("OK: committed tool file is a fixed point of splice() with the committed payload")

    # 6. PKG-02 file budget, read from the build script's own constant (never
    #    duplicated here), measured on bytes — read_text() applies
    #    universal-newline translation and can shift the count.
    tool_bytes = TOOL_OUT.read_bytes()
    if len(tool_bytes) > build.MAX_TOOL_FILE_BYTES:
        failures.append(
            f"tool file is {len(tool_bytes)} bytes, exceeds tool file budget of "
            f"{build.MAX_TOOL_FILE_BYTES} bytes"
        )
    else:
        print(
            f"OK: tool file is {len(tool_bytes)} bytes "
            f"(budget: {build.MAX_TOOL_FILE_BYTES})"
        )

    # 6b. The budget gate must actually refuse to write. Case 6 only proves the
    #     committed file is under budget — a refactor that moved main()'s
    #     TOOL_OUT.write_text() above the gate would pass it with zero signal.
    #     Drive the build script in a fresh subprocess with the constant
    #     monkeypatched low (never hand-edit the committed constant) against a
    #     TEMP COPY of the tool file, with DIGEST_OUT redirected to a temp path
    #     too, so the probe can never write either committed artifact. Assert
    #     the non-zero exit, the diagnostic, AND that nothing was written.
    gate_ok = True
    with tempfile.TemporaryDirectory(prefix="owui-tool-budget-gate-") as gate_tmp:
        tool_tmp = Path(gate_tmp) / "diagram_design_tool.py"
        digest_tmp = Path(gate_tmp) / "owui-context-digest.json"
        tool_tmp.write_bytes(tool_bytes)
        probe = (
            "import importlib.util, pathlib, sys\n"
            "sys.dont_write_bytecode = True\n"
            "spec = importlib.util.spec_from_file_location('b', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "module.MAX_TOOL_FILE_BYTES = 1\n"
            "module.TOOL_OUT = pathlib.Path(sys.argv[2])\n"
            "module.DIGEST_OUT = pathlib.Path(sys.argv[3])\n"
            "sys.exit(module.main())\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe, str(BUILD_SCRIPT), str(tool_tmp), str(digest_tmp)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            gate_ok = False
            failures.append("tool-file budget gate exited 0 on an over-budget tool file")
        if tool_tmp.read_bytes() != tool_bytes:
            gate_ok = False
            failures.append("tool-file budget gate rewrote the tool file despite exceeding the budget")
        if digest_tmp.exists():
            gate_ok = False
            failures.append("tool-file budget gate wrote the digest despite exceeding the budget")
        if "exceeds tool file budget of" not in proc.stderr:
            gate_ok = False
            failures.append(
                "tool-file budget gate diagnostic missing from stderr — got "
                f"{proc.stderr.strip()[-400:]!r}"
            )
    if gate_ok:
        print("OK: tool-file budget gate refuses to write an over-budget file")

    # 7. Zero-third-party proof (PKG-01, Pitfall 1), inside a `python3 -I -S`
    #    subprocess. This dev environment has pydantic installed while CI does
    #    not — which is precisely why an isolated interpreter is required for
    #    the claim to mean anything: import success is a property of the
    #    environment, not of the file. The probe exec-loads the tool from a bare
    #    namespace, diffs `sys.modules` across the load, and sanity-serves one
    #    brief so the load is proven usable, not merely survivable.
    zerodep_ok = True
    probe = (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "src = open(sys.argv[1], encoding='utf-8').read()\n"
        "ns = {'__name__': 'tool_diagram_design', '__file__': sys.argv[1]}\n"
        "exec(compile(src, sys.argv[1], 'exec'), ns)\n"
        "new = sorted(set(sys.modules) - before)\n"
        "brief = ns['Tools']()._brief_impl('sankey')\n"
        "print(json.dumps({'new': new, 'brief_len': len(brief)}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", probe, str(TOOL_OUT)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        zerodep_ok = False
        failures.append(
            "tool file does not exec-load under `python3 -I -S` — "
            f"{proc.stderr.strip()[-400:]!r}"
        )
    else:
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            zerodep_ok = False
            failures.append(
                f"zero-dependency probe printed no JSON report — got {proc.stdout[-200:]!r}"
            )
        else:
            # sys.stdlib_module_names lists top-level names only, so a stdlib
            # *submodule* import (html.parser, html.entities) must be judged by
            # its top-level package — comparing the dotted name directly would
            # red every stdlib `from html.parser import …` load.
            third_party = [
                m for m in report["new"] if m.split(".")[0] not in sys.stdlib_module_names
            ]
            if third_party:
                zerodep_ok = False
                failures.append(
                    "tool load imports non-stdlib module(s) under -I -S: "
                    + ", ".join(third_party)
                )
            if report["brief_len"] <= 0:
                zerodep_ok = False
                failures.append("zero-dependency probe's _brief_impl('sankey') returned an empty string")
    if zerodep_ok:
        print(
            "OK: tool exec-loads under python3 -I -S with only stdlib imports "
            f"({len(report['new'])} new module(s)) and serves a brief"
        )

    # 8. Static source contract: exactly four public async methods, no
    #    `except BaseException`/bare `except:` (asyncio.CancelledError must keep
    #    propagating so the host can cancel an in-flight call), and every
    #    class-body-indented `def` is `_`-prefixed or one of the four public
    #    names — OpenWebUI would expose any other public callable as its own
    #    model-callable tool (Pitfall 3).
    static_ok = True
    # The class-body checks are scoped to the Tools class itself: Phase 3's
    # module-level validator code owns 4-space-indented defs of its own (an
    # HTMLParser subclass's handle_* methods, nested `add` accumulators) that
    # are not class members and are invisible to OpenWebUI's schema builder.
    class_source = tool_source[tool_source.index("\nclass Tools:"):]
    async_defs = re.findall(r"^    async def (\w+)\(", class_source, re.MULTILINE)
    # Phase 4 added `_`-prefixed async helpers (`_export_impl`, `_render_png`,
    # `_emit_files`) beside the four public seams. The sync half of this case
    # has always filtered underscore names; the async list now does the same,
    # because the invariant under test is the *public* surface OpenWebUI would
    # expose, not the number of coroutines in the class body.
    public_async_defs = [n for n in async_defs if not n.startswith("_")]
    if sorted(public_async_defs) != sorted(PUBLIC_METHODS):
        static_ok = False
        failures.append(
            "public async method set is "
            + ", ".join(sorted(public_async_defs))
            + f" — expected exactly {', '.join(sorted(PUBLIC_METHODS))}"
        )
    if "except BaseException" in tool_source:
        static_ok = False
        failures.append("tool source catches BaseException — cancellation could no longer propagate")
    if re.search(r"except\s*:", tool_source):
        static_ok = False
        failures.append("tool source contains a bare `except:` — cancellation could no longer propagate")
    class_defs = re.findall(r"^    def (\w+)\(", class_source, re.MULTILINE)
    exposed = [
        name
        for name in class_defs
        if not name.startswith("_") and name not in PUBLIC_METHODS
    ]
    if exposed:
        static_ok = False
        failures.append(
            "class-body def(s) would be exposed as model-callable tools: "
            + ", ".join(sorted(exposed))
        )
    if static_ok:
        print(
            "OK: static contract holds — 4 public async methods, no BaseException/bare except, "
            "no exposed public helper"
        )

    # 9. Valve shape, branch-agnostic (Pitfall 6): never assert
    #    `isinstance(..., BaseModel)` — that is false under the stdlib stand-in
    #    a bare CI or `python3 -I -S` environment takes, so a pydantic-only
    #    assertion would only pass in one of the two environments. Assert the
    #    shape facts both branches must satisfy instead.
    def zero_declared_fields(model: type) -> bool:
        """Zero fields under real pydantic v2 (`model_fields`) or the stand-in."""
        fields = getattr(model, "model_fields", None)
        if fields is not None:
            return len(fields) == 0
        return not vars(model())

    valve_ok = True
    try:
        Tools.Valves()
        Tools.UserValves()
    except TypeError as exc:
        valve_ok = False
        failures.append(f"Valves/UserValves do not construct with zero arguments: {exc}")
    valve_tool = Tools()
    if not isinstance(valve_tool.valves, Tools.Valves):
        valve_ok = False
        failures.append("Tools().valves is not an instance of Tools.Valves")
    # D-05: Valves declares exactly two export fields. Read off the INSTANCE —
    # a declared field read off the class (`Tools.Valves.export_dir`) raises
    # AttributeError under real pydantic v2, so class reads are never used here.
    for valve_name, expected_default in (
        ("export_dir", "exports"),
        ("max_export_bytes", 2_000_000),
        # D-03/BRND-01: the brand-profile store's directory valve, defaulted to
        # a repo-relative name that resolves exactly like export_dir does.
        ("profiles_dir", "brand-profiles"),
    ):
        try:
            actual = getattr(valve_tool.valves, valve_name)
        except AttributeError as exc:
            valve_ok = False
            failures.append(f"Tools().valves.{valve_name} is unreadable — D-05 field missing: {exc}")
            continue
        if actual != expected_default:
            valve_ok = False
            failures.append(
                f"Tools().valves.{valve_name} is {actual!r} — D-05 default is {expected_default!r}"
            )
    # Stricter declaration check where real pydantic is present: the field-name
    # set must be exactly D-05's two, with matching FieldInfo defaults. The
    # stdlib stand-in (CI, `python3 -I -S`) has no `model_fields`, so the guard
    # skips it and the branch-agnostic instance reads above carry the case.
    declared = getattr(Tools.Valves, "model_fields", None)
    if declared is not None:
        expected_fields = {
            "export_dir": "exports",
            "max_export_bytes": 2_000_000,
            "profiles_dir": "brand-profiles",
        }
        if set(declared) != set(expected_fields):
            valve_ok = False
            failures.append(
                "Tools.Valves declares " + ", ".join(sorted(declared))
                + " — D-05 declares exactly " + ", ".join(sorted(expected_fields))
            )
        for field_name, expected_value in expected_fields.items():
            info_default = getattr(declared.get(field_name), "default", None)
            if info_default != expected_value:
                valve_ok = False
                failures.append(
                    f"Tools.Valves.{field_name} FieldInfo.default is {info_default!r} "
                    f"— D-05 default is {expected_value!r}"
                )
    if not zero_declared_fields(Tools.UserValves):
        valve_ok = False
        failures.append("Tools.UserValves declares field(s) — D-05 keeps user-level valves empty")
    if valve_tool.citation is not True:
        valve_ok = False
        failures.append("Tools().citation is not True (D-14)")
    if getattr(valve_tool, "file_handler", None) is not True:
        valve_ok = False
        failures.append(
            "Tools().file_handler is not True — export writes files as of Phase 4 "
            "(D-14/A7), so the tool must declare itself a file handler"
        )
    if valve_ok:
        print(
            "OK: valve shape holds — export_dir/max_export_bytes/profiles_dir defaulted "
            "(D-05/D-03), UserValves empty, valves instance, citation True, file_handler True"
        )

    # 10. Public surface (Pitfall 3): dir(Tools) minus underscore names, minus
    #     classes, minus non-callables must equal exactly the four methods.
    public_surface = sorted(
        name
        for name in dir(Tools)
        if not name.startswith("_")
        and not isinstance(getattr(Tools, name, None), type)
        and callable(getattr(Tools, name, None))
    )
    if public_surface != sorted(PUBLIC_METHODS):
        failures.append(
            "public callable surface is [" + ", ".join(public_surface)
            + f"] — expected exactly [{', '.join(sorted(PUBLIC_METHODS))}]; OpenWebUI "
            "exposes every public callable as its own model-callable tool"
        )
    else:
        print("OK: public surface is exactly the four model-callable methods")

    # 11. Async + docstring contract (TEST-01): OpenWebUI awaits tool calls and
    #     turns each method's docstring into the model-facing description, so a
    #     sync or undocumented method is a silent functional regression.
    #     `__event_emitter__` must default to None: the host strips `__`-prefixed
    #     params from the model-facing schema and omits them entirely when
    #     absent, so the reserved param is ABSENT at dispatch, not None.
    contract_ok = True
    for name in PUBLIC_METHODS:
        method = getattr(Tools, name)
        if not inspect.iscoroutinefunction(method):
            contract_ok = False
            failures.append(f"{name} is not a coroutine function — OpenWebUI awaits tool calls")
        if not inspect.getdoc(method):
            contract_ok = False
            failures.append(f"{name} has no docstring — the docstring is the model-facing description")
        params = inspect.signature(method).parameters
        if "__event_emitter__" not in params:
            contract_ok = False
            failures.append(f"{name} does not declare the __event_emitter__ parameter")
        elif params["__event_emitter__"].default is not None:
            contract_ok = False
            failures.append(f"{name}'s __event_emitter__ default is not None")
    brief_doc = inspect.getdoc(Tools.get_design_brief) or ""
    if ":param diagram_type:" not in brief_doc:
        contract_ok = False
        failures.append("get_design_brief's docstring lacks ':param diagram_type:' (D-04)")
    html_doc = inspect.getdoc(Tools.validate_diagram) or ""
    if ":param html:" not in html_doc:
        contract_ok = False
        failures.append("validate_diagram's docstring lacks ':param html:' (D-04)")
    if contract_ok:
        print("OK: all four methods are async, documented, __event_emitter__-defaulted and :param-annotated")

    # 12. RecordingEmitter self-test: the mocked emitter harness must be a
    #     satisfied Phase 2 claim, not dead code awaiting Phase 4.
    emitter_probe = RecordingEmitter()
    asyncio.run(
        emitter_probe({"type": "status", "data": {"description": "harness probe", "done": True}})
    )
    if len(emitter_probe.events) != 1:
        failures.append(
            "RecordingEmitter did not record a directly awaited event — the mocked "
            "emitter harness itself is broken"
        )
    else:
        print("OK: RecordingEmitter records an awaited event (mocked emitter harness works)")

    # 13. Single-brief behaviour: one type, both call shapes. Expectations are
    #     stated from the digest, never by re-calling the tool's own renderer
    #     (the sibling's non-self-referential shape-floor discipline).
    sankey_entry = Tools._DESIGN_DIGEST["types"]["sankey"]
    token_roles = sorted(Tools._DESIGN_DIGEST["style_guide"]["tokens"])

    async def brief_scenario() -> tuple[RecordingEmitter, object, object]:
        emitter = RecordingEmitter()
        tool = Tools()
        with_emitter = await tool.get_design_brief("sankey", __event_emitter__=emitter)
        omitted = await tool.get_design_brief("sankey")
        return emitter, with_emitter, omitted

    emitter, brief, omitted = asyncio.run(brief_scenario())
    brief_ok = True
    if not isinstance(brief, str) or not isinstance(omitted, str):
        brief_ok = False
        failures.append("get_design_brief returned a non-str for at least one call shape (D-01)")
    else:
        if brief != omitted:
            brief_ok = False
            failures.append("get_design_brief differs with __event_emitter__ passed vs omitted")
        if emitter.events:
            brief_ok = False
            failures.append(
                f"get_design_brief emitted {len(emitter.events)} event(s) — a pure dict "
                "lookup must not spam status events"
            )
        if not brief.startswith(f"# {sankey_entry['name']}"):
            brief_ok = False
            failures.append("sankey brief's first line is not '# ' + the digest name")
        if sankey_entry["layout_conventions"][0] not in brief:
            brief_ok = False
            failures.append("sankey brief omits the entry's first layout convention verbatim")
        if "### Design tokens" not in brief or not any(role in brief for role in token_roles):
            brief_ok = False
            failures.append("sankey brief does not carry the appended style-guide tokens (D-02)")
        brief_size = len(brief.encode("utf-8"))
        if brief_size > MAX_BRIEF_BYTES:
            brief_ok = False
            failures.append(
                f"sankey brief is {brief_size} bytes, exceeds the {MAX_BRIEF_BYTES}-byte "
                "context ceiling"
            )
    if brief_ok:
        print(
            "OK: sankey brief is type-specific, style-guide-appended, "
            f"{len(brief.encode('utf-8'))} bytes, and emits zero events"
        )

    # 14. The 40-type sweep (CTX-01): every key, non-empty, type-specific,
    #     style-guide-appended, size-capped. One accumulated boolean + one OK
    #     line, offenders named on failure.
    offenders: list[str] = []
    for slug, entry in Tools._DESIGN_DIGEST["types"].items():
        text = asyncio.run(Tools().get_design_brief(slug))
        problems: list[str] = []
        if not text:
            problems.append("empty return")
        else:
            if not text.startswith(f"# {entry['name']}"):
                problems.append("wrong first line")
            if entry["layout_conventions"] and entry["layout_conventions"][0] not in text:
                problems.append("first layout bullet missing")
            if "accent" not in text:
                problems.append("no 'accent' token role (style guide not appended)")
            size = len(text.encode("utf-8"))
            if size > MAX_BRIEF_BYTES:
                problems.append(f"{size} bytes, over the {MAX_BRIEF_BYTES}-byte ceiling")
            # Never a bare case-insensitive `internal` token match here: the
            # digest's dependency layout bullet legitimately contains a
            # capitalized "Internal", and a future lowercase corpus edit would
            # red healthy briefs. Assert the two real failure signals instead.
            if "Error:" in text:
                problems.append("contains 'Error:'")
            if "internal failure" in text:
                problems.append("contains 'internal failure'")
        if problems:
            offenders.append(f"{slug} ({'; '.join(problems)})")
    if offenders:
        failures.append(
            f"{len(offenders)}/{EXPECTED_TYPE_COUNT} type briefs failed the sweep: "
            + "; ".join(offenders)
        )
    else:
        print(
            f"OK: all {EXPECTED_TYPE_COUNT} types return non-empty, type-specific, "
            f"style-guide-appended briefs under {MAX_BRIEF_BYTES} bytes"
        )

    # 15. Unknown key (D-03): targeted envelope, !r-rendered input, EVERY valid
    #     key present, and no internal-failure clause.
    valid_keys = sorted(Tools._DESIGN_DIGEST["types"])
    unknown = asyncio.run(Tools().get_design_brief("nope"))
    unknown_ok = True
    if not unknown.startswith("Error: unknown diagram_type "):
        unknown_ok = False
        failures.append(f"unknown key returned {unknown[:80]!r} — expected the D-03 envelope")
    else:
        if "'nope'" not in unknown:
            unknown_ok = False
            failures.append("unknown-key envelope does not render the input with !r")
        missing_keys = [key for key in valid_keys if key not in unknown]
        if missing_keys:
            unknown_ok = False
            failures.append("unknown-key envelope omits valid key(s): " + ", ".join(missing_keys))
        if "internal failure" in unknown:
            unknown_ok = False
            failures.append("unknown-key envelope claims an internal failure — this is a targeted path")
    if unknown_ok:
        print(f"OK: unknown key returns the D-03 envelope listing all {len(valid_keys)} valid keys")

    # 16. Bad argument (D-11): the explicit entry check must be distinct from
    #     the outer handler for both a None and an int.
    bad_arg_ok = True
    for bad_value in (None, 7):
        message = asyncio.run(Tools().get_design_brief(bad_value))
        if not message.startswith("Error: diagram_type must be a string"):
            bad_arg_ok = False
            failures.append(
                f"diagram_type={bad_value!r} returned {message[:80]!r} — expected the D-11 envelope"
            )
        elif "internal failure" in message:
            bad_arg_ok = False
            failures.append("bad-argument envelope claims an internal failure — the check is targeted")
    if bad_arg_ok:
        print("OK: non-string diagram_type (None, int) returns the D-11 envelope")

    # 17. Stubs (D-10): each unimplemented method names itself in a not-available
    #     envelope instead of crashing or pretending success. validate_diagram is
    #     implemented as of Phase 3, export_diagram as of Phase 4, and
    #     apply_brand_profile as of Phase 5 — no stub remains, so the tuple is
    #     empty and the per-stub assertions below are simply not exercised.
    #     Case 19 still proves the REL-01 wrapper never propagates from the seam.
    stub_specs = ()
    stubs_ok = True
    for name, call in stub_specs:
        message = asyncio.run(call(Tools()))
        if not isinstance(message, str) or not message.startswith("Error: "):
            stubs_ok = False
            failures.append(f"{name} returned {message[:80]!r} — expected an Error: envelope")
        elif name not in message:
            stubs_ok = False
            failures.append(f"{name}'s stub envelope does not name its own method: {message[:80]!r}")
        elif "not available yet" not in message:
            stubs_ok = False
            failures.append(f"{name}'s stub envelope is not a not-available message: {message[:80]!r}")
        elif "internal failure" in message:
            stubs_ok = False
            failures.append(f"{name}'s stub envelope claims an internal failure — it is a named path")
    if stubs_ok:
        print("OK: remaining stubs return their named not-available envelopes (D-10)")

    # 18. No-live-read proof (CTX-02): audit-hook denylist + a __import__ record.
    #     The hook is installed IMMEDIATELY before the audited call and the hits
    #     list is snapshotted into a delta slice immediately after — the hook is
    #     process-global and irremovable, so later file operations in this same
    #     run would otherwise pollute the assertion. The denylist (not "zero
    #     events") keeps asyncio's own benign `socket.__new__`/asyncgen
    #     construction events from false-reding, and the __import__ record is a
    #     second signal for call-time imports.
    imports_seen: list[str] = []
    real_import = builtins.__import__

    def recording_import(name, globals=None, locals=None, fromlist=(), level=0):
        imports_seen.append(name)
        return real_import(name, globals, locals, fromlist, level)

    sys.addaudithook(_denylist_hook)
    window_start = len(AUDIT_HITS)
    selfpipe_start = len(SELFPIPE_HITS)
    _reset_selfpipe_licenses()
    builtins.__import__ = recording_import
    try:
        audited_brief = asyncio.run(Tools().get_design_brief("sankey"))
    finally:
        builtins.__import__ = real_import
    audited_window = AUDIT_HITS[window_start:]
    selfpipe_window = SELFPIPE_HITS[selfpipe_start:]
    noio_ok = True
    if not audited_brief:
        noio_ok = False
        failures.append("audited get_design_brief returned nothing — no-I/O is unprovable on an empty call")
    if audited_window:
        noio_ok = False
        failures.append(
            f"get_design_brief produced {len(audited_window)} audited I/O event(s) at call "
            f"time: {audited_window[:5]}"
        )
    flagged_imports = _flagged_call_time_imports(imports_seen, bool(selfpipe_window))
    if flagged_imports:
        noio_ok = False
        failures.append(
            "get_design_brief imported module(s) at call time: "
            + ", ".join(sorted(set(flagged_imports)))
        )
    if noio_ok:
        print("OK: get_design_brief performs zero audited I/O events and zero call-time imports (CTX-02)")

    # 18b. Self-pipe filter self-test (Windows CI run 35117831059): the filter
    #     must swallow exactly the host runtime's wakeup pair and nothing
    #     else. Driven through the real hook with the exact event shapes from
    #     that CI log — bind to ('127.0.0.1', 0), connect to ('127.0.0.1',
    #     58154) — plus the negatives that must STAY flagged: an unlicensed
    #     loopback connect (what a real loopback service connection looks
    #     like), a non-loopback port-0 bind, and a non-loopback connect. The
    #     socket object in args is a stand-in; the hook never inspects it.
    #     The recorded negatives append to AUDIT_HITS before any later
    #     window's start index, exactly like the benign pre-window traffic
    #     the delta-slice design already tolerates.
    class _LogSocket:
        def __repr__(self) -> str:
            return "<socket.socket fd=352, family=2, type=1, proto=0>"

    selfpipe_test_ok = True
    _reset_selfpipe_licenses()
    hits_before = len(AUDIT_HITS)
    selfpipe_before = len(SELFPIPE_HITS)
    _denylist_hook("socket.bind", (_LogSocket(), ("127.0.0.1", 0)))
    _denylist_hook("socket.connect", (_LogSocket(), ("127.0.0.1", 58154)))
    if len(AUDIT_HITS) != hits_before or len(SELFPIPE_HITS) != selfpipe_before + 2:
        selfpipe_test_ok = False
        failures.append(
            "the selfpipe filter did not swallow the wakeup pair from CI run 35117831059 "
            f"(AUDIT_HITS +{len(AUDIT_HITS) - hits_before}, SELFPIPE_HITS "
            f"+{len(SELFPIPE_HITS) - selfpipe_before})"
        )
    _denylist_hook("socket.connect", (_LogSocket(), ("127.0.0.1", 5432)))
    _denylist_hook("socket.bind", (_LogSocket(), ("0.0.0.0", 0)))
    _denylist_hook("socket.connect", (_LogSocket(), ("192.0.2.10", 443)))
    recorded = AUDIT_HITS[hits_before:]
    if len(recorded) != 3:
        selfpipe_test_ok = False
        failures.append(
            "the selfpipe filter swallowed real I/O: the unlicensed loopback connect, the "
            f"non-loopback bind and the non-loopback connect must all stay recorded, got "
            f"{recorded}"
        )
    if _flagged_call_time_imports(["_socket", "json"], True) != ["json"]:
        selfpipe_test_ok = False
        failures.append(
            "_socket was not dropped from a call-time import list in a window that ran "
            "the wakeup machinery"
        )
    if _flagged_call_time_imports(["_socket"], False) != ["_socket"]:
        selfpipe_test_ok = False
        failures.append(
            "_socket was dropped from a window with NO wakeup events — that import must "
            "stay flagged"
        )
    _reset_selfpipe_licenses()
    if selfpipe_test_ok:
        print(
            "OK: the selfpipe filter swallows exactly the wakeup pair — unlicensed "
            "loopback and non-loopback socket I/O stay flagged (35117831059)"
        )

    # 19. REL-01 raise seam on all four methods: an injected raiser must surface
    #     as a structured envelope and never propagate out of the call.
    def raiser(self, *args, **kwargs):
        raise RuntimeError("boom")

    seam_specs = (
        ("get_design_brief", "_brief_impl", ("sankey",)),
        ("validate_diagram", "_validate_impl", ("<svg/>",)),
        ("export_diagram", "_export_impl", ("<svg/>",)),
        ("apply_brand_profile", "_brand_impl", ()),
    )
    raise_ok = True
    for public_name, seam_name, call_args in seam_specs:
        tool = Tools()
        setattr(tool, seam_name, types.MethodType(raiser, tool))
        try:
            result = asyncio.run(getattr(tool, public_name)(*call_args))
        except Exception as exc:  # noqa: BLE001 — the propagation REL-01 forbids
            raise_ok = False
            failures.append(
                f"{public_name} propagated {type(exc).__name__} from {seam_name} — REL-01 broken"
            )
            continue
        if not isinstance(result, str):
            raise_ok = False
            failures.append(
                f"{public_name} returned {type(result).__name__} from a raised seam — expected a str envelope"
            )
        elif not result.startswith("Error: internal failure — "):
            raise_ok = False
            failures.append(
                f"{public_name}'s raise envelope is {result[:80]!r} — expected 'Error: internal failure — '"
            )
        elif "RuntimeError" not in result or "boom" not in result:
            raise_ok = False
            failures.append(
                f"{public_name}'s raise envelope omits the exception type/message: {result[:120]!r}"
            )
    if raise_ok:
        print("OK: an injected raiser on all four _impl seams returns 'Error: internal failure — …' and never propagates")

    # 20. VAL-01: the validator's whole model-facing surface. Four degenerate
    #     inputs each get their own targeted envelope (never the REL-01
    #     catch-all, never a vacuous PASS), markup without a diagram and
    #     truncated/comment-only references are bounced rather than passed, a
    #     bare <svg> fragment is validated like a document, a real report is
    #     verdict-first (D-02) with family headings (D-03), severity-marked
    #     bullets (D-03/D-08) and a retry line on FAIL only (D-04), a
    #     warnings-only diagram stays a PASS (D-08), findings cap at 8 per
    #     family behind a "+4 more in this family" trailer (D-13), and the
    #     report is byte-identical with and without an injected emitter
    #     (validation emits nothing — research open question 4, adopted).
    val_ok = True
    validator = _isolated_profiles()

    # (1) Non-string html — the targeted entry check, distinct from the REL-01
    #     catch-all (case 16's discipline, mirrored for the html parameter).
    for bad_value in (None, 7):
        message = asyncio.run(validator.validate_diagram(bad_value))
        if not message.startswith("Error: html must be a string"):
            val_ok = False
            failures.append(
                f"html={bad_value!r} returned {message[:80]!r} — expected the "
                "'Error: html must be a string' envelope"
            )
        elif "internal failure" in message:
            val_ok = False
            failures.append("non-string html envelope claims an internal failure — the check is targeted")

    # (2) Empty and whitespace-only input (D-14: never a vacuous PASS).
    for empty_value in ("", "   \n"):
        message = asyncio.run(validator.validate_diagram(empty_value))
        if not message.startswith("Error: html is empty"):
            val_ok = False
            failures.append(
                f"html={empty_value!r} returned {message[:80]!r} — expected the "
                "'Error: html is empty' envelope"
            )

    # (3) Oversize input (D-12). Built as a literal string, never by reading a
    #     file: 513,000 bytes against the shipped 512,000-byte limit.
    oversize = asyncio.run(
        validator.validate_diagram("x" * (namespace["MAX_HTML_INPUT_BYTES"] + 1_000))
    )
    if not oversize.startswith("Error: html is larger than"):
        val_ok = False
        failures.append(
            f"a MAX_HTML_INPUT_BYTES+1000-byte input returned {oversize[:80]!r} — "
            "expected the D-12 size envelope"
        )

    # (4) Markup that holds no diagram (D-14).
    no_svg = asyncio.run(validator.validate_diagram("<html><body>x</body></html>"))
    if not no_svg.startswith("Error: html contains no SVG diagram"):
        val_ok = False
        failures.append(f"a no-SVG document returned {no_svg[:80]!r} — expected the D-14 envelope")

    # (5) A bare <svg>…</svg> fragment is a diagram, not degenerate input
    #     (D-14). The unwired variant cannot PASS — the accessibility family
    #     requires aria-labelledby — so assert it is *validated* (verdict-first
    #     report, its own a11y finding, not the no-SVG envelope) and that the
    #     same fragment with the aria wiring passes clean.
    fragment = (
        '<svg role="img" viewBox="0 0 40 30" xmlns="http://www.w3.org/2000/svg">'
        "<title>Test</title><desc>Minimal valid fragment</desc>"
        '<rect x="4" y="4" width="32" height="22" fill="#f5f5f5"/></svg>'
    )
    fragment_report = asyncio.run(validator.validate_diagram(fragment))
    if fragment_report.startswith("Error:"):
        val_ok = False
        failures.append(
            f"a bare <svg> fragment was bounced as degenerate input: {fragment_report[:80]!r}"
        )
    elif not re.match(r"^(PASS|FAIL): ", fragment_report):
        val_ok = False
        failures.append(f"a bare <svg> fragment returned a non-report: {fragment_report[:80]!r}")
    elif "aria-labelledby must name the <title> and <desc>" not in fragment_report:
        val_ok = False
        failures.append("an unwired <svg> fragment produced no accessibility finding — was it parsed at all?")
    wired_fragment = (
        '<svg role="img" aria-labelledby="t1 d1" viewBox="0 0 40 30" '
        'xmlns="http://www.w3.org/2000/svg">'
        '<title id="t1">Test</title><desc id="d1">Minimal valid fragment</desc>'
        '<rect x="4" y="4" width="32" height="22" fill="#f5f5f5"/></svg>'
    )
    wired_report = asyncio.run(validator.validate_diagram(wired_fragment))
    if not wired_report.startswith("PASS: 0 issues"):
        val_ok = False
        failures.append(f"an aria-wired <svg> fragment did not pass: {wired_report[:80]!r}")

    # (5b) Malformed fragments never vacuously pass (plan-checker blocker 1):
    #     each substring-matches "<svg" but parses zero svgs, so every one of
    #     them must return the D-14 envelope, never "PASS: 0 issues".
    for broken_fragment in (
        '<svg role="img" viewBox="0 0 10 10"',  # unterminated tag
        "<svg",  # bare opening tag
        "<!-- <svg> -->",  # comment-only reference
    ):
        message = asyncio.run(validator.validate_diagram(broken_fragment))
        if message != "Error: html contains no SVG diagram to validate.":
            val_ok = False
            failures.append(
                f"malformed fragment {broken_fragment!r} returned {message[:60]!r} — "
                "a vacuous PASS (D-14)"
            )

    # (6) Report shape on a deliberately broken diagram (no role, no title, no
    #     viewBox, plus an off-palette fill and a remote image href so every
    #     family is exercised).
    broken_diagram = (
        '<svg><rect x="4" y="4" width="32" height="22" fill="#cc3388"/>'
        '<image href="https://example.com/x.png"/></svg>'
    )
    broken_report = asyncio.run(validator.validate_diagram(broken_diagram))
    report_lines = broken_report.splitlines()
    retry_line = "Fix the issues above and call validate_diagram again with the corrected HTML."
    if not re.match(r"^FAIL: \d+ issue\(s\)$", report_lines[0]):
        val_ok = False
        failures.append(
            f"broken diagram's first line is {report_lines[0]!r} — the verdict must come first (D-02)"
        )
    family_headings = {f"## {heading}" for _family, heading in namespace["_FAMILY_HEADINGS"]}
    headings_present = [line for line in report_lines if line in family_headings]
    if len(headings_present) < 2:
        val_ok = False
        failures.append(
            f"broken diagram's report carries {len(headings_present)} family heading(s) — "
            "expected at least two (D-03)"
        )
    bullets = [line for line in report_lines if line.startswith("- **")]
    if not bullets or any(
        not (line.startswith("- **error**") or line.startswith("- **warning**"))
        for line in bullets
    ):
        val_ok = False
        failures.append("a finding bullet is not '- **error**'/'- **warning**' shaped (D-03/D-08)")
    if not broken_report.rstrip("\n").endswith(retry_line):
        val_ok = False
        failures.append("broken diagram's report does not end with the retry line (D-04)")

    # (7) Severity split (D-08): one out-of-bounds coordinate is a warning, so
    #     the verdict stays a PASS with a warnings section and no retry line;
    #     adding one off-palette hex turns the same diagram into FAIL: 1.
    base_html = (FIXTURE_DIR / "clean-base.html").read_text(encoding="utf-8")
    warnings_html = base_html.replace('cx="220"', 'cx="500"')
    failing_html = warnings_html.replace('fill="#2e5aa8"', 'fill="#cc3388"')
    if warnings_html == base_html or failing_html == warnings_html:
        val_ok = False
        failures.append(
            'clean-base.html lost the cx="220" or fill="#2e5aa8" anchor — the severity-split '
            "inputs could not be built from the committed corpus"
        )
    else:
        warnings_report = asyncio.run(validator.validate_diagram(warnings_html))
        if not warnings_report.startswith("PASS: 0 issues"):
            val_ok = False
            failures.append(
                f"warnings-only diagram returned {warnings_report.splitlines()[0]!r} — "
                "a warning is not an issue (D-08)"
            )
        if "- **warning**" not in warnings_report:
            val_ok = False
            failures.append("warnings-only diagram's PASS report carries no warnings section")
        if retry_line in warnings_report:
            val_ok = False
            failures.append("warnings-only diagram's PASS report carries the FAIL-only retry line (D-04)")
        failing_report = asyncio.run(validator.validate_diagram(failing_html))
        if failing_report.splitlines()[0] != "FAIL: 1 issue(s)":
            val_ok = False
            failures.append(
                f"one off-palette hex returned {failing_report.splitlines()[0]!r} — expected 'FAIL: 1 issue(s)'"
            )

    # (8) Per-family cap (D-13): 12 distinct off-palette hexes must render at
    #     most 8 bullets inside the skin section plus exactly one trailer.
    #     Counted between the "## Skin tokens" heading and the next heading or
    #     blank-line boundary — never by regexing the whole report.
    off_palette = (
        "#cc3388", "#123456", "#abcdef", "#654321", "#f00ba5", "#0ff1ce",
        "#badf00", "#c0ffee", "#deadd0", "#facade", "#0b0b0b", "#5ca1ab1e",
    )
    capped_diagram = (
        '<svg role="img" aria-labelledby="t1 d1" viewBox="0 0 400 300" '
        'xmlns="http://www.w3.org/2000/svg">'
        '<title id="t1">Cap</title><desc id="d1">Twelve off-palette fills</desc>'
        + "".join(
            f'<rect x="{8 * index}" y="8" width="6" height="6" fill="{value}"/>'
            for index, value in enumerate(off_palette)
        )
        + "</svg>"
    )
    capped_report = asyncio.run(validator.validate_diagram(capped_diagram))
    capped_lines = capped_report.splitlines()
    if "## Skin tokens" not in capped_lines:
        val_ok = False
        failures.append("12-off-palette-hex input produced no '## Skin tokens' section to count")
    else:
        section: list[str] = []
        for line in capped_lines[capped_lines.index("## Skin tokens") + 1:]:
            if line.startswith("## ") or not line.strip():
                break
            section.append(line)
        section_bullets = [line for line in section if line.startswith("- **")]
        trailers = [line for line in capped_lines if "more in this family" in line]
        if len(section_bullets) != namespace["_MAX_PER_FAMILY"]:
            val_ok = False
            failures.append(
                f"skin section renders {len(section_bullets)} bullets for 12 findings — expected "
                f"exactly the per-family cap of {namespace['_MAX_PER_FAMILY']} (D-13)"
            )
        if trailers != ["- +4 more in this family"]:
            val_ok = False
            failures.append(
                f"cap trailer is {trailers!r} — expected exactly ['- +4 more in this family'] "
                "for 12 findings"
            )

    # (9) Emitter independence: the report is a pure function of the html, and
    #     validation emits no events at all (research open question 4, adopted).
    emitter = RecordingEmitter()
    for call_html, label in ((base_html, "valid"), (broken_diagram, "failing")):
        with_emitter = asyncio.run(validator.validate_diagram(call_html, __event_emitter__=emitter))
        without_emitter = asyncio.run(validator.validate_diagram(call_html))
        if with_emitter != without_emitter:
            val_ok = False
            failures.append(f"the {label} diagram's report differs with __event_emitter__ injected")
    if emitter.events:
        val_ok = False
        failures.append(
            f"validate_diagram emitted {len(emitter.events)} event(s) — validation must stay silent"
        )
    if val_ok:
        print(
            "OK: validate_diagram envelopes, verdict-first report shape, per-family cap "
            "(+4 more in this family), severity split and emitter independence hold (VAL-01)"
        )

    # 21. D-15 as narrowed in 05-02 (a recorded deviation from D-08's literal
    #     "validate_diagram remains pure"): validate_diagram performs AT MOST
    #     ONE profile-file read per call, gated by an existence pre-check — so
    #     with NO profile active the call is still pure at call time: zero
    #     denylisted audit events (a stat call occurs — os.path.exists — but
    #     stat is not in the denylist) and no call-time import while it does
    #     real work; with a profile active the single sanctioned `open` is that
    #     profile read itself, and it is covered by case 36/38's behaviour rows,
    #     never by a purity claim. Case 18's exact shape, driven on the
    #     validator: the fixture is read into a string BEFORE the window opens
    #     (that read must not be a hit), the assertion is a delta slice over
    #     AUDIT_HITS — never whole-list emptiness, because the hook is
    #     process-global and irremovable and every later file operation in this
    #     run keeps appending to it — and a fresh __import__ recorder is the
    #     second signal for call-time imports. Re-arming the hook here is
    #     harmless: it only ever appends to AUDIT_HITS, so a second registration
    #     could at worst double-count an entry in an already-failing assertion.
    fixture_html = (FIXTURE_DIR / "clean-base.html").read_text(encoding="utf-8")
    validator_imports: list[str] = []
    real_import = builtins.__import__

    def recording_validator_import(name, globals=None, locals=None, fromlist=(), level=0):
        validator_imports.append(name)
        return real_import(name, globals, locals, fromlist, level)

    # Constructed BEFORE the window opens (plan-checker W5): aiming
    # profiles_dir at a fresh temp dir makes the validator's profile read
    # an `open` on a real path, and a TemporaryDirectory emits `open`/`os.remove`
    # events of its own — constructed inside the slice, those would false-red the
    # zero-I/O claim.
    audited_tool = _isolated_profiles()
    # (21b) The narrowed D-15's no-profile window, made explicit: profiles_dir
    #     aims at a directory that does not exist, so a naive `open` on the
    #     profile path would emit a denylisted `open` event — the existence
    #     pre-check must answer the question with a stat instead. Constructed
    #     before the window for the same W5 reason as audited_tool.
    absent_holder = tempfile.TemporaryDirectory(prefix="owui-profiles-absent-")
    profile_tmp_dirs.append(absent_holder)
    absent_tool = Tools()
    absent_tool.valves.profiles_dir = str(Path(absent_holder.name) / "never-created")
    sys.addaudithook(_denylist_hook)
    window_start = len(AUDIT_HITS)
    selfpipe_start = len(SELFPIPE_HITS)
    _reset_selfpipe_licenses()
    builtins.__import__ = recording_validator_import
    try:
        audited_report = asyncio.run(audited_tool.validate_diagram(fixture_html))
        absent_report = asyncio.run(absent_tool.validate_diagram(fixture_html))
    finally:
        builtins.__import__ = real_import
    audited_window = AUDIT_HITS[window_start:]
    selfpipe_window = SELFPIPE_HITS[selfpipe_start:]
    d15_ok = True
    if not audited_report.startswith("PASS: 0 issues"):
        d15_ok = False
        failures.append(
            f"audited validate_diagram returned {audited_report.splitlines()[0]!r} — the "
            "no-I/O claim (D-15) is unprovable unless the call does real work"
        )
    if not absent_report.startswith("PASS: 0 issues"):
        d15_ok = False
        failures.append(
            f"validate_diagram with an absent profiles dir returned {absent_report.splitlines()[0]!r} "
            "— a missing profile must degrade to the default verdict (D-03/D-15)"
        )
    if audited_window:
        d15_ok = False
        failures.append(
            f"validate_diagram produced {len(audited_window)} denylisted audit event(s) at call "
            f"time with no profile active (D-15 as narrowed in 05-02 — a stat call occurs, "
            f"os.path.exists, but stat is not in the denylist): {audited_window[:5]}"
        )
    flagged_validator_imports = _flagged_call_time_imports(
        validator_imports, bool(selfpipe_window)
    )
    if flagged_validator_imports:
        d15_ok = False
        failures.append(
            "validate_diagram imported module(s) at call time (D-15): "
            + ", ".join(sorted(set(flagged_validator_imports)))
        )
    if d15_ok:
        print(
            "OK: validate_diagram performs zero denylisted audit events when no profile is "
            "active (a stat call occurs — os.path.exists — but stat is not in the denylist), "
            "zero call-time imports, and an absent profiles dir degrades to the default verdict "
            "(D-15 as narrowed in 05-02)"
        )

    # 22. VAL-02 / TEST-02: the phase's thresholds re-proven on every suite run
    #     with the measured numbers printed, never assumed (D-11/T-03-13).
    #     Every file read here is repo-side — the suite's own I/O — and the
    #     tool only ever receives strings, so case 21's no-I/O proof is
    #     untouched. lint-skin is deliberately NOT loaded in this case: that
    #     load (and its import side effects) belongs to case 23 alone.
    corpus_ok = True
    baseline_file = ROOT / "scripts" / "lint-skin-baseline.txt"
    # lint-skin's own loader shape (load_baseline()): blank lines and #-comment
    # lines are skipped, so an annotated baseline still parses to its entries.
    baseline = {
        line.strip()
        for line in baseline_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if len(baseline) != 20:
        corpus_ok = False
        failures.append(
            f"{baseline_file.relative_to(ROOT)} parses to {len(baseline)} entries, expected 20 — "
            "an upstream baseline edit is a conscious update to this expectation, not a silent one"
        )
    missing_baseline = sorted(name for name in baseline if not (ROOT / "skills/diagram-design/assets" / name).exists())
    if missing_baseline:
        corpus_ok = False
        failures.append(
            "baseline name(s) with no asset file behind them — a thinned corpus would silently "
            f"weaken the claims below: {missing_baseline[:5]}"
        )
    asset_dir = ROOT / "skills" / "diagram-design" / "assets"
    clean_assets = sorted(path for path in asset_dir.glob("example-*.html") if path.name not in baseline)
    # 2026-09-10 upstream sync (merge of upstream/main): +13 example assets
    # (beeswarm ×3, bump ×3, waterfall ×3, tree-block-decomposition ×3,
    # import-excalidraw ×1) take the clean set 122 → 135. Baseline unchanged.
    if len(clean_assets) != 135:
        corpus_ok = False
        failures.append(
            f"clean set is {len(clean_assets)} assets, expected 135 — computed as the "
            f"{asset_dir.relative_to(ROOT)}/example-*.html glob minus the names in "
            f"{baseline_file.relative_to(ROOT)}; an upstream asset addition or baseline edit "
            "must consciously update this expectation (D-09), never a silent threshold change"
        )

    # Zero false positives: D-11's contract is error-level, so a clean asset
    # must produce no "- **error**" bullet at all. Offenders are named with
    # their first finding instead of failing the whole sweep on the first one.
    false_positives: list[str] = []
    for asset in clean_assets:
        report = asyncio.run(_isolated_profiles().validate_diagram(asset.read_text(encoding="utf-8")))
        if "- **error**" in report:
            first_bullet = next((line for line in report.splitlines() if line.startswith("- **")), "")
            false_positives.append(f"{asset.name}: {first_bullet}")
    if false_positives:
        corpus_ok = False
        failures.append(
            f"{len(false_positives)}/{len(clean_assets)} clean assets produced an error-level "
            f"finding (D-11's zero-false-positive contract): {false_positives[:5]}"
        )

    # Honesty check (D-09, rescoped per plan-checker blocker 2): every baseline
    # asset lint-skin ITSELF still flags must yield at least one error finding.
    # The set is derived, never hardcoded: lint-skin is run over exactly the
    # baseline names, and a file it prints a `path:line:` finding for is
    # flagged — i.e. baseline ∩ (files with `lint-skin --all` findings). Nine
    # baseline assets (the data-flow / dp-integration / it-state clusters)
    # carry zero lint-skin findings and pass cleanly, so asserting all 20 here
    # would assert a measured-false 20-of-20 — exactly the tampering T-03-13
    # forbids. A flagged asset that now passes cleanly means upstream fixed it
    # and the expectation should be updated deliberately.
    baseline_paths = sorted(asset_dir / name for name in baseline)
    lint_run = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lint-skin.py"), *[str(path) for path in baseline_paths]],
        capture_output=True,
        text=True,
    )
    flagged_names = {
        line.split(":", 1)[0].rsplit("/", 1)[-1]
        for line in lint_run.stdout.splitlines()
        if line.startswith("skills/")
    }
    if not flagged_names:
        corpus_ok = False
        failures.append(
            "lint-skin flagged none of the baseline assets — the ≥1-error assertion below would be "
            "vacuous, so this run refuses to claim it (D-09)"
        )
    stale_baseline: list[str] = []
    for name in sorted(flagged_names):
        report = asyncio.run(
            _isolated_profiles().validate_diagram((asset_dir / name).read_text(encoding="utf-8"))
        )
        if "- **error**" not in report:
            stale_baseline.append(name)
    if stale_baseline:
        corpus_ok = False
        failures.append(
            f"{len(stale_baseline)} lint-skin-flagged baseline asset(s) now pass the validator "
            "cleanly — upstream likely fixed them; update this expectation deliberately: "
            f"{sorted(stale_baseline)[:5]}"
        )

    # Defect fixtures, driven from the generator-emitted manifest (T-03-15):
    # expectations come from the committed manifest, and the generator already
    # re-verified every fixture against the real validator at build time, so
    # loosening either side is visible. Reports are parsed from their markdown
    # only — headings and bullets, the exact surface the model receives.
    #
    # CR-03 guard (code review, Phase 3): the committed manifest is NOT trusted
    # as ground truth on its own — nothing else ran the generator, so a thinned
    # manifest (>= 25 floor) or emptied `expect` strings would leave this case
    # green while silently weakening VAL-02. Cross-check the committed corpus
    # against the generator's own build_corpus() output, byte for byte.
    generator_spec = importlib.util.spec_from_file_location(
        "build_validation_fixtures", ROOT / "scripts" / "build-validation-fixtures.py"
    )
    generator = importlib.util.module_from_spec(generator_spec)
    generator_spec.loader.exec_module(generator)
    for fname, payload in generator.build_corpus():
        committed = (FIXTURE_DIR / fname).read_text(encoding="utf-8")
        if committed != payload:
            corpus_ok = False
            failures.append(
                f"committed fixture {fname} does not match the generator's output — "
                "run python3 scripts/build-validation-fixtures.py and commit the result"
            )
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    defects = manifest.get("defects", [])
    if len(defects) < 25:
        corpus_ok = False
        failures.append(
            f"manifest.json carries {len(defects)} defect entries — Plan 02 ships 31, and a thinned "
            "corpus would silently weaken the catch-rate claim"
        )
    base_report = asyncio.run(
        _isolated_profiles().validate_diagram(
            (FIXTURE_DIR / manifest.get("base", "clean-base.html")).read_text(encoding="utf-8")
        )
    )
    if not base_report.startswith("PASS: 0 issues"):
        corpus_ok = False
        failures.append(
            f"clean-base.html returned {base_report.splitlines()[0]!r} — the corpus's clean anchor "
            "must stay a zero-finding diagram (T-03-12)"
        )
    family_headings = {family: f"## {heading}" for family, heading in namespace["_FAMILY_HEADINGS"]}

    def parse_report(report: str) -> dict[str, list[tuple[str, str]]]:
        """{family: [(severity, message), …]} from the report markdown alone."""
        parsed: dict[str, list[tuple[str, str]]] = {}
        family = None
        for line in report.splitlines():
            if line.startswith("## "):
                family = next(
                    (name for name, heading in family_headings.items() if heading == line.strip()),
                    None,
                )
                if family:
                    parsed.setdefault(family, [])
            elif line.startswith("- **") and family:
                end = line.index("**", 4)
                severity = line[4:end]
                rest = line[end + 2:]
                message = rest.split("): ", 1)[1] if "): " in rest else rest
                parsed[family].append((severity.strip(), message))
        return parsed

    caught = 0
    misses: list[str] = []
    for entry in defects:
        name = entry.get("name", "<unnamed>")
        fixture_path = FIXTURE_DIR / f"{name}.html"
        if not fixture_path.exists():
            misses.append(f"{name}: fixture missing from {FIXTURE_DIR.relative_to(ROOT)}")
            continue
        parsed = parse_report(
            asyncio.run(_isolated_profiles().validate_diagram(fixture_path.read_text(encoding="utf-8")))
        )
        family_findings = parsed.get(entry["family"], [])
        expected_hit = any(
            severity == entry["severity"] and entry["expect"] in message
            for severity, message in family_findings
        )
        cross_family_errors = [
            f"{family}: {severity}"
            for family, findings in parsed.items()
            if family != entry["family"]
            for severity, _message in findings
            if severity == "error"
        ]
        if not expected_hit:
            misses.append(
                f"{name}: no {entry['severity']} in the {entry['family']} family containing "
                f"{entry['expect']!r}; got {family_findings!r}"
            )
        elif cross_family_errors:
            # Cross-family warnings are tolerated (measured); cross-family
            # errors would mean the fixture does not isolate its class.
            misses.append(f"{name}: error-level finding(s) in another family {cross_family_errors}")
        else:
            caught += 1
    catch_rate = caught / len(defects) if defects else 0.0
    if catch_rate < 0.9:
        corpus_ok = False
        failures.append(
            f"defect catch rate is {caught}/{len(defects)} = {catch_rate:.0%} — VAL-02 requires "
            f">= 90%; first misses: {misses[:5]}"
        )
    elif misses:
        print(f"   (note: {len(misses)} fixture(s) outside the catch assertion: {misses[:3]})")
    if corpus_ok:
        print(
            f"OK: corpus re-proven — {len(clean_assets)} clean assets with zero error findings, "
            f"{len(flagged_names)} lint-skin-flagged baseline assets each carry an error, "
            f"defect catch rate {caught}/{len(defects)} = {catch_rate:.0%} (VAL-02)"
        )

    # 23. VAL-03 / D-07: the validator is derived from lint-skin.py, not
    #     reinvented. lint-skin is loaded into this process and the port is
    #     pinned at four constant levels plus one corpus level, so a rule change
    #     upstream — or a silent drift inside the tool — fails here instead of
    #     in production. Loading is the load-bearing detail: lint-skin's
    #     @dataclass declarations resolve __module__ through sys.modules, so the
    #     module must be registered there BEFORE exec_module, or the load raises
    #     AttributeError. sys.dont_write_bytecode (module level, above) keeps
    #     that load from writing __pycache__ into the tree.
    spec = importlib.util.spec_from_file_location("lint_skin", ROOT / "scripts" / "lint-skin.py")
    lint_skin = importlib.util.module_from_spec(spec)
    sys.modules["lint_skin"] = lint_skin  # REQUIRED before exec_module (see above)
    spec.loader.exec_module(lint_skin)

    parity_ok = True
    # (1) Regex identity. All 13 ported constants are shared with lint-skin and
    #     pinned byte-for-byte (pattern string AND flags). IMPORT_ANY_RE and
    #     URL_ANY_RE are pinned here as the shared constants they are; the
    #     documented deviation is about *application*, not the pattern — the
    #     tool runs them over every @import/url() where lint-skin's older
    #     IMPORT_HTTP_RE/URL_HTTP_RE matched only remote ones, so nothing is
    #     asserted about those two narrower constants.
    ported_regexes = (
        "HEX_RE", "RGBA_RE", "BLACK_RGB_RE", "FONT_CSS_RE", "FONT_ATTR_RE",
        "SRC_HTTP_RE", "IMPORT_ANY_RE", "URL_ANY_RE", "LINK_RE", "HREF_RE",
        "REL_RE", "SCRIPT_OPEN_RE", "SCRIPT_BLOCK_RE",
    )
    drifted_regexes = [
        name
        for name in ported_regexes
        if namespace[name].pattern != getattr(lint_skin, name).pattern
        or namespace[name].flags != getattr(lint_skin, name).flags
    ]
    if drifted_regexes:
        parity_ok = False
        failures.append(
            "ported regex constant(s) drifted from lint-skin.py (D-07/VAL-03): "
            + ", ".join(drifted_regexes)
        )

    # (2) Palette identity: the hardcoded palette must equal lint-skin's
    #     style-guide-derived colors, so drift from upstream's style guide fails
    #     here rather than as production false positives (Pitfall 5).
    colors, triplets = lint_skin.allowed_colors()
    if namespace["_PALETTE"] != set(colors):
        parity_ok = False
        failures.append(
            f"_PALETTE ({len(namespace['_PALETTE'])} values) != lint-skin allowed_colors()[0] "
            f"({len(colors)} values) — symmetric difference "
            f"{sorted(namespace['_PALETTE'] ^ set(colors))[:6]}"
        )
    if namespace["_PALETTE_RGB"] != set(triplets):
        parity_ok = False
        failures.append(
            f"_PALETTE_RGB ({len(namespace['_PALETTE_RGB'])} triplets) != lint-skin "
            f"allowed_colors()[1] ({len(triplets)} triplets)"
        )
    if len(colors) != 31 or len(triplets) != 30:
        # Measured upstream shape: an equality pass with different cardinalities
        # would mean BOTH sides changed together and the counts deserve a look.
        parity_ok = False
        failures.append(
            f"lint-skin's palette is now {len(colors)} hex / {len(triplets)} rgb triplets, "
            "expected 31 / 30 — an upstream style-guide change must be ported consciously"
        )

    # (3) Font identity. Documented delta, deliberately not ported: lint-skin's
    #     runtime union adds style_guide_families(), whose only extra member is
    #     "instrument serif *italic" — a markdown-emphasis artifact of the
    #     typography table, not a CSS family name. Assert against ALLOWED_FONTS.
    if namespace["_ALLOWED_FONTS"] != set(lint_skin.ALLOWED_FONTS):
        parity_ok = False
        failures.append(
            f"_ALLOWED_FONTS ({len(namespace['_ALLOWED_FONTS'])}) != lint-skin ALLOWED_FONTS "
            f"({len(lint_skin.ALLOWED_FONTS)}) — symmetric difference "
            f"{sorted(namespace['_ALLOWED_FONTS'] ^ set(lint_skin.ALLOWED_FONTS))[:6]}"
        )
    # 2026-09-10 upstream sync: upstream added the 10 Traditional-Chinese/
    # Simplified-Chinese faces (16 → 26), ported into the tool's _ALLOWED_FONTS
    # above — the parity check enforces the sets stay identical, this pins the
    # cardinality so a future upstream addition is a conscious port too.
    if len(lint_skin.ALLOWED_FONTS) != 26:
        parity_ok = False
        failures.append(
            f"lint-skin's ALLOWED_FONTS now holds {len(lint_skin.ALLOWED_FONTS)} members, "
            "expected 26 — an upstream font-list change must be ported consciously"
        )

    # (4) Controller-digest identity: recomputed from template-motion.html
    #     through lint-skin's own loader, so an upstream motion-controller edit
    #     fails here instead of silently rejecting every animated diagram at
    #     validation time.
    if namespace["_CANONICAL_CONTROLLER_SHA256"] != lint_skin.canonical_controller_digest():
        parity_ok = False
        failures.append(
            f"_CANONICAL_CONTROLLER_SHA256 ({namespace['_CANONICAL_CONTROLLER_SHA256'][:12]}…) != "
            f"lint-skin canonical_controller_digest() "
            f"({lint_skin.canonical_controller_digest()[:12]}…) — template-motion.html changed "
            "upstream; re-embed the digest deliberately"
        )

    # (5) Offset-level skin-family corpus parity. Both sides iterate their OWN
    #     constants and helpers over the same text, and the flagged character
    #     offsets must be identical — message wording is free, positions are not
    #     (T-03-14: a rewording cannot hide a rule change).
    def lint_skin_skin_offsets(text: str) -> set[int]:
        """Offsets lint-skin flags in its skin families (color/pure-black/font)."""
        offsets: set[int] = set()
        for match in lint_skin.HEX_RE.finditer(text):
            normalized = lint_skin.normalize_hex(match.group())
            if normalized == "#000000" or normalized not in colors:
                offsets.add(match.start())
        for match in lint_skin.RGBA_RE.finditer(text):
            rgb = tuple(int(match.group(index)) for index in (1, 2, 3))
            if rgb not in triplets:
                offsets.add(match.start())
        for match in lint_skin.BLACK_RGB_RE.finditer(text):
            offsets.add(match.start())
        allowed = set(lint_skin.ALLOWED_FONTS)
        for link_match in lint_skin.LINK_RE.finditer(text):
            href_match = lint_skin.HREF_RE.search(link_match.group())
            rel_match = lint_skin.REL_RE.search(link_match.group())
            rel_values = rel_match.group(2).casefold().split() if rel_match else []
            if "stylesheet" in rel_values and href_match:
                allowed |= lint_skin.google_fonts_families(href_match.group(2).strip())
        for regex, group_index in ((lint_skin.FONT_CSS_RE, 1), (lint_skin.FONT_ATTR_RE, 2)):
            for match in regex.finditer(text):
                if lint_skin.named_families(match.group(group_index), allowed):
                    offsets.add(match.start())
        return offsets

    def tool_skin_offsets(text: str) -> set[int]:
        """The identical iteration through the tool's own ported constants."""
        offsets: set[int] = set()
        for match in namespace["HEX_RE"].finditer(text):
            normalized = namespace["_normalize_hex"](match.group())
            if normalized == "#000000" or normalized not in namespace["_PALETTE"]:
                offsets.add(match.start())
        for match in namespace["RGBA_RE"].finditer(text):
            rgb = tuple(int(match.group(index)) for index in (1, 2, 3))
            if rgb not in namespace["_PALETTE_RGB"]:
                offsets.add(match.start())
        for match in namespace["BLACK_RGB_RE"].finditer(text):
            offsets.add(match.start())
        _egress, approved = namespace["_check_egress"](text)
        allowed = set(namespace["_ALLOWED_FONTS"]) | set(approved)
        for regex, group_index in ((namespace["FONT_CSS_RE"], 1), (namespace["FONT_ATTR_RE"], 2)):
            for match in regex.finditer(text):
                if namespace["_named_families"](match.group(group_index), allowed):
                    offsets.add(match.start())
        return offsets

    # Deliberately NOT asserted: a11y/geometry offset parity. The tool
    # intentionally drops lint-skin's file-slug id match and the
    # template-placeholder allowlist (no model-side context for either) and
    # replaces the runtime controller-file read with the embedded digest
    # constant (D-15), so exact parity there is impossible by design. Those two
    # families' behaviour is enforced by the corpus cases above instead.
    corpus_assets = sorted(asset_dir.glob("example-*.html"))
    drifted_assets: list[str] = []
    for asset in corpus_assets:
        text = asset.read_text(encoding="utf-8")
        upstream = lint_skin_skin_offsets(text)
        ported = tool_skin_offsets(text)
        if upstream != ported:
            difference = sorted(upstream ^ ported)
            drifted_assets.append(
                f"{asset.name}: {len(upstream)} upstream vs {len(ported)} ported offsets, "
                f"first differing at {difference[:4]}"
            )
    if drifted_assets:
        parity_ok = False
        failures.append(
            f"skin-family offsets differ from lint-skin.py on {len(drifted_assets)}/"
            f"{len(corpus_assets)} asset(s) — the port has drifted (D-07): {drifted_assets[:3]}"
        )
    if parity_ok:
        print(
            "OK: parity with lint-skin.py holds — 13 regex constants, palette "
            f"({len(colors)} hex / {len(triplets)} rgb triplets), fonts "
            f"({len(lint_skin.ALLOWED_FONTS)}), controller digest and skin-family offsets "
            f"across {len(corpus_assets)} assets (VAL-03)"
        )

    # T-2-04: no call in this suite may have mutated the shared class attribute
    # (one Tools instance serves the whole process on a real host).
    if Tools._DESIGN_DIGEST != json.loads(DIGEST.read_text(encoding="utf-8")):
        failures.append(
            "Tools._DESIGN_DIGEST changed across the call suite — a call mutated the "
            "shared class attribute (cross-user leak on a real host)"
        )
    else:
        print("OK: _DESIGN_DIGEST is unchanged after every call (immutable class attribute)")

    # 24. D-07 / EXP-01: the envelope ladder. Every degenerate export input
    #     gets its OWN targeted Error: prefix (never the REL-01 "internal
    #     failure" catch-all — case 20's discipline) and leaves the export
    #     directory empty: the ladder runs before the first mkdir/write
    #     (Pitfall 7), so a rejected call must not even create the directory's
    #     contents. The oversized probe lowers the valve to 100 instead of
    #     allocating a real 2 MB payload (T-4-14), on its own Tools() instance
    #     so no later case inherits the lowered cap.
    diagram_svg = (
        '<svg role="img" aria-labelledby="x-title" viewBox="0 0 400 240" '
        'xmlns="http://www.w3.org/2000/svg"><title id="x-title">One bar</title>'
        '<rect x="24" y="60" width="88" height="140" fill="#2e5aa8" stroke="#0a0a0a"/></svg>'
    )
    ladder_ok = True
    with tempfile.TemporaryDirectory(prefix="owui-ladder-") as ladder_tmp:
        ladder_dir = Path(ladder_tmp)
        ladder_tool = Tools()
        ladder_tool.valves.export_dir = str(ladder_dir)
        ladder_cases = (
            ("html=None", lambda: ladder_tool.export_diagram(None, "html", "x"),
             "Error: html must be a string containing the diagram markup to export."),
            ("html=7", lambda: ladder_tool.export_diagram(7, "html", "x"),
             "Error: html must be a string containing the diagram markup to export."),
            ('html=""', lambda: ladder_tool.export_diagram("", "html", "x"),
             "Error: html is empty — there is no diagram to export."),
            ('html="   \\n"', lambda: ladder_tool.export_diagram("   \n", "html", "x"),
             "Error: html is empty — there is no diagram to export."),
            ('format="pdf"', lambda: ladder_tool.export_diagram(diagram_svg, "pdf", "x"),
             'Error: format must be one of "html", "svg", "png"'),
            ("name=None", lambda: ladder_tool.export_diagram(diagram_svg, "html", None),
             "Error: name must be a string"),
            ("name=7", lambda: ladder_tool.export_diagram(diagram_svg, "html", 7),
             "Error: name must be a string"),
            ("no <svg", lambda: ladder_tool.export_diagram("<p>no diagram here</p>", "html", "x"),
             "Error: html contains no SVG diagram to export."),
            ('unbalanced root, format="svg"', lambda: ladder_tool.export_diagram("<svg><svg></svg>", "svg", "x"),
             "Error: no extractable root"),
        )
        for label, call, expected_prefix in ladder_cases:
            message = asyncio.run(call())
            if not message.startswith(expected_prefix):
                ladder_ok = False
                failures.append(
                    f"{label} returned {message.splitlines()[0]!r} — expected the targeted "
                    f"prefix {expected_prefix!r} (D-07)"
                )
            if "internal failure" in message:
                ladder_ok = False
                failures.append(f"{label} fell through to the REL-01 catch-all instead of its own envelope (D-07)")
            if any(ladder_dir.iterdir()):
                ladder_ok = False
                failures.append(
                    f"{label} left files behind in the export directory — the ladder must run "
                    "before any mkdir/write (Pitfall 7)"
                )
        # Oversized, with the cap valve lowered rather than a 2 MB string built.
        cap_tool = Tools()
        cap_tool.valves.export_dir = str(ladder_dir)
        cap_tool.valves.max_export_bytes = 100
        oversized = diagram_svg + '<rect x="24" y="60" width="88" height="140" fill="#2e5aa8"/>' * 3
        if len(oversized.encode("utf-8")) <= 100:
            ladder_ok = False
            failures.append("case 24's oversized probe is not actually over the lowered 100-byte cap")
        cap_message = asyncio.run(cap_tool.export_diagram(oversized, "html", "x"))
        if not cap_message.startswith("Error: html is larger than"):
            ladder_ok = False
            failures.append(
                f"oversized input returned {cap_message.splitlines()[0]!r} — expected the "
                "'Error: html is larger than' envelope (T-4-06)"
            )
        if "internal failure" in cap_message or any(ladder_dir.iterdir()):
            ladder_ok = False
            failures.append("oversized input wrote a file or hit the catch-all instead of its envelope (D-07)")
    if ladder_ok:
        print(
            f"OK: the envelope ladder rejects all {len(ladder_cases) + 1} degenerate inputs with "
            "targeted Error: prefixes and never writes a file (D-07)"
        )

    # 25. D-04: the slug contract, asserted two ways — the module-level helper
    #     through the exec namespace on all eight measured rows, then the
    #     end-to-end traversal proof (T-4-05): a "../.." name must land as a
    #     flat slug inside the export dir and never outside it.
    slug_ok = True
    slug_rows = (
        ("../../etc/hosts", "etc-hosts"),
        ("..%2F..%2Fetc", "2f-2fetc"),
        ("my diagram: final v3?", "my-diagram-final-v3"),
        ("a.b.c", "a-b-c"),
        ("-lead-", "lead"),
        ("Únicode", "nicode"),
        ("A" * 200, "a" * 80),
    )
    slug_fn = namespace.get("_slug_name")
    if slug_fn is None:
        slug_ok = False
        failures.append("the tool namespace exposes no _slug_name helper to assert the slug contract against")
    else:
        for raw, expected in slug_rows:
            actual = slug_fn(raw)
            if actual != expected:
                slug_ok = False
                failures.append(f"_slug_name({raw!r}) returned {actual!r} — expected {expected!r} (D-04)")
        for raw in ("", "  ", None, 7):
            if slug_fn(raw) != "diagram":
                slug_ok = False
                failures.append(f"_slug_name({raw!r}) returned {slug_fn(raw)!r} — expected the 'diagram' fallback (D-04)")
    with tempfile.TemporaryDirectory(prefix="owui-slug-parent-") as slug_parent:
        with tempfile.TemporaryDirectory(prefix="owui-slug-out-", dir=slug_parent) as slug_tmp:
            slug_tool = Tools()
            slug_tool.valves.export_dir = slug_tmp
            traversal_report = asyncio.run(slug_tool.export_diagram(diagram_svg, "html", "../../etc/hosts"))
            written_slug = Path(slug_tmp) / "etc-hosts.html"
            if not written_slug.is_file():
                slug_ok = False
                failures.append(
                    "the '../../etc/hosts' export did not write <export dir>/etc-hosts.html — "
                    f"report said: {traversal_report.splitlines()[0]!r} (D-04/T-4-05)"
                )
            if sorted(p.name for p in Path(slug_parent).iterdir()) != [Path(slug_tmp).name]:
                slug_ok = False
                failures.append(
                    "the traversal attempt created a sibling of the export dir — "
                    f"parent holds {sorted(p.name for p in Path(slug_parent).iterdir())!r} (T-4-05)"
                )
            if "etc-hosts.html" not in traversal_report:
                slug_ok = False
                failures.append("the traversal export's report does not name the sanitized path (D-07)")
    if slug_ok:
        print(
            f"OK: _slug_name maps all {len(slug_rows) + 1} measured rows (the 'diagram' fallback is "
            f"the {len(slug_rows) + 1}th) and '../../etc/hosts' writes only <export dir>/etc-hosts.html (D-04)"
        )

    # 26. EXP-01 / D-02 / D-07: the html tier round-trips byte-identically,
    #     keeps the fonts link, re-validates PASS, and reports a path + byte
    #     size — never the markup — under the 2,000-character ceiling. Same-name
    #     re-export overwrites in place (D-04's collision semantics).
    html_ok = True
    with tempfile.TemporaryDirectory(prefix="owui-html-") as html_tmp:
        html_tool = _isolated_profiles()
        html_tool.valves.export_dir = html_tmp
        html_input = (FIXTURE_DIR / "clean-base.html").read_text(encoding="utf-8")
        html_report = asyncio.run(html_tool.export_diagram(html_input, "html", "Round Trip Doc"))
        html_path = Path(html_tmp) / "round-trip-doc.html"
        first_line = html_report.splitlines()[0]
        if not html_path.is_file():
            html_ok = False
            failures.append(f"the html tier wrote no round-trip-doc.html — report: {first_line!r}")
        if not first_line.startswith("Exported "):
            html_ok = False
            failures.append(f"the html tier's report starts {first_line!r} — expected 'Exported …' (D-07)")
        else:
            if "round-trip-doc.html" not in first_line:
                html_ok = False
                failures.append(f"the html tier's report does not name the slug file: {first_line!r}")
            if not re.search(r"\([\d,]+ bytes\)", first_line):
                html_ok = False
                failures.append(f"the html tier's report carries no byte count: {first_line!r}")
            if str(html_tmp) not in first_line:
                html_ok = False
                failures.append(f"the html tier's report does not name the resolved directory: {first_line!r}")
        if html_path.is_file():
            if html_path.read_bytes() != html_input.encode("utf-8"):
                html_ok = False
                failures.append("the written .html is not byte-identical to the input (D-02/EXP-01)")
            written_text = html_path.read_text(encoding="utf-8")
            if "fonts.googleapis.com" not in written_text:
                html_ok = False
                failures.append("the written .html lost the fonts.googleapis.com link (D-02)")
            revalidated = asyncio.run(html_tool.validate_diagram(written_text))
            if not revalidated.startswith("PASS: 0 issues"):
                html_ok = False
                failures.append(
                    f"validate_diagram on the written file returned {revalidated.splitlines()[0]!r} — "
                    "expected 'PASS: 0 issues' (EXP-01)"
                )
        if len(html_report) > 2_000:
            html_ok = False
            failures.append(f"the export report is {len(html_report)} characters — over the 2,000-byte ceiling (research)")
        if "<svg" in html_report:
            html_ok = False
            failures.append("the export report carries markup — it must carry a path and sizes only (D-07/T-4-08)")
        mutated = html_input.replace(">p95 ms<", ">p99 ms<")
        if mutated == html_input:
            html_ok = False
            failures.append("case 26's mutation did not change the input — the overwrite assertion would be vacuous")
        overwrite_report = asyncio.run(html_tool.export_diagram(mutated, "html", "Round Trip Doc"))
        if html_path.read_bytes() != mutated.encode("utf-8"):
            html_ok = False
            failures.append("re-exporting the same name did not overwrite the file with the new content (D-04)")
        if sorted(p.name for p in Path(html_tmp).iterdir()) != ["round-trip-doc.html"]:
            html_ok = False
            failures.append(
                "a same-name re-export left more than one file in the export directory: "
                f"{sorted(p.name for p in Path(html_tmp).iterdir())!r} (D-04)"
            )
        if "round-trip-doc.html" not in overwrite_report:
            html_ok = False
            failures.append("the overwrite report does not name the same slug path (D-04)")
    if html_ok:
        print(
            f"OK: the html tier writes the input byte-identically, keeps the fonts link, re-validates "
            f"PASS, and returns a {len(html_report)}-char report with no markup (EXP-01/D-02/D-07)"
        )

    # 27. EXP-02 / D-06: the svg tier writes the VERBATIM root span — the
    #     bytes _extract_root_svg returns, no wrapping, no XML preamble, no
    #     rewrite — so a nested <svg> survives whole and the document outside
    #     the span does not. The inner svg here is a closed <svg>…</svg> pair:
    #     a self-closing <svg/> is rejected by the single-root depth guard
    #     (measured), and so would this case red. Also asserts D-03's "every
    #     tier is always reported" on the two unrequested lines.
    svg_ok = True
    nested_markup = (
        '<!DOCTYPE html>\n<html lang="en"><body>\n'
        '<svg role="img" aria-labelledby="o-title" viewBox="0 0 400 240" '
        'xmlns="http://www.w3.org/2000/svg">\n'
        '  <title id="o-title">Nested inset</title>\n'
        '  <rect x="24" y="60" width="88" height="140" fill="#2e5aa8" stroke="#0a0a0a"/>\n'
        '  <svg x="150" y="40" width="100" height="60" viewBox="0 0 100 60">\n'
        '    <title>inset</title><circle cx="50" cy="30" r="20" fill="#2e5aa8"/>\n'
        '  </svg>\n'
        '</svg>\n'
        '<p>trailing element after the diagram</p>\n'
        '</body></html>\n'
    )
    with tempfile.TemporaryDirectory(prefix="owui-svg-") as svg_tmp:
        svg_tool = Tools()
        svg_tool.valves.export_dir = svg_tmp
        expected_span = namespace.get("_extract_root_svg")(nested_markup)
        if not expected_span:
            svg_ok = False
            failures.append("case 27's own markup yields no root span — the fixture is broken, not the tool")
        svg_report = asyncio.run(svg_tool.export_diagram(nested_markup, "svg", "span check"))
        svg_path = Path(svg_tmp) / "span-check.svg"
        if not svg_path.is_file():
            svg_ok = False
            failures.append(f"the svg tier wrote no span-check.svg — report: {svg_report.splitlines()[0]!r}")
        else:
            written_bytes = svg_path.read_bytes()
            if expected_span is not None and written_bytes != expected_span.encode("utf-8"):
                svg_ok = False
                failures.append("the written .svg is not byte-identical to _extract_root_svg's span (D-06)")
            span_text = written_bytes.decode("utf-8")
            if not (span_text.startswith("<svg") and span_text.endswith("</svg>")):
                svg_ok = False
                failures.append(
                    f"the written span starts {span_text[:16]!r} and ends {span_text[-16:]!r} — "
                    "expected a bare <svg …></svg> span with no preamble or wrapper (D-06)"
                )
            if "<circle" not in span_text or "</svg>\n</svg>" not in span_text.replace("\r\n", "\n"):
                svg_ok = False
                failures.append(
                    "the written .svg lost the nested inner svg — the last-close extraction regressed "
                    "to first-close truncation (D-06)"
                )
            if "trailing element" in span_text:
                svg_ok = False
                failures.append("the written .svg carried document content from outside the root span (D-06)")
        if not svg_report.startswith("Exported span-check.svg"):
            svg_ok = False
            failures.append(f"the svg tier's report starts {svg_report.splitlines()[0]!r} (D-07)")
        if f"- svg: {svg_path} (written, {len((expected_span or '').encode('utf-8')):,} bytes)" not in svg_report:
            svg_ok = False
            failures.append("the svg tier line does not report the written path and byte size (D-03)")
        if "- html: not written" not in svg_report:
            svg_ok = False
            failures.append("the svg export's report omits the html tier's 'not written' line (D-03)")
        if "- png: not requested" not in svg_report:
            svg_ok = False
            failures.append("the svg export's report omits the png tier's 'not requested' line (D-03)")
        # The negative: a document with no extractable root errors and writes nothing.
        with tempfile.TemporaryDirectory(prefix="owui-svg-none-") as svg_none_tmp:
            none_tool = Tools()
            none_tool.valves.export_dir = svg_none_tmp
            none_report = asyncio.run(none_tool.export_diagram("<svg><svg></svg>", "svg", "no-root"))
            if not none_report.startswith("Error: no extractable root"):
                svg_ok = False
                failures.append(
                    "format=\"svg\" on a rootless document returned "
                    f"{none_report.splitlines()[0]!r} — expected the 'Error: no extractable root' "
                    "envelope (D-06)"
                )
            if any(Path(svg_none_tmp).iterdir()):
                svg_ok = False
                failures.append("the rootless svg export wrote a file before erroring (Pitfall 7)")
    if svg_ok:
        print(
            "OK: the svg tier writes the verbatim root span (nested svg whole, no document "
            "outside it), reports all three tiers, and errors on a rootless document with "
            "nothing written (EXP-02/D-06)"
        )

    # 28. EXP-04 / D-07 / D-08: delivery both ways, driven by one async scenario
    #     under a single asyncio.run (case 12's shape). The report is the
    #     guaranteed channel; the files event is best-effort, emitted strictly
    #     after the write, once per written file — and an emitter that raises
    #     must never fail an export whose file is already on disk.
    deliver_ok = True
    with tempfile.TemporaryDirectory(prefix="owui-deliver-") as deliver_tmp:
        deliver_tool = Tools()
        deliver_tool.valves.export_dir = deliver_tmp
        deliver_html = (
            '<svg role="img" aria-labelledby="d-title" viewBox="0 0 200 120" '
            'xmlns="http://www.w3.org/2000/svg"><title id="d-title">Delivery</title>'
            '<rect x="8" y="8" width="40" height="20" fill="#2e5aa8" stroke="#0a0a0a"/></svg>'
        )
        deliver_path = Path(deliver_tmp) / "delivery-doc.html"
        expected_payload = [{
            "type": "files",
            "data": {"files": [{"type": "file", "name": "delivery-doc.html", "url": str(deliver_path)}]},
        }]

        async def delivery_scenario():
            recorder = RecordingEmitter()
            with_emitter = await deliver_tool.export_diagram(
                deliver_html, "html", "Delivery Doc", __event_emitter__=recorder
            )
            without_emitter = await deliver_tool.export_diagram(deliver_html, "html", "Delivery Doc")

            # D-08: a raising emitter double — no analog elsewhere in this suite,
            # because every other case injects a recorder or nothing at all.
            async def boom(payload):
                raise RuntimeError("emit failed")

            raised = await deliver_tool.export_diagram(
                deliver_html, "html", "Delivery Doc", __event_emitter__=boom
            )
            return recorder.events, with_emitter, without_emitter, raised

        events, with_report, without_report, raised_report = asyncio.run(delivery_scenario())
        if events != expected_payload:
            deliver_ok = False
            failures.append(
                f"the files event was {events!r} — expected exactly one event with the documented "
                f"payload naming {deliver_path} (D-08)"
            )
        if not deliver_path.is_file():
            deliver_ok = False
            failures.append(
                "the recorded files event names a file that is not on disk — the event must "
                "follow the write (D-08)"
            )
        for label, report in (
            ("with-emitter", with_report),
            ("emitter-omitted", without_report),
            ("raising-emitter", raised_report),
        ):
            first = report.splitlines()[0]
            if not first.startswith("Exported "):
                deliver_ok = False
                failures.append(f"the {label} export's report starts {first!r} — expected 'Exported …' (D-07)")
            if str(deliver_path) not in report or not re.search(r"\([\d,]+ bytes\)", report):
                deliver_ok = False
                failures.append(f"the {label} export's report does not name the written path and byte size (D-07)")
        if deliver_path.read_bytes() != deliver_html.encode("utf-8"):
            deliver_ok = False
            failures.append("the delivered file is not the full markup — a partial write reached disk (D-08)")
        # The negative: exactly one event total across all three calls, so the
        # omitted and raising calls recorded nothing and nothing fired twice.
        if len(events) != 1:
            deliver_ok = False
            failures.append(f"{len(events)} files events recorded for three export calls — expected exactly one (D-08)")
    if deliver_ok:
        print(
            "OK: delivery works both ways — exactly one files event with the documented payload "
            "after the write, the same report with the emitter omitted, and a raising emitter "
            "still delivered report + file (EXP-04/D-08)"
        )

    # 29. EXP-03 / D-09 / A2: the png tier with playwright ABSENT (this host
    #     and CI — research's "Environment Availability") must return the
    #     report-shaped notice, never an Error: envelope, and write nothing.
    #     Guarded on find_spec so a playwright-equipped host narrows the case
    #     instead of reding: the render itself stays release-gate UAT (T-4-17).
    png_ok = True
    png_markup = (
        '<svg role="img" aria-labelledby="p-title" viewBox="0 0 200 120" '
        'xmlns="http://www.w3.org/2000/svg"><title id="p-title">Png</title>'
        '<rect x="8" y="8" width="40" height="20" fill="#2e5aa8" stroke="#0a0a0a"/></svg>'
    )
    if importlib.util.find_spec("playwright") is None:
        with tempfile.TemporaryDirectory(prefix="owui-png-") as png_tmp:
            png_tool = Tools()
            png_tool.valves.export_dir = png_tmp
            png_report = asyncio.run(png_tool.export_diagram(png_markup, "png", "png absent"))
            png_lines = png_report.splitlines()
            if png_report.startswith("Error:"):
                png_ok = False
                failures.append(f"the png-absent path returned an error envelope: {png_lines[0]!r} (A2)")
            if "PNG export unavailable" not in png_lines[0] or "playwright is not installed" not in png_lines[0]:
                png_ok = False
                failures.append(
                    f"the png-absent notice's first line is {png_lines[0]!r} — expected "
                    "'PNG export unavailable … playwright is not installed' (EXP-03)"
                )
            if len(png_lines) < 2 or png_lines[1] != "Nothing was written.":
                png_ok = False
                failures.append(
                    f"the png-absent notice's second line is {png_lines[1] if len(png_lines) > 1 else '<none>'!r} "
                    "— expected 'Nothing was written.' (A2)"
                )
            for tier in ("html", "svg", "png"):
                if not any(line.startswith(f"- {tier}:") for line in png_lines):
                    png_ok = False
                    failures.append(f"the png-absent notice carries no '{tier}' tier line (D-03)")
            if not any(line.startswith("- png: unavailable") for line in png_lines):
                png_ok = False
                failures.append(
                    f"the png tier line is {[line for line in png_lines if line.startswith('- png:') ]!r} "
                    "— expected it to start '- png: unavailable' (EXP-03)"
                )
            if any(Path(png_tmp).iterdir()):
                png_ok = False
                failures.append("the degraded png path wrote a file — absence must write nothing (A2)")
        if png_ok:
            print(
                "OK: with playwright absent the png tier returns the report-shaped notice "
                "('PNG export unavailable … playwright is not installed' / 'Nothing was written.' / "
                "all three tier lines) and writes nothing (EXP-03/A2)"
            )
    else:
        # Playwright IS importable here: only the contract holds, not absence.
        with tempfile.TemporaryDirectory(prefix="owui-png-present-") as png_tmp:
            png_tool = Tools()
            png_tool.valves.export_dir = png_tmp
            png_report = asyncio.run(png_tool.export_diagram(png_markup, "png", "png present"))
            names_png = bool(re.search(r"\.png \([\d,]+ bytes\)", png_report))
            if not (names_png or "PNG export unavailable" in png_report):
                png_ok = False
                failures.append(
                    f"the png export on a playwright host returned {png_report.splitlines()[0]!r} — "
                    "expected either a written .png or the unavailable notice"
                )
        if png_ok:
            print(
                "NOTE: playwright is importable on this host, so case 29 skipped the "
                "absence assertions (a browser install is release-gate UAT, not a suite dependency)"
            )

    # 30. D-05 / A6: the export valves are read at CALL time from the instance,
    #     never cached in __init__ and never through the class attribute (the
    #     pydantic v2.13.4 constraint case 9 already encodes). Three sub-checks:
    #     an override assigned after construction still lands, the relative
    #     default chains through DATA_DIR and auto-creates the leaf, and the
    #     input cap rejects an oversized payload without writing.
    valve_ok = True
    with tempfile.TemporaryDirectory(prefix="owui-valve-override-") as override_tmp:
        override_tool = Tools()  # constructed with the "exports" default, THEN aimed
        override_tool.valves.export_dir = override_tmp
        override_report = asyncio.run(override_tool.export_diagram(png_markup, "html", "override"))
        if not (Path(override_tmp) / "override.html").is_file():
            valve_ok = False
            failures.append(
                f"an export_dir assigned after construction was not honoured — report: "
                f"{override_report.splitlines()[0]!r} (D-05/A6: valves are read at call time)"
            )
        if getattr(override_tool.valves, "export_dir", None) != override_tmp:
            valve_ok = False
            failures.append("the instance valve read does not round-trip the assigned value (case 9's instance-read contract)")
    data_dir_saved = os.environ.get("DATA_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="owui-datadir-") as data_tmp:
            os.environ["DATA_DIR"] = data_tmp
            chain_tool = Tools()  # export_dir stays at its "exports" default
            if getattr(chain_tool.valves, "export_dir", None) != "exports":
                valve_ok = False
                failures.append(
                    f"the default export_dir is {getattr(chain_tool.valves, 'export_dir', None)!r} — expected 'exports' (D-05)"
                )
            chain_report = asyncio.run(chain_tool.export_diagram(png_markup, "html", "chained"))
            chained = Path(data_tmp) / "exports" / "chained.html"
            if not chained.is_file():
                valve_ok = False
                failures.append(
                    f"the relative default did not resolve to <DATA_DIR>/exports/ — report: "
                    f"{chain_report.splitlines()[0]!r} (D-05)"
                )
        # (the TemporaryDirectory context exited above: the leaf must have been
        # auto-created by the tool, not pre-existing)
    finally:
        if data_dir_saved is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = data_dir_saved
    with tempfile.TemporaryDirectory(prefix="owui-cap-") as cap_tmp:
        cap_valve_tool = Tools()
        cap_valve_tool.valves.export_dir = cap_tmp
        cap_valve_tool.valves.max_export_bytes = 100
        if len(png_markup.encode("utf-8")) <= 100:
            valve_ok = False
            failures.append("case 30's cap probe is not over the lowered 100-byte cap — the assertion would be vacuous")
        cap_report = asyncio.run(cap_valve_tool.export_diagram(png_markup, "html", "capped"))
        if not cap_report.startswith("Error: html is larger than"):
            valve_ok = False
            failures.append(
                f"an over-cap payload returned {cap_report.splitlines()[0]!r} — expected the "
                "'Error: html is larger than' envelope (T-4-06)"
            )
        if any(Path(cap_tmp).iterdir()):
            valve_ok = False
            failures.append("the capped export wrote a file before rejecting the payload (Pitfall 7)")
    if valve_ok:
        print(
            "OK: the export valves are honoured at call time — post-construction override lands, "
            "the relative default chains through DATA_DIR/exports with auto-create, and the input "
            "cap rejects an oversized payload with nothing written (D-05/A6)"
        )

    # 31. Security domain (T-4-13): an export's I/O stays inside the resolved
    #     export directory — no network, no process event. Case 21's exact
    #     mechanics, adapted: the hook is installed immediately before the
    #     window, the assertion is a delta slice (hooks are process-global and
    #     irremovable), the `open` events of the write itself are EXPECTED and
    #     filtered, and the call is first proven to do real work so the
    #     denylist assertion cannot green on a no-op.
    scoped_ok = True
    scoped_imports: list[str] = []
    real_import = builtins.__import__

    def recording_scoped_import(name, globals=None, locals=None, fromlist=(), level=0):
        scoped_imports.append(name)
        return real_import(name, globals, locals, fromlist, level)

    scoped_file_events = ("open", "os.remove", "os.rename", "os.truncate", "os.chmod",
                          "shutil.copyfile", "mmap.__new__")
    with tempfile.TemporaryDirectory(prefix="owui-scoped-") as scoped_tmp:
        scoped_tool = Tools()
        scoped_tool.valves.export_dir = scoped_tmp
        sys.addaudithook(_denylist_hook)
        window_start = len(AUDIT_HITS)
        _reset_selfpipe_licenses()
        builtins.__import__ = recording_scoped_import
        try:
            scoped_report = asyncio.run(scoped_tool.export_diagram(png_markup, "html", "scoped"))
        finally:
            builtins.__import__ = real_import
        scoped_window = AUDIT_HITS[window_start:]
        # Fail loud first (case 21's framing): the artifact must exist and the
        # report must name it, inside the temp dir, before any denylist claim.
        named_dir = re.search(r"to (\S+)\.", scoped_report.splitlines()[0])
        scoped_path = Path(scoped_tmp) / "scoped.html"
        if named_dir is None:
            scoped_ok = False
            failures.append(f"the audited export's report starts {scoped_report.splitlines()[0]!r} — no directory to audit")
        elif Path(named_dir.group(1)) != Path(scoped_tmp) or not Path(named_dir.group(1)).is_absolute():
            scoped_ok = False
            failures.append(
                f"the audited export wrote to {named_dir.group(1)!r}, not the absolute temp valve dir "
                f"{scoped_tmp!r} (T-4-13)"
            )
        if not scoped_path.is_file():
            scoped_ok = False
            failures.append("the audited export produced no file — the scoped-I/O claim is unprovable without real work (T-4-16)")
        remaining = [hit for hit in scoped_window if not hit.startswith(scoped_file_events)]
        if remaining:
            scoped_ok = False
            failures.append(
                f"export_diagram produced {len(remaining)} audited network/process event(s) at call "
                f"time (T-4-13): {remaining[:5]}"
            )
        if len(scoped_window) == 0:
            scoped_ok = False
            failures.append(
                "the audit window recorded nothing at all — the write's own `open` events were "
                "expected, so an empty slice means the hook never saw the call (T-4-16)"
            )
    non_stdlib = sorted(
        name for name in scoped_imports
        if name.split(".")[0] not in sys.stdlib_module_names
    )
    playwright_imports = [name for name in scoped_imports if name.split(".")[0] == "playwright"]
    if playwright_imports:
        scoped_ok = False
        failures.append(
            "export_diagram imported playwright on an html-tier call — the optional dependency must "
            "only load inside the png branch (D-09)"
        )
    if non_stdlib:
        scoped_ok = False
        failures.append(
            f"export_diagram imported non-stdlib module(s) at call time: {non_stdlib[:5]} (T-4-SC)"
        )
    if scoped_ok:
        print(
            f"OK: the export's audited I/O stays scoped — {len(scoped_window)} file event(s), "
            f"{len(remaining)} network/process event(s), and only stdlib call-time imports "
            f"({len(scoped_imports)} name(s), no playwright) (T-4-13)"
        )

    # 33. BRND-01: the apply_brand_profile call grammar (D-05) — read, store,
    #     clear — and the markdown reports those calls return (D-10). Every
    #     call runs against its own isolated profiles dir, so no case can
    #     pollute another's on-disk state or the default-palette verdicts.
    brand_ok = True
    brand_user = {"id": "user-1"}
    brand_tool = _isolated_profiles()
    empty_read = asyncio.run(brand_tool.apply_brand_profile(__user__=brand_user))
    if "no profile active" not in empty_read:
        brand_ok = False
        failures.append(
            f"apply_brand_profile() with both args empty returned {empty_read.splitlines()[0]!r} "
            "— the read leg must report that no profile is active (D-05)"
        )
    if empty_read.startswith("Error: "):
        brand_ok = False
        failures.append(
            f"the both-empty read came back as an error envelope: {empty_read.splitlines()[0]!r} "
            "— a read is a report, not a rejection (D-05/D-10)"
        )
    store_report = asyncio.run(
        brand_tool.apply_brand_profile(
            '{"paper": "#F5F5F5", "ink": "#0a0a0a", "accent": "#eb6c36"}', __user__=brand_user
        )
    )
    brand_dir = Path(brand_tool.valves.profiles_dir)
    brand_file = brand_dir / "user-1.json"
    if not brand_file.is_file():
        brand_ok = False
        failures.append(
            f"the store leg wrote no {brand_file} — report was {store_report.splitlines()[0]!r} "
            "(BRND-01: state lives on disk, one file per user)"
        )
    if not store_report.startswith("Error: ") and "user-1.json" not in store_report:
        brand_ok = False
        failures.append(
            f"the store report does not name the sanitized file: {store_report.splitlines()[0]!r} "
            "(D-10: the report carries the path, never the markup or the raw id)"
        )
    if "validate_diagram" not in store_report:
        brand_ok = False
        failures.append(
            "the store report does not say what validate_diagram now does with the palette (D-10)"
        )
    if brand_file.is_file():
        payload = json.loads(brand_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "tokens" not in payload or "updated_at" not in payload:
            brand_ok = False
            failures.append(
                "the stored profile is not the {tokens, updated_at} schema — "
                f"keys were {sorted(payload) if isinstance(payload, dict) else type(payload).__name__}"
            )
        else:
            if payload["tokens"].get("paper") != "#f5f5f5":
                brand_ok = False
                failures.append(
                    f"the stored paper role is {payload['tokens'].get('paper')!r} — hexes are "
                    "normalized (lowercased, #abc expanded) before the store"
                )
            if not isinstance(payload["updated_at"], str) or not payload["updated_at"]:
                brand_ok = False
                failures.append("the stored profile carries no readable updated_at timestamp")
    read_back = asyncio.run(brand_tool.apply_brand_profile(__user__=brand_user))
    if "no profile active" in read_back:
        brand_ok = False
        failures.append("the read-back leg reports no profile active right after a store (D-05)")
    if "#eb6c36" not in read_back:
        brand_ok = False
        failures.append(
            f"the read-back report does not name the stored accent value: {read_back.splitlines()[0]!r}"
        )
    clear_report = asyncio.run(brand_tool.apply_brand_profile("{}", __user__=brand_user))
    if brand_file.exists():
        brand_ok = False
        failures.append(
            "tokens_json='{}' left the profile file on disk — the clear leg must remove it (D-05)"
        )
    if not clear_report.startswith("Error: ") and "cleared" not in clear_report:
        brand_ok = False
        failures.append(
            f"the clear leg returned {clear_report.splitlines()[0]!r} — expected a deletion "
            "confirmation that names the removed profile (D-10)"
        )
    after_clear = asyncio.run(brand_tool.apply_brand_profile(__user__=brand_user))
    if "no profile active" not in after_clear:
        brand_ok = False
        failures.append(
            "a read after the clear still sees the old profile — the file must be gone (D-05)"
        )
    clean_html = (FIXTURE_DIR / "clean-base.html").read_text(encoding="utf-8")
    post_clear_validate = asyncio.run(brand_tool.validate_diagram(clean_html))
    if "brand profile" in post_clear_validate.splitlines()[0]:
        brand_ok = False
        failures.append(
            "validate_diagram still names a brand profile after the clear — the verdict must "
            "fall back to the default palette (D-05)"
        )
    for bad_arg_report, label in (
        (asyncio.run(brand_tool.apply_brand_profile(7, __user__=brand_user)), "tokens_json=7"),
        (asyncio.run(brand_tool.apply_brand_profile("", 7, __user__=brand_user)), "url=7"),
    ):
        if not bad_arg_report.startswith("Error: "):
            brand_ok = False
            failures.append(f"{label} returned {bad_arg_report.splitlines()[0]!r} — non-string args are targeted rejections")
        elif "internal failure" in bad_arg_report:
            brand_ok = False
            failures.append(
                f"{label} claims an internal failure — the arg check is targeted, not a crash (REL-01)"
            )
    if brand_ok:
        print("OK: apply_brand_profile reads, stores, clears and reports per user (BRND-01/D-05/D-10)")

    # 34. D-02 reject-whole: a bad key or a malformed hex rejects the WHOLE
    #     payload, names the offending key AND all ten valid roles, writes
    #     nothing, and leaves the previously stored profile active.
    reject_ok = True
    reject_user = {"id": "reject-user"}
    reject_tool = _isolated_profiles()
    seed = asyncio.run(reject_tool.apply_brand_profile('{"ink": "#0a0a0a"}', __user__=reject_user))
    reject_dir = Path(reject_tool.valves.profiles_dir)
    listing_before = sorted(p.name for p in reject_dir.iterdir()) if reject_dir.is_dir() else []
    if not seed.startswith("Error: ") and "reject-user.json" not in seed:
        reject_ok = False
        failures.append(f"the reject case's seed store wrote nothing: {seed.splitlines()[0]!r}")
    for payload_json, bad_name in (
        ('{"paper": "#1122zz"}', "paper"),
        ('{"brand-x": "#123456"}', "brand-x"),
        ('{"ink": 7}', "ink"),
        ("[1, 2]", ""),
        ("{not json}", ""),
    ):
        report = asyncio.run(
            reject_tool.apply_brand_profile(payload_json, __user__=reject_user)
        )
        if not report.startswith("Error: "):
            reject_ok = False
            failures.append(
                f"tokens_json={payload_json!r} returned {report.splitlines()[0]!r} — D-02 rejects "
                "the whole payload with an Error: envelope"
            )
            continue
        if "internal failure" in report:
            reject_ok = False
            failures.append(
                f"tokens_json={payload_json!r} claims an internal failure — the rejection is a "
                "targeted check, not a crash (REL-01)"
            )
        if bad_name and bad_name not in report:
            reject_ok = False
            failures.append(
                f"the rejection of {payload_json!r} does not name the offending key {bad_name!r} (D-02)"
            )
        for valid_role in ("accent-tint", "rule-solid", "paper-2"):
            if bad_name and valid_role not in report:
                reject_ok = False
                failures.append(
                    f"the rejection of {payload_json!r} does not list the valid role "
                    f"{valid_role!r} — D-02's envelope names all ten roles"
                )
                break
    listing_after = sorted(p.name for p in reject_dir.iterdir()) if reject_dir.is_dir() else []
    if listing_before != listing_after:
        reject_ok = False
        failures.append(
            f"the profiles dir changed across {len(listing_before)} -> {len(listing_after)} entries "
            "— a rejected payload must write nothing (D-02)"
        )
    still_active = asyncio.run(reject_tool.apply_brand_profile(__user__=reject_user))
    if "#0a0a0a" not in still_active:
        reject_ok = False
        failures.append(
            "the previously stored profile is gone after the rejections — reject-whole must leave "
            "the last good profile active (D-02)"
        )
    if reject_ok:
        print("OK: a bad role key or hex rejects whole, writes nothing, and keeps the last profile (D-02)")

    # 35. BRND-02/T-5-01/T-5-02/T-5-03: profile state is per user, a hostile id
    #     cannot escape the profiles directory, and no report ever carries the
    #     raw id. Two ids share one dir; each must see only its own palette.
    iso_ok = True
    iso_dir_tool = _isolated_profiles()
    iso_dir = Path(iso_dir_tool.valves.profiles_dir)
    user_a, user_b, hostile = {"id": "user-a"}, {"id": "user-b"}, {"id": "../../etc/passwd"}
    reports: list[str] = []
    reports.append(
        asyncio.run(iso_dir_tool.apply_brand_profile('{"accent": "#eb6c36"}', __user__=user_a))
    )
    reports.append(
        asyncio.run(iso_dir_tool.apply_brand_profile('{"ink": "#2b2b2b"}', __user__=user_b))
    )
    a_file, b_file = iso_dir / "user-a.json", iso_dir / "user-b.json"
    if not (a_file.is_file() and b_file.is_file()):
        iso_ok = False
        failures.append(
            f"two user ids against one dir did not produce two files: "
            f"{sorted(p.name for p in iso_dir.iterdir()) if iso_dir.is_dir() else []} (T-5-03)"
        )
    read_a = asyncio.run(iso_dir_tool.apply_brand_profile(__user__=user_a))
    reports.append(read_a)
    if "#2b2b2b" in read_a:
        iso_ok = False
        failures.append(
            "user-a's read-back names user-b's ink value — profile state leaked across users (T-5-03)"
        )
    read_b = asyncio.run(iso_dir_tool.apply_brand_profile(__user__=user_b))
    reports.append(read_b)
    if "#eb6c36" in read_b:
        iso_ok = False
        failures.append(
            "user-b's read-back names user-a's accent value — profile state leaked across users (T-5-03)"
        )
    hostile_store = asyncio.run(
        iso_dir_tool.apply_brand_profile('{"paper": "#f5f5f5"}', __user__=hostile)
    )
    # Assert the hostile store landed BEFORE the clear leg runs — the clear
    # removes the very file this leg is proving the location of.
    hostile_file = iso_dir / "etc-passwd.json"
    if not hostile_file.is_file():
        iso_ok = False
        failures.append(
            "the hostile id did not land as etc-passwd.json inside the profiles dir — found "
            f"{sorted(p.name for p in iso_dir.iterdir()) if iso_dir.is_dir() else []} (T-5-01); "
            f"store report was {hostile_store.splitlines()[0]!r}"
        )
    hostile_reports = (
        hostile_store,
        asyncio.run(iso_dir_tool.apply_brand_profile(__user__=hostile)),
        asyncio.run(iso_dir_tool.apply_brand_profile("{}", __user__=hostile)),
    )
    if (iso_dir.parent / "etc-passwd.json").exists() or (Path("/etc") / "passwd.json").exists():
        iso_ok = False
        failures.append("a file escaped the profiles directory — traversal was not neutralized (T-5-01)")
    for report in (*reports, *hostile_reports):
        if "../../etc/passwd" in report:
            iso_ok = False
            failures.append(
                f"a returned string carries the raw id: {report.splitlines()[0]!r} (T-5-02/D-10)"
            )
    if iso_ok:
        print(
            "OK: profiles are per user, the hostile id stays inside the dir as etc-passwd.json, "
            "and no report carries the raw id (T-5-01/T-5-02/T-5-03)"
        )

    # 36. SC3/D-09: an active profile substitutes its palette for the default in
    #     the skin family and the verdict's first line names it — the same
    #     markup that FAILS on the default palette PASSES for the profile's
    #     owner, and FAILS again (plain first line) for a second user, for a
    #     second tool with its own empty dir, and after a clear (T-5-09). Also
    #     pinned here: BOTH membership tests are substituted — a profile-derived
    #     rgba must survive, not only a profile hex (B3: a hex-only substitution
    #     would silently leave the rgba set on the defaults) — short and
    #     uppercase profile hexes normalize exactly the way authored hex does,
    #     pure #000000 stays banned even when the profile contains it (D-09's
    #     branch order, no special case), and a substitution call never rebinds
    #     the module globals (T-5-08: a global write would leak one user's
    #     palette to every other user in the singleton process).
    substitution_ok = True

    def _brand_svg(fill_hex="", fill_rgba=""):
        """A minimal aria-wired diagram over the given skin tokens, so the only
        possible findings are skin ones and the first line is exact."""
        fills = ""
        if fill_hex:
            fills += f'<rect x="4" y="4" width="32" height="22" fill="{fill_hex}"/>'
        if fill_rgba:
            fills += f'<rect x="6" y="6" width="28" height="18" fill="{fill_rgba}"/>'
        return (
            '<svg role="img" aria-labelledby="bt1 bt2" viewBox="0 0 40 30" '
            'xmlns="http://www.w3.org/2000/svg">'
            '<title id="bt1">Brand</title><desc id="bt2">Brand palette fragment</desc>'
            + fills
            + "</svg>"
        )

    substitution_tool = _isolated_profiles()
    profile_owner = {"id": "user-1"}
    profile_stranger = {"id": "user-2"}
    stored_palette = '{"paper": "#fbfbf2", "ink": "#101d42", "accent": "#00a4a4"}'
    asyncio.run(substitution_tool.apply_brand_profile(stored_palette, __user__=profile_owner))
    palette_clean = _brand_svg("#101d42", "rgba(0, 164, 164, 0.5)")

    # (1) The owner's verdict: profile hex AND profile-derived rgba pass, and
    #     the first line carries the D-09 suffix naming the active slug.
    owner_report = asyncio.run(
        substitution_tool.validate_diagram(palette_clean, __user__=profile_owner)
    )
    owner_first = owner_report.splitlines()[0]
    if not re.match(r"^PASS: 0 issues — brand profile: user-1$", owner_first):
        substitution_ok = False
        failures.append(
            f"a profile-colour diagram returned {owner_first!r} — expected the suffixed PASS "
            "first line (D-09: the verdict names the active profile)"
        )
    if "not in the style-guide palette" in owner_report:
        substitution_ok = False
        failures.append(
            "a profile hex was flagged as off-palette for its own owner — the substitution "
            "did not replace the default palette (D-09/SC3)"
        )
    if "not derived from an allowed palette color" in owner_report:
        substitution_ok = False
        failures.append(
            "a profile-derived rgba() was flagged for its own owner — only the hex membership "
            "test was substituted, the rgba set stayed on the defaults (B3)"
        )

    # (2) The FAIL variant of the same first line: an off-palette colour under
    #     an ACTIVE profile still fails, and the line still names the profile.
    owner_fail = asyncio.run(
        substitution_tool.validate_diagram(_brand_svg("#123456"), __user__=profile_owner)
    ).splitlines()[0]
    if not re.match(r"^FAIL: \d+ issue\(s\) — brand profile: user-1$", owner_fail):
        substitution_ok = False
        failures.append(
            f"an off-palette colour under an active profile returned {owner_fail!r} — expected "
            "'FAIL: N issue(s) — brand profile: <slug>' (D-09)"
        )

    # (3) Cross-user: the SAME markup fails on the default palette for a second
    #     user on the same instance, with a PLAIN first line (no suffix).
    stranger_report = asyncio.run(
        substitution_tool.validate_diagram(palette_clean, __user__=profile_stranger)
    )
    stranger_first = stranger_report.splitlines()[0]
    if not re.match(r"^FAIL: \d+ issue\(s\)$", stranger_first):
        substitution_ok = False
        failures.append(
            f"the same markup for a second user returned {stranger_first!r} — expected the plain "
            "FAIL first line on the default palette (D-09/T-5-09)"
        )
    if "not in the style-guide palette" not in stranger_report:
        substitution_ok = False
        failures.append(
            "a profile hex was not flagged for a second user — one user's palette leaked into "
            "another's verdict (T-5-09)"
        )

    # (4) Cross-instance: a second Tools() with its own empty dir also gets the
    #     default verdict — substitution is per-call state, never process-global.
    default_report = asyncio.run(_isolated_profiles().validate_diagram(palette_clean))
    if not re.match(r"^FAIL: \d+ issue\(s\)$", default_report.splitlines()[0]):
        substitution_ok = False
        failures.append(
            f"the same markup on a profile-free instance returned {default_report.splitlines()[0]!r} "
            "— substitution must be scoped to a resolved profile, never process-global"
        )

    # (5) The rgba-only markup: the second membership test stands on its own.
    rgba_only = _brand_svg(fill_rgba="rgba(0, 164, 164, 0.5)")
    owner_rgba = asyncio.run(
        substitution_tool.validate_diagram(rgba_only, __user__=profile_owner)
    ).splitlines()[0]
    stranger_rgba = asyncio.run(
        substitution_tool.validate_diagram(rgba_only, __user__=profile_stranger)
    ).splitlines()[0]
    if not re.match(r"^PASS: 0 issues — brand profile: user-1$", owner_rgba):
        substitution_ok = False
        failures.append(
            f"a profile-derived rgba() alone returned {owner_rgba!r} — expected a suffixed PASS "
            "(B3: the rgba membership set must follow the profile too)"
        )
    if "not derived from an allowed palette color" not in asyncio.run(
        substitution_tool.validate_diagram(rgba_only, __user__=profile_stranger)
    ):
        substitution_ok = False
        failures.append(
            f"a default-illegal rgba() was not flagged for a second user ({stranger_rgba!r}) — "
            "the rgba set must fall back to the default palette"
        )

    # (6) Short and uppercase profile values normalize exactly as authored hex
    #     does (#1a2 -> #11aa22, #F0F -> #ff00ff), so a diagram authored with the
    #     expanded form passes for the profile's owner.
    short_palette = '{"accent": "#1a2", "ink": "#F0F"}'
    short_user = {"id": "user-3"}
    asyncio.run(substitution_tool.apply_brand_profile(short_palette, __user__=short_user))
    short_report = asyncio.run(
        substitution_tool.validate_diagram(
            _brand_svg("#11aa22", "rgba(255, 0, 255, 0.5)"), __user__=short_user
        )
    ).splitlines()[0]
    if not re.match(r"^PASS: 0 issues — brand profile: user-3$", short_report):
        substitution_ok = False
        failures.append(
            f"a diagram over expanded short-form profile hexes returned {short_report!r} — the "
            "substituted set must normalize profile values like authored hex (#1a2 -> #11aa22)"
        )

    # (7) Pure black stays banned universally: a profile that CONTAINS #000000
    #     does not license it (branch order, no special case) — D-09.
    black_user = {"id": "user-4"}
    asyncio.run(
        substitution_tool.apply_brand_profile('{"ink": "#000000"}', __user__=black_user)
    )
    black_report = asyncio.run(
        substitution_tool.validate_diagram(_brand_svg("#000000"), __user__=black_user)
    )
    if "pure black #000000 is not allowed" not in black_report:
        substitution_ok = False
        failures.append(
            f"a profile containing #000000 licensed it: {black_report.splitlines()[0]!r} — pure "
            "black must stay banned even when the profile itself contains it (D-09)"
        )
    if not re.match(
        r"^FAIL: \d+ issue\(s\) — brand profile: user-4$", black_report.splitlines()[0]
    ):
        substitution_ok = False
        failures.append(
            f"the pure-black verdict under a profile returned {black_report.splitlines()[0]!r} — "
            "the ban fires inside a profiled verdict, so the line must still name the profile"
        )

    # (8) The clear leg returns the owner to the default verdict: the same
    #     markup that passed in (1) fails again, with a plain first line.
    asyncio.run(substitution_tool.apply_brand_profile("{}", __user__=profile_owner))
    cleared_report = asyncio.run(
        substitution_tool.validate_diagram(palette_clean, __user__=profile_owner)
    ).splitlines()[0]
    if not re.match(r"^FAIL: \d+ issue\(s\)$", cleared_report):
        substitution_ok = False
        failures.append(
            f"after a clear the owner's markup returned {cleared_report!r} — clearing must restore "
            "the default palette and the plain first line (D-05/D-09)"
        )

    # (9) Guard row (T-5-08): a substitution call must never rebind the module
    #     palette — expected values are recomputed here from the module's own
    #     constants, never from a snapshot the tool could have mutated in step.
    expected_default_palette = frozenset(namespace["_PALETTE_HEX_STR"].split())
    expected_default_rgb = frozenset(
        tuple(int(c[i : i + 2], 16) for i in (1, 3, 5))
        for c in expected_default_palette
        if len(c) == 7
    )
    if (
        namespace["_PALETTE"] != expected_default_palette
        or namespace["_PALETTE_RGB"] != expected_default_rgb
    ):
        substitution_ok = False
        failures.append(
            "a substitution call changed _PALETTE/_PALETTE_RGB (T-5-08) — the module globals must "
            "never be rebound, or one user's palette becomes every user's palette"
        )

    if substitution_ok:
        print(
            "OK: an active profile substitutes its palette in the skin family and the first line "
            "names it — profile hexes and profile rgba pass for the owner and fail for a second "
            "user, on a profile-free instance and after a clear, while pure black stays banned "
            "and the module palette is untouched (D-09/SC3/B3/T-5-08/T-5-09)"
        )

    # 37. T-5-04/D-03: every corrupt or absent profile shape degrades to "no
    #     profile active" and the default verdict — never a crash, never a
    #     claimed internal failure, never another user's data.
    corrupt_ok = True
    corrupt_tool = _isolated_profiles()
    corrupt_dir = Path(corrupt_tool.valves.profiles_dir)
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    corrupt_shapes = (
        ("truncated JSON", '{"tokens": {"ink": "#0a0a'),
        ("a JSON array", '["not", "an", "object"]'),
        ("a non-dict tokens map", '{"tokens": 7, "updated_at": "2026-01-01T00:00:00Z"}'),
        ("an empty tokens map", '{"tokens": {}, "updated_at": "2026-01-01T00:00:00Z"}'),
    )
    clean_html = (FIXTURE_DIR / "clean-base.html").read_text(encoding="utf-8")
    for label, body in corrupt_shapes:
        (corrupt_dir / "user-c.json").write_text(body, encoding="utf-8")
        read = asyncio.run(corrupt_tool.apply_brand_profile(__user__={"id": "user-c"}))
        verdict = asyncio.run(corrupt_tool.validate_diagram(clean_html))
        outputs = (read, verdict)
        if "no profile active" not in read:
            corrupt_ok = False
            failures.append(f"a {label} read as an active profile: {read.splitlines()[0]!r} (T-5-04)")
        for output in outputs:
            if "internal failure" in output:
                corrupt_ok = False
                failures.append(
                    f"a {label} produced an internal-failure string — a corrupt own file must "
                    "degrade to no profile active, never an error (T-5-04/D-03)"
                )
        if "brand profile" in verdict.splitlines()[0]:
            corrupt_ok = False
            failures.append(
                f"a {label} still drove the verdict first line: {verdict.splitlines()[0]!r} — "
                "the default palette must apply"
            )
    missing_read = asyncio.run(corrupt_tool.apply_brand_profile(__user__={"id": "no-such-user"}))
    if "no profile active" not in missing_read or "internal failure" in missing_read:
        corrupt_ok = False
        failures.append(
            f"an absent profile file read as {missing_read.splitlines()[0]!r} — a missing dir/file "
            "must behave as no profile active (D-03)"
        )
    if corrupt_ok:
        print(
            "OK: truncated JSON, arrays, non-dict/empty token maps and absent files all degrade "
            "to no profile active with the default verdict (T-5-04/D-03)"
        )

    # 38. SC2/TEST-03 (the phase's crown proof): the profile is DISK state, not
    #     instance state — a fresh Tools() applies a profile a previous instance
    #     stored, and (the cache-killer, T-5-12) a mutation of that file BETWEEN
    #     the two instances is observed by the second one. An implementation
    #     that caches the palette on the instance or at module scope cannot pass
    #     the mutation legs: the verdict has to flip because the FILE changed,
    #     not because a call was remembered (research M5's step 4 is what
    #     distinguishes "reads the file" from "remembers the call"). Also pinned:
    #     the cross-instance file-existence check, the sanitized path in the
    #     store report, second-user isolation on the fresh instance, zero
    #     profile state cached at module or class scope, and — the SC4 leg — an
    #     export html-tier round trip through a profiled instance still writing
    #     the markup byte-identically (cases 26/27/29 remain the canonical tier
    #     assertions and run green in this same pass).
    reinstantiate_ok = True
    reinstantiation_tmp = tempfile.TemporaryDirectory(
        prefix="owui-profiles-reinstantiation-"
    )
    profile_tmp_dirs.append(reinstantiation_tmp)
    reinstantiation_dir = reinstantiation_tmp.name
    brand_markup = _brand_svg("#101d42", "rgba(0, 164, 164, 0.5)")

    # M5 step 1-2: instance A stores; the file and the sanitized report path.
    instance_a = Tools()
    instance_a.valves.profiles_dir = reinstantiation_dir
    stored_report_a = asyncio.run(
        instance_a.apply_brand_profile(
            '{"paper": "#fbfbf2", "ink": "#101d42", "accent": "#00a4a4"}',
            __user__={"id": "user-1"},
        )
    )
    profile_file = Path(reinstantiation_dir) / "user-1.json"
    if not profile_file.is_file():
        reinstantiate_ok = False
        failures.append(
            f"instance A's store wrote no {profile_file} — report was "
            f"{stored_report_a.splitlines()[0]!r} (BRND-02: the state must be on disk for a "
            "fresh instance to find)"
        )
    if "user-1.json" not in stored_report_a:
        reinstantiate_ok = False
        failures.append(
            f"instance A's store report does not name the sanitized path: "
            f"{stored_report_a.splitlines()[0]!r} (D-10)"
        )

    # M5 step 3: instance B is a FRESH Tools() — the only thing it shares with A
    # is the directory A wrote into.
    instance_b = Tools()
    instance_b.valves.profiles_dir = reinstantiation_dir
    b_first = asyncio.run(
        instance_b.validate_diagram(brand_markup, __user__={"id": "user-1"})
    ).splitlines()[0]
    if not re.match(r"^PASS: 0 issues — brand profile: user-1$", b_first):
        reinstantiate_ok = False
        failures.append(
            f"a fresh Tools() did not apply the profile A stored: {b_first!r} — expected the "
            "suffixed PASS first line (SC2/TEST-03)"
        )

    # Isolation leg, on the fresh instance: a second user id on the SAME
    # directory gets the default verdict for the same markup (T-5-09).
    b_stranger = asyncio.run(
        instance_b.validate_diagram(brand_markup, __user__={"id": "user-2"})
    ).splitlines()[0]
    if not re.match(r"^FAIL: \d+ issue\(s\)$", b_stranger) or "brand profile" in b_stranger:
        reinstantiate_ok = False
        failures.append(
            f"a second user on the fresh instance got {b_stranger!r} — expected the plain FAIL "
            "first line of the default palette (T-5-09)"
        )

    # M5 step 4, cache-killer 1: a DIFFERENT palette lands on disk through A.
    # B's next validate must reflect the file: the markup's rgba() is no longer
    # derived from the stored accent, so the verdict flips to a profiled FAIL.
    asyncio.run(
        instance_a.apply_brand_profile(
            '{"paper": "#fbfbf2", "ink": "#101d42", "accent": "#112233"}',
            __user__={"id": "user-1"},
        )
    )
    b_after_restore = asyncio.run(
        instance_b.validate_diagram(brand_markup, __user__={"id": "user-1"})
    )
    if not re.match(
        r"^FAIL: 1 issue\(s\) — brand profile: user-1$", b_after_restore.splitlines()[0]
    ):
        reinstantiate_ok = False
        failures.append(
            f"after instance A re-stored a different palette, instance B returned "
            f"{b_after_restore.splitlines()[0]!r} — expected 'FAIL: 1 issue(s) — brand profile: "
            "user-1'; a verdict that did not flip is being fed by a cache, not the file (T-5-12)"
        )
    if "not derived from an allowed palette color" not in b_after_restore:
        reinstantiate_ok = False
        failures.append(
            "after the re-store, the no-longer-derived rgba() was not flagged — the substituted "
            "rgb set did not follow the mutated file (B3/T-5-12)"
        )

    # M5 step 4, cache-killer 2: the file is REMOVED. B must fall back to the
    # default verdict and flag both skin tokens again.
    os.remove(profile_file)
    b_after_remove = asyncio.run(
        instance_b.validate_diagram(brand_markup, __user__={"id": "user-1"})
    )
    removed_first = b_after_remove.splitlines()[0]
    if not re.match(r"^FAIL: 2 issue\(s\)$", removed_first) or "brand profile" in removed_first:
        reinstantiate_ok = False
        failures.append(
            f"after the profile file was removed, instance B returned {removed_first!r} — "
            "expected the plain 'FAIL: 2 issue(s)' default verdict; a surviving profiled verdict "
            "is a cached one (BRND-02/T-5-12)"
        )
    if (
        "not in the style-guide palette" not in b_after_remove
        or "not derived from an allowed palette color" not in b_after_remove
    ):
        reinstantiate_ok = False
        failures.append(
            "after the removal, instance B's findings were not the default palette's — the skin "
            "family must fall back wholesale when no profile is active (D-09)"
        )

    # The no-module-cache spot check (research Pitfall 6): neither the exec
    # namespace nor the class may hold a dict/list/set-valued profile attribute
    # for an in-memory implementation to hide behind.
    module_profile_state = [
        name
        for name, value in namespace.items()
        if "profile" in name.casefold() and isinstance(value, (dict, list, set))
    ]
    class_profile_state = [
        name
        for name, value in vars(Tools).items()
        if "profile" in name.casefold() and isinstance(value, (dict, list, set))
    ]
    if module_profile_state or class_profile_state:
        reinstantiate_ok = False
        failures.append(
            "profile state found cached outside the profile files — module: "
            f"{module_profile_state}, class: {class_profile_state} (BRND-02 forbids in-memory "
            "profile state)"
        )

    # SC4 leg: the export html tier is untouched by the palette seam. A profile
    # is re-stored for user-1 first, so the tier is driven end to end under an
    # ACTIVE profile: the markup is written byte-identically, the report is the
    # tier report naming the file, and re-validating the written file through
    # instance B still lands the profiled verdict (export itself stays
    # palette-blind — it writes what was authored, D-07/EXP-01).
    export_tmp = tempfile.TemporaryDirectory(prefix="owui-exports-case38-")
    profile_tmp_dirs.append(export_tmp)
    instance_b.valves.export_dir = export_tmp.name
    asyncio.run(
        instance_a.apply_brand_profile(
            '{"ink": "#101d42", "accent": "#00a4a4"}', __user__={"id": "user-1"}
        )
    )
    export_report = asyncio.run(
        instance_b.export_diagram(brand_markup, "html", "case38-brand")
    )
    exported_file = Path(export_tmp.name) / "case38-brand.html"
    if not exported_file.is_file():
        reinstantiate_ok = False
        failures.append(
            f"the profiled export wrote no {exported_file} — report was "
            f"{export_report.splitlines()[0]!r} (SC4: the palette seam must not disturb export)"
        )
    elif exported_file.read_text(encoding="utf-8") != brand_markup:
        reinstantiate_ok = False
        failures.append(
            "the profiled export did not write the markup byte-identically (EXP-01/D-07)"
        )
    if not export_report.startswith("Exported") or "case38-brand.html" not in export_report:
        reinstantiate_ok = False
        failures.append(
            f"the profiled export returned {export_report.splitlines()[0]!r} — expected the "
            "html-tier report naming the written file (EXP-01)"
        )
    revalidated = asyncio.run(
        instance_b.validate_diagram(exported_file.read_text(encoding="utf-8"), __user__={"id": "user-1"})
    ) if exported_file.is_file() else ""
    if not re.match(r"^PASS: 0 issues — brand profile: user-1$", revalidated.splitlines()[0]):
        reinstantiate_ok = False
        failures.append(
            f"re-validating the exported file returned {revalidated.splitlines()[0]!r} — the "
            "round trip must survive the palette seam with the profile still applied (SC4)"
        )

    if reinstantiate_ok:
        print(
            "OK: a fresh Tools() applies the profile a previous instance stored and observes a "
            "disk mutation between the two (cache-killer) — verdicts flip on re-store and on "
            "removal, a second user stays on the default palette, no profile state is cached at "
            "module or class scope, and the profiled export round trip is untouched "
            "(SC2/TEST-03/BRND-02/T-5-09/T-5-12)"
        )

    # 39. D-07: the url= SSRF rejection battery — every refusal class proven
    #     offline. Pure string/scheme/host rows call the gate's classifier
    #     directly (no socket is connected); the localhost row exercises the
    #     one real getaddrinfo, which resolves out of /etc/hosts with no
    #     network egress; the redirect row asserts _NoRedirect's documented
    #     interception point returns None; every transport/status/size/body
    #     row is driven through a fake opener or fake stream, so no case in
    #     this battery performs a real fetch and the suite stays offline.
    ssrf_ok = True
    tool_mod = {
        name: namespace.get(name)
        for name in ("_refuse_url_reason", "_fetch_brand_url", "_BrandFetchError",
                     "_NoRedirect", "_read_body_capped", "_MAX_URL_BODY", "_URL_TIMEOUT")
    }
    if any(value is None for value in tool_mod.values()):
        ssrf_ok = False
        failures.append(
            "the tool namespace is missing a D-07 symbol: "
            f"{[k for k, v in tool_mod.items() if v is None]} — case 39 cannot reach the gate"
        )
    else:
        refuse = tool_mod["_refuse_url_reason"]
        fetch = tool_mod["_fetch_brand_url"]
        brand_fetch_error = tool_mod["_BrandFetchError"]
        no_redirect = tool_mod["_NoRedirect"]

        # (url, expected-substring) — each refusal must name its own cause.
        string_rows = [
            ("http://example.com/tokens.json", "only https://"),
            ("ftp://example.com/tokens.json", "only https://"),
            ("HTTPS://EXAMPLE.COM/t", None),  # allowed: upper-scheme, checked below
            ("", "must be a non-empty string"),
            ("   ", "must be a non-empty string"),
            ("https:///tokens.json", "only https://"),
            ("https:///tokens.json", "with a hostname"),
            ("https://127.0.0.1/tokens.json", "non-public address (127.0.0.1)"),
            ("https://10.0.0.1/tokens.json", "non-public address (10.0.0.1)"),
            ("https://192.168.1.4/tokens.json", "non-public address (192.168.1.4)"),
            ("https://169.254.169.254/tokens.json", "non-public address (169.254.169.254)"),
            ("https://0.0.0.0/tokens.json", "non-public address (0.0.0.0)"),
            # The dedicated CGNAT row: 100.64.0.0/10 (RFC 6598) has is_private,
            # is_reserved and is_loopback all False, so a flag-list predicate
            # would ALLOW it. Only the inverted is_global form refuses it.
            ("https://100.64.0.1/tokens.json", "non-public address (100.64.0.1)"),
            ("https://[::1]/tokens.json", "non-public address (::1)"),
            ("https://[fc00::1]/tokens.json", "non-public address (fc00::1)"),
            ("https://localhost/tokens.json", "non-public address"),
            # The named adversary: the prototype's host regex ALLOWED this one.
                        ("https://example.com@127.0.0.1/tokens.json", "userinfo"),
            # A bad port must be a targeted envelope, not a ValueError.
            ("https://example.com:notaport/tokens.json", "port"),
        ]
        for row_url, needle in string_rows:
            reason = refuse(row_url)
            if row_url == "HTTPS://EXAMPLE.COM/t":
                if reason is not None:
                    ssrf_ok = False
                    failures.append(
                        f"{row_url!r} was refused ({reason}) — an upper-cased https scheme with a "
                        "public host must still be fetched (scheme compare is case-insensitive)"
                    )
                continue
            if reason is None:
                ssrf_ok = False
                failures.append(f"{row_url!r} was NOT refused — the D-07 gate allowed it")
            elif not reason.startswith("Error: "):
                ssrf_ok = False
                failures.append(
                    f"{row_url!r} produced a non-envelope refusal {reason[:80]!r} — every D-07 "
                    "rejection is a returned 'Error: ...' envelope (REL-01)"
                )
            elif needle and needle not in reason:
                ssrf_ok = False
                failures.append(
                    f"{row_url!r} was refused with {reason[:90]!r} — expected it to name {needle!r}"
                )
        # No hand-rolled host regex may exist on the url path (T-5-SSRF2).
        gate_source = inspect.getsource(refuse)
        if "urllib.parse.urlsplit" not in gate_source:
            ssrf_ok = False
            failures.append(
                "_refuse_url_reason does not parse the host with urllib.parse.urlsplit — a "
                "hand-rolled regex is a named, defeated adversary (T-5-SSRF2)"
            )
            ssrf_ok = False
            failures.append(
                "_refuse_url_reason does not parse the host with urllib.parse.urlsplit — a "
                "hand-rolled regex is a named, defeated adversary (T-5-SSRF2)"
            )

        def expect_envelope(label, fn):
            try:
                fn()
            except brand_fetch_error as exc:
                text = str(exc)
                if not text.startswith("Error: "):
                    ssrf_ok = False
                    failures.append(
                        f"{label} produced a non-envelope refusal {text[:80]!r} (REL-01)"
                    )
                return text
            except Exception as exc:  # noqa: BLE001
                ssrf_ok = False
                failures.append(
                    f"{label} raised {type(exc).__name__} instead of returning an envelope — "
                    "nothing may propagate to the host (REL-01)"
                )
                return ""
            ssrf_ok = False
            failures.append(f"{label} was not refused at all — the gate allowed it through")
            return ""

        # Redirect refusal without any network: the stdlib interception point
        # must return None, which is what turns every 3xx into a refusal.
        if no_redirect().redirect_request(
            None, None, 302, "Found", {}, "https://internal/tokens.json"
        ) is not None:
            ssrf_ok = False
            failures.append(
                "_NoRedirect.redirect_request did not return None — a 3xx would be followed (T-5-SSRF4)"
            )

        class _FakeOpener:
            """Stands in for the built opener; raises or returns on demand."""

            def __init__(self, outcome):
                self._outcome = outcome
                self.requested = None
                self.timeout = None

            def open(self, request, timeout=None):
                self.requested = request
                self.timeout = timeout
                if isinstance(self._outcome, Exception):
                    raise self._outcome
                return self._outcome

        class _FakeStream:
            def __init__(self, payload, chunk=65536):
                self._payload = payload
                self._chunk = chunk
                self._offset = 0

            def read(self, size=-1):
                end = min(self._offset + (size if size > 0 else self._chunk), len(self._payload))
                data = self._payload[self._offset:end]
                self._offset = end
                return data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        base = "https://example.com/tokens.json"

        def _via_opener(outcome):
            opener = _FakeOpener(outcome)
            fetch(base, _opener=opener)
            if opener.timeout != tool_mod['_URL_TIMEOUT']:
                ssrf_ok = False
                failures.append(
                    f"the fetch opened with timeout={opener.timeout!r} — D-07 mandates the 5 s cap (T-5-SSRF6)"
                )

        redirect_text = expect_envelope(
            "a 302 redirect",
            lambda: _via_opener(
                urllib.error.HTTPError(base, 302, "Found", {}, io.BytesIO(b""))
            ),
        )
        if redirect_text and "redirect" not in redirect_text:
            ssrf_ok = False
            failures.append(
                f"the 302 row surfaced {redirect_text[:90]!r} — the envelope must name the redirect"
            )
        expect_envelope(
            "an HTTP 404",
            lambda: _via_opener(urllib.error.HTTPError(base, 404, "Not Found", {}, io.BytesIO(b""))),
        )
        expect_envelope(
            "a URLError",
            lambda: _via_opener(urllib.error.URLError("no route to host")),
        )
        expect_envelope("an OSError", lambda: _via_opener(OSError("network is down")))

        # Oversize: a 262145-byte body must be refused by the read-loop cap,
        # while a 100000-byte body is accepted — proving the cap streams rather
        # than buffering (the fake stream yields 64 KiB at a time, so the cap
        # has to fire mid-stream).
        oversize = b"x" * (tool_mod['_MAX_URL_BODY'] + 1)
        expect_envelope(
            "an over-cap body",
            lambda: tool_mod['_read_body_capped'](_FakeStream(oversize, chunk=65536)),
        )
        try:
            tool_mod['_read_body_capped'](_FakeStream(b"x" * 100000, chunk=65536))
        except brand_fetch_error:
            ssrf_ok = False
            failures.append(
                "a 100000-byte body was refused — the 262144-byte cap must accept it (T-5-SSRF5)"
            )
        except Exception as exc:  # noqa: BLE001
            ssrf_ok = False
            failures.append(f"the 100000-byte body raised {type(exc).__name__}")
        # A non-JSON body is refused whole — nothing reaches the store (T-5-SSRF7).
        expect_envelope(
            "a non-JSON body",
            lambda: _via_opener(_FakeStream(b"<html>not json</html>")),
        )
        # A valid public https JSON body flows all the way through to text.
        valid_body = json.dumps({"ink": "#111111", "accent": "#2563eb"}).encode("utf-8")
        try:
            got = fetch(base, _opener=_FakeOpener(_FakeStream(valid_body)))
        except Exception as exc:  # noqa: BLE001
            ssrf_ok = False
            failures.append(
                f"a valid https JSON body was not returned — {type(exc).__name__}: {exc}"
            )
        else:
            if json.loads(got) != {"ink": "#111111", "accent": "#2563eb"}:
                ssrf_ok = False
                failures.append("the fetched body did not survive the gate unchanged")
    if ssrf_ok:
        print(
            "OK: the url= fetch is guarded by the full D-07 battery — https-only, urlsplit host "
            "parsing, resolved-IP refusal covering loopback/private/link-local/CGNAT 100.64.0.0/10 "
            "and IPv6 literals, localhost refused after resolution, the userinfo trick defeated, "
            "redirects refused unfollowed, a 5 s timeout, a 262144-byte streamed cap, and "
            f"{len(string_rows)} string row(s) each naming its own cause — all proven offline "
            "(D-07/T-5-SSRF1..7)"
        )

    # 40. D-08 (scoped): apply_brand_profile with url=None does zero network
    #     I/O — only the write's own file events appear in the audit window.
    #     Case 31's delta-slice discipline, reused: the hook is process-global
    #     and irremovable, so the window is a delta slice, the write's own
    #     `open`/`os.rename` events are expected and filtered, and the call is
    #     first proven to do real work so a denylist claim cannot green on a
    #     no-op. D-08 deliberately makes no such claim for the url= path.
    purity_ok = True
    purity_imports: list[str] = []
    real_import = builtins.__import__

    def recording_purity_import(name, globals=None, locals=None, fromlist=(), level=0):
        purity_imports.append(name)
        return real_import(name, globals, locals, fromlist, level)

    purity_file_events = ("open", "os.remove", "os.rename", "os.truncate", "os.chmod",
                          "shutil.copyfile", "mmap.__new__")
    with tempfile.TemporaryDirectory(prefix="owui-purity-") as purity_tmp:
        purity_tool = _isolated_profiles()
        purity_tool.valves.profiles_dir = purity_tmp
        sys.addaudithook(_denylist_hook)
        purity_start = len(AUDIT_HITS)
        _reset_selfpipe_licenses()
        builtins.__import__ = recording_purity_import
        try:
            purity_report = asyncio.run(
                purity_tool.apply_brand_profile(
                    '{"ink": "#111111", "accent": "#2563eb"}',
                    url=None,
                    __user__={"id": "purity-user"},
                )
            )
        finally:
            builtins.__import__ = real_import
        purity_window = AUDIT_HITS[purity_start:]
        # Fail loud: the profile must really have been written, or a no-op
        # would pass the denylist claim vacuously (case 21's framing).
        purity_path = Path(purity_tmp) / "purity-user.json"
        if not purity_path.is_file():
            purity_ok = False
            failures.append(
                "apply_brand_profile(url=None) wrote no profile file — the purity claim is "
                "unprovable without real work (D-08)"
            )
        elif "## Brand profile stored to" not in purity_report.splitlines()[0]:
            purity_ok = False
            failures.append(
                f"the audited url=None store returned {purity_report.splitlines()[0]!r} — expected "
                "the store report (BRND-01)"
            )
        purity_remaining = [h for h in purity_window if not h.startswith(purity_file_events)]
        if purity_remaining:
            purity_ok = False
            failures.append(
                f"apply_brand_profile(url=None) produced {len(purity_remaining)} audited "
                f"network/process event(s): {purity_remaining[:5]} — the no-url path must do "
                "zero network I/O (D-08/T-5-14)"
            )
        for shape in ("urllib", "socket", "getaddrinfo"):
            leaks = [h for h in purity_window if shape in h.split(":")[0]]
            if leaks:
                purity_ok = False
                failures.append(
                    f"apply_brand_profile(url=None) touched {shape} {len(leaks)} time(s) inside the "
                    f"window: {leaks[:3]} — only the url= path may reach the network (D-08)"
                )
        if len(purity_window) == 0:
            purity_ok = False
            failures.append(
                "the audit window recorded nothing at all — the write's own file events were "
                "expected, so an empty slice means the hook never saw the call (D-08)"
            )
    purity_non_stdlib = sorted(
        name for name in purity_imports
        if name.split(".")[0] not in sys.stdlib_module_names
    )
    if purity_non_stdlib:
        purity_ok = False
        failures.append(
            f"apply_brand_profile(url=None) imported non-stdlib module(s) at call time: "
            f"{purity_non_stdlib[:5]} (D-08/T-5-SC)"
        )
    if purity_ok:
        print(
            f"OK: apply_brand_profile with url=None stays pure — {len(purity_window)} file "
            f"event(s) from the write itself, 0 network-shaped event(s) (no urllib, socket or "
            f"getaddrinfo), and only stdlib call-time imports ({len(purity_imports)} name(s)), "
            "with the stored profile proven present (D-08/T-5-14/T-5-SC)"
        )

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        print(f"\n{len(failures)} case(s) failed.")
        return 1
    print("\nAll OWUI tool contract cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
