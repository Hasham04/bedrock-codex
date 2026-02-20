/* ============================================================
   Bedrock Codex — Chat Messages, Tool UI, Plan, Diffs, Phases
   ============================================================ */
(function (BX) {
    "use strict";

    // DOM ref aliases
    var $chatMessages = BX.$chatMessages;
    var $conversationTitle = BX.$conversationTitle;
    var $actionBar = BX.$actionBar;
    var $actionBtns = BX.$actionBtns;
    var $stickyTodoBar = BX.$stickyTodoBar;
    var $stickyTodoCount = BX.$stickyTodoCount;
    var $stickyTodoList = BX.$stickyTodoList;
    var $stickyTodoDropdown = BX.$stickyTodoDropdown;
    var $stickyTodoAddInput = BX.$stickyTodoAddInput;
    var $stickyTodoAddBtn = BX.$stickyTodoAddBtn;
    var $stickyTodoToggle = BX.$stickyTodoToggle;
    var $input = BX.$input;
    var $statusStrip = BX.$statusStrip;
    var $stickyTodoAddRow = document.getElementById("sticky-todo-add-row");

    // Reference-type aliases
    var toolRunById = BX.toolRunById;
    var modifiedFiles = BX.modifiedFiles;
    var pendingImages = BX.pendingImages;
    var openTabs = BX.openTabs;
    var fileChangesThisSession = BX.fileChangesThisSession;
    var sessionCumulativeStats = BX.sessionCumulativeStats;

    function addUserMessage(text, images) {
        var div = document.createElement("div"); div.className = "message user";
        var bubble = document.createElement("div"); bubble.className = "msg-bubble";
        var safeText = String(text || "");
        var imgs = Array.isArray(images) ? images : [];
        if (safeText) {
            var textEl = document.createElement("div");
            textEl.className = "user-message-text";
            textEl.textContent = safeText;
            bubble.appendChild(textEl);
        }
        if (imgs.length) {
            var grid = document.createElement("div");
            grid.className = "user-images-grid";
            imgs.forEach(function(img) {
                var src = BX.imageSrcForMessage(img);
                if (!src) return;
                var tile = document.createElement("div");
                tile.className = "user-image-tile";
                tile.innerHTML = `<img src="${BX.escapeHtml(src)}" alt="${BX.escapeHtml(img.name || "image")}">`;
                grid.appendChild(tile);
            });
            if (grid.children.length > 0) bubble.appendChild(grid);
        }
        var copyPayload = safeText || `[${imgs.length} image attachment${imgs.length === 1 ? "" : "s"}]`;
        bubble.appendChild(BX.makeCopyBtn(copyPayload));
        div.appendChild(bubble);
        var timeEl = document.createElement("span");
        timeEl.className = "msg-timestamp";
        timeEl.textContent = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        div.appendChild(timeEl);
        $chatMessages.appendChild(div);
        if (!BX.sessionStartTime) {
            BX.sessionStartTime = Date.now();
            BX.updateFileChangesDropdown();
        }
        if ($conversationTitle && $chatMessages.querySelectorAll(".message.user").length === 1) {
            var truncated = safeText.trim().slice(0, 50);
            $conversationTitle.textContent = truncated ? (truncated + (safeText.length > 50 ? "\u2026" : "")) : "New conversation";
        }
        BX.scrollChat();
    }

    function addGuidanceMessage(text) {
        var div = document.createElement("div");
        div.className = "message user guidance-msg";
        div.setAttribute("role", "status");
        div.setAttribute("aria-label", "Guidance message: " + text);
        var bubble = document.createElement("div");
        bubble.className = "msg-bubble guidance-bubble";
        var label = document.createElement("span");
        label.className = "guidance-label";
        label.setAttribute("aria-hidden", "true");
        label.textContent = "Guidance";
        bubble.appendChild(label);
        var textEl = document.createElement("div");
        textEl.className = "user-message-text";
        textEl.textContent = text;
        bubble.appendChild(textEl);
        div.appendChild(bubble);
        $chatMessages.appendChild(div);
        BX.scrollChat();
    }

    function addAssistantMessage() {
        var div = document.createElement("div"); div.className = "message assistant";
        var bubble = document.createElement("div"); bubble.className = "msg-bubble";
        div.appendChild(bubble);
        $chatMessages.appendChild(div);
        BX.scrollChat();
        return bubble;
    }

    function getOrCreateBubble() {
        var last = $chatMessages.querySelector(".message.assistant:last-child .msg-bubble");
        return last || addAssistantMessage();
    }

    // Thinking blocks
    var _thinkingBuffer = ""; // accumulates raw thinking text for markdown rendering
    var _thinkingRenderTimer = null; // debounce timer for markdown render
    var _thinkingUserCollapsed = false; // track if user manually collapsed during stream
    var _thinkingTickInterval = null; // 1s interval to update the reasoning timer in real time

    function updateThinkingHeader(block, done = false) {
        if (!block) return;
        var started = Number(block.dataset.startedAt || Date.now());
        var elapsed = Math.max(0, Math.round((Date.now() - started) / 1000));
        var titleEl = block.querySelector(".thinking-title");
        if (titleEl) {
            if (done) {
                titleEl.textContent = elapsed > 0 ? `Reasoned for ${elapsed}s` : "Reasoning";
            } else {
                titleEl.textContent = elapsed > 0 ? `Reasoning\u2026 ${elapsed}s` : "Reasoning\u2026";
            }
        }
        if (done) {
            block.classList.remove("thinking-active");
        } else {
            block.classList.add("thinking-active");
        }
    }

    function _startThinkingTick(block) {
        _stopThinkingTick();
        _thinkingTickInterval = setInterval(() => updateThinkingHeader(block, false), 1000);
    }
    function _stopThinkingTick() {
        if (_thinkingTickInterval) { clearInterval(_thinkingTickInterval); _thinkingTickInterval = null; }
    }

    function createThinkingBlock() {
        var bubble = getOrCreateBubble();
        var block = document.createElement("div"); block.className = "thinking-block thinking-active";
        block.dataset.startedAt = String(Date.now());
        _thinkingBuffer = "";
        _thinkingUserCollapsed = false;
        block.innerHTML = `
            <div class="thinking-header">
                <div class="thinking-left">
                    <span class="thinking-icon-wrap">
                        <svg class="thinking-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M12 3a6 6 0 0 0-6 6c0 2.5 1.3 4 2.5 5.1.8.8 1.5 1.4 1.5 2.4h4c0-1 .7-1.6 1.5-2.4C16.7 13 18 11.5 18 9a6 6 0 0 0-6-6z"/>
                            <path d="M9 19h6"/><path d="M10 22h4"/>
                        </svg>
                    </span>
                    <span class="thinking-title">Reasoning\u2026</span>
                </div>
                <div class="thinking-right">
                    <span class="thinking-spinner spinner"></span>
                    <span class="thinking-chevron">\u25BC</span>
                </div>
            </div>
            <div class="thinking-content"></div>`;
        block.querySelector(".thinking-header").addEventListener("click", function() {
            block.classList.toggle("collapsed");
            if (block.classList.contains("thinking-active")) {
                _thinkingUserCollapsed = block.classList.contains("collapsed");
            }
        });
        bubble.appendChild(block);
        updateThinkingHeader(block, false);
        _startThinkingTick(block);
        BX.scrollChat();
        return block.querySelector(".thinking-content");
    }

    function _renderThinkingContent(el) {
        if (!el || !_thinkingBuffer) return;
        if (typeof marked !== "undefined") {
            el.innerHTML = marked.parse(_thinkingBuffer);
            el.querySelectorAll("pre code").forEach(function(b) {
                if (typeof hljs !== "undefined") try { hljs.highlightElement(b); } catch (_) {}
            });
        } else {
            el.textContent = _thinkingBuffer;
        }
        // Auto-scroll to bottom of thinking content
        el.scrollTop = el.scrollHeight;
    }

    function appendThinkingContent(el, delta) {
        if (!el) return;
        _thinkingBuffer += delta;
        // Debounce markdown rendering (every 80ms) to avoid jank during fast streaming
        if (_thinkingRenderTimer) clearTimeout(_thinkingRenderTimer);
        _thinkingRenderTimer = setTimeout(function() { _renderThinkingContent(el); }, 80);
        var block = el.closest(".thinking-block");
        if (block) updateThinkingHeader(block, false);
        BX.scrollChat();
    }

    function finishThinking(el) {
        if (!el) return;
        _stopThinkingTick();
        if (_thinkingRenderTimer) { clearTimeout(_thinkingRenderTimer); _thinkingRenderTimer = null; }
        // Final render with full content
        _renderThinkingContent(el);
        var block = el.closest(".thinking-block"); if (!block) return;
        updateThinkingHeader(block, true);
        var spinner = block.querySelector(".thinking-spinner"); if (spinner) spinner.remove();
        // Thinking panels always stay expanded (user can manually collapse via header click)
        var header = block.querySelector(".thinking-header");
        if (header && !header.querySelector(".copy-btn")) {
            header.appendChild(BX.makeCopyBtn(() => _thinkingBuffer || el.textContent));
        }
        _thinkingBuffer = "";
        _thinkingUserCollapsed = false;
    }

    // Tool blocks
    var lastToolGroup = null;
    var toolRunState = new WeakMap(); // runEl -> { name, input, output }

    function formatClock(ts) {
        return new Date(ts).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }
    function toolGroupKey(name, input) { return `${name}::${toolDesc(name, input)}`; }
    function toolActionIcon(kind) {
        var icons = {
            done: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            pending: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="2" fill="none"/></svg>`,
            failed: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>`,
            open: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            rerun: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            retry: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m1 4 4 4 4-4M23 20l-4-4-4 4M20 8a8 8 0 0 0-13-3M4 16a8 8 0 0 0 13 3" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            copy: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2" stroke="currentColor" stroke-width="2" fill="none"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>`,
            stop: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5" ry="1.5" stroke="currentColor" stroke-width="2" fill="none"/></svg>`,
            pause: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1" stroke="currentColor" stroke-width="2" fill="none"/><rect x="14" y="5" width="4" height="14" rx="1" stroke="currentColor" stroke-width="2" fill="none"/></svg>`,
            play: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7L8 5z" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>`,
            more: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="6" r="1.5" fill="currentColor"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/><circle cx="12" cy="18" r="1.5" fill="currentColor"/></svg>`,
            revert: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 10h10a5 5 0 0 1 5 5v0M3 10l4-4M3 10l4 4" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            plus: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            pen: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 11l6 6 2-2-6-6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M15 5l4 4" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
        };
        return icons[kind] || "";
    }
    function toolCanOpenFile(name, input) {
        return Boolean((name === "Read" || name === "Write" || name === "Edit" || name === "symbol_edit" || name === "lint_file") && input?.path);
    }
    function toolFollowupPrompt(name, input, failedOnly = false) {
        var title = toolTitle(name);
        if (name === "Bash" && input?.command) {
            return failedOnly
                ? `The command failed. Please rerun this command and fix the issue:\n\n${input.command}`
                : `Please rerun this exact command and summarize the result:\n\n${input.command}`;
        }
        if (failedOnly) return `The ${title} tool call failed. Retry it and fix the issue. Input:\n${JSON.stringify(input || {}, null, 2)}`;
        return `Please rerun this ${title} tool call with the same input:\n${JSON.stringify(input || {}, null, 2)}`;
    }
    function runFollowupPrompt(prompt) {
        if (!prompt) return;
        if (BX.isRunning) { showInfo("Agent is running. Cancel first to rerun."); return; }
        addUserMessage(prompt);
        BX.setRunning(true);
        addAssistantMessage();
        var editorCtx = BX.gatherEditorContext();
        BX.send({ type: "task", content: prompt, ...(editorCtx ? { context: editorCtx } : {}) });
    }
    function openFileAt(path, lineNumber) {
        if (!path) return;
        BX.openFile(path).then(function() {
            if (BX.monacoInstance && lineNumber) {
                var ln = Math.max(1, Number(lineNumber) || 1);
                BX.monacoInstance.setPosition({ lineNumber: ln, column: 1 });
                BX.monacoInstance.revealLineInCenter(ln);
                BX.monacoInstance.focus();
            }
        }).catch(function() {});
    }
    function parseLocationLine(line) {
        var m = String(line || "").trim().match(/^(.+?):(\d+):(?:(\d+):)?(.*)$/);
        if (!m) return null;
        return { path: m[1], line: Number(m[2]), col: m[3] ? Number(m[3]) : null, text: (m[4] || "").trim() };
    }
    function buildEditPreviewDiff(name, input) {
        var path = input?.path || "(unknown)";
        var MAX_PREVIEW = 80;
        if (name === "Write") {
            var lines = String(input?.file_text || input?.content || "").split("\n");
            var show = lines.slice(0, MAX_PREVIEW);
            var added = show.map(l => `+${l}`);
            if (lines.length > MAX_PREVIEW) added.push(`+... (${lines.length - MAX_PREVIEW} more lines)`);
            return `+++ ${path}\n@@ new file @@\n${added.join("\n")}`;
        }
        if (name === "Edit") {
            var oldLines = String(input?.old_str || input?.old_string || "").split("\n");
            var newLines = String(input?.new_str || input?.new_string || "").split("\n");
            var removed = oldLines.slice(0, MAX_PREVIEW).map(l => `-${l}`);
            var added = newLines.slice(0, MAX_PREVIEW).map(l => `+${l}`);
            if (oldLines.length > MAX_PREVIEW) removed.push(`-... (${oldLines.length - MAX_PREVIEW} more lines)`);
            if (newLines.length > MAX_PREVIEW) added.push(`+... (${newLines.length - MAX_PREVIEW} more lines)`);
            return `--- ${path}\n+++ ${path}\n@@ edit @@\n${removed.join("\n")}\n${added.join("\n")}`;
        }
        if (name === "symbol_edit") {
            var symbol = input?.symbol || "(symbol)";
            var newLines = String(input?.new_string || "").split("\n");
            var added = newLines.slice(0, MAX_PREVIEW).map(l => `+${l}`);
            if (newLines.length > MAX_PREVIEW) added.push(`+... (${newLines.length - MAX_PREVIEW} more lines)`);
            return `--- ${path}\n+++ ${path}\n@@ symbol ${symbol} (${input?.kind || "all"}) @@\n${added.join("\n")}`;
        }
        return "";
    }
    function failureSummary(name, outputText) {
        var txt = String(outputText || "");
        var issue = "", next = "";
        var mMissing = txt.match(/File not found:\s*([^\n]+)/i);
        var mOldMiss = txt.match(/old_string not found/i);
        var mMultiple = txt.match(/Found\s+(\d+)\s+occurrences\s+of\s+old_string/i);
        var mTimeout = txt.match(/timed out/i);
        var mExit = txt.match(/\[exit code:\s*(-?\d+)\]/i) || txt.match(/exited with code\s+(-?\d+)/i);
        var mPerm = txt.match(/permission denied/i);
        if (mMissing) {
            issue = `Target file does not exist: ${mMissing[1].trim()}.`;
            next = "Verify the path and create the file or correct the path before retrying.";
        } else if (mOldMiss) {
            issue = "Edit anchor was not found in the file.";
            next = "Use more surrounding context or re-read the file to update the exact snippet.";
        } else if (mMultiple) {
            issue = `Edit anchor matched multiple locations (${mMultiple[1]} occurrences).`;
            next = "Narrow the edit target with unique context lines.";
        } else if (mTimeout) {
            issue = "Tool operation timed out.";
            next = "Retry with a narrower scope or increase timeout.";
        } else if (mPerm) {
            issue = "Permission denied while running the tool.";
            next = "Check file/command permissions and retry.";
        } else if (mExit) {
            issue = `${toolTitle(name)} failed with exit code ${mExit[1]}.`;
            next = "Inspect the output details and rerun after fixing the reported error.";
        }
        if (!issue) return null;
        return { issue, next };
    }
    function makeProgressiveBody(text, className = "", maxChars = 2400) {
        var wrap = document.createElement("div");
        var pre = document.createElement("pre");
        pre.className = `tool-result-body ${className}`.trim();
        var full = String(text || "(no output)");
        var short = full.slice(0, maxChars);
        pre.textContent = full.length > maxChars ? `${short}\n…` : full;
        wrap.appendChild(pre);
        if (full.length > maxChars) {
            var btn = document.createElement("button");
            btn.className = "tool-show-more-btn";
            btn.textContent = "Show more";
            var expanded = false;
            btn.addEventListener("click", function(e) {
                e.stopPropagation();
                expanded = !expanded;
                pre.textContent = expanded ? full : `${short}\n…`;
                btn.textContent = expanded ? "Show less" : "Show more";
            });
            wrap.appendChild(btn);
        }
        return wrap;
    }

    function formatToolOutputBody(content) {
        var str = String(content || "").trim();
        if (!str) return makeProgressiveBody("(no output)", "tool-result-body");
        try {
            var parsed = JSON.parse(str);
            if (parsed && typeof parsed === "object") {
                return renderStructuredOutput(parsed);
            }
        } catch (_) {}
        if (str.length > 4000) return makeProgressiveBody(str, "", 4000);
        var wrap = document.createElement("div");
        var pre = document.createElement("pre");
        pre.className = "tool-result-body";
        pre.textContent = str;
        wrap.appendChild(pre);
        return wrap;
    }
    function renderStructuredOutput(obj) {
        var wrap = document.createElement("div");
        wrap.className = "tool-structured-output";
        if (Array.isArray(obj)) {
            if (obj.length === 0) {
                wrap.innerHTML = `<span class="tool-output-empty">No results</span>`;
                return wrap;
            }
            var isStringArray = obj.every(v => typeof v === "string");
            if (isStringArray && obj.length <= 50) {
                var list = document.createElement("div");
                list.className = "tool-output-list";
                obj.forEach(function(item) {
                    var row = document.createElement("div");
                    row.className = "tool-output-list-item";
                    row.textContent = item;
                    list.appendChild(row);
                });
                wrap.appendChild(list);
                return wrap;
            }
            obj.slice(0, 30).forEach(function(item, i) {
                if (typeof item === "object" && item !== null) {
                    var card = document.createElement("div");
                    card.className = "tool-output-card";
                    Object.entries(item).forEach(([k, v]) => {
                        var row = document.createElement("div");
                        row.className = "tool-output-row";
                        var key = document.createElement("span");
                        key.className = "tool-output-key";
                        key.textContent = k;
                        var val = document.createElement("span");
                        val.className = "tool-output-val";
                        val.textContent = typeof v === "object" ? JSON.stringify(v) : String(v);
                        row.appendChild(key);
                        row.appendChild(val);
                        card.appendChild(row);
                    });
                    wrap.appendChild(card);
                } else {
                    var row = document.createElement("div");
                    row.className = "tool-output-list-item";
                    row.textContent = String(item);
                    wrap.appendChild(row);
                }
            });
            if (obj.length > 30) {
                var more = document.createElement("div");
                more.className = "tool-output-more";
                more.textContent = `... and ${obj.length - 30} more items`;
                wrap.appendChild(more);
            }
            return wrap;
        }
        // Plain object: render as key-value pairs
        Object.entries(obj).forEach(([k, v]) => {
            var row = document.createElement("div");
            row.className = "tool-output-row";
            var key = document.createElement("span");
            key.className = "tool-output-key";
            key.textContent = k;
            var val = document.createElement("span");
            val.className = "tool-output-val";
            if (typeof v === "object" && v !== null) {
                val.textContent = JSON.stringify(v, null, 2);
                val.classList.add("tool-output-val-complex");
            } else if (typeof v === "string" && v.length > 200) {
                val.textContent = v.slice(0, 200) + "…";
                val.title = v;
            } else {
                val.textContent = String(v ?? "");
            }
            row.appendChild(key);
            row.appendChild(val);
            wrap.appendChild(row);
        });
        return wrap;
    }
    function makeLocationList(rawText) {
        var lines = String(rawText || "").split("\n");
        var groups = [];
        var currentGroup = "Matches";
        for (var ln of lines) {
            if (/^\s*definitions:\s*$/i.test(ln)) { currentGroup = "Definitions"; continue; }
            if (/^\s*references:\s*$/i.test(ln)) { currentGroup = "References"; continue; }
            var hit = parseLocationLine(ln);
            if (hit) groups.push({ group: currentGroup, ...hit });
        }
        if (!groups.length) return null;
        var wrap = document.createElement("div");
        wrap.className = "tool-match-list";
        var initial = 40;
        var render = function(maxItems) {
            wrap.innerHTML = "";
            groups.slice(0, maxItems).forEach(function(hit) {
                var row = document.createElement("button");
                row.type = "button";
                row.className = "tool-match-item";
                var shortPath = condensePath(hit.path) || hit.path;
                row.innerHTML = `<span class="tool-match-group">${BX.escapeHtml(hit.group)}</span><span class="tool-match-loc" title="${BX.escapeHtml(hit.path)}">${BX.escapeHtml(shortPath)}:${hit.line}</span><span class="tool-match-text">${BX.escapeHtml(hit.text || "")}</span>`;
                row.addEventListener("click", function(e) { e.stopPropagation(); openFileAt(hit.path, hit.line); });
                wrap.appendChild(row);
            });
        };
        render(initial);
        if (groups.length > initial) {
            var btn = document.createElement("button");
            btn.className = "tool-show-more-btn";
            btn.textContent = `Show ${groups.length - initial} more matches`;
            var expanded = false;
            btn.addEventListener("click", function(e) {
                e.stopPropagation();
                expanded = !expanded;
                render(expanded ? groups.length : initial);
                btn.textContent = expanded ? "Show fewer matches" : `Show ${groups.length - initial} more matches`;
                wrap.appendChild(btn);
            });
            wrap.appendChild(btn);
        }
        return wrap;
    }
    function makeReadFilePreview(rawText, input) {
        var lines = String(rawText || "").split("\n");
        var codeLines = [];
        var header = "";
        lines.forEach(function(ln) {
            if (!header && ln.startsWith("[")) header = ln;
            var m = ln.match(/^\s*\d+\|(.*)$/);
            if (m) codeLines.push(m[1]);
        });
        if (!codeLines.length) return null;
        var wrap = document.createElement("div");
        if (header) {
            var meta = document.createElement("div");
            meta.className = "tool-read-meta";
            meta.textContent = header;
            wrap.appendChild(meta);
        }
        var preWrap = makeProgressiveBody(codeLines.join("\n"), "tool-code-preview", 3000);
        var pre = preWrap.querySelector("pre");
        if (pre && typeof hljs !== "undefined") {
            var ext = (input?.path || "").split(".").pop() || "";
            var lang = BX.langFromExt(ext);
            if (hljs.getLanguage(lang)) {
                var code = document.createElement("code");
                code.className = `language-${lang}`;
                code.textContent = pre.textContent;
                pre.innerHTML = "";
                pre.appendChild(code);
                try { hljs.highlightElement(code); } catch {}
            }
        }
        wrap.appendChild(preWrap);
        return wrap;
    }
    /** Check if content contains a unified diff (has --- and +++ headers or @@ hunks). */
    function _contentHasDiff(text) {
        if (!text) return false;
        return (/^---\s/m.test(text) && /^\+\+\+\s/m.test(text)) || /^@@\s/m.test(text);
    }

    /** Extract the diff portion from tool output (skip the summary line). */
    function _extractDiffFromContent(text) {
        if (!text) return { summary: "", diff: "" };
        var lines = text.split("\n");
        var diffStart = lines.findIndex(l => /^---\s/.test(l) || /^@@\s/.test(l));
        if (diffStart <= 0) return { summary: "", diff: text };
        return {
            summary: lines.slice(0, diffStart).join("\n").trim(),
            diff: lines.slice(diffStart).join("\n"),
        };
    }

    function renderToolOutput(runEl, name, input, content, success, extraData) {
        var out = document.createElement("div");
        out.className = `tool-result ${success ? "tool-result-success" : "tool-result-error"} ${name === "Bash" ? "tool-result-terminal" : ""}`;
        out.innerHTML = `<div class="tool-section-label">${name === "Bash" ? "Output" : "Result"}</div>`;

        if (!success) {
            var summary = failureSummary(name, content || extraData?.error || "");
            if (summary) {
                var box = document.createElement("div");
                box.className = "tool-failure-summary";
                box.innerHTML = `<div class="tool-failure-title">What failed: ${BX.escapeHtml(summary.issue)}</div><div class="tool-failure-next">Next step: ${BX.escapeHtml(summary.next)}</div>`;
                out.appendChild(box);
            }
        }

        if (name === "search" || name === "find_symbol") {
            var list = makeLocationList(content);
            if (list) out.appendChild(list);
        }
        if (name === "Read") {
            var preview = makeReadFilePreview(content, input);
            if (preview) out.appendChild(preview);
        }

        // Write/Edit/symbol_edit: render real diff from tool result (replaces preview)
        var isFileEdit = name === "Write" || name === "Edit" || name === "symbol_edit";
        if (isFileEdit && _contentHasDiff(content)) {
            var { summary, diff } = _extractDiffFromContent(content);
            if (summary) {
                var meta = document.createElement("div");
                meta.className = "tool-read-meta";
                meta.textContent = summary;
                out.appendChild(meta);
            }
            var mini = document.createElement("div");
            mini.className = "tool-mini-diff";
            mini.innerHTML = renderDiff(diff);
            out.appendChild(mini);
            // Remove preview diff if it was shown from tool_call
            var existingPreview = runEl.querySelector(".tool-edit-preview");
            if (existingPreview) existingPreview.remove();
        } else if (isFileEdit) {
            // Fallback: use input-based preview diff if tool didn't return a diff
            var diff = buildEditPreviewDiff(name, input);
            if (diff) {
                var mini = document.createElement("div");
                mini.className = "tool-mini-diff";
                mini.innerHTML = renderDiff(diff);
                out.appendChild(mini);
            }
            // Remove preview if present
            var existingPreview = runEl.querySelector(".tool-edit-preview");
            if (existingPreview) existingPreview.remove();
        }

        if (name === "Bash") {
            out.appendChild(makeProgressiveBody(content || "(no output)", "tool-terminal-body", 6000));
        } else if (isFileEdit && _contentHasDiff(content)) {
            // Don't show raw JSON/text for file edits that have diffs — diff is enough
        } else {
            out.appendChild(formatToolOutputBody(content));
        }
        runEl.appendChild(out);
        return out;
    }
    function updateToolGroupHeader(groupEl) {
        if (!groupEl) return;
        var summaryEl = groupEl.querySelector(".tool-summary");
        var count = Number(groupEl.dataset.count || "1");
        if (summaryEl) {
            var base = (summaryEl.textContent || "").replace(/\s*\(\d+\s*runs?\)\s*$/i, "").trim();
            summaryEl.textContent = count > 1 ? `${base} (${count} runs)` : base;
        }
    }
    function maybeAutoFollow(groupEl, runEl) {
        if (!groupEl || groupEl.dataset.toolName !== "Bash") return;
        if (groupEl.dataset.follow !== "1") return;
        var contentEl = groupEl.querySelector(".tool-content");
        if (contentEl) contentEl.scrollTop = contentEl.scrollHeight;
        var body = runEl.querySelector(".tool-terminal-body");
        if (body) body.scrollTop = body.scrollHeight;
        BX.scrollChat();
    }
    function _fadeOutTool(block) {
        if (block._fadingOut || block.classList.contains("tool-gone")) return;
        if (block.classList.contains("tool-block-loading")) return;
        block._fadingOut = true;
        // Collapse content but keep header visible as a one-liner
        block.classList.add("collapsed");
        block._fadingOut = false;
        BX.scrollChat();
    }
    function _scheduleToolFadeOut(block, delay) {
        if (block._fadeTimer || block._fadingOut || block.classList.contains("tool-gone")) return;
        block._fadeTimer = setTimeout(function() {
            block._fadeTimer = null;
            // Only fade if it's not the last visible tool in the bubble
            var bubble = block.closest(".msg-bubble");
            if (!bubble) return;
            var visible = Array.from(bubble.querySelectorAll(".tool-block:not(.tool-gone)"));
            if (visible.length > 1 && visible[visible.length - 1] !== block) {
                _fadeOutTool(block);
            }
        }, delay);
    }

    function _fadeOutInfo(infoEl) {
        if (infoEl._fadingOut || infoEl.classList.contains("info-gone")) return;
        infoEl._fadingOut = true;
        var h = infoEl.offsetHeight;
        // Phase 1: fade opacity
        infoEl.animate(
            [{ opacity: 1 }, { opacity: 0 }],
            { duration: 350, easing: "ease-out", fill: "forwards" }
        ).onfinish = function() {
            // Phase 2: collapse height so content below slides up
            infoEl.animate(
                [{ height: h + "px", marginTop: "0px", marginBottom: "0px" },
                 { height: "0px", marginTop: "0px", marginBottom: "0px" }],
                { duration: 280, easing: "cubic-bezier(0.4, 0, 0.2, 1)", fill: "forwards" }
            ).onfinish = function() {
                infoEl.classList.add("info-gone");
                infoEl.style.display = "none";
                infoEl._fadingOut = false;
                BX.scrollChat();
            };
        };
    }

    function _scheduleInfoFadeOut(infoEl, delay) {
        if (infoEl._fadeTimer || infoEl._fadingOut || infoEl.classList.contains("info-gone")) return;
        infoEl._fadeTimer = setTimeout(function() {
            infoEl._fadeTimer = null;
            _fadeOutInfo(infoEl);
        }, delay);
    }

    function addToolCallPlaceholder(name, toolUseId) {
        name = normalizedToolName(name);
        var bubble = getOrCreateBubble();
        var icon = toolIcon(name, {});
        var isWrite = name === "Write" || name === "Edit" || name === "symbol_edit";
        var group = document.createElement("div");
        group.className = "tool-block tool-block-loading";
        group.dataset.toolName = name;
        group.dataset.groupKey = name;
        group.dataset.count = "1";
        group.dataset.firstAt = String(Date.now());
        group.dataset.lastAt = String(Date.now());
        group.dataset.follow = "1";
        var statusHtml = `<span class="tool-status tool-status-pending" title="Generating\u2026"><span class="tool-status-dot"></span></span>`;
        group.innerHTML = `
            <div class="tool-header">
                <div class="tool-left">
                    <span class="tool-icon-wrap"><span class="tool-icon">${icon}</span></span>
                    <span class="tool-summary">${BX.escapeHtml(toolTitle(name))} <span class="tool-generating-label">generating\u2026</span></span>
                </div>
                <div class="tool-right">${statusHtml}<span class="tool-chevron">\u25BC</span></div>
            </div>
            <div class="tool-content">
                <div class="tool-run-list">
                    <div class="tool-run tool-run-placeholder" ${toolUseId ? `data-tool-use-id="${BX.escapeHtml(String(toolUseId))}"` : ""}>
                        <div class="tool-input-progress" style="padding:8px 12px;color:var(--muted);font-size:0.85em;">
                            Generating input\u2026
                        </div>
                    </div>
                </div>
            </div>`;
        if (isWrite) group.classList.remove("collapsed");
        else group.classList.add("collapsed");
        group.querySelector(".tool-header").addEventListener("click", () => group.classList.toggle("collapsed"));
        bubble.appendChild(group);
        lastToolGroup = group;
        var run = group.querySelector(".tool-run-placeholder");
        if (toolUseId && run) toolRunById.set(String(toolUseId), run);
        toolRunState.set(run, { name, input: {}, output: "" });
        BX.scrollChat();
        return group;
    }

    function updateToolInputProgress(runEl, bytes, path) {
        var prog = runEl.querySelector ? runEl.querySelector(".tool-input-progress") : null;
        if (!prog) return;
        var kb = (bytes / 1024).toFixed(1);
        var text = `Generating input\u2026 ${kb} KB`;
        if (path) {
            var group = runEl.closest(".tool-block");
            if (group) {
                var summaryEl = group.querySelector(".tool-summary");
                if (summaryEl) {
                    var name = group.dataset.toolName || "";
                    summaryEl.innerHTML = `${BX.escapeHtml(toolTitle(name))} <span class="tool-generating-path">${BX.escapeHtml(condensePath(path))}</span> <span class="tool-generating-label">generating\u2026 ${BX.escapeHtml(kb)} KB</span>`;
                }
            }
            text = `Writing ${condensePath(path)}\u2026 ${kb} KB`;
        }
        prog.textContent = text;
    }

    function finalizeToolCallPlaceholder(runEl, name, input) {
        name = normalizedToolName(name, input);
        var group = runEl.closest(".tool-block");
        if (!group) return;
        var genLabel = group.querySelector(".tool-generating-label");
        if (genLabel) genLabel.remove();
        var genPath = group.querySelector(".tool-generating-path");
        if (genPath) genPath.remove();
        var summaryEl = group.querySelector(".tool-summary");
        var headerDesc = (name === "Read" && input?.path) ? readFileDisplayString(input) : toolDescForHeader(name, input);
        var summaryText = headerDesc ? `${toolTitle(name)} ${headerDesc}` : toolTitle(name);
        if (summaryEl) summaryEl.textContent = summaryText;
        if (input?.path) group.dataset.path = input.path;
        var placeholder = runEl.querySelector(".tool-input-progress");
        if (placeholder) placeholder.remove();
        runEl.classList.remove("tool-run-placeholder");
        runEl.dataset.toolName = name;
        if (input?.path) runEl.dataset.path = input.path;
        runEl._toolInput = input || {};
        var formattedInput = formatToolInput(name, input);
        if (formattedInput) {
            runEl.appendChild(formattedInput);
        } else {
            var isCmd = name === "Bash";
            var inputText = isCmd ? (input?.command || JSON.stringify(input, null, 2)) : JSON.stringify(input, null, 2);
            runEl.appendChild(makeProgressiveBody(inputText || "{}", isCmd ? "tool-input tool-input-cmd" : "tool-input", isCmd ? 2800 : 1800));
        }
        if (name === "Write" || name === "Edit" || name === "symbol_edit") {
            var previewDiff = buildEditPreviewDiff(name, input);
            if (previewDiff) {
                var previewWrap = document.createElement("div");
                previewWrap.className = "tool-edit-preview";
                var label = document.createElement("div");
                label.className = "tool-section-label";
                label.textContent = "Changes";
                var miniDiff = document.createElement("div");
                miniDiff.className = "tool-mini-diff";
                previewWrap.appendChild(label);
                previewWrap.appendChild(miniDiff);
                runEl.appendChild(previewWrap);
                var diffLines = previewDiff.split("\n");
                var lineIdx = 0;
                var _lastTime = 0;
                var LINE_DELAY = 12;
                var cursor = document.createElement("div");
                cursor.className = "diff-stream-cursor";
                miniDiff.appendChild(cursor);
                function _streamTick(ts) {
                    if (lineIdx >= diffLines.length) {
                        cursor.classList.add("fade-out");
                        setTimeout(() => cursor.remove(), 200);
                        return;
                    }
                    if (ts - _lastTime < LINE_DELAY) { requestAnimationFrame(_streamTick); return; }
                    _lastTime = ts;
                    var l = diffLines[lineIdx];
                    var div = document.createElement("div");
                    var c = "ctx";
                    if (l.startsWith("+++") || l.startsWith("---")) c = "hunk";
                    else if (l.startsWith("@@")) c = "hunk";
                    else if (l.startsWith("+")) c = "add";
                    else if (l.startsWith("-")) c = "del";
                    div.className = "diff-line " + c;
                    div.textContent = l;
                    miniDiff.insertBefore(div, cursor);
                    lineIdx++;
                    miniDiff.scrollTop = miniDiff.scrollHeight;
                    if (lineIdx % 4 === 0 && group.dataset.follow === "1") BX.scrollChat();
                    requestAnimationFrame(_streamTick);
                }
                requestAnimationFrame(_streamTick);
            }
            group.classList.remove("collapsed");
        }
        var state = toolRunState.get(runEl);
        if (state) { state.name = name; state.input = input || {}; toolRunState.set(runEl, state); }
        BX.scrollChat();
    }

    function addToolCall(name, input, toolUseId = null, { stream = true } = {}) {
        name = normalizedToolName(name, input);
        var bubble = getOrCreateBubble();
        var isCmd = name === "Bash";
        var now = Date.now();
        var key = toolGroupKey(name, input);

        var group = null;
        var canReuse =
            lastToolGroup &&
            lastToolGroup.isConnected &&
            lastToolGroup.dataset.groupKey === key &&
            (now - Number(lastToolGroup.dataset.lastAt || 0) < 60000);

        if (canReuse) {
            group = lastToolGroup;
            group.dataset.lastAt = String(now);
            group.dataset.count = String(Number(group.dataset.count || "1") + 1);
            updateToolGroupHeader(group);
            var statusEl = group.querySelector(".tool-status");
            if (statusEl) {
                statusEl.outerHTML = isCmd
                    ? `<span class="tool-status tool-status-running" title="Running"><span class="tool-status-dot"></span></span>`
                    : `<span class="tool-status tool-status-pending" title="Pending"><span class="tool-status-dot"></span></span>`;
            }
            if (name === "TodoWrite" && input?.todos && Array.isArray(input.todos)) {
                checklistSource = "todos";
                var normalized = input.todos.map((t, i) => ({
                    id: t.id != null ? t.id : String(i + 1),
                    content: t.content || "",
                    status: (t.status || "pending").toLowerCase()
                }));
                showAgentChecklist(normalized);
            }
            /* Stop button lives in .tool-content-actions; no need to inject into options panel */
        } else {
            var desc = toolDesc(name, input);
            var headerDesc = (name === "Read" && input?.path) ? readFileDisplayString(input) : toolDescForHeader(name, input);
            var linkHtml = webToolLinkHtml(name, input);
            var icon = toolIcon(name, input);
            group = document.createElement("div");
            group.className = isCmd ? "tool-block tool-block-command" : "tool-block tool-block-loading";
            group.dataset.toolName = name;
            group.dataset.groupKey = key;
            group.dataset.count = "1";
            group.dataset.firstAt = String(now);
            group.dataset.lastAt = String(now);
            group.dataset.follow = "1";
            if (input?.path) group.dataset.path = input.path;
            var statusHtml = isCmd
                ? `<span class="tool-status tool-status-running" title="Running"><span class="tool-status-dot"></span></span>`
                : `<span class="tool-status tool-status-pending" title="Pending"><span class="tool-status-dot"></span></span>`;

            var summaryText = headerDesc ? `${toolTitle(name)} ${headerDesc}` : toolTitle(name);
            var fileToolWithPath = (name === "Read" || name === "Write" || name === "Edit" || name === "symbol_edit" || name === "lint_file") && input?.path;
            var headerTitle = fileToolWithPath ? String(input.path).replace(/\\/g, "/") : (headerDesc || toolTitle(name));
            group.innerHTML = `
                <div class="tool-header ${isCmd ? "tool-header-cmd" : ""}">
                    <div class="tool-left">
                        <span class="tool-icon-wrap"><span class="tool-icon">${icon}</span></span>
                        <span class="tool-summary" title="${BX.escapeHtml(headerTitle)}">${BX.escapeHtml(summaryText)}</span>${linkHtml}
                    </div>
                    <div class="tool-right">
                        ${statusHtml}
                        <div class="tool-options-wrap tool-options-wrap-empty">
                            <button type="button" class="tool-options-trigger" aria-label="Options" aria-expanded="false" title="Options">${toolActionIcon("more")}</button>
                            <div class="tool-options-panel" role="menu" aria-hidden="true"></div>
                        </div>
                        <span class="tool-chevron">\u25BC</span>
                    </div>
                </div>
                <div class="tool-content ${isCmd ? "tool-content-cmd" : ""}">
                    <div class="tool-content-actions">
                        <button type="button" class="tool-content-btn tool-action-open ${toolCanOpenFile(name, input) ? "" : "hidden"}" title="Open file" data-action="open"><span class="tool-content-btn-icon">${toolActionIcon("open")}</span><span class="tool-content-btn-label">Open file</span></button>
                        <button type="button" class="tool-content-btn tool-action-rerun" title="Rerun" data-action="rerun"><span class="tool-content-btn-icon">${toolActionIcon("rerun")}</span><span class="tool-content-btn-label">Rerun</span></button>
                        <button type="button" class="tool-content-btn tool-action-retry hidden" title="Retry failed" data-action="retry"><span class="tool-content-btn-icon">${toolActionIcon("retry")}</span><span class="tool-content-btn-label">Retry</span></button>
                        <button type="button" class="tool-content-btn tool-action-copy hidden" title="Copy output" data-action="copy"><span class="tool-content-btn-icon">${toolActionIcon("copy")}</span><span class="tool-content-btn-label">Copy output</span></button>
                        <button type="button" class="tool-content-btn tool-stop-btn ${isCmd ? "" : "hidden"}" title="Stop command" data-action="stop"><span class="tool-content-btn-icon">${toolActionIcon("stop")}</span><span class="tool-content-btn-label">Stop</span></button>
                        ${isCmd ? `<button type="button" class="tool-content-btn tool-follow-btn" title="Pause follow" data-action="follow"><span class="tool-content-btn-icon">${toolActionIcon("pause")}</span><span class="tool-content-btn-label">Pause follow</span></button>` : ""}
                    </div>
                    <div class="tool-run-list"></div>
                </div>`;
            // Write/Edit/symbol_edit start expanded so user can see the diff; others start collapsed
            var startsExpanded = name === "Write" || name === "Edit" || name === "symbol_edit";
            if (!startsExpanded) group.classList.add("collapsed");
            group.querySelector(".tool-header").addEventListener("click", () => group.classList.toggle("collapsed"));

            var optionsWrap = group.querySelector(".tool-options-wrap");
            var optionsTrigger = group.querySelector(".tool-options-trigger");
            var optionsPanel = group.querySelector(".tool-options-panel");
            function closeOptionsPanel() {
                if (!optionsPanel || !optionsTrigger) return;
                optionsPanel.classList.remove("is-open");
                optionsTrigger.setAttribute("aria-expanded", "false");
                optionsPanel.setAttribute("aria-hidden", "true");
                document.removeEventListener("click", closeOptionsPanel);
            }
            if (optionsTrigger && optionsPanel) {
                optionsTrigger.addEventListener("click", function(e) {
                    e.stopPropagation();
                    var open = optionsPanel.classList.toggle("is-open");
                    optionsTrigger.setAttribute("aria-expanded", String(open));
                    optionsPanel.setAttribute("aria-hidden", String(!open));
                    if (open) {
                        requestAnimationFrame(() => document.addEventListener("click", closeOptionsPanel));
                    } else {
                        document.removeEventListener("click", closeOptionsPanel);
                    }
                });
                optionsPanel.addEventListener("click", (e) => e.stopPropagation());
            }

            var path = input?.path;
            var openBtn = group.querySelector(".tool-action-open");
            if (openBtn && path) openBtn.addEventListener("click", function(e) { e.stopPropagation(); closeOptionsPanel(); BX.openFile(path); });
            var rerunBtn = group.querySelector(".tool-action-rerun");
            if (rerunBtn) rerunBtn.addEventListener("click", function(e) { e.stopPropagation(); closeOptionsPanel(); runFollowupPrompt(toolFollowupPrompt(name, input, false)); });
            var retryBtn = group.querySelector(".tool-action-retry");
            if (retryBtn) retryBtn.addEventListener("click", function(e) { e.stopPropagation(); closeOptionsPanel(); runFollowupPrompt(toolFollowupPrompt(name, input, true)); });
            var copyBtn = group.querySelector(".tool-action-copy");
            if (copyBtn) copyBtn.addEventListener("click", function(e) { e.stopPropagation(); closeOptionsPanel(); BX.copyText(group.dataset.latestOutput || ""); });
            var followBtn = group.querySelector(".tool-follow-btn");
            if (followBtn) {
                var followIcon = followBtn.querySelector(".tool-content-btn-icon");
                followBtn.addEventListener("click", function(e) {
                    e.stopPropagation();
                    var enabled = group.dataset.follow === "1";
                    group.dataset.follow = enabled ? "0" : "1";
                    if (followIcon) followIcon.innerHTML = toolActionIcon(enabled ? "play" : "pause");
                    var labelEl = followBtn.querySelector(".tool-content-btn-label");
                    if (labelEl) labelEl.textContent = enabled ? "Resume follow" : "Pause follow";
                    followBtn.title = enabled ? "Resume follow" : "Pause follow";
                    followBtn.classList.toggle("paused", enabled);
                    if (!enabled) {
                        var contentEl = group.querySelector(".tool-content");
                        if (contentEl) contentEl.scrollTop = contentEl.scrollHeight;
                    }
                });
            }
            var stopBtn = group.querySelector(".tool-stop-btn");
            if (stopBtn) {
                var stopIcon = stopBtn.querySelector(".tool-content-btn-icon");
                stopBtn.addEventListener("click", function(e) {
                    e.stopPropagation();
                    closeOptionsPanel();
                    BX.send({ type: "cancel" });
                    stopBtn.disabled = true;
                    stopBtn.classList.add("is-stopping");
                    if (stopIcon) stopIcon.innerHTML = `<span class="tool-stop-spinner"></span>`;
                    stopBtn.title = "Stopping\u2026";
                });
            }
            group.querySelectorAll(".tool-content-btn").forEach(btn => btn.addEventListener("click", e => e.stopPropagation()));

            bubble.appendChild(group);
            lastToolGroup = group;
            // Fade out older completed tools now that a new one appeared
            var older = Array.from(bubble.querySelectorAll(".tool-block:not(.tool-gone):not(.tool-block-loading)"));
            older.forEach(function(b) { if (b !== group) _fadeOutTool(b); });
        }

        var runList = group.querySelector(".tool-run-list");
        var runIndex = Number(group.dataset.count || "1");
        var run = document.createElement("div");
        run.className = "tool-run";
        run.dataset.runIndex = String(runIndex);
        run.dataset.toolName = name;
        if (toolUseId) run.dataset.toolUseId = String(toolUseId);
        if (input?.path) run.dataset.path = input.path;
        run._toolInput = input || {};
        run.innerHTML = `
            <div class="tool-run-header">
                <span class="tool-run-index">Run ${runIndex}</span>
                <span class="tool-run-time">${formatClock(now)}</span>
            </div>
            `;
        // Human-readable input display (replaces raw JSON)
        var formattedInput = formatToolInput(name, input);
        if (formattedInput) {
            run.appendChild(formattedInput);
        } else {
            // Fallback for tools with no special formatting
            var inputText = isCmd ? (input?.command || JSON.stringify(input, null, 2)) : JSON.stringify(input, null, 2);
            run.appendChild(makeProgressiveBody(inputText || "{}", isCmd ? "tool-input tool-input-cmd" : "tool-input", isCmd ? 2800 : 1800));
        }
        // For Write/Edit: stream the diff lines progressively (Cursor-style)
        if (name === "Write" || name === "Edit" || name === "symbol_edit") {
            var previewDiff = buildEditPreviewDiff(name, input);
            if (previewDiff) {
                var previewWrap = document.createElement("div");
                previewWrap.className = "tool-edit-preview";
                var label = document.createElement("div");
                label.className = "tool-section-label";
                label.textContent = "Changes";
                var miniDiff = document.createElement("div");
                miniDiff.className = "tool-mini-diff";
                previewWrap.appendChild(label);
                previewWrap.appendChild(miniDiff);
                run.appendChild(previewWrap);

                if (stream) {
                    // Smooth line-by-line streaming (Cursor-style)
                    var diffLines = previewDiff.split("\n");
                    var lineIdx = 0;
                    var _rafId = 0;
                    var _lastTime = 0;
                    var LINE_DELAY = 12; // ms per line — fast but perceptible

                    // Thin caret cursor
                    var cursor = document.createElement("div");
                    cursor.className = "diff-stream-cursor";
                    miniDiff.appendChild(cursor);

                    function _streamTick(ts) {
                        if (lineIdx >= diffLines.length) {
                            cursor.classList.add("fade-out");
                            setTimeout(() => cursor.remove(), 200);
                            return;
                        }
                        // Throttle to LINE_DELAY ms per line
                        if (ts - _lastTime < LINE_DELAY) {
                            _rafId = requestAnimationFrame(_streamTick);
                            return;
                        }
                        _lastTime = ts;
                        var l = diffLines[lineIdx];
                        var div = document.createElement("div");
                        var c = "ctx";
                        if (l.startsWith("+++") || l.startsWith("---")) c = "hunk";
                        else if (l.startsWith("@@")) c = "hunk";
                        else if (l.startsWith("+")) c = "add";
                        else if (l.startsWith("-")) c = "del";
                        div.className = "diff-line " + c;
                        div.textContent = l;
                        miniDiff.insertBefore(div, cursor);
                        lineIdx++;
                        miniDiff.scrollTop = miniDiff.scrollHeight;
                        if (lineIdx % 4 === 0 && group.dataset.follow === "1") BX.scrollChat();
                        _rafId = requestAnimationFrame(_streamTick);
                    }
                    // Kick off after a micro-delay so the tool header paints first
                    requestAnimationFrame(_streamTick);
                } else {
                    // Instant render (replay / history restore)
                    miniDiff.classList.add("no-animate");
                    miniDiff.innerHTML = renderDiff(previewDiff);
                }
            }
        }
        runList.appendChild(run);
        toolRunState.set(run, { name, input: input || {}, output: "" });
        if (toolUseId) toolRunById.set(String(toolUseId), run);
        BX.scrollChat();
        return run;
    }
    function addToolResult(content, success, runEl, extraData) {
        if (!runEl) return;
        // If we were passed the group (e.g. fallback when tool_use_id didn't match), use last run in group
        if (runEl.classList && runEl.classList.contains("tool-block")) {
            var list = runEl.querySelector(".tool-run-list");
            runEl = (list && list.lastElementChild) || runEl;
        }
        var state = toolRunState.get(runEl);
        if (!state) {
            // Still update group header status so circle -> tick/cross works even when run state is missing (e.g. no output)
            var group = runEl.closest ? runEl.closest(".tool-block") : (runEl.parentElement && runEl.parentElement.closest(".tool-block"));
            if (group) {
                var header = group.querySelector(".tool-header");
                var statusEl = (header && header.querySelector(".tool-status")) || group.querySelector(".tool-status");
                if (statusEl) {
                    statusEl.outerHTML = success
                        ? `<span class="tool-status tool-status-success" title="Done">${toolActionIcon("done")}</span>`
                        : `<span class="tool-status tool-status-error" title="Failed">${toolActionIcon("failed")}</span>`;
                    group.classList.remove("tool-block-loading");
                }
            }
            return;
        }
        var group = runEl.closest(".tool-block");
        if (!group && runEl.parentElement) group = runEl.parentElement.closest(".tool-block");
        if (!group) return;
        var isCmd = state.name === "Bash";

        var baseOutput = String(content || extraData?.error || "");
        var rawOutput = baseOutput || "(no output)";
        var prior = state.output || "";
        var merged = isCmd
            ? (baseOutput ? ((prior && !prior.includes(baseOutput)) ? `${prior}\n${baseOutput}` : (prior || baseOutput)) : (prior || rawOutput))
            : (rawOutput || prior || "(no output)");
        state.output = merged;
        toolRunState.set(runEl, state);

        var searchStats = null;
        if (state.name === "search" && group.dataset.toolName === "search") {
            searchStats = countFilesAndMatchesInSearchOutput(merged);
            if (searchStats.fileCount === 0) {
                group.remove();
                BX.scrollChat();
                return;
            }
        }

        runEl.querySelector(".tool-result")?.remove();
        renderToolOutput(runEl, state.name, state.input, merged, success, extraData);

        group.dataset.latestOutput = merged;
        var copyBtn = group.querySelector(".tool-action-copy");
        if (copyBtn) copyBtn.classList.remove("hidden");
        var retryBtn = group.querySelector(".tool-action-retry");
        if (retryBtn) retryBtn.classList.toggle("hidden", success !== false);

        var header = group.querySelector(".tool-header");
        var statusEl = (header && header.querySelector(".tool-status")) || group.querySelector(".tool-status");
        if (statusEl) {
            if (isCmd) {
                var exitCode = extraData?.exit_code;
                var duration = extraData?.duration;
                var badge = success
                    ? `<span class="tool-status tool-status-success" title="Command succeeded">${toolActionIcon("done")}</span>`
                    : `<span class="tool-status tool-status-error" title="Command failed (exit ${exitCode ?? "?"})">${toolActionIcon("failed")}</span>`;
                if (duration !== undefined && duration !== null) badge += `<span class="tool-status tool-status-duration">${duration}s</span>`;
                statusEl.outerHTML = badge;
            } else {
                statusEl.outerHTML = success
                    ? `<span class="tool-status tool-status-success" title="Done">${toolActionIcon("done")}</span>`
                    : `<span class="tool-status tool-status-error" title="Failed">${toolActionIcon("failed")}</span>`;
            }
        }

        group.classList.remove("tool-block-loading");

        _scheduleToolFadeOut(group, 700);

        var stopBtn = group.querySelector(".tool-stop-btn");
        if (stopBtn && success !== undefined) stopBtn.remove();
        if (searchStats) {
            var summaryEl = group.querySelector(".tool-summary");
            if (summaryEl) {
                var fileStr = searchStats.fileCount === 1 ? "1 file" : searchStats.fileCount + " files";
                var searchStr = searchStats.matchCount === 1 ? "1 search" : searchStats.matchCount + " searches";
                summaryEl.textContent = "Explored " + fileStr + " " + searchStr;
            }
        }
        if (state.name === "Write" || state.name === "Edit" || state.name === "symbol_edit") {
            setTimeout(updateModifiedFilesBar, 600);
        }
        maybeAutoFollow(group, runEl);
        BX.scrollChat();
    }

    function appendCommandOutput(toolUseId, chunk, isStderr) {
        if (!toolUseId || !chunk) return;
        var runEl = toolRunById.get(String(toolUseId));
        if (!runEl) return;
        var state = toolRunState.get(runEl);
        if (!state) return;
        var group = runEl.closest(".tool-block");
        if (!group) return;

        var next = `${state.output || ""}${chunk}`;
        state.output = next.length > 50000 ? next.slice(-50000) : next;
        toolRunState.set(runEl, state);

        var live = runEl.querySelector(".tool-terminal-live");
        if (!live) {
            var label = document.createElement("div");
            label.className = "tool-section-label";
            label.textContent = "Live output";
            live = document.createElement("pre");
            live.className = "tool-terminal-live";
            runEl.appendChild(label);
            runEl.appendChild(live);
        }
        live.textContent = state.output;
        if (isStderr) live.classList.add("stderr");

        group.dataset.latestOutput = state.output;
        var copyBtn = group.querySelector(".tool-action-copy");
        if (copyBtn) copyBtn.classList.remove("hidden");
        maybeAutoFollow(group, runEl);
        BX.scrollChat();
    }
    /** Show only the relevant part of a path in headers (last 2 segments, e.g. src/foo.tsx). */
    function condensePath(fullPath) {
        if (!fullPath || typeof fullPath !== "string") return "";
        var path = String(fullPath).replace(/\\/g, "/").replace(/\/+$/, "");
        var segments = path.split("/").filter(Boolean);
        if (segments.length === 0) return "";
        if (segments.length <= 2) return segments.join("/");
        return segments.slice(-2).join("/");
    }
    function readFileDisplayString(input) {
        if (!input?.path) return "Read";
        var path = String(input.path).replace(/\\/g, "/");
        var short = condensePath(path);
        var base = path.split("/").pop() || path;
        var offset = input.offset != null ? Number(input.offset) : null;
        var limit = input.limit != null ? Number(input.limit) : null;
        if (offset != null && limit != null && limit > 0) {
            var end = offset + limit - 1;
            return short + " L" + offset + "\u2013" + end;
        }
        if (offset != null) return short + " L" + offset;
        return short;
    }
    function countFilesAndMatchesInSearchOutput(output) {
        if (!output || typeof output !== "string") return { fileCount: 0, matchCount: 0 };
        var seen = new Set();
        var matchCount = 0;
        var lineRe = /^([^:]+):\d+:/;
        output.split("\n").forEach(function(line) {
            var match = line.match(lineRe);
            if (match) {
                seen.add(match[1].trim());
                matchCount += 1;
            }
        });
        return { fileCount: seen.size, matchCount };
    }
    function toolLabel(n) {
        var labels = {
            Read: "Read",
            Write: "Write",
            Edit: "Edit",
            symbol_edit: "Symbol",
            lint_file: "Lint",
            Bash: "Run",
            search: "Explored",
            list_directory: "List",
            Glob: "Glob",
            find_symbol: "Symbols",
            scout: "Scout",
            project_tree: "Project Tree",
            TodoWrite: "Planning next steps",
            TodoRead: "Read todos",
            MemoryWrite: "Store",
            MemoryRead: "Recall",
            WebFetch: "Fetch",
            WebSearch: "Search web",
            semantic_retrieve: "Code search",
        };
        return labels[n] || n;
    }
    /** Map API/implementation tool names to canonical display names.
     *  Native tools: str_replace_based_edit_tool → Read/Write/Edit based on command, bash → Bash, web_search → WebSearch.
     *  When input is available, pass it to resolve the sub-command for the editor tool. */
    function normalizedToolName(n, input) {
        if (n === "str_replace_based_edit_tool") {
            var cmd = input?.command;
            if (cmd === "view") return "Read";
            if (cmd === "create") return "Write";
            if (cmd === "str_replace" || cmd === "insert") return "Edit";
            return "Edit"; // fallback to Edit for unknown sub-commands
        }
        if (n === "bash") return "Bash";
        if (n === "web_search") return "WebSearch";
        var map = {
            read_file: "Read",
            write_file: "Write",
            edit_file: "Edit",
            glob_find: "Glob",
            run_command: "Bash",
            SemanticRetrieve: "semantic_retrieve",
            AskUserQuestion: "AskUserQuestion",
        };
        return map[n] || n;
    }
    function toolTitle(n) {
        var titles = {
            Read: "Read",
            Write: "Write",
            Edit: "Edit",
            symbol_edit: "Symbol",
            lint_file: "Lint",
            Bash: "Run",
            search: "Explored",
            list_directory: "List",
            Glob: "Glob",
            find_symbol: "Symbols",
            scout: "Scout",
            project_tree: "Project Tree",
            TodoWrite: "Planning next steps",
            TodoRead: "Read todos",
            MemoryWrite: "Store",
            MemoryRead: "Recall",
            WebFetch: "Fetch",
            WebSearch: "Search web",
            semantic_retrieve: "Code search",
            SemanticRetrieve: "Code search",
        };
        return titles[n] || titles[typeof n === "string" ? n.replace(/([A-Z])/g, "_$1").toLowerCase().replace(/^_/, "") : n] || toolLabel(n);
    }
    function toolDesc(n, i) {
        /* Used for grouping; keep so distinct calls (e.g. different paths) stay in separate groups. */
        switch(n) {
            case "Read": return i?.path || "";
            case "Write": return i?.path || "";
            case "Edit": return i?.path || "";
            case "symbol_edit": return `${i?.path || ""}::${i?.symbol || ""}`;
            case "lint_file": return i?.path || "";
            case "Bash": return i?.command || "";
            case "search": return `${i?.pattern || ""}@${i?.path || "."}`;
            case "list_directory": return i?.path || ".";
            case "Glob": return i?.pattern || "";
            case "find_symbol": return `${i?.symbol || ""}@${i?.path || "."}`;
            case "scout": return i?.task || "";
            case "TodoWrite": return (i?.todos?.length ? `${i.todos.length} items` : "") || "";
            case "TodoRead": return "";
            case "MemoryWrite": return i?.key || "";
            case "MemoryRead": return i?.key ? i.key : "all";
            case "WebFetch": return i?.url || "";
            case "WebSearch": return i?.query || "";
            case "semantic_retrieve": return i?.query || "";
            default: return "";
        }
    }
    function toolDescForHeader(n, i) {
        /* Compact headers: show condensed path (or other short hint); full input is in expanded body. */
        if (!i) return "";
        var path = i.path != null ? String(i.path).replace(/\\/g, "/") : "";
        if (n === "Write" || n === "Edit" || n === "lint_file") return condensePath(path) || "";
        if (n === "symbol_edit") {
            var p = condensePath(path);
            var sym = i.symbol ? (i.symbol.length > 20 ? i.symbol.slice(0, 17) + "…" : i.symbol) : "";
            return [p, sym].filter(Boolean).join(" · ") || "";
        }
        if (n === "list_directory" || n === "search" || n === "find_symbol") return condensePath(path) || (n === "search" ? (i.pattern || "") : n === "find_symbol" ? (i.symbol || "") : "") || "";
        return "";
    }
    /** Understated link/query for WebFetch/WebSearch in tool header. Returns safe HTML or "". */
    function webToolLinkHtml(name, input) {
        if (!input) return "";
        if (name === "WebFetch" && input.url) {
            var url = String(input.url).trim();
            var short = url.length > 52 ? url.slice(0, 49) + "\u2026" : url;
            return `<span class="tool-link-wrap"><a href="${BX.escapeHtml(url)}" class="tool-link-subtle" target="_blank" rel="noopener noreferrer">${BX.escapeHtml(short)}</a></span>`;
        }
        if (name === "WebSearch" && input.query) {
            var q = String(input.query).trim();
            var short = q.length > 48 ? q.slice(0, 45) + "\u2026" : q;
            return `<span class="tool-link-wrap tool-link-query">${BX.escapeHtml(short)}</span>`;
        }
        return "";
    }
    function toolIcon(n, input) {
        // File-based tools → show the file type icon
        if ((n === "Read" || n === "Write" || n === "Edit" || n === "symbol_edit" || n === "lint_file") && input?.path) {
            return BX.fileTypeIcon(input.path, 14);
        }
        var svgs = {
            Bash: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>`,
            search: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`,
            semantic_retrieve: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="10" cy="10" r="7"/><line x1="19" y1="19" x2="15.5" y2="15.5"/><line x1="6" y1="14" x2="14" y2="14"/><line x1="6" y1="17" x2="12" y2="17"/><line x1="6" y1="20" x2="13" y2="20"/></svg>`,
            SemanticRetrieve: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="10" cy="10" r="7"/><line x1="19" y1="19" x2="15.5" y2="15.5"/><line x1="6" y1="14" x2="14" y2="14"/><line x1="6" y1="17" x2="12" y2="17"/><line x1="6" y1="20" x2="13" y2="20"/></svg>`,
            find_symbol: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16"/><path d="M7 4v3a5 5 0 0 0 10 0V4"/><line x1="12" y1="17" x2="12" y2="21"/><line x1="8" y1="21" x2="16" y2="21"/></svg>`,
            list_directory: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`,
            project_tree: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="7" height="5" rx="1"/><rect x="14" y="8" width="7" height="5" rx="1"/><rect x="14" y="16" width="7" height="5" rx="1"/><line x1="10" y1="5.5" x2="14" y2="10.5"/><line x1="10" y1="5.5" x2="14" y2="18.5"/></svg>`,
            Glob: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><path d="M11 8v6"/><path d="M8 11h6"/></svg>`,
            scout: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
            TodoWrite: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2h6v4H9V2z"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M9 10l2 2 4-4"/><path d="M9 14h6M9 18h6"/></svg>`,
            TodoRead: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`,
            MemoryWrite: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a7 7 0 0 1 7 7c0 2.5-1.3 4.7-3.2 6H8.2C6.3 13.7 5 11.5 5 9a7 7 0 0 1 7-7z"/><path d="M9 17h6"/><path d="M10 21h4"/></svg>`,
            MemoryRead: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a7 7 0 0 1 7 7c0 2.5-1.3 4.7-3.2 6H8.2C6.3 13.7 5 11.5 5 9a7 7 0 0 1 7-7z"/><path d="M9 17h6"/><path d="M10 21h4"/></svg>`,
            WebFetch: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
            WebSearch: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><path d="M8 11h6"/></svg>`,
        };
        var snake = typeof n === "string" ? n.replace(/([A-Z])/g, "_$1").toLowerCase().replace(/^_/, "") : n;
        return svgs[n] || svgs[snake] || `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/></svg>`;
    }

    function formatToolInput(name, input) {
        if (!input || typeof input !== "object") return null;
        var wrap = document.createElement("div");
        wrap.className = "tool-input-formatted";

        function chip(val, cls) {
            var s = document.createElement("span");
            s.className = "tool-input-chip" + (cls ? " " + cls : "");
            s.textContent = val;
            s.title = val;
            return s;
        }
        function kvChip(key, val) {
            var s = document.createElement("span");
            s.className = "tool-input-chip";
            s.innerHTML = `<span class="chip-key">${BX.escapeHtml(key)}</span> <span class="chip-val">${BX.escapeHtml(String(val).slice(0, 120))}</span>`;
            s.title = String(val);
            return s;
        }

        switch (name) {
            case "Read":
                if (input.path) wrap.appendChild(chip(input.path, "chip-path"));
                if (input.view_range) wrap.appendChild(kvChip("range", `${input.view_range[0]}-${input.view_range[1]}`));
                else if (input.offset) wrap.appendChild(kvChip("from", `line ${input.offset}`));
                if (input.limit) wrap.appendChild(kvChip("lines", input.limit));
                break;
            case "Write":
                if (input.path) wrap.appendChild(chip(input.path, "chip-path"));
                { var ct = input.file_text || input.content || "";
                  if (ct) { var lc = (ct.match(/\n/g) || []).length + 1; wrap.appendChild(kvChip("lines", lc)); } }
                break;
            case "Edit":
            case "symbol_edit":
                if (input.path) wrap.appendChild(chip(input.path, "chip-path"));
                if (input.symbol) wrap.appendChild(kvChip("symbol", input.symbol));
                { var os = input.old_str || input.old_string || "";
                  if (os) { var preview = os.split("\n")[0].slice(0, 60);
                    wrap.appendChild(kvChip("find", preview + (os.length > 60 ? "..." : ""))); } }
                if (input.replace_all) wrap.appendChild(kvChip("mode", "replace all"));
                if (input.insert_line != null) wrap.appendChild(kvChip("at line", input.insert_line));
                break;
            case "Bash":
                if (input.command) wrap.appendChild(chip(input.command, "chip-cmd"));
                if (input.timeout) wrap.appendChild(kvChip("timeout", input.timeout + "s"));
                break;
            case "search":
                if (input.pattern) wrap.appendChild(chip(input.pattern, "chip-cmd"));
                if (input.path) wrap.appendChild(kvChip("in", input.path));
                if (input.include) wrap.appendChild(kvChip("filter", input.include));
                break;
            case "find_symbol":
                if (input.symbol) wrap.appendChild(chip(input.symbol, "chip-path"));
                if (input.kind) wrap.appendChild(kvChip("kind", input.kind));
                if (input.path) wrap.appendChild(kvChip("in", input.path));
                break;
            case "semantic_retrieve":
                if (input.query) wrap.appendChild(chip(input.query, "chip-cmd"));
                break;
            case "Glob":
                if (input.pattern) wrap.appendChild(chip(input.pattern, "chip-cmd"));
                break;
            case "project_tree":
                wrap.appendChild(chip(input.focus_path || "full tree", "chip-path"));
                break;
            case "lint_file":
                if (input.path) wrap.appendChild(chip(input.path, "chip-path"));
                break;
            case "list_directory":
                wrap.appendChild(chip(input.path || ".", "chip-path"));
                break;
            case "WebFetch":
                if (input.url) wrap.appendChild(chip(input.url, "chip-cmd"));
                break;
            case "WebSearch":
                if (input.query) wrap.appendChild(chip(input.query, "chip-cmd"));
                break;
            case "TodoWrite":
                if (input.todos && Array.isArray(input.todos)) {
                    wrap.appendChild(kvChip("items", input.todos.length));
                }
                break;
            default:
                // Generic: show first 2 key-value pairs
                var keys = Object.keys(input).slice(0, 2);
                for (var k of keys) {
                    wrap.appendChild(kvChip(k, input[k]));
                }
        }
        return wrap.children.length > 0 ? wrap : null;
    }

    var currentPlanSteps = [];
    var currentChecklistItems = [];
    var checklistSource = ""; // "plan" | "todos" — tracks which event source owns the checklist

    function showPlan(steps, planFile, planText, skipChecklist, showButtons, planTitle) {
        showButtons = showButtons !== false; // default to true
        currentPlanSteps = [...steps];
        // Remove any previous plan block so only one #active-plan exists at a time
        var oldPlan = document.getElementById("active-plan");
        if (oldPlan) oldPlan.remove();
        var bubble = getOrCreateBubble();
        var block = document.createElement("div"); block.className = "plan-block plan-block--tab"; block.id = "active-plan";

        // Plan header: single-row with title, filename, and inline action buttons
        var html = `<div class="plan-tab-row">`;
        html += `<div class="plan-title">Plan</div>`;
        var displayTitle = planTitle || "";
        if (!displayTitle && planText) {
            var headingMatch = planText.match(/^#+\s+(.+)/m);
            if (headingMatch) {
                displayTitle = headingMatch[1].replace(/^(Implementation Plan|Plan|Audit)[:\s\u2014-]*/i, "").trim();
            }
        }
        if (!displayTitle && planFile) {
            displayTitle = planFile.split('/').pop().replace(/\.md$/, '');
        }
        if (displayTitle) {
            html += `<div class="plan-filename" title="${BX.escapeHtml(displayTitle)}">${BX.escapeHtml(displayTitle)}</div>`;
        }
        if (planFile) {
            html += `<button type="button" class="plan-open-tab" title="Open plan in editor" aria-label="Open in editor" data-path="${BX.escapeHtml(planFile)}">${toolActionIcon("open")}</button>`;
        }
        if (showButtons) {
            html += `<div class="plan-actions">`;
            html += `<button type="button" class="action-btn primary plan-build-btn">\u25B6 Build</button>`;
            html += `<button type="button" class="action-btn secondary plan-feedback-btn">Feedback</button>`;
            html += `<button type="button" class="action-btn danger plan-reject-btn">\u2715 Reject</button>`;
            html += `</div>`;
        }
        html += `</div>`;

        block.innerHTML = html;
        block.appendChild(BX.makeCopyBtn(planText || steps.join("\n")));
        bubble.appendChild(block);

        var openBtn = block.querySelector(".plan-open-tab");
        if (openBtn) {
            openBtn.addEventListener("click", function() {
                var path = openBtn.dataset.path;
                if (path && typeof BX.openFile === "function") BX.openFile(path);
            });
        }

        var buildBtn = block.querySelector(".plan-build-btn");
        if (buildBtn) {
            var stepsForBuild = [...steps]; // capture at creation time, not module-level
            buildBtn.addEventListener("click", function() {
                buildBtn.closest(".plan-actions").remove();
                var editorCtx = BX.gatherEditorContext();
                BX.send({ type: "build", steps: stepsForBuild, ...(editorCtx ? { context: editorCtx } : {}) });
                BX.setRunning(true);
            });
        }
        var fbBtn = block.querySelector(".plan-feedback-btn");
        if (fbBtn) fbBtn.addEventListener("click", () => showPlanFeedbackInput());
        var rejectBtn = block.querySelector(".plan-reject-btn");
        if (rejectBtn) rejectBtn.addEventListener("click", function() {
            block.querySelector(".plan-actions")?.remove();
            BX.send({ type: "reject_plan" });
        });

        if (!skipChecklist) {
            checklistSource = "plan";
            currentChecklistItems = steps.map((s, i) => ({ id: String(i + 1), content: s, status: "pending" }));
            showAgentChecklist(currentChecklistItems);
        }
        BX.scrollChat();
    }

    function showPlanFeedbackInput() {
        var existing = document.querySelector(".plan-feedback-box");
        if (existing) { existing.querySelector("textarea")?.focus(); return; }
        var planBlock = document.getElementById("active-plan");
        if (!planBlock) return;
        var box = document.createElement("div");
        box.className = "plan-feedback-box";
        box.innerHTML = `
            <div class="plan-feedback-label">What would you like changed?</div>
            <textarea class="plan-feedback-input" rows="3" placeholder="e.g. Don\u2019t modify auth.py, use the existing middleware instead\u2026"></textarea>
            <div class="plan-feedback-actions">
                <button class="action-btn primary plan-feedback-send">Re-plan</button>
                <button class="action-btn secondary plan-feedback-cancel">Cancel</button>
            </div>
        `;
        planBlock.appendChild(box);
        var ta = box.querySelector("textarea");
        ta.focus();
        box.querySelector(".plan-feedback-send").addEventListener("click", function() {
            var text = ta.value.trim();
            if (!text) return;
            box.remove();
            BX.send({ type: "plan_feedback", feedback: text });
            BX.setRunning(true);
        });
        box.querySelector(".plan-feedback-cancel").addEventListener("click", () => box.remove());
    }

    function hidePlan() {
        currentPlanSteps = [];
        document.querySelectorAll(".plan-block").forEach(el => el.remove());
    }

    // ── Todos: only in bottom sticky dropdown (no in-chat checklist) ───
    function showAgentChecklist(todos, progress) {
        currentChecklistItems = Array.isArray(todos) ? [...todos] : [];
        var block = document.getElementById("agent-checklist");
        if (block) block.remove();
        updateStickyTodoBar(progress);
    }

    function updateStickyTodoBar(progress) {
        if (!$stickyTodoBar || !$stickyTodoCount || !$stickyTodoList) return;
        var items = currentChecklistItems || [];
        if (items.length === 0) {
            $stickyTodoBar.classList.add("hidden");
            if ($stickyTodoDropdown) $stickyTodoDropdown.classList.add("hidden");
            $stickyTodoList.classList.add("hidden");
            if ($stickyTodoAddRow) $stickyTodoAddRow.classList.add("hidden");
            $stickyTodoBar.removeAttribute("data-expanded");
            BX.updateStripVisibility();
            return;
        }
        $stickyTodoBar.classList.remove("hidden");
        BX.updateStripVisibility();

        // Count completed/in-progress
        var completed = items.filter(t => (t.status || "").toLowerCase() === "completed").length;
        $stickyTodoCount.textContent = `${completed}/${items.length}`;

        // Update inline progress bar in the pill
        var pct = items.length > 0 ? Math.round((completed / items.length) * 100) : 0;
        var fill = $stickyTodoBar.querySelector(".ss-progress-fill");
        if (fill) fill.style.width = pct + "%";

        $stickyTodoList.innerHTML = items.map(function(t) {
            var status = (t.status || "pending").toLowerCase();
            var content = (t.content || "").trim() || "\u2014";
            var statusChar = status === "completed" ? "\u2713" : status === "in_progress" ? "\u25B6" : "\u25CB";
            var cls = status === "completed" ? "done" : status === "in_progress" ? "active" : "";
            var todoId = t.id != null ? String(t.id) : "";
            return `<div class="sticky-todo-item ${cls}" data-todo-id="${BX.escapeHtml(todoId)}">
                <span class="sticky-todo-status">${statusChar}</span>
                <span class="sticky-todo-content" title="${BX.escapeHtml(content)}">${BX.escapeHtml(content)}</span>
                <button type="button" class="sticky-todo-remove" title="Remove" aria-label="Remove">\u00D7</button>
            </div>`;
        }).join("");
        var isExpanded = $stickyTodoBar.getAttribute("data-expanded") === "true";
        if ($stickyTodoDropdown) $stickyTodoDropdown.classList.toggle("hidden", !isExpanded);
        $stickyTodoList.classList.toggle("hidden", !isExpanded);
        if ($stickyTodoAddRow) $stickyTodoAddRow.classList.toggle("hidden", !isExpanded);
        $stickyTodoList.querySelectorAll(".sticky-todo-remove").forEach(function(btn) {
            btn.addEventListener("click", function(e) {
                e.stopPropagation();
                var item = btn.closest(".sticky-todo-item");
                var id = item && item.getAttribute("data-todo-id");
                if (id != null) BX.send({ type: "remove_todo", id });
            });
        });
    }

    function updatePlanStepProgress(stepNum, totalSteps) {
        if (checklistSource !== "plan") return;
        if (currentChecklistItems.length === 0) return;
        currentChecklistItems.forEach(function(item, i) {
            var num = i + 1;
            item.status = num < stepNum ? "completed" : num === stepNum ? "in_progress" : "pending";
        });
        showAgentChecklist(currentChecklistItems, { stepNum, totalSteps });
    }

    function markPlanComplete() {
        if (checklistSource !== "plan") return;
        currentChecklistItems.forEach(function(item) { item.status = "completed"; });
        showAgentChecklist(currentChecklistItems);
    }

    function showDiffs(files, isCumulative) {
        // Remove any previous cumulative diff container so we replace, not duplicate
        var existing = document.getElementById("cumulative-diff-container");
        if (existing) existing.remove();

        var bubble = getOrCreateBubble();
        var container = document.createElement("div");
        container.id = "cumulative-diff-container";
        container.className = "cumulative-diff-container";

        // Summary header
        var totalAdds = files.reduce((s, f) => s + (f.additions || 0), 0);
        var totalDels = files.reduce((s, f) => s + (f.deletions || 0), 0);
        var newFiles = files.filter(f => f.label === "new file").length;
        var modFiles = files.length - newFiles;
        var summaryText = `${files.length} file${files.length !== 1 ? "s" : ""} changed`;
        var parts = [];
        if (modFiles > 0) parts.push(`${modFiles} modified`);
        if (newFiles > 0) parts.push(`${newFiles} new`);
        if (parts.length) summaryText += ` (${parts.join(", ")})`;

        var summary = document.createElement("div");
        summary.className = "diff-summary-header";
        summary.innerHTML = `<span class="diff-summary-text">${BX.escapeHtml(summaryText)}</span><span class="diff-summary-right"><button class="diff-toggle-all-btn" title="Expand all">Expand all</button><span class="diff-stats"><span class="add">+${totalAdds}</span><span class="del">-${totalDels}</span></span></span>`;
        var toggleAllBtn = summary.querySelector(".diff-toggle-all-btn");
        toggleAllBtn.addEventListener("click", function() {
            var blocks = container.querySelectorAll(".diff-block");
            var allExpanded = Array.from(blocks).every(function(b) { return !b.classList.contains("collapsed"); });
            blocks.forEach(function(b) { b.classList.toggle("collapsed", allExpanded); });
            toggleAllBtn.textContent = allExpanded ? "Expand all" : "Collapse all";
        });
        container.appendChild(summary);

        files.forEach(function(f) {
            var block = document.createElement("div"); block.className = "diff-block collapsed";
            var labelCls = f.label === "new file" ? "new-file" : "modified";
            block.innerHTML = `<div class="diff-file-header"><div style="display:flex;align-items:center;gap:8px"><span class="diff-chevron">\u25B6</span><span class="diff-file-name">${BX.escapeHtml(f.path)}</span><span class="diff-file-label ${labelCls}">${BX.escapeHtml(f.label)}</span></div><div class="diff-stats"><span class="add">+${f.additions}</span><span class="del">-${f.deletions}</span></div></div><div class="diff-content">${renderDiff(f.diff)}</div>`;

            block.querySelector(".diff-file-header").addEventListener("click", () => block.classList.toggle("collapsed"));

            block.querySelector(".diff-file-name").style.cursor = "pointer";
            block.querySelector(".diff-file-name").addEventListener("click", function(e) { e.stopPropagation(); BX.openDiffForFile(f.path); });

            block.appendChild(BX.makeCopyBtn(f.diff));
            container.appendChild(block);
            BX.markFileModified(f.path);
        });

        bubble.appendChild(container);

        var fileCount = files.length;
        BX.showActionBar([
            { label: "Keep", cls: "success", onClick: function() { BX.send({type:"keep"}); }},
            { label: "Revert", cls: "danger", onClick: function() { BX.send({type:"revert"}); }},
        ]);
        BX.scrollChat();
    }

    function renderDiff(text) {
        if (!text) return "";
        return text.split("\n").map(function(l) {
            var c = "ctx";
            if (l.startsWith("+++") || l.startsWith("---")) c = "hunk";
            else if (l.startsWith("@@")) c = "hunk";
            else if (l.startsWith("+")) c = "add";
            else if (l.startsWith("-")) c = "del";
            return `<div class="diff-line ${c}">${BX.escapeHtml(l)}</div>`;
        }).join("");
    }

    function showError(text) {
        var bubble = getOrCreateBubble();
        var el = document.createElement("div"); el.className = "error-msg";
        el.textContent = text; el.appendChild(BX.makeCopyBtn(text));
        bubble.appendChild(el); BX.scrollChat();
    }
    function showInfo(text) {
        var div = document.createElement("div"); div.className = "info-msg"; div.textContent = text;
        $chatMessages.appendChild(div); BX.scrollChat();
        // Auto-fade out info messages after 5 seconds (including checkpoint messages)
        _scheduleInfoFadeOut(div, 5000);
    }

    function showClarifyingQuestion(question, context, tool_use_id, options) {
        var wrap = document.createElement("div");
        wrap.className = "clarifying-question-box";
        var optionsHtml = (options && options.length)
            ? `<div class="clarifying-question-options">${options.map((opt) => `<button type="button" class="clarifying-question-option" data-answer="${BX.escapeHtml(opt)}">${BX.escapeHtml(opt)}</button>`).join("")}</div>`
            : "";
        wrap.innerHTML = `<div class="clarifying-question-label">\u2753 Agent is asking:</div><div class="clarifying-question-text">${BX.escapeHtml(question)}</div>${context ? `<div class="clarifying-question-context">${BX.escapeHtml(context)}</div>` : ""}${optionsHtml}<textarea class="clarifying-question-input" rows="2" placeholder="Type your answer..."></textarea><button type="button" class="clarifying-question-send">Send answer</button>`;
        var ta = wrap.querySelector(".clarifying-question-input");
        var btn = wrap.querySelector(".clarifying-question-send");

        function submitAnswer(answer) {
            if (!answer) return;
            BX.send({ type: "user_answer", answer: answer, tool_use_id: tool_use_id });
            wrap.classList.add("answered");
            wrap.querySelectorAll(".clarifying-question-input, .clarifying-question-send, .clarifying-question-option").forEach(function(el) { if (el) el.style.display = "none"; });
            var optsEl = wrap.querySelector(".clarifying-question-options");
            if (optsEl) optsEl.style.display = "none";
            var done = document.createElement("div"); done.className = "clarifying-question-done"; done.textContent = "\u2713 Sent: " + (answer.length > 60 ? answer.slice(0, 60) + "..." : answer); wrap.appendChild(done);
        }

        wrap.querySelectorAll(".clarifying-question-option").forEach(function(optBtn) {
            optBtn.addEventListener("click", () => submitAnswer(optBtn.getAttribute("data-answer") || optBtn.textContent));
        });
        btn.addEventListener("click", () => submitAnswer(ta.value.trim()));
        ta.addEventListener("keydown", function(e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitAnswer(ta.value.trim()); } });
        $chatMessages.appendChild(wrap);
        BX.scrollChat();
        ta.focus();
    }
    var _phaseTickIntervals = {};
    function showPhase(name) {
        if (name === "direct") return;
        var bubble = getOrCreateBubble();
        var div = document.createElement("div"); div.className = "phase-indicator"; div.id = `phase-${name}`;
        div.dataset.startedAt = String(Date.now());
        var label = phaseLabel(name);
        div.innerHTML = `<span class="phase-label">${BX.escapeHtml(label)}</span>`;
        bubble.appendChild(div);
        BX.scrollChat();
        _phaseTickIntervals[name] = setInterval(function() {
            if (div.classList.contains("done")) { clearInterval(_phaseTickIntervals[name]); return; }
            var s = Math.max(0, Math.round((Date.now() - Number(div.dataset.startedAt)) / 1000));
            var lbl = div.querySelector(".phase-label");
            if (lbl && s > 0) {
                var cur = lbl.textContent.replace(/\s+\d+s$/, "");
                lbl.textContent = `${cur} ${s}s`;
            }
        }, 1000);
    }
    function endPhase(name, elapsed) {
        if (_phaseTickIntervals[name]) { clearInterval(_phaseTickIntervals[name]); delete _phaseTickIntervals[name]; }
        var el = document.getElementById(`phase-${name}`);
        if (el) {
            el.classList.add("done");
            var lbl = el.querySelector(".phase-label");
            if (lbl) lbl.textContent = `${phaseDoneLabel(name)} \u2014 ${elapsed}s`;
        }
    }
    function phaseLabel(n) {
        var m = {plan:"Planning\u2026",build:"Building\u2026",direct:"Running\u2026"}[n];
        if (m) return m;
        var bp = n.match(/^build_phase_(\d+)$/);
        if (bp) return "Building phase " + bp[1] + "\u2026";
        return n;
    }
    function phaseDoneLabel(n) {
        var m = {plan:"Planned",build:"Built",direct:"Completed"}[n];
        if (m) return m;
        var bp = n.match(/^build_phase_(\d+)$/);
        if (bp) return "Phase " + bp[1] + " complete";
        return n;
    }

    var _scoutTickInterval = null;
    var _scoutStartedAt = 0;
    function showScoutProgress(text) {
        // If a plan phase is active, update its label instead of showing a separate scout block
        var planPhase = document.getElementById("phase-plan");
        if (planPhase && !planPhase.classList.contains("done")) {
            var lbl = planPhase.querySelector(".phase-label");
            if (lbl) lbl.textContent = text || "Planning\u2026";
            BX.scrollChat();
            return;
        }
        if (!BX.scoutEl) {
            var bubble = getOrCreateBubble();
            BX.scoutEl = document.createElement("div"); BX.scoutEl.className = "scout-block";
            BX.scoutEl.innerHTML = `<span class="scout-text"></span>`;
            _scoutStartedAt = Date.now();
            bubble.appendChild(BX.scoutEl);
            _scoutTickInterval = setInterval(function() {
                if (!BX.scoutEl || BX.scoutEl.classList.contains("scout-done")) { clearInterval(_scoutTickInterval); return; }
                var s = Math.max(0, Math.round((Date.now() - _scoutStartedAt) / 1000));
                var textEl = BX.scoutEl.querySelector(".scout-text");
                if (textEl && s > 0) {
                    var base = textEl.textContent.replace(/\s+\d+s$/, "");
                    textEl.textContent = `${base} ${s}s`;
                }
            }, 1000);
        }
        var textEl = BX.scoutEl.querySelector(".scout-text");
        if (textEl) textEl.textContent = text || "Scanning\u2026";
        BX.scrollChat();
    }
    function endScout() {
        if (_scoutTickInterval) { clearInterval(_scoutTickInterval); _scoutTickInterval = null; }
        if (BX.scoutEl) {
            var s = Math.max(0, Math.round((Date.now() - _scoutStartedAt) / 1000));
            BX.scoutEl.classList.add("scout-done");
            var textEl = BX.scoutEl.querySelector(".scout-text");
            if (textEl) textEl.textContent = s > 0 ? `Scanned \u2014 ${s}s` : "\u2713 Scan complete";
            BX.scoutEl = null;
        }
    }

    // ================================================================


    // ── Exports ────────────────────────────────────────────────
    BX.addUserMessage = addUserMessage;
    BX.addGuidanceMessage = addGuidanceMessage;
    BX.addAssistantMessage = addAssistantMessage;
    BX.getOrCreateBubble = getOrCreateBubble;
    BX.updateThinkingHeader = updateThinkingHeader;
    BX.createThinkingBlock = createThinkingBlock;
    BX.appendThinkingContent = appendThinkingContent;
    BX.finishThinking = finishThinking;
    BX.formatClock = formatClock;
    BX.toolGroupKey = toolGroupKey;
    BX.toolActionIcon = toolActionIcon;
    BX.toolCanOpenFile = toolCanOpenFile;
    BX.toolFollowupPrompt = toolFollowupPrompt;
    BX.runFollowupPrompt = runFollowupPrompt;
    BX.openFileAt = openFileAt;
    BX.parseLocationLine = parseLocationLine;
    BX.buildEditPreviewDiff = buildEditPreviewDiff;
    BX.failureSummary = failureSummary;
    BX.makeProgressiveBody = makeProgressiveBody;
    BX.formatToolOutputBody = formatToolOutputBody;
    BX.renderStructuredOutput = renderStructuredOutput;
    BX.makeLocationList = makeLocationList;
    BX.makeReadFilePreview = makeReadFilePreview;
    BX.renderToolOutput = renderToolOutput;
    BX.updateToolGroupHeader = updateToolGroupHeader;
    BX.maybeAutoFollow = maybeAutoFollow;
    BX.addToolCallPlaceholder = addToolCallPlaceholder;
    BX.updateToolInputProgress = updateToolInputProgress;
    BX.finalizeToolCallPlaceholder = finalizeToolCallPlaceholder;
    BX.addToolCall = addToolCall;
    BX.addToolResult = addToolResult;
    BX.appendCommandOutput = appendCommandOutput;
    BX.condensePath = condensePath;
    BX.readFileDisplayString = readFileDisplayString;
    BX.countFilesAndMatchesInSearchOutput = countFilesAndMatchesInSearchOutput;
    BX.toolLabel = toolLabel;
    BX.normalizedToolName = normalizedToolName;
    BX.toolTitle = toolTitle;
    BX.toolDesc = toolDesc;
    BX.toolDescForHeader = toolDescForHeader;
    BX.webToolLinkHtml = webToolLinkHtml;
    BX.toolIcon = toolIcon;
    BX.formatToolInput = formatToolInput;
    BX.showPlan = showPlan;
    BX.showPlanFeedbackInput = showPlanFeedbackInput;
    BX.hidePlan = hidePlan;
    BX.showAgentChecklist = showAgentChecklist;
    BX.updateStickyTodoBar = updateStickyTodoBar;
    BX.updatePlanStepProgress = updatePlanStepProgress;
    BX.markPlanComplete = markPlanComplete;
    BX.showDiffs = showDiffs;
    BX.renderDiff = renderDiff;
    BX.showError = showError;
    BX.showInfo = showInfo;
    BX.showClarifyingQuestion = showClarifyingQuestion;
    BX.showPhase = showPhase;
    BX.endPhase = endPhase;
    BX.phaseLabel = phaseLabel;
    BX.phaseDoneLabel = phaseDoneLabel;
    BX.showScoutProgress = showScoutProgress;
    BX.endScout = endScout;

})(window.BX);
