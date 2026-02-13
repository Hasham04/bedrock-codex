# Bedrock Project Standards (Claude 4.6)

These standards are the default behavior for all agent work in this repository.

## Meta-Patterns (System-Wide Habits)

The system and all agent work in this repository abide by these patterns.

**1. Systematic skepticism**  
Do not modify (or add) any control mechanism or behavior until you can explain what it controls, how it works, and what happens when you change it. Prove by tracing execution — e.g. "this seems redundant" → trace the flow and prove it before removing; "I'll add a check" → trace where the flow comes from first. Applies to all code, not just cleanup.

**2. Minimal necessary change**  
Change the smallest set of things that achieves the goal. Do not refactor unrelated code, "clean up while you're here," or add features in the same edit. If the task is "add a null check," add the null check; do not rewrite the function.

**3. State made visible**  
Do not hide important state. Surfaces, logs, or APIs should make outcomes and current state obvious so the next step (agent or human) can reason about them. "What did that call actually do?" should be answerable without digging.

**4. Fail fast, fail loud**  
Prefer explicit failure with a clear signal over silent wrong behavior or undefined state. In agentic systems: errors the agent can see and fix, rather than proceeding with bad or inconsistent state.

**5. Reversibility**  
Prefer changes that can be undone or rolled back: one logical change per step, snapshots before risky batches, clear revert semantics. Supports "try this, then revert if it's wrong" without losing the baseline.

**6. Contract before implementation**  
For interfaces, APIs, or "what this code is responsible for": define the contract (inputs, outputs, errors, invariants) before writing the implementation. Reduces rework and makes "done" and "correct" testable.

**7. Read the room**  
Before acting, confirm context: right repo, branch, file, process, and current state. Agentic version: "Where am I and what is the state?" before "What do I do next?"

**8. One level of indirection**  
Abstract when you see the same pattern at least twice, not on the first occurrence. Do not abstract speculatively; add indirection when the repetition is real and the abstraction pays for itself.

## Workflow
- For non-trivial requests, follow: Explore -> Plan -> Implement -> Verify.
- For simple one-file changes, implement directly, but still verify.
- If requirements are ambiguous and would change implementation, ask a focused clarifying question.

## Planning Quality
- Plans must include: Why, Approach, Affected Files, Checklist, Steps, Verification.
- Multi-part requests must produce multi-item checklists and multiple actionable steps.
- Every step must be concrete: file path + symbol/function + exact change.
- Do not emit vague placeholder todos (for example, "let me check X").

## Verification-First Execution
- Always define "done" with concrete checks (tests/lint/build/expected output).
- After edits: re-read changed sections, run lint/type checks, then relevant tests.
- Prefer root-cause fixes over suppressions and shortcuts.
- If verification fails, keep iterating until checks pass or blockers are explicit.

## Large Codebase Navigation
- Search before reading large files.
- Read targeted sections with offset/limit instead of full-file scans where possible.
- Batch independent reads/searches in parallel.
- Reuse existing patterns and utilities before introducing new abstractions.

## Reasoning Transparency
- Use deep internal thinking for complex tasks.
- Provide a visible reasoning trace after meaningful actions:
  1) What was learned
  2) Why it matters
  3) Decision made
  4) Next actions
  5) Verification status
- Keep traces evidence-based (file/symbol/command/result), not vague narration.
- Final completion messages after tool-driven work must include structured trace headings.

## Context Discipline
- Keep active context focused on relevant files and current objective.
- Summarize older work when sessions are long.
- Preserve key state artifacts (modified files, test commands, decisions).

## Claude 4.6 Thinking Defaults
- Prefer adaptive thinking on Claude 4.6 (`USE_ADAPTIVE_THINKING=true`).
- Use high effort by default (`ADAPTIVE_THINKING_EFFORT=high`) for complex engineering tasks.
- If provider compatibility issues occur, disable adaptive and fall back to fixed budget thinking.

## Deterministic Verification Gate
- Before final completion, run deterministic checks on modified files:
  - per-file lint/type checks
  - targeted tests when discoverable
- If gate fails, fix issues first; do not finalize early.

## Verification Orchestrator
- Use language/framework-aware verification commands in addition to per-file lint.
- Python: py_compile + ruff/flake8 + targeted pytest when tests are discoverable.
- TS/JS: tsc (when tsconfig exists) + eslint on modified files.
- Rust/Go: run project-appropriate test commands when those file types are modified.

## Policy Engine
- Evaluate every risky operation with policy checks.
- Block destructive commands by default (unless policy is explicitly relaxed).
- Require explicit approval for sensitive file writes and shared-impact commands.

## Symbol-Aware Navigation
- Prefer symbol-level search (`find_symbol`) over broad text search for ambiguous edits.
- Resolve definitions/references before editing high-fanout identifiers.

## Task Decomposition
- Decompose plan steps into execution batches (file work vs command/verification work).
- Use decomposition metadata to execute and report progress deterministically.

## Human Review Mode
- When review mode is enabled, require explicit approval before build execution starts.
- Present task + plan decomposition in the review prompt.

## Learning Loop
- Persist recurring failure signatures.
- Surface top known failure patterns in system context to prevent repeat mistakes.

## Safety and Reversibility
- Prefer local reversible actions.
- Ask before hard-to-reverse or shared-impact actions (destructive operations, force pushes, dropping data).
- Do not bypass safety controls as a shortcut.

