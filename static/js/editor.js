/* ============================================================
   Bedrock Codex — editor.js
   Monaco editor init, tab management, breadcrumb, file open/save,
   diff views, inline diff decorations, resize handles
   ============================================================ */
(function (BX) {
    "use strict";

    // DOM ref aliases (immutable — safe to alias)
    var $editorWelcome = BX.$editorWelcome;
    var $monacoEl = BX.$monacoEl;
    var $tabBar = BX.$tabBar;
    var $fileTree = BX.$fileTree;

    // Module-local DOM refs
    var $breadcrumb = document.getElementById("editor-breadcrumb");

    // Reference-type state aliases (safe — same underlying object)
    var openTabs = BX.openTabs;
    var modifiedFiles = BX.modifiedFiles;

    // Module-private state
    var diffDecorationIds = new Map();
    var _monacoInited = false;

    // ================================================================
    // MONACO EDITOR INIT
    // ================================================================

    async function initMonaco() {
        if (_monacoInited) return;
        var m = await window.monacoReady;
        m.editor.defineTheme("bedrock-dark", {
            base: "vs-dark",
            inherit: true,
            rules: [],
            colors: {
                "editor.background": "#0f1117",
                "editor.foreground": "#e4e7ef",
                "editorLineNumber.foreground": "#4e5568",
                "editorCursor.foreground": "#6c9fff",
                "editor.selectionBackground": "#264f78",
                "editor.lineHighlightBackground": "#151822",
                "editorWidget.background": "#151822",
                "editorWidget.border": "#1a1f2e",
                "editorSuggestWidget.background": "#151822",
                "editorSuggestWidget.border": "#1a1f2e"
            }
        });

        m.languages.registerDefinitionProvider("*", {
            provideDefinition: async function (model, position) {
                var word = model.getWordAtPosition(position);
                if (!word) return null;
                try {
                    var res = await fetch("/api/find-symbol?symbol=" + encodeURIComponent(word.word) + "&kind=definition");
                    if (!res.ok) return null;
                    var data = await res.json();
                    if (!data.results || !data.results.length) return null;
                    var r = data.results[0];
                    openFile(r.path).then(function () {
                        if (BX.monacoInstance) {
                            BX.monacoInstance.revealLineInCenter(r.line);
                            BX.monacoInstance.setPosition({ lineNumber: r.line, column: 1 });
                        }
                    });
                    var existingModel = m.editor.getModel(m.Uri.file(r.path));
                    if (existingModel) return [{ uri: m.Uri.file(r.path), range: new m.Range(r.line, 1, r.line, 1) }];
                    return null;
                } catch (ex) { return null; }
            }
        });

        m.languages.registerReferenceProvider("*", {
            provideReferences: async function (model, position) {
                var word = model.getWordAtPosition(position);
                if (!word) return [];
                try {
                    var res = await fetch("/api/find-symbol?symbol=" + encodeURIComponent(word.word) + "&kind=all");
                    if (!res.ok) return [];
                    var data = await res.json();
                    if (!data.results || !data.results.length) return [];
                    return data.results.map(function (r) {
                        var existingModel = m.editor.getModel(m.Uri.file(r.path));
                        if (existingModel) return { uri: m.Uri.file(r.path), range: new m.Range(r.line, 1, r.line, 1) };
                        return { uri: model.uri, range: new m.Range(1, 1, 1, 1) };
                    }).filter(function (r) { return r.uri !== model.uri || r.range.startLineNumber > 1; });
                } catch (ex) { return []; }
            }
        });

        if (m.editor.registerEditorOpener) {
            m.editor.registerEditorOpener({
                openCodeEditor: function (source, resource, selectionOrPosition) {
                    var filePath = resource.path.startsWith("/") ? resource.path.slice(1) : resource.path;
                    openFile(filePath).then(function () {
                        if (BX.monacoInstance && selectionOrPosition) {
                            var line = selectionOrPosition.startLineNumber || selectionOrPosition.lineNumber || 1;
                            var col = selectionOrPosition.startColumn || selectionOrPosition.column || 1;
                            BX.monacoInstance.setPosition({ lineNumber: line, column: col });
                            BX.monacoInstance.revealLineInCenter(line);
                        }
                    });
                    return true;
                }
            });
        }

        _monacoInited = true;
    }

    // ================================================================
    // FILE OPEN / TAB MANAGEMENT
    // ================================================================

    async function openFile(path) {
        path = (path || "").replace(/\\/g, "/").trim();
        if (!path || path.endsWith("/")) return;
        await initMonaco();
        var m = await window.monacoReady;

        if (BX.activeTab && BX.monacoInstance) {
            var curInfo = openTabs.get(BX.activeTab);
            if (curInfo) curInfo.viewState = BX.monacoInstance.saveViewState();
        }

        if (openTabs.has(path)) { switchToTab(path); return; }

        try {
            var res = await fetch("/api/file?path=" + encodeURIComponent(path));
            if (!res.ok) {
                var msg = "Failed to open file";
                try { var errBody = await res.json(); if (errBody && errBody.error) msg = errBody.error; } catch (_) {}
                BX.showToast(msg);
                return;
            }
            var content = await res.text();
            var ext = path.split(".").pop();
            var lang = BX.langFromExt(ext);
            var model = m.editor.createModel(content, lang, m.Uri.file(path));

            openTabs.set(path, { model: model, viewState: null, content: content });
            createTabEl(path);
            switchToTab(path);
        } catch (e) {
            BX.showToast("Error opening file");
        }
    }

    function switchToTab(path) {
        if (!openTabs.has(path)) return;
        $editorWelcome.classList.add("hidden");

        if (BX.activeTab && BX.monacoInstance && openTabs.has(BX.activeTab)) {
            openTabs.get(BX.activeTab).viewState = BX.monacoInstance.saveViewState();
        }

        if (BX.diffEditorInstance) { BX.diffEditorInstance.dispose(); BX.diffEditorInstance = null; }

        BX.activeTab = path;
        var info = openTabs.get(path);

        if (!BX.monacoInstance) {
            BX.monacoInstance = monaco.editor.create($monacoEl, {
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
                stickyScroll: { enabled: true }
            });
            BX.monacoInstance.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, function () { saveCurrentFile(); });
        } else {
            BX.monacoInstance.setModel(info.model);
        }

        if (info.viewState) BX.monacoInstance.restoreViewState(info.viewState);
        BX.monacoInstance.focus();

        if (modifiedFiles.has(path)) {
            applyInlineDiffDecorations(path);
        } else {
            var pathNorm = (path || "").replace(/\\/g, "/");
            var g = BX.gitStatus.get(pathNorm);
            if (g === "M" || g === "A" || g === "U") applyGitInlineDiffDecorations(path);
            else clearDiffDecorations(path);
        }

        $tabBar.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
        var tabEl = $tabBar.querySelector('.tab[data-path="' + CSS.escape(path) + '"]');
        if (tabEl) tabEl.classList.add("active");

        $fileTree.querySelectorAll(".tree-item").forEach(function (t) { t.classList.remove("active"); });
        var treeItem = $fileTree.querySelector('.tree-item[data-path="' + CSS.escape(path) + '"]');
        if (treeItem) treeItem.classList.add("active");

        updateBreadcrumb(path);
    }

    // ================================================================
    // BREADCRUMB
    // ================================================================

    function updateBreadcrumb(path) {
        if (!$breadcrumb) return;
        if (!path) { $breadcrumb.classList.add("hidden"); return; }
        $breadcrumb.classList.remove("hidden");
        var parts = path.replace(/\\/g, "/").split("/");
        var html = "";
        parts.forEach(function (part, i) {
            if (i > 0) html += '<span class="bc-sep">\u203A</span>';
            var isCurrent = i === parts.length - 1;
            var partPath = parts.slice(0, i + 1).join("/");
            html += '<span class="bc-part' + (isCurrent ? " current" : "") + '" data-path="' + BX.escapeHtml(partPath) + '">' + BX.escapeHtml(part) + '</span>';
        });
        $breadcrumb.innerHTML = html;
        $breadcrumb.querySelectorAll(".bc-part").forEach(function (el) {
            el.addEventListener("click", function () { var p = el.dataset.path; if (openTabs.has(p)) switchToTab(p); });
        });
    }

    // ================================================================
    // TAB CREATE / CLOSE
    // ================================================================

    function createTabEl(path) {
        var tab = document.createElement("div");
        tab.className = "tab active";
        tab.dataset.path = path;
        var pathNorm = (path || "").replace(/\\/g, "/");
        var g = BX.gitStatus.get(pathNorm);
        if (modifiedFiles.has(path) || modifiedFiles.has(pathNorm) || g === "M" || g === "A" || g === "U") tab.classList.add("modified");
        tab.innerHTML = '<span class="tab-name">' + BX.escapeHtml(BX.basename(path)) + '</span><span class="tab-close">\u00D7</span>';
        tab.addEventListener("click", function (e) {
            if (e.target.classList.contains("tab-close")) { closeTab(path); return; }
            switchToTab(path);
        });
        $tabBar.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
        $tabBar.appendChild(tab);
    }

    function closeTab(path) {
        var info = openTabs.get(path);
        if (info) { info.model.dispose(); openTabs.delete(path); }
        diffDecorationIds.delete(path);
        var tabEl = $tabBar.querySelector('.tab[data-path="' + CSS.escape(path) + '"]');
        if (tabEl) tabEl.remove();
        if (BX.activeTab === path) {
            BX.activeTab = null;
            var remaining = Array.from(openTabs.keys());
            if (remaining.length > 0) { switchToTab(remaining[remaining.length - 1]); }
            else { if (BX.monacoInstance) { BX.monacoInstance.setModel(null); } $editorWelcome.classList.remove("hidden"); }
        }
    }

    // ================================================================
    // FILE SAVE
    // ================================================================

    async function saveCurrentFile() {
        if (!BX.activeTab || !BX.monacoInstance) return;
        var content = BX.monacoInstance.getValue();
        try {
            var res = await fetch("/api/file", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: BX.activeTab, content: content }) });
            var data = await res.json();
            if (data.ok) BX.showToast("Saved " + BX.basename(BX.activeTab));
            else BX.showToast("Save failed: " + (data.error || "unknown"));
        } catch (ex) { BX.showToast("Save error"); }
    }

    // ================================================================
    // DIFF VIEW
    // ================================================================

    async function openDiffForFile(path) {
        await initMonaco();
        var m = await window.monacoReady;
        try {
            var res = await fetch("/api/file-diff?path=" + encodeURIComponent(path));
            if (!res.ok) { BX.showToast("No diff available"); return; }
            var data = await res.json();
            $editorWelcome.classList.add("hidden");

            if (BX.activeTab && BX.monacoInstance && openTabs.has(BX.activeTab)) {
                openTabs.get(BX.activeTab).viewState = BX.monacoInstance.saveViewState();
            }

            if (BX.monacoInstance) { BX.monacoInstance.dispose(); BX.monacoInstance = null; }
            if (BX.diffEditorInstance) { BX.diffEditorInstance.dispose(); }

            var originalModel = m.editor.createModel(data.original || "", BX.langFromExt(path.split(".").pop()), m.Uri.parse("original:///" + path));
            var modifiedModel = m.editor.createModel(data.current || "", BX.langFromExt(path.split(".").pop()), m.Uri.parse("modified:///" + path));

            BX.diffEditorInstance = m.editor.createDiffEditor($monacoEl, {
                theme: "bedrock-dark",
                fontSize: 13,
                fontFamily: "'SF Mono', 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace",
                automaticLayout: true,
                readOnly: true,
                renderSideBySide: true,
                scrollBeyondLastLine: false,
                padding: { top: 8 }
            });
            BX.diffEditorInstance.setModel({ original: originalModel, modified: modifiedModel });

            BX.activeTab = path;
            $tabBar.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active", "diff-mode"); });
            var tabEl = $tabBar.querySelector('.tab[data-path="' + CSS.escape(path) + '"]');
            if (!tabEl) {
                openTabs.set(path, { model: modifiedModel, viewState: null, content: data.current });
                createTabEl(path);
                tabEl = $tabBar.querySelector('.tab[data-path="' + CSS.escape(path) + '"]');
            }
            if (tabEl) tabEl.classList.add("active", "diff-mode");
        } catch (e) { BX.showToast("Error loading diff"); }
    }

    // ================================================================
    // RELOAD FILE IN EDITOR
    // ================================================================

    async function reloadFileInEditor(path) {
        if (!openTabs.has(path)) return;
        try {
            var res = await fetch("/api/file?path=" + encodeURIComponent(path));
            if (!res.ok) return;
            var content = await res.text();
            var info = openTabs.get(path);
            if (info && info.model) { info.model.setValue(content); info.content = content; }
            if (BX.activeTab === path) await applyInlineDiffDecorations(path);
        } catch (ex) {}
    }

    // ================================================================
    // INLINE DIFF DECORATIONS
    // ================================================================

    function computeLineDiff(originalText, currentText) {
        var origLines = (originalText || "").split("\n");
        var currLines = (currentText || "").split("\n");
        var result = { added: [], modified: [], deleted: [] };
        var n = origLines.length, m = currLines.length;

        if (n * m > 25000000) {
            var maxLen = Math.max(n, m);
            for (var i = 0; i < maxLen; i++) {
                if (i >= n) result.added.push({ start: i + 1, end: i + 1 });
                else if (i >= m) result.deleted.push(m > 0 ? m : 1);
                else if (origLines[i] !== currLines[i]) result.modified.push({ start: i + 1, end: i + 1 });
            }
        } else {
            var dp = new Array(n + 1);
            for (var di = 0; di <= n; di++) dp[di] = new Uint16Array(m + 1);
            for (var ri = 1; ri <= n; ri++) {
                for (var ci = 1; ci <= m; ci++) {
                    if (origLines[ri - 1] === currLines[ci - 1]) dp[ri][ci] = dp[ri - 1][ci - 1] + 1;
                    else dp[ri][ci] = Math.max(dp[ri - 1][ci], dp[ri][ci - 1]);
                }
            }
            var origMatched = new Set(), currMatched = new Set();
            var bi = n, bj = m;
            while (bi > 0 && bj > 0) {
                if (origLines[bi - 1] === currLines[bj - 1]) { origMatched.add(bi - 1); currMatched.add(bj - 1); bi--; bj--; }
                else if (dp[bi - 1][bj] >= dp[bi][bj - 1]) bi--;
                else bj--;
            }
            var deletedOrigIndices = [];
            for (var k = 0; k < n; k++) { if (!origMatched.has(k)) deletedOrigIndices.push(k); }
            var addedCurrIndices = [];
            for (var k2 = 0; k2 < m; k2++) { if (!currMatched.has(k2)) addedCurrIndices.push(k2); }
            var pairedAdds = new Set();
            for (var di2 = 0; di2 < deletedOrigIndices.length; di2++) {
                var dIdx = deletedOrigIndices[di2];
                for (var ai = 0; ai < addedCurrIndices.length; ai++) {
                    var aIdx = addedCurrIndices[ai];
                    if (!pairedAdds.has(aIdx) && Math.abs(dIdx - aIdx) <= 3) { result.modified.push({ start: aIdx + 1, end: aIdx + 1 }); pairedAdds.add(aIdx); break; }
                }
            }
            for (var ai2 = 0; ai2 < addedCurrIndices.length; ai2++) {
                if (!pairedAdds.has(addedCurrIndices[ai2])) result.added.push({ start: addedCurrIndices[ai2] + 1, end: addedCurrIndices[ai2] + 1 });
            }
            var deletedUnpaired = deletedOrigIndices.filter(function (dIdx) {
                var iter = pairedAdds.values();
                var cur = iter.next();
                while (!cur.done) { if (Math.abs(dIdx - cur.value) <= 3) return false; cur = iter.next(); }
                return true;
            });
            for (var du = 0; du < deletedUnpaired.length; du++) {
                var nearLine = Math.min(deletedUnpaired[du] + 1, m) || 1;
                result.deleted.push(nearLine);
            }
        }

        function mergeRanges(ranges) {
            if (ranges.length === 0) return [];
            ranges.sort(function (a, b) { return a.start - b.start; });
            var merged = [ranges[0]];
            for (var mi = 1; mi < ranges.length; mi++) {
                var last = merged[merged.length - 1];
                if (ranges[mi].start <= last.end + 1) last.end = Math.max(last.end, ranges[mi].end);
                else merged.push(ranges[mi]);
            }
            return merged;
        }
        result.added = mergeRanges(result.added);
        result.modified = mergeRanges(result.modified);
        result.deleted = Array.from(new Set(result.deleted)).sort(function (a, b) { return a - b; });
        return result;
    }

    function _makeDiffDecorations(diff) {
        var decorations = [];
        for (var ai = 0; ai < diff.added.length; ai++) {
            var r = diff.added[ai];
            decorations.push({ range: new monaco.Range(r.start, 1, r.end, 1), options: { isWholeLine: true, linesDecorationsClassName: "diff-gutter-added", className: "diff-line-added-bg", overviewRuler: { color: "#4ec9b0", position: monaco.editor.OverviewRulerLane.Left } } });
        }
        for (var mi = 0; mi < diff.modified.length; mi++) {
            var rm = diff.modified[mi];
            decorations.push({ range: new monaco.Range(rm.start, 1, rm.end, 1), options: { isWholeLine: true, linesDecorationsClassName: "diff-gutter-modified", className: "diff-line-modified-bg", overviewRuler: { color: "#6c9fff", position: monaco.editor.OverviewRulerLane.Left } } });
        }
        for (var di = 0; di < diff.deleted.length; di++) {
            var lineNum = diff.deleted[di];
            decorations.push({ range: new monaco.Range(lineNum, 1, lineNum, 1), options: { isWholeLine: false, linesDecorationsClassName: "diff-gutter-deleted", overviewRuler: { color: "#f44747", position: monaco.editor.OverviewRulerLane.Left } } });
        }
        return decorations;
    }

    async function applyInlineDiffDecorations(path) {
        if (!BX.monacoInstance || !openTabs.has(path)) return;
        if (!modifiedFiles.has(path)) return;
        try {
            var res = await fetch("/api/file-diff?path=" + encodeURIComponent(path));
            if (!res.ok) return;
            var data = await res.json();
            var info = openTabs.get(path);
            if (!info || !info.model) return;
            var currentText = info.model.getValue();
            var diff = computeLineDiff(data.original || "", currentText);
            var decorations = _makeDiffDecorations(diff);
            var oldIds = diffDecorationIds.get(path) || [];
            var newIds = BX.monacoInstance.deltaDecorations(oldIds, decorations);
            diffDecorationIds.set(path, newIds);
        } catch (ex) {}
    }

    async function applyGitInlineDiffDecorations(path) {
        if (!BX.monacoInstance || !openTabs.has(path) || typeof monaco === "undefined") return;
        try {
            var pathForApi = (path || "").replace(/\\/g, "/");
            var res = await fetch("/api/git-file-diff?path=" + encodeURIComponent(pathForApi));
            if (!res.ok) return;
            var data = await res.json();
            var info = openTabs.get(path);
            if (!info || !info.model) return;
            var currentText = info.model.getValue();
            var diff = computeLineDiff(data.original || "", currentText);
            var decorations = _makeDiffDecorations(diff);
            if (BX.activeTab !== path) return;
            var oldIds = diffDecorationIds.get(path) || [];
            var newIds = BX.monacoInstance.deltaDecorations(oldIds, decorations);
            diffDecorationIds.set(path, newIds);
        } catch (e) { console.warn("Git inline diff decorations failed:", e); }
    }

    function clearDiffDecorations(path) {
        if (path) {
            var oldIds = diffDecorationIds.get(path) || [];
            if (oldIds.length && BX.monacoInstance && BX.activeTab === path) BX.monacoInstance.deltaDecorations(oldIds, []);
            diffDecorationIds.delete(path);
        }
    }

    async function reloadAllModifiedFiles() {
        var keys = Array.from(openTabs.keys());
        for (var i = 0; i < keys.length; i++) {
            var path = keys[i];
            try {
                var res = await fetch("/api/file?path=" + encodeURIComponent(path));
                if (!res.ok) continue;
                var content = await res.text();
                var info = openTabs.get(path);
                if (info && info.model) { info.model.setValue(content); info.content = content; }
            } catch (ex) {}
        }
    }

    function clearAllDiffDecorations() {
        diffDecorationIds.forEach(function (ids, path) {
            if (BX.monacoInstance && BX.activeTab === path && ids.length) BX.monacoInstance.deltaDecorations(ids, []);
        });
        diffDecorationIds.clear();
    }

    // ================================================================
    // RESIZE HANDLES
    // ================================================================

    function setupResize(handleId, leftEl, rightEl, direction) {
        var handle = document.getElementById(handleId);
        if (!handle) return;
        var startX, startLeftW, startRightW;

        handle.addEventListener("mousedown", function (e) {
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
            var dx = e.clientX - startX;
            if (direction === "left" && leftEl) leftEl.style.width = Math.max(120, startLeftW + dx) + "px";
            else if (direction === "right" && rightEl) rightEl.style.width = Math.max(280, startRightW - dx) + "px";
            if (BX.monacoInstance) BX.monacoInstance.layout();
            if (BX.diffEditorInstance) BX.diffEditorInstance.layout();
        }
        function onUp() {
            handle.classList.remove("dragging");
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            if (BX.monacoInstance) BX.monacoInstance.layout();
            if (BX.diffEditorInstance) BX.diffEditorInstance.layout();
        }
    }

    setupResize("resize-left", document.getElementById("file-explorer"), null, "left");
    setupResize("resize-right", null, document.getElementById("chat-panel"), "right");

    // Source control vertical resize
    var SOURCE_CONTROL_MIN_HEIGHT = 80;
    var SOURCE_CONTROL_MAX_HEIGHT = 0.6 * window.innerHeight;
    var $resizeExplorerSc = document.getElementById("resize-explorer-sc");
    var $sourceControlPanel = document.getElementById("source-control-panel");
    var $fileExplorer = document.getElementById("file-explorer");
    if ($resizeExplorerSc && $sourceControlPanel && $fileExplorer) {
        $resizeExplorerSc.addEventListener("mousedown", function (e) {
            e.preventDefault();
            var startY = e.clientY;
            var startHeight = $sourceControlPanel.offsetHeight;
            $resizeExplorerSc.classList.add("dragging");
            document.body.style.cursor = "row-resize";
            document.body.style.userSelect = "none";
            function onMove(ev) {
                var dy = ev.clientY - startY;
                var newHeight = Math.max(SOURCE_CONTROL_MIN_HEIGHT, Math.min(SOURCE_CONTROL_MAX_HEIGHT, startHeight - dy));
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

    // Save button handler
    var $saveFileBtn = BX.$saveFileBtn;
    if ($saveFileBtn) {
        $saveFileBtn.addEventListener("click", saveCurrentFile);
    }

    // ================================================================
    // EXPORTS
    // ================================================================

    BX.initMonaco = initMonaco;
    BX.openFile = openFile;
    BX.switchToTab = switchToTab;
    BX.updateBreadcrumb = updateBreadcrumb;
    BX.createTabEl = createTabEl;
    BX.closeTab = closeTab;
    BX.saveCurrentFile = saveCurrentFile;
    BX.openDiffForFile = openDiffForFile;
    BX.reloadFileInEditor = reloadFileInEditor;
    BX.reloadAllModifiedFiles = reloadAllModifiedFiles;
    BX.computeLineDiff = computeLineDiff;
    BX.applyInlineDiffDecorations = applyInlineDiffDecorations;
    BX.applyGitInlineDiffDecorations = applyGitInlineDiffDecorations;
    BX.clearDiffDecorations = clearDiffDecorations;
    BX.clearAllDiffDecorations = clearAllDiffDecorations;
    BX.setupResize = setupResize;
    BX.diffDecorationIds = diffDecorationIds;

})(window.BX);
