# Codebase Audit — Bugs & Edge Cases

**Date:** 2025-02 (session audit)  
**Scope:** agent.py, web.py, tools.py, backend.py, sessions.py, codebase_index.py, bedrock_service.py, config.py, static (app.js, style.css).

**Status:** Fixes applied (see below). Each item notes **[FIXED]** when addressed.

---

## High / Must-fix

### 1. `generate_diffs()` assumes local filesystem (SSH broken) **[FIXED]**
**Where:** `web.py` — `generate_diffs()`  
**Issue:** Uses `agent.modified_files` (keys are from `backend.resolve_path()`). For **SSH**, those keys are remote absolute paths (e.g. `/home/user/proj/file.py`). The code then does `open(abs_path, "r")` and `os.path.relpath(abs_path, wd)` — i.e. it treats paths as **local**. On an SSH project, `abs_path` is not on the server’s disk; `wd` can be the composite `user@host:port:dir`. So:
- `open(abs_path)` may open a **different** local project or fail.
- `os.path.relpath(remote_abs_path, composite_wd)` is meaningless.

**Impact:** Reconnect with “awaiting_keep_revert” (e.g. after a run with uncommitted changes) calls `generate_diffs()` for `replay_state`. For SSH projects the diff list and “Open file” can be wrong or “file not found” (you already saw this and removed the **final** diff at end of run; the **replay_state** path still uses `generate_diffs()`).

**Fix:** Either:
- Skip or stub `generate_diffs()` when backend is SSH (e.g. `if getattr(agent.backend, "_host", None) is not None: return []`), or
- Implement diff generation via backend (e.g. `backend.read_file` for “current” and use snapshot content for “original”) so it works for both local and SSH.

---

### 2. Agent `working_directory` with SSH
**Where:** `agent.py` — `CodingAgent.__init__`  
**Issue:** `self.working_directory = os.path.abspath(working_directory)`. For **local** projects this is correct. For **SSH**, `web.py` passes `agent_wd = _ssh_info["directory"]` (the remote path, e.g. `/home/x/proj`). Then `os.path.abspath("/home/x/proj")` is that same string. On the **web server** that path might point to a **local** folder (another project), so:
- `_file_cache` and dedup keys in the agent use `os.path.abspath(os.path.join(self.working_directory, path))`, which are then local-style paths that can **collide** with another project on the same machine.
- Logic that assumes “path on disk” (e.g. cache invalidation) is tied to a path that might not be the one the backend is actually using for SSH.

**Impact:** Cross-project cache confusion when the same path exists locally and remotely; possible wrong cache hits.

**Fix:** For SSH, avoid using `os.path.abspath(working_directory)` and keep `working_directory` as the remote path string; use a cache key that includes a “backend id” (e.g. `ssh:user@host:dir`) so local and SSH never share keys.

---

### 3. Path traversal / validation
**Where:** `web.py` — `/api/file` (read), `/api/file` (write), and other path-taking endpoints  
**Issue:** Read path rejects `".."` and `path.startswith("/")` (good). Write path comes from JSON body and is passed to `backend.write_file(rel_path, content)` without checking for `..` or absolute paths in the same way.

**Where:** `tools.py` — file tools  
**Issue:** Paths come from the model; backend’s `resolve_path` joins with `working_directory`. If the model sends `../../etc/passwd`, `resolve_path` can escape the project. LocalBackend uses `os.path.normpath(os.path.join(...))`, which can still escape above the working dir.

**Fix applied:** Both backends implement `_ensure_under_working(resolved)` and call it in read_file, write_file, remove_file, list_dir, file_exists, is_dir, is_file, file_size. Local: real path must be under wd; SSH: remote path must be under working_directory (POSIX). Web write_file also rejects `..` and leading `/`.

---

### 4. `run_command` uses `shell=True` with raw command **[FIXED]**
**Where:** `backend.py` — `LocalBackend.run_command` (and stream variant)  
**Issue:** `subprocess.Popen(command, shell=True, ...)` runs the user/model-provided string in the shell. So `$(...)`, backticks, `;`, `|` etc. are executed. Policy engine can block some commands but not arbitrary shell metacharacters.

**Impact:** If the model is tricked or hallucinates a malicious command, arbitrary code can run in the project’s cwd.

**Fix:** Either run without shell (`shell=False`, list of args) for a restricted set of “allowed” commands, or strictly validate/sanitize the command string (e.g. no `$`, backticks, `;`, `|`, `&&`, `>` to unrelated paths). SSH side uses `_exec(cmd)` which is also full shell.

---

## Medium / Should-fix

### 5. `revert_all` passes `abs_path` to backend methods that may expect relative
**Where:** `agent.py` — `revert_all()`  
**Issue:** Snapshot keys are from `backend.resolve_path(rel_path)` (so for LocalBackend they’re absolute; for SSHBackend they’re the same string). We then call `backend.file_exists(abs_path)`, `backend.remove_file(abs_path)`, `backend.write_file(abs_path, original)`. LocalBackend and SSHBackend treat absolute paths in `resolve_path` / `_remote_path` as “return as-is”, so behavior is consistent. **Edge case:** If a backend ever expected only relative paths, this would break. Worth a short comment in code that snapshot keys are in backend’s “resolved” form.

---

### 6. Session `_read_file` — no schema validation **[FIXED]**
**Where:** `sessions.py` — `_read_file()`  
**Issue:** Loads JSON and builds `Session` from `data.get(...)`. Corrupted or old JSON (e.g. missing keys, wrong types) can produce a half-valid session (e.g. `history` not a list, `extra_state` not a dict). Later code may assume types and crash or misbehave.

**Fix applied:** _read_file validates root is dict; history, token_usage, extra_state type-checked and defaulted; scalar fields coerced to str/int.

---

### 7. Codebase index — SSH backend skipped but no explicit contract **[FIXED]**
**Where:** `codebase_index.py` and callers (e.g. `tools.semantic_retrieve`)  
**Issue:** Index build is skipped when backend has `_host` (SSH). That’s correct, but `retrieve()` can still be called; it returns `[]` if no chunks/embed_fn. So semantic_retrieve on SSH just returns “No relevant chunks” without explaining that indexing isn’t supported for remote projects. Minor UX/confusion.

**Fix:** Document that semantic_retrieve is local-only; optionally return a single “Indexing is not available for SSH projects” chunk when backend is SSH.

---

### 8. `POST /api/projects/remove` — path normalization vs. list_all_projects
**Where:** `web.py` — `remove_project`; `sessions.py` — `list_all_projects`, `delete_all_sessions_for_project`  
**Issue:** Frontend sends `p.path` from the project list. Backend uses `_normalize_wd(path)`. If the client ever sent a path in a different form (e.g. with trailing slash, or different casing on Windows), it might not match the session files’ keys and “deleted: 0” could occur. Low risk if the only caller is the welcome list.

**Fix applied:** `_normalize_wd` now strips input and for SSH strips trailing slash from directory part so "user@host:port:/home/x/proj/" matches stored sessions.

---

### 9. Replay / restore — `extra_state` keys and agent `from_dict` **[FIXED]**
**Where:** `web.py` — restore flow; `agent.py` — `from_dict`  
**Issue:** `extra_state` holds both UI state and agent state. If a key is added to `extra_state` in `save_session` but not to `restore_data` (e.g. typo or rename), that field is lost on reload. Similarly, if `agent.from_dict` doesn’t handle a key, it’s ignored. No schema or version check, so old sessions can drift.

**Fix applied:** from_dict logs unknown keys (logger.debug) and only restores known keys; docstring updated.

---

## Low / Edge cases

### 10. `_cap_tool_results` — capped text can still exceed cap **[FIXED]**
**Where:** `agent.py` — `_cap_tool_results()`  
**Issue:** When `len(lines) > 50`, we build `head + "... N lines omitted ..." + tail`. The total length is not forced to `<= cap`; it can exceed it slightly. Minor context bloat.

**Fix applied:** After building head+tail or truncated text, `if len(text) > cap: text = text[:cap] + "..."` before appending.

---

### 11. Scout / plan — `run_plan` lambda capture
**Where:** `agent.py` — scout loop and similar  
**Issue:** `lambda: self.service.generate_response(...)` captures `self` and args. No bug found, but any `lambda _tu=tu`-style capture in loops is correct; other lambdas in the same file were checked and are safe.

---

### 12. Empty or whitespace-only tool input
**Where:** `tools.py` — e.g. `read_file(path="")`, `edit_file(path="  ")`  
**Issue:** Some tools strip path; others may not. `resolve_path("")` can return `working_directory` (directory), and reading “a directory” can fail or behave oddly. Backend may or may not reject.

**Fix applied:** `_require_path(path)` in read_file, write_file, edit_file, symbol_edit, lint_file; run_command rejects empty command.

---

### 13. Config — unknown model fallback **[FIXED]**
**Where:** `config.py` — `get_model_config(model_id)`  
**Issue:** For an unknown model ID, returns a fallback dict. If the fallback is missing a key that new code expects (e.g. `max_output_tokens`), callers get a default from `.get(key, default)`. So behavior is safe but may be inconsistent across models.

**Fix applied:** get_model_config docstring documents fallback and recommends `.get(key, sensible_default)`.

---

### 14. Frontend — `openFile` / diff file path
**Where:** `static/app.js` — diff view and file open  
**Issue:** You previously saw “file not found” when opening from the (now-removed) final diff. For replay_state diff, the path sent to `openFile` is still from `generate_diffs()`; for SSH that path is wrong (see #1). So “remove from recents” and “open file” from any remaining diff UI on SSH remain at risk until #1 is fixed.

---

## Summary

| Severity | Count | Main areas |
|----------|-------|------------|
| High     | 4     | generate_diffs SSH, agent wd/cache for SSH, path traversal, shell=True in run_command |
| Medium   | 5     | revert_all path contract, session schema, codebase index SSH, projects/remove path, replay extra_state |
| Low      | 5     | cap overflow, lambda captures, empty path, config fallback, frontend path from diff |

**Recommended order of work:** Fix path traversal and shell injection (security), then generate_diffs/SSH and agent SSH working_directory/cache, then session validation and the rest as needed.
