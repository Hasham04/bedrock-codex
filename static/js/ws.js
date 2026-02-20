/* ============================================================
   Bedrock Codex — WebSocket + Input
   Reconnect logic, event handling, command/task submission
   ============================================================ */
(function (BX) {
    "use strict";

    // ── DOM ref aliases ─────────────────────────────────────────
    var $chatMessages      = BX.$chatMessages;
    var $input             = BX.$input;
    var $connStatus        = BX.$connStatus;
    var $modelName         = BX.$modelName;
    var $conversationTitle = BX.$conversationTitle;
    var $workingDir        = BX.$workingDir;
    var $agentSelect       = BX.$agentSelect;
    var $tokenCount        = BX.$tokenCount;
    var $attachImageBtn    = BX.$attachImageBtn;
    var $imageInput        = BX.$imageInput;
    var $sendBtn           = BX.$sendBtn;
    var $cancelBtn         = BX.$cancelBtn;
    var $resetBtn          = BX.$resetBtn;
    var $newAgentBtn       = BX.$newAgentBtn;
    var $chatMenuDropdown  = BX.$chatMenuDropdown;

    // ── Reference-type state aliases ────────────────────────────
    var toolRunById             = BX.toolRunById;
    var openTabs                = BX.openTabs;
    var modifiedFiles           = BX.modifiedFiles;
    var pendingImages           = BX.pendingImages;
    var fileChangesThisSession  = BX.fileChangesThisSession;
    var sessionCumulativeStats  = BX.sessionCumulativeStats;

    // ── Module-private reconnect state ──────────────────────────
    var _preventReconnect = false;
    var _isFirstConnect   = true;
    var _reconnectAttempt = 0;
    var _reconnectBase    = 1000;
    var _reconnectMax     = 30000;
    var _reconnectTimer   = null;

    // ================================================================
    // WEBSOCKET — with exponential backoff and reconnect banner
    // ================================================================

    function _getReconnectDelay() {
        var delay = Math.min(_reconnectBase * Math.pow(2, _reconnectAttempt), _reconnectMax);
        var jitter = delay * 0.3 * Math.random();
        return delay + jitter;
    }

    function _showReconnectBanner(msg) {
        var banner = document.getElementById("reconnect-banner");
        if (!banner) {
            banner = document.createElement("div");
            banner.id = "reconnect-banner";
            $chatMessages.parentElement.insertBefore(banner, $chatMessages);
        }
        banner.innerHTML = '<span class="reconnect-spinner"></span> ' +
            (msg || (BX.isRunning
                ? "Connection lost. Reconnecting — agent is still working on the server\u2026"
                : "Connection lost. Reconnecting..."));
        banner.style.display = "flex";
    }

    function _hideReconnectBanner() {
        var banner = document.getElementById("reconnect-banner");
        if (banner) banner.style.display = "none";
    }

    function connect() {
        _preventReconnect = false;
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
        if (!BX.currentSessionId) BX.currentSessionId = BX.loadPersistedSessionId();
        var proto = location.protocol === "https:" ? "wss:" : "ws:";
        var wsUrl = proto + "//" + location.host + "/ws";
        if (BX.currentSessionId) {
            wsUrl += "?session_id=" + encodeURIComponent(BX.currentSessionId);
        }
        BX.ws = new WebSocket(wsUrl);
        $connStatus.className = "status-dot connecting"; $connStatus.title = "Connecting\u2026";

        BX.ws.onopen = function () {
            $connStatus.className = "status-dot connected"; $connStatus.title = "Connected";
            _reconnectAttempt = 0;
            _hideReconnectBanner();
        };
        BX.ws.onclose = function () {
            $connStatus.className = "status-dot disconnected"; $connStatus.title = "Disconnected";
            if (!_preventReconnect) {
                _isFirstConnect = false;
                _showReconnectBanner();
                var delay = _getReconnectDelay();
                _reconnectAttempt++;
                _reconnectTimer = setTimeout(connect, delay);
            }
        };
        BX.ws.onerror = function () { $connStatus.className = "status-dot disconnected"; };
        BX.ws.onmessage = function (evt) { try { handleEvent(JSON.parse(evt.data)); } catch (e) { console.error("[BX] Event handler error:", e, evt.data); } };
    }

    function disconnectWs() {
        _preventReconnect = true;
        _isFirstConnect = true;
        _reconnectAttempt = 0;
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
        if (BX.ws) {
            BX.ws.onclose = null;
            BX.ws.onerror = null;
            BX.ws.close();
            BX.ws = null;
        }
    }

    function gatherEditorContext() {
        var ctx = {};
        if (BX.activeTab) {
            ctx.activeFile = { path: BX.activeTab };
            if (BX.monacoInstance) {
                var pos = BX.monacoInstance.getPosition();
                if (pos) ctx.activeFile.cursorLine = pos.lineNumber;
                var sel = BX.monacoInstance.getSelection();
                if (sel && !sel.isEmpty()) {
                    ctx.selectedText = BX.monacoInstance.getModel().getValueInRange(sel);
                    if (ctx.selectedText.length > 2000) ctx.selectedText = ctx.selectedText.slice(0, 2000) + "\u2026";
                }
            }
        }
        if (openTabs.size > 0) {
            ctx.openFiles = Array.from(openTabs.keys());
        }
        return Object.keys(ctx).length > 0 ? ctx : undefined;
    }

    function send(obj) {
        if (!BX.ws || BX.ws.readyState !== WebSocket.OPEN) return false;
        BX.ws.send(JSON.stringify(obj));
        return true;
    }

    function _textMorphRender() {
        BX._textRenderRAF = null;
        if (!BX.currentTextEl || !BX._textDirty) return;
        BX._textDirty = false;
        var display = BX.currentTextBuffer
            .replace(/<updated_plan>[\s\S]*?<\/updated_plan>/g, "").trim();
        if (!display) return;
        var html = BX.renderMarkdown(display);
        var temp = document.createElement("div");
        temp.className = BX.currentTextEl.className;
        temp.innerHTML = html;
        morphdom(BX.currentTextEl, temp, {
            childrenOnly: false,
            onBeforeElUpdated: function (fromEl, toEl) {
                if (fromEl.isEqualNode(toEl)) return false;
                return true;
            }
        });
        BX.currentTextEl.querySelectorAll("pre code").forEach(function (b) {
            if (!b._hlDone && typeof hljs !== "undefined") {
                hljs.highlightElement(b);
                b._hlDone = true;
            }
        });
        BX.scrollChat();
    }

    function _scheduleTextRender() {
        if (!BX._textRenderRAF) {
            BX._textRenderRAF = requestAnimationFrame(_textMorphRender);
        }
    }

    // ================================================================
    // EVENT HANDLER
    // ================================================================

    function handleEvent(evt) {
        switch (evt.type) {
            case "init": {
                BX.setRunning(false);
                $modelName.textContent = evt.model_name || "?";
                BX.currentSessionId = evt.session_id || BX.currentSessionId;
                BX.persistSessionId(BX.currentSessionId);
                if (evt.session_id) {
                    fileChangesThisSession.clear();
                    BX.sessionStartTime = BX.sessionStartTime || Date.now();
                    BX.updateFileChangesDropdown();
                }
                if ($conversationTitle) $conversationTitle.textContent = evt.session_name || "New conversation";
                if (evt.input_tokens !== undefined && evt.output_tokens !== undefined) {
                    var parts = ["In: " + BX.formatTokens(evt.input_tokens), "Out: " + BX.formatTokens(evt.output_tokens)];
                    if (evt.cache_read) parts.push("Cache: " + BX.formatTokens(evt.cache_read));
                    $tokenCount.textContent = parts.join(" | ");
                    $tokenCount.title = "Input: " + (evt.input_tokens || 0).toLocaleString() + " | Output: " + (evt.output_tokens || 0).toLocaleString() + " | Cache: " + (evt.cache_read || 0).toLocaleString();
                } else {
                    $tokenCount.textContent = BX.formatTokens(evt.total_tokens || 0) + " tokens";
                    $tokenCount.title = "Total tokens used";
                }
                $workingDir.textContent = evt.working_directory || "";
                var gaugeFill = document.getElementById("context-gauge-fill");
                var gauge = document.getElementById("context-gauge");
                if (_isFirstConnect) {
                    if (gaugeFill) { gaugeFill.style.width = "0%"; gaugeFill.className = "context-gauge-fill"; }
                    if (gauge) gauge.title = "Context window usage";
                }
                BX.loadAgentSessions();
                if (_isFirstConnect) {
                    toolRunById.clear();
                    $chatMessages.innerHTML = "";
                    if ($conversationTitle) $conversationTitle.textContent = "New conversation";
                }
                BX.isReplaying = false;
                BX.loadTree();
                BX.startModifiedFilesPolling();
                BX.updateModifiedFilesBar();
                _isFirstConnect = false;
                break;
            }
            case "thinking_start":
                if (BX.scoutEl && !BX.scoutEl.classList.contains("scout-done")) BX.endScout();
                BX.currentThinkingEl = BX.createThinkingBlock();
                break;
            case "thinking":
                BX.appendThinkingContent(BX.currentThinkingEl, evt.content || "");
                break;
            case "thinking_end": BX.finishThinking(BX.currentThinkingEl); BX.currentThinkingEl = null; break;
            case "text_start":
                BX.currentTextEl = null;
                BX.currentTextBuffer = "";
                BX._textDirty = false;
                if (BX._textRenderRAF) { cancelAnimationFrame(BX._textRenderRAF); BX._textRenderRAF = null; }
                break;
            case "text":
                BX.currentTextBuffer += evt.content || "";
                if (!BX.currentTextEl) {
                    var b = BX.getOrCreateBubble();
                    BX.currentTextEl = document.createElement("div");
                    BX.currentTextEl.className = "text-content";
                    b.appendChild(BX.currentTextEl);
                }
                BX._textDirty = true;
                _scheduleTextRender();
                break;
            case "text_end":
                if (BX._textRenderRAF) { cancelAnimationFrame(BX._textRenderRAF); BX._textRenderRAF = null; }
                if (BX.currentTextEl && BX.currentTextBuffer) {
                    var _display = BX.currentTextBuffer.replace(/<updated_plan>[\s\S]*?<\/updated_plan>/g, "").trim();
                    BX.currentTextEl.innerHTML = BX.renderMarkdown(_display);
                    BX.currentTextEl.querySelectorAll("pre code").forEach(function (b) { if (typeof hljs !== "undefined") hljs.highlightElement(b); });
                    var bubble = BX.currentTextEl.closest(".msg-bubble");
                    if (bubble && !bubble.querySelector(":scope > .copy-btn")) bubble.appendChild(BX.makeCopyBtn(BX.currentTextBuffer));
                }
                BX.currentTextEl = null; BX.currentTextBuffer = "";
                BX._textDirty = false;
                BX.scrollChat();
                break;
            case "tool_use_start": {
                var _tusName = BX.normalizedToolName(evt.data?.name || "tool");
                var _tusId = evt.data?.id || null;
                BX.lastToolBlock = BX.addToolCallPlaceholder(_tusName, _tusId);
                break;
            }
            case "tool_input_delta": {
                var _tidId = evt.data?.id;
                var _tidEl = (_tidId && toolRunById.get(String(_tidId))) || BX.lastToolBlock;
                if (_tidEl) BX.updateToolInputProgress(_tidEl, evt.data?.bytes || 0, evt.data?.path || "");
                break;
            }
            case "tool_call": {
                var _tcId = evt.data?.id || evt.data?.tool_use_id || null;
                var _tcExisting = _tcId ? toolRunById.get(String(_tcId)) : null;
                if (_tcExisting) {
                    BX.finalizeToolCallPlaceholder(
                        _tcExisting,
                        evt.data?.name || "tool",
                        evt.data?.input || evt.data || {}
                    );
                    BX.lastToolBlock = _tcExisting.closest(".tool-block") || _tcExisting;
                } else {
                    BX.lastToolBlock = BX.addToolCall(
                        evt.data?.name || "tool",
                        evt.data?.input || evt.data || {},
                        _tcId
                    );
                }
                var toolName = evt.data?.name;
                if (toolName === "TodoWrite" && evt.data?.input?.todos && Array.isArray(evt.data.input.todos)) {
                    BX.checklistSource = "todos";
                    var normalized = evt.data.input.todos.map(function (t, i) {
                        return {
                            id: t.id != null ? t.id : String(i + 1),
                            content: t.content || "",
                            status: (t.status || "pending").toLowerCase()
                        };
                    });
                    BX.showAgentChecklist(normalized);
                }
                var _normalized = BX.normalizedToolName(toolName, evt.data?.input);
                if (_normalized === "Write" || _normalized === "Edit" || toolName === "symbol_edit") {
                    var p = evt.data?.input?.path;
                    if (p) { BX.markFileModified(p); BX.reloadFileInEditor(p); }
                }
                break;
            }
            case "tool_result":
                {
                    var _trId = evt.data?.tool_use_id || evt.data?.id;
                    var runEl = (_trId && toolRunById.get(String(_trId))) || BX.lastToolBlock;
                    if (runEl && runEl.classList && runEl.classList.contains("tool-block")) {
                        var list = runEl.querySelector(".tool-run-list");
                        if (list && list.lastElementChild) runEl = list.lastElementChild;
                    }
                    BX.addToolResult(evt.content || "", evt.data?.success !== false, runEl, evt.data);
                    var todoList = evt.data?.todos ?? evt.data?.data?.todos;
                    var isTodoWrite = evt.data?.tool_name === "TodoWrite" || (runEl && (runEl.dataset.toolName === "TodoWrite" || runEl.dataset.toolName === "todo_write"));
                    if (isTodoWrite && Array.isArray(todoList)) {
                        BX.checklistSource = "todos";
                        var tdList = todoList.map(function (t, i) {
                            return {
                                id: t.id != null ? t.id : String(i + 1),
                                content: t.content || "",
                                status: (String(t.status || "pending")).toLowerCase()
                            };
                        });
                        BX.showAgentChecklist(tdList);
                    }
                }
                {
                    var runEl2 = (_trId && toolRunById.get(String(_trId))) || BX.lastToolBlock;
                    if (runEl2) {
                        var tn = runEl2.dataset.toolName;
                        var isWrite = tn === "Write" || tn === "write_file";
                        var isEdit = tn === "Edit" || tn === "edit_file" || tn === "symbol_edit";
                        if (isWrite || isEdit) {
                            var path = runEl2.dataset.path;
                            if (path && evt.data?.success !== false) {
                                BX.reloadFileInEditor(path);
                                var inputData = runEl2._toolInput || {};
                                if (isWrite) {
                                    var contentLines = (inputData.file_text || inputData.content || "").split('\n').length;
                                    BX.trackFileChange(path, contentLines, 0);
                                } else {
                                    var oldStr = inputData.old_str || inputData.old_string || "";
                                    var newStr = inputData.new_str || inputData.new_string || "";
                                    var oldLineCount = oldStr ? oldStr.split('\n').length : 0;
                                    var newLineCount = newStr ? newStr.split('\n').length : 0;
                                    BX.trackFileChange(path, newLineCount, oldLineCount);
                                }
                            }
                        }
                        if (tn === "Bash" && evt.data?.success !== false) {
                            var inputData2 = runEl2._toolInput || {};
                            BX.detectFileDeletesFromBash(inputData2.command, evt.content || "");
                        }
                    }
                }
                if (_trId) {
                    toolRunById.delete(String(_trId));
                }
                BX.lastToolBlock = null;
                break;
            case "server_tool_use": {
                var stName = evt.data?.name || "web_search";
                var stQuery = evt.data?.input?.query || "";
                var stEl = BX.addToolCall("WebSearch", { query: stQuery }, evt.data?.id, { stream: false });
                break;
            }
            case "web_search_result": {
                var wsResults = evt.data?.content || [];
                var wsId = evt.data?.tool_use_id;
                var wsRunEl = wsId ? toolRunById.get(String(wsId)) : BX.lastToolBlock;
                if (wsRunEl) {
                    var snippets = wsResults
                        .filter(function (r) { return r.type === "web_search_result"; })
                        .map(function (r) { return (r.title || "") + "\n" + (r.url || "") + "\n" + (r.encrypted_content ? "(content)" : ""); })
                        .join("\n\n");
                    BX.addToolResult(snippets || "(search results)", true, wsRunEl, {});
                }
                break;
            }
            case "command_output":
                BX.appendCommandOutput(evt.data?.tool_use_id, evt.content || "", !!evt.data?.is_stderr);
                break;
            case "command_partial_failure":
                BX.showInfo(evt.content || "Potential command failure detected.");
                break;
            case "checkpoint_list":
                if (Array.isArray(evt.data?.checkpoints)) {
                    var rows = evt.data.checkpoints
                        .slice(0, 12)
                        .map(function (cp) { return cp.id + " (" + cp.file_count + " files) " + (cp.label || ""); });
                    BX.showInfo(rows.length ? "Checkpoints:\n" + rows.join("\n") : "No checkpoints available.");
                }
                break;
            case "checkpoint_restored":
                BX.showInfo("Rewound " + (evt.data?.count || 0) + " files from checkpoint " + (evt.data?.checkpoint_id || "latest") + ".");
                if (Array.isArray(evt.data?.paths)) {
                    evt.data.paths.slice(0, 20).forEach(function (p) { BX.reloadFileInEditor(p); });
                }
                break;
            case "checkpoint_created":
                if (evt.data?.checkpoint_id) {
                    BX.showInfo("Checkpoint");
                }
                break;
            case "checkpoint_error":
                BX.showError(evt.content || "Checkpoint rewind failed.");
                break;
            case "command_start":
                break;
            case "auto_approved": break;
            case "scout_start": BX.showScoutProgress("Scanning\u2026"); break;
            case "scout_progress": BX.showScoutProgress(evt.content); break;
            case "scout_end": BX.endScout(); break;
            case "phase_start":
                if (BX.scoutEl && !BX.scoutEl.classList.contains("scout-done")) BX.endScout();
                BX.showPhase(evt.content);
                break;
            case "user_question": BX.showClarifyingQuestion(evt.question || "", evt.context || "", evt.tool_use_id || "", evt.options); break;
            case "phase_end":
                BX.endPhase(evt.content, evt.elapsed || 0);
                if (evt.content === "build") BX.markPlanComplete();
                break;
            case "plan": case "phase_plan":
                BX.showPlan(
                    evt.steps || (evt.data && evt.data.steps) || [],
                    evt.plan_file || (evt.data && evt.data.plan_file) || null,
                    evt.plan_text || (evt.data && evt.data.plan_text) || "",
                    false,
                    !BX.isReplaying,
                    evt.plan_title || (evt.data && evt.data.plan_title) || ""
                );
                if (!BX.isReplaying) BX.setRunning(false);
                break;
            case "updated_plan":
                {
                    var steps = evt.steps || [];
                    var planFile = evt.plan_file || null;
                    var planText = evt.plan_text || "";
                    if (steps.length) {
                        BX.currentPlanSteps = steps.slice();
                        BX.currentChecklistItems = steps.map(function (s, i) {
                            return { id: String(i + 1), content: s, status: "pending" };
                        });
                        BX.showAgentChecklist(BX.currentChecklistItems);
                        BX.showPlan(steps, planFile, planText, true, !BX.isRunning, evt.plan_title || "");
                        var notif = document.createElement("div");
                        notif.className = "info-msg plan-updated-msg";
                        notif.textContent = "Plan updated \u2014 " + steps.length + " steps";
                        $chatMessages.appendChild(notif);
                        BX.scrollChat();
                    }
                }
                break;
            case "todos_updated": {
                BX.checklistSource = "todos";
                var tuList = evt.todos || (evt.data && evt.data.todos) || [];
                var tuNormalized = Array.isArray(tuList) ? tuList.map(function (t, i) {
                    return {
                        id: t.id != null ? t.id : String(i + 1),
                        content: t.content || "",
                        status: (String(t.status || "pending")).toLowerCase()
                    };
                }) : [];
                BX.showAgentChecklist(tuNormalized);
                break;
            }
            case "plan_step_progress":
                BX.updatePlanStepProgress(
                    evt.step || (evt.data && evt.data.step) || 1,
                    evt.total || (evt.data && evt.data.total) || 1
                );
                break;
            case "plan_rejected":
                BX.currentPlanSteps = [];
                BX.currentChecklistItems = [];
                BX.checklistSource = "";
                document.querySelectorAll(".plan-block").forEach(function (el) { el.remove(); });
                BX.updateStickyTodoBar();
                BX.showInfo("Plan rejected.");
                break;
            case "diff":
                BX.showDiffs(evt.files || [], !!evt.cumulative);
                BX.setRunning(false);
                BX.refreshTree();
                BX.updateModifiedFilesBar();
                break;
            case "no_changes": BX.showInfo("No file changes."); BX.setRunning(false); break;
            case "no_plan": BX.showInfo("Completed directly."); BX.setRunning(false); break;
            case "guidance_queued":
                BX._guidancePending = true;
                BX.showInfo("Guidance sent \u2014 agent will incorporate it.");
                break;
            case "guidance_applied":
                BX._guidancePending = false;
                BX.showInfo("Agent received your guidance.");
                break;
            case "guidance_interrupt":
                BX._stopThinkingTick();
                document.querySelectorAll(".thinking-block .thinking-spinner").forEach(function (el) { el.remove(); });
                document.querySelectorAll(".thinking-block").forEach(function (el) { BX.updateThinkingHeader(el, true); });
                BX.showInfo(evt.content || "Incorporating your guidance\u2026");
                break;
            case "done":
                BX.setRunning(false);
                if (evt.data) BX.updateTokenDisplay(evt.data);
                BX._stopThinkingTick();
                Object.keys(BX._phaseTickIntervals).forEach(function (k) { clearInterval(BX._phaseTickIntervals[k]); delete BX._phaseTickIntervals[k]; });
                document.querySelectorAll(".tool-block-loading").forEach(function (b) {
                    b.classList.remove("tool-block-loading");
                    var st = b.querySelector(".tool-status");
                    if (st && !st.classList.contains("tool-status-success") && !st.classList.contains("tool-status-error")) {
                        st.className = "tool-status tool-status-success";
                        st.title = "Done";
                        st.innerHTML = BX.toolActionIcon("done");
                    }
                });
                document.querySelectorAll(".phase-indicator:not(.done)").forEach(function (el) { el.classList.add("done"); });
                if (BX.scoutEl && !BX.scoutEl.classList.contains("scout-done")) BX.endScout();
                if (BX._guidancePending) {
                    BX.showInfo("⚠ Guidance was not incorporated — task completed before it could be applied.");
                    BX._guidancePending = false;
                }
                break;
            case "kept":
                BX.hideActionBar();
                BX.clearAllDiffDecorations();
                modifiedFiles.clear();
                fileChangesThisSession.clear();
                { var dc = document.getElementById("cumulative-diff-container"); if (dc) dc.remove(); }
                BX.showInfo("\u2713 Changes accepted \u2014 you can still revert later.");
                BX.refreshTree();
                BX.updateModifiedFilesBar();
                BX.updateFileChangesDropdown();
                break;
            case "reverted":
                BX.hideActionBar();
                BX.clearAllDiffDecorations();
                modifiedFiles.clear();
                fileChangesThisSession.clear();
                { var dc2 = document.getElementById("cumulative-diff-container"); if (dc2) dc2.remove(); }
                BX.showInfo("\u21A9 Reverted " + (evt.files || []).length + " file(s).");
                BX.refreshTree();
                BX.updateModifiedFilesBar();
                BX.updateFileChangesDropdown();
                BX.reloadAllModifiedFiles();
                break;
            case "clear_keep_revert":
                BX.hideActionBar();
                BX.clearAllDiffDecorations();
                break;
            case "reverted_to_step": {
                var rvFiles = evt.files || [];
                if (evt.no_checkpoint || (rvFiles.length === 0 && evt.step != null)) {
                    BX.showInfo("No checkpoint for step " + evt.step + " (e.g. after reconnect or step not yet completed).");
                } else {
                    BX.showInfo("\u21A9 Reverted to step " + evt.step + " (" + rvFiles.length + " file(s))");
                    BX.reloadAllModifiedFiles();
                }
                BX.refreshTree();
                BX.updateModifiedFilesBar();
                break;
            }

            case "cancelled":
                BX.showInfo("Cancelled.");
                BX.setRunning(false);
                BX.hideActionBar();
                document.querySelectorAll(".tool-status-running").forEach(function (el) {
                    el.className = "tool-status tool-status-error";
                    el.title = "Cancelled";
                    el.innerHTML = BX.toolActionIcon("stop");
                });
                document.querySelectorAll(".tool-stop-btn").forEach(function (el) { el.remove(); });
                if (BX.scoutEl) { BX.endScout(); }
                document.querySelectorAll(".phase-indicator:not(.done)").forEach(function (el) {
                    el.classList.add("done");
                    var sp = el.querySelector(".spinner"); if (sp) sp.remove();
                    var span = el.querySelector("span");
                    if (span) span.textContent = span.textContent.replace(/…$/, "") + " \u2014 cancelled";
                });
                document.querySelectorAll(".thinking-block .thinking-spinner").forEach(function (el) { el.remove(); });
                document.querySelectorAll(".thinking-block").forEach(function (el) { BX.updateThinkingHeader(el, true); });
                BX.lastToolBlock = null;
                break;
            case "reset_done":
                $chatMessages.innerHTML = "";
                BX.currentSessionId = evt.session_id || BX.currentSessionId;
                BX.persistSessionId(BX.currentSessionId);
                BX.sessionStartTime = null;
                fileChangesThisSession.clear();
                BX.currentChecklistItems = [];
                BX.currentPlanSteps = [];
                BX.updateFileChangesDropdown();
                BX.updateStickyTodoBar();
                if ($conversationTitle) $conversationTitle.textContent = evt.session_name || "New conversation";
                $tokenCount.textContent = "0 tokens";
                BX.loadAgentSessions();
                toolRunById.clear();
                BX.clearPendingImages();
                BX.setRunning(false);
                BX.hideActionBar();
                modifiedFiles.clear();
                BX.clearAllDiffDecorations();
                document.querySelectorAll(".plan-block").forEach(function (el) { el.remove(); });
                var todoBar = document.getElementById("sticky-todo-bar");
                if (todoBar) todoBar.classList.add("hidden");
                BX._stopThinkingTick();
                Object.keys(BX._phaseTickIntervals).forEach(function (k) { clearInterval(BX._phaseTickIntervals[k]); delete BX._phaseTickIntervals[k]; });
                if (BX._scoutTickInterval) { clearInterval(BX._scoutTickInterval); BX._scoutTickInterval = null; }
                var resetGaugeFill = document.getElementById("context-gauge-fill");
                if (resetGaugeFill) { resetGaugeFill.style.width = "0%"; resetGaugeFill.className = "gauge-fill"; }
                BX.refreshTree();
                BX.updateModifiedFilesBar();
                break;
            case "session_name_update":
                if (evt.new_session_id && evt.old_session_id === BX.currentSessionId) {
                    BX.currentSessionId = evt.new_session_id;
                    BX.persistSessionId(BX.currentSessionId);
                }
                if ($conversationTitle && evt.session_name &&
                    (!evt.session_id || evt.session_id === BX.currentSessionId)) {
                    $conversationTitle.textContent = evt.session_name;
                }
                BX.loadAgentSessions();
                break;
            case "info":
                BX.showInfo(evt.content || "");
                break;
            case "error":
                BX.showError(evt.content || "Unknown error");
                BX.setRunning(false);
                BX.hideActionBar();
                document.querySelectorAll(".tool-block-loading").forEach(function (b) {
                    b.classList.remove("tool-block-loading");
                    var st = b.querySelector(".tool-status");
                    if (st) { st.className = "tool-status tool-status-error"; st.title = "Error"; st.innerHTML = BX.toolActionIcon("failed"); }
                });
                document.querySelectorAll(".phase-indicator:not(.done)").forEach(function (el) { el.classList.add("done"); });
                Object.keys(BX._phaseTickIntervals).forEach(function (k) { clearInterval(BX._phaseTickIntervals[k]); delete BX._phaseTickIntervals[k]; });
                if (BX._scoutTickInterval) { clearInterval(BX._scoutTickInterval); BX._scoutTickInterval = null; }
                if (BX.scoutEl && !BX.scoutEl.classList.contains("scout-done")) BX.endScout();
                break;
            case "stream_retry": case "stream_recovering": BX.showInfo(evt.content || "Recovering\u2026"); break;
            case "stream_failed": BX.showError(evt.content || "Stream failed."); BX.setRunning(false); break;
            case "status":
                BX.updateTokenDisplay(evt);
                break;

            // ── Replay events (history restore on reconnect) ──
            case "replay_user":
                BX.addUserMessage(evt.content || "");
                break;
            case "replay_guidance":
                BX.addGuidanceMessage(evt.content || "");
                break;
            case "replay_text":
                if (evt.content) {
                    var rb = BX.addAssistantMessage();
                    var rd = document.createElement("div");
                    rd.className = "text-content";
                    rd.innerHTML = BX.renderMarkdown(evt.content);
                    rd.querySelectorAll("pre code").forEach(function (b) { if (typeof hljs !== "undefined") hljs.highlightElement(b); });
                    rb.appendChild(rd);
                    rb.appendChild(BX.makeCopyBtn(evt.content));
                }
                break;
            case "replay_thinking": {
                var rt = BX.createThinkingBlock();
                BX._thinkingBuffer = evt.content || "";
                BX.finishThinking(rt);
                break;
            }
            case "replay_tool_call":
                BX.lastToolBlock = BX.addToolCall(
                    evt.data?.name || "tool",
                    evt.data?.input || {},
                    evt.data?.id || evt.data?.tool_use_id || null,
                    { stream: false }
                );
                break;
            case "replay_tool_result":
                {
                    var _rtrId = evt.data?.tool_use_id || evt.data?.id;
                    var rtrRunEl = (_rtrId && toolRunById.get(String(_rtrId))) || BX.lastToolBlock;
                    BX.addToolResult(evt.content || "", evt.data?.success !== false, rtrRunEl, evt.data);
                    if (rtrRunEl && evt.data?.success !== false) {
                        var rtrTn = rtrRunEl.dataset.toolName;
                        var rtrIsWrite = rtrTn === "Write" || rtrTn === "write_file";
                        var rtrIsEdit = rtrTn === "Edit" || rtrTn === "edit_file" || rtrTn === "symbol_edit";
                        if ((rtrIsWrite || rtrIsEdit) && rtrRunEl.dataset.path) {
                            var rtrInputData = rtrRunEl._toolInput || {};
                            if (rtrIsWrite) {
                                var rtrContentLines = (rtrInputData.content || "").split('\n').length;
                                BX.trackFileChange(rtrRunEl.dataset.path, rtrContentLines, 0);
                            } else {
                                var rtrOldStr = rtrInputData.old_string || "";
                                var rtrNewStr = rtrInputData.new_string || "";
                                var rtrOldLineCount = rtrOldStr ? rtrOldStr.split('\n').length : 0;
                                var rtrNewLineCount = rtrNewStr ? rtrNewStr.split('\n').length : 0;
                                BX.trackFileChange(rtrRunEl.dataset.path, rtrNewLineCount, rtrOldLineCount);
                            }
                        }
                        if (rtrTn === "Bash") {
                            var rtrBashInput = rtrRunEl._toolInput || {};
                            BX.detectFileDeletesFromBash(rtrBashInput.command, evt.content || "");
                        }
                    }
                }
                BX.lastToolBlock = null;
                break;
            case "replay_done":
                BX.isReplaying = false;
                BX.scrollChat();
                break;
            case "resumed":
                _hideReconnectBanner();
                if (evt.agent_running) {
                    BX.setRunning(true);
                    BX.showInfo("Reconnected \u2014 agent is still working\u2026");
                } else {
                    BX.setRunning(false);
                    BX.showInfo("Reconnected \u2014 agent has finished.");
                }
                BX.scrollChat();
                break;
            case "replay_state":
                if (evt.todos && Array.isArray(evt.todos)) {
                    BX.checklistSource = "todos";
                    BX.showAgentChecklist(evt.todos);
                }
                if (evt.pending_plan && evt.pending_plan.length > 0) {
                    BX.checklistSource = "plan";
                    BX.currentChecklistItems = evt.pending_plan.map(function (s, i) { return { id: String(i + 1), content: s, status: "pending" }; });
                    BX.showAgentChecklist(BX.currentChecklistItems);
                }
                if (evt.awaiting_build && evt.pending_plan) {
                    BX.showPlan(evt.pending_plan, evt.plan_file || null, evt.plan_text || "", !!evt.todos?.length, true, evt.plan_title || "");
                }
                if (evt.awaiting_keep_revert && evt.has_diffs) {
                    if (evt.diffs && evt.diffs.length > 0) {
                        BX.showDiffs(evt.diffs, true);
                    } else {
                        BX.showActionBar([
                            { label: "Keep", cls: "success", onClick: function () { BX.hideActionBar(); send({ type: "keep" }); BX.updateFileChangesDropdown(); }},
                            { label: "Revert", cls: "danger", onClick: function () { BX.hideActionBar(); send({ type: "revert" }); fileChangesThisSession.clear(); BX.updateFileChangesDropdown(); }},
                        ]);
                    }
                    BX.showInfo("You have pending file changes from a previous session.");
                }
                break;

            // ── External file changes ──
            case "file_changed":
                if (!BX.isRunning) BX.refreshTree();
                if (typeof BX.monacoInstance !== "undefined" && BX.monacoInstance) {
                    var model = BX.monacoInstance.getModel();
                    if (model && evt.path && model.uri.path.endsWith(evt.path)) {
                        fetch("/api/file?path=" + encodeURIComponent(evt.path))
                            .then(function (r) { return r.ok ? r.text() : null; })
                            .then(function (content) {
                                if (content !== null && content !== model.getValue()) {
                                    model.setValue(content);
                                }
                            }).catch(function () {});
                    }
                }
                break;
        }
    }

    // ================================================================
    // INPUT
    // ================================================================

    function submitTask() {
        var text = ($input && $input.value) ? $input.value.trim() : "";
        var hasImages = pendingImages.length > 0;
        if (!text && !hasImages) return;

        if (!BX.ws || BX.ws.readyState !== WebSocket.OPEN) {
            BX.showInfo("Not connected. Waiting for connection\u2026");
            if (typeof BX.showToast === "function") BX.showToast("Not connected");
            return;
        }

        if (BX.isRunning) {
            if (!text) return;
            if (text.startsWith("/") && /^\/[a-zA-Z]/.test(text)) { handleCommand(text); $input.value = ""; autoResizeInput(); return; }
            // Debounce guidance — 500ms cooldown
            var now = Date.now();
            if (now - (BX._lastGuidanceSendTime || 0) < 500) {
                BX.showInfo("Please wait before sending more guidance.");
                return;
            }
            BX._lastGuidanceSendTime = now;
            BX.hideActionBar();
            BX.addGuidanceMessage(text);
            $input.value = ""; autoResizeInput();
            send({ type: "guidance", content: text });
            return;
        }

        if (text.startsWith("/") && /^\/[a-zA-Z]/.test(text) && !hasImages) { handleCommand(text); $input.value = ""; autoResizeInput(); return; }

        var imagesPayload = [];
        if (hasImages) {
            if (BX._submitting) return;
            BX._submitting = true;
            return BX.serializePendingImages().then(function (imgs) {
                imagesPayload = imgs;
                _doSubmit(text, imagesPayload);
            }).catch(function (e) {
                BX.showError("Failed to attach image: " + (e?.message || e));
            }).finally(function () {
                BX._submitting = false;
            });
        }

        _doSubmit(text, imagesPayload);
    }

    function _doSubmit(text, imagesPayload) {
        BX.addUserMessage(text, imagesPayload);
        $input.value = ""; autoResizeInput();
        BX.clearPendingImages();
        // Clear stale keep/revert UI if user sends a new task instead of clicking Keep/Revert
        BX.hideActionBar();
        var existingDiff = document.getElementById("cumulative-diff-container");
        if (existingDiff) {
            existingDiff.querySelectorAll(".diff-block").forEach(function(b) { b.classList.add("collapsed"); });
        }
        // Clear stale plan Build/Feedback/Reject buttons
        document.querySelectorAll(".plan-actions").forEach(function(el) { el.remove(); });
        BX.setRunning(true);
        BX.addAssistantMessage();
        var editorCtx = gatherEditorContext();
        var payload = { type: "task", content: text, images: imagesPayload };
        if (editorCtx) payload.context = editorCtx;
        var sent = send(payload);
        if (!sent) {
            BX.setRunning(false);
            BX.showInfo("Send failed. Check connection.");
        }
    }

    function handleCommand(text) {
        var parts = text.trim().split(/\s+/);
        var cmd = (parts[0] || "").toLowerCase();
        switch(cmd) {
            case "/reset": send({type:"reset"}); break;
            case "/cancel": send({type:"cancel"}); break;
            case "/checkpoints": send({ type: "checkpoint_list" }); break;
            case "/rewind": send({ type: "checkpoint_restore", checkpoint_id: parts[1] || "latest" }); break;
            case "/help":
                BX.showInfo("Commands: /reset | /cancel | /checkpoints | /rewind <checkpoint-id|latest>");
                break;
            default: BX.showInfo("Unknown: " + cmd);
        }
    }

    function autoResizeInput() { if ($input) { $input.style.height = "auto"; $input.style.height = Math.min($input.scrollHeight, 150) + "px"; } }

    if ($input) {
        $input.addEventListener("input", autoResizeInput);
        $input.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitTask(); } });
    }
    if ($attachImageBtn && $imageInput) {
        $attachImageBtn.addEventListener("click", function () { $imageInput.click(); });
        $imageInput.addEventListener("change", function (e) {
            var files = Array.from(e.target.files || []);
            BX.addPendingImageFiles(files);
            $imageInput.value = "";
        });
    }
    if ($sendBtn) $sendBtn.addEventListener("click", submitTask);
    $cancelBtn.addEventListener("click", function () { send({type:"cancel"}); });
    if ($resetBtn) {
        $resetBtn.addEventListener("click", function () {
            if ($chatMenuDropdown) $chatMenuDropdown.classList.add("hidden");
            if (confirm("Clear this conversation and start a new one? This cannot be undone.")) {
                send({ type: "reset" });
            }
        });
    }
    if ($newAgentBtn) {
        $newAgentBtn.addEventListener("click", function () { BX.createNewAgentSession(); });
    }
    if ($agentSelect) {
        $agentSelect.addEventListener("change", function () {
            if (BX.suppressAgentSwitch) return;
            var nextId = $agentSelect.value || null;
            if (!nextId || nextId === BX.currentSessionId) return;

            BX.hideActionBar();
            BX.hidePlan();
            BX.clearAllDiffDecorations();
            modifiedFiles.clear();
            fileChangesThisSession.clear();
            BX.updateModifiedFilesBar();
            BX.updateFileChangesDropdown();

            BX.currentSessionId = nextId;
            BX.persistSessionId(BX.currentSessionId);
            disconnectWs();
            connect();
        });
    }

    function isTerminalFocused() {
        var panel = document.getElementById("terminal-panel");
        if (!panel || panel.classList.contains("hidden")) return false;
        return panel.contains(document.activeElement);
    }

    document.addEventListener("keydown", function (e) {
        if (isTerminalFocused()) return;
        if (e.key === "Escape" && BX.isRunning) { e.preventDefault(); send({type:"cancel"}); }
    });

    document.addEventListener("keydown", function (e) {
        if (isTerminalFocused()) return;
        var isMeta = e.metaKey || e.ctrlKey;
        if (!isMeta) return;

        if ((e.key === "Backspace" && e.shiftKey) || e.key === "k") {
            e.preventDefault();
            if (confirm("Clear this conversation and start a new one? This cannot be undone.")) {
                send({ type: "reset" });
            }
            return;
        }
        if (e.key === "l") {
            e.preventDefault();
            $input.focus();
            return;
        }
        if (e.key === "b") {
            e.preventDefault();
            var tree = document.getElementById("file-tree-panel");
            if (tree) tree.style.display = tree.style.display === "none" ? "" : "none";
            return;
        }
        if (e.key === "j") {
            e.preventDefault();
            var chatPanel = document.getElementById("chat-panel");
            if (chatPanel) chatPanel.style.display = chatPanel.style.display === "none" ? "" : "none";
            return;
        }
        if (e.key === "/") {
            e.preventDefault();
            if (document.activeElement === $input) {
                if (typeof BX.monacoInstance !== "undefined" && BX.monacoInstance) BX.monacoInstance.focus();
            } else {
                $input.focus();
            }
            return;
        }
    });

    // ── Exports ─────────────────────────────────────────────────
    BX.connect          = connect;
    BX.disconnectWs     = disconnectWs;
    BX.gatherEditorContext = gatherEditorContext;
    BX.send             = send;
    BX.handleEvent      = handleEvent;
    BX.handleCommand    = handleCommand;
    BX.autoResizeInput  = autoResizeInput;
    BX.isTerminalFocused = isTerminalFocused;
    BX.submitTask       = submitTask;

})(window.BX);
