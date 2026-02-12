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

    // ── DOM refs — IDE ────────────────────────────────────────
    const $fileTree      = document.getElementById("file-tree");
    const $refreshTree   = document.getElementById("refresh-tree-btn");
    const $tabBar        = document.getElementById("tab-bar");
    const $editorWelcome = document.getElementById("editor-welcome");
    const $monacoEl      = document.getElementById("monaco-container");
    const $chatMessages  = document.getElementById("chat-messages");
    const $input         = document.getElementById("user-input");
    const $sendBtn       = document.getElementById("send-btn");
    const $cancelBtn     = document.getElementById("cancel-btn");
    const $actionBar     = document.getElementById("action-bar");
    const $actionBtns    = document.getElementById("action-buttons");
    const $modelName     = document.getElementById("model-name");
    const $tokenCount    = document.getElementById("token-count");
    const $connStatus    = document.getElementById("connection-status");
    const $sessionName   = document.getElementById("session-name");
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

    // ── State ─────────────────────────────────────────────────
    let ws = null;
    let isRunning = false;
    let monacoInstance = null;    // monaco.editor reference
    let diffEditorInstance = null;
    let activeTab = null;         // path of active tab
    const openTabs = new Map();   // path -> { model, viewState, content }
    const modifiedFiles = new Set(); // paths changed by agent
    let currentThinkingEl = null;
    let currentTextEl = null;
    let currentTextBuffer = "";
    let lastToolBlock = null;
    let scoutEl = null;

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

    async function loadTree(parentPath = "", parentEl = null) {
        const target = parentEl || $fileTree;
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
                    el.innerHTML = `
                        <div class="tree-item ${modifiedFiles.has(item.path) ? 'modified' : ''}" data-path="${escapeHtml(item.path)}" data-type="file" style="padding-left:${8 + depth*16 + 16}px">
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

    function refreshTree() { $fileTree.innerHTML = ""; loadTree(); }
    $refreshTree.addEventListener("click", refreshTree);

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

        // Apply inline diff decorations if this file was modified by the agent
        if (modifiedFiles.has(path)) {
            applyInlineDiffDecorations(path);
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
        if (modifiedFiles.has(path)) tab.classList.add("modified");

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
    // CHAT — Messages
    // ================================================================

    function addUserMessage(text) {
        const div = document.createElement("div"); div.className = "message user";
        const bubble = document.createElement("div"); bubble.className = "msg-bubble";
        bubble.textContent = text;
        bubble.appendChild(makeCopyBtn(text));
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
    function createThinkingBlock() {
        const bubble = getOrCreateBubble();
        const block = document.createElement("div"); block.className = "thinking-block";
        block.innerHTML = `<div class="thinking-header"><span class="thinking-chevron">\u25BC</span><span class="spinner" style="width:10px;height:10px;border:2px solid var(--border);border-top:2px solid var(--accent);border-radius:50%;animation:spin .8s linear infinite;"></span><span>Thinking\u2026</span></div><div class="thinking-content"></div>`;
        block.querySelector(".thinking-header").addEventListener("click", () => block.classList.toggle("collapsed"));
        bubble.appendChild(block);
        scrollChat();
        return block.querySelector(".thinking-content");
    }
    function finishThinking(el) {
        if (!el) return;
        const block = el.closest(".thinking-block"); if (!block) return;
        const header = block.querySelector(".thinking-header");
        const spinner = header.querySelector(".spinner"); if (spinner) spinner.remove();
        header.querySelector("span:last-child").textContent = "Thought process";
        block.classList.add("collapsed");
        block.appendChild(makeCopyBtn(() => el.textContent));
    }

    // Tool blocks
    function addToolCall(name, input) {
        const bubble = getOrCreateBubble();
        const isCmd = name === "run_command";
        // Commands auto-expand; everything else starts collapsed
        const block = document.createElement("div");
        block.className = isCmd ? "tool-block tool-block-command" : "tool-block collapsed";
        block.dataset.toolName = name;
        const desc = toolDesc(name, input), icon = toolIcon(name, input);
        let statusHtml = "";
        if (isCmd) {
            statusHtml = `<span class="tool-status tool-status-running"><span class="tool-spinner"></span> Running</span>`
                + `<button class="tool-stop-btn" title="Stop command">Stop</button>`;
        }
        const label = toolLabel(name);
        if (isCmd) {
            // Terminal-style layout for commands
            block.innerHTML = `<div class="tool-header tool-header-cmd"><span class="tool-icon">${icon}</span><span class="tool-name">${escapeHtml(label)}</span><span class="tool-desc tool-desc-cmd">${escapeHtml(desc)}</span>${statusHtml}<span class="tool-chevron">\u25BC</span></div><div class="tool-content tool-content-cmd"><div class="tool-input tool-input-cmd">${escapeHtml(input?.command || JSON.stringify(input, null, 2))}</div></div>`;
        } else {
            block.innerHTML = `<div class="tool-header"><span class="tool-icon">${icon}</span><span class="tool-name">${escapeHtml(label)}</span><span class="tool-desc">${escapeHtml(desc)}</span>${statusHtml}<span class="tool-chevron">\u25BC</span></div><div class="tool-content"><div class="tool-input">${escapeHtml(JSON.stringify(input, null, 2))}</div></div>`;
        }
        block.querySelector(".tool-header").addEventListener("click", () => block.classList.toggle("collapsed"));

        // Stop button for commands — sends cancel
        const stopBtn = block.querySelector(".tool-stop-btn");
        if (stopBtn) {
            stopBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                send({type: "cancel"});
                stopBtn.disabled = true;
                stopBtn.textContent = "Stopping\u2026";
                stopBtn.title = "Stopping\u2026";
            });
        }

        // Click file path to open in editor
        if ((name === "read_file" || name === "write_file" || name === "edit_file" || name === "lint_file") && input?.path) {
            const nameEl = block.querySelector(".tool-desc");
            nameEl.style.cursor = "pointer";
            nameEl.style.textDecoration = "underline";
            nameEl.addEventListener("click", (e) => { e.stopPropagation(); openFile(input.path); });
        }

        bubble.appendChild(block);
        scrollChat();
        return block;
    }
    function addToolResult(content, success, block, extraData) {
        if (!block) return;
        const div = block.querySelector(".tool-content");
        const isCmd = block.dataset.toolName === "run_command";
        const r = document.createElement("div");
        if (isCmd) {
            // Terminal-style output
            r.className = `tool-result tool-result-terminal ${success ? "tool-result-success" : "tool-result-error"}`;
            r.textContent = content || "(no output)";
        } else {
            r.className = `tool-result ${success ? "tool-result-success" : "tool-result-error"}`;
            r.style.marginTop = "6px"; r.style.borderTop = "1px solid var(--border-light)"; r.style.paddingTop = "6px";
            r.textContent = truncate(content, 1500);
        }
        div.appendChild(r);
        block.appendChild(makeCopyBtn(content));

        // Remove stop button once done
        const stopBtn = block.querySelector(".tool-stop-btn");
        if (stopBtn) stopBtn.remove();

        // Update status badge for commands
        const statusEl = block.querySelector(".tool-status");
        if (statusEl) {
            if (isCmd) {
                const exitCode = extraData?.exit_code;
                const duration = extraData?.duration;
                let badge = "";
                if (success) {
                    badge = `<span class="tool-status tool-status-success">exit 0</span>`;
                } else {
                    const code = exitCode !== undefined && exitCode !== null ? exitCode : "?";
                    badge = `<span class="tool-status tool-status-error">exit ${code}</span>`;
                }
                if (duration !== undefined && duration !== null) {
                    badge += `<span class="tool-status tool-status-duration">${duration}s</span>`;
                }
                statusEl.outerHTML = badge;
            } else {
                statusEl.remove();
            }
        }
    }
    function toolLabel(n) {
        const labels = {
            read_file: "Read",
            write_file: "Write",
            edit_file: "Edit",
            lint_file: "Lint",
            run_command: "Terminal",
            search: "Search",
            list_directory: "List Files",
            glob_find: "Find Files",
            scout: "Scout",
        };
        return labels[n] || n;
    }
    function toolDesc(n, i) {
        switch(n) {
            case "read_file": return i?.path || "";
            case "write_file": return i?.path || "";
            case "edit_file": return i?.path || "";
            case "lint_file": return i?.path || "";
            case "run_command": return i?.command || "";
            case "search": return `"${i?.pattern || ""}" in ${i?.path || "."}`;
            case "list_directory": return i?.path || ".";
            case "glob_find": return i?.pattern || "";
            case "scout": return i?.task || "";
            default: return "";
        }
    }
    function toolIcon(n, input) {
        // File-based tools → show the file type icon
        if ((n === "read_file" || n === "write_file" || n === "edit_file" || n === "lint_file") && input?.path) {
            return fileTypeIcon(input.path, 14);
        }
        const svgs = {
            run_command: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>`,
            search: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`,
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
        ws = new WebSocket(`${proto}//${location.host}/ws`);
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
        if (ws) { ws.close(); ws = null; }
    }
    function send(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

    function handleEvent(evt) {
        switch (evt.type) {
            case "init":
                $modelName.textContent = evt.model_name || "?";
                $sessionName.textContent = evt.session_name || "default";
                $tokenCount.textContent = formatTokens(evt.total_tokens || 0) + " tokens";
                $workingDir.textContent = evt.working_directory || "";
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
                if (currentThinkingEl) { currentThinkingEl.textContent += evt.content || ""; scrollChat(); }
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
                lastToolBlock = addToolCall(evt.data?.name || "tool", evt.data?.input || evt.data || {});
                // Track file modifications
                if (evt.data?.name === "write_file" || evt.data?.name === "edit_file") {
                    const p = evt.data?.input?.path;
                    if (p) { markFileModified(p); reloadFileInEditor(p); }
                }
                break;
            case "tool_result":
                addToolResult(evt.content || "", evt.data?.success !== false, lastToolBlock, evt.data);
                // Reload file if it was just written
                if (lastToolBlock) {
                    const tn = lastToolBlock.dataset.toolName;
                    if (tn === "write_file" || tn === "edit_file") {
                        const desc = lastToolBlock.querySelector(".tool-desc")?.textContent;
                        if (desc) reloadFileInEditor(desc);
                    }
                }
                lastToolBlock = null;
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
                    el.outerHTML = `<span class="tool-status tool-status-error">cancelled</span>`;
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
                document.querySelectorAll(".thinking-block .spinner").forEach(el => el.remove());
                lastToolBlock = null;
                break;
            case "reset_done":
                $chatMessages.innerHTML = "";
                $sessionName.textContent = evt.session_name || "default";
                $tokenCount.textContent = "0 tokens";
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
                lastToolBlock = addToolCall(evt.data?.name || "tool", evt.data?.input || {});
                break;
            case "replay_tool_result":
                addToolResult(evt.content || "", evt.data?.success !== false, lastToolBlock);
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

    function submitTask() {
        const text = $input.value.trim();
        if (!text || isRunning) return;
        if (text.startsWith("/")) { handleCommand(text); $input.value = ""; autoResizeInput(); return; }
        addUserMessage(text);
        $input.value = ""; autoResizeInput();
        setRunning(true);
        addAssistantMessage();
        send({ type: "task", content: text });
    }

    function handleCommand(text) {
        const cmd = text.split(/\s+/)[0].toLowerCase();
        switch(cmd) {
            case "/reset": send({type:"reset"}); break;
            case "/cancel": send({type:"cancel"}); break;
            case "/help": showInfo("Commands: /reset \u2014 new session  |  /cancel \u2014 stop task"); break;
            default: showInfo(`Unknown: ${cmd}`);
        }
    }

    function autoResizeInput() { $input.style.height = "auto"; $input.style.height = Math.min($input.scrollHeight, 150) + "px"; }
    $input.addEventListener("input", autoResizeInput);
    $input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitTask(); } });
    $sendBtn.addEventListener("click", submitTask);
    $cancelBtn.addEventListener("click", () => send({type:"cancel"}));
    $resetBtn.addEventListener("click", () => send({type:"reset"}));
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
                if (p.is_ssh && p.ssh_info) {
                    displayPath = `${p.ssh_info.user}@${p.ssh_info.host}:${p.ssh_info.directory}`;
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

    async function openProject(project) {
        // If it's an SSH project, reconnect via SSH with saved details
        if (project.is_ssh && project.ssh_info) {
            try {
                showToast("Reconnecting via SSH...");
                const info = project.ssh_info;
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
        setRunning(false);

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
