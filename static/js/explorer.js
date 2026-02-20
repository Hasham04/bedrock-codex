/* ============================================================
   Bedrock Codex — explorer.js
   File tree, source control, context menu, inline rename/create,
   file type icons, git status, tree refresh, fuzzy match, file
   filter, mention popup, command palette, polling, dropdowns
   ============================================================ */
(function (BX) {
    "use strict";

    // DOM ref aliases (immutable — safe to alias)
    var $fileTree = BX.$fileTree;
    var $fileFilter = BX.$fileFilter;
    var $refreshTree = BX.$refreshTree;
    var $tabBar = BX.$tabBar;
    var $input = BX.$input;
    var $sourceControlList = BX.$sourceControlList;
    var $sourceControlRefreshBtn = BX.$sourceControlRefreshBtn;
    var $modifiedFilesBar = BX.$modifiedFilesBar;
    var $modifiedFilesToggle = BX.$modifiedFilesToggle;
    var $modifiedFilesList = BX.$modifiedFilesList;
    var $modifiedFilesDropdown = BX.$modifiedFilesDropdown;
    var $stickyTodoBar = BX.$stickyTodoBar;
    var $stickyTodoToggle = BX.$stickyTodoToggle;
    var $stickyTodoList = BX.$stickyTodoList;
    var $stickyTodoDropdown = BX.$stickyTodoDropdown;
    var $stickyTodoAddInput = BX.$stickyTodoAddInput;
    var $stickyTodoAddBtn = BX.$stickyTodoAddBtn;
    var $chatComposerStats = BX.$chatComposerStats;
    var $chatComposerStatsToggle = BX.$chatComposerStatsToggle;
    var $chatComposerFilesDropup = BX.$chatComposerFilesDropup;
    var $chatMenuBtn = BX.$chatMenuBtn;
    var $chatMenuDropdown = BX.$chatMenuDropdown;

    // Module-local DOM refs
    var $ctxMenu = document.getElementById("explorer-ctx-menu");
    var $mentionPopup = document.getElementById("mention-popup");
    var $cp = document.getElementById("command-palette");
    var $cpInput = document.getElementById("command-palette-input");
    var $cpResults = document.getElementById("command-palette-results");
    var $stickyTodoAddRow = document.getElementById("sticky-todo-add-row");
    var $explorerBody = document.querySelector(".explorer-body");

    // Reference-type state aliases (safe — same underlying object)
    var modifiedFiles = BX.modifiedFiles;

    // Module-private state
    var treeState = {};
    var _ctxTarget = null;
    var _gitStatusDebounce = null;
    var _refreshTreeTimer = null;
    var _refreshTreePromise = null;
    var _fileFilterTimeout = null;
    var _allFilePaths = null;
    var _mentionActive = false;
    var _mentionStart = -1;
    var _mentionSelectedIdx = 0;
    var _mentionItems = [];
    var _cpMode = "command";
    var _cpSelectedIdx = 0;
    var _cpItems = [];
    var MODIFIED_FILES_POLL_MS = 10000; // Sync with git status to reduce API calls
    var modifiedFilesPollTimer = null;
    var GIT_STATUS_POLL_MS = 10000;
    var gitStatusPollTimer = null;

    // ================================================================
    // SOURCE CONTROL
    // ================================================================

    function renderSourceControl() {
        if (!$sourceControlList) return;
        var entries = [].concat(Array.from(BX.gitStatus.entries())).sort(function (a, b) { return a[0].localeCompare(b[0]); });
        if (entries.length === 0) {
            $sourceControlList.innerHTML = '<div class="source-control-empty">No changes</div>';
            return;
        }
        var html = "";
        for (var idx = 0; idx < entries.length; idx++) {
            var path = entries[idx][0];
            var status = entries[idx][1];
            if (path.endsWith("/")) continue;
            var statusCls = status === "M" ? "modified" : status === "A" ? "added" : status === "D" ? "deleted" : "untracked";
            var label = status === "M" ? "M" : status === "A" ? "A" : status === "D" ? "D" : "U";
            html += '<div class="source-control-item" data-path="' + BX.escapeHtml(path) + '" data-status="' + statusCls + '">'
                + '<span class="sc-status ' + statusCls + '">' + BX.escapeHtml(label) + '</span>'
                + '<span class="sc-path">' + BX.escapeHtml(path) + '</span>'
                + '</div>';
        }
        $sourceControlList.innerHTML = html;
        $sourceControlList.querySelectorAll(".source-control-item").forEach(function (el) {
            el.addEventListener("click", function () {
                var p = (el.dataset.path || "").replace(/\\/g, "/");
                if (p.endsWith("/")) return;
                BX.openFile(p);
            });
        });
    }

    // ================================================================
    // GIT STATUS
    // ================================================================

    async function fetchGitStatus() {
        try {
            var res = await fetch("/api/git-status?t=" + Date.now());
            if (!res.ok) { BX.gitStatus = new Map(); return; }
            var data = await res.json();
            var status = data.status && typeof data.status === "object" ? data.status : {};
            BX.gitStatus = new Map(Object.entries(status));
            if (data.error && BX.gitStatus.size === 0) console.warn("Git status unavailable:", data.error);
            renderSourceControl();
            syncFileStatusIndicators();
        } catch (e) {
            BX.gitStatus = new Map();
            console.warn("Git status fetch failed:", e);
            renderSourceControl();
            syncFileStatusIndicators();
        }
    }

    // ================================================================
    // FILE TREE
    // ================================================================

    async function loadTree(parentPath, parentEl) {
        parentPath = parentPath || "";
        var target = parentEl || $fileTree;
        if (!parentEl) await fetchGitStatus();
        try {
            var res = await fetch("/api/files?path=" + encodeURIComponent(parentPath));
            var items = await res.json();
            if (!parentEl) target.innerHTML = "";

            items.forEach(function (item) {
                var el = document.createElement("div");
                var depth = parentPath ? parentPath.split("/").length : 0;

                if (item.type === "directory") {
                    var isOpen = treeState[item.path] || false;
                    el.innerHTML =
                        '<div class="tree-item" data-path="' + BX.escapeHtml(item.path) + '" data-type="dir" style="padding-left:' + (8 + depth * 16) + 'px">'
                        + '<span class="tree-chevron ' + (isOpen ? "open" : "") + '">\u25B6</span>'
                        + '<span class="tree-icon">\uD83D\uDCC1</span>'
                        + '<span class="tree-file-name">' + BX.escapeHtml(item.name) + '</span>'
                        + '</div>'
                        + '<div class="tree-children"' + (isOpen ? "" : ' style="display:none"') + '></div>';
                    var header = el.querySelector(".tree-item");
                    var children = el.querySelector(".tree-children");
                    var chevron = el.querySelector(".tree-chevron");

                    header.addEventListener("click", async function () {
                        var open = children.style.display !== "none";
                        children.style.display = open ? "none" : "";
                        chevron.classList.toggle("open", !open);
                        treeState[item.path] = !open;
                        if (!open && children.children.length === 0) {
                            await loadTree(item.path, children);
                        }
                    });

                    if (isOpen) loadTree(item.path, children);
                } else {
                    var icon = fileIcon(item.ext);
                    var pathNorm = (item.path || "").replace(/\\/g, "/");
                    var g = BX.gitStatus.get(pathNorm);
                    var agentMod = modifiedFiles.has(item.path) || modifiedFiles.has(pathNorm);
                    var statusCls = agentMod ? "modified" : (g === "M" ? "modified" : g === "A" ? "added" : g === "D" ? "deleted" : g === "U" ? "untracked" : "");
                    el.innerHTML =
                        '<div class="tree-item ' + statusCls + '" data-path="' + BX.escapeHtml(item.path) + '" data-type="file" style="padding-left:' + (8 + depth * 16 + 16) + 'px">'
                        + '<span class="tree-icon">' + icon + '</span>'
                        + '<span class="tree-file-name">' + BX.escapeHtml(item.name) + '</span>'
                        + '</div>';
                    el.querySelector(".tree-item").addEventListener("click", function () { BX.openFile(item.path); });
                }
                target.appendChild(el);
            });
        } catch (e) {
            target.innerHTML = '<div class="info-msg" style="padding:10px">Failed to load files</div>';
        }
    }

    // ================================================================
    // CONTEXT MENU
    // ================================================================

    function _showCtxMenu(x, y, target) {
        if (!$ctxMenu) return;
        _ctxTarget = target;
        var hasPath = target && target.path;
        $ctxMenu.querySelector('[data-action="rename"]').style.display = hasPath ? "" : "none";
        $ctxMenu.querySelector('[data-action="delete"]').style.display = hasPath ? "" : "none";
        $ctxMenu.querySelector('[data-action="copy-path"]').style.display = hasPath ? "" : "none";
        $ctxMenu.querySelector('[data-action="copy-relative"]').style.display = hasPath ? "" : "none";
        var seps = $ctxMenu.querySelectorAll(".ctx-menu-sep");
        if (seps[0]) seps[0].style.display = hasPath ? "" : "none";
        if (seps[1]) seps[1].style.display = hasPath ? "" : "none";
        $ctxMenu.classList.remove("hidden");
        var rect = $ctxMenu.getBoundingClientRect();
        var mx = Math.min(x, window.innerWidth - rect.width - 8);
        var my = Math.min(y, window.innerHeight - rect.height - 8);
        $ctxMenu.style.left = mx + "px";
        $ctxMenu.style.top = my + "px";
    }

    function _hideCtxMenu() {
        if ($ctxMenu) $ctxMenu.classList.add("hidden");
        _ctxTarget = null;
    }

    document.addEventListener("click", _hideCtxMenu);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") _hideCtxMenu(); });

    $fileTree.addEventListener("contextmenu", function (e) {
        e.preventDefault();
        var treeItem = e.target.closest(".tree-item");
        if (treeItem) {
            var path = treeItem.dataset.path || "";
            var type = treeItem.dataset.type === "dir" ? "dir" : "file";
            _showCtxMenu(e.clientX, e.clientY, { path: path, type: type });
        } else {
            _showCtxMenu(e.clientX, e.clientY, { path: null, type: null });
        }
    });

    if ($explorerBody) {
        $explorerBody.addEventListener("contextmenu", function (e) {
            if (e.target === $explorerBody || e.target === $fileTree) {
                e.preventDefault();
                _showCtxMenu(e.clientX, e.clientY, { path: null, type: null });
            }
        });
    }

    // ================================================================
    // INLINE RENAME / CREATE
    // ================================================================

    function _startInlineRename(treeItem, currentName, onCommit) {
        var nameEl = treeItem.querySelector(".tree-file-name");
        if (!nameEl) return;
        var original = nameEl.textContent;
        var input = document.createElement("input");
        input.type = "text";
        input.className = "tree-rename-input";
        input.value = currentName;
        nameEl.textContent = "";
        nameEl.appendChild(input);
        input.focus();
        var dotIdx = currentName.lastIndexOf(".");
        input.setSelectionRange(0, dotIdx > 0 ? dotIdx : currentName.length);

        var committed = false;
        function commit() {
            if (committed) return;
            committed = true;
            var newName = input.value.trim();
            if (input.parentNode) input.remove();
            nameEl.textContent = original;
            if (newName && newName !== currentName) onCommit(newName);
        }
        input.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); commit(); }
            if (e.key === "Escape") { committed = true; if (input.parentNode) input.remove(); nameEl.textContent = original; }
            e.stopPropagation();
        });
        input.addEventListener("blur", commit);
        input.addEventListener("click", function (e) { e.stopPropagation(); });
    }

    function _startInlineCreate(parentPath, isFolder) {
        var container = $fileTree;
        if (parentPath) {
            var dirItem = $fileTree.querySelector('.tree-item[data-path="' + CSS.escape(parentPath) + '"][data-type="dir"]');
            if (dirItem) {
                var wrapper = dirItem.parentElement;
                var children = wrapper ? wrapper.querySelector(".tree-children") : null;
                if (children) {
                    if (children.style.display === "none") {
                        children.style.display = "";
                        var chevron = wrapper.querySelector(".tree-chevron");
                        if (chevron) chevron.classList.add("open");
                        treeState[parentPath] = true;
                    }
                    container = children;
                }
            }
        }
        var depth = parentPath ? parentPath.split("/").length : 0;
        var row = document.createElement("div");
        row.innerHTML =
            '<div class="tree-item" style="padding-left:' + (8 + depth * 16 + (isFolder ? 0 : 16)) + 'px">'
            + '<span class="tree-icon">' + (isFolder ? "\uD83D\uDCC1" : "\uD83D\uDCC4") + '</span>'
            + '<span class="tree-file-name"><input type="text" class="tree-rename-input" placeholder="' + (isFolder ? "folder name" : "filename") + '" /></span>'
            + '</div>';
        var input = row.querySelector("input");
        container.insertBefore(row, container.firstChild);
        input.focus();

        var committed = false;
        function commit() {
            if (committed) return;
            committed = true;
            var name = input.value.trim();
            row.remove();
            if (!name) return;
            var fullPath = parentPath ? parentPath + "/" + name : name;
            if (isFolder) {
                fetch("/api/file/mkdir", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: fullPath }) }).then(function () { refreshTree(); });
            } else {
                fetch("/api/file", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: fullPath, content: "" }) }).then(function () { refreshTree(); BX.openFile(fullPath); });
            }
        }
        input.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); commit(); }
            if (e.key === "Escape") { committed = true; row.remove(); }
            e.stopPropagation();
        });
        input.addEventListener("blur", commit);
        input.addEventListener("click", function (e) { e.stopPropagation(); });
    }

    // ================================================================
    // CONTEXT MENU ACTION HANDLER
    // ================================================================

    if ($ctxMenu) {
        $ctxMenu.addEventListener("click", async function (e) {
            var btn = e.target.closest(".ctx-menu-item");
            if (!btn) return;
            e.stopPropagation();
            var action = btn.dataset.action;
            var target = _ctxTarget;
            _hideCtxMenu();
            if (!action) return;

            var parentDir = target && target.type === "dir" ? target.path
                : target && target.path ? target.path.replace(/\/[^/]+$/, "") || ""
                : "";

            switch (action) {
                case "new-file": _startInlineCreate(parentDir, false); break;
                case "new-folder": _startInlineCreate(parentDir, true); break;
                case "rename": {
                    if (!target || !target.path) break;
                    var treeItem = $fileTree.querySelector('.tree-item[data-path="' + CSS.escape(target.path) + '"]');
                    if (!treeItem) break;
                    var oldName = target.path.split("/").pop();
                    _startInlineRename(treeItem, oldName, async function (newName) {
                        var dir = target.path.replace(/\/[^/]+$/, "");
                        var newPath = dir ? dir + "/" + newName : newName;
                        try {
                            var res = await fetch("/api/file/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ old_path: target.path, new_path: newPath }) });
                            if (res.ok) {
                                refreshTree();
                                var tab = $tabBar.querySelector('.tab[data-path="' + CSS.escape(target.path) + '"]');
                                if (tab) { tab.dataset.path = newPath; var nameEl = tab.querySelector(".tab-name"); if (nameEl) nameEl.textContent = newName; }
                            }
                        } catch (ex) {}
                    });
                    break;
                }
                case "delete": {
                    if (!target || !target.path) break;
                    var name = target.path.split("/").pop();
                    var kind = target.type === "dir" ? "folder" : "file";
                    if (!confirm('Delete ' + kind + ' "' + name + '"?')) break;
                    try {
                        var res = await fetch("/api/file/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: target.path }) });
                        if (res.ok) {
                            refreshTree();
                            var tab = $tabBar.querySelector('.tab[data-path="' + CSS.escape(target.path) + '"]');
                            if (tab) { var closeBtn = tab.querySelector(".tab-close"); if (closeBtn) closeBtn.click(); }
                        }
                    } catch (ex) {}
                    break;
                }
                case "copy-path":
                case "copy-relative":
                    if (target && target.path) try { await navigator.clipboard.writeText(target.path); } catch (ex) {}
                    break;
                case "refresh": refreshTree(); break;
            }
        });
    }

    // ================================================================
    // FILE TYPE ICONS
    // ================================================================

    var _ftColors = {
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
        tf:"#5c4ee5",hcl:"#5c4ee5"
    };
    var _ftLabels = {
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
        lock:"Lk",log:"\u25B6",
        txt:"Tx",csv:"\u2261",
        png:"\u25A3",jpg:"\u25A3",jpeg:"\u25A3",gif:"\u25A3",webp:"\u25A3",ico:"\u25A3"
    };

    function fileTypeIcon(pathOrExt, size) {
        var s = size || 14;
        var ext = pathOrExt;
        if (pathOrExt && pathOrExt.includes(".")) ext = pathOrExt.split(".").pop().toLowerCase();
        if (ext) ext = ext.toLowerCase();
        var bname = pathOrExt ? pathOrExt.split("/").pop().toLowerCase() : "";
        if (bname === "dockerfile" || bname.startsWith("dockerfile.")) ext = "dockerfile";
        else if (bname === ".env" || bname.startsWith(".env.")) ext = "env";
        else if (bname === ".gitignore") ext = "gitignore";

        var color = _ftColors[ext] || "#8b949e";
        var label = _ftLabels[ext] || (ext ? ext.slice(0, 2).toUpperCase() : "F");
        return '<span class="ft-icon" style="width:' + s + 'px;height:' + s + 'px;background:' + color + '20;color:' + color + ';border:1px solid ' + color + '40;font-size:' + Math.max(s - 5, 7) + 'px">' + label + '</span>';
    }

    function fileIcon(ext) { return fileTypeIcon(ext, 15); }

    // ================================================================
    // GIT STATUS DEBOUNCE / FILE MODIFIED
    // ================================================================

    function _debouncedGitStatus() {
        if (_gitStatusDebounce) clearTimeout(_gitStatusDebounce);
        _gitStatusDebounce = setTimeout(function () {
            _gitStatusDebounce = null;
            fetchGitStatus();
        }, 1500);
    }

    function markFileModified(path) {
        modifiedFiles.add(path);
        document.querySelectorAll('.tree-item[data-path="' + CSS.escape(path) + '"]').forEach(function (el) { el.classList.add("modified"); });
        var tab = $tabBar.querySelector('.tab[data-path="' + CSS.escape(path) + '"]');
        if (tab) tab.classList.add("modified");
        _debouncedGitStatus();
    }

    function syncFileStatusIndicators() {
        document.querySelectorAll('#file-tree .tree-item[data-type="file"]').forEach(function (el) {
            var path = el.dataset.path || "";
            var pathNorm = path.replace(/\\/g, "/");
            var g = BX.gitStatus.get(pathNorm);
            var agentMod = modifiedFiles.has(path) || modifiedFiles.has(pathNorm);
            el.classList.remove("modified", "added", "deleted", "untracked");
            if (agentMod) el.classList.add("modified");
            else if (g === "M") el.classList.add("modified");
            else if (g === "A") el.classList.add("added");
            else if (g === "D") el.classList.add("deleted");
            else if (g === "U") el.classList.add("untracked");
        });
        document.querySelectorAll('#tab-bar .tab').forEach(function (tab) {
            var path = tab.dataset.path || "";
            var pathNorm = path.replace(/\\/g, "/");
            var g = BX.gitStatus.get(pathNorm);
            var agentMod = modifiedFiles.has(path) || modifiedFiles.has(pathNorm);
            if (agentMod || g === "M" || g === "A" || g === "U") tab.classList.add("modified");
            else tab.classList.remove("modified");
        });
    }

    // ================================================================
    // TREE REFRESH
    // ================================================================

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
            _refreshTreePromise = _doRefreshTree().finally(function () { _refreshTreePromise = null; });
            return _refreshTreePromise;
        }
        return new Promise(function (resolve) {
            _refreshTreeTimer = setTimeout(function () {
                _refreshTreeTimer = null;
                _refreshTreePromise = _doRefreshTree().finally(function () { _refreshTreePromise = null; });
                _refreshTreePromise.then(resolve);
            }, 300);
        });
    }

    // ================================================================
    // FUZZY MATCH / FILE FILTER
    // ================================================================

    function fuzzyMatch(query, text) {
        query = query.toLowerCase(); text = text.toLowerCase();
        var qi = 0;
        for (var ti = 0; ti < text.length && qi < query.length; ti++) { if (text[ti] === query[qi]) qi++; }
        return qi === query.length;
    }

    async function fetchAllFiles() {
        try {
            var res = await fetch("/api/files?recursive=true");
            if (res.ok) { _allFilePaths = await res.json(); return _allFilePaths; }
        } catch (ex) {}
        return null;
    }

    function renderFilteredFiles(matches) {
        $fileTree.innerHTML = "";
        if (!matches || matches.length === 0) {
            $fileTree.innerHTML = '<div class="info-msg" style="padding:10px;opacity:0.6">No files match filter</div>';
            return;
        }
        matches.slice(0, 100).forEach(function (item) {
            var el = document.createElement("div");
            var icon = fileIcon(item.ext || item.name.split(".").pop());
            var pathNorm = (item.path || "").replace(/\\/g, "/");
            var g = BX.gitStatus.get(pathNorm);
            var agentMod = modifiedFiles.has(item.path) || modifiedFiles.has(pathNorm);
            var statusCls = agentMod ? "modified" : (g === "M" ? "modified" : g === "A" ? "added" : g === "D" ? "deleted" : g === "U" ? "untracked" : "");
            el.innerHTML =
                '<div class="tree-item ' + statusCls + '" data-path="' + BX.escapeHtml(item.path) + '" data-type="file" style="padding-left:12px">'
                + '<span class="tree-icon">' + icon + '</span>'
                + '<span class="tree-file-name">' + BX.escapeHtml(item.name) + '</span>'
                + '<span class="tree-file-path-hint" style="margin-left:6px;opacity:0.45;font-size:11px">' + BX.escapeHtml(item.dir || "") + '</span>'
                + '</div>';
            el.querySelector(".tree-item").addEventListener("click", function () { BX.openFile(item.path); });
            $fileTree.appendChild(el);
        });
        if (matches.length > 100) {
            $fileTree.insertAdjacentHTML("beforeend", '<div class="info-msg" style="padding:8px;opacity:0.5">' + (matches.length - 100) + ' more...</div>');
        }
    }

    if ($fileFilter) {
        $fileFilter.addEventListener("input", function () {
            clearTimeout(_fileFilterTimeout);
            var q = $fileFilter.value.trim();
            if (!q) { $fileTree.innerHTML = ""; loadTree(); return; }
            _fileFilterTimeout = setTimeout(async function () {
                if (!_allFilePaths) await fetchAllFiles();
                if (!_allFilePaths) return;
                var matches = _allFilePaths.filter(function (f) { return fuzzyMatch(q, f.name) || fuzzyMatch(q, f.path); });
                renderFilteredFiles(matches);
            }, 150);
        });
        $fileFilter.addEventListener("keydown", function (e) {
            if (e.key === "Escape") { $fileFilter.value = ""; $fileTree.innerHTML = ""; loadTree(); }
        });
    }

    // ================================================================
    // @ MENTION POPUP
    // ================================================================

    var SPECIAL_MENTIONS = [
        { label: "codebase", desc: "Inject project tree + entry points", type: "special", icon: "\uD83D\uDDC2" },
        { label: "git", desc: "Inject git diff output", type: "special", icon: "\uD83D\uDCCB" },
        { label: "terminal", desc: "Inject recent terminal output", type: "special", icon: "\u2B1B" }
    ];

    function scoredFuzzyMatch(query, text) {
        query = query.toLowerCase(); text = text.toLowerCase();
        var qi = 0, score = 0, lastMatch = -1;
        for (var ti = 0; ti < text.length && qi < query.length; ti++) {
            if (text[ti] === query[qi]) {
                if (ti === 0 || text[ti - 1] === "/" || text[ti - 1] === "." || text[ti - 1] === "_" || text[ti - 1] === "-") score += 10;
                if (lastMatch === ti - 1) score += 5;
                score += 1; lastMatch = ti; qi++;
            }
        }
        return qi === query.length ? score : -1;
    }

    function getMentionCandidates(query) {
        var results = [];
        var q = query.toLowerCase();
        for (var si = 0; si < SPECIAL_MENTIONS.length; si++) {
            var s = SPECIAL_MENTIONS[si];
            if (!q || s.label.toLowerCase().includes(q)) {
                results.push({ label: s.label, desc: s.desc, type: s.type, icon: s.icon, score: q ? (s.label.toLowerCase().startsWith(q) ? 100 : 50) : 10 });
            }
        }
        if (_allFilePaths) {
            for (var fi = 0; fi < _allFilePaths.length; fi++) {
                if (results.length >= 12) break;
                var f = _allFilePaths[fi];
                var nameScore = scoredFuzzyMatch(q, f.name);
                var pathScore = scoredFuzzyMatch(q, f.path);
                var best = Math.max(nameScore, pathScore);
                if (!q || best > 0) results.push({ label: f.name, path: f.path, dir: f.dir || "", type: "file", icon: "", score: best > 0 ? best : 1 });
            }
        }
        results.sort(function (a, b) { return b.score - a.score; });
        return results.slice(0, 10);
    }

    function renderMentionPopup(items) {
        if (!$mentionPopup) return;
        if (!items || items.length === 0) { $mentionPopup.classList.add("hidden"); _mentionActive = false; return; }
        _mentionItems = items;
        _mentionSelectedIdx = 0;
        var html = "";
        items.forEach(function (item, i) {
            var icon = item.type === "file" ? fileTypeIcon(item.label, 14) : item.icon;
            var dir = item.dir ? '<span class="mention-dir">' + BX.escapeHtml(item.dir) + '</span>' : "";
            var desc = item.desc ? '<span class="mention-dir">' + BX.escapeHtml(item.desc) + '</span>' : "";
            var typeBadge = item.type === "special" ? '<span class="mention-type">special</span>' : "";
            html += '<div class="mention-item' + (i === 0 ? " selected" : "") + '" data-idx="' + i + '">'
                + '<span class="mention-icon">' + icon + '</span>'
                + '<span class="mention-label">' + BX.escapeHtml(item.label) + '</span>'
                + dir + desc + typeBadge
                + '</div>';
        });
        $mentionPopup.innerHTML = html;
        $mentionPopup.classList.remove("hidden");
        _mentionActive = true;
        $mentionPopup.querySelectorAll(".mention-item").forEach(function (el) {
            el.addEventListener("mousedown", function (e) { e.preventDefault(); selectMention(parseInt(el.dataset.idx)); });
        });
    }

    function selectMention(idx) {
        var item = _mentionItems[idx];
        if (!item) return;
        var val = $input.value;
        var before = val.slice(0, _mentionStart);
        var after = val.slice($input.selectionStart);
        var mention = item.type === "file" ? "@" + item.path + " " : "@" + item.label + " ";
        $input.value = before + mention + after;
        closeMentionPopup();
        $input.focus();
        var newPos = before.length + mention.length;
        $input.setSelectionRange(newPos, newPos);
    }

    function closeMentionPopup() {
        if ($mentionPopup) $mentionPopup.classList.add("hidden");
        _mentionActive = false;
        _mentionStart = -1;
    }

    function updateMentionHighlight() {
        if (!$mentionPopup) return;
        $mentionPopup.querySelectorAll(".mention-item").forEach(function (el, i) { el.classList.toggle("selected", i === _mentionSelectedIdx); });
        var sel = $mentionPopup.querySelector(".mention-item.selected");
        if (sel) sel.scrollIntoView({ block: "nearest" });
    }

    if ($input && $mentionPopup) {
        $input.addEventListener("input", async function () {
            var val = $input.value;
            var pos = $input.selectionStart;
            if (_mentionActive) {
                if (pos <= _mentionStart || val[_mentionStart] !== "@") { closeMentionPopup(); return; }
                var query = val.slice(_mentionStart + 1, pos);
                if (query.includes(" ") || query.includes("\n")) { closeMentionPopup(); return; }
                if (!_allFilePaths) await fetchAllFiles();
                renderMentionPopup(getMentionCandidates(query));
                return;
            }
            if (pos > 0 && val[pos - 1] === "@") {
                var charBefore = pos >= 2 ? val[pos - 2] : " ";
                if (charBefore === " " || charBefore === "\n" || pos === 1) {
                    _mentionStart = pos - 1;
                    if (!_allFilePaths) await fetchAllFiles();
                    renderMentionPopup(getMentionCandidates(""));
                }
            }
        });
        $input.addEventListener("keydown", function (e) {
            if (!_mentionActive) return;
            if (e.key === "ArrowDown") { e.preventDefault(); _mentionSelectedIdx = Math.min(_mentionSelectedIdx + 1, _mentionItems.length - 1); updateMentionHighlight(); }
            else if (e.key === "ArrowUp") { e.preventDefault(); _mentionSelectedIdx = Math.max(_mentionSelectedIdx - 1, 0); updateMentionHighlight(); }
            else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); selectMention(_mentionSelectedIdx); }
            else if (e.key === "Escape") { e.preventDefault(); closeMentionPopup(); }
        });
        $input.addEventListener("blur", function () { setTimeout(closeMentionPopup, 150); });
    }

    // ================================================================
    // COMMAND PALETTE
    // ================================================================

    var CP_COMMANDS = [
        { id: "open-file", label: "Open File\u2026", shortcut: "\u2318P", icon: "\uD83D\uDCC4", action: function () { openCommandPalette("file"); } },
        { id: "search-files", label: "Search in Files", shortcut: "\u2318\u21E7F", icon: "\uD83D\uDD0D", action: function () { closeCommandPalette(); var btn = document.getElementById("search-toggle-btn"); if (btn) btn.click(); } },
        { id: "new-chat", label: "New Chat / Reset Session", shortcut: "", icon: "\uD83D\uDCAC", action: function () { closeCommandPalette(); BX.send({ type: "reset" }); } },
        { id: "refresh-tree", label: "Refresh File Tree", shortcut: "", icon: "\uD83D\uDD04", action: function () { closeCommandPalette(); refreshTree(); } },
        { id: "toggle-explorer", label: "Toggle Explorer Panel", shortcut: "\u2318B", icon: "\uD83D\uDCC1", action: function () { closeCommandPalette(); var ex = document.getElementById("file-explorer"); if (ex) ex.style.display = ex.style.display === "none" ? "" : "none"; } },
        { id: "go-to-line", label: "Go to Line\u2026", shortcut: "\u2318G", icon: "\u2195", action: function () { closeCommandPalette(); if (BX.monacoInstance) { var a = BX.monacoInstance.getAction("editor.action.gotoLine"); if (a) a.run(); } } }
    ];

    function openCommandPalette(mode) {
        mode = mode || "command";
        if (!$cp) return;
        _cpMode = mode;
        $cp.classList.remove("hidden");
        $cpInput.value = mode === "file" ? "" : "> ";
        $cpInput.placeholder = mode === "file" ? "Search files by name\u2026" : "Type a command\u2026";
        $cpInput.focus();
        updatePaletteResults();
    }

    function closeCommandPalette() {
        if ($cp) $cp.classList.add("hidden");
        $cpInput.value = "";
    }

    function updatePaletteResults() {
        var raw = $cpInput.value;
        var isCmd = raw.startsWith("> ");
        _cpMode = isCmd ? "command" : "file";
        var q = isCmd ? raw.slice(2).trim() : raw.trim();

        if (_cpMode === "command") {
            _cpItems = CP_COMMANDS.filter(function (c) { return !q || c.label.toLowerCase().includes(q.toLowerCase()); });
            _cpSelectedIdx = 0;
            renderPaletteItems(_cpItems.map(function (c) { return { icon: c.icon, label: c.label, shortcut: c.shortcut, dir: "" }; }));
        } else {
            if (!_allFilePaths) { fetchAllFiles().then(function () { updatePaletteResults(); }); return; }
            var matches;
            if (!q) {
                matches = _allFilePaths.slice(0, 15);
            } else {
                var scored = [];
                for (var i = 0; i < _allFilePaths.length; i++) {
                    var f = _allFilePaths[i];
                    var s = scoredFuzzyMatch(q, f.name);
                    var ps = scoredFuzzyMatch(q, f.path);
                    var best = Math.max(s, ps);
                    if (best > 0) scored.push({ name: f.name, path: f.path, dir: f.dir, ext: f.ext, score: best });
                }
                scored.sort(function (a, b) { return b.score - a.score; });
                matches = scored.slice(0, 15);
            }
            _cpItems = matches.map(function (f) { return { icon: fileTypeIcon(f.name, 14), label: f.name, dir: f.dir || "", path: f.path, shortcut: "", _isFile: true }; });
            _cpSelectedIdx = 0;
            renderPaletteItems(_cpItems);
        }
    }

    function renderPaletteItems(items) {
        if (!$cpResults) return;
        if (!items.length) { $cpResults.innerHTML = '<div class="cp-item" style="opacity:0.4;cursor:default">No results</div>'; return; }
        var html = "";
        items.forEach(function (item, i) {
            var dir = item.dir ? '<span class="cp-dir">' + BX.escapeHtml(item.dir) + '</span>' : "";
            var shortcut = item.shortcut ? '<span class="cp-shortcut">' + item.shortcut + '</span>' : "";
            html += '<div class="cp-item' + (i === 0 ? " selected" : "") + '" data-idx="' + i + '">'
                + '<span class="cp-icon">' + item.icon + '</span>'
                + '<span class="cp-label">' + BX.escapeHtml(item.label) + '</span>'
                + dir + shortcut
                + '</div>';
        });
        $cpResults.innerHTML = html;
        $cpResults.querySelectorAll(".cp-item").forEach(function (el) {
            el.addEventListener("mousedown", function (e) { e.preventDefault(); executePaletteItem(parseInt(el.dataset.idx)); });
        });
    }

    function executePaletteItem(idx) {
        var item = _cpItems[idx];
        if (!item) return;
        if (item._isFile || item.path) { closeCommandPalette(); BX.openFile(item.path); }
        else if (item.action) item.action();
        else { var cmd = CP_COMMANDS[idx]; if (cmd && cmd.action) cmd.action(); }
    }

    function updatePaletteHighlight() {
        if (!$cpResults) return;
        $cpResults.querySelectorAll(".cp-item").forEach(function (el, i) { el.classList.toggle("selected", i === _cpSelectedIdx); });
        var sel = $cpResults.querySelector(".cp-item.selected");
        if (sel) sel.scrollIntoView({ block: "nearest" });
    }

    if ($cp && $cpInput) {
        $cpInput.addEventListener("input", updatePaletteResults);
        $cpInput.addEventListener("keydown", function (e) {
            if (e.key === "ArrowDown") { e.preventDefault(); _cpSelectedIdx = Math.min(_cpSelectedIdx + 1, _cpItems.length - 1); updatePaletteHighlight(); }
            else if (e.key === "ArrowUp") { e.preventDefault(); _cpSelectedIdx = Math.max(_cpSelectedIdx - 1, 0); updatePaletteHighlight(); }
            else if (e.key === "Enter") { e.preventDefault(); executePaletteItem(_cpSelectedIdx); }
            else if (e.key === "Escape") { e.preventDefault(); closeCommandPalette(); }
        });
        $cp.querySelector(".command-palette-backdrop").addEventListener("click", closeCommandPalette);
    }

    document.addEventListener("keydown", function (e) {
        var isMac = navigator.platform.toUpperCase().indexOf("MAC") >= 0;
        var mod = isMac ? e.metaKey : e.ctrlKey;
        if (mod && e.shiftKey && e.key.toLowerCase() === "p") { e.preventDefault(); openCommandPalette("command"); }
        else if (mod && !e.shiftKey && e.key.toLowerCase() === "p") { e.preventDefault(); openCommandPalette("file"); }
    });

    // ================================================================
    // MODIFIED FILES BAR
    // ================================================================

    async function updateModifiedFilesBar() {
        if (!$modifiedFilesBar || !$modifiedFilesToggle || !$modifiedFilesList || !$modifiedFilesDropdown) return;
        try {
            var res = await fetch("/api/git-diff-stats?t=" + Date.now());
            if (!res.ok) { $modifiedFilesBar.classList.add("hidden"); return; }
            var data = await res.json();
            var files = data.files || [];
            var totalAdd = data.total_additions || 0;
            var totalDel = data.total_deletions || 0;
            if (files.length === 0) {
                $modifiedFilesBar.classList.add("hidden");
                if ($chatComposerStats) { $chatComposerStats.classList.add("hidden"); BX.updateStripVisibility(); }
                return;
            }
            $modifiedFilesBar.classList.remove("hidden");
            var addEl = $modifiedFilesToggle.querySelector(".modified-files-add");
            var delEl = $modifiedFilesToggle.querySelector(".modified-files-del");
            var countEl = $modifiedFilesToggle.querySelector(".modified-files-count");
            if (addEl) addEl.textContent = "+" + totalAdd;
            if (delEl) delEl.textContent = "\u2212" + totalDel;
            if (countEl) countEl.textContent = files.length + " file" + (files.length !== 1 ? "s" : "");
            $modifiedFilesList.innerHTML = files.map(function (f) {
                var path = (f.path || "").replace(/\\/g, "/");
                var add = f.additions != null ? f.additions : 0;
                var del = f.deletions != null ? f.deletions : 0;
                var icon = fileTypeIcon(path, 14);
                return '<div class="modified-files-item" data-path="' + BX.escapeHtml(path) + '" title="Open ' + BX.escapeHtml(path) + '">'
                    + '<span class="file-icon">' + icon + '</span>'
                    + '<span class="file-path">' + BX.escapeHtml(path) + '</span>'
                    + '<span class="file-stats"><span class="add">+' + add + '</span><span class="del">\u2212' + del + '</span></span>'
                    + '</div>';
            }).join("");
            $modifiedFilesList.querySelectorAll(".modified-files-item").forEach(function (el) {
                el.addEventListener("click", function () {
                    var p = el.dataset.path || "";
                    if (p) BX.openFile(p);
                    $modifiedFilesDropdown.classList.add("hidden");
                    $modifiedFilesBar.setAttribute("aria-expanded", "false");
                });
            });
            // Hide composer stats pill (edit counts removed by user request)
            if ($chatComposerStats) {
                $chatComposerStats.classList.add("hidden");
                BX.updateStripVisibility();
            }
        } catch (e) {
            $modifiedFilesBar.classList.add("hidden");
            if ($chatComposerStats) $chatComposerStats.classList.add("hidden");
        }
    }

    // ================================================================
    // POLLING
    // ================================================================

    function startModifiedFilesPolling() {
        if (modifiedFilesPollTimer) return;
        modifiedFilesPollTimer = setInterval(updateModifiedFilesBar, MODIFIED_FILES_POLL_MS);
    }
    function stopModifiedFilesPolling() {
        if (modifiedFilesPollTimer) { clearInterval(modifiedFilesPollTimer); modifiedFilesPollTimer = null; }
    }
    function startGitStatusPolling() {
        if (gitStatusPollTimer) return;
        gitStatusPollTimer = setInterval(function () { fetchGitStatus(); }, GIT_STATUS_POLL_MS);
    }
    function stopGitStatusPolling() {
        if (gitStatusPollTimer) { clearInterval(gitStatusPollTimer); gitStatusPollTimer = null; }
    }
    startGitStatusPolling();
    startModifiedFilesPolling();

    // ================================================================
    // DROPDOWNS / MENUS
    // ================================================================

    function closeFilesDropdown() {
        if ($chatComposerFilesDropup) { $chatComposerFilesDropup.classList.add("hidden"); $chatComposerFilesDropup.setAttribute("aria-hidden", "true"); }
        if ($chatComposerStats) $chatComposerStats.removeAttribute("data-expanded");
        if ($chatComposerStatsToggle) $chatComposerStatsToggle.setAttribute("aria-expanded", "false");
    }

    function closeTodoDropdown() {
        if ($stickyTodoDropdown) $stickyTodoDropdown.classList.add("hidden");
        $stickyTodoList.classList.add("hidden");
        if ($stickyTodoAddRow) $stickyTodoAddRow.classList.add("hidden");
        if ($stickyTodoBar) $stickyTodoBar.setAttribute("data-expanded", "false");
        if ($stickyTodoToggle) $stickyTodoToggle.setAttribute("aria-expanded", "false");
    }

    function closeModifiedFilesDropdown() {
        if ($modifiedFilesDropdown) $modifiedFilesDropdown.classList.add("hidden");
        if ($modifiedFilesBar) $modifiedFilesBar.setAttribute("aria-expanded", "false");
    }

    if ($modifiedFilesToggle && $modifiedFilesDropdown && $modifiedFilesBar) {
        $modifiedFilesToggle.addEventListener("click", function () {
            var expanded = $modifiedFilesBar.getAttribute("aria-expanded") === "true";
            $modifiedFilesBar.setAttribute("aria-expanded", expanded ? "false" : "true");
            $modifiedFilesDropdown.classList.toggle("hidden", expanded);
        });
    }

    if ($stickyTodoToggle && $stickyTodoList && $stickyTodoBar) {
        $stickyTodoToggle.addEventListener("click", function () {
            var expanded = $stickyTodoBar.getAttribute("data-expanded") === "true";
            if (!expanded) closeFilesDropdown();
            $stickyTodoBar.setAttribute("data-expanded", expanded ? "false" : "true");
            if ($stickyTodoDropdown) $stickyTodoDropdown.classList.toggle("hidden", expanded);
            $stickyTodoList.classList.toggle("hidden", expanded);
            if ($stickyTodoAddRow) $stickyTodoAddRow.classList.toggle("hidden", expanded);
            $stickyTodoToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
        });
    }

    if ($stickyTodoAddBtn && $stickyTodoAddInput) {
        function submitStickyAddTask() {
            var content = ($stickyTodoAddInput.value || "").trim();
            if (!content) return;
            BX.send({ type: "add_todo", content: content });
            $stickyTodoAddInput.value = "";
        }
        $stickyTodoAddBtn.addEventListener("click", submitStickyAddTask);
        $stickyTodoAddInput.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); submitStickyAddTask(); } });
    }

    if ($chatComposerStatsToggle && $chatComposerFilesDropup && $chatComposerStats) {
        function toggleComposerFilesDropup() {
            var expanded = $chatComposerStats.getAttribute("data-expanded") === "true";
            if (!expanded) closeTodoDropdown();
            $chatComposerStats.setAttribute("data-expanded", expanded ? "false" : "true");
            $chatComposerFilesDropup.classList.toggle("hidden", expanded);
            $chatComposerStatsToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
            $chatComposerFilesDropup.setAttribute("aria-hidden", expanded ? "true" : "false");
        }
        $chatComposerStatsToggle.addEventListener("click", toggleComposerFilesDropup);
    }

    if ($chatMenuBtn && $chatMenuDropdown) {
        $chatMenuBtn.addEventListener("click", function (e) { e.stopPropagation(); $chatMenuDropdown.classList.toggle("hidden"); });
        $chatMenuDropdown.addEventListener("click", function (e) { e.stopPropagation(); });
        document.addEventListener("click", function () { $chatMenuDropdown.classList.add("hidden"); });
    }

    $refreshTree.addEventListener("click", refreshTree);
    var $collapseAll = document.getElementById("collapse-all-btn");
    if ($collapseAll) {
        $collapseAll.addEventListener("click", function () {
            for (var key in treeState) treeState[key] = false;
            $fileTree.innerHTML = "";
            loadTree();
        });
    }
    if ($sourceControlRefreshBtn) {
        $sourceControlRefreshBtn.addEventListener("click", async function () { await fetchGitStatus(); $fileTree.innerHTML = ""; loadTree(); });
    }

    // ================================================================
    // EXPORTS
    // ================================================================

    BX.renderSourceControl = renderSourceControl;
    BX.fetchGitStatus = fetchGitStatus;
    BX.loadTree = loadTree;
    BX.refreshTree = refreshTree;
    BX._doRefreshTree = _doRefreshTree;
    BX.markFileModified = markFileModified;
    BX.syncFileStatusIndicators = syncFileStatusIndicators;
    BX.fileTypeIcon = fileTypeIcon;
    BX.fileIcon = fileIcon;
    BX._debouncedGitStatus = _debouncedGitStatus;
    BX.fuzzyMatch = fuzzyMatch;
    BX.renderFilteredFiles = renderFilteredFiles;
    BX.scoredFuzzyMatch = scoredFuzzyMatch;
    BX.getMentionCandidates = getMentionCandidates;
    BX.renderMentionPopup = renderMentionPopup;
    BX.selectMention = selectMention;
    BX.closeMentionPopup = closeMentionPopup;
    BX.updateMentionHighlight = updateMentionHighlight;
    BX.fetchAllFiles = fetchAllFiles;
    BX.openCommandPalette = openCommandPalette;
    BX.closeCommandPalette = closeCommandPalette;
    BX.updatePaletteResults = updatePaletteResults;
    BX.renderPaletteItems = renderPaletteItems;
    BX.executePaletteItem = executePaletteItem;
    BX.updatePaletteHighlight = updatePaletteHighlight;
    BX.startModifiedFilesPolling = startModifiedFilesPolling;
    BX.stopModifiedFilesPolling = stopModifiedFilesPolling;
    BX.startGitStatusPolling = startGitStatusPolling;
    BX.stopGitStatusPolling = stopGitStatusPolling;
    BX.closeFilesDropdown = closeFilesDropdown;
    BX.closeTodoDropdown = closeTodoDropdown;
    BX.updateModifiedFilesBar = updateModifiedFilesBar;

})(window.BX);
