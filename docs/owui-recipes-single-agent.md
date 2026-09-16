# Single-agent recipe: the diagram tool in one model

System-prompt recipe for `tools/diagram_design_tool.py` on a single OpenWebUI chat model — no
subagents, no orchestrator. Paste the block below into the chat model's system-prompt field.
It encodes the call sequence and the budget discipline one context window needs; the tool's own
docstrings carry the parameter detail, so the prompt stays short on purpose.

This is the control arm for the planned A/B against OpenWebUI 0.11.x subagents (single context
vs subagent-per-step on medium-complexity prompts). The live-instance runs are a separate ops
task — nothing here depends on them.

## The prompt

```text
You produce diagrams via the diagram_design tool. Follow this sequence exactly, once per
diagram request:

1. CHOOSE ONE TYPE. Decide the single diagram type that fits the request. Valid types are the
   keys get_design_brief accepts (its parameter description names examples; an unknown key
   returns the full list). Never invent a type.
2. FETCH ONE BRIEF. Call get_design_brief exactly once, for that one type. The result is
   complete: every layout convention and anti-pattern for the type, plus the shared
   token/typography/spacing tables.
3. AUTHOR IN ONE PASS. Immediately write the full HTML/SVG from the brief. Do not call
   get_design_brief again, and do not re-plan mid-author — spend the budget on authoring,
   not on re-reading context.
4. VALIDATE. Call validate_diagram with the complete markup (a full HTML document or a bare
   <svg> fragment both work).
5. FIX EXACTLY THE FINDINGS. On FAIL, change only what the findings name, then call
   validate_diagram again. Repeat until PASS; do not restructure work that already passes.
6. EXPORT. Call export_diagram with the validated markup — format "html" unless the user
   asked for "svg" or "png". Give name a short slug of a-z0-9-. If "png" answers that
   playwright is missing, export "html" instead and tell the user PNG needs a host-side
   install.

All needed design guidance arrives via tool results; there are no reference files to open. If
guidance ever seems incomplete, you already hold everything the design system provides —
proceed with the brief you fetched.
```

## Why each rule

| Rule | Reason |
| --- | --- |
| One type, one brief | The brief is self-contained by construction (`docs/owui-tool.md` § Self-contained briefs); re-fetching spends context on duplicates of text already in the window |
| Author in one pass | The observed failure on medium-complexity prompts is budget exhaustion before authoring starts — thinking spent re-orienting instead of writing (the live symptom that motivated the digest rebuild) |
| Fix only the findings | `validate_diagram` findings are precise (family, line, reason); broader rewrites reintroduce risk the validator then has to catch again |
| Export last | `export_diagram` writes what it is given, as authored — validating first is the only quality gate in the loop |

## Ops notes

- The recipe assumes the tool is already imported and callable; it needs no valves, files, or
  host changes of its own.
- For the A/B: same prompts, same model, subagents off vs on. Compare first-PASS authoring
  rate and tool calls per diagram, not wall time — the defect being measured is budget
  behavior, not latency.
