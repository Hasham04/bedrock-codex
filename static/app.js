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
    const $sessionName   = document.getElementById("session-name");
    const $agentSelect   = document.getElementById("agent-select");
    const $newAgentBtn   = document.getElementById("new-agent-btn");
    const $workingDir    = document.getElementById("working-dir");
    const $resetBtn      = document.getElementById("reset-btn");
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
    const $terminalOutput = document.getElementById("terminal-output");
    const $terminalInput = document.getElementById("terminal-input");
    const $terminalPrompt = document.getElementById("terminal-prompt");
    const $terminalCloseBtn = document.getElementById("terminal-close-btn");
    const $terminalClearBtn = document.getElementById("terminal-clear-btn");
    const $resizeTerminal = document.getElementById("resize-terminal");
    const $sourceControlList = document.getElementById("source-control-list");
    const $sourceControlRefreshBtn = document.getElementById("source-control-refresh-btn");
    let terminalCwd = null;  // current working directory for terminal (project root or after cd)
    let terminalCompletionState = null;  // { prefix, completions, index } for Tab cycling

    // ── State ─────────────────────────────────────────────────
    let ws = null;
    let isRunning = false;
    let monacoInstance = null;    // monaco.editor reference
    let diffEditorInstance = null;
    let activeTab = null;         // path of active tab
    const openTabs = new Map();   // path -> { model, viewState, content }
    const modifiedFiles = new Set(); // paths changed by agent
    let gitStatus = new Map();       // path -> 'M'|'A'|'D'|'U' (git status for explorer + inline diffs)
    let currentThinkingEl = null;
    let currentTextEl = null;
    let currentTextBuffer = "";
    let lastToolBlock = null;
    const toolRunById = new Map(); // tool_use_id -> run element
    let scoutEl = null;
    const pendingImages = []; // { id, file, previewUrl, name, size, media_type }
    let currentSessionId = null;
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

    // ── UI State ──────────────────────────────────────────────
    function setRunning(running) {
        isRunning = running;
        $sendBtn.classList.toggle("hidden", running);
        $cancelBtn.classList.toggle("hidden", !running);
        $input.disabled = running;
        if (!running) $input.focus();
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
            const statusCls = status === "M" ? "modified" : status === "A" ? "added" : status === "D" ? "deleted" : "untracked";
            const label = status === "M" ? "M" : status === "A" ? "A" : status === "D" ? "D" : "U";
            html += `<div class="source-control-item" data-path="${escapeHtml(path)}" data-status="${statusCls}">
                <span class="sc-status ${statusCls}">${escapeHtml(label)}</span>
                <span class="sc-path">${escapeHtml(path)}</span>
            </div>`;
        }
        $sourceControlList.innerHTML = html;
        $sourceControlList.querySelectorAll(".source-control-item").forEach(el => {
            el.addEventListener("click", () => openFile(el.dataset.path));
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

    async function refreshTree() {
        await fetchGitStatus();
        $fileTree.innerHTML = "";
        loadTree();
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
                "editor.background": "#0d1117",
                "editor.foreground": "#e6edf3",
                "editorLineNumber.foreground": "#6e7681",
                "editorCursor.foreground": "#58a6ff",
                "editor.selectionBackground": "#264f78",
                "editor.lineHighlightBackground": "#161b22",
            }
        });
        monacoReady = true;
    }

    async function openFile(path) {
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
            if (!res.ok) { showToast("Failed to open file"); return; }
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
                minimap: { enabled: true },
                scrollBeyondLastLine: false,
                automaticLayout: true,
                lineNumbers: "on",
                renderLineHighlight: "gutter",
                padding: { top: 8 },
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
                        overviewRuler: { color: "#3fb950", position: monaco.editor.OverviewRulerLane.Left },
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
                        overviewRuler: { color: "#58a6ff", position: monaco.editor.OverviewRulerLane.Left },
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
                        overviewRuler: { color: "#f85149", position: monaco.editor.OverviewRulerLane.Left },
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
                        overviewRuler: { color: "#3fb950", position: monaco.editor.OverviewRulerLane.Left },
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
                        overviewRuler: { color: "#58a6ff", position: monaco.editor.OverviewRulerLane.Left },
                    }
                });
            }
            for (const lineNum of diff.deleted) {
                decorations.push({
                    range: new monaco.Range(lineNum, 1, lineNum, 1),
                    options: {
                        isWholeLine: false,
                        linesDecorationsClassName: "diff-gutter-deleted",
                        overviewRuler: { color: "#f85149", position: monaco.editor.OverviewRulerLane.Left },
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

    // ================================================================
    // INTEGRATED TERMINAL (bottom of editor panel)
    // ================================================================

    const TERMINAL_DEFAULT_HEIGHT = 220;
    const TERMINAL_MIN_HEIGHT = 100;

    async function updateTerminalPrompt() {
        if (!$terminalPrompt) return;
        try {
            const res = await fetch("/api/terminal-cwd");
            const data = await res.json();
            const cwd = data.ok && data.cwd ? data.cwd : "~";
            terminalCwd = cwd !== "~" ? cwd : null;
            const short = cwd.split("/").filter(Boolean).pop() || cwd.replace(/^.*@/, "").split(":").pop() || "~";
            $terminalPrompt.textContent = short + " $ ";
        } catch {
            terminalCwd = null;
            $terminalPrompt.textContent = "$ ";
        }
    }

    function setTerminalCwdFromResponse(cwd) {
        if (cwd) terminalCwd = cwd;
        if ($terminalPrompt && terminalCwd) {
            const short = terminalCwd.split("/").filter(Boolean).pop() || terminalCwd.replace(/^.*@/, "").split(":").pop() || "~";
            $terminalPrompt.textContent = short + " $ ";
        }
    }

    function appendTerminalLine(htmlOrText, className) {
        if (!$terminalOutput) return;
        const line = document.createElement("div");
        line.className = "terminal-line" + (className ? " " + className : "");
        if (htmlOrText.startsWith("<")) {
            line.innerHTML = htmlOrText;
        } else {
            line.textContent = htmlOrText;
        }
        $terminalOutput.appendChild(line);
        $terminalOutput.scrollTop = $terminalOutput.scrollHeight;
    }

    async function runTerminalCommand() {
        const cmd = $terminalInput && $terminalInput.value.trim();
        if (!cmd) return;
        if ($terminalInput) $terminalInput.value = "";
        const promptText = ($terminalPrompt && $terminalPrompt.textContent) || "$ ";
        appendTerminalLine(promptText + escapeHtml(cmd), "terminal-command");
        appendTerminalLine("", "terminal-running");
        const runningEl = $terminalOutput.lastElementChild;
        const isCd = cmd === "cd" || (cmd.startsWith("cd ") && cmd.length > 3);
        const runCmd = isCd ? (cmd === "cd" ? "cd && pwd" : cmd + " && pwd") : cmd;
        const body = { command: runCmd };
        if (terminalCwd) body.cwd = terminalCwd;
        try {
            const res = await fetch("/api/terminal-run", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (runningEl) runningEl.remove();
            if (data.ok) {
                if (data.stdout) appendTerminalLine(data.stdout, "terminal-stdout");
                if (data.stderr) appendTerminalLine(data.stderr, "terminal-stderr");
                if (data.cwd) {
                    if (isCd && data.stdout) {
                        const firstLine = data.stdout.trim().split("\n")[0].trim();
                        if (firstLine) terminalCwd = firstLine;
                        else terminalCwd = data.cwd;
                    } else {
                        terminalCwd = data.cwd;
                    }
                    setTerminalCwdFromResponse(terminalCwd);
                }
            } else {
                appendTerminalLine("Error: " + (data.error && data.error.trim() ? data.error.trim() : "Unknown"), "terminal-stderr");
            }
        } catch (e) {
            if (runningEl) runningEl.remove();
            appendTerminalLine("Error: " + (e.message || "Request failed"), "terminal-stderr");
        }
        if ($terminalOutput) $terminalOutput.scrollTop = $terminalOutput.scrollHeight;
    }

    function getTerminalWordAtCursor() {
        if (!$terminalInput) return null;
        const line = $terminalInput.value;
        const pos = $terminalInput.selectionStart;
        let start = pos;
        while (start > 0 && line[start - 1] !== " " && line[start - 1] !== "\t") start--;
        let end = pos;
        while (end < line.length && line[end] !== " " && line[end] !== "\t") end++;
        const word = line.slice(start, end);
        const beforeCursor = line.slice(0, start);
        const isFirstWord = !beforeCursor.trim();
        return { word, start, end, isFirstWord };
    }

    function commonPrefix(arr) {
        if (!arr.length) return "";
        let p = arr[0];
        for (let i = 1; i < arr.length; i++) {
            while (arr[i].indexOf(p) !== 0) {
                p = p.slice(0, -1);
                if (!p) return "";
            }
        }
        return p;
    }

    async function handleTerminalTab() {
        if (!$terminalInput) return;
        const info = getTerminalWordAtCursor();
        if (!info) return;
        const { word, start, end, isFirstWord } = info;
        const completeType = isFirstWord ? "command" : "path";
        const body = { prefix: word, type: completeType };
        if (terminalCwd) body.cwd = terminalCwd;
        let completions = [];
        let prefix = word;
        try {
            const res = await fetch("/api/terminal-complete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!data.ok || !Array.isArray(data.completions)) return;
            completions = data.completions;
            prefix = data.prefix != null ? data.prefix : word;
        } catch {
            return;
        }
        if (!completions.length) return;
        let replacement;
        if (completions.length === 1) {
            replacement = completions[0];
            terminalCompletionState = null;
        } else {
            const samePrefix = terminalCompletionState && terminalCompletionState.prefix === prefix &&
                JSON.stringify(terminalCompletionState.completions) === JSON.stringify(completions);
            if (samePrefix && terminalCompletionState.index != null) {
                terminalCompletionState.index = (terminalCompletionState.index + 1) % completions.length;
                replacement = completions[terminalCompletionState.index];
            } else {
                const cp = commonPrefix(completions);
                replacement = cp && cp.length > prefix.length ? cp : completions[0];
                terminalCompletionState = { prefix, completions, index: 0 };
            }
        }
        const line = $terminalInput.value;
        $terminalInput.value = line.slice(0, start) + replacement + line.slice(end);
        $terminalInput.setSelectionRange(start + replacement.length, start + replacement.length);
    }

    function setTerminalPanelVisible(visible) {
        if (!$terminalPanel || !$resizeTerminal) return;
        if (visible) {
            $terminalPanel.classList.remove("hidden");
            $resizeTerminal.classList.remove("hidden");
            if (!$terminalPanel.style.height || $terminalPanel.dataset.height) {
                $terminalPanel.style.height = ($terminalPanel.dataset.height || TERMINAL_DEFAULT_HEIGHT) + "px";
            }
            updateTerminalPrompt();
            if ($terminalToggleBtn) $terminalToggleBtn.classList.add("active");
        } else {
            $terminalPanel.classList.add("hidden");
            $resizeTerminal.classList.add("hidden");
            if ($terminalToggleBtn) $terminalToggleBtn.classList.remove("active");
        }
        requestAnimationFrame(() => {
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
        $terminalCloseBtn.addEventListener("click", () => setTerminalPanelVisible(false));
    }
    if ($terminalClearBtn) {
        $terminalClearBtn.addEventListener("click", () => {
            if ($terminalOutput) $terminalOutput.innerHTML = "";
        });
    }
    if ($terminalInput) {
        $terminalInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                runTerminalCommand();
                return;
            }
            if (e.key === "Tab") {
                e.preventDefault();
                handleTerminalTab();
                return;
            }
            terminalCompletionState = null;
        });
    }

    // Terminal panel vertical resize
    if ($resizeTerminal && $terminalPanel) {
        $resizeTerminal.addEventListener("mousedown", (e) => {
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
    function updateThinkingHeader(block, done = false) {
        if (!block) return;
        const started = Number(block.dataset.startedAt || Date.now());
        const elapsed = Math.max(0, Math.round((Date.now() - started) / 1000));
        const titleEl = block.querySelector(".thinking-title");
        const statusEl = block.querySelector(".thinking-status-text");
        if (titleEl) {
            titleEl.textContent = done ? `Thought for ${elapsed}s` : (elapsed > 0 ? `Thinking for ${elapsed}s` : "Thinking...");
        }
        if (statusEl) {
            statusEl.textContent = done ? "Completed" : "In progress";
        }
    }

    function createThinkingBlock() {
        const bubble = getOrCreateBubble();
        const block = document.createElement("div"); block.className = "thinking-block";
        block.dataset.startedAt = String(Date.now());
        block.innerHTML = `
            <div class="thinking-header">
                <div class="thinking-left">
                    <span class="thinking-icon-wrap">
                        <svg class="thinking-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M12 3a6 6 0 0 0-6 6c0 2.5 1.3 4 2.5 5.1.8.8 1.5 1.4 1.5 2.4h4c0-1 .7-1.6 1.5-2.4C16.7 13 18 11.5 18 9a6 6 0 0 0-6-6z"/>
                            <path d="M9 19h6"/><path d="M10 22h4"/>
                        </svg>
                    </span>
                    <div class="thinking-meta">
                        <span class="thinking-title">Thinking...</span>
                        <span class="thinking-status-text">In progress</span>
                    </div>
                </div>
                <div class="thinking-right">
                    <span class="thinking-spinner spinner"></span>
                    <span class="thinking-chevron">\u25BC</span>
                </div>
            </div>
            <div class="thinking-content"></div>`;
        block.querySelector(".thinking-header").addEventListener("click", () => block.classList.toggle("collapsed"));
        bubble.appendChild(block);
        updateThinkingHeader(block, false);
        scrollChat();
        return block.querySelector(".thinking-content");
    }
    function finishThinking(el) {
        if (!el) return;
        const block = el.closest(".thinking-block"); if (!block) return;
        updateThinkingHeader(block, true);
        const spinner = block.querySelector(".thinking-spinner"); if (spinner) spinner.remove();
        block.classList.add("collapsed");
        block.appendChild(makeCopyBtn(() => el.textContent));
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
            pending: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.5" stroke="currentColor" stroke-width="2" fill="none"/></svg>`,
            failed: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>`,
            open: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            rerun: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            retry: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m1 4 4 4 4-4M23 20l-4-4-4 4M20 8a8 8 0 0 0-13-3M4 16a8 8 0 0 0 13 3" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            copy: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2" stroke="currentColor" stroke-width="2" fill="none"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>`,
            stop: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5" ry="1.5" stroke="currentColor" stroke-width="2" fill="none"/></svg>`,
        };
        return icons[kind] || "";
    }
    function toolCanOpenFile(name, input) {
        return Boolean((name === "read_file" || name === "write_file" || name === "edit_file" || name === "symbol_edit" || name === "lint_file") && input?.path);
    }
    function toolFollowupPrompt(name, input, failedOnly = false) {
        const title = toolTitle(name);
        if (name === "run_command" && input?.command) {
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
        send({ type: "task", content: prompt });
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
        if (name === "write_file") {
            const lines = String(input?.content || "").split("\n");
            const added = lines.slice(0, 16).map(l => `+${l}`);
            if (lines.length > 16) added.push(`+... (${lines.length - 16} more lines)`);
            return `+++ ${path}\n@@ new content preview @@\n${added.join("\n")}`;
        }
        if (name === "edit_file") {
            const oldLines = String(input?.old_string || "").split("\n");
            const newLines = String(input?.new_string || "").split("\n");
            const removed = oldLines.slice(0, 8).map(l => `-${l}`);
            const added = newLines.slice(0, 8).map(l => `+${l}`);
            if (oldLines.length > 8) removed.push(`-... (${oldLines.length - 8} more lines)`);
            if (newLines.length > 8) added.push(`+... (${newLines.length - 8} more lines)`);
            return `--- ${path}\n+++ ${path}\n@@ edit preview @@\n${removed.join("\n")}\n${added.join("\n")}`;
        }
        if (name === "symbol_edit") {
            const symbol = input?.symbol || "(symbol)";
            const newLines = String(input?.new_string || "").split("\n");
            const added = newLines.slice(0, 12).map(l => `+${l}`);
            if (newLines.length > 12) added.push(`+... (${newLines.length - 12} more lines)`);
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
                row.innerHTML = `<span class="tool-match-group">${escapeHtml(hit.group)}</span><span class="tool-match-loc">${escapeHtml(hit.path)}:${hit.line}</span><span class="tool-match-text">${escapeHtml(hit.text || "")}</span>`;
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
    function renderToolOutput(runEl, name, input, content, success, extraData) {
        const out = document.createElement("div");
        out.className = `tool-result ${success ? "tool-result-success" : "tool-result-error"} ${name === "run_command" ? "tool-result-terminal" : ""}`;
        out.innerHTML = `<div class="tool-section-label">${name === "run_command" ? "Output" : "Result"}</div>`;

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
        if (name === "read_file") {
            const preview = makeReadFilePreview(content, input);
            if (preview) out.appendChild(preview);
        }
        if (name === "write_file" || name === "edit_file" || name === "symbol_edit") {
            const diff = buildEditPreviewDiff(name, input);
            if (diff) {
                const mini = document.createElement("div");
                mini.className = "tool-mini-diff";
                mini.innerHTML = renderDiff(diff);
                out.appendChild(mini);
            }
        }

        out.appendChild(makeProgressiveBody(content || "(no output)", name === "run_command" ? "tool-terminal-body" : "", name === "run_command" ? 6000 : 2400));
        runEl.appendChild(out);
        return out;
    }
    function updateToolGroupHeader(_groupEl) {}
    function maybeAutoFollow(groupEl, runEl) {
        if (!groupEl || groupEl.dataset.toolName !== "run_command") return;
        if (groupEl.dataset.follow !== "1") return;
        const contentEl = groupEl.querySelector(".tool-content");
        if (contentEl) contentEl.scrollTop = contentEl.scrollHeight;
        const body = runEl.querySelector(".tool-terminal-body");
        if (body) body.scrollTop = body.scrollHeight;
        scrollChat();
    }
    function addToolCall(name, input, toolUseId = null) {
        const bubble = getOrCreateBubble();
        const isCmd = name === "run_command";
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
            group.classList.remove("collapsed");
            group.dataset.lastAt = String(now);
            group.dataset.count = String(Number(group.dataset.count || "1") + 1);
            updateToolGroupHeader(group);
            const statusEl = group.querySelector(".tool-status");
            if (statusEl) {
                statusEl.outerHTML = isCmd
                    ? `<span class="tool-status tool-status-running" title="Running"><span class="tool-spinner"></span></span>`
                    : `<span class="tool-status tool-status-pending" title="Pending">${toolActionIcon("pending")}</span>`;
            }
            if (isCmd && !group.querySelector(".tool-stop-btn")) {
                const actions = group.querySelector(".tool-actions");
                if (actions) {
                    const iconRow = actions.querySelector(".tool-action-icons") || actions;
                    const slot = document.createElement("span");
                    slot.className = "tool-action-slot";
                    const stopBtn = document.createElement("button");
                    stopBtn.className = "tool-stop-btn tool-icon-btn";
                    stopBtn.setAttribute("aria-label", "Stop command");
                    stopBtn.title = "Stop command";
                    stopBtn.innerHTML = toolActionIcon("stop");
                    stopBtn.addEventListener("click", (e) => {
                        e.stopPropagation();
                        send({ type: "cancel" });
                        stopBtn.disabled = true;
                        stopBtn.classList.add("is-stopping");
                        stopBtn.innerHTML = `<span class="tool-stop-spinner"></span>`;
                        stopBtn.title = "Stopping\u2026";
                    });
                    slot.appendChild(stopBtn);
                    iconRow.appendChild(slot);
                }
            }
        } else {
            const desc = toolDesc(name, input);
            const icon = toolIcon(name, input);
            group = document.createElement("div");
            group.className = isCmd ? "tool-block tool-block-command" : "tool-block collapsed";
            group.dataset.toolName = name;
            group.dataset.groupKey = key;
            group.dataset.count = "1";
            group.dataset.firstAt = String(now);
            group.dataset.lastAt = String(now);
            group.dataset.follow = "1";
            if (input?.path) group.dataset.path = input.path;
            const statusHtml = isCmd
                ? `<span class="tool-status tool-status-running" title="Running"><span class="tool-spinner"></span></span>`
                : `<span class="tool-status tool-status-pending" title="Pending">${toolActionIcon("pending")}</span>`;

            group.innerHTML = `
                <div class="tool-header ${isCmd ? "tool-header-cmd" : ""}">
                    <div class="tool-left">
                        <span class="tool-icon-wrap"><span class="tool-icon">${icon}</span></span>
                        <div class="tool-meta">
                            <div class="tool-title-row"><span class="tool-title">${escapeHtml(toolTitle(name))}</span></div>
                            <span class="tool-desc ${isCmd ? "tool-desc-cmd" : ""}">${escapeHtml(desc)}</span>
                        </div>
                    </div>
                    <div class="tool-right">
                        ${statusHtml}
                        <div class="tool-actions">
                            <div class="tool-action-icons">
                                <span class="tool-action-slot"><button type="button" class="tool-action-btn tool-icon-btn tool-action-open ${toolCanOpenFile(name, input) ? "" : "hidden"}" title="Open file" aria-label="Open file">${toolActionIcon("open")}</button></span>
                                <span class="tool-action-slot"><button type="button" class="tool-action-btn tool-icon-btn tool-action-rerun" title="Rerun" aria-label="Rerun">${toolActionIcon("rerun")}</button></span>
                                <span class="tool-action-slot"><button type="button" class="tool-action-btn tool-icon-btn tool-action-retry hidden" title="Retry failed" aria-label="Retry failed">${toolActionIcon("retry")}</button></span>
                                <span class="tool-action-slot"><button type="button" class="tool-action-btn tool-icon-btn tool-action-copy hidden" title="Copy output" aria-label="Copy output">${toolActionIcon("copy")}</button></span>
                                <span class="tool-action-slot"><button type="button" class="tool-stop-btn tool-icon-btn ${isCmd ? "" : "hidden"}" title="Stop command" aria-label="Stop command">${toolActionIcon("stop")}</button></span>
                            </div>
                            ${isCmd ? `<button type="button" class="tool-action-btn tool-follow-btn">Pause follow</button>` : ""}
                        </div>
                        <span class="tool-chevron">\u25BC</span>
                    </div>
                </div>
                <div class="tool-content ${isCmd ? "tool-content-cmd" : ""}">
                    <div class="tool-run-list"></div>
                </div>`;
            group.querySelector(".tool-header").addEventListener("click", () => group.classList.toggle("collapsed"));

            const path = input?.path;
            const openBtn = group.querySelector(".tool-action-open");
            if (openBtn && path) openBtn.addEventListener("click", (e) => { e.stopPropagation(); openFile(path); });
            const rerunBtn = group.querySelector(".tool-action-rerun");
            if (rerunBtn) rerunBtn.addEventListener("click", (e) => { e.stopPropagation(); runFollowupPrompt(toolFollowupPrompt(name, input, false)); });
            const retryBtn = group.querySelector(".tool-action-retry");
            if (retryBtn) retryBtn.addEventListener("click", (e) => { e.stopPropagation(); runFollowupPrompt(toolFollowupPrompt(name, input, true)); });
            const copyBtn = group.querySelector(".tool-action-copy");
            if (copyBtn) copyBtn.addEventListener("click", (e) => { e.stopPropagation(); copyText(group.dataset.latestOutput || ""); });
            const followBtn = group.querySelector(".tool-follow-btn");
            if (followBtn) {
                followBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const enabled = group.dataset.follow === "1";
                    group.dataset.follow = enabled ? "0" : "1";
                    followBtn.textContent = enabled ? "Resume follow" : "Pause follow";
                    followBtn.classList.toggle("paused", enabled);
                    if (!enabled) {
                        const contentEl = group.querySelector(".tool-content");
                        if (contentEl) contentEl.scrollTop = contentEl.scrollHeight;
                    }
                });
            }
            const stopBtn = group.querySelector(".tool-stop-btn");
            if (stopBtn) {
                stopBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    send({ type: "cancel" });
                    stopBtn.disabled = true;
                    stopBtn.classList.add("is-stopping");
                    stopBtn.innerHTML = `<span class="tool-stop-spinner"></span>`;
                    stopBtn.title = "Stopping\u2026";
                });
            }
            group.querySelectorAll(".tool-action-btn").forEach(btn => btn.addEventListener("click", e => e.stopPropagation()));

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
        run.innerHTML = `
            <div class="tool-run-header">
                <span class="tool-run-index">Run ${runIndex}</span>
                <span class="tool-run-time">${formatClock(now)}</span>
            </div>
            <div class="tool-section-label">${isCmd ? "Command" : "Input"}</div>`;
        const inputText = isCmd ? (input?.command || JSON.stringify(input, null, 2)) : JSON.stringify(input, null, 2);
        run.appendChild(makeProgressiveBody(inputText || "{}", isCmd ? "tool-input tool-input-cmd" : "tool-input", isCmd ? 2800 : 1800));
        runList.appendChild(run);
        toolRunState.set(run, { name, input: input || {}, output: "" });
        if (toolUseId) toolRunById.set(String(toolUseId), run);
        scrollChat();
        return run;
    }
    function addToolResult(content, success, runEl, extraData) {
        if (!runEl) return;
        const state = toolRunState.get(runEl);
        if (!state) return;
        const group = runEl.closest(".tool-block");
        if (!group) return;
        const isCmd = state.name === "run_command";

        const baseOutput = String(content || extraData?.error || "");
        const rawOutput = baseOutput || "(no output)";
        const prior = state.output || "";
        const merged = isCmd
            ? (baseOutput ? ((prior && !prior.includes(baseOutput)) ? `${prior}\n${baseOutput}` : (prior || baseOutput)) : (prior || rawOutput))
            : (rawOutput || prior || "(no output)");
        state.output = merged;
        toolRunState.set(runEl, state);

        runEl.querySelector(".tool-result")?.remove();
        renderToolOutput(runEl, state.name, state.input, merged, success, extraData);

        group.dataset.latestOutput = merged;
        const copyBtn = group.querySelector(".tool-action-copy");
        if (copyBtn) copyBtn.classList.remove("hidden");
        const retryBtn = group.querySelector(".tool-action-retry");
        if (retryBtn) retryBtn.classList.toggle("hidden", success !== false);

        const statusEl = group.querySelector(".tool-status");
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

        const stopBtn = group.querySelector(".tool-stop-btn");
        if (stopBtn && success !== undefined) stopBtn.remove();
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
    function toolLabel(n) {
        const labels = {
            read_file: "Read",
            write_file: "Write",
            edit_file: "Edit",
            symbol_edit: "Symbol",
            lint_file: "Lint",
            run_command: "Run",
            search: "Search",
            list_directory: "List",
            glob_find: "Glob",
            find_symbol: "Symbols",
            scout: "Scout",
        };
        return labels[n] || n;
    }
    function toolTitle(n) {
        const titles = {
            read_file: "Read",
            write_file: "Write",
            edit_file: "Edit",
            symbol_edit: "Symbol",
            lint_file: "Lint",
            run_command: "Run",
            search: "Search",
            list_directory: "List",
            glob_find: "Glob",
            find_symbol: "Symbols",
            scout: "Scout",
        };
        return titles[n] || toolLabel(n);
    }
    function toolDesc(n, i) {
        switch(n) {
            case "read_file": return i?.path || "";
            case "write_file": return i?.path || "";
            case "edit_file": return i?.path || "";
            case "symbol_edit": return `${i?.path || ""} :: ${i?.symbol || ""}`;
            case "lint_file": return i?.path || "";
            case "run_command": return i?.command || "";
            case "search": return `"${i?.pattern || ""}" in ${i?.path || "."}`;
            case "list_directory": return i?.path || ".";
            case "glob_find": return i?.pattern || "";
            case "find_symbol": return `${i?.symbol || ""} (${i?.kind || "all"}) in ${i?.path || "."}`;
            case "scout": return i?.task || "";
            default: return "";
        }
    }
    function toolIcon(n, input) {
        // File-based tools → show the file type icon
        if ((n === "read_file" || n === "write_file" || n === "edit_file" || n === "symbol_edit" || n === "lint_file") && input?.path) {
            return fileTypeIcon(input.path, 14);
        }
        const svgs = {
            run_command: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>`,
            search: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`,
            find_symbol: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16"/><path d="M7 4v3a5 5 0 0 0 10 0V4"/><line x1="12" y1="17" x2="12" y2="21"/><line x1="8" y1="21" x2="16" y2="21"/></svg>`,
            list_directory: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`,
            glob_find: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><path d="M11 8v6"/><path d="M8 11h6"/></svg>`,
            scout: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
        };
        return svgs[n] || `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/></svg>`;
    }

    // ================================================================
    // CHAT — Editable Plan
    // ================================================================

    let currentPlanSteps = [];

    function showPlan(steps, planFile, planText) {
        currentPlanSteps = [...steps];
        const bubble = getOrCreateBubble();
        const block = document.createElement("div"); block.className = "plan-block"; block.id = "active-plan";

        let html = `<div class="plan-title">\uD83D\uDCCB Plan`;
        if (planFile) {
            html += ` <button class="plan-open-file" title="Open plan file in editor" data-path="${escapeHtml(planFile)}">\uD83D\uDCC4 Open in Editor</button>`;
        }
        html += `</div>`;

        // Show the full plan document if available (rendered as markdown)
        if (planText && planText.length > 0) {
            html += `<div class="plan-document">`;
            html += `<div class="plan-document-content">${renderMarkdown(planText)}</div>`;
            html += `</div>`;
        }

        // Editable steps section
        html += `<div class="plan-steps-header">Implementation Steps <span style="font-weight:normal;font-size:10px;color:var(--text-muted)">(editable \u2014 modify before building)</span></div>`;
        html += `<div class="plan-steps-list">`;
        steps.forEach((s, i) => {
            html += `<div class="plan-step" data-idx="${i}">
                <span class="plan-step-num">${i+1}</span>
                <textarea class="plan-step-input" rows="1">${escapeHtml(s)}</textarea>
                <button class="plan-step-delete" title="Remove step">\u00D7</button>
            </div>`;
        });
        html += `</div>`;
        html += `<button class="plan-add-step">+ Add step</button>`;
        block.innerHTML = html;
        block.appendChild(makeCopyBtn(planText || steps.join("\n")));
        bubble.appendChild(block);

        // Highlight code blocks in the plan document
        block.querySelectorAll(".plan-document pre code").forEach(b => {
            if (typeof hljs !== "undefined") hljs.highlightElement(b);
        });

        // Open plan file in editor when clicked
        const openBtn = block.querySelector(".plan-open-file");
        if (openBtn) {
            openBtn.addEventListener("click", () => {
                const path = openBtn.dataset.path;
                if (path && typeof openFile === "function") {
                    openFile(path);
                }
            });
        }

        // Auto-resize textareas
        block.querySelectorAll(".plan-step-input").forEach(ta => {
            autoResizeTA(ta);
            ta.addEventListener("input", () => { autoResizeTA(ta); syncPlanSteps(); });
        });

        // Delete step
        block.querySelectorAll(".plan-step-delete").forEach(btn => {
            btn.addEventListener("click", () => {
                btn.closest(".plan-step").remove();
                renumberPlan();
                syncPlanSteps();
            });
        });

        // Add step
        block.querySelector(".plan-add-step").addEventListener("click", () => {
            addPlanStep("");
        });

        // Action bar with Build, Feedback, and Reject
        showActionBar([
            { label: "\u25B6 Build", cls: "primary", onClick: () => { hideActionBar(); send({ type: "build", steps: currentPlanSteps }); setRunning(true); }},
            { label: "\uD83D\uDCAC Feedback", cls: "secondary", onClick: () => { showPlanFeedbackInput(); }},
            { label: "\u2715 Reject", cls: "danger", onClick: () => { hideActionBar(); send({ type: "reject_plan" }); }},
        ]);
        scrollChat();
    }

    function addPlanStep(text) {
        const list = document.querySelector("#active-plan .plan-steps-list");
        if (!list) return;
        const idx = list.children.length;
        const step = document.createElement("div"); step.className = "plan-step"; step.dataset.idx = idx;
        step.innerHTML = `<span class="plan-step-num">${idx+1}</span><textarea class="plan-step-input" rows="1">${escapeHtml(text)}</textarea><button class="plan-step-delete" title="Remove step">\u00D7</button>`;
        const ta = step.querySelector(".plan-step-input");
        autoResizeTA(ta);
        ta.addEventListener("input", () => { autoResizeTA(ta); syncPlanSteps(); });
        step.querySelector(".plan-step-delete").addEventListener("click", () => { step.remove(); renumberPlan(); syncPlanSteps(); });
        list.appendChild(step);
        ta.focus();
        syncPlanSteps();
    }

    function renumberPlan() {
        document.querySelectorAll("#active-plan .plan-step").forEach((el, i) => {
            el.dataset.idx = i;
            el.querySelector(".plan-step-num").textContent = i + 1;
        });
    }

    function syncPlanSteps() {
        currentPlanSteps = [...document.querySelectorAll("#active-plan .plan-step-input")].map(ta => ta.value.trim()).filter(Boolean);
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

    // ── Plan step progress during build ────────────────────────
    function updatePlanStepProgress(stepNum, totalSteps) {
        const planBlock = document.getElementById("active-plan");
        if (!planBlock) return;

        // Disable editing during build
        planBlock.querySelectorAll(".plan-step-input").forEach(ta => { ta.readOnly = true; ta.style.cursor = "default"; });
        planBlock.querySelectorAll(".plan-step-delete, .plan-add-step").forEach(el => { el.style.display = "none"; });

        const steps = planBlock.querySelectorAll(".plan-step");
        steps.forEach((el, i) => {
            const num = i + 1;
            const numEl = el.querySelector(".plan-step-num");
            // Remove all states first
            el.classList.remove("step-done", "step-active", "step-pending");
            if (num < stepNum) {
                el.classList.add("step-done");
                if (numEl) numEl.innerHTML = "\u2713"; // checkmark
                // Add "Revert to here" button on completed steps
                if (!el.querySelector(".step-revert-btn")) {
                    const rb = document.createElement("button");
                    rb.className = "step-revert-btn";
                    rb.textContent = "Revert to here";
                    rb.title = `Revert all files to state after step ${num}`;
                    rb.addEventListener("click", (e) => {
                        e.stopPropagation();
                        if (confirm(`Revert all changes back to the end of step ${num}?`)) {
                            send({ type: "revert_to_step", step: num });
                        }
                    });
                    el.appendChild(rb);
                }
            } else if (num === stepNum) {
                el.classList.add("step-active");
                if (numEl) numEl.textContent = num;
            } else {
                el.classList.add("step-pending");
                if (numEl) numEl.textContent = num;
            }
        });

        // Also update/create a progress indicator at the top
        let prog = planBlock.querySelector(".plan-progress");
        if (!prog) {
            prog = document.createElement("div");
            prog.className = "plan-progress";
            const title = planBlock.querySelector(".plan-title");
            if (title) title.after(prog);
            else planBlock.prepend(prog);
        }
        prog.innerHTML = `<div class="plan-progress-bar"><div class="plan-progress-fill" style="width:${Math.round(((stepNum - 1) / totalSteps) * 100)}%"></div></div><span class="plan-progress-text">Step ${stepNum} of ${totalSteps}</span>`;
    }

    function markPlanComplete() {
        const planBlock = document.getElementById("active-plan");
        if (!planBlock) return;

        // Mark all steps as done
        planBlock.querySelectorAll(".plan-step").forEach(el => {
            el.classList.remove("step-active", "step-pending");
            el.classList.add("step-done");
            const numEl = el.querySelector(".plan-step-num");
            if (numEl) numEl.innerHTML = "\u2713";
        });

        // Update progress bar to 100%
        const prog = planBlock.querySelector(".plan-progress");
        if (prog) {
            const total = planBlock.querySelectorAll(".plan-step").length;
            prog.innerHTML = `<div class="plan-progress-bar"><div class="plan-progress-fill" style="width:100%"></div></div><span class="plan-progress-text">\u2713 All ${total} steps complete</span>`;
        }
    }

    // ================================================================
    // CHAT — Diff display
    // ================================================================

    function showDiffs(files) {
        const bubble = getOrCreateBubble();
        files.forEach(f => {
            const block = document.createElement("div"); block.className = "diff-block";
            const labelCls = f.label === "new file" ? "new-file" : "modified";
            block.innerHTML = `<div class="diff-file-header"><div style="display:flex;align-items:center;gap:8px"><span class="diff-file-name">${escapeHtml(f.path)}</span><span class="diff-file-label ${labelCls}">${escapeHtml(f.label)}</span></div><div class="diff-stats"><span class="add">+${f.additions}</span><span class="del">-${f.deletions}</span></div></div><div class="diff-content">${renderDiff(f.diff)}</div>`;

            block.querySelector(".diff-file-header").addEventListener("click", () => block.classList.toggle("collapsed"));

            // Click file name to open in Monaco diff view
            block.querySelector(".diff-file-name").style.cursor = "pointer";
            block.querySelector(".diff-file-name").addEventListener("click", (e) => { e.stopPropagation(); openDiffForFile(f.path); });

            block.appendChild(makeCopyBtn(f.diff));
            bubble.appendChild(block);
            markFileModified(f.path);
        });

        showActionBar([
            { label: "\u2713 Keep All Changes", cls: "success", onClick: () => { hideActionBar(); send({type:"keep"}); showInfo("\u2713 Changes kept."); clearAllDiffDecorations(); modifiedFiles.clear(); refreshTree(); }},
            { label: "\u2715 Revert All", cls: "danger", onClick: () => { hideActionBar(); send({type:"revert"}); showInfo("\u21A9 Reverted."); clearAllDiffDecorations(); modifiedFiles.clear(); refreshTree(); reloadAllModifiedFiles(); }},
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

    function showClarifyingQuestion(question, context, tool_use_id) {
        const wrap = document.createElement("div");
        wrap.className = "clarifying-question-box";
        wrap.innerHTML = `<div class="clarifying-question-label">\u2753 Agent is asking:</div><div class="clarifying-question-text">${escapeHtml(question)}</div>${context ? `<div class="clarifying-question-context">${escapeHtml(context)}</div>` : ""}<textarea class="clarifying-question-input" rows="2" placeholder="Type your answer..."></textarea><button type="button" class="clarifying-question-send">Send answer</button>`;
        const ta = wrap.querySelector(".clarifying-question-input");
        const btn = wrap.querySelector(".clarifying-question-send");
        btn.addEventListener("click", () => {
            const answer = ta.value.trim();
            if (!answer) return;
            send({ type: "user_answer", answer: answer, tool_use_id: tool_use_id });
            wrap.classList.add("answered");
            wrap.querySelector(".clarifying-question-input").style.display = "none";
            wrap.querySelector(".clarifying-question-send").style.display = "none";
            const done = document.createElement("div"); done.className = "clarifying-question-done"; done.textContent = "\u2713 Sent: " + (answer.length > 60 ? answer.slice(0, 60) + "..." : answer); wrap.appendChild(done);
        });
        ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); btn.click(); } });
        $chatMessages.appendChild(wrap);
        scrollChat();
        ta.focus();
    }
    function showPhase(name) {
        const div = document.createElement("div"); div.className = "phase-indicator"; div.id = `phase-${name}`;
        div.innerHTML = `<div class="spinner"></div><span>${escapeHtml(phaseLabel(name))}</span>`;
        $chatMessages.appendChild(div); scrollChat();
    }
    function endPhase(name, elapsed) {
        const el = document.getElementById(`phase-${name}`);
        if (el) { el.classList.add("done"); el.querySelector("span").textContent = `${phaseLabel(name)} \u2014 ${elapsed}s`; }
    }
    function phaseLabel(n) { return {plan:"Planning\u2026",build:"Building\u2026",direct:"Running\u2026"}[n]||n; }

    function showScoutProgress(text) {
        if (!scoutEl) {
            const bubble = getOrCreateBubble();
            scoutEl = document.createElement("div"); scoutEl.className = "scout-block";
            scoutEl.innerHTML = `<div class="spinner"></div><span></span>`;
            bubble.appendChild(scoutEl);
        }
        scoutEl.querySelector("span").textContent = text || "Scanning\u2026";
        scrollChat();
    }
    function endScout() { if (scoutEl) { scoutEl.querySelector(".spinner")?.remove(); scoutEl.querySelector("span").textContent = "\u2713 Scan complete"; scoutEl = null; } }

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

    function _showReconnectBanner() {
        let banner = document.getElementById("reconnect-banner");
        if (!banner) {
            banner = document.createElement("div");
            banner.id = "reconnect-banner";
            banner.innerHTML = '<span class="reconnect-spinner"></span> Connection lost. Reconnecting...';
            $chatMessages.parentElement.insertBefore(banner, $chatMessages);
        }
        banner.style.display = "flex";
    }

    function _hideReconnectBanner() {
        const banner = document.getElementById("reconnect-banner");
        if (banner) banner.style.display = "none";
    }

    function connect() {
        _preventReconnect = false;
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
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
    function send(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

    function handleEvent(evt) {
        switch (evt.type) {
            case "init":
                $modelName.textContent = evt.model_name || "?";
                currentSessionId = evt.session_id || currentSessionId;
                $sessionName.textContent = evt.session_name || "default";
                $tokenCount.textContent = formatTokens(evt.total_tokens || 0) + " tokens";
                $workingDir.textContent = evt.working_directory || "";
                loadAgentSessions();
                toolRunById.clear();
                if (_isFirstConnect) {
                    // First connect: clear chat, load tree fresh
                    $chatMessages.innerHTML = "";
                    loadTree();
                } else {
                    // Reconnect: clear chat before replay rebuilds it,
                    // but don't flash — the reconnect banner covers the gap
                    $chatMessages.innerHTML = "";
                    loadTree();
                }
                _isFirstConnect = false;
                break;
            case "thinking_start": currentThinkingEl = createThinkingBlock(); break;
            case "thinking":
                if (currentThinkingEl) {
                    currentThinkingEl.textContent += evt.content || "";
                    const thinkingBlock = currentThinkingEl.closest(".thinking-block");
                    updateThinkingHeader(thinkingBlock, false);
                    scrollChat();
                }
                break;
            case "thinking_end": finishThinking(currentThinkingEl); currentThinkingEl = null; break;
            case "text_start": currentTextEl = null; currentTextBuffer = ""; break;
            case "text":
                currentTextBuffer += evt.content || "";
                if (!currentTextEl) { const b = getOrCreateBubble(); currentTextEl = document.createElement("div"); currentTextEl.className = "text-content"; b.appendChild(currentTextEl); }
                currentTextEl.innerHTML = renderMarkdown(currentTextBuffer);
                currentTextEl.querySelectorAll("pre code").forEach(b => { if (typeof hljs !== "undefined") hljs.highlightElement(b); });
                scrollChat();
                break;
            case "text_end":
                if (currentTextEl && currentTextBuffer) {
                    currentTextEl.innerHTML = renderMarkdown(currentTextBuffer);
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
                // Track file modifications
                if (evt.data?.name === "write_file" || evt.data?.name === "edit_file" || evt.data?.name === "symbol_edit") {
                    const p = evt.data?.input?.path;
                    if (p) { markFileModified(p); reloadFileInEditor(p); }
                }
                break;
            case "tool_result":
                {
                    const runEl = (evt.data?.tool_use_id && toolRunById.get(String(evt.data.tool_use_id))) || lastToolBlock;
                    addToolResult(evt.content || "", evt.data?.success !== false, runEl, evt.data);
                }
                // Reload file if it was just written
                {
                    const runEl = (evt.data?.tool_use_id && toolRunById.get(String(evt.data.tool_use_id))) || lastToolBlock;
                    if (runEl) {
                        const tn = runEl.dataset.toolName;
                        if (tn === "write_file" || tn === "edit_file" || tn === "symbol_edit") {
                            const path = runEl.dataset.path;
                            if (path) reloadFileInEditor(path);
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
                    showInfo(`Checkpoint created: ${evt.data.checkpoint_id}`);
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
            case "user_question": showClarifyingQuestion(evt.question || "", evt.context || "", evt.tool_use_id || ""); break;
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
            case "plan_step_progress":
                updatePlanStepProgress(
                    evt.step || (evt.data && evt.data.step) || 1,
                    evt.total || (evt.data && evt.data.total) || 1
                );
                break;
            case "diff":
                showDiffs(evt.files || []);
                setRunning(false);
                refreshTree();
                break;
            case "no_changes": showInfo("No file changes."); setRunning(false); break;
            case "no_plan": showInfo("Completed directly."); setRunning(false); break;
            case "done":
                setRunning(false);
                if (evt.data) updateTokenDisplay(evt.data);
                break;
            case "kept": showInfo("\u2713 Changes kept."); break;
            case "reverted": showInfo("\u21A9 Reverted " + (evt.files||[]).length + " file(s)."); refreshTree(); break;
            case "reverted_to_step": showInfo("\u21A9 Reverted to step " + evt.step + " (" + (evt.files||[]).length + " file(s))"); refreshTree(); break;
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
                $sessionName.textContent = evt.session_name || "default";
                $tokenCount.textContent = "0 tokens";
                loadAgentSessions();
                toolRunById.clear();
                clearPendingImages();
                hideActionBar(); modifiedFiles.clear(); refreshTree();
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
                rt.textContent = evt.content || "";
                finishThinking(rt);
                break;
            }
            case "replay_tool_call":
                lastToolBlock = addToolCall(
                    evt.data?.name || "tool",
                    evt.data?.input || {},
                    evt.data?.id || evt.data?.tool_use_id || null
                );
                break;
            case "replay_tool_result":
                {
                    const runEl = (evt.data?.tool_use_id && toolRunById.get(String(evt.data.tool_use_id))) || lastToolBlock;
                    addToolResult(evt.content || "", evt.data?.success !== false, runEl, evt.data);
                    if (evt.data?.tool_use_id) toolRunById.delete(String(evt.data.tool_use_id));
                }
                lastToolBlock = null;
                break;
            case "replay_done":
                scrollChat();
                break;
            case "replay_state":
                // Restore interactive UI state after reconnect
                if (evt.awaiting_build && evt.pending_plan) {
                    showPlan(evt.pending_plan, null, "");
                    // Don't setRunning(false) — we want the user to click Build
                }
                if (evt.awaiting_keep_revert && evt.has_diffs) {
                    if (evt.diffs && evt.diffs.length > 0) {
                        // Show the full diff view with keep/revert
                        showDiffs(evt.diffs);
                    } else {
                        // Fallback: just show keep/revert action bar
                        showActionBar([
                            { label: "\u2713 Keep All", cls: "primary", onClick: () => { hideActionBar(); send({ type: "keep" }); }},
                            { label: "\u21A9 Revert All", cls: "danger", onClick: () => { hideActionBar(); send({ type: "revert" }); }},
                        ]);
                    }
                    showInfo("You have pending file changes from a previous session.");
                }
                break;

            // ── External file changes ──
            case "file_changed":
                // Refresh file tree entry and reload file in editor if open
                refreshTree();
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
        const text = $input.value.trim();
        const hasImages = pendingImages.length > 0;
        if ((!text && !hasImages) || isRunning) return;
        if (text.startsWith("/") && !hasImages) { handleCommand(text); $input.value = ""; autoResizeInput(); return; }

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
        send({ type: "task", content: text, images: imagesPayload });
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

    function autoResizeInput() { $input.style.height = "auto"; $input.style.height = Math.min($input.scrollHeight, 150) + "px"; }
    $input.addEventListener("input", autoResizeInput);
    $input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitTask(); } });
    if ($attachImageBtn && $imageInput) {
        $attachImageBtn.addEventListener("click", () => $imageInput.click());
        $imageInput.addEventListener("change", (e) => {
            const files = Array.from(e.target.files || []);
            addPendingImageFiles(files);
            $imageInput.value = "";
        });
    }
    $sendBtn.addEventListener("click", submitTask);
    $cancelBtn.addEventListener("click", () => send({type:"cancel"}));
    $resetBtn.addEventListener("click", () => send({type:"reset"}));
    if ($newAgentBtn) {
        $newAgentBtn.addEventListener("click", createNewAgentSession);
    }
    if ($agentSelect) {
        $agentSelect.addEventListener("change", () => {
            if (suppressAgentSwitch) return;
            const nextId = $agentSelect.value || null;
            if (!nextId || nextId === currentSessionId) return;
            currentSessionId = nextId;
            disconnectWs();
            connect();
        });
    }
    // Escape key cancels the running agent
    document.addEventListener("keydown", e => {
        if (e.key === "Escape" && isRunning) { e.preventDefault(); send({type:"cancel"}); }
    });

    // ── Keyboard shortcuts ──
    document.addEventListener("keydown", e => {
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
                `;
                el.addEventListener("click", () => openProject(p));
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
        $input.focus();
    }

    function showWelcome() {
        // Disconnect without auto-reconnect
        disconnectWs();

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
