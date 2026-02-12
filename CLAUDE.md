# Bedrock Project Standards (Claude 4.6)

These standards are the default behavior for all agent work in this repository.

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

