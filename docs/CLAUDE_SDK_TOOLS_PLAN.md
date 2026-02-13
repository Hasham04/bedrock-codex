# Plan: Adopting More Claude SDK–Style Tools

We already use **TodoWrite** (and **AskUserQuestion**). This plan lists other SDK-style tools we can adopt and a suggested order.

---

## 1. TodoRead (high value, low effort)

**What it is (SDK):** Model calls `TodoRead` to get the current todo list (e.g. before planning the next step).

**Our case:** We have `agent._todos` but no tool for the model to “read” them. Today the list is only in context when we inject `<current_todos>` in the prompt.

**Plan:**
- Add a **TodoRead** tool: no input (or optional empty object). Handler returns the current `self._todos` as JSON/text.
- When the model calls TodoRead, we return the same list we already store (id, content, status).
- **Effort:** Small. One new tool definition + handle it in `_execute_tools_parallel` (like TodoWrite; no approval).

**Why do it:** Matches SDK behavior, lets the model explicitly refresh the list and avoid drift.

---

## 2. Name alignment: Read, Write, Edit, Bash, Glob (medium value, medium effort)

**What it is (SDK):** Claude Code uses **Read**, **Write**, **Edit**, **Bash**, **Glob** (and we use read_file, write_file, edit_file, run_command, glob_find).

**Plan (optional):**
- **Option A – Names only:** Rename tools to Read, Write, Edit, Bash, Glob. Keep our schemas (path, offset, limit for Read; command/timeout for Bash; etc.). Update agent prompts and any UI labels. Model sees the same names as in the SDK.
- **Option B – Names + schemas:** Also align input_schema with Anthropic’s published schemas for these tools (if we have them). Bigger change and may drop things we care about (e.g. offset/limit on Read).

**Recommendation:** Option A only, and only if you want maximum naming consistency with the SDK. Our behavior (approvals, timeouts, line numbers, etc.) stays as-is.

---

## 3. Memory tool (medium value, higher effort)

**What it is (SDK):** A **Memory** tool for storing and retrieving key facts across the conversation (e.g. “user prefers TypeScript”, “API base URL is X”).

**Our case:** We don’t have this. Context is only in the conversation history and injected blocks (e.g. current_todos).

**Plan:**
- Add **Memory** (or **MemoryWrite** / **MemoryRead**): store key–value or structured snippets; return them on read (e.g. “give me everything” or “key X”).
- Persist in agent state (e.g. `agent._memory: Dict[str, Any]`) and include in checkpoints/session so it survives refresh.
- **Effort:** Medium. New tool(s), schema, handler, and where to surface in UI (optional).

**Why do it:** Reduces repeated questions and keeps preferences/facts explicit.

---

## 4. Built-in Bedrock tools: Bash, Text editor (lower priority)

**What it is:** Bedrock supports built-in **bash** and **text_editor** (with `anthropic_beta`). The model uses a fixed schema; we run the command or apply the edit.

**Our case:** We already have run_command and read_file/edit_file/write_file with approval, timeouts, and streaming.

**Plan:**
- Only consider if we want **stateful bash** (one persistent shell) or a **single “text editor”** tool instead of read + edit + write.
- Requires beta header, executor that matches their I/O, and possibly replacing or complementing our current tools.

**Recommendation:** Defer. Our custom tools give us control and safety; adopt built-ins only if we specifically want stateful bash or a single editor contract.

---

## 5. Other SDK tools (Web fetch, Web search, Tool search)

- **Web fetch / Web search:** Useful for “look up latest docs” or “search the web”. We don’t have these; add only if the product needs them.
- **Tool search:** Model searches over tool definitions. More relevant when there are many tools or MCPs. Optional later.

---

## Suggested order

| Priority | Tool(s)        | Reason |
|----------|----------------|--------|
| 1        | **TodoRead**   | Pairs with TodoWrite, small change, clear behavior. |
| 2        | **Memory**     | High product value (preferences, facts); medium implementation. |
| 3        | **Read/Write/Edit/Bash/Glob** (names only) | Only if we want full SDK name alignment. |
| 4        | Web fetch / Web search | Only if we add “look things up” as a feature. |
| 5        | Bedrock built-in bash/text_editor | Defer unless we want stateful bash or unified editor. |

---

## Next step

Implement **TodoRead** first (add tool definition + handler that returns `agent._todos`). Then decide whether to add **Memory** and/or name alignment for Read/Write/Edit/Bash/Glob.
