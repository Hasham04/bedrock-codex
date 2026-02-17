/* ============================================================
   Bedrock Codex — Mini Cursor IDE
   File tree + Monaco Editor + Agent Chat
   ============================================================ */

(() => {
    "use strict";

    // ── DOM refs — Welcome Screen ─────────────────────────────
    const $welcomeScreen    = document.getElementById("welcome-screen");
    const $ideWrapper       = document.getElementById("ide-wrapper");
    const $welcomeOpenLocal = document.getElementById("welcome-open-local");
    const $welcomeSshBtn    = document.getElementById("welcome-ssh-connect");
    const $projectList      = document.getElementById("welcome-project-list");
    const $localModal       = document.getElementById("welcome-local-modal");
    const $localPath        = document.getElementById("welcome-local-path");
    const $localError       = document.getElementById("welcome-local-error");
    const $localOpen        = document.getElementById("welcome-local-open");
    const $localCancel      = document.getElementById("welcome-local-cancel");
    const $sshModal         = document.getElementById("welcome-ssh-modal");
    const $sshHost          = document.getElementById("ssh-host");
    const $sshUser          = document.getElementById("ssh-user");
    const $sshPort          = document.getElementById("ssh-port");
    const $sshKey           = document.getElementById("ssh-key");
    const $sshDir           = document.getElementById("ssh-dir");
    const $sshError         = document.getElementById("welcome-ssh-error");
    const $sshOpen          = document.getElementById("welcome-ssh-open");
    const $sshCancel        = document.getElementById("welcome-ssh-cancel");
    const $sshBrowseBtn     = document.getElementById("ssh-browse-btn");
    const $sshBrowseModal   = document.getElementById("ssh-browse-modal");
    const $sshBrowseList    = document.getElementById("ssh-browse-list");
    const $sshBrowseBreadcrumb = document.getElementById("ssh-browse-breadcrumb");
    const $sshBrowseCurrent = document.getElementById("ssh-browse-current");
    const $sshBrowseSelect  = document.getElementById("ssh-browse-select");
    const $sshBrowseCancel  = document.getElementById("ssh-browse-cancel");

    // ── DOM refs — IDE ────────────────────────────────────────
    const $fileTree      = document.getElementById("file-tree");
    const $fileFilter    = document.getElementById("file-filter-input");
    const $refreshTree   = document.getElementById("refresh-tree-btn");
    const $tabBar        = document.getElementById("tab-bar");
    const $editorWelcome = document.getElementById("editor-welcome");
    const $monacoEl      = document.getElementById("monaco-container");
    const $chatMessages  = document.getElementById("chat-messages");
    const $input         = document.getElementById("user-input");
    const $attachImageBtn = document.getElementById("attach-image-btn");
    const $imageInput    = document.getElementById("image-input");
    const $imagePreviewStrip = document.getElementById("image-preview-strip");
    const $sendBtn       = document.getElementById("send-btn");
    const $cancelBtn     = document.getElementById("cancel-btn");
    const $actionBar     = document.getElementById("action-bar");
    const $actionBtns    = document.getElementById("action-buttons");

    const $modelName     = document.getElementById("model-name");
    const $tokenCount    = document.getElementById("token-count");
    const $connStatus    = document.getElementById("connection-status");
    const $conversationTitle = document.getElementById("conversation-title");
    const $agentSelect   = document.getElementById("agent-select");
    const $newAgentBtn   = document.getElementById("new-agent-btn");
    const $workingDir    = document.getElementById("working-dir");
    const $resetBtn      = document.getElementById("reset-btn");
    const $chatSessionStats = document.getElementById("chat-session-stats");
    const $chatSessionTime = document.getElementById("chat-session-time");
    const $chatSessionTotals = document.getElementById("chat-session-totals");
    const $chatMenuBtn = document.getElementById("chat-menu-btn");
    const $chatMenuDropdown = document.getElementById("chat-menu-dropdown");
    const $stickyTodoBar = document.getElementById("sticky-todo-bar");
    const $stickyTodoToggle = document.getElementById("sticky-todo-toggle");
    const $stickyTodoCount = document.getElementById("sticky-todo-count");
    const $stickyTodoList = document.getElementById("sticky-todo-list");
    const $stickyTodoAddInput = document.getElementById("sticky-todo-add-input");
    const $stickyTodoAddBtn = document.getElementById("sticky-todo-add-btn");
    const $chatComposerStats = document.getElementById("chat-composer-stats");
    const $chatComposerStatsToggle = document.getElementById("chat-composer-stats-toggle");
    const $chatComposerFilesDropup = document.getElementById("chat-composer-files-dropup");
    const $chatComposerTime = document.getElementById("chat-composer-time");
    const $chatComposerTotals = document.getElementById("chat-composer-totals");
    const $chatComposerFiles = document.getElementById("chat-composer-files");
    const $chatComposerEdits = document.getElementById("chat-composer-edits");
    const $openBtn       = document.getElementById("open-project-btn");
    const $logoHome      = document.getElementById("logo-home");
    const $searchToggle  = document.getElementById("search-toggle-btn");
    const $searchPanel   = document.getElementById("search-panel");
    const $searchInput   = document.getElementById("search-input");
    const $searchInclude = document.getElementById("search-include");
    const $searchGoBtn   = document.getElementById("search-go-btn");
    const $searchResults = document.getElementById("search-results");
    const $searchStatus  = document.getElementById("search-status");
    const $searchRegex   = document.getElementById("search-regex-btn");
    const $searchCase    = document.getElementById("search-case-btn");
    const $replaceRow    = document.getElementById("replace-row");
    const $replaceInput  = document.getElementById("replace-input");
    const $replaceAllBtn = document.getElementById("replace-all-btn");
    const $replaceToggle = document.getElementById("search-replace-toggle");
    const $terminalToggleBtn = document.getElementById("terminal-toggle-btn");
    const $terminalPanel = document.getElementById("terminal-panel");
    const $terminalXtermContainer = document.getElementById("terminal-xterm-container");
    const $terminalCloseBtn = document.getElementById("terminal-close-btn");
    const $terminalClearBtn = document.getElementById("terminal-clear-btn");
    const $resizeTerminal = document.getElementById("resize-terminal");
    const $sourceControlList = document.getElementById("source-control-list");
    const $sourceControlRefreshBtn = document.getElementById("source-control-refresh-btn");
    let terminalXterm = null;
    let terminalFitAddon = null;
    let terminalWs = null;
    let terminalFocusInput = null;
    var terminalOutputBuffer = "";
    var terminalFlushRaf = 0;
    var terminalBlobQueue = [];
    var terminalBlobProcessing = false;

    function terminalFlushOutput() {
        terminalFlushRaf = 0;
        if (terminalOutputBuffer.length && terminalXterm) {
            terminalXterm.write(terminalOutputBuffer);
            terminalOutputBuffer = "";
        }
    }

    function terminalScheduleFlush() {
        if (terminalFlushRaf) return;
        terminalFlushRaf = requestAnimationFrame(terminalFlushOutput);
    }

    function terminalProcessNextBlob(wsRef) {
        if (terminalBlobProcessing || terminalBlobQueue.length === 0 || terminalWs !== wsRef) return;
        terminalBlobProcessing = true;
        var blob = terminalBlobQueue.shift();
        blob.arrayBuffer().then(function (buf) {
            if (terminalWs === wsRef) {
                terminalOutputBuffer += new TextDecoder().decode(buf);
                terminalScheduleFlush();
            }
            terminalBlobProcessing = false;
            if (terminalBlobQueue.length > 0) terminalProcessNextBlob(wsRef);
        }).catch(function () {
            terminalBlobProcessing = false;
            if (terminalBlobQueue.length > 0) terminalProcessNextBlob(wsRef);
        });
    }

    // ── State ─────────────────────────────────────────────────
    let ws = null;
    let isRunning = false;
    let monacoInstance = null;    // monaco.editor reference
    let diffEditorInstance = null;
    let activeTab = null;         // path of active tab
    const openTabs = new Map();   // path -> { model, viewState, content }
    const modifiedFiles = new Set(); // paths changed by agent
    let gitStatus = new Map();       // path -> 'M'|'A'|'D'|'U' (git status for explorer + inline diffs)
    let fileChangesThisSession = new Map(); // path -> {edits: number, deletions: number}
    let currentThinkingEl = null;
    let currentTextEl = null;
    let currentTextBuffer = "";
    let lastToolBlock = null;
    const toolRunById = new Map(); // tool_use_id -> run element
    let scoutEl = null;
    const pendingImages = []; // { id, file, previewUrl, name, size, media_type }
    let currentSessionId = null;
    const BEDROCK_SESSION_KEY = "bedrock_session_" + (location.host || "default");
    function persistSessionId(id) {
        try {
            if (id) localStorage.setItem(BEDROCK_SESSION_KEY, id);
            else localStorage.removeItem(BEDROCK_SESSION_KEY);
        } catch (_) {}
    }
    function loadPersistedSessionId() {
        try {
            return localStorage.getItem(BEDROCK_SESSION_KEY) || null;
        } catch (_) { return null; }
    }
    let sessionStartTime = null; // set on first user message for "Xm" / "Xh" in header
    let suppressAgentSwitch = false;

    // ── Markdown ──────────────────────────────────────────────
    if (typeof marked !== "undefined") {
        marked.setOptions({
            breaks: true, gfm: true,
            highlight: function(code, lang) {
                if (typeof hljs !== "undefined" && lang && hljs.getLanguage(lang))
                    try { return hljs.highlight(code, { language: lang }).value; } catch {}
                return code;
            }
        });
    }

    // ── Helpers ───────────────────────────────────────────────
    function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
    // ── Smart auto-scroll: only scroll if user hasn't scrolled up ──
    let _isUserScrolledUp = false;
    $chatMessages.addEventListener("scroll", () => {
        const el = $chatMessages;
        _isUserScrolledUp = (el.scrollTop + el.clientHeight < el.scrollHeight - 60);
        const btn = document.getElementById("scroll-to-bottom-btn");
        if (btn) btn.style.display = _isUserScrolledUp ? "flex" : "none";
    });
    // Create scroll-to-bottom button
    (function createScrollBtn() {
        const btn = document.createElement("button");
        btn.id = "scroll-to-bottom-btn";
        btn.innerHTML = "&darr;";
        btn.title = "Scroll to bottom";
        btn.style.display = "none";
        btn.addEventListener("click", () => {
            _isUserScrolledUp = false;
            $chatMessages.scrollTop = $chatMessages.scrollHeight;
            btn.style.display = "none";
        });
        // Position relative to chat messages container
        $chatMessages.parentElement.style.position = "relative";
        $chatMessages.parentElement.appendChild(btn);
    })();
    function scrollChat() { if (!_isUserScrolledUp) requestAnimationFrame(() => { $chatMessages.scrollTop = $chatMessages.scrollHeight; }); }
    function showToast(text) {
        const t = document.createElement("div"); t.className = "toast"; t.textContent = text;
        document.body.appendChild(t); setTimeout(() => t.remove(), 2200);
    }
    function copyText(text) { navigator.clipboard.writeText(text).then(() => showToast("Copied"), () => showToast("Copy failed")); }
    function makeCopyBtn(getText) {
        const btn = document.createElement("button"); btn.className = "copy-btn"; btn.title = "Copy";
        btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
        btn.addEventListener("click", e => { e.stopPropagation(); copyText(typeof getText === "function" ? getText() : getText); btn.classList.add("copied"); setTimeout(() => btn.classList.remove("copied"), 1500); });
        return btn;
    }
    function renderMarkdown(text) { return typeof marked !== "undefined" ? marked.parse(text) : escapeHtml(text).replace(/\n/g, "<br>"); }
    function formatTokens(n) { return n >= 1e6 ? (n/1e6).toFixed(1)+"M" : n >= 1e3 ? (n/1e3).toFixed(1)+"K" : String(n); }
    function updateTokenDisplay(data) {
        if (data.input_tokens !== undefined && data.output_tokens !== undefined) {
            const parts = [`In: ${formatTokens(data.input_tokens)}`, `Out: ${formatTokens(data.output_tokens)}`];
            if (data.cache_read) parts.push(`Cache: ${formatTokens(data.cache_read)}`);
            $tokenCount.textContent = parts.join(" | ");
            $tokenCount.title = `Input: ${data.input_tokens?.toLocaleString() || 0} | Output: ${data.output_tokens?.toLocaleString() || 0} | Cache read: ${(data.cache_read || 0).toLocaleString()}`;
        } else if (data.tokens !== undefined) {
            $tokenCount.textContent = formatTokens(data.tokens) + " tokens";
        }
        // Update context gauge
        if (data.context_usage_pct !== undefined) {
            const pct = Math.min(data.context_usage_pct, 100);
            const fill = document.getElementById("context-gauge-fill");
            const gauge = document.getElementById("context-gauge");
            if (fill) {
                fill.style.width = pct + "%";
                fill.className = "context-gauge-fill" + (pct > 75 ? " danger" : pct > 50 ? " warn" : "");
            }
            if (gauge) gauge.title = `Context: ${pct}% used`;
        }
    }
    function truncate(text, max) { return (!text || text.length <= max) ? text : text.slice(0, max) + `\n... (${text.length-max} more chars)`; }
    function basename(path) { return path.split("/").pop(); }
    function formatBytes(bytes) {
        if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
        const units = ["B", "KB", "MB", "GB"];
        let size = bytes;
        let i = 0;
        while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
        return `${size.toFixed(size >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
    }
    function mediaTypeFromName(name) {
        const low = String(name || "").toLowerCase();
        if (low.endsWith(".png")) return "image/png";
        if (low.endsWith(".jpg") || low.endsWith(".jpeg")) return "image/jpeg";
        if (low.endsWith(".webp")) return "image/webp";
        if (low.endsWith(".gif")) return "image/gif";
        return "application/octet-stream";
    }
    function imageSrcForMessage(img) {
        if (img?.previewUrl) return img.previewUrl;
        if (img?.data && img?.media_type) return `data:${img.media_type};base64,${img.data}`;
        return "";
    }
    function renderImagePreviewStrip() {
        if (!$imagePreviewStrip) return;
        $imagePreviewStrip.innerHTML = "";
        if (pendingImages.length === 0) {
            $imagePreviewStrip.classList.add("hidden");
            return;
        }
        pendingImages.forEach((img) => {
            const chip = document.createElement("div");
            chip.className = "image-preview-chip";
            chip.innerHTML = `<img src="${escapeHtml(img.previewUrl)}" alt="${escapeHtml(img.name)}"><button class="image-preview-remove" title="Remove image" data-id="${escapeHtml(img.id)}">×</button>`;
            chip.querySelector(".image-preview-remove").addEventListener("click", (e) => {
                e.preventDefault();
                e.stopPropagation();
                removePendingImage(img.id);
            });
            $imagePreviewStrip.appendChild(chip);
        });
        const meta = document.createElement("div");
        meta.className = "image-preview-meta";
        const totalBytes = pendingImages.reduce((acc, i) => acc + (i.size || 0), 0);
        meta.textContent = `${pendingImages.length} image${pendingImages.length === 1 ? "" : "s"} • ${formatBytes(totalBytes)}`;
        $imagePreviewStrip.appendChild(meta);
        $imagePreviewStrip.classList.remove("hidden");
    }
    function removePendingImage(id) {
        const idx = pendingImages.findIndex((x) => x.id === id);
        if (idx === -1) return;
        const [removed] = pendingImages.splice(idx, 1);
        if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
        renderImagePreviewStrip();
    }
    function clearPendingImages() {
        while (pendingImages.length) {
            const img = pendingImages.pop();
            if (img?.previewUrl) URL.revokeObjectURL(img.previewUrl);
        }
        if ($imageInput) $imageInput.value = "";
        renderImagePreviewStrip();
    }
    function addPendingImageFiles(files) {
        if (!files || files.length === 0) return;
        const MAX_COUNT = 3;
        const MAX_BYTES = 2 * 1024 * 1024;
        for (const file of files) {
            if (pendingImages.length >= MAX_COUNT) {
                showInfo(`Max ${MAX_COUNT} images per message.`);
                break;
            }
            if (!file.type || !file.type.startsWith("image/")) {
                showInfo(`Skipped non-image file: ${file.name}`);
                continue;
            }
            if (file.size > MAX_BYTES) {
                showInfo(`Skipped ${file.name}: exceeds ${formatBytes(MAX_BYTES)}.`);
                continue;
            }
            const previewUrl = URL.createObjectURL(file);
            pendingImages.push({
                id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
                file,
                previewUrl,
                name: file.name,
                size: file.size,
                media_type: file.type || mediaTypeFromName(file.name),
            });
        }
        renderImagePreviewStrip();
    }
    function fileToBase64Data(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                const result = String(reader.result || "");
                const comma = result.indexOf(",");
                if (comma < 0) {
                    reject(new Error("Invalid file encoding"));
                    return;
                }
                resolve(result.slice(comma + 1));
            };
            reader.onerror = () => reject(new Error("Failed to read image"));
            reader.readAsDataURL(file);
        });
    }
    async function serializePendingImages() {
        const payload = [];
        for (const img of pendingImages) {
            const b64 = await fileToBase64Data(img.file);
            payload.push({
                name: img.name,
                media_type: img.media_type || mediaTypeFromName(img.name),
                data: b64,
                size: img.size || 0,
            });
        }
        return payload;
    }

    // Language detection for Monaco
    function langFromExt(ext) {
        const map = {
            js:"javascript", jsx:"javascript", ts:"typescript", tsx:"typescript",
            py:"python", rb:"ruby", rs:"rust", go:"go", java:"java",
            c:"c", cpp:"cpp", h:"c", hpp:"cpp", cs:"csharp",
            html:"html", htm:"html", css:"css", scss:"scss", less:"less",
            json:"json", yaml:"yaml", yml:"yaml", toml:"toml",
            md:"markdown", txt:"plaintext", sh:"shell", bash:"shell",
            sql:"sql", xml:"xml", svg:"xml", dockerfile:"dockerfile",
            makefile:"makefile", env:"plaintext", gitignore:"plaintext",
        };
        return map[ext?.toLowerCase()] || "plaintext";
    }

    // ── File Changes Tracking (line counts) ────────────────────
    function trackFileChange(path, linesAdded = 0, linesDeleted = 0) {
        if (!path) return;
        const current = fileChangesThisSession.get(path) || { edits: 0, deletions: 0 };
        fileChangesThisSession.set(path, {
            edits: current.edits + linesAdded,
            deletions: current.deletions + linesDeleted
        });
        updateFileChangesDropdown();
    }
    function untrackFileChange(path) {
        if (!path) return;
        const hadEntry = fileChangesThisSession.has(path);
        fileChangesThisSession.delete(path);
        modifiedFiles.delete(path);
        if (hadEntry) {
            updateFileChangesDropdown();
            updateModifiedFilesBar();
        }
    }
    function detectFileDeletesFromBash(command, output) {
        if (!command) return;
        const cmd = command.trim();
        // Match rm commands that target tracked files
        const rmMatch = cmd.match(/\brm\s+(?:-[rfiv]+\s+)*(.+)/);
        if (rmMatch) {
            const targets = rmMatch[1].split(/\s+/).filter(t => t && !t.startsWith("-"));
            for (const t of targets) {
                const cleaned = t.replace(/["']/g, "");
                // Check if any tracked path ends with this target
                for (const [trackedPath] of fileChangesThisSession) {
                    if (trackedPath.endsWith(cleaned) || trackedPath.endsWith("/" + cleaned)) {
                        untrackFileChange(trackedPath);
                    }
                }
            }
        }
    }

    function getFileIcon(path) {
        const ext = path.split('.').pop()?.toLowerCase();
        const iconMap = {
            js: "📄", jsx: "⚛️", ts: "📘", tsx: "⚛️", 
            py: "🐍", java: "☕", cpp: "⚡", c: "⚡", 
            html: "🌐", css: "🎨", scss: "🎨", 
            json: "📋", xml: "📄", md: "📝", 
            txt: "📄", log: "📜", yaml: "⚙️", yml: "⚙️",
            png: "🖼️", jpg: "🖼️", jpeg: "🖼️", gif: "🖼️", svg: "🎨"
        };
        return iconMap[ext] || "📄";
    }

    function formatSessionDuration(ms) {
        if (!ms || ms < 0) return "";
        const sec = Math.floor(ms / 1000);
        if (sec < 60) return sec + "s";
        const min = Math.floor(sec / 60);
        if (min < 60) return min + "m";
        const hr = Math.floor(min / 60);
        return hr + "h";
    }

    function updateFileChangesDropdown() {
        const changes = Array.from(fileChangesThisSession.entries())
            .filter(([_, stats]) => stats.edits > 0 || stats.deletions > 0)
            .sort(([a], [b]) => a.localeCompare(b));

        let totalAdd = 0, totalDel = 0;
        changes.forEach(([_, stats]) => {
            totalAdd += stats.edits;
            totalDel += stats.deletions;
        });

        const hasFileEdits = changes.length > 0;
        const showHeaderStats = sessionStartTime || hasFileEdits;
        const elapsed = sessionStartTime ? Date.now() - sessionStartTime : 0;
        const timeStr = formatSessionDuration(elapsed);
        const filesLabel = hasFileEdits ? (changes.length + " file" + (changes.length !== 1 ? "s" : "")) : "0 files";
        const totalEdits = totalAdd + totalDel;

        // Update header session stats (only when session or file edits)
        if ($chatSessionStats) {
            if (showHeaderStats) {
                $chatSessionStats.classList.remove("hidden");
                if ($chatSessionTime) $chatSessionTime.textContent = timeStr;
                if ($chatSessionTotals) {
                    const addEl = $chatSessionTotals.querySelector(".add");
                    const delEl = $chatSessionTotals.querySelector(".del");
                    if (addEl) addEl.textContent = "+" + totalAdd;
                    if (delEl) delEl.textContent = "\u2212" + totalDel;
                }
            } else {
                $chatSessionStats.classList.add("hidden");
            }
        }

        // File edits bar: only show when there are edited files
        if ($chatComposerStats) {
            if (hasFileEdits) {
                $chatComposerStats.classList.remove("hidden");
                if ($chatComposerTime) $chatComposerTime.textContent = timeStr;
                if ($chatComposerTotals) {
                    const addEl = $chatComposerTotals.querySelector(".add");
                    const delEl = $chatComposerTotals.querySelector(".del");
                    if (addEl) addEl.textContent = "+" + totalAdd;
                    if (delEl) delEl.textContent = "\u2212" + totalDel;
                }
                if ($chatComposerFiles) $chatComposerFiles.textContent = filesLabel;
                if ($chatComposerEdits) $chatComposerEdits.textContent = "\u2009\u22c5\u2009" + totalEdits + " line" + (totalEdits !== 1 ? "s" : "");
                if ($chatComposerFilesDropup) {
                    $chatComposerFilesDropup.innerHTML = changes.map(([path, stats]) => {
                        const icon = fileTypeIcon(path, 14);
                        const add = stats.edits > 0 ? `<span class="add">+${stats.edits}L</span>` : "";
                        const del = stats.deletions > 0 ? `<span class="del">\u2212${stats.deletions}L</span>` : "";
                        return `<div class="composer-file-item" data-path="${escapeHtml(path)}" title="${escapeHtml(path)}">
                            <span class="file-icon">${icon}</span>
                            <span class="file-path">${escapeHtml(path)}</span>
                            <span class="file-stats">${add} ${del}</span>
                        </div>`;
                    }).join("");
                    $chatComposerFilesDropup.querySelectorAll(".composer-file-item[data-path]").forEach((item) => {
                        item.addEventListener("click", () => {
                            const path = item.dataset.path;
                            if (path) openFile(path);
                        });
                    });
                }
            } else {
                $chatComposerStats.classList.add("hidden");
                if ($chatComposerFilesDropup) $chatComposerFilesDropup.innerHTML = "";
                $chatComposerStats.removeAttribute("data-expanded");
                if ($chatComposerEdits) $chatComposerEdits.textContent = "";
            }
        }

    }

    // ── UI State ──────────────────────────────────────────────
    function setRunning(running) {
        isRunning = running;
        if ($sendBtn) $sendBtn.classList.toggle("hidden", running);
        if ($cancelBtn) $cancelBtn.classList.toggle("hidden", !running);
        if ($input) { $input.disabled = running; if (!running) $input.focus(); }
    }

    function showActionBar(buttons) {
        $actionBtns.innerHTML = "";
        buttons.forEach(({ label, cls, onClick }) => {
            const btn = document.createElement("button");
            btn.className = `action-btn ${cls}`;
            btn.textContent = label;
            btn.addEventListener("click", onClick);
            $actionBtns.appendChild(btn);
        });
        $actionBar.classList.remove("hidden");
    }
    function hideActionBar() { $actionBar.classList.add("hidden"); $actionBtns.innerHTML = ""; }

    // ================================================================
    // FILE TREE
    // ================================================================

    const treeState = {}; // path -> expanded (bool)

    async function fetchGitStatus() {
        try {
            const res = await fetch("/api/git-status?t=" + Date.now());
            if (!res.ok) {
                gitStatus = new Map();
                return;
            }
            const data = await res.json();
            const status = data.status && typeof data.status === "object" ? data.status : {};
            gitStatus = new Map(Object.entries(status));
            if (data.error && gitStatus.size === 0) {
                console.warn("Git status unavailable:", data.error);
            }
            renderSourceControl();
        } catch (e) {
            gitStatus = new Map();
            console.warn("Git status fetch failed:", e);
            renderSourceControl();
        }
    }

    function renderSourceControl() {
        if (!$sourceControlList) return;
        const entries = [...gitStatus.entries()].sort((a, b) => a[0].localeCompare(b[0]));
        if (entries.length === 0) {
            $sourceControlList.innerHTML = '<div class="source-control-empty">No changes</div>';
            return;
        }
        let html = "";
        for (const [path, status] of entries) {
            if (path.endsWith("/")) continue;
            const statusCls = status === "M" ? "modified" : status === "A" ? "added" : status === "D" ? "deleted" : "untracked";
            const label = status === "M" ? "M" : status === "A" ? "A" : status === "D" ? "D" : "U";
            html += `<div class="source-control-item" data-path="${escapeHtml(path)}" data-status="${statusCls}">
                <span class="sc-status ${statusCls}">${escapeHtml(label)}</span>
                <span class="sc-path">${escapeHtml(path)}</span>
            </div>`;
        }
        $sourceControlList.innerHTML = html;
        $sourceControlList.querySelectorAll(".source-control-item").forEach(el => {
            el.addEventListener("click", () => {
                const p = (el.dataset.path || "").replace(/\\/g, "/");
                if (p.endsWith("/")) return;
                openFile(p);
            });
        });
    }

    async function loadTree(parentPath = "", parentEl = null) {
        const target = parentEl || $fileTree;
        if (!parentEl) await fetchGitStatus();
        try {
            const res = await fetch(`/api/files?path=${encodeURIComponent(parentPath)}`);
            const items = await res.json();
            if (!parentEl) target.innerHTML = "";

            items.forEach(item => {
                const el = document.createElement("div");
                const depth = parentPath ? parentPath.split("/").length : 0;

                if (item.type === "directory") {
                    const isOpen = treeState[item.path] || false;
                    el.innerHTML = `
                        <div class="tree-item" data-path="${escapeHtml(item.path)}" data-type="dir" style="padding-left:${8 + depth*16}px">
                            <span class="tree-chevron ${isOpen ? 'open' : ''}">\u25B6</span>
                            <span class="tree-icon">\uD83D\uDCC1</span>
                            <span class="tree-file-name">${escapeHtml(item.name)}</span>
                        </div>
                        <div class="tree-children" ${isOpen ? '' : 'style="display:none"'}></div>
                    `;
                    const header = el.querySelector(".tree-item");
                    const children = el.querySelector(".tree-children");
                    const chevron = el.querySelector(".tree-chevron");

                    header.addEventListener("click", async () => {
                        const open = children.style.display !== "none";
                        children.style.display = open ? "none" : "";
                        chevron.classList.toggle("open", !open);
                        treeState[item.path] = !open;
                        if (!open && children.children.length === 0) {
                            await loadTree(item.path, children);
                        }
                    });

                    if (isOpen) loadTree(item.path, children);
                } else {
                    const icon = fileIcon(item.ext);
                    const pathNorm = (item.path || "").replace(/\\/g, "/");
                    const g = gitStatus.get(pathNorm);
                    const agentMod = modifiedFiles.has(item.path) || modifiedFiles.has(pathNorm);
                    const statusCls = agentMod ? "modified" : (g === "M" ? "modified" : g === "A" ? "added" : g === "D" ? "deleted" : g === "U" ? "untracked" : "");
                    el.innerHTML = `
                        <div class="tree-item ${statusCls}" data-path="${escapeHtml(item.path)}" data-type="file" style="padding-left:${8 + depth*16 + 16}px">
                            <span class="tree-icon">${icon}</span>
                            <span class="tree-file-name">${escapeHtml(item.name)}</span>
                        </div>
                    `;
                    el.querySelector(".tree-item").addEventListener("click", () => openFile(item.path));
                }
                target.appendChild(el);
            });
        } catch (e) {
            target.innerHTML = `<div class="info-msg" style="padding:10px">Failed to load files</div>`;
        }
    }

    // ── Explorer context menu (right-click) ────────────────────
    const $ctxMenu = document.getElementById("explorer-ctx-menu");
    let _ctxTarget = null;   // { path, type: "file"|"dir"|null }

    function _showCtxMenu(x, y, target) {
        if (!$ctxMenu) return;
        _ctxTarget = target;
        // Show/hide items based on context
        const hasPath = target && target.path;
        $ctxMenu.querySelector('[data-action="rename"]').style.display = hasPath ? "" : "none";
        $ctxMenu.querySelector('[data-action="delete"]').style.display = hasPath ? "" : "none";
        $ctxMenu.querySelector('[data-action="copy-path"]').style.display = hasPath ? "" : "none";
        $ctxMenu.querySelector('[data-action="copy-relative"]').style.display = hasPath ? "" : "none";
        // separators — hide the 2nd group separator when no path
        const seps = $ctxMenu.querySelectorAll(".ctx-menu-sep");
        if (seps[0]) seps[0].style.display = hasPath ? "" : "none";
        if (seps[1]) seps[1].style.display = hasPath ? "" : "none";

        $ctxMenu.classList.remove("hidden");
        // Position: keep inside viewport
        const rect = $ctxMenu.getBoundingClientRect();
        const mx = Math.min(x, window.innerWidth - rect.width - 8);
        const my = Math.min(y, window.innerHeight - rect.height - 8);
        $ctxMenu.style.left = mx + "px";
        $ctxMenu.style.top = my + "px";
    }

    function _hideCtxMenu() {
        if ($ctxMenu) $ctxMenu.classList.add("hidden");
        _ctxTarget = null;
    }

    // Close on any outside click or Escape
    document.addEventListener("click", _hideCtxMenu);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") _hideCtxMenu(); });

    // Right-click on file tree items
    $fileTree.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const treeItem = e.target.closest(".tree-item");
        if (treeItem) {
            const path = treeItem.dataset.path || "";
            const type = treeItem.dataset.type === "dir" ? "dir" : "file";
            _showCtxMenu(e.clientX, e.clientY, { path, type });
        } else {
            // Right-clicked on empty space in explorer
            _showCtxMenu(e.clientX, e.clientY, { path: null, type: null });
        }
    });

    // Also allow right-click on the explorer body background
    const $explorerBody = document.querySelector(".explorer-body");
    if ($explorerBody) {
        $explorerBody.addEventListener("contextmenu", (e) => {
            if (e.target === $explorerBody || e.target === $fileTree) {
                e.preventDefault();
                _showCtxMenu(e.clientX, e.clientY, { path: null, type: null });
            }
        });
    }

    // ── Inline rename input helper ───────────────────────────────
    function _startInlineRename(treeItem, currentName, onCommit) {
        const nameEl = treeItem.querySelector(".tree-file-name");
        if (!nameEl) return;
        const original = nameEl.textContent;
        const input = document.createElement("input");
        input.type = "text";
        input.className = "tree-rename-input";
        input.value = currentName;
        nameEl.textContent = "";
        nameEl.appendChild(input);
        input.focus();
        // Select filename without extension
        const dotIdx = currentName.lastIndexOf(".");
        input.setSelectionRange(0, dotIdx > 0 ? dotIdx : currentName.length);

        let committed = false;
        function commit() {
            if (committed) return;
            committed = true;
            const newName = input.value.trim();
            if (input.parentNode) input.remove();
            nameEl.textContent = original;
            if (newName && newName !== currentName) {
                onCommit(newName);
            }
        }
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); commit(); }
            if (e.key === "Escape") { committed = true; if (input.parentNode) input.remove(); nameEl.textContent = original; }
            e.stopPropagation();
        });
        input.addEventListener("blur", commit);
        input.addEventListener("click", (e) => e.stopPropagation());
    }

    // ── Inline new-file/folder input at top of tree or inside a dir ──
    function _startInlineCreate(parentPath, isFolder) {
        // Find the container to insert the input row into
        let container = $fileTree;
        if (parentPath) {
            // Find the tree-children container for this directory
            const dirItem = $fileTree.querySelector(`.tree-item[data-path="${CSS.escape(parentPath)}"][data-type="dir"]`);
            if (dirItem) {
                const wrapper = dirItem.parentElement;
                const children = wrapper?.querySelector(".tree-children");
                if (children) {
                    // Expand the directory if collapsed
                    if (children.style.display === "none") {
                        children.style.display = "";
                        const chevron = wrapper.querySelector(".tree-chevron");
                        if (chevron) chevron.classList.add("open");
                        treeState[parentPath] = true;
                    }
                    container = children;
                }
            }
        }
        const depth = parentPath ? parentPath.split("/").length : 0;
        const row = document.createElement("div");
        row.innerHTML = `
            <div class="tree-item" style="padding-left:${8 + depth * 16 + (isFolder ? 0 : 16)}px">
                <span class="tree-icon">${isFolder ? "📁" : "📄"}</span>
                <span class="tree-file-name"><input type="text" class="tree-rename-input" placeholder="${isFolder ? "folder name" : "filename"}" /></span>
            </div>
        `;
        const input = row.querySelector("input");
        container.insertBefore(row, container.firstChild);
        input.focus();

        let committed = false;
        function commit() {
            if (committed) return;
            committed = true;
            const name = input.value.trim();
            row.remove();
            if (!name) return;
            const fullPath = parentPath ? parentPath + "/" + name : name;
            if (isFolder) {
                fetch("/api/file/mkdir", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ path: fullPath })
                }).then(() => refreshTree());
            } else {
                fetch("/api/file", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ path: fullPath, content: "" })
                }).then(() => { refreshTree(); openFile(fullPath); });
            }
        }
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); commit(); }
            if (e.key === "Escape") { committed = true; row.remove(); }
            e.stopPropagation();
        });
        input.addEventListener("blur", commit);
        input.addEventListener("click", (e) => e.stopPropagation());
    }

    // ── Context menu action handler ──────────────────────────────
    if ($ctxMenu) {
        $ctxMenu.addEventListener("click", async (e) => {
            const btn = e.target.closest(".ctx-menu-item");
            if (!btn) return;
            e.stopPropagation();
            const action = btn.dataset.action;
            const target = _ctxTarget;
            _hideCtxMenu();
            if (!action) return;

            // Determine the parent directory for new file/folder creation
            const parentDir = target?.type === "dir" ? target.path
                            : target?.path ? target.path.replace(/\/[^/]+$/, "") || ""
                            : "";

            switch (action) {
                case "new-file":
                    _startInlineCreate(parentDir, false);
                    break;

                case "new-folder":
                    _startInlineCreate(parentDir, true);
                    break;

                case "rename": {
                    if (!target?.path) break;
                    const treeItem = $fileTree.querySelector(`.tree-item[data-path="${CSS.escape(target.path)}"]`);
                    if (!treeItem) break;
                    const oldName = target.path.split("/").pop();
                    _startInlineRename(treeItem, oldName, async (newName) => {
                        const dir = target.path.replace(/\/[^/]+$/, "");
                        const newPath = dir ? dir + "/" + newName : newName;
                        try {
                            const res = await fetch("/api/file/rename", {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ old_path: target.path, new_path: newPath })
                            });
                            if (res.ok) {
                                refreshTree();
                                // Update any open tab for this file
                                const tab = $tabBar.querySelector(`.tab[data-path="${CSS.escape(target.path)}"]`);
                                if (tab) {
                                    tab.dataset.path = newPath;
                                    const nameEl = tab.querySelector(".tab-name");
                                    if (nameEl) nameEl.textContent = newName;
                                }
                            }
                        } catch {}
                    });
                    break;
                }

                case "delete": {
                    if (!target?.path) break;
                    const name = target.path.split("/").pop();
                    const kind = target.type === "dir" ? "folder" : "file";
                    if (!confirm(`Delete ${kind} "${name}"?`)) break;
                    try {
                        const res = await fetch("/api/file/delete", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ path: target.path })
                        });
                        if (res.ok) {
                            refreshTree();
                            // Close tab if the file was open
                            const tab = $tabBar.querySelector(`.tab[data-path="${CSS.escape(target.path)}"]`);
                            if (tab) {
                                const closeBtn = tab.querySelector(".tab-close");
                                if (closeBtn) closeBtn.click();
                            }
                        }
                    } catch {}
                    break;
                }

                case "copy-path": {
                    if (!target?.path) break;
                    try { await navigator.clipboard.writeText(target.path); } catch {}
                    break;
                }

                case "copy-relative": {
                    if (!target?.path) break;
                    try { await navigator.clipboard.writeText(target.path); } catch {}
                    break;
                }

                case "refresh":
                    refreshTree();
                    break;
            }
        });
    }

    // ── File-type-aware icons (VS Code / Cursor style) ────────
    const _ftColors = {
        py:"#3572A5",js:"#f1e05a",ts:"#3178c6",jsx:"#61dafb",tsx:"#61dafb",
        json:"#a8a8a8",html:"#e34c26",css:"#563d7c",scss:"#c6538c",less:"#1d365d",
        md:"#519aba",mdx:"#519aba",
        sh:"#89e051",bash:"#89e051",zsh:"#89e051",fish:"#89e051",
        yaml:"#cb171e",yml:"#cb171e",toml:"#9c4121",ini:"#9c4121",
        rs:"#dea584",go:"#00ADD8",rb:"#701516",java:"#b07219",kt:"#A97BFF",
        c:"#555555",cpp:"#f34b7d",h:"#555555",hpp:"#f34b7d",cs:"#178600",
        swift:"#F05138",m:"#438eff",
        php:"#4F5D95",lua:"#000080",r:"#198CE7",
        sql:"#e38c00",graphql:"#e10098",
        xml:"#0060ac",svg:"#ff9900",
        vue:"#41b883",svelte:"#ff3e00",astro:"#ff5a03",
        env:"#ecd53f",dockerfile:"#384d54",docker:"#384d54",
        gitignore:"#f05033",git:"#f05033",
        lock:"#6a737d",log:"#6a737d",
        txt:"#6a737d",csv:"#6a737d",
        png:"#a66e28",jpg:"#a66e28",jpeg:"#a66e28",gif:"#a66e28",webp:"#a66e28",ico:"#a66e28",
        wasm:"#654ff0",
        tf:"#5c4ee5",hcl:"#5c4ee5",
    };
    const _ftLabels = {
        py:"PY",js:"JS",ts:"TS",jsx:"JSX",tsx:"TSX",
        json:"{}",html:"<>",css:"#",scss:"S#",less:"L#",
        md:"M\u2193",mdx:"MDX",
        sh:">_",bash:">_",zsh:">_",
        yaml:"\u2261",yml:"\u2261",toml:"T",ini:"I",
        rs:"Rs",go:"Go",rb:"Rb",java:"J",kt:"Kt",
        c:"C",cpp:"C+",h:"H",hpp:"H+",cs:"C#",
        swift:"Sw",m:"Ob",
        php:"P",lua:"Lu",r:"R",
        sql:"SQ",graphql:"GQ",
        xml:"<>",svg:"SV",
        vue:"V",svelte:"Sv",astro:"A",
        env:".e",dockerfile:"D",docker:"D",
        gitignore:"\u2718",git:"G",
        lock:"\uD83D\uDD12",log:"\u25B6",
        txt:"Tx",csv:"\u2261",
        png:"\u25A3",jpg:"\u25A3",jpeg:"\u25A3",gif:"\u25A3",webp:"\u25A3",ico:"\u25A3",
    };

    function fileTypeIcon(pathOrExt, size) {
        const s = size || 14;
        let ext = pathOrExt;
        if (pathOrExt && pathOrExt.includes(".")) {
            ext = pathOrExt.split(".").pop().toLowerCase();
        }
        if (ext) ext = ext.toLowerCase();
        // Special filenames
        const basename = pathOrExt ? pathOrExt.split("/").pop().toLowerCase() : "";
        if (basename === "dockerfile" || basename.startsWith("dockerfile.")) ext = "dockerfile";
        else if (basename === ".env" || basename.startsWith(".env.")) ext = "env";
        else if (basename === ".gitignore") ext = "gitignore";

        const color = _ftColors[ext] || "#8b949e";
        const label = _ftLabels[ext] || (ext ? ext.slice(0,2).toUpperCase() : "F");
        return `<span class="ft-icon" style="width:${s}px;height:${s}px;background:${color}20;color:${color};border:1px solid ${color}40;font-size:${Math.max(s-5,7)}px">${label}</span>`;
    }

    function fileIcon(ext) {
        return fileTypeIcon(ext, 15);
    }

    function markFileModified(path) {
        modifiedFiles.add(path);
        // Update tree indicator
        document.querySelectorAll(`.tree-item[data-path="${CSS.escape(path)}"]`).forEach(el => el.classList.add("modified"));
        // Update tab indicator
        const tab = $tabBar.querySelector(`.tab[data-path="${CSS.escape(path)}"]`);
        if (tab) tab.classList.add("modified");
    }

    let _refreshTreeTimer = null;
    let _refreshTreePromise = null;

    async function _doRefreshTree() {
        await fetchGitStatus();
        $fileTree.innerHTML = "";
        if ($fileFilter) $fileFilter.value = "";
        loadTree();
        updateModifiedFilesBar();
    }

    function refreshTree() {
        if (_refreshTreeTimer) clearTimeout(_refreshTreeTimer);
        if (!_refreshTreePromise) {
            _refreshTreePromise = _doRefreshTree().finally(() => { _refreshTreePromise = null; });
            return _refreshTreePromise;
        }
        return new Promise(resolve => {
            _refreshTreeTimer = setTimeout(() => {
                _refreshTreeTimer = null;
                _refreshTreePromise = _doRefreshTree().finally(() => { _refreshTreePromise = null; });
                _refreshTreePromise.then(resolve);
            }, 300);
        });
    }

    /* ── File filter: fuzzy search in explorer tree ─────────────── */
    let _fileFilterTimeout = null;
    let _allFilePaths = null;

    function fuzzyMatch(query, text) {
        query = query.toLowerCase();
        text = text.toLowerCase();
        let qi = 0;
        for (let ti = 0; ti < text.length && qi < query.length; ti++) {
            if (text[ti] === query[qi]) qi++;
        }
        return qi === query.length;
    }

    async function fetchAllFiles() {
        try {
            const res = await fetch("/api/files?recursive=true");
            if (res.ok) {
                _allFilePaths = await res.json();
                return _allFilePaths;
            }
        } catch {}
        return null;
    }

    function renderFilteredFiles(matches) {
        $fileTree.innerHTML = "";
        if (!matches || matches.length === 0) {
            $fileTree.innerHTML = `<div class="info-msg" style="padding:10px;opacity:0.6">No files match filter</div>`;
            return;
        }
        matches.slice(0, 100).forEach(item => {
            const el = document.createElement("div");
            const icon = fileIcon(item.ext || item.name.split(".").pop());
            const pathNorm = (item.path || "").replace(/\\/g, "/");
            const g = gitStatus.get(pathNorm);
            const agentMod = modifiedFiles.has(item.path) || modifiedFiles.has(pathNorm);
            const statusCls = agentMod ? "modified" : (g === "M" ? "modified" : g === "A" ? "added" : g === "D" ? "deleted" : g === "U" ? "untracked" : "");
            el.innerHTML = `
                <div class="tree-item ${statusCls}" data-path="${escapeHtml(item.path)}" data-type="file" style="padding-left:12px">
                    <span class="tree-icon">${icon}</span>
                    <span class="tree-file-name">${escapeHtml(item.name)}</span>
                    <span class="tree-file-path-hint" style="margin-left:6px;opacity:0.45;font-size:11px">${escapeHtml(item.dir || "")}</span>
                </div>
            `;
            el.querySelector(".tree-item").addEventListener("click", () => openFile(item.path));
            $fileTree.appendChild(el);
        });
        if (matches.length > 100) {
            $fileTree.insertAdjacentHTML("beforeend", `<div class="info-msg" style="padding:8px;opacity:0.5">${matches.length - 100} more...</div>`);
        }
    }

    if ($fileFilter) {
        $fileFilter.addEventListener("input", () => {
            clearTimeout(_fileFilterTimeout);
            const q = $fileFilter.value.trim();
            if (!q) {
                $fileTree.innerHTML = "";
                loadTree();
                return;
            }
            _fileFilterTimeout = setTimeout(async () => {
                if (!_allFilePaths) await fetchAllFiles();
                if (!_allFilePaths) return;
                const matches = _allFilePaths.filter(f => fuzzyMatch(q, f.name) || fuzzyMatch(q, f.path));
                renderFilteredFiles(matches);
            }, 150);
        });
        $fileFilter.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
                $fileFilter.value = "";
                $fileTree.innerHTML = "";
                loadTree();
            }
        });
    }

    /* ── @ Mention autocomplete system ────────────────────────── */
    const $mentionPopup = document.getElementById("mention-popup");
    let _mentionActive = false;
    let _mentionStart = -1;
    let _mentionSelectedIdx = 0;
    let _mentionItems = [];

    const SPECIAL_MENTIONS = [
        { label: "codebase", desc: "Inject project tree + entry points", type: "special", icon: "🗂" },
        { label: "git", desc: "Inject git diff output", type: "special", icon: "📋" },
        { label: "terminal", desc: "Inject recent terminal output", type: "special", icon: "⬛" },
    ];

    function scoredFuzzyMatch(query, text) {
        query = query.toLowerCase();
        text = text.toLowerCase();
        let qi = 0, score = 0, lastMatch = -1;
        for (let ti = 0; ti < text.length && qi < query.length; ti++) {
            if (text[ti] === query[qi]) {
                if (ti === 0 || text[ti - 1] === "/" || text[ti - 1] === "." || text[ti - 1] === "_" || text[ti - 1] === "-") {
                    score += 10;
                }
                if (lastMatch === ti - 1) score += 5;
                score += 1;
                lastMatch = ti;
                qi++;
            }
        }
        return qi === query.length ? score : -1;
    }

    function getMentionCandidates(query) {
        const results = [];
        const q = query.toLowerCase();

        for (const s of SPECIAL_MENTIONS) {
            if (!q || s.label.toLowerCase().includes(q)) {
                results.push({ ...s, score: q ? (s.label.toLowerCase().startsWith(q) ? 100 : 50) : 10 });
            }
        }

        if (_allFilePaths) {
            for (const f of _allFilePaths) {
                if (results.length >= 12) break;
                const nameScore = scoredFuzzyMatch(q, f.name);
                const pathScore = scoredFuzzyMatch(q, f.path);
                const best = Math.max(nameScore, pathScore);
                if (!q || best > 0) {
                    results.push({
                        label: f.name,
                        path: f.path,
                        dir: f.dir || "",
                        type: "file",
                        icon: "",
                        score: best > 0 ? best : 1,
                    });
                }
            }
        }

        results.sort((a, b) => b.score - a.score);
        return results.slice(0, 10);
    }

    function renderMentionPopup(items) {
        if (!$mentionPopup) return;
        if (!items || items.length === 0) {
            $mentionPopup.classList.add("hidden");
            _mentionActive = false;
            return;
        }
        _mentionItems = items;
        _mentionSelectedIdx = 0;
        let html = "";
        items.forEach((item, i) => {
            const icon = item.type === "file" ? (typeof fileTypeIcon === "function" ? fileTypeIcon(item.label, 14) : "📄") : item.icon;
            const dir = item.dir ? `<span class="mention-dir">${escapeHtml(item.dir)}</span>` : "";
            const desc = item.desc ? `<span class="mention-dir">${escapeHtml(item.desc)}</span>` : "";
            const typeBadge = item.type === "special" ? `<span class="mention-type">special</span>` : "";
            html += `<div class="mention-item${i === 0 ? " selected" : ""}" data-idx="${i}">
                <span class="mention-icon">${icon}</span>
                <span class="mention-label">${escapeHtml(item.label)}</span>
                ${dir}${desc}${typeBadge}
            </div>`;
        });
        $mentionPopup.innerHTML = html;
        $mentionPopup.classList.remove("hidden");
        _mentionActive = true;

        $mentionPopup.querySelectorAll(".mention-item").forEach(el => {
            el.addEventListener("mousedown", (e) => {
                e.preventDefault();
                selectMention(parseInt(el.dataset.idx));
            });
        });
    }

    function selectMention(idx) {
        const item = _mentionItems[idx];
        if (!item) return;
        const val = $input.value;
        const before = val.slice(0, _mentionStart);
        const after = val.slice($input.selectionStart);
        const mention = item.type === "file" ? `@${item.path} ` : `@${item.label} `;
        $input.value = before + mention + after;
        closeMentionPopup();
        $input.focus();
        const newPos = before.length + mention.length;
        $input.setSelectionRange(newPos, newPos);
    }

    function closeMentionPopup() {
        if ($mentionPopup) $mentionPopup.classList.add("hidden");
        _mentionActive = false;
        _mentionStart = -1;
    }

    function updateMentionHighlight() {
        if (!$mentionPopup) return;
        $mentionPopup.querySelectorAll(".mention-item").forEach((el, i) => {
            el.classList.toggle("selected", i === _mentionSelectedIdx);
        });
        const sel = $mentionPopup.querySelector(".mention-item.selected");
        if (sel) sel.scrollIntoView({ block: "nearest" });
    }

    if ($input && $mentionPopup) {
        $input.addEventListener("input", async () => {
            const val = $input.value;
            const pos = $input.selectionStart;

            if (_mentionActive) {
                if (pos <= _mentionStart || val[_mentionStart] !== "@") {
                    closeMentionPopup();
                    return;
                }
                const query = val.slice(_mentionStart + 1, pos);
                if (query.includes(" ") || query.includes("\n")) {
                    closeMentionPopup();
                    return;
                }
                if (!_allFilePaths) await fetchAllFiles();
                renderMentionPopup(getMentionCandidates(query));
                return;
            }

            if (pos > 0 && val[pos - 1] === "@") {
                const charBefore = pos >= 2 ? val[pos - 2] : " ";
                if (charBefore === " " || charBefore === "\n" || pos === 1) {
                    _mentionStart = pos - 1;
                    if (!_allFilePaths) await fetchAllFiles();
                    renderMentionPopup(getMentionCandidates(""));
                }
            }
        });

        $input.addEventListener("keydown", (e) => {
            if (!_mentionActive) return;
            if (e.key === "ArrowDown") {
                e.preventDefault();
                _mentionSelectedIdx = Math.min(_mentionSelectedIdx + 1, _mentionItems.length - 1);
                updateMentionHighlight();
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                _mentionSelectedIdx = Math.max(_mentionSelectedIdx - 1, 0);
                updateMentionHighlight();
            } else if (e.key === "Enter" || e.key === "Tab") {
                e.preventDefault();
                selectMention(_mentionSelectedIdx);
            } else if (e.key === "Escape") {
                e.preventDefault();
                closeMentionPopup();
            }
        });

        $input.addEventListener("blur", () => {
            setTimeout(closeMentionPopup, 150);
        });
    }

    /* ── Command Palette + Quick File Open (Cmd+Shift+P / Cmd+P) ── */
    const $cp = document.getElementById("command-palette");
    const $cpInput = document.getElementById("command-palette-input");
    const $cpResults = document.getElementById("command-palette-results");
    let _cpMode = "command";
    let _cpSelectedIdx = 0;
    let _cpItems = [];

    const CP_COMMANDS = [
        { id: "open-file", label: "Open File…", shortcut: "⌘P", icon: "📄", action: () => openCommandPalette("file") },
        { id: "search-files", label: "Search in Files", shortcut: "⌘⇧F", icon: "🔍", action: () => { closeCommandPalette(); const btn = document.getElementById("search-toggle-btn"); if (btn) btn.click(); } },
        { id: "new-chat", label: "New Chat / Reset Session", shortcut: "", icon: "💬", action: () => { closeCommandPalette(); send({ type: "reset" }); } },
        { id: "refresh-tree", label: "Refresh File Tree", shortcut: "", icon: "🔄", action: () => { closeCommandPalette(); refreshTree(); } },
        { id: "toggle-explorer", label: "Toggle Explorer Panel", shortcut: "⌘B", icon: "📁", action: () => { closeCommandPalette(); const ex = document.getElementById("file-explorer"); if (ex) ex.style.display = ex.style.display === "none" ? "" : "none"; } },
        { id: "go-to-line", label: "Go to Line…", shortcut: "⌘G", icon: "↕", action: () => { closeCommandPalette(); if (monacoInstance) monacoInstance.getAction("editor.action.gotoLine")?.run(); } },
    ];

    function openCommandPalette(mode = "command") {
        if (!$cp) return;
        _cpMode = mode;
        $cp.classList.remove("hidden");
        $cpInput.value = mode === "file" ? "" : "> ";
        $cpInput.placeholder = mode === "file" ? "Search files by name…" : "Type a command…";
        $cpInput.focus();
        updatePaletteResults();
    }

    function closeCommandPalette() {
        if ($cp) $cp.classList.add("hidden");
        $cpInput.value = "";
    }

    function updatePaletteResults() {
        const raw = $cpInput.value;
        const isCmd = raw.startsWith("> ");
        _cpMode = isCmd ? "command" : "file";
        const q = isCmd ? raw.slice(2).trim() : raw.trim();

        if (_cpMode === "command") {
            _cpItems = CP_COMMANDS.filter(c => !q || c.label.toLowerCase().includes(q.toLowerCase()));
            _cpSelectedIdx = 0;
            renderPaletteItems(_cpItems.map(c => ({
                icon: c.icon,
                label: c.label,
                shortcut: c.shortcut,
                dir: "",
            })));
        } else {
            if (!_allFilePaths) {
                fetchAllFiles().then(() => updatePaletteResults());
                return;
            }
            let matches;
            if (!q) {
                matches = _allFilePaths.slice(0, 15);
            } else {
                const scored = [];
                for (const f of _allFilePaths) {
                    const s = scoredFuzzyMatch(q, f.name);
                    const ps = scoredFuzzyMatch(q, f.path);
                    const best = Math.max(s, ps);
                    if (best > 0) scored.push({ ...f, score: best });
                }
                scored.sort((a, b) => b.score - a.score);
                matches = scored.slice(0, 15);
            }
            _cpItems = matches.map(f => ({
                icon: typeof fileTypeIcon === "function" ? fileTypeIcon(f.name, 14) : "📄",
                label: f.name,
                dir: f.dir || "",
                path: f.path,
                shortcut: "",
                _isFile: true,
            }));
            _cpSelectedIdx = 0;
            renderPaletteItems(_cpItems);
        }
    }

    function renderPaletteItems(items) {
        if (!$cpResults) return;
        if (!items.length) {
            $cpResults.innerHTML = `<div class="cp-item" style="opacity:0.4;cursor:default">No results</div>`;
            return;
        }
        let html = "";
        items.forEach((item, i) => {
            const dir = item.dir ? `<span class="cp-dir">${escapeHtml(item.dir)}</span>` : "";
            const shortcut = item.shortcut ? `<span class="cp-shortcut">${item.shortcut}</span>` : "";
            html += `<div class="cp-item${i === 0 ? " selected" : ""}" data-idx="${i}">
                <span class="cp-icon">${item.icon}</span>
                <span class="cp-label">${escapeHtml(item.label)}</span>
                ${dir}${shortcut}
            </div>`;
        });
        $cpResults.innerHTML = html;
        $cpResults.querySelectorAll(".cp-item").forEach(el => {
            el.addEventListener("mousedown", (e) => {
                e.preventDefault();
                executePaletteItem(parseInt(el.dataset.idx));
            });
        });
    }

    function executePaletteItem(idx) {
        const item = _cpItems[idx];
        if (!item) return;
        if (item._isFile || item.path) {
            closeCommandPalette();
            openFile(item.path);
        } else if (item.action) {
            item.action();
        } else {
            const cmd = CP_COMMANDS[idx];
            if (cmd && cmd.action) cmd.action();
        }
    }

    function updatePaletteHighlight() {
        if (!$cpResults) return;
        $cpResults.querySelectorAll(".cp-item").forEach((el, i) => {
            el.classList.toggle("selected", i === _cpSelectedIdx);
        });
        const sel = $cpResults.querySelector(".cp-item.selected");
        if (sel) sel.scrollIntoView({ block: "nearest" });
    }

    if ($cp && $cpInput) {
        $cpInput.addEventListener("input", updatePaletteResults);
        $cpInput.addEventListener("keydown", (e) => {
            if (e.key === "ArrowDown") {
                e.preventDefault();
                _cpSelectedIdx = Math.min(_cpSelectedIdx + 1, _cpItems.length - 1);
                updatePaletteHighlight();
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                _cpSelectedIdx = Math.max(_cpSelectedIdx - 1, 0);
                updatePaletteHighlight();
            } else if (e.key === "Enter") {
                e.preventDefault();
                executePaletteItem(_cpSelectedIdx);
            } else if (e.key === "Escape") {
                e.preventDefault();
                closeCommandPalette();
            }
        });
        $cp.querySelector(".command-palette-backdrop").addEventListener("click", closeCommandPalette);
    }

    document.addEventListener("keydown", (e) => {
        const isMac = navigator.platform.toUpperCase().indexOf("MAC") >= 0;
        const mod = isMac ? e.metaKey : e.ctrlKey;

        if (mod && e.shiftKey && e.key.toLowerCase() === "p") {
            e.preventDefault();
            openCommandPalette("command");
        } else if (mod && !e.shiftKey && e.key.toLowerCase() === "p") {
            e.preventDefault();
            openCommandPalette("file");
        }
    });

    async function updateModifiedFilesBar() {
        if (!$modifiedFilesBar || !$modifiedFilesToggle || !$modifiedFilesList || !$modifiedFilesDropdown) return;
        try {
            const res = await fetch("/api/git-diff-stats?t=" + Date.now());
            if (!res.ok) {
                $modifiedFilesBar.classList.add("hidden");
                if ($modifiedFilesTotalsTop) $modifiedFilesTotalsTop.classList.add("hidden");
                return;
            }
            const data = await res.json();
            const files = data.files || [];
            const totalAdd = data.total_additions || 0;
            const totalDel = data.total_deletions || 0;
            if (files.length === 0) {
                $modifiedFilesBar.classList.add("hidden");
                if ($modifiedFilesTotalsTop) $modifiedFilesTotalsTop.classList.add("hidden");
                return;
            }
            $modifiedFilesBar.classList.remove("hidden");
            if ($modifiedFilesTotalsTop) {
                $modifiedFilesTotalsTop.classList.remove("hidden");
                const topAdd = $modifiedFilesTotalsTop.querySelector(".modified-files-totals-top-add");
                const topDel = $modifiedFilesTotalsTop.querySelector(".modified-files-totals-top-del");
                const topCount = $modifiedFilesTotalsTop.querySelector(".modified-files-totals-top-count");
                if (topAdd) topAdd.textContent = "+" + totalAdd;
                if (topDel) topDel.textContent = "\u2212" + totalDel;
                if (topCount) topCount.textContent = files.length + " file" + (files.length !== 1 ? "s" : "");
            }
            const addEl = $modifiedFilesToggle.querySelector(".modified-files-add");
            const delEl = $modifiedFilesToggle.querySelector(".modified-files-del");
            const countEl = $modifiedFilesToggle.querySelector(".modified-files-count");
            if (addEl) addEl.textContent = "+" + totalAdd;
            if (delEl) delEl.textContent = "\u2212" + totalDel;
            if (countEl) countEl.textContent = files.length + " file" + (files.length !== 1 ? "s" : "");
            $modifiedFilesList.innerHTML = files.map((f) => {
                const path = (f.path || "").replace(/\\/g, "/");
                const add = f.additions != null ? f.additions : 0;
                const del = f.deletions != null ? f.deletions : 0;
                const icon = fileTypeIcon(path, 14);
                return `<div class="modified-files-item" data-path="${escapeHtml(path)}" title="Open ${escapeHtml(path)}">
                    <span class="file-icon">${icon}</span>
                    <span class="file-path">${escapeHtml(path)}</span>
                    <span class="file-stats"><span class="add">+${add}</span><span class="del">−${del}</span></span>
                </div>`;
            }).join("");
            $modifiedFilesList.querySelectorAll(".modified-files-item").forEach((el) => {
                el.addEventListener("click", () => {
                    const p = el.dataset.path || "";
                    if (p) openFile(p);
                    $modifiedFilesDropdown.classList.add("hidden");
                    $modifiedFilesBar.setAttribute("aria-expanded", "false");
                });
            });
        } catch (e) {
            $modifiedFilesBar.classList.add("hidden");
            if ($modifiedFilesTotalsTop) $modifiedFilesTotalsTop.classList.add("hidden");
        }
    }
    const MODIFIED_FILES_POLL_MS = 8000;
    let modifiedFilesPollTimer = null;
    function startModifiedFilesPolling() {
        if (modifiedFilesPollTimer) return;
        modifiedFilesPollTimer = setInterval(updateModifiedFilesBar, MODIFIED_FILES_POLL_MS);
    }
    function stopModifiedFilesPolling() {
        if (modifiedFilesPollTimer) {
            clearInterval(modifiedFilesPollTimer);
            modifiedFilesPollTimer = null;
        }
    }
    const GIT_STATUS_POLL_MS = 10000;
    let gitStatusPollTimer = null;
    function startGitStatusPolling() {
        if (gitStatusPollTimer) return;
        gitStatusPollTimer = setInterval(() => {
            fetchGitStatus();
        }, GIT_STATUS_POLL_MS);
    }
    function stopGitStatusPolling() {
        if (gitStatusPollTimer) {
            clearInterval(gitStatusPollTimer);
            gitStatusPollTimer = null;
        }
    }
    startGitStatusPolling();
    // Review files bar: toggle expand list
    if ($stickyTodoToggle && $stickyTodoList && $stickyTodoBar) {
        $stickyTodoToggle.addEventListener("click", () => {
            const expanded = $stickyTodoBar.getAttribute("data-expanded") === "true";
            $stickyTodoBar.setAttribute("data-expanded", expanded ? "false" : "true");
            $stickyTodoList.classList.toggle("hidden", expanded);
            $stickyTodoToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
        });
    }
    if ($stickyTodoAddBtn && $stickyTodoAddInput) {
        function submitStickyAddTask() {
            const content = ($stickyTodoAddInput.value || "").trim();
            if (!content) return;
            send({ type: "add_todo", content });
            $stickyTodoAddInput.value = "";
        }
        $stickyTodoAddBtn.addEventListener("click", submitStickyAddTask);
        $stickyTodoAddInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitStickyAddTask(); } });
    }
    if ($chatComposerStatsToggle && $chatComposerFilesDropup && $chatComposerStats) {
        function toggleComposerFilesDropup() {
            const expanded = $chatComposerStats.getAttribute("data-expanded") === "true";
            $chatComposerStats.setAttribute("data-expanded", expanded ? "false" : "true");
            $chatComposerFilesDropup.classList.toggle("hidden", expanded);
            $chatComposerStatsToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
            $chatComposerFilesDropup.setAttribute("aria-hidden", expanded ? "true" : "false");
        }
        $chatComposerStatsToggle.addEventListener("click", toggleComposerFilesDropup);
    }
    // Chat menu (…) dropdown
    if ($chatMenuBtn && $chatMenuDropdown) {
        $chatMenuBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            $chatMenuDropdown.classList.toggle("hidden");
        });
        $chatMenuDropdown.addEventListener("click", (e) => e.stopPropagation());
        document.addEventListener("click", () => {
            $chatMenuDropdown.classList.add("hidden");
        });
    }
    $refreshTree.addEventListener("click", refreshTree);
    if ($sourceControlRefreshBtn) {
        $sourceControlRefreshBtn.addEventListener("click", async () => {
            await fetchGitStatus();
            renderSourceControl();
            $fileTree.innerHTML = "";
            loadTree();
        });
    }

    // ================================================================
    // MONACO EDITOR
    // ================================================================

    let monacoReady = false;

    async function initMonaco() {
        const m = await window.monacoReady;
        m.editor.defineTheme("bedrock-dark", {
            base: "vs-dark",
            inherit: true,
            rules: [],
            colors: {
                "editor.background": "#1a1a2e",
                "editor.foreground": "#e8eaf0",
                "editorLineNumber.foreground": "#5c6370",
                "editorCursor.foreground": "#6c9fff",
                "editor.selectionBackground": "#264f78",
                "editor.lineHighlightBackground": "#16213e",
                "editorWidget.background": "#16213e",
                "editorWidget.border": "#2a3a5c",
                "editorSuggestWidget.background": "#16213e",
                "editorSuggestWidget.border": "#2a3a5c",
            }
        });

        // Go to Definition provider (F12 / Cmd+Click)
        m.languages.registerDefinitionProvider("*", {
            provideDefinition: async (model, position) => {
                const word = model.getWordAtPosition(position);
                if (!word) return null;
                try {
                    const res = await fetch(`/api/find-symbol?symbol=${encodeURIComponent(word.word)}&kind=definition`);
                    if (!res.ok) return null;
                    const data = await res.json();
                    if (!data.results || !data.results.length) return null;
                    const r = data.results[0];
                    openFile(r.path).then(() => {
                        if (monacoInstance) {
                            monacoInstance.revealLineInCenter(r.line);
                            monacoInstance.setPosition({ lineNumber: r.line, column: 1 });
                        }
                    });
                    const existingModel = m.editor.getModel(m.Uri.file(r.path));
                    if (existingModel) {
                        return [{ uri: m.Uri.file(r.path), range: new m.Range(r.line, 1, r.line, 1) }];
                    }
                    return null;
                } catch { return null; }
            }
        });

        // Find References provider (Shift+F12)
        m.languages.registerReferenceProvider("*", {
            provideReferences: async (model, position) => {
                const word = model.getWordAtPosition(position);
                if (!word) return [];
                try {
                    const res = await fetch(`/api/find-symbol?symbol=${encodeURIComponent(word.word)}&kind=all`);
                    if (!res.ok) return [];
                    const data = await res.json();
                    if (!data.results || !data.results.length) return [];
                    return data.results.map(r => {
                        const existingModel = m.editor.getModel(m.Uri.file(r.path));
                        if (existingModel) {
                            return { uri: m.Uri.file(r.path), range: new m.Range(r.line, 1, r.line, 1) };
                        }
                        return { uri: model.uri, range: new m.Range(1, 1, 1, 1) };
                    }).filter(r => r.uri !== model.uri || r.range.startLineNumber > 1);
                } catch { return []; }
            }
        });

        // Register editor opener so Go to Definition opens files in our editor
        if (m.editor.registerEditorOpener) {
            m.editor.registerEditorOpener({
                openCodeEditor(source, resource, selectionOrPosition) {
                    const filePath = resource.path.startsWith("/") ? resource.path.slice(1) : resource.path;
                    openFile(filePath).then(() => {
                        if (monacoInstance && selectionOrPosition) {
                            const line = selectionOrPosition.startLineNumber || selectionOrPosition.lineNumber || 1;
                            const col = selectionOrPosition.startColumn || selectionOrPosition.column || 1;
                            monacoInstance.setPosition({ lineNumber: line, column: col });
                            monacoInstance.revealLineInCenter(line);
                        }
                    });
                    return true;
                }
            });
        }

        monacoReady = true;
    }

    async function openFile(path) {
        path = (path || "").replace(/\\/g, "/").trim();
        if (!path || path.endsWith("/")) return;
        await initMonaco();
        const m = await window.monacoReady;

        // Save current viewState
        if (activeTab && monacoInstance) {
            const info = openTabs.get(activeTab);
            if (info) info.viewState = monacoInstance.saveViewState();
        }

        // If already open, switch to it
        if (openTabs.has(path)) {
            switchToTab(path);
            return;
        }

        // Fetch file content
        try {
            const res = await fetch(`/api/file?path=${encodeURIComponent(path)}`);
            if (!res.ok) {
                let msg = "Failed to open file";
                try {
                    const errBody = await res.json();
                    if (errBody && errBody.error) msg = errBody.error;
                } catch (_) {}
                showToast(msg);
                return;
            }
            const content = await res.text();
            const ext = path.split(".").pop();
            const lang = langFromExt(ext);
            const model = m.editor.createModel(content, lang, m.Uri.file(path));

            openTabs.set(path, { model, viewState: null, content });
            createTabEl(path);
            switchToTab(path);
        } catch (e) {
            showToast("Error opening file");
        }
    }

    function switchToTab(path) {
        if (!openTabs.has(path)) return;

        // Hide welcome
        $editorWelcome.classList.add("hidden");

        // Save current viewState
        if (activeTab && monacoInstance && openTabs.has(activeTab)) {
            openTabs.get(activeTab).viewState = monacoInstance.saveViewState();
        }

        // Destroy diff editor if active
        if (diffEditorInstance) {
            diffEditorInstance.dispose();
            diffEditorInstance = null;
        }

        activeTab = path;
        const info = openTabs.get(path);

        // Create or reconfigure editor
        if (!monacoInstance) {
            monacoInstance = monaco.editor.create($monacoEl, {
                model: info.model,
                theme: "bedrock-dark",
                fontSize: 13,
                fontFamily: "'SF Mono', 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace",
                minimap: { enabled: true, maxColumn: 80 },
                scrollBeyondLastLine: false,
                automaticLayout: true,
                lineNumbers: "on",
                renderLineHighlight: "gutter",
                padding: { top: 8 },
                "bracketPairColorization.enabled": true,
                guides: { bracketPairs: true, indentation: true },
                stickyScroll: { enabled: true },
            });

            // Save on Cmd/Ctrl+S
            monacoInstance.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveCurrentFile());
        } else {
            monacoInstance.setModel(info.model);
        }

        if (info.viewState) monacoInstance.restoreViewState(info.viewState);
        monacoInstance.focus();

        // Apply inline diff decorations: agent-modified first, else git changes
        if (modifiedFiles.has(path)) {
            applyInlineDiffDecorations(path);
        } else {
            const pathNorm = (path || "").replace(/\\/g, "/");
            const g = gitStatus.get(pathNorm);
            if (g === "M" || g === "A" || g === "U") applyGitInlineDiffDecorations(path);
            else clearDiffDecorations(path);
        }

        // Update tab bar UI
        $tabBar.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
        const tabEl = $tabBar.querySelector(`.tab[data-path="${CSS.escape(path)}"]`);
        if (tabEl) tabEl.classList.add("active");

        // Update tree selection
        $fileTree.querySelectorAll(".tree-item").forEach(t => t.classList.remove("active"));
        const treeItem = $fileTree.querySelector(`.tree-item[data-path="${CSS.escape(path)}"]`);
        if (treeItem) treeItem.classList.add("active");

        // Update breadcrumb
        updateBreadcrumb(path);
    }

    const $breadcrumb = document.getElementById("editor-breadcrumb");
    function updateBreadcrumb(path) {
        if (!$breadcrumb) return;
        if (!path) {
            $breadcrumb.classList.add("hidden");
            return;
        }
        $breadcrumb.classList.remove("hidden");
        const parts = path.replace(/\\/g, "/").split("/");
        let html = "";
        parts.forEach((part, i) => {
            if (i > 0) html += `<span class="bc-sep">›</span>`;
            const isCurrent = i === parts.length - 1;
            const partPath = parts.slice(0, i + 1).join("/");
            html += `<span class="bc-part${isCurrent ? " current" : ""}" data-path="${escapeHtml(partPath)}">${escapeHtml(part)}</span>`;
        });
        $breadcrumb.innerHTML = html;
        $breadcrumb.querySelectorAll(".bc-part").forEach(el => {
            el.addEventListener("click", () => {
                const p = el.dataset.path;
                if (openTabs.has(p)) switchToTab(p);
            });
        });
    }

    function createTabEl(path) {
        const tab = document.createElement("div");
        tab.className = "tab active";
        tab.dataset.path = path;
        const pathNorm = (path || "").replace(/\\/g, "/");
        const g = gitStatus.get(pathNorm);
        if (modifiedFiles.has(path) || modifiedFiles.has(pathNorm) || g === "M" || g === "A" || g === "U") tab.classList.add("modified");

        tab.innerHTML = `<span class="tab-name">${escapeHtml(basename(path))}</span><span class="tab-close">\u00D7</span>`;
        tab.addEventListener("click", (e) => {
            if (e.target.classList.contains("tab-close")) { closeTab(path); return; }
            switchToTab(path);
        });
        $tabBar.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
        $tabBar.appendChild(tab);
    }

    function closeTab(path) {
        const info = openTabs.get(path);
        if (info) { info.model.dispose(); openTabs.delete(path); }
        diffDecorationIds.delete(path);

        const tabEl = $tabBar.querySelector(`.tab[data-path="${CSS.escape(path)}"]`);
        if (tabEl) tabEl.remove();

        if (activeTab === path) {
            activeTab = null;
            const remaining = [...openTabs.keys()];
            if (remaining.length > 0) {
                switchToTab(remaining[remaining.length - 1]);
            } else {
                if (monacoInstance) { monacoInstance.setModel(null); }
                $editorWelcome.classList.remove("hidden");
            }
        }
    }

    async function saveCurrentFile() {
        if (!activeTab || !monacoInstance) return;
        const content = monacoInstance.getValue();
        try {
            const res = await fetch("/api/file", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: activeTab, content }),
            });
            const data = await res.json();
            if (data.ok) showToast("Saved " + basename(activeTab));
            else showToast("Save failed: " + (data.error || "unknown"));
        } catch { showToast("Save error"); }
    }

    async function openDiffForFile(path) {
        await initMonaco();
        const m = await window.monacoReady;

        try {
            const res = await fetch(`/api/file-diff?path=${encodeURIComponent(path)}`);
            if (!res.ok) { showToast("No diff available"); return; }
            const data = await res.json();

            // Hide welcome
            $editorWelcome.classList.add("hidden");

            // Save current state
            if (activeTab && monacoInstance && openTabs.has(activeTab)) {
                openTabs.get(activeTab).viewState = monacoInstance.saveViewState();
            }

            // Dispose normal editor temporarily
            if (monacoInstance) { monacoInstance.dispose(); monacoInstance = null; }
            if (diffEditorInstance) { diffEditorInstance.dispose(); }

            const originalModel = m.editor.createModel(data.original || "", langFromExt(path.split(".").pop()), m.Uri.parse("original:///" + path));
            const modifiedModel = m.editor.createModel(data.current || "", langFromExt(path.split(".").pop()), m.Uri.parse("modified:///" + path));

            diffEditorInstance = m.editor.createDiffEditor($monacoEl, {
                theme: "bedrock-dark",
                fontSize: 13,
                fontFamily: "'SF Mono', 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace",
                automaticLayout: true,
                readOnly: true,
                renderSideBySide: true,
                scrollBeyondLastLine: false,
                padding: { top: 8 },
            });

            diffEditorInstance.setModel({
                original: originalModel,
                modified: modifiedModel,
            });

            // Mark tab as diff mode
            activeTab = path;
            $tabBar.querySelectorAll(".tab").forEach(t => t.classList.remove("active", "diff-mode"));
            let tabEl = $tabBar.querySelector(`.tab[data-path="${CSS.escape(path)}"]`);
            if (!tabEl) {
                openTabs.set(path, { model: modifiedModel, viewState: null, content: data.current });
                createTabEl(path);
                tabEl = $tabBar.querySelector(`.tab[data-path="${CSS.escape(path)}"]`);
            }
            if (tabEl) { tabEl.classList.add("active", "diff-mode"); }

        } catch (e) {
            showToast("Error loading diff");
        }
    }

    // Reload file content in editor after agent modification
    async function reloadFileInEditor(path) {
        if (!openTabs.has(path)) return;
        try {
            const res = await fetch(`/api/file?path=${encodeURIComponent(path)}`);
            if (!res.ok) return;
            const content = await res.text();
            const info = openTabs.get(path);
            if (info && info.model) {
                info.model.setValue(content);
                info.content = content;
            }
            // Apply inline diff decorations if this file is currently active
            if (activeTab === path) {
                await applyInlineDiffDecorations(path);
            }
        } catch {}
    }

    // ================================================================
    // INLINE DIFF DECORATIONS (Cursor-style gutter highlights)
    // ================================================================

    // Stores decoration IDs per file path so we can clear/update them
    const diffDecorationIds = new Map();  // path -> string[]

    /**
     * Lightweight line-diff: computes added, modified, and deleted line ranges
     * by comparing original and current text line-by-line using an LCS approach.
     * Returns { added: [{start,end}], modified: [{start,end}], deleted: [lineAfter] }
     * where line numbers are 1-indexed (for Monaco).
     */
    function computeLineDiff(originalText, currentText) {
        const origLines = (originalText || "").split("\n");
        const currLines = (currentText || "").split("\n");
        const result = { added: [], modified: [], deleted: [] };

        // Simple LCS-based diff using Hunt-McIlroy approach
        // Build a map of original line content -> list of indices
        const origMap = new Map();
        origLines.forEach((line, i) => {
            if (!origMap.has(line)) origMap.set(line, []);
            origMap.get(line).push(i);
        });

        // Find longest common subsequence using patience-like matching
        // For performance, use a simpler O(n*m) approach with space optimization
        const n = origLines.length, m = currLines.length;

        // For files under 5000 lines, use full DP; otherwise use a heuristic
        if (n * m > 25_000_000) {
            // Heuristic for very large files: simple line-by-line comparison
            const maxLen = Math.max(n, m);
            for (let i = 0; i < maxLen; i++) {
                if (i >= n) {
                    // Added line in current
                    result.added.push({ start: i + 1, end: i + 1 });
                } else if (i >= m) {
                    // Deleted line from original
                    result.deleted.push(m > 0 ? m : 1);
                } else if (origLines[i] !== currLines[i]) {
                    result.modified.push({ start: i + 1, end: i + 1 });
                }
            }
        } else {
            // Full diff using DP to find LCS
            // Build edit script
            const dp = new Array(n + 1);
            for (let i = 0; i <= n; i++) dp[i] = new Uint16Array(m + 1);
            for (let i = 1; i <= n; i++) {
                for (let j = 1; j <= m; j++) {
                    if (origLines[i - 1] === currLines[j - 1]) {
                        dp[i][j] = dp[i - 1][j - 1] + 1;
                    } else {
                        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
                    }
                }
            }

            // Backtrack to find which lines are common
            const origMatched = new Set();
            const currMatched = new Set();
            let i = n, j = m;
            while (i > 0 && j > 0) {
                if (origLines[i - 1] === currLines[j - 1]) {
                    origMatched.add(i - 1);
                    currMatched.add(j - 1);
                    i--; j--;
                } else if (dp[i - 1][j] >= dp[i][j - 1]) {
                    i--;
                } else {
                    j--;
                }
            }

            // Lines in current not in LCS = added or modified
            // Lines in original not in LCS = deleted
            const deletedOrigIndices = [];
            for (let k = 0; k < n; k++) {
                if (!origMatched.has(k)) deletedOrigIndices.push(k);
            }

            const addedCurrIndices = [];
            for (let k = 0; k < m; k++) {
                if (!currMatched.has(k)) addedCurrIndices.push(k);
            }

            // Classify: if roughly the same position has both a deletion and an addition,
            // it's a modification; otherwise pure add or delete
            const delSet = new Set(deletedOrigIndices);
            const addSet = new Set(addedCurrIndices);

            // Try to pair up deletions and additions that are near each other
            const pairedAdds = new Set();
            for (const dIdx of deletedOrigIndices) {
                // Look for an addition near the same line number
                for (const aIdx of addedCurrIndices) {
                    if (!pairedAdds.has(aIdx) && Math.abs(dIdx - aIdx) <= 3) {
                        result.modified.push({ start: aIdx + 1, end: aIdx + 1 });
                        pairedAdds.add(aIdx);
                        break;
                    }
                }
            }

            // Remaining unpaired additions
            for (const aIdx of addedCurrIndices) {
                if (!pairedAdds.has(aIdx)) {
                    result.added.push({ start: aIdx + 1, end: aIdx + 1 });
                }
            }

            // Remaining unpaired deletions → mark the position in current file
            const deletedUnpaired = deletedOrigIndices.filter(dIdx => {
                for (const aIdx of pairedAdds) {
                    if (Math.abs(dIdx - aIdx) <= 3) return false;
                }
                return true;
            });
            for (const dIdx of deletedUnpaired) {
                // Find nearest line in current file
                const nearLine = Math.min(dIdx + 1, m) || 1;
                result.deleted.push(nearLine);
            }
        }

        // Merge consecutive ranges
        function mergeRanges(ranges) {
            if (ranges.length === 0) return [];
            ranges.sort((a, b) => a.start - b.start);
            const merged = [ranges[0]];
            for (let k = 1; k < ranges.length; k++) {
                const last = merged[merged.length - 1];
                if (ranges[k].start <= last.end + 1) {
                    last.end = Math.max(last.end, ranges[k].end);
                } else {
                    merged.push(ranges[k]);
                }
            }
            return merged;
        }

        result.added = mergeRanges(result.added);
        result.modified = mergeRanges(result.modified);
        result.deleted = [...new Set(result.deleted)].sort((a, b) => a - b);

        return result;
    }

    /**
     * Fetch original content from /api/file-diff and apply gutter decorations
     * to the Monaco editor for the given file path.
     */
    async function applyInlineDiffDecorations(path) {
        if (!monacoInstance || !openTabs.has(path)) return;
        if (!modifiedFiles.has(path)) return;

        try {
            const res = await fetch(`/api/file-diff?path=${encodeURIComponent(path)}`);
            if (!res.ok) return;
            const data = await res.json();

            const info = openTabs.get(path);
            if (!info || !info.model) return;

            const currentText = info.model.getValue();
            const diff = computeLineDiff(data.original || "", currentText);

            const decorations = [];

            // Green bar — added lines
            for (const r of diff.added) {
                decorations.push({
                    range: new monaco.Range(r.start, 1, r.end, 1),
                    options: {
                        isWholeLine: true,
                        linesDecorationsClassName: "diff-gutter-added",
                        className: "diff-line-added-bg",
                        overviewRuler: { color: "#4ec9b0", position: monaco.editor.OverviewRulerLane.Left },
                    }
                });
            }

            // Blue bar — modified lines
            for (const r of diff.modified) {
                decorations.push({
                    range: new monaco.Range(r.start, 1, r.end, 1),
                    options: {
                        isWholeLine: true,
                        linesDecorationsClassName: "diff-gutter-modified",
                        className: "diff-line-modified-bg",
                        overviewRuler: { color: "#6c9fff", position: monaco.editor.OverviewRulerLane.Left },
                    }
                });
            }

            // Red marker — deleted lines
            for (const lineNum of diff.deleted) {
                decorations.push({
                    range: new monaco.Range(lineNum, 1, lineNum, 1),
                    options: {
                        isWholeLine: false,
                        linesDecorationsClassName: "diff-gutter-deleted",
                        overviewRuler: { color: "#f44747", position: monaco.editor.OverviewRulerLane.Left },
                    }
                });
            }

            // Apply decorations (clear old ones first)
            const oldIds = diffDecorationIds.get(path) || [];
            const newIds = monacoInstance.deltaDecorations(oldIds, decorations);
            diffDecorationIds.set(path, newIds);

        } catch {
            // Non-fatal — decorations are a visual enhancement
        }
    }

    /**
     * Fetch git original vs current and apply inline diff decorations (same gutter as agent diffs).
     */
    async function applyGitInlineDiffDecorations(path) {
        if (!monacoInstance || !openTabs.has(path) || typeof monaco === "undefined") return;
        try {
            const pathForApi = (path || "").replace(/\\/g, "/");
            const res = await fetch(`/api/git-file-diff?path=${encodeURIComponent(pathForApi)}`);
            if (!res.ok) return;
            const data = await res.json();
            const info = openTabs.get(path);
            if (!info || !info.model) return;
            const currentText = info.model.getValue();
            const diff = computeLineDiff(data.original || "", currentText);
            const decorations = [];
            for (const r of diff.added) {
                decorations.push({
                    range: new monaco.Range(r.start, 1, r.end, 1),
                    options: {
                        isWholeLine: true,
                        linesDecorationsClassName: "diff-gutter-added",
                        className: "diff-line-added-bg",
                        overviewRuler: { color: "#4ec9b0", position: monaco.editor.OverviewRulerLane.Left },
                    }
                });
            }
            for (const r of diff.modified) {
                decorations.push({
                    range: new monaco.Range(r.start, 1, r.end, 1),
                    options: {
                        isWholeLine: true,
                        linesDecorationsClassName: "diff-gutter-modified",
                        className: "diff-line-modified-bg",
                        overviewRuler: { color: "#6c9fff", position: monaco.editor.OverviewRulerLane.Left },
                    }
                });
            }
            for (const lineNum of diff.deleted) {
                decorations.push({
                    range: new monaco.Range(lineNum, 1, lineNum, 1),
                    options: {
                        isWholeLine: false,
                        linesDecorationsClassName: "diff-gutter-deleted",
                        overviewRuler: { color: "#f44747", position: monaco.editor.OverviewRulerLane.Left },
                    }
                });
            }
            if (activeTab !== path) return;
            const oldIds = diffDecorationIds.get(path) || [];
            const newIds = monacoInstance.deltaDecorations(oldIds, decorations);
            diffDecorationIds.set(path, newIds);
        } catch (e) {
            console.warn("Git inline diff decorations failed:", e);
        }
    }

    /**
     * Clear diff decorations for a file (called on keep/revert).
     */
    function clearDiffDecorations(path) {
        if (path) {
            const oldIds = diffDecorationIds.get(path) || [];
            if (oldIds.length && monacoInstance && activeTab === path) {
                monacoInstance.deltaDecorations(oldIds, []);
            }
            diffDecorationIds.delete(path);
        }
    }

    /**
     * Reload all files that were modified (useful after revert).
     */
    async function reloadAllModifiedFiles() {
        for (const path of [...openTabs.keys()]) {
            try {
                const res = await fetch(`/api/file?path=${encodeURIComponent(path)}`);
                if (!res.ok) continue;
                const content = await res.text();
                const info = openTabs.get(path);
                if (info && info.model) {
                    info.model.setValue(content);
                    info.content = content;
                }
            } catch {}
        }
    }

    /**
     * Clear ALL diff decorations across all files.
     */
    function clearAllDiffDecorations() {
        for (const [path, ids] of diffDecorationIds) {
            if (monacoInstance && activeTab === path && ids.length) {
                monacoInstance.deltaDecorations(ids, []);
            }
        }
        diffDecorationIds.clear();
    }

    // ================================================================
    // RESIZE HANDLES
    // ================================================================

    function setupResize(handleId, leftEl, rightEl, direction) {
        const handle = document.getElementById(handleId);
        if (!handle) return;
        let startX, startLeftW, startRightW;

        handle.addEventListener("mousedown", (e) => {
            e.preventDefault();
            startX = e.clientX;
            if (leftEl) startLeftW = leftEl.offsetWidth;
            if (rightEl) startRightW = rightEl.offsetWidth;
            handle.classList.add("dragging");
            document.body.style.cursor = "col-resize";
            document.body.style.userSelect = "none";
            document.addEventListener("mousemove", onMove);
            document.addEventListener("mouseup", onUp);
        });

        function onMove(e) {
            const dx = e.clientX - startX;
            if (direction === "left" && leftEl) {
                leftEl.style.width = Math.max(120, startLeftW + dx) + "px";
            } else if (direction === "right" && rightEl) {
                rightEl.style.width = Math.max(280, startRightW - dx) + "px";
            }
            // Re-layout editors as we drag
            if (monacoInstance) monacoInstance.layout();
            if (diffEditorInstance) diffEditorInstance.layout();
        }
        function onUp() {
            handle.classList.remove("dragging");
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            if (monacoInstance) monacoInstance.layout();
            if (diffEditorInstance) diffEditorInstance.layout();
        }
    }

    setupResize("resize-left", document.getElementById("file-explorer"), null, "left");
    setupResize("resize-right", null, document.getElementById("chat-panel"), "right");

    // Source Control panel vertical resize (expand/collapse in explorer)
    const SOURCE_CONTROL_MIN_HEIGHT = 80;
    const SOURCE_CONTROL_MAX_HEIGHT = 0.6 * window.innerHeight;
    const $resizeExplorerSc = document.getElementById("resize-explorer-sc");
    const $sourceControlPanel = document.getElementById("source-control-panel");
    const $fileExplorer = document.getElementById("file-explorer");
    if ($resizeExplorerSc && $sourceControlPanel && $fileExplorer) {
        $resizeExplorerSc.addEventListener("mousedown", (e) => {
            e.preventDefault();
            const startY = e.clientY;
            const startHeight = $sourceControlPanel.offsetHeight;
            $resizeExplorerSc.classList.add("dragging");
            document.body.style.cursor = "row-resize";
            document.body.style.userSelect = "none";
            function onMove(ev) {
                const dy = ev.clientY - startY;
                const newHeight = Math.max(SOURCE_CONTROL_MIN_HEIGHT, Math.min(SOURCE_CONTROL_MAX_HEIGHT, startHeight - dy));
                $fileExplorer.style.setProperty("--source-control-height", newHeight + "px");
            }
            function onUp() {
                $resizeExplorerSc.classList.remove("dragging");
                document.body.style.cursor = "";
                document.body.style.userSelect = "";
                document.removeEventListener("mousemove", onMove);
                document.removeEventListener("mouseup", onUp);
            }
            document.addEventListener("mousemove", onMove);
            document.addEventListener("mouseup", onUp);
        });
    }

    // ================================================================
    // INTEGRATED TERMINAL (full PTY + xterm.js)
    // ================================================================

    const TERMINAL_DEFAULT_HEIGHT = 220;
    const TERMINAL_MIN_HEIGHT = 100;

    function terminalDisconnect(clearDisplay) {
        if (terminalWs) {
            try { terminalWs.close(); } catch (e) {}
            terminalWs = null;
            setTerminalStatus("", "");
        }
        if (terminalFlushRaf) {
            cancelAnimationFrame(terminalFlushRaf);
            terminalFlushRaf = 0;
        }
        terminalOutputBuffer = "";
        terminalBlobQueue = [];
        if (clearDisplay && terminalXterm) {
            terminalXterm.clear();
            setTerminalStatus("", "");
        }
    }

    function setTerminalStatus(text, className) {
        var el = document.getElementById("terminal-status");
        if (el) {
            el.textContent = text || "";
            el.className = "terminal-status" + (className ? " " + className : "");
        }
    }

    function terminalConnect() {
        if (!window.Terminal || !$terminalXtermContainer) return;
        if (terminalWs && terminalWs.readyState === WebSocket.CONNECTING) return;
        terminalDisconnect();
        setTerminalStatus("Connecting…", "");
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = protocol + "//" + window.location.host + "/ws/terminal";
        const ws = new WebSocket(wsUrl);
        terminalWs = ws;

        ws.onopen = function () {};

        ws.onmessage = function (ev) {
            if (ev.data instanceof Blob) {
                terminalBlobQueue.push(ev.data);
                terminalProcessNextBlob(ws);
                return;
            }
            try {
                const obj = JSON.parse(ev.data);
                if (obj.type === "error") {
                    if (terminalXterm && terminalWs === ws) {
                        var msg = obj.message || obj.content || "Connection error.";
                        terminalXterm.writeln("\r\n\u001b[31mTerminal: " + msg + "\u001b[0m");
                    }
                    terminalDisconnect();
                } else if (obj.type === "ready" && terminalFitAddon && terminalXterm && terminalWs === ws) {
                    setTerminalStatus("Connected", "connected");
                    requestAnimationFrame(function () {
                        terminalFitAddon.fit();
                        terminalSendResize(terminalXterm.rows, terminalXterm.cols);
                        setTimeout(function () {
                            if (terminalFocusInput) terminalFocusInput();
                        }, 50);
                    });
                }
            } catch (e) {}
        };

        ws.onclose = function () {
            if (terminalWs === ws) {
                terminalWs = null;
                setTerminalStatus("Disconnected", "");
            }
        };
        ws.onerror = function () {
            setTerminalStatus("Error", "error");
            if (terminalXterm && terminalWs === ws) {
                terminalXterm.writeln("\r\n\u001b[31mConnection failed. Open a local project for full terminal.\u001b[0m");
            }
            terminalDisconnect();
        };
    }

    function terminalInit() {
        if (!window.Terminal || !$terminalXtermContainer) return;
        if (terminalXterm) return;
        $terminalXtermContainer.innerHTML = "";
        const term = new window.Terminal({
            cursorBlink: true,
            fontSize: 12,
            fontFamily: "ui-monospace, monospace",
            theme: {
                background: "#1a1a2e",
                foreground: "#e8eaf0",
                cursor: "#6c9fff",
                cursorAccent: "#1a1a2e",
                selectionBackground: "#264f78",
            },
        });
        const FitAddonCtor = window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon);
        const fitAddon = FitAddonCtor ? new FitAddonCtor() : null;
        if (fitAddon) term.loadAddon(fitAddon);
        term.open($terminalXtermContainer);
        terminalXterm = term;
        terminalFitAddon = fitAddon;

        function terminalSendKeys(data) {
            if (terminalWs && terminalWs.readyState === WebSocket.OPEN) {
                terminalWs.send(data);
            }
        }

        var bodyWrap = document.getElementById("terminal-body-wrap");
        terminalFocusInput = function () {
            if (bodyWrap) bodyWrap.focus();
        };
        if ($terminalPanel) {
            $terminalPanel.addEventListener("mousedown", function focusTerminalOnClick(e) {
                if (e.target.closest && e.target.closest("button")) return;
                if ($terminalPanel.contains(e.target)) {
                    e.preventDefault();
                    if (bodyWrap) bodyWrap.focus();
                }
            });
            $terminalPanel.addEventListener("keydown", function terminalPanelKeydown(e) {
                var panelHasFocus = $terminalPanel.contains(document.activeElement);
                var targetInPanel = e.target && $terminalPanel.contains(e.target);
                var panelVisible = $terminalPanel && !$terminalPanel.classList.contains("hidden");
                if (!panelVisible || (!panelHasFocus && !targetInPanel)) return;
                if (targetInPanel && !panelHasFocus && bodyWrap) {
                    bodyWrap.focus();
                }
                var key = e.key;
                var toSend = null;
                if (key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
                    toSend = key;
                } else if (e.ctrlKey && !e.metaKey && !e.altKey) {
                    if (key === "c") {
                        var sel = (terminalXterm && typeof terminalXterm.getSelection === "function") ? terminalXterm.getSelection() : "";
                        if (sel && sel.length > 0) return;
                        toSend = "\x03";
                    } else if (key === "d") { toSend = "\x04"; }
                    else if (key === "z") { toSend = "\x1a"; }
                    else if (key === "l") { toSend = "\x0c"; }
                    else if (key === "a") { toSend = "\x01"; }
                    else if (key === "e") { toSend = "\x05"; }
                    else if (key === "k") { toSend = "\x0b"; }
                    else if (key === "u") { toSend = "\x15"; }
                    else if (key === "w") { toSend = "\x17"; }
                    else if (key === "\\") { toSend = "\x1c"; }
                    else if (key >= "a" && key <= "z") { toSend = String.fromCharCode(key.charCodeAt(0) - 96); }
                    else if (key >= "@" && key <= "_") { toSend = String.fromCharCode(key.charCodeAt(0) - 64); }
                } else if (key === "Enter") { toSend = "\r"; }
                else if (key === "Backspace") { toSend = "\x7f"; }
                else if (key === "Tab") { toSend = "\t"; }
                else if (key === "Escape") { toSend = "\x1b"; }
                else if (key === "ArrowUp") { toSend = "\x1b[A"; }
                else if (key === "ArrowDown") { toSend = "\x1b[B"; }
                else if (key === "ArrowRight") { toSend = "\x1b[C"; }
                else if (key === "ArrowLeft") { toSend = "\x1b[D"; }
                if (toSend !== null) {
                    e.preventDefault();
                    e.stopPropagation();
                    if (terminalWs && terminalWs.readyState === WebSocket.OPEN) {
                        terminalSendKeys(toSend);
                    } else if (terminalXterm) {
                        terminalXterm.writeln("\r\n\u001b[33mTerminal not connected. Open a project first.\u001b[0m");
                    }
                }
            }, true);
            $terminalPanel.addEventListener("paste", function terminalPanelPaste(e) {
                var panelVisible = $terminalPanel && !$terminalPanel.classList.contains("hidden");
                var inPanel = $terminalPanel.contains(document.activeElement) || (e.target && $terminalPanel.contains(e.target));
                if (!bodyWrap || !panelVisible || !inPanel) return;
                e.preventDefault();
                var text = (e.clipboardData || window.clipboardData).getData("text");
                if (text && terminalWs && terminalWs.readyState === WebSocket.OPEN) {
                    terminalSendKeys(text);
                } else if (text && terminalXterm) {
                    terminalXterm.writeln("\r\n\u001b[33mTerminal not connected. Open a project first.\u001b[0m");
                }
            }, true);
        }

        terminalConnect();

        term.onBinary(function (data) {
            if (terminalWs && terminalWs.readyState === WebSocket.OPEN) {
                terminalWs.send(data);
            }
        });
    }

    function terminalSendResize(rows, cols) {
        if (terminalWs && terminalWs.readyState === WebSocket.OPEN) {
            terminalWs.send(JSON.stringify({ resize: [rows, cols] }));
        }
    }

    function setTerminalPanelVisible(visible) {
        if (!$terminalPanel || !$resizeTerminal) return;
        if (visible) {
            $terminalPanel.classList.remove("hidden");
            $resizeTerminal.classList.remove("hidden");
            if (!$terminalPanel.style.height || $terminalPanel.dataset.height) {
                $terminalPanel.style.height = ($terminalPanel.dataset.height || TERMINAL_DEFAULT_HEIGHT) + "px";
            }
            if ($terminalToggleBtn) $terminalToggleBtn.classList.add("active");
            requestAnimationFrame(function () {
                terminalInit();
                if (terminalFitAddon) {
                    terminalFitAddon.fit();
                    if (terminalXterm) {
                        terminalSendResize(terminalXterm.rows, terminalXterm.cols);
                    }
                }
                if (terminalXterm && (!terminalWs || terminalWs.readyState !== WebSocket.OPEN)) {
                    terminalConnect();
                }
                if (terminalFocusInput) terminalFocusInput();
                setTimeout(function () { if (terminalFocusInput) terminalFocusInput(); }, 50);
                setTimeout(function () { if (terminalFocusInput) terminalFocusInput(); }, 300);
                if (monacoInstance) monacoInstance.layout();
                if (diffEditorInstance) diffEditorInstance.layout();
            });
        } else {
            $terminalPanel.classList.add("hidden");
            $resizeTerminal.classList.add("hidden");
            if ($terminalToggleBtn) $terminalToggleBtn.classList.remove("active");
            terminalDisconnect();
        }
        requestAnimationFrame(function () {
            if (monacoInstance) monacoInstance.layout();
            if (diffEditorInstance) diffEditorInstance.layout();
        });
    }

    function toggleTerminalPanel() {
        const isHidden = $terminalPanel && $terminalPanel.classList.contains("hidden");
        setTerminalPanelVisible(!!isHidden);
    }

    if ($terminalToggleBtn) {
        $terminalToggleBtn.addEventListener("click", toggleTerminalPanel);
    }
    if ($terminalCloseBtn) {
        $terminalCloseBtn.addEventListener("click", function () { setTerminalPanelVisible(false); });
    }
    if ($terminalClearBtn) {
        $terminalClearBtn.addEventListener("click", function () {
            if (terminalXterm) terminalXterm.clear();
        });
    }

    if ($resizeTerminal && $terminalPanel) {
        $resizeTerminal.addEventListener("mousedown", function (e) {
            e.preventDefault();
            const startY = e.clientY;
            const startHeight = $terminalPanel.offsetHeight;
            $resizeTerminal.classList.add("dragging");
            document.body.style.cursor = "row-resize";
            document.body.style.userSelect = "none";
            function onMove(ev) {
                const dy = ev.clientY - startY;
                const newHeight = Math.max(TERMINAL_MIN_HEIGHT, startHeight - dy);
                $terminalPanel.style.height = newHeight + "px";
                $terminalPanel.dataset.height = newHeight;
                if (monacoInstance) monacoInstance.layout();
                if (diffEditorInstance) diffEditorInstance.layout();
                if (terminalFitAddon) {
                    terminalFitAddon.fit();
                    if (terminalXterm && terminalWs && terminalWs.readyState === WebSocket.OPEN) {
                        terminalSendResize(terminalXterm.rows, terminalXterm.cols);
                    }
                }
            }
            function onUp() {
                $resizeTerminal.classList.remove("dragging");
                document.body.style.cursor = "";
                document.body.style.userSelect = "";
                document.removeEventListener("mousemove", onMove);
                document.removeEventListener("mouseup", onUp);
            }
            document.addEventListener("mousemove", onMove);
            document.addEventListener("mouseup", onUp);
        });
    }

    window.addEventListener("resize", function () {
        if (terminalFitAddon && $terminalPanel && !$terminalPanel.classList.contains("hidden")) {
            terminalFitAddon.fit();
            if (terminalXterm && terminalWs && terminalWs.readyState === WebSocket.OPEN) {
                terminalSendResize(terminalXterm.rows, terminalXterm.cols);
            }
        }
    });

    // ================================================================
    // CHAT — Messages
    // ================================================================

    function addUserMessage(text, images) {
        const div = document.createElement("div"); div.className = "message user";
        const bubble = document.createElement("div"); bubble.className = "msg-bubble";
        const safeText = String(text || "");
        const imgs = Array.isArray(images) ? images : [];
        if (safeText) {
            const textEl = document.createElement("div");
            textEl.className = "user-message-text";
            textEl.textContent = safeText;
            bubble.appendChild(textEl);
        }
        if (imgs.length) {
            const grid = document.createElement("div");
            grid.className = "user-images-grid";
            imgs.forEach((img) => {
                const src = imageSrcForMessage(img);
                if (!src) return;
                const tile = document.createElement("div");
                tile.className = "user-image-tile";
                tile.innerHTML = `<img src="${escapeHtml(src)}" alt="${escapeHtml(img.name || "image")}">`;
                grid.appendChild(tile);
            });
            if (grid.children.length > 0) bubble.appendChild(grid);
        }
        const copyPayload = safeText || `[${imgs.length} image attachment${imgs.length === 1 ? "" : "s"}]`;
        bubble.appendChild(makeCopyBtn(copyPayload));
        div.appendChild(bubble);
        $chatMessages.appendChild(div);
        if (!sessionStartTime) {
            sessionStartTime = Date.now();
            updateFileChangesDropdown();
        }
        if ($conversationTitle && $chatMessages.querySelectorAll(".message.user").length === 1) {
            const truncated = safeText.trim().slice(0, 50);
            $conversationTitle.textContent = truncated ? (truncated + (safeText.length > 50 ? "\u2026" : "")) : "New conversation";
        }
        scrollChat();
    }

    function addAssistantMessage() {
        const div = document.createElement("div"); div.className = "message assistant";
        const bubble = document.createElement("div"); bubble.className = "msg-bubble";
        div.appendChild(bubble);
        $chatMessages.appendChild(div);
        scrollChat();
        return bubble;
    }

    function getOrCreateBubble() {
        const last = $chatMessages.querySelector(".message.assistant:last-child .msg-bubble");
        return last || addAssistantMessage();
    }

    // Thinking blocks
    let _thinkingBuffer = ""; // accumulates raw thinking text for markdown rendering
    let _thinkingRenderTimer = null; // debounce timer for markdown render
    let _thinkingUserCollapsed = false; // track if user manually collapsed during stream

    function updateThinkingHeader(block, done = false) {
        if (!block) return;
        const started = Number(block.dataset.startedAt || Date.now());
        const elapsed = Math.max(0, Math.round((Date.now() - started) / 1000));
        const titleEl = block.querySelector(".thinking-title");
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

    function createThinkingBlock() {
        const bubble = getOrCreateBubble();
        const block = document.createElement("div"); block.className = "thinking-block thinking-active";
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
        block.querySelector(".thinking-header").addEventListener("click", () => {
            block.classList.toggle("collapsed");
            if (block.classList.contains("thinking-active")) {
                _thinkingUserCollapsed = block.classList.contains("collapsed");
            }
        });
        bubble.appendChild(block);
        updateThinkingHeader(block, false);
        scrollChat();
        return block.querySelector(".thinking-content");
    }

    function _renderThinkingContent(el) {
        if (!el || !_thinkingBuffer) return;
        if (typeof marked !== "undefined") {
            el.innerHTML = marked.parse(_thinkingBuffer);
            el.querySelectorAll("pre code").forEach(b => {
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
        _thinkingRenderTimer = setTimeout(() => { _renderThinkingContent(el); }, 80);
        const block = el.closest(".thinking-block");
        if (block) updateThinkingHeader(block, false);
        scrollChat();
    }

    function finishThinking(el) {
        if (!el) return;
        if (_thinkingRenderTimer) { clearTimeout(_thinkingRenderTimer); _thinkingRenderTimer = null; }
        // Final render with full content
        _renderThinkingContent(el);
        const block = el.closest(".thinking-block"); if (!block) return;
        updateThinkingHeader(block, true);
        const spinner = block.querySelector(".thinking-spinner"); if (spinner) spinner.remove();
        // Only auto-collapse trivially short thinking (< 50 chars) — keep substantial reasoning visible
        const contentLen = (_thinkingBuffer || el.textContent || "").length;
        if (contentLen < 50) {
            block.classList.add("collapsed");
        }
        const header = block.querySelector(".thinking-header");
        if (header && !header.querySelector(".copy-btn")) {
            header.appendChild(makeCopyBtn(() => _thinkingBuffer || el.textContent));
        }
        _thinkingBuffer = "";
        _thinkingUserCollapsed = false;
    }

    // Tool blocks
    let lastToolGroup = null;
    const toolRunState = new WeakMap(); // runEl -> { name, input, output }

    function formatClock(ts) {
        return new Date(ts).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }
    function toolGroupKey(name, input) { return `${name}::${toolDesc(name, input)}`; }
    function toolActionIcon(kind) {
        const icons = {
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
        const title = toolTitle(name);
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
        if (isRunning) { showInfo("Agent is running. Cancel first to rerun."); return; }
        addUserMessage(prompt);
        setRunning(true);
        addAssistantMessage();
        const editorCtx = gatherEditorContext();
        send({ type: "task", content: prompt, ...(editorCtx ? { context: editorCtx } : {}) });
    }
    function openFileAt(path, lineNumber) {
        if (!path) return;
        openFile(path).then(() => {
            if (monacoInstance && lineNumber) {
                const ln = Math.max(1, Number(lineNumber) || 1);
                monacoInstance.setPosition({ lineNumber: ln, column: 1 });
                monacoInstance.revealLineInCenter(ln);
                monacoInstance.focus();
            }
        }).catch(() => {});
    }
    function parseLocationLine(line) {
        const m = String(line || "").trim().match(/^(.+?):(\d+):(?:(\d+):)?(.*)$/);
        if (!m) return null;
        return { path: m[1], line: Number(m[2]), col: m[3] ? Number(m[3]) : null, text: (m[4] || "").trim() };
    }
    function buildEditPreviewDiff(name, input) {
        const path = input?.path || "(unknown)";
        const MAX_PREVIEW = 80; // show up to 80 lines for streaming preview
        if (name === "Write") {
            const lines = String(input?.content || "").split("\n");
            const show = lines.slice(0, MAX_PREVIEW);
            const added = show.map(l => `+${l}`);
            if (lines.length > MAX_PREVIEW) added.push(`+... (${lines.length - MAX_PREVIEW} more lines)`);
            return `+++ ${path}\n@@ new file @@\n${added.join("\n")}`;
        }
        if (name === "Edit") {
            const oldLines = String(input?.old_string || "").split("\n");
            const newLines = String(input?.new_string || "").split("\n");
            const removed = oldLines.slice(0, MAX_PREVIEW).map(l => `-${l}`);
            const added = newLines.slice(0, MAX_PREVIEW).map(l => `+${l}`);
            if (oldLines.length > MAX_PREVIEW) removed.push(`-... (${oldLines.length - MAX_PREVIEW} more lines)`);
            if (newLines.length > MAX_PREVIEW) added.push(`+... (${newLines.length - MAX_PREVIEW} more lines)`);
            return `--- ${path}\n+++ ${path}\n@@ edit @@\n${removed.join("\n")}\n${added.join("\n")}`;
        }
        if (name === "symbol_edit") {
            const symbol = input?.symbol || "(symbol)";
            const newLines = String(input?.new_string || "").split("\n");
            const added = newLines.slice(0, MAX_PREVIEW).map(l => `+${l}`);
            if (newLines.length > MAX_PREVIEW) added.push(`+... (${newLines.length - MAX_PREVIEW} more lines)`);
            return `--- ${path}\n+++ ${path}\n@@ symbol ${symbol} (${input?.kind || "all"}) @@\n${added.join("\n")}`;
        }
        return "";
    }
    function failureSummary(name, outputText) {
        const txt = String(outputText || "");
        let issue = "", next = "";
        const mMissing = txt.match(/File not found:\s*([^\n]+)/i);
        const mOldMiss = txt.match(/old_string not found/i);
        const mMultiple = txt.match(/Found\s+(\d+)\s+occurrences\s+of\s+old_string/i);
        const mTimeout = txt.match(/timed out/i);
        const mExit = txt.match(/\[exit code:\s*(-?\d+)\]/i) || txt.match(/exited with code\s+(-?\d+)/i);
        const mPerm = txt.match(/permission denied/i);
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
        const wrap = document.createElement("div");
        const pre = document.createElement("pre");
        pre.className = `tool-result-body ${className}`.trim();
        const full = String(text || "(no output)");
        const short = full.slice(0, maxChars);
        pre.textContent = full.length > maxChars ? `${short}\n…` : full;
        wrap.appendChild(pre);
        if (full.length > maxChars) {
            const btn = document.createElement("button");
            btn.className = "tool-show-more-btn";
            btn.textContent = "Show more";
            let expanded = false;
            btn.addEventListener("click", (e) => {
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
        const str = String(content || "").trim();
        if (!str) return makeProgressiveBody("(no output)", "tool-result-body");
        try {
            const parsed = JSON.parse(str);
            if (parsed && typeof parsed === "object") {
                return renderStructuredOutput(parsed);
            }
        } catch (_) {}
        if (str.length > 4000) return makeProgressiveBody(str, "", 4000);
        const wrap = document.createElement("div");
        const pre = document.createElement("pre");
        pre.className = "tool-result-body";
        pre.textContent = str;
        wrap.appendChild(pre);
        return wrap;
    }
    function renderStructuredOutput(obj) {
        const wrap = document.createElement("div");
        wrap.className = "tool-structured-output";
        if (Array.isArray(obj)) {
            if (obj.length === 0) {
                wrap.innerHTML = `<span class="tool-output-empty">No results</span>`;
                return wrap;
            }
            const isStringArray = obj.every(v => typeof v === "string");
            if (isStringArray && obj.length <= 50) {
                const list = document.createElement("div");
                list.className = "tool-output-list";
                obj.forEach(item => {
                    const row = document.createElement("div");
                    row.className = "tool-output-list-item";
                    row.textContent = item;
                    list.appendChild(row);
                });
                wrap.appendChild(list);
                return wrap;
            }
            obj.slice(0, 30).forEach((item, i) => {
                if (typeof item === "object" && item !== null) {
                    const card = document.createElement("div");
                    card.className = "tool-output-card";
                    Object.entries(item).forEach(([k, v]) => {
                        const row = document.createElement("div");
                        row.className = "tool-output-row";
                        const key = document.createElement("span");
                        key.className = "tool-output-key";
                        key.textContent = k;
                        const val = document.createElement("span");
                        val.className = "tool-output-val";
                        val.textContent = typeof v === "object" ? JSON.stringify(v) : String(v);
                        row.appendChild(key);
                        row.appendChild(val);
                        card.appendChild(row);
                    });
                    wrap.appendChild(card);
                } else {
                    const row = document.createElement("div");
                    row.className = "tool-output-list-item";
                    row.textContent = String(item);
                    wrap.appendChild(row);
                }
            });
            if (obj.length > 30) {
                const more = document.createElement("div");
                more.className = "tool-output-more";
                more.textContent = `... and ${obj.length - 30} more items`;
                wrap.appendChild(more);
            }
            return wrap;
        }
        // Plain object: render as key-value pairs
        Object.entries(obj).forEach(([k, v]) => {
            const row = document.createElement("div");
            row.className = "tool-output-row";
            const key = document.createElement("span");
            key.className = "tool-output-key";
            key.textContent = k;
            const val = document.createElement("span");
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
        const lines = String(rawText || "").split("\n");
        const groups = [];
        let currentGroup = "Matches";
        for (const ln of lines) {
            if (/^\s*definitions:\s*$/i.test(ln)) { currentGroup = "Definitions"; continue; }
            if (/^\s*references:\s*$/i.test(ln)) { currentGroup = "References"; continue; }
            const hit = parseLocationLine(ln);
            if (hit) groups.push({ group: currentGroup, ...hit });
        }
        if (!groups.length) return null;
        const wrap = document.createElement("div");
        wrap.className = "tool-match-list";
        const initial = 40;
        const render = (maxItems) => {
            wrap.innerHTML = "";
            groups.slice(0, maxItems).forEach((hit) => {
                const row = document.createElement("button");
                row.type = "button";
                row.className = "tool-match-item";
                const shortPath = condensePath(hit.path) || hit.path;
                row.innerHTML = `<span class="tool-match-group">${escapeHtml(hit.group)}</span><span class="tool-match-loc" title="${escapeHtml(hit.path)}">${escapeHtml(shortPath)}:${hit.line}</span><span class="tool-match-text">${escapeHtml(hit.text || "")}</span>`;
                row.addEventListener("click", (e) => { e.stopPropagation(); openFileAt(hit.path, hit.line); });
                wrap.appendChild(row);
            });
        };
        render(initial);
        if (groups.length > initial) {
            const btn = document.createElement("button");
            btn.className = "tool-show-more-btn";
            btn.textContent = `Show ${groups.length - initial} more matches`;
            let expanded = false;
            btn.addEventListener("click", (e) => {
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
        const lines = String(rawText || "").split("\n");
        const codeLines = [];
        let header = "";
        lines.forEach((ln) => {
            if (!header && ln.startsWith("[")) header = ln;
            const m = ln.match(/^\s*\d+\|(.*)$/);
            if (m) codeLines.push(m[1]);
        });
        if (!codeLines.length) return null;
        const wrap = document.createElement("div");
        if (header) {
            const meta = document.createElement("div");
            meta.className = "tool-read-meta";
            meta.textContent = header;
            wrap.appendChild(meta);
        }
        const preWrap = makeProgressiveBody(codeLines.join("\n"), "tool-code-preview", 3000);
        const pre = preWrap.querySelector("pre");
        if (pre && typeof hljs !== "undefined") {
            const ext = (input?.path || "").split(".").pop() || "";
            const lang = langFromExt(ext);
            if (hljs.getLanguage(lang)) {
                const code = document.createElement("code");
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
        const lines = text.split("\n");
        const diffStart = lines.findIndex(l => /^---\s/.test(l) || /^@@\s/.test(l));
        if (diffStart <= 0) return { summary: "", diff: text };
        return {
            summary: lines.slice(0, diffStart).join("\n").trim(),
            diff: lines.slice(diffStart).join("\n"),
        };
    }

    function renderToolOutput(runEl, name, input, content, success, extraData) {
        const out = document.createElement("div");
        out.className = `tool-result ${success ? "tool-result-success" : "tool-result-error"} ${name === "Bash" ? "tool-result-terminal" : ""}`;
        out.innerHTML = `<div class="tool-section-label">${name === "Bash" ? "Output" : "Result"}</div>`;

        if (!success) {
            const summary = failureSummary(name, content || extraData?.error || "");
            if (summary) {
                const box = document.createElement("div");
                box.className = "tool-failure-summary";
                box.innerHTML = `<div class="tool-failure-title">What failed: ${escapeHtml(summary.issue)}</div><div class="tool-failure-next">Next step: ${escapeHtml(summary.next)}</div>`;
                out.appendChild(box);
            }
        }

        if (name === "search" || name === "find_symbol") {
            const list = makeLocationList(content);
            if (list) out.appendChild(list);
        }
        if (name === "Read") {
            const preview = makeReadFilePreview(content, input);
            if (preview) out.appendChild(preview);
        }

        // Write/Edit/symbol_edit: render real diff from tool result (replaces preview)
        const isFileEdit = name === "Write" || name === "Edit" || name === "symbol_edit";
        if (isFileEdit && _contentHasDiff(content)) {
            const { summary, diff } = _extractDiffFromContent(content);
            if (summary) {
                const meta = document.createElement("div");
                meta.className = "tool-read-meta";
                meta.textContent = summary;
                out.appendChild(meta);
            }
            const mini = document.createElement("div");
            mini.className = "tool-mini-diff";
            mini.innerHTML = renderDiff(diff);
            out.appendChild(mini);
            // Remove preview diff if it was shown from tool_call
            const existingPreview = runEl.querySelector(".tool-edit-preview");
            if (existingPreview) existingPreview.remove();
        } else if (isFileEdit) {
            // Fallback: use input-based preview diff if tool didn't return a diff
            const diff = buildEditPreviewDiff(name, input);
            if (diff) {
                const mini = document.createElement("div");
                mini.className = "tool-mini-diff";
                mini.innerHTML = renderDiff(diff);
                out.appendChild(mini);
            }
            // Remove preview if present
            const existingPreview = runEl.querySelector(".tool-edit-preview");
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
        const summaryEl = groupEl.querySelector(".tool-summary");
        const count = Number(groupEl.dataset.count || "1");
        if (summaryEl) {
            const base = (summaryEl.textContent || "").replace(/\s*\(\d+\s*runs?\)\s*$/i, "").trim();
            summaryEl.textContent = count > 1 ? `${base} (${count} runs)` : base;
        }
    }
    function maybeAutoFollow(groupEl, runEl) {
        if (!groupEl || groupEl.dataset.toolName !== "Bash") return;
        if (groupEl.dataset.follow !== "1") return;
        const contentEl = groupEl.querySelector(".tool-content");
        if (contentEl) contentEl.scrollTop = contentEl.scrollHeight;
        const body = runEl.querySelector(".tool-terminal-body");
        if (body) body.scrollTop = body.scrollHeight;
        scrollChat();
    }
    function addToolCall(name, input, toolUseId = null, { stream = true } = {}) {
        name = normalizedToolName(name);
        const bubble = getOrCreateBubble();
        const isCmd = name === "Bash";
        const now = Date.now();
        const key = toolGroupKey(name, input);

        let group = null;
        const canReuse =
            lastToolGroup &&
            lastToolGroup.isConnected &&
            lastToolGroup.dataset.groupKey === key &&
            (now - Number(lastToolGroup.dataset.lastAt || 0) < 60000);

        if (canReuse) {
            group = lastToolGroup;
            group.dataset.lastAt = String(now);
            group.dataset.count = String(Number(group.dataset.count || "1") + 1);
            updateToolGroupHeader(group);
            const statusEl = group.querySelector(".tool-status");
            if (statusEl) {
                statusEl.outerHTML = isCmd
                    ? `<span class="tool-status tool-status-running" title="Running"><span class="tool-status-dot"></span></span>`
                    : `<span class="tool-status tool-status-pending" title="Pending"><span class="tool-status-dot"></span></span>`;
            }
            if (name === "TodoWrite" && input?.todos && Array.isArray(input.todos)) {
                const normalized = input.todos.map((t, i) => ({
                    id: t.id != null ? t.id : String(i + 1),
                    content: t.content || "",
                    status: (t.status || "pending").toLowerCase()
                }));
                showAgentChecklist(normalized);
            }
            /* Stop button lives in .tool-content-actions; no need to inject into options panel */
        } else {
            const desc = toolDesc(name, input);
            const headerDesc = (name === "Read" && input?.path) ? readFileDisplayString(input) : toolDescForHeader(name, input);
            const linkHtml = webToolLinkHtml(name, input);
            const icon = toolIcon(name, input);
            group = document.createElement("div");
            group.className = isCmd ? "tool-block tool-block-command" : "tool-block tool-block-loading";
            group.dataset.toolName = name;
            group.dataset.groupKey = key;
            group.dataset.count = "1";
            group.dataset.firstAt = String(now);
            group.dataset.lastAt = String(now);
            group.dataset.follow = "1";
            if (input?.path) group.dataset.path = input.path;
            const statusHtml = isCmd
                ? `<span class="tool-status tool-status-running" title="Running"><span class="tool-status-dot"></span></span>`
                : `<span class="tool-status tool-status-pending" title="Pending"><span class="tool-status-dot"></span></span>`;

            const summaryText = headerDesc ? `${toolTitle(name)} ${headerDesc}` : toolTitle(name);
            const fileToolWithPath = (name === "Read" || name === "Write" || name === "Edit" || name === "symbol_edit" || name === "lint_file") && input?.path;
            const headerTitle = fileToolWithPath ? String(input.path).replace(/\\/g, "/") : (headerDesc || toolTitle(name));
            group.innerHTML = `
                <div class="tool-header ${isCmd ? "tool-header-cmd" : ""}">
                    <div class="tool-left">
                        <span class="tool-icon-wrap"><span class="tool-icon">${icon}</span></span>
                        <span class="tool-summary" title="${escapeHtml(headerTitle)}">${escapeHtml(summaryText)}</span>${linkHtml}
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
            const startsExpanded = name === "Write" || name === "Edit" || name === "symbol_edit";
            if (!startsExpanded) group.classList.add("collapsed");
            group.querySelector(".tool-header").addEventListener("click", () => group.classList.toggle("collapsed"));

            const optionsWrap = group.querySelector(".tool-options-wrap");
            const optionsTrigger = group.querySelector(".tool-options-trigger");
            const optionsPanel = group.querySelector(".tool-options-panel");
            function closeOptionsPanel() {
                if (!optionsPanel || !optionsTrigger) return;
                optionsPanel.classList.remove("is-open");
                optionsTrigger.setAttribute("aria-expanded", "false");
                optionsPanel.setAttribute("aria-hidden", "true");
                document.removeEventListener("click", closeOptionsPanel);
            }
            if (optionsTrigger && optionsPanel) {
                optionsTrigger.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const open = optionsPanel.classList.toggle("is-open");
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

            const path = input?.path;
            const openBtn = group.querySelector(".tool-action-open");
            if (openBtn && path) openBtn.addEventListener("click", (e) => { e.stopPropagation(); closeOptionsPanel(); openFile(path); });
            const rerunBtn = group.querySelector(".tool-action-rerun");
            if (rerunBtn) rerunBtn.addEventListener("click", (e) => { e.stopPropagation(); closeOptionsPanel(); runFollowupPrompt(toolFollowupPrompt(name, input, false)); });
            const retryBtn = group.querySelector(".tool-action-retry");
            if (retryBtn) retryBtn.addEventListener("click", (e) => { e.stopPropagation(); closeOptionsPanel(); runFollowupPrompt(toolFollowupPrompt(name, input, true)); });
            const copyBtn = group.querySelector(".tool-action-copy");
            if (copyBtn) copyBtn.addEventListener("click", (e) => { e.stopPropagation(); closeOptionsPanel(); copyText(group.dataset.latestOutput || ""); });
            const followBtn = group.querySelector(".tool-follow-btn");
            if (followBtn) {
                const followIcon = followBtn.querySelector(".tool-content-btn-icon");
                followBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const enabled = group.dataset.follow === "1";
                    group.dataset.follow = enabled ? "0" : "1";
                    if (followIcon) followIcon.innerHTML = toolActionIcon(enabled ? "play" : "pause");
                    const labelEl = followBtn.querySelector(".tool-content-btn-label");
                    if (labelEl) labelEl.textContent = enabled ? "Resume follow" : "Pause follow";
                    followBtn.title = enabled ? "Resume follow" : "Pause follow";
                    followBtn.classList.toggle("paused", enabled);
                    if (!enabled) {
                        const contentEl = group.querySelector(".tool-content");
                        if (contentEl) contentEl.scrollTop = contentEl.scrollHeight;
                    }
                });
            }
            const stopBtn = group.querySelector(".tool-stop-btn");
            if (stopBtn) {
                const stopIcon = stopBtn.querySelector(".tool-content-btn-icon");
                stopBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    closeOptionsPanel();
                    send({ type: "cancel" });
                    stopBtn.disabled = true;
                    stopBtn.classList.add("is-stopping");
                    if (stopIcon) stopIcon.innerHTML = `<span class="tool-stop-spinner"></span>`;
                    stopBtn.title = "Stopping\u2026";
                });
            }
            group.querySelectorAll(".tool-content-btn").forEach(btn => btn.addEventListener("click", e => e.stopPropagation()));

            bubble.appendChild(group);
            lastToolGroup = group;
        }

        const runList = group.querySelector(".tool-run-list");
        const runIndex = Number(group.dataset.count || "1");
        const run = document.createElement("div");
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
        const formattedInput = formatToolInput(name, input);
        if (formattedInput) {
            run.appendChild(formattedInput);
        } else {
            // Fallback for tools with no special formatting
            const inputText = isCmd ? (input?.command || JSON.stringify(input, null, 2)) : JSON.stringify(input, null, 2);
            run.appendChild(makeProgressiveBody(inputText || "{}", isCmd ? "tool-input tool-input-cmd" : "tool-input", isCmd ? 2800 : 1800));
        }
        // For Write/Edit: stream the diff lines progressively (Cursor-style)
        if (name === "Write" || name === "Edit" || name === "symbol_edit") {
            const previewDiff = buildEditPreviewDiff(name, input);
            if (previewDiff) {
                const previewWrap = document.createElement("div");
                previewWrap.className = "tool-edit-preview";
                const label = document.createElement("div");
                label.className = "tool-section-label";
                label.textContent = "Changes";
                const miniDiff = document.createElement("div");
                miniDiff.className = "tool-mini-diff";
                previewWrap.appendChild(label);
                previewWrap.appendChild(miniDiff);
                run.appendChild(previewWrap);

                if (stream) {
                    // Stream diff lines with a typing effect
                    const diffLines = previewDiff.split("\n");
                    let lineIdx = 0;
                    const LINES_PER_TICK = 3;   // reveal 3 lines per frame for speed
                    const TICK_MS = 18;         // ~18ms between ticks (~55fps)

                    // Blinking write cursor at the bottom of the diff
                    const cursor = document.createElement("div");
                    cursor.className = "diff-stream-cursor";
                    miniDiff.appendChild(cursor);

                    function _streamNextLines() {
                        if (lineIdx >= diffLines.length) {
                            cursor.remove(); // done streaming — remove cursor
                            return;
                        }
                        const frag = document.createDocumentFragment();
                        const end = Math.min(lineIdx + LINES_PER_TICK, diffLines.length);
                        for (let i = lineIdx; i < end; i++) {
                            const l = diffLines[i];
                            const div = document.createElement("div");
                            let c = "ctx";
                            if (l.startsWith("+++") || l.startsWith("---")) c = "hunk";
                            else if (l.startsWith("@@")) c = "hunk";
                            else if (l.startsWith("+")) c = "add";
                            else if (l.startsWith("-")) c = "del";
                            div.className = "diff-line " + c;
                            div.textContent = l;
                            frag.appendChild(div);
                        }
                        // Insert lines before the cursor so cursor stays at the end
                        miniDiff.insertBefore(frag, cursor);
                        lineIdx = end;
                        // Auto-scroll the diff container to follow the latest lines
                        miniDiff.scrollTop = miniDiff.scrollHeight;
                        // Auto-scroll the chat to follow the streaming code
                        if (group.dataset.follow === "1") scrollChat();
                        if (lineIdx < diffLines.length) {
                            setTimeout(_streamNextLines, TICK_MS);
                        } else {
                            cursor.remove(); // done streaming — remove cursor
                        }
                    }
                    // Kick off the streaming after a micro-delay so the tool header paints first
                    setTimeout(_streamNextLines, 30);
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
        scrollChat();
        return run;
    }
    function addToolResult(content, success, runEl, extraData) {
        if (!runEl) return;
        // If we were passed the group (e.g. fallback when tool_use_id didn't match), use last run in group
        if (runEl.classList && runEl.classList.contains("tool-block")) {
            const list = runEl.querySelector(".tool-run-list");
            runEl = (list && list.lastElementChild) || runEl;
        }
        let state = toolRunState.get(runEl);
        if (!state) {
            // Still update group header status so circle -> tick/cross works even when run state is missing (e.g. no output)
            const group = runEl.closest ? runEl.closest(".tool-block") : (runEl.parentElement && runEl.parentElement.closest(".tool-block"));
            if (group) {
                const header = group.querySelector(".tool-header");
                const statusEl = (header && header.querySelector(".tool-status")) || group.querySelector(".tool-status");
                if (statusEl) {
                    statusEl.outerHTML = success
                        ? `<span class="tool-status tool-status-success" title="Done">${toolActionIcon("done")}</span>`
                        : `<span class="tool-status tool-status-error" title="Failed">${toolActionIcon("failed")}</span>`;
                    group.classList.remove("tool-block-loading");
                }
            }
            return;
        }
        let group = runEl.closest(".tool-block");
        if (!group && runEl.parentElement) group = runEl.parentElement.closest(".tool-block");
        if (!group) return;
        const isCmd = state.name === "Bash";

        const baseOutput = String(content || extraData?.error || "");
        const rawOutput = baseOutput || "(no output)";
        const prior = state.output || "";
        const merged = isCmd
            ? (baseOutput ? ((prior && !prior.includes(baseOutput)) ? `${prior}\n${baseOutput}` : (prior || baseOutput)) : (prior || rawOutput))
            : (rawOutput || prior || "(no output)");
        state.output = merged;
        toolRunState.set(runEl, state);

        let searchStats = null;
        if (state.name === "search" && group.dataset.toolName === "search") {
            searchStats = countFilesAndMatchesInSearchOutput(merged);
            if (searchStats.fileCount === 0) {
                group.remove();
                scrollChat();
                return;
            }
        }

        runEl.querySelector(".tool-result")?.remove();
        renderToolOutput(runEl, state.name, state.input, merged, success, extraData);

        group.dataset.latestOutput = merged;
        const copyBtn = group.querySelector(".tool-action-copy");
        if (copyBtn) copyBtn.classList.remove("hidden");
        const retryBtn = group.querySelector(".tool-action-retry");
        if (retryBtn) retryBtn.classList.toggle("hidden", success !== false);

        const header = group.querySelector(".tool-header");
        const statusEl = (header && header.querySelector(".tool-status")) || group.querySelector(".tool-status");
        if (statusEl) {
            if (isCmd) {
                const exitCode = extraData?.exit_code;
                const duration = extraData?.duration;
                let badge = success
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

        const stopBtn = group.querySelector(".tool-stop-btn");
        if (stopBtn && success !== undefined) stopBtn.remove();
        if (searchStats) {
            const summaryEl = group.querySelector(".tool-summary");
            if (summaryEl) {
                const fileStr = searchStats.fileCount === 1 ? "1 file" : searchStats.fileCount + " files";
                const searchStr = searchStats.matchCount === 1 ? "1 search" : searchStats.matchCount + " searches";
                summaryEl.textContent = "Explored " + fileStr + " " + searchStr;
            }
        }
        if (state.name === "Write" || state.name === "Edit" || state.name === "symbol_edit") {
            setTimeout(updateModifiedFilesBar, 600);
        }
        maybeAutoFollow(group, runEl);
        scrollChat();
    }

    function appendCommandOutput(toolUseId, chunk, isStderr) {
        if (!toolUseId || !chunk) return;
        const runEl = toolRunById.get(String(toolUseId));
        if (!runEl) return;
        const state = toolRunState.get(runEl);
        if (!state) return;
        const group = runEl.closest(".tool-block");
        if (!group) return;

        const next = `${state.output || ""}${chunk}`;
        state.output = next.length > 50000 ? next.slice(-50000) : next;
        toolRunState.set(runEl, state);

        let live = runEl.querySelector(".tool-terminal-live");
        if (!live) {
            const label = document.createElement("div");
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
        const copyBtn = group.querySelector(".tool-action-copy");
        if (copyBtn) copyBtn.classList.remove("hidden");
        maybeAutoFollow(group, runEl);
        scrollChat();
    }
    /** Show only the relevant part of a path in headers (last 2 segments, e.g. src/foo.tsx). */
    function condensePath(fullPath) {
        if (!fullPath || typeof fullPath !== "string") return "";
        const path = String(fullPath).replace(/\\/g, "/").replace(/\/+$/, "");
        const segments = path.split("/").filter(Boolean);
        if (segments.length === 0) return "";
        if (segments.length <= 2) return segments.join("/");
        return segments.slice(-2).join("/");
    }
    function readFileDisplayString(input) {
        if (!input?.path) return "Read";
        const path = String(input.path).replace(/\\/g, "/");
        const short = condensePath(path);
        const base = path.split("/").pop() || path;
        const offset = input.offset != null ? Number(input.offset) : null;
        const limit = input.limit != null ? Number(input.limit) : null;
        if (offset != null && limit != null && limit > 0) {
            const end = offset + limit - 1;
            return short + " L" + offset + "\u2013" + end;
        }
        if (offset != null) return short + " L" + offset;
        return short;
    }
    function countFilesAndMatchesInSearchOutput(output) {
        if (!output || typeof output !== "string") return { fileCount: 0, matchCount: 0 };
        const seen = new Set();
        let matchCount = 0;
        const lineRe = /^([^:]+):\d+:/;
        output.split("\n").forEach((line) => {
            const match = line.match(lineRe);
            if (match) {
                seen.add(match[1].trim());
                matchCount += 1;
            }
        });
        return { fileCount: seen.size, matchCount };
    }
    function toolLabel(n) {
        const labels = {
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
    /** Map API/implementation tool names to canonical display names (e.g. read_file → Read). */
    function normalizedToolName(n) {
        const map = {
            read_file: "Read",
            write_file: "Write",
            edit_file: "Edit",
            glob_find: "Glob",
            run_command: "Bash",
            SemanticRetrieve: "semantic_retrieve",
        };
        return map[n] || n;
    }
    function toolTitle(n) {
        const titles = {
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
        const path = i.path != null ? String(i.path).replace(/\\/g, "/") : "";
        if (n === "Write" || n === "Edit" || n === "lint_file") return condensePath(path) || "";
        if (n === "symbol_edit") {
            const p = condensePath(path);
            const sym = i.symbol ? (i.symbol.length > 20 ? i.symbol.slice(0, 17) + "…" : i.symbol) : "";
            return [p, sym].filter(Boolean).join(" · ") || "";
        }
        if (n === "list_directory" || n === "search" || n === "find_symbol") return condensePath(path) || (n === "search" ? (i.pattern || "") : n === "find_symbol" ? (i.symbol || "") : "") || "";
        return "";
    }
    /** Understated link/query for WebFetch/WebSearch in tool header. Returns safe HTML or "". */
    function webToolLinkHtml(name, input) {
        if (!input) return "";
        if (name === "WebFetch" && input.url) {
            const url = String(input.url).trim();
            const short = url.length > 52 ? url.slice(0, 49) + "\u2026" : url;
            return `<span class="tool-link-wrap"><a href="${escapeHtml(url)}" class="tool-link-subtle" target="_blank" rel="noopener noreferrer">${escapeHtml(short)}</a></span>`;
        }
        if (name === "WebSearch" && input.query) {
            const q = String(input.query).trim();
            const short = q.length > 48 ? q.slice(0, 45) + "\u2026" : q;
            return `<span class="tool-link-wrap tool-link-query">${escapeHtml(short)}</span>`;
        }
        return "";
    }
    function toolIcon(n, input) {
        // File-based tools → show the file type icon
        if ((n === "Read" || n === "Write" || n === "Edit" || n === "symbol_edit" || n === "lint_file") && input?.path) {
            return fileTypeIcon(input.path, 14);
        }
        const svgs = {
            Bash: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>`,
            search: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`,
            semantic_retrieve: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="10" cy="10" r="7"/><line x1="19" y1="19" x2="15.5" y2="15.5"/><line x1="6" y1="14" x2="14" y2="14"/><line x1="6" y1="17" x2="12" y2="17"/><line x1="6" y1="20" x2="13" y2="20"/></svg>`,
            SemanticRetrieve: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="10" cy="10" r="7"/><line x1="19" y1="19" x2="15.5" y2="15.5"/><line x1="6" y1="14" x2="14" y2="14"/><line x1="6" y1="17" x2="12" y2="17"/><line x1="6" y1="20" x2="13" y2="20"/></svg>`,
            find_symbol: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16"/><path d="M7 4v3a5 5 0 0 0 10 0V4"/><line x1="12" y1="17" x2="12" y2="21"/><line x1="8" y1="21" x2="16" y2="21"/></svg>`,
            list_directory: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`,
            Glob: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><path d="M11 8v6"/><path d="M8 11h6"/></svg>`,
            scout: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
            TodoWrite: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2h6v4H9V2z"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M9 10l2 2 4-4"/><path d="M9 14h6M9 18h6"/></svg>`,
            TodoRead: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`,
            MemoryWrite: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`,
            MemoryRead: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>`,
            WebFetch: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
            WebSearch: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><path d="M8 11h6"/></svg>`,
        };
        const snake = typeof n === "string" ? n.replace(/([A-Z])/g, "_$1").toLowerCase().replace(/^_/, "") : n;
        return svgs[n] || svgs[snake] || `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/></svg>`;
    }

    // ================================================================
    // TOOL INPUT FORMATTING — Human-readable, no raw JSON
    // ================================================================

    function formatToolInput(name, input) {
        if (!input || typeof input !== "object") return null;
        const wrap = document.createElement("div");
        wrap.className = "tool-input-formatted";

        function chip(val, cls) {
            const s = document.createElement("span");
            s.className = "tool-input-chip" + (cls ? " " + cls : "");
            s.textContent = val;
            s.title = val;
            return s;
        }
        function kvChip(key, val) {
            const s = document.createElement("span");
            s.className = "tool-input-chip";
            s.innerHTML = `<span class="chip-key">${escapeHtml(key)}</span> <span class="chip-val">${escapeHtml(String(val).slice(0, 120))}</span>`;
            s.title = String(val);
            return s;
        }

        switch (name) {
            case "Read":
                if (input.path) wrap.appendChild(chip(input.path, "chip-path"));
                if (input.offset) wrap.appendChild(kvChip("from", `line ${input.offset}`));
                if (input.limit) wrap.appendChild(kvChip("lines", input.limit));
                break;
            case "Write":
                if (input.path) wrap.appendChild(chip(input.path, "chip-path"));
                if (input.content) {
                    const lc = (input.content.match(/\n/g) || []).length + 1;
                    wrap.appendChild(kvChip("lines", lc));
                }
                break;
            case "Edit":
            case "symbol_edit":
                if (input.path) wrap.appendChild(chip(input.path, "chip-path"));
                if (input.symbol) wrap.appendChild(kvChip("symbol", input.symbol));
                if (input.old_string) {
                    const preview = input.old_string.split("\n")[0].slice(0, 60);
                    wrap.appendChild(kvChip("find", preview + (input.old_string.length > 60 ? "..." : "")));
                }
                if (input.replace_all) wrap.appendChild(kvChip("mode", "replace all"));
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
                const keys = Object.keys(input).slice(0, 2);
                for (const k of keys) {
                    wrap.appendChild(kvChip(k, input[k]));
                }
        }
        return wrap.children.length > 0 ? wrap : null;
    }

    // ================================================================
    // CHAT — Editable Plan
    // ================================================================

    let currentPlanSteps = [];
    let currentChecklistItems = [];

    function showPlan(steps, planFile, planText, skipChecklist) {
        currentPlanSteps = [...steps];
        const bubble = getOrCreateBubble();
        const block = document.createElement("div"); block.className = "plan-block plan-block--tab"; block.id = "active-plan";

        // Compact tab: open plan in editor (no full plan text in panel)
        let html = `<div class="plan-tab-row">`;
        if (planFile) {
            html += `<button type="button" class="plan-open-tab" title="Open plan in editor" aria-label="Open in editor" data-path="${escapeHtml(planFile)}">${toolActionIcon("open")}</button>`;
        }
        html += `</div>`;

        block.innerHTML = html;
        block.appendChild(makeCopyBtn(planText || steps.join("\n")));
        bubble.appendChild(block);

        const openBtn = block.querySelector(".plan-open-tab");
        if (openBtn) {
            openBtn.addEventListener("click", () => {
                const path = openBtn.dataset.path;
                if (path && typeof openFile === "function") openFile(path);
            });
        }

        showActionBar([
            { label: "\u25B6 Build", cls: "primary", onClick: () => { hideActionBar(); send({ type: "build", steps: currentPlanSteps }); setRunning(true); }},
            { label: "\uD83D\uDCAC Feedback", cls: "secondary", onClick: () => { showPlanFeedbackInput(); }},
            { label: "\u2715 Reject", cls: "danger", onClick: () => { hideActionBar(); send({ type: "reject_plan" }); }},
        ]);
        if (!skipChecklist) {
            currentChecklistItems = steps.map((s, i) => ({ id: String(i + 1), content: s, status: "pending" }));
            showAgentChecklist(currentChecklistItems);
        }
        scrollChat();
    }

    function autoResizeTA(ta) { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; }

    // ── Plan feedback input ────────────────────────────────────
    function showPlanFeedbackInput() {
        // If already showing, focus it
        const existing = document.querySelector(".plan-feedback-box");
        if (existing) { existing.querySelector("textarea").focus(); return; }

        const planBlock = document.getElementById("active-plan");
        if (!planBlock) return;

        const box = document.createElement("div");
        box.className = "plan-feedback-box";
        box.innerHTML = `
            <div class="plan-feedback-label">What would you like changed?</div>
            <textarea class="plan-feedback-input" rows="3" placeholder="e.g. Don\u2019t modify auth.py, use the existing middleware instead\u2026"></textarea>
            <div class="plan-feedback-actions">
                <button class="plan-feedback-send action-btn primary">Re-plan</button>
                <button class="plan-feedback-cancel action-btn secondary">Cancel</button>
            </div>
        `;
        planBlock.appendChild(box);

        const ta = box.querySelector("textarea");
        const sendBtn = box.querySelector(".plan-feedback-send");
        const cancelBtn = box.querySelector(".plan-feedback-cancel");

        ta.focus();

        sendBtn.addEventListener("click", () => {
            const feedback = ta.value.trim();
            if (!feedback) { ta.focus(); return; }
            hideActionBar();
            box.remove();
            send({ type: "replan", content: feedback });
            setRunning(true);
        });

        cancelBtn.addEventListener("click", () => { box.remove(); });

        // Enter to send (Shift+Enter for newline)
        ta.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                sendBtn.click();
            }
        });

        scrollChat();
    }

    // ── Todos: only in bottom sticky dropdown (no in-chat checklist) ───
    function showAgentChecklist(todos, progress) {
        currentChecklistItems = Array.isArray(todos) ? [...todos] : [];
        const block = document.getElementById("agent-checklist");
        if (block) block.remove();
        updateStickyTodoBar(progress);
    }

    function updateStickyTodoBar(progress) {
        if (!$stickyTodoBar || !$stickyTodoCount || !$stickyTodoList) return;
        const items = currentChecklistItems || [];
        if (items.length === 0) {
            $stickyTodoBar.classList.add("hidden");
            $stickyTodoList.classList.add("hidden");
            $stickyTodoBar.removeAttribute("data-expanded");
            return;
        }
        $stickyTodoBar.classList.remove("hidden");

        // Count completed/in-progress/pending
        const completed = items.filter(t => (t.status || "").toLowerCase() === "completed").length;
        const inProgress = items.filter(t => (t.status || "").toLowerCase() === "in_progress").length;
        let countText = `${completed}/${items.length} tasks`;
        if (progress && progress.stepNum != null && progress.totalSteps != null) {
            countText += ` \u2014 Step ${progress.stepNum}/${progress.totalSteps}`;
        } else if (inProgress > 0) {
            countText += ` \u2014 ${inProgress} in progress`;
        }
        $stickyTodoCount.textContent = countText;

        // Build progress bar
        const pct = items.length > 0 ? Math.round((completed / items.length) * 100) : 0;
        let progressBar = $stickyTodoBar.querySelector(".sticky-todo-progress");
        if (!progressBar) {
            progressBar = document.createElement("div");
            progressBar.className = "sticky-todo-progress";
            progressBar.innerHTML = `<div class="sticky-todo-progress-fill"></div>`;
            $stickyTodoBar.insertBefore(progressBar, $stickyTodoList);
        }
        progressBar.querySelector(".sticky-todo-progress-fill").style.width = pct + "%";

        $stickyTodoList.innerHTML = items.map((t) => {
            const status = (t.status || "pending").toLowerCase();
            const content = (t.content || "").trim() || "\u2014";
            const statusChar = status === "completed" ? "\u2713" : status === "in_progress" ? "\u25B6" : "\u25CB";
            const cls = status === "completed" ? "done" : status === "in_progress" ? "active" : "";
            const todoId = t.id != null ? String(t.id) : "";
            return `<div class="sticky-todo-item ${cls}" data-todo-id="${escapeHtml(todoId)}">
                <span class="sticky-todo-status">${statusChar}</span>
                <span class="sticky-todo-content" title="${escapeHtml(content)}">${escapeHtml(content)}</span>
                <button type="button" class="sticky-todo-remove" title="Remove" aria-label="Remove">\u00D7</button>
            </div>`;
        }).join("");
        $stickyTodoList.classList.toggle("hidden", $stickyTodoBar.getAttribute("data-expanded") !== "true");
        $stickyTodoList.querySelectorAll(".sticky-todo-remove").forEach((btn) => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const item = btn.closest(".sticky-todo-item");
                const id = item && item.getAttribute("data-todo-id");
                if (id != null) send({ type: "remove_todo", id });
            });
        });
    }

    function updatePlanStepProgress(stepNum, totalSteps) {
        if (currentChecklistItems.length === 0) return;
        currentChecklistItems.forEach((item, i) => {
            const num = i + 1;
            item.status = num < stepNum ? "completed" : num === stepNum ? "in_progress" : "pending";
        });
        showAgentChecklist(currentChecklistItems, { stepNum, totalSteps });
    }

    function markPlanComplete() {
        currentChecklistItems.forEach(item => { item.status = "completed"; });
        showAgentChecklist(currentChecklistItems);
    }

    // ================================================================
    // CHAT — Diff display
    // ================================================================

    function showDiffs(files, isCumulative) {
        // Remove any previous cumulative diff container so we replace, not duplicate
        const existing = document.getElementById("cumulative-diff-container");
        if (existing) existing.remove();

        const bubble = getOrCreateBubble();
        const container = document.createElement("div");
        container.id = "cumulative-diff-container";
        container.className = "cumulative-diff-container";

        // Summary header
        const totalAdds = files.reduce((s, f) => s + (f.additions || 0), 0);
        const totalDels = files.reduce((s, f) => s + (f.deletions || 0), 0);
        const newFiles = files.filter(f => f.label === "new file").length;
        const modFiles = files.length - newFiles;
        let summaryText = `${files.length} file${files.length !== 1 ? "s" : ""} changed`;
        const parts = [];
        if (modFiles > 0) parts.push(`${modFiles} modified`);
        if (newFiles > 0) parts.push(`${newFiles} new`);
        if (parts.length) summaryText += ` (${parts.join(", ")})`;

        const summary = document.createElement("div");
        summary.className = "diff-summary-header";
        summary.innerHTML = `<span class="diff-summary-text">${escapeHtml(summaryText)}</span><span class="diff-stats"><span class="add">+${totalAdds}</span><span class="del">-${totalDels}</span></span>`;
        container.appendChild(summary);

        files.forEach(f => {
            const block = document.createElement("div"); block.className = "diff-block";
            const labelCls = f.label === "new file" ? "new-file" : "modified";
            block.innerHTML = `<div class="diff-file-header"><div style="display:flex;align-items:center;gap:8px"><span class="diff-file-name">${escapeHtml(f.path)}</span><span class="diff-file-label ${labelCls}">${escapeHtml(f.label)}</span></div><div class="diff-stats"><span class="add">+${f.additions}</span><span class="del">-${f.deletions}</span></div></div><div class="diff-content">${renderDiff(f.diff)}</div>`;

            block.querySelector(".diff-file-header").addEventListener("click", () => block.classList.toggle("collapsed"));

            block.querySelector(".diff-file-name").style.cursor = "pointer";
            block.querySelector(".diff-file-name").addEventListener("click", (e) => { e.stopPropagation(); openDiffForFile(f.path); });

            block.appendChild(makeCopyBtn(f.diff));
            container.appendChild(block);
            markFileModified(f.path);
        });

        bubble.appendChild(container);

        const fileCount = files.length;
        showActionBar([
            { label: `Keep`, cls: "success", onClick: async () => { hideActionBar(); send({type:"keep"}); showInfo("\u2713 Changes kept."); clearAllDiffDecorations(); modifiedFiles.clear(); fileChangesThisSession.clear(); const dc = document.getElementById("cumulative-diff-container"); if (dc) dc.remove(); await refreshTree(); await fetchGitStatus(); if ($sourceControlList) renderSourceControl(); updateModifiedFilesBar(); updateFileChangesDropdown(); }},
            { label: `Revert`, cls: "danger", onClick: async () => { hideActionBar(); send({type:"revert"}); showInfo("\u21A9 Reverted " + fileCount + " file(s)."); clearAllDiffDecorations(); modifiedFiles.clear(); fileChangesThisSession.clear(); const dc = document.getElementById("cumulative-diff-container"); if (dc) dc.remove(); await refreshTree(); await fetchGitStatus(); if ($sourceControlList) renderSourceControl(); updateModifiedFilesBar(); updateFileChangesDropdown(); reloadAllModifiedFiles(); }},
        ]);
        scrollChat();
    }

    function renderDiff(text) {
        if (!text) return "";
        return text.split("\n").map(l => {
            let c = "ctx";
            if (l.startsWith("+++") || l.startsWith("---")) c = "hunk";
            else if (l.startsWith("@@")) c = "hunk";
            else if (l.startsWith("+")) c = "add";
            else if (l.startsWith("-")) c = "del";
            return `<div class="diff-line ${c}">${escapeHtml(l)}</div>`;
        }).join("");
    }

    // ================================================================
    // CHAT — Misc UI
    // ================================================================

    function showError(text) {
        const bubble = getOrCreateBubble();
        const el = document.createElement("div"); el.className = "error-msg";
        el.textContent = text; el.appendChild(makeCopyBtn(text));
        bubble.appendChild(el); scrollChat();
    }
    function showInfo(text) {
        const div = document.createElement("div"); div.className = "info-msg"; div.textContent = text;
        $chatMessages.appendChild(div); scrollChat();
    }

    function showClarifyingQuestion(question, context, tool_use_id, options) {
        const wrap = document.createElement("div");
        wrap.className = "clarifying-question-box";
        const optionsHtml = (options && options.length)
            ? `<div class="clarifying-question-options">${options.map((opt) => `<button type="button" class="clarifying-question-option" data-answer="${escapeHtml(opt)}">${escapeHtml(opt)}</button>`).join("")}</div>`
            : "";
        wrap.innerHTML = `<div class="clarifying-question-label">\u2753 Agent is asking:</div><div class="clarifying-question-text">${escapeHtml(question)}</div>${context ? `<div class="clarifying-question-context">${escapeHtml(context)}</div>` : ""}${optionsHtml}<textarea class="clarifying-question-input" rows="2" placeholder="Type your answer..."></textarea><button type="button" class="clarifying-question-send">Send answer</button>`;
        const ta = wrap.querySelector(".clarifying-question-input");
        const btn = wrap.querySelector(".clarifying-question-send");

        function submitAnswer(answer) {
            if (!answer) return;
            send({ type: "user_answer", answer: answer, tool_use_id: tool_use_id });
            wrap.classList.add("answered");
            wrap.querySelectorAll(".clarifying-question-input, .clarifying-question-send, .clarifying-question-option").forEach((el) => { if (el) el.style.display = "none"; });
            const optsEl = wrap.querySelector(".clarifying-question-options");
            if (optsEl) optsEl.style.display = "none";
            const done = document.createElement("div"); done.className = "clarifying-question-done"; done.textContent = "\u2713 Sent: " + (answer.length > 60 ? answer.slice(0, 60) + "..." : answer); wrap.appendChild(done);
        }

        wrap.querySelectorAll(".clarifying-question-option").forEach((optBtn) => {
            optBtn.addEventListener("click", () => submitAnswer(optBtn.getAttribute("data-answer") || optBtn.textContent));
        });
        btn.addEventListener("click", () => submitAnswer(ta.value.trim()));
        ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitAnswer(ta.value.trim()); } });
        $chatMessages.appendChild(wrap);
        scrollChat();
        ta.focus();
    }
    function showPhase(name) {
        if (name === "direct") return; // no indicator needed — output speaks for itself
        const bubble = getOrCreateBubble();
        const div = document.createElement("div"); div.className = "phase-indicator"; div.id = `phase-${name}`;
        div.innerHTML = `<span class="phase-label">${escapeHtml(phaseLabel(name))}</span>`;
        bubble.appendChild(div);
        scrollChat();
    }
    function endPhase(name, elapsed) {
        const el = document.getElementById(`phase-${name}`);
        if (el) {
            el.classList.add("done");
            const lbl = el.querySelector(".phase-label");
            if (lbl) lbl.textContent = `${phaseDoneLabel(name)} \u2014 ${elapsed}s`;
        }
    }
    function phaseLabel(n) { return {plan:"Planning\u2026",build:"Building\u2026",direct:"Running\u2026"}[n]||n; }
    function phaseDoneLabel(n) { return {plan:"Planned",build:"Built",direct:"Completed"}[n]||n; }

    function showScoutProgress(text) {
        if (!scoutEl) {
            const bubble = getOrCreateBubble();
            scoutEl = document.createElement("div"); scoutEl.className = "scout-block";
            scoutEl.innerHTML = `<span class="scout-text"></span>`;
            bubble.appendChild(scoutEl);
        }
        const textEl = scoutEl.querySelector(".scout-text");
        if (textEl) textEl.textContent = text || "Scanning\u2026";
        scrollChat();
    }
    function endScout() {
        if (scoutEl) {
            scoutEl.classList.add("scout-done");
            const textEl = scoutEl.querySelector(".scout-text");
            if (textEl) textEl.textContent = "\u2713 Scan complete";
            scoutEl = null;
        }
    }

    // ================================================================
    // WEBSOCKET — with exponential backoff and reconnect banner
    // ================================================================

    let _preventReconnect = false;
    let _isFirstConnect = true;
    let _reconnectAttempt = 0;
    const _reconnectBase = 1000;   // 1s initial
    const _reconnectMax = 30000;   // 30s max
    let _reconnectTimer = null;

    function _getReconnectDelay() {
        // Exponential backoff with jitter: 1s, 2s, 4s, 8s... up to 30s
        const delay = Math.min(_reconnectBase * Math.pow(2, _reconnectAttempt), _reconnectMax);
        const jitter = delay * 0.3 * Math.random();
        return delay + jitter;
    }

    function _showReconnectBanner(msg) {
        let banner = document.getElementById("reconnect-banner");
        if (!banner) {
            banner = document.createElement("div");
            banner.id = "reconnect-banner";
            $chatMessages.parentElement.insertBefore(banner, $chatMessages);
        }
        banner.innerHTML = '<span class="reconnect-spinner"></span> ' +
            (msg || (isRunning
                ? "Connection lost. Reconnecting — agent is still working on the server…"
                : "Connection lost. Reconnecting..."));
        banner.style.display = "flex";
    }

    function _hideReconnectBanner() {
        const banner = document.getElementById("reconnect-banner");
        if (banner) banner.style.display = "none";
    }

    function connect() {
        _preventReconnect = false;
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
        // Restore session_id from localStorage so reconnect/reopen restores same conversation
        if (!currentSessionId) currentSessionId = loadPersistedSessionId();
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        let wsUrl = `${proto}//${location.host}/ws`;
        if (currentSessionId) {
            wsUrl += `?session_id=${encodeURIComponent(currentSessionId)}`;
        }
        ws = new WebSocket(wsUrl);
        $connStatus.className = "status-dot connecting"; $connStatus.title = "Connecting\u2026";

        ws.onopen = () => {
            $connStatus.className = "status-dot connected"; $connStatus.title = "Connected";
            _reconnectAttempt = 0;
            _hideReconnectBanner();
            if (!_isFirstConnect) {
                // On reconnect, we don't clear the chat — replay_done will handle any adjustments
                // The server replays history, frontend just validates
            }
        };
        ws.onclose = () => {
            $connStatus.className = "status-dot disconnected"; $connStatus.title = "Disconnected";
            setRunning(false);
            if (!_preventReconnect) {
                _isFirstConnect = false;
                _showReconnectBanner();
                const delay = _getReconnectDelay();
                _reconnectAttempt++;
                _reconnectTimer = setTimeout(connect, delay);
            }
        };
        ws.onerror = () => { $connStatus.className = "status-dot disconnected"; };
        ws.onmessage = (evt) => { try { handleEvent(JSON.parse(evt.data)); } catch {} };
    }

    function disconnectWs() {
        _preventReconnect = true;
        _isFirstConnect = true;
        _reconnectAttempt = 0;
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
        if (ws) {
            ws.onclose = null;
            ws.onerror = null;
            ws.close();
            ws = null;
        }
    }
    function gatherEditorContext() {
        const ctx = {};
        if (activeTab) {
            ctx.activeFile = { path: activeTab };
            if (monacoInstance) {
                const pos = monacoInstance.getPosition();
                if (pos) ctx.activeFile.cursorLine = pos.lineNumber;
                const sel = monacoInstance.getSelection();
                if (sel && !sel.isEmpty()) {
                    ctx.selectedText = monacoInstance.getModel().getValueInRange(sel);
                    if (ctx.selectedText.length > 2000) ctx.selectedText = ctx.selectedText.slice(0, 2000) + "…";
                }
            }
        }
        if (openTabs.size > 0) {
            ctx.openFiles = [...openTabs.keys()];
        }
        return Object.keys(ctx).length > 0 ? ctx : undefined;
    }

    function send(obj) {
        if (!ws || ws.readyState !== WebSocket.OPEN) return false;
        ws.send(JSON.stringify(obj));
        return true;
    }

    function handleEvent(evt) {
        switch (evt.type) {
            case "init": {
                // Always reset running state on init — if the agent is still
                // running on the server, the "resumed" event (sent right after
                // replay) will set it back to true.
                setRunning(false);
                $modelName.textContent = evt.model_name || "?";
                currentSessionId = evt.session_id || currentSessionId;
                persistSessionId(currentSessionId);
                if (evt.session_id) {
                    fileChangesThisSession.clear();
                    sessionStartTime = sessionStartTime || Date.now();
                    updateFileChangesDropdown();
                }
                if ($conversationTitle) $conversationTitle.textContent = evt.session_name || "New conversation";
                if (evt.input_tokens !== undefined && evt.output_tokens !== undefined) {
                    const parts = [`In: ${formatTokens(evt.input_tokens)}`, `Out: ${formatTokens(evt.output_tokens)}`];
                    if (evt.cache_read) parts.push(`Cache: ${formatTokens(evt.cache_read)}`);
                    $tokenCount.textContent = parts.join(" | ");
                    $tokenCount.title = `Input: ${(evt.input_tokens || 0).toLocaleString()} | Output: ${(evt.output_tokens || 0).toLocaleString()} | Cache: ${(evt.cache_read || 0).toLocaleString()}`;
                } else {
                    $tokenCount.textContent = formatTokens(evt.total_tokens || 0) + " tokens";
                    $tokenCount.title = "Total tokens used";
                }
                $workingDir.textContent = evt.working_directory || "";
                // Reset context gauge so we don't show stale yellow/red from previous session
                const gaugeFill = document.getElementById("context-gauge-fill");
                const gauge = document.getElementById("context-gauge");
                if (gaugeFill) { gaugeFill.style.width = "0%"; gaugeFill.className = "context-gauge-fill"; }
                if (gauge) gauge.title = "Context window usage";
                loadAgentSessions();
                toolRunById.clear();
                if (_isFirstConnect) {
                    $chatMessages.innerHTML = "";
                    if ($conversationTitle) $conversationTitle.textContent = "New conversation";
                    loadTree();
                } else {
                    $chatMessages.innerHTML = "";
                    if ($conversationTitle) $conversationTitle.textContent = "New conversation";
                    loadTree();
                }
                _isFirstConnect = false;
                break;
            }
            case "thinking_start": currentThinkingEl = createThinkingBlock(); break;
            case "thinking":
                appendThinkingContent(currentThinkingEl, evt.content || "");
                break;
            case "thinking_end": finishThinking(currentThinkingEl); currentThinkingEl = null; break;
            case "text_start": currentTextEl = null; currentTextBuffer = ""; break;
            case "text":
                currentTextBuffer += evt.content || "";
                if (!currentTextEl) { const b = getOrCreateBubble(); currentTextEl = document.createElement("div"); currentTextEl.className = "text-content"; b.appendChild(currentTextEl); }
                { const _display = currentTextBuffer.replace(/<updated_plan>[\s\S]*?<\/updated_plan>/g, "").trim();
                  currentTextEl.innerHTML = renderMarkdown(_display);
                  currentTextEl.querySelectorAll("pre code").forEach(b => { if (typeof hljs !== "undefined") hljs.highlightElement(b); }); }
                scrollChat();
                break;
            case "text_end":
                if (currentTextEl && currentTextBuffer) {
                    const _display = currentTextBuffer.replace(/<updated_plan>[\s\S]*?<\/updated_plan>/g, "").trim();
                    currentTextEl.innerHTML = renderMarkdown(_display);
                    currentTextEl.querySelectorAll("pre code").forEach(b => { if (typeof hljs !== "undefined") hljs.highlightElement(b); });
                    const bubble = currentTextEl.closest(".msg-bubble");
                    if (bubble && !bubble.querySelector(":scope > .copy-btn")) bubble.appendChild(makeCopyBtn(currentTextBuffer));
                }
                currentTextEl = null; currentTextBuffer = "";
                break;
            case "tool_call":
                lastToolBlock = addToolCall(
                    evt.data?.name || "tool",
                    evt.data?.input || evt.data || {},
                    evt.data?.id || evt.data?.tool_use_id || null
                );
                // Update todo UI immediately when agent sends TodoWrite (don't wait for todos_updated)
                const toolName = evt.data?.name;
                if (toolName === "TodoWrite" && evt.data?.input?.todos && Array.isArray(evt.data.input.todos)) {
                    const normalized = evt.data.input.todos.map((t, i) => ({
                        id: t.id != null ? t.id : String(i + 1),
                        content: t.content || "",
                        status: (t.status || "pending").toLowerCase()
                    }));
                    showAgentChecklist(normalized);
                }
                // Track file modifications (accept Write/Edit or write_file/edit_file)
                if (toolName === "Write" || toolName === "write_file" || toolName === "Edit" || toolName === "edit_file" || toolName === "symbol_edit") {
                    const p = evt.data?.input?.path;
                    if (p) { markFileModified(p); reloadFileInEditor(p); }
                }
                break;
            case "tool_result":
                {
                    let runEl = (evt.data?.tool_use_id && toolRunById.get(String(evt.data.tool_use_id))) || lastToolBlock;
                    // If we fell back to lastToolBlock and it's the group (not a run), use the last run in that group
                    if (runEl && runEl.classList && runEl.classList.contains("tool-block")) {
                        const list = runEl.querySelector(".tool-run-list");
                        if (list && list.lastElementChild) runEl = list.lastElementChild;
                    }
                    addToolResult(evt.content || "", evt.data?.success !== false, runEl, evt.data);
                    const todoList = evt.data?.todos ?? evt.data?.data?.todos;
                    const isTodoWrite = evt.data?.tool_name === "TodoWrite" || (runEl && (runEl.dataset.toolName === "TodoWrite" || runEl.dataset.toolName === "todo_write"));
                    if (isTodoWrite && Array.isArray(todoList)) {
                        const list = todoList.map((t, i) => ({
                            id: t.id != null ? t.id : String(i + 1),
                            content: t.content || "",
                            status: (String(t.status || "pending")).toLowerCase()
                        }));
                        showAgentChecklist(list);
                    }
                }
                // Reload file if it was just written and track changes (accept Write/Edit or write_file/edit_file)
                {
                    const runEl = (evt.data?.tool_use_id && toolRunById.get(String(evt.data.tool_use_id))) || lastToolBlock;
                    if (runEl) {
                        const tn = runEl.dataset.toolName;
                        const isWrite = tn === "Write" || tn === "write_file";
                        const isEdit = tn === "Edit" || tn === "edit_file" || tn === "symbol_edit";
                        if (isWrite || isEdit) {
                            const path = runEl.dataset.path;
                            if (path && evt.data?.success !== false) {
                                reloadFileInEditor(path);
                                // Count actual lines added/deleted
                                const inputData = runEl._toolInput || {};
                                if (isWrite) {
                                    const contentLines = (inputData.content || "").split('\n').length;
                                    trackFileChange(path, contentLines, 0);
                                } else {
                                    const oldStr = inputData.old_string || "";
                                    const newStr = inputData.new_string || "";
                                    const oldLineCount = oldStr ? oldStr.split('\n').length : 0;
                                    const newLineCount = newStr ? newStr.split('\n').length : 0;
                                    trackFileChange(path, newLineCount, oldLineCount);
                                }
                            }
                        }
                        // Detect file deletions from Bash commands
                        if (tn === "Bash" && evt.data?.success !== false) {
                            const inputData = runEl._toolInput || {};
                            detectFileDeletesFromBash(inputData.command, evt.content || "");
                        }
                    }
                }
                if (evt.data?.tool_use_id) {
                    toolRunById.delete(String(evt.data.tool_use_id));
                }
                lastToolBlock = null;
                break;
            case "command_output":
                appendCommandOutput(evt.data?.tool_use_id, evt.content || "", !!evt.data?.is_stderr);
                break;
            case "command_partial_failure":
                showInfo(evt.content || "Potential command failure detected.");
                break;
            case "checkpoint_list":
                if (Array.isArray(evt.data?.checkpoints)) {
                    const rows = evt.data.checkpoints
                        .slice(0, 12)
                        .map(cp => `${cp.id} (${cp.file_count} files) ${cp.label || ""}`);
                    showInfo(rows.length ? `Checkpoints:\n${rows.join("\n")}` : "No checkpoints available.");
                }
                break;
            case "checkpoint_restored":
                showInfo(`Rewound ${evt.data?.count || 0} files from checkpoint ${evt.data?.checkpoint_id || "latest"}.`);
                if (Array.isArray(evt.data?.paths)) {
                    evt.data.paths.slice(0, 20).forEach(p => reloadFileInEditor(p));
                }
                break;
            case "checkpoint_created":
                if (evt.data?.checkpoint_id) {
                    showInfo("Checkpoint");
                }
                break;
            case "checkpoint_error":
                showError(evt.content || "Checkpoint rewind failed.");
                break;
            case "command_start":
                // Update existing tool block with "running" status if visible
                break;
            case "auto_approved": break;
            case "scout_start": showScoutProgress("Scanning\u2026"); break;
            case "scout_progress": showScoutProgress(evt.content); break;
            case "scout_end": endScout(); break;
            case "phase_start": showPhase(evt.content); break;
            case "user_question": showClarifyingQuestion(evt.question || "", evt.context || "", evt.tool_use_id || "", evt.options); break;
            case "phase_end":
                endPhase(evt.content, evt.elapsed || 0);
                if (evt.content === "build") markPlanComplete();
                break;
            case "plan": case "phase_plan":
                showPlan(
                    evt.steps || (evt.data && evt.data.steps) || [],
                    evt.plan_file || (evt.data && evt.data.plan_file) || null,
                    evt.plan_text || (evt.data && evt.data.plan_text) || ""
                );
                setRunning(false);
                break;
            case "updated_plan":
                {
                    const steps = evt.steps || [];
                    const planFile = evt.plan_file || null;
                    const planText = evt.plan_text || "";
                    if (steps.length) {
                        currentPlanSteps = [...steps];
                        // Update checklist from updated plan steps
                        currentChecklistItems = steps.map((s, i) => ({
                            id: String(i + 1), content: s, status: "pending",
                        }));
                        showAgentChecklist(currentChecklistItems);
                        // Re-show plan UI with updated steps (preserves Build/Feedback/Reject buttons)
                        showPlan(steps, planFile, planText, true);
                        // Show a brief notification in chat
                        const notif = document.createElement("div");
                        notif.className = "info-msg plan-updated-msg";
                        notif.textContent = `Plan updated — ${steps.length} steps`;
                        $chatMessages.appendChild(notif);
                        scrollChat();
                    }
                }
                break;
            case "todos_updated": {
                const list = evt.todos || (evt.data && evt.data.todos) || [];
                const normalized = Array.isArray(list) ? list.map((t, i) => ({
                    id: t.id != null ? t.id : String(i + 1),
                    content: t.content || "",
                    status: normalizeTodoStatus(t.status)
                })) : [];
                showAgentChecklist(normalized);
                break;
            }
            case "plan_step_progress":
                updatePlanStepProgress(
                    evt.step || (evt.data && evt.data.step) || 1,
                    evt.total || (evt.data && evt.data.total) || 1
                );
                break;
            case "diff":
                showDiffs(evt.files || [], !!evt.cumulative);
                setRunning(false);
                refreshTree();
                updateModifiedFilesBar();
                break;
            case "no_changes": showInfo("No file changes."); setRunning(false); break;
            case "no_plan": showInfo("Completed directly."); setRunning(false); break;
            case "done":
                setRunning(false);
                if (evt.data) updateTokenDisplay(evt.data);
                break;
            case "kept":
                hideActionBar();
                clearAllDiffDecorations();
                modifiedFiles.clear();
                fileChangesThisSession.clear();
                { const dc = document.getElementById("cumulative-diff-container"); if (dc) dc.remove(); }
                showInfo("\u2713 Changes kept.");
                refreshTree();
                fetchGitStatus().then(() => { if ($sourceControlList) renderSourceControl(); updateModifiedFilesBar(); });
                updateFileChangesDropdown();
                break;
            case "reverted":
                hideActionBar();
                clearAllDiffDecorations();
                modifiedFiles.clear();
                fileChangesThisSession.clear();
                { const dc = document.getElementById("cumulative-diff-container"); if (dc) dc.remove(); }
                showInfo("\u21A9 Reverted " + (evt.files || []).length + " file(s).");
                refreshTree();
                fetchGitStatus().then(() => { if ($sourceControlList) renderSourceControl(); updateModifiedFilesBar(); });
                updateFileChangesDropdown();
                reloadAllModifiedFiles();
                break;
            case "clear_keep_revert":
                hideActionBar();
                clearAllDiffDecorations();
                break;
            case "reverted_to_step": {
                const files = evt.files || [];
                if (evt.no_checkpoint || (files.length === 0 && evt.step != null)) {
                    showInfo("No checkpoint for step " + evt.step + " (e.g. after reconnect or step not yet completed).");
                } else {
                    showInfo("\u21A9 Reverted to step " + evt.step + " (" + files.length + " file(s))");
                    reloadAllModifiedFiles(); // refresh open tabs so editor shows reverted content
                }
                refreshTree();
                fetchGitStatus().then(() => { if ($sourceControlList) renderSourceControl(); updateModifiedFilesBar(); });
                break;
            }
            case "plan_rejected": showInfo("Plan rejected."); break;
            case "cancelled":
                showInfo("Cancelled.");
                setRunning(false);
                // Clean up ALL running indicators
                // 1. Tool running badges & stop buttons
                document.querySelectorAll(".tool-status-running").forEach(el => {
                    el.outerHTML = `<span class="tool-status tool-status-error" title="Cancelled">${toolActionIcon("stop")}</span>`;
                });
                document.querySelectorAll(".tool-stop-btn").forEach(el => el.remove());
                // 2. Scout spinner
                if (scoutEl) { endScout(); }
                // 3. Phase spinners (plan/build/direct)
                document.querySelectorAll(".phase-indicator:not(.done)").forEach(el => {
                    el.classList.add("done");
                    const sp = el.querySelector(".spinner"); if (sp) sp.remove();
                    const span = el.querySelector("span");
                    if (span) span.textContent = span.textContent.replace(/…$/, "") + " — cancelled";
                });
                // 4. Thinking spinner
                document.querySelectorAll(".thinking-block .thinking-spinner").forEach(el => el.remove());
                document.querySelectorAll(".thinking-block").forEach(el => updateThinkingHeader(el, true));
                lastToolBlock = null;
                break;
            case "reset_done":
                $chatMessages.innerHTML = "";
                currentSessionId = evt.session_id || currentSessionId;
                persistSessionId(currentSessionId);
                sessionStartTime = null;
                fileChangesThisSession.clear();
                currentChecklistItems = [];
                updateFileChangesDropdown();
                updateStickyTodoBar();
                if ($conversationTitle) $conversationTitle.textContent = evt.session_name || "New conversation";
                $tokenCount.textContent = "0 tokens";
                loadAgentSessions();
                toolRunById.clear();
                clearPendingImages();
                hideActionBar(); modifiedFiles.clear(); refreshTree();
                break;
            case "session_name_update":
                if ($conversationTitle && evt.session_name) {
                    $conversationTitle.textContent = evt.session_name;
                }
                loadAgentSessions();
                break;
            case "error": showError(evt.content || "Unknown error"); setRunning(false); break;
            case "stream_retry": case "stream_recovering": showInfo(evt.content || "Recovering\u2026"); break;
            case "stream_failed": showError(evt.content || "Stream failed."); setRunning(false); break;
            case "status":
                updateTokenDisplay(evt);
                break;

            // ── Replay events (history restore on reconnect) ──
            case "replay_user":
                addUserMessage(evt.content || "");
                break;
            case "replay_text":
                if (evt.content) {
                    const rb = addAssistantMessage();
                    const rd = document.createElement("div");
                    rd.className = "text-content";
                    rd.innerHTML = renderMarkdown(evt.content);
                    rd.querySelectorAll("pre code").forEach(b => { if (typeof hljs !== "undefined") hljs.highlightElement(b); });
                    rb.appendChild(rd);
                    rb.appendChild(makeCopyBtn(evt.content));
                }
                break;
            case "replay_thinking": {
                const rt = createThinkingBlock();
                _thinkingBuffer = evt.content || "";
                finishThinking(rt);
                break;
            }
            case "replay_tool_call":
                lastToolBlock = addToolCall(
                    evt.data?.name || "tool",
                    evt.data?.input || {},
                    evt.data?.id || evt.data?.tool_use_id || null,
                    { stream: false }
                );
                break;
            case "replay_tool_result":
                {
                    const runEl = (evt.data?.tool_use_id && toolRunById.get(String(evt.data.tool_use_id))) || lastToolBlock;
                    addToolResult(evt.content || "", evt.data?.success !== false, runEl, evt.data);
                    if (runEl && evt.data?.success !== false) {
                        const tn = runEl.dataset.toolName;
                        const isWrite = tn === "Write" || tn === "write_file";
                        const isEdit = tn === "Edit" || tn === "edit_file" || tn === "symbol_edit";
                        if ((isWrite || isEdit) && runEl.dataset.path) {
                            const inputData = runEl._toolInput || {};
                            if (isWrite) {
                                const contentLines = (inputData.content || "").split('\n').length;
                                trackFileChange(runEl.dataset.path, contentLines, 0);
                            } else {
                                const oldStr = inputData.old_string || "";
                                const newStr = inputData.new_string || "";
                                const oldLineCount = oldStr ? oldStr.split('\n').length : 0;
                                const newLineCount = newStr ? newStr.split('\n').length : 0;
                                trackFileChange(runEl.dataset.path, newLineCount, oldLineCount);
                            }
                        }
                        if (tn === "Bash") {
                            const inputData = runEl._toolInput || {};
                            detectFileDeletesFromBash(inputData.command, evt.content || "");
                        }
                    }
                    if (evt.data?.tool_use_id) toolRunById.delete(String(evt.data.tool_use_id));
                }
                lastToolBlock = null;
                break;
            case "replay_done":
                scrollChat();
                break;
            case "resumed":
                // Server reconnected us to a running (or just-finished) agent session
                _hideReconnectBanner();
                if (evt.agent_running) {
                    setRunning(true);
                    showInfo("Reconnected — agent is still working…");
                } else {
                    setRunning(false);
                    showInfo("Reconnected — agent has finished.");
                }
                scrollChat();
                break;
            case "replay_state":
                if (evt.todos && Array.isArray(evt.todos)) {
                    showAgentChecklist(evt.todos);
                }
                if (evt.pending_plan && evt.pending_plan.length > 0) {
                    currentChecklistItems = evt.pending_plan.map((s, i) => ({ id: String(i + 1), content: s, status: "pending" }));
                    showAgentChecklist(currentChecklistItems);
                }
                if (evt.awaiting_build && evt.pending_plan) {
                    showPlan(evt.pending_plan, evt.plan_file || null, evt.plan_text || "", !!evt.todos?.length);
                }
                if (evt.awaiting_keep_revert && evt.has_diffs) {
                    if (evt.diffs && evt.diffs.length > 0) {
                        showDiffs(evt.diffs, true);
                    } else {
                        showActionBar([
                            { label: "Keep", cls: "success", onClick: () => { hideActionBar(); send({ type: "keep" }); fileChangesThisSession.clear(); updateFileChangesDropdown(); }},
                            { label: "Revert", cls: "danger", onClick: () => { hideActionBar(); send({ type: "revert" }); fileChangesThisSession.clear(); updateFileChangesDropdown(); }},
                        ]);
                    }
                    showInfo("You have pending file changes from a previous session.");
                }
                break;

            // ── External file changes ──
            case "file_changed":
                // Skip tree refresh while agent is running — its own tool_result events handle updates
                if (!isRunning) refreshTree();
                if (typeof monacoInstance !== "undefined" && monacoInstance) {
                    const model = monacoInstance.getModel();
                    if (model && evt.path && model.uri.path.endsWith(evt.path)) {
                        // Reload the file content
                        fetch(`/api/file?path=${encodeURIComponent(evt.path)}`)
                            .then(r => r.ok ? r.text() : null)
                            .then(content => {
                                if (content !== null && content !== model.getValue()) {
                                    model.setValue(content);
                                }
                            }).catch(() => {});
                    }
                }
                break;
        }
    }

    // ================================================================
    // INPUT
    // ================================================================

    async function submitTask() {
        const text = ($input && $input.value) ? $input.value.trim() : "";
        const hasImages = pendingImages.length > 0;
        if ((!text && !hasImages) || isRunning) return;
        if (text.startsWith("/") && /^\/[a-zA-Z]/.test(text) && !hasImages) { handleCommand(text); $input.value = ""; autoResizeInput(); return; }

        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showInfo("Not connected. Waiting for connection…");
            if (typeof showToast === "function") showToast("Not connected");
            return;
        }

        let imagesPayload = [];
        if (hasImages) {
            try {
                imagesPayload = await serializePendingImages();
            } catch (e) {
                showError(`Failed to attach image: ${e?.message || e}`);
                return;
            }
        }

        addUserMessage(text, imagesPayload);
        $input.value = ""; autoResizeInput();
        clearPendingImages();
        setRunning(true);
        addAssistantMessage();
        const editorCtx = gatherEditorContext();
        const sent = send({ type: "task", content: text, images: imagesPayload, ...(editorCtx ? { context: editorCtx } : {}) });
        if (!sent) {
            setRunning(false);
            showInfo("Send failed. Check connection.");
        }
    }

    function handleCommand(text) {
        const parts = text.trim().split(/\s+/);
        const cmd = (parts[0] || "").toLowerCase();
        switch(cmd) {
            case "/reset": send({type:"reset"}); break;
            case "/cancel": send({type:"cancel"}); break;
            case "/checkpoints": send({ type: "checkpoint_list" }); break;
            case "/rewind": send({ type: "checkpoint_restore", checkpoint_id: parts[1] || "latest" }); break;
            case "/help":
                showInfo("Commands: /reset | /cancel | /checkpoints | /rewind <checkpoint-id|latest>");
                break;
            default: showInfo(`Unknown: ${cmd}`);
        }
    }

    function autoResizeInput() { if ($input) { $input.style.height = "auto"; $input.style.height = Math.min($input.scrollHeight, 150) + "px"; } }
    if ($input) {
        $input.addEventListener("input", autoResizeInput);
        $input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitTask(); } });
    }
    if ($attachImageBtn && $imageInput) {
        $attachImageBtn.addEventListener("click", () => $imageInput.click());
        $imageInput.addEventListener("change", (e) => {
            const files = Array.from(e.target.files || []);
            addPendingImageFiles(files);
            $imageInput.value = "";
        });
    }
    if ($sendBtn) $sendBtn.addEventListener("click", submitTask);
    $cancelBtn.addEventListener("click", () => send({type:"cancel"}));
    if ($resetBtn) {
        $resetBtn.addEventListener("click", () => {
            if ($chatMenuDropdown) $chatMenuDropdown.classList.add("hidden");
            if (confirm("Clear this conversation and start a new one? This cannot be undone.")) {
                send({ type: "reset" });
            }
        });
    }
    if ($newAgentBtn) {
        $newAgentBtn.addEventListener("click", createNewAgentSession);
    }
    if ($agentSelect) {
        $agentSelect.addEventListener("change", () => {
            if (suppressAgentSwitch) return;
            const nextId = $agentSelect.value || null;
            if (!nextId || nextId === currentSessionId) return;
            currentSessionId = nextId;
            persistSessionId(currentSessionId);
            disconnectWs();
            connect();
        });
    }
    function isTerminalFocused() {
        var panel = document.getElementById("terminal-panel");
        if (!panel || panel.classList.contains("hidden")) return false;
        return panel.contains(document.activeElement);
    }

    // Escape key cancels the running agent
    document.addEventListener("keydown", e => {
        if (isTerminalFocused()) return;
        if (e.key === "Escape" && isRunning) { e.preventDefault(); send({type:"cancel"}); }
    });

    // ── Keyboard shortcuts ──
    document.addEventListener("keydown", e => {
        if (isTerminalFocused()) return;
        const isMeta = e.metaKey || e.ctrlKey;
        if (!isMeta) return;

        // Cmd+Shift+Backspace or Cmd+K: Reset/new conversation
        if ((e.key === "Backspace" && e.shiftKey) || e.key === "k") {
            e.preventDefault();
            send({ type: "reset" });
            return;
        }
        // Cmd+L: Focus chat input
        if (e.key === "l") {
            e.preventDefault();
            $input.focus();
            return;
        }
        // Cmd+B: Toggle file tree
        if (e.key === "b") {
            e.preventDefault();
            const tree = document.getElementById("file-tree-panel");
            if (tree) tree.style.display = tree.style.display === "none" ? "" : "none";
            return;
        }
        // Cmd+J: Toggle chat panel
        if (e.key === "j") {
            e.preventDefault();
            const chatPanel = document.getElementById("chat-panel");
            if (chatPanel) chatPanel.style.display = chatPanel.style.display === "none" ? "" : "none";
            return;
        }
        // Cmd+/: Toggle between editor and chat focus
        if (e.key === "/") {
            e.preventDefault();
            if (document.activeElement === $input) {
                if (typeof monacoInstance !== "undefined" && monacoInstance) monacoInstance.focus();
            } else {
                $input.focus();
            }
            return;
        }
    });

    // ================================================================
    // PROJECT MODAL
    // ================================================================

    const $dirModal = document.getElementById("dir-modal");
    const $dirInput = document.getElementById("dir-input");
    const $dirError = document.getElementById("dir-error");
    const $dirOpen  = document.getElementById("dir-open");
    const $dirCancel = document.getElementById("dir-cancel");

    function openDirModal() { $dirInput.value = ""; $dirError.classList.add("hidden"); $dirModal.classList.remove("hidden"); $dirInput.focus(); }
    function closeDirModal() { $dirModal.classList.add("hidden"); }

    async function submitDir() {
        const path = $dirInput.value.trim(); if (!path) return;
        $dirOpen.disabled = true; $dirOpen.textContent = "Opening\u2026"; $dirError.classList.add("hidden");
        try {
            const res = await fetch("/api/set-directory", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({path}) });
            const data = await res.json();
            if (data.ok) {
                closeDirModal();
                if (ws) ws.close(); // reconnect triggers new agent
                $workingDir.textContent = data.path;
                $chatMessages.innerHTML = "";
                // Close all tabs
                openTabs.forEach((info) => info.model.dispose());
                openTabs.clear(); $tabBar.innerHTML = ""; activeTab = null;
                if (monacoInstance) { monacoInstance.setModel(null); }
                if (diffEditorInstance) { diffEditorInstance.dispose(); diffEditorInstance = null; }
                $editorWelcome.classList.remove("hidden");
                modifiedFiles.clear();
                if (typeof terminalDisconnect === "function" && typeof terminalConnect === "function") {
                    terminalDisconnect(true);
                    if ($terminalPanel && !$terminalPanel.classList.contains("hidden")) terminalConnect();
                }
                showToast("Opened: " + data.path);
            } else {
                $dirError.textContent = data.error || "Failed"; $dirError.classList.remove("hidden");
            }
        } catch(e) { $dirError.textContent = "Error: " + e.message; $dirError.classList.remove("hidden"); }
        finally { $dirOpen.disabled = false; $dirOpen.textContent = "Open"; }
    }

    $openBtn.addEventListener("click", openDirModal);
    $dirCancel.addEventListener("click", closeDirModal);
    $dirModal.querySelector(".modal-overlay").addEventListener("click", closeDirModal);
    $dirOpen.addEventListener("click", submitDir);
    $dirInput.addEventListener("keydown", e => { if (e.key === "Enter") submitDir(); if (e.key === "Escape") closeDirModal(); });

    // ================================================================
    // SEARCH PANEL
    // ================================================================

    let searchUseRegex = false;
    let searchCaseSensitive = false;
    let lastSearchFiles = [];  // files from last search (for replace)

    $searchToggle.addEventListener("click", () => {
        $searchPanel.classList.toggle("hidden");
        if (!$searchPanel.classList.contains("hidden")) {
            $searchInput.focus();
        }
    });

    $searchRegex.addEventListener("click", () => {
        searchUseRegex = !searchUseRegex;
        $searchRegex.classList.toggle("active", searchUseRegex);
    });
    $searchCase.addEventListener("click", () => {
        searchCaseSensitive = !searchCaseSensitive;
        $searchCase.classList.toggle("active", searchCaseSensitive);
    });
    $replaceToggle.addEventListener("click", () => {
        $replaceRow.classList.toggle("hidden");
        if (!$replaceRow.classList.contains("hidden")) $replaceInput.focus();
    });

    async function performSearch() {
        const pattern = $searchInput.value.trim();
        if (!pattern) return;

        let searchPattern = pattern;
        if (!searchUseRegex) {
            // Escape regex special chars for literal search
            searchPattern = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        }
        if (!searchCaseSensitive) {
            searchPattern = "(?i)" + searchPattern;
        }

        $searchStatus.textContent = "Searching...";
        $searchResults.innerHTML = "";
        lastSearchFiles = [];

        try {
            const params = new URLSearchParams({ pattern: searchPattern });
            const include = $searchInclude.value.trim();
            if (include) params.append("include", include);
            const res = await fetch("/api/search?" + params.toString());
            const data = await res.json();

            if (data.error) {
                $searchStatus.textContent = "Error: " + data.error;
                return;
            }

            const results = data.results || [];
            $searchStatus.textContent = results.length === 0
                ? "No results found"
                : `${data.count} match${data.count === 1 ? "" : "es"} in ${new Set(results.map(r => r.file)).size} file${new Set(results.map(r => r.file)).size === 1 ? "" : "s"}`;

            // Group by file
            const grouped = {};
            for (const r of results) {
                if (!grouped[r.file]) grouped[r.file] = [];
                grouped[r.file].push(r);
            }
            lastSearchFiles = Object.keys(grouped);

            for (const [file, matches] of Object.entries(grouped)) {
                const group = document.createElement("div");
                group.className = "search-file-group";

                const header = document.createElement("div");
                header.className = "search-file-name";
                header.innerHTML = `<span>${escapeHtml(file)}</span><span class="match-count">${matches.length}</span>`;
                header.addEventListener("click", () => openFile(file));
                group.appendChild(header);

                for (const m of matches) {
                    const line = document.createElement("div");
                    line.className = "search-match";

                    let displayText = escapeHtml(m.text);
                    // Try to highlight the match
                    try {
                        const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                        const re = new RegExp(`(${searchUseRegex ? pattern : escaped})`, searchCaseSensitive ? "g" : "gi");
                        displayText = m.text.replace(re, '<span class="match-highlight">$1</span>');
                    } catch(e) { /* keep plain text */ }

                    line.innerHTML = `<span class="line-num">${m.line}</span>${displayText}`;
                    line.addEventListener("click", () => {
                        openFile(file).then(() => {
                            // Jump to line in Monaco
                            if (monacoInstance) {
                                monacoInstance.revealLineInCenter(m.line);
                                monacoInstance.setPosition({ lineNumber: m.line, column: 1 });
                                monacoInstance.focus();
                            }
                        });
                    });
                    group.appendChild(line);
                }
                $searchResults.appendChild(group);
            }
        } catch (e) {
            $searchStatus.textContent = "Search failed: " + e.message;
        }
    }

    $searchGoBtn.addEventListener("click", performSearch);
    $searchInput.addEventListener("keydown", e => { if (e.key === "Enter") performSearch(); });

    $replaceAllBtn.addEventListener("click", async () => {
        const pattern = $searchInput.value.trim();
        const replacement = $replaceInput.value;
        if (!pattern || lastSearchFiles.length === 0) return;

        const count = lastSearchFiles.length;
        if (!confirm(`Replace in ${count} file${count === 1 ? "" : "s"}?`)) return;

        $replaceAllBtn.disabled = true;
        $replaceAllBtn.textContent = "Replacing...";

        try {
            const res = await fetch("/api/replace", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    pattern: pattern,
                    replacement: replacement,
                    files: lastSearchFiles,
                    regex: searchUseRegex,
                }),
            });
            const data = await res.json();
            if (data.error) {
                showToast("Replace error: " + data.error, true);
            } else {
                const totalReplacements = (data.changed || []).reduce((s, c) => s + c.replacements, 0);
                showToast(`Replaced ${totalReplacements} occurrence${totalReplacements === 1 ? "" : "s"} in ${(data.changed || []).length} file${(data.changed || []).length === 1 ? "" : "s"}`);
                // Reload affected files in editor
                for (const c of (data.changed || [])) {
                    await reloadFileInEditor(c.file);
                }
                // Re-search to update results
                performSearch();
            }
        } catch (e) {
            showToast("Replace failed: " + e.message, true);
        } finally {
            $replaceAllBtn.disabled = false;
            $replaceAllBtn.textContent = "Replace All";
        }
    });

    // ================================================================
    // WELCOME SCREEN
    // ================================================================

    function timeAgo(isoStr) {
        if (!isoStr) return "";
        const d = new Date(isoStr);
        const now = new Date();
        const secs = Math.floor((now - d) / 1000);
        if (secs < 60) return "just now";
        const mins = Math.floor(secs / 60);
        if (mins < 60) return `${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h ago`;
        const days = Math.floor(hrs / 24);
        if (days < 30) return `${days}d ago`;
        return d.toLocaleDateString();
    }

    function formatAgentOptionLabel(session) {
        const name = String(session?.name || "default");
        const age = timeAgo(session?.updated_at || "");
        return age ? `${name} (${age})` : name;
    }

    async function loadAgentSessions() {
        if (!$agentSelect) return;
        try {
            const res = await fetch("/api/sessions");
            if (!res.ok) throw new Error("Failed to load sessions");
            const sessions = await res.json();
            const list = Array.isArray(sessions) ? sessions : [];

            suppressAgentSwitch = true;
            $agentSelect.innerHTML = "";
            for (const s of list) {
                const opt = document.createElement("option");
                opt.value = s.session_id || "";
                opt.textContent = formatAgentOptionLabel(s);
                $agentSelect.appendChild(opt);
            }

            const hasCurrent = !!currentSessionId && list.some(s => s.session_id === currentSessionId);
            if (!hasCurrent && list.length > 0) {
                currentSessionId = list[0].session_id;
            }
            if (currentSessionId) {
                $agentSelect.value = currentSessionId;
            }
            $agentSelect.disabled = list.length === 0;
        } catch {
            suppressAgentSwitch = true;
            $agentSelect.innerHTML = "";
            const opt = document.createElement("option");
            opt.value = "";
            opt.textContent = "No agents";
            $agentSelect.appendChild(opt);
            $agentSelect.disabled = true;
        } finally {
            suppressAgentSwitch = false;
        }
    }

    async function createNewAgentSession() {
        const name = (window.prompt("New agent name (optional):", "") || "").trim();
        try {
            const res = await fetch("/api/sessions/new", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
            const data = await res.json();
            if (!data.ok) {
                showToast("Failed to create agent");
                return;
            }
            currentSessionId = data.session_id || null;
            persistSessionId(currentSessionId);
            await loadAgentSessions();
            disconnectWs();
            connect();
            showToast(`New agent: ${data.name || "agent"}`);
        } catch (e) {
            showToast("Error: " + (e?.message || "unable to create agent"));
        }
    }

    async function loadRecentProjects() {
        try {
            const res = await fetch("/api/projects");
            const projects = await res.json();

            if (!projects || projects.length === 0) {
                $projectList.innerHTML = '<div class="welcome-no-projects">No recent projects. Open a local folder or connect via SSH to get started.</div>';
                return;
            }

            $projectList.innerHTML = "";
            for (const p of projects) {
                const el = document.createElement("div");
                el.className = "welcome-project";
                const icon = p.is_ssh ? "🖥️" : "📁";
                const badge = p.is_ssh ? '<span class="welcome-project-badge ssh">SSH</span>' : "";
                // Display-friendly path: for SSH show user@host:directory, for local show full path
                let displayPath = p.path;
                const sshMeta = getSshProjectInfo(p);
                if (p.is_ssh && sshMeta) {
                    displayPath = `${sshMeta.user}@${sshMeta.host}:${sshMeta.directory}`;
                }
                el.innerHTML = `
                    <div class="welcome-project-icon">${icon}</div>
                    <div class="welcome-project-info">
                        <span class="welcome-project-name">${escapeHtml(p.name)} ${badge}</span>
                        <span class="welcome-project-path">${escapeHtml(displayPath)}</span>
                    </div>
                    <div class="welcome-project-meta">
                        ${p.session_name ? `<span class="welcome-project-session" title="${escapeHtml(p.session_name)}">${escapeHtml(p.session_name)}</span>` : ""}
                        <span class="welcome-project-time">${timeAgo(p.updated_at)}</span>
                        <span class="welcome-project-stats">${p.message_count} msgs</span>
                    </div>
                    <button type="button" class="welcome-project-remove" title="Remove from recents" aria-label="Remove from recents">\u2715</button>
                `;
                el.addEventListener("click", (e) => { if (!e.target.closest(".welcome-project-remove")) openProject(p); });
                const removeBtn = el.querySelector(".welcome-project-remove");
                removeBtn.addEventListener("click", async (e) => {
                    e.stopPropagation();
                    try {
                        const res = await fetch("/api/projects/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: p.path }) });
                        const data = await res.json();
                        if (data.ok) { el.remove(); if ($projectList.children.length === 0) $projectList.innerHTML = '<div class="welcome-no-projects">No recent projects. Open a local folder or connect via SSH to get started.</div>'; }
                        else showToast(data.error || "Failed to remove");
                    } catch (err) { showToast("Failed to remove from recents"); }
                });
                $projectList.appendChild(el);
            }
        } catch (e) {
            $projectList.innerHTML = '<div class="welcome-loading">Failed to load projects</div>';
        }
    }

    function parseSshCompositePath(path) {
        const raw = String(path || "").trim();
        const m = raw.match(/^([^@:\s]+)@([^:\s]+):(\d+):(.*)$/);
        if (!m) return null;
        const port = parseInt(m[3], 10);
        return {
            user: m[1].trim(),
            host: m[2].trim(),
            port: Number.isFinite(port) ? port : 22,
            key_path: "",
            directory: (m[4] || "").trim() || "/",
        };
    }

    function getSshProjectInfo(project) {
        const fromSaved = (project && typeof project.ssh_info === "object" && project.ssh_info) ? project.ssh_info : {};
        const fromPath = parseSshCompositePath(project?.path);
        const merged = {
            user: String(fromSaved.user || fromPath?.user || "").trim(),
            host: String(fromSaved.host || fromPath?.host || "").trim(),
            port: Number(fromSaved.port || fromPath?.port || 22) || 22,
            key_path: String(fromSaved.key_path || "").trim(),
            directory: String(fromSaved.directory || fromPath?.directory || "").trim(),
        };
        if (merged.host.startsWith("ssh://")) merged.host = merged.host.slice("ssh://".length).trim();
        if (merged.host.includes("@") && !merged.user) {
            const parts = merged.host.split("@");
            merged.user = (parts[0] || "").trim();
            merged.host = (parts[1] || "").trim();
        }
        if (!merged.directory) merged.directory = "/";
        if (!merged.user || !merged.host) return null;
        return merged;
    }

    async function openProject(project) {
        // If it's an SSH project, reconnect via SSH with saved details
        if (project.is_ssh) {
            try {
                showToast("Reconnecting via SSH...");
                const info = getSshProjectInfo(project);
                if (!info) {
                    showToast("SSH reconnect failed: missing host/user. Reconnect manually.");
                    return;
                }
                const res = await fetch("/api/ssh-connect", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        host: info.host,
                        user: info.user,
                        port: info.port || 22,
                        key_path: info.key_path || "",
                        directory: info.directory,
                    }),
                });
                const data = await res.json();
                if (!data.ok) {
                    showToast("SSH reconnect failed: " + (data.error || "unknown error"));
                    return;
                }
                transitionToIDE(data.path || `${info.user}@${info.host}:${info.directory}`);
            } catch (e) {
                showToast("Error: " + e.message);
            }
        } else {
            // Local project
            try {
                const res = await fetch("/api/set-directory", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ path: project.path }),
                });
                const data = await res.json();
                if (!data.ok) {
                    showToast("Failed to open: " + (data.error || "unknown error"));
                    return;
                }
                transitionToIDE(data.path);
            } catch (e) {
                showToast("Error: " + e.message);
            }
        }
    }

    function transitionToIDE(dirPath) {
        // Hide welcome, show IDE
        $welcomeScreen.classList.add("hidden");
        $ideWrapper.classList.remove("hidden");
        $workingDir.textContent = dirPath || "";

        // Initialize Monaco if not done
        if (!monacoInstance) initMonaco();

        // Connect WebSocket (will load session + replay history)
        disconnectWs();
        connect();
        // Terminal follows the project: disconnect and clear so it uses the new backend (local or SSH)
        if (typeof terminalDisconnect === "function" && typeof terminalConnect === "function") {
            terminalDisconnect(true);
            if ($terminalPanel && !$terminalPanel.classList.contains("hidden")) {
                terminalConnect();
            }
        }
        $input.focus();
    }

    function showWelcome() {
        // Disconnect without auto-reconnect
        disconnectWs();
        if (typeof terminalDisconnect === "function") terminalDisconnect(true);

        // Reset IDE state
        $chatMessages.innerHTML = "";
        clearAllDiffDecorations();
        openTabs.forEach((info) => { try { info.model.dispose(); } catch {} });
        openTabs.clear();
        $tabBar.innerHTML = "";
        activeTab = null;
        if (monacoInstance) monacoInstance.setModel(null);
        if (diffEditorInstance) { diffEditorInstance.dispose(); diffEditorInstance = null; }
        $editorWelcome.classList.remove("hidden");
        modifiedFiles.clear();
        clearPendingImages();
        setRunning(false);
        currentSessionId = null;
        persistSessionId(null);
        if ($agentSelect) {
            suppressAgentSwitch = true;
            $agentSelect.innerHTML = "";
            const opt = document.createElement("option");
            opt.value = "";
            opt.textContent = "No agents";
            $agentSelect.appendChild(opt);
            $agentSelect.disabled = true;
            suppressAgentSwitch = false;
        }

        // Show welcome, hide IDE
        $ideWrapper.classList.add("hidden");
        $welcomeScreen.classList.remove("hidden");

        // Refresh project list
        loadRecentProjects();
    }

    // ── Welcome: Open Local ──
    $welcomeOpenLocal.addEventListener("click", () => {
        $localPath.value = "";
        $localError.classList.add("hidden");
        $localModal.classList.remove("hidden");
        $localPath.focus();
    });
    $localCancel.addEventListener("click", () => $localModal.classList.add("hidden"));
    $localModal.querySelector(".welcome-modal-overlay").addEventListener("click", () => $localModal.classList.add("hidden"));

    async function submitLocalProject() {
        const path = $localPath.value.trim();
        if (!path) return;
        $localOpen.disabled = true;
        $localOpen.textContent = "Opening\u2026";
        $localError.classList.add("hidden");
        try {
            const res = await fetch("/api/set-directory", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path }),
            });
            const data = await res.json();
            if (data.ok) {
                $localModal.classList.add("hidden");
                transitionToIDE(data.path);
            } else {
                $localError.textContent = data.error || "Failed to open directory";
                $localError.classList.remove("hidden");
            }
        } catch (e) {
            $localError.textContent = "Error: " + e.message;
            $localError.classList.remove("hidden");
        } finally {
            $localOpen.disabled = false;
            $localOpen.textContent = "Open Project";
        }
    }
    $localOpen.addEventListener("click", submitLocalProject);
    $localPath.addEventListener("keydown", e => {
        if (e.key === "Enter") submitLocalProject();
        if (e.key === "Escape") $localModal.classList.add("hidden");
    });

    // ── Welcome: SSH Connect ──
    $welcomeSshBtn.addEventListener("click", () => {
        $sshError.classList.add("hidden");
        $sshModal.classList.remove("hidden");
        $sshHost.focus();
    });
    $sshCancel.addEventListener("click", () => $sshModal.classList.add("hidden"));
    $sshModal.querySelector(".welcome-modal-overlay").addEventListener("click", () => $sshModal.classList.add("hidden"));

    async function submitSSH() {
        const host = $sshHost.value.trim();
        const user = $sshUser.value.trim();
        const port = $sshPort.value.trim() || "22";
        const key = $sshKey.value.trim();
        const dir = $sshDir.value.trim();

        if (!host || !user || !dir) {
            $sshError.textContent = "Host, user, and remote directory are required.";
            $sshError.classList.remove("hidden");
            return;
        }

        $sshOpen.disabled = true;
        $sshOpen.textContent = "Connecting\u2026";
        $sshError.classList.add("hidden");

        try {
            const res = await fetch("/api/ssh-connect", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ host, user, port: parseInt(port), key_path: key, directory: dir }),
            });
            const data = await res.json();
            if (data.ok) {
                $sshModal.classList.add("hidden");
                transitionToIDE(data.path || `${user}@${host}:${dir}`);
            } else {
                $sshError.textContent = data.error || "SSH connection failed";
                $sshError.classList.remove("hidden");
            }
        } catch (e) {
            $sshError.textContent = "Error: " + e.message;
            $sshError.classList.remove("hidden");
        } finally {
            $sshOpen.disabled = false;
            $sshOpen.textContent = "Connect";
        }
    }
    $sshOpen.addEventListener("click", submitSSH);
    $sshDir.addEventListener("keydown", e => {
        if (e.key === "Enter") submitSSH();
        if (e.key === "Escape") $sshModal.classList.add("hidden");
    });

    // ── SSH browse remote folder ──
    let sshBrowseCurrentPath = "";

    async function loadSshBrowseDir(directory) {
        if (!$sshBrowseList) return;
        const host = $sshHost.value.trim();
        const user = $sshUser.value.trim();
        const port = $sshPort.value.trim() || "22";
        const key = $sshKey.value.trim();
        if (!host || !user) {
            $sshBrowseList.innerHTML = '<div class="ssh-browse-error">Enter host and user first.</div>';
            return;
        }
        $sshBrowseList.innerHTML = '<div class="ssh-browse-loading">Loading…</div>';
        $sshBrowseCurrent.textContent = "";
        try {
            const res = await fetch("/api/ssh-list-dir", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    host,
                    user,
                    port: parseInt(port, 10) || 22,
                    key_path: key || "",
                    directory: directory || "~",
                }),
            });
            const data = await res.json();
            if (!data.ok) {
                $sshBrowseList.innerHTML = `<div class="ssh-browse-error">${escapeHtml(data.error || "Failed to list directory")}</div>`;
                return;
            }
            sshBrowseCurrentPath = data.path || directory || "~";
            const entries = data.entries || [];
            const parent = data.parent;

            // Breadcrumb: e.g. / home user project -> clickable segments
            let pathParts = sshBrowseCurrentPath.replace(/\/$/, "").split("/").filter(Boolean);
            if (!sshBrowseCurrentPath.startsWith("/") && sshBrowseCurrentPath !== "") pathParts.unshift(sshBrowseCurrentPath);
            if (sshBrowseCurrentPath === "/" && pathParts.length === 0) pathParts = ["/"];
            let breadcrumbHtml = "";
            if (parent) {
                breadcrumbHtml += `<button type="button" class="ssh-browse-up" data-dir="${escapeHtml(parent)}" title="Parent">↩</button> `;
            }
            const isAbsolute = sshBrowseCurrentPath.startsWith("/");
            breadcrumbHtml += pathParts.map((p, i) => {
                const segPath = (isAbsolute && pathParts[0] !== "/")
                    ? "/" + pathParts.slice(0, i + 1).join("/")
                    : pathParts.slice(0, i + 1).join("/");
                const isLast = i === pathParts.length - 1;
                return isLast
                    ? `<span class="ssh-browse-seg current">${escapeHtml(p)}</span>`
                    : `<button type="button" class="ssh-browse-seg" data-dir="${escapeHtml(segPath)}">${escapeHtml(p)}</button> / `;
            }).join("");
            $sshBrowseBreadcrumb.innerHTML = breadcrumbHtml || escapeHtml(sshBrowseCurrentPath);
            $sshBrowseCurrent.textContent = sshBrowseCurrentPath;

            const dirs = entries.filter(e => e.type === "directory");
            const files = entries.filter(e => e.type === "file");
            let listHtml = "";
            dirs.forEach(e => {
                const nextPath = sshBrowseCurrentPath.replace(/\/?$/, "") + "/" + e.name;
                listHtml += `<button type="button" class="ssh-browse-entry dir" data-dir="${escapeHtml(nextPath)}"><span class="ssh-browse-icon">📁</span> ${escapeHtml(e.name)}</button>`;
            });
            files.forEach(e => {
                listHtml += `<div class="ssh-browse-entry file"><span class="ssh-browse-icon">📄</span> ${escapeHtml(e.name)}</div>`;
            });
            if (listHtml === "") listHtml = '<div class="ssh-browse-empty">No entries</div>';
            $sshBrowseList.innerHTML = listHtml;
        } catch (e) {
            $sshBrowseList.innerHTML = `<div class="ssh-browse-error">${escapeHtml(e.message || "Network error")}</div>`;
        }
    }

    if ($sshBrowseBtn) {
        $sshBrowseBtn.addEventListener("click", () => {
            if (!$sshBrowseModal) return;
            $sshBrowseModal.classList.remove("hidden");
            sshBrowseCurrentPath = $sshDir.value.trim() || "~";
            loadSshBrowseDir(sshBrowseCurrentPath);
        });
    }
    // One delegated listener so clicking any folder (or breadcrumb) navigates
    if ($sshBrowseList) {
        $sshBrowseList.addEventListener("click", (e) => {
            const btn = e.target.closest(".ssh-browse-entry.dir[data-dir]");
            if (btn && btn.dataset.dir) {
                e.preventDefault();
                loadSshBrowseDir(btn.dataset.dir);
            }
        });
    }
    if ($sshBrowseBreadcrumb) {
        $sshBrowseBreadcrumb.addEventListener("click", (e) => {
            const btn = e.target.closest(".ssh-browse-up[data-dir], .ssh-browse-seg[data-dir]");
            if (btn && btn.dataset.dir) {
                e.preventDefault();
                loadSshBrowseDir(btn.dataset.dir);
            }
        });
    }
    if ($sshBrowseSelect) {
        $sshBrowseSelect.addEventListener("click", () => {
            $sshDir.value = sshBrowseCurrentPath;
            if ($sshBrowseModal) $sshBrowseModal.classList.add("hidden");
        });
    }
    if ($sshBrowseCancel) {
        $sshBrowseCancel.addEventListener("click", () => {
            if ($sshBrowseModal) $sshBrowseModal.classList.add("hidden");
        });
    }
    if ($sshBrowseModal && $sshBrowseModal.querySelector(".welcome-modal-overlay")) {
        $sshBrowseModal.querySelector(".welcome-modal-overlay").addEventListener("click", () => {
            $sshBrowseModal.classList.add("hidden");
        });
    }

    // ── Logo click → back to welcome ──
    if ($logoHome) {
        $logoHome.addEventListener("click", showWelcome);
    }

    // ================================================================
    // INIT
    // ================================================================

    async function init() {
        initMonaco();

        try {
            const res = await fetch("/api/info");
            const info = await res.json();

            if (info.show_welcome === false) {
                // Started with explicit --dir or --ssh — go straight to IDE
                transitionToIDE(info.working_directory || ".");
            } else {
                // Show welcome screen with recent projects
                loadRecentProjects();
            }
        } catch {
            // API not ready — show welcome
            loadRecentProjects();
        }
    }

    init();

})();
