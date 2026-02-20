/* ============================================================
   Bedrock Codex — welcome.js
   Project modal, search panel, welcome screen (session management,
   projects, local/SSH modals, SSH browse), and init function
   ============================================================ */
(function (BX) {
    "use strict";

    // ── DOM ref aliases ─────────────────────────────────────────
    var $welcomeScreen       = BX.$welcomeScreen;
    var $ideWrapper          = BX.$ideWrapper;
    var $welcomeOpenLocal    = BX.$welcomeOpenLocal;
    var $welcomeSshBtn       = BX.$welcomeSshBtn;
    var $projectList         = BX.$projectList;
    var $localModal          = BX.$localModal;
    var $localPath           = BX.$localPath;
    var $localError          = BX.$localError;
    var $localOpen           = BX.$localOpen;
    var $localCancel         = BX.$localCancel;
    var $sshModal            = BX.$sshModal;
    var $sshHost             = BX.$sshHost;
    var $sshUser             = BX.$sshUser;
    var $sshPort             = BX.$sshPort;
    var $sshKey              = BX.$sshKey;
    var $sshDir              = BX.$sshDir;
    var $sshError            = BX.$sshError;
    var $sshOpen             = BX.$sshOpen;
    var $sshCancel           = BX.$sshCancel;
    var $sshBrowseBtn        = BX.$sshBrowseBtn;
    var $sshBrowseModal      = BX.$sshBrowseModal;
    var $sshBrowseList       = BX.$sshBrowseList;
    var $sshBrowseBreadcrumb = BX.$sshBrowseBreadcrumb;
    var $sshBrowseCurrent    = BX.$sshBrowseCurrent;
    var $sshBrowseSelect     = BX.$sshBrowseSelect;
    var $sshBrowseCancel     = BX.$sshBrowseCancel;
    var $dirModal            = document.getElementById("dir-modal");
    var $dirInput            = document.getElementById("dir-input");
    var $dirError            = document.getElementById("dir-error");
    var $dirOpen             = document.getElementById("dir-open");
    var $dirCancel           = document.getElementById("dir-cancel");
    var $workingDir          = BX.$workingDir;
    var $chatMessages        = BX.$chatMessages;
    var $tabBar              = BX.$tabBar;
    var $editorWelcome       = BX.$editorWelcome;
    var $terminalPanel       = BX.$terminalPanel;
    var $input               = BX.$input;
    var $openBtn             = BX.$openBtn;
    var $logoHome            = BX.$logoHome;
    var $agentSelect         = BX.$agentSelect;
    var $stickyTodoBar       = BX.$stickyTodoBar;
    var $chatComposerStats   = BX.$chatComposerStats;
    var $statusStrip         = BX.$statusStrip;
    var $searchToggle        = BX.$searchToggle;
    var $searchPanel         = BX.$searchPanel;
    var $searchInput         = BX.$searchInput;
    var $searchInclude       = BX.$searchInclude;
    var $searchGoBtn         = BX.$searchGoBtn;
    var $searchResults       = BX.$searchResults;
    var $searchStatus        = BX.$searchStatus;
    var $searchRegex         = BX.$searchRegex;
    var $searchCase          = BX.$searchCase;
    var $replaceRow          = BX.$replaceRow;
    var $replaceInput        = BX.$replaceInput;
    var $replaceAllBtn       = BX.$replaceAllBtn;
    var $replaceToggle       = BX.$replaceToggle;

    // ── Reference-type state aliases ────────────────────────────
    var modifiedFiles            = BX.modifiedFiles;
    var openTabs                 = BX.openTabs;
    var fileChangesThisSession   = BX.fileChangesThisSession;
    var sessionCumulativeStats   = BX.sessionCumulativeStats;

    // ── Module-local state ──────────────────────────────────────
    var searchUseRegex      = false;
    var searchCaseSensitive = false;
    var lastSearchFiles     = [];
    var sshBrowseCurrentPath = "";

    // ================================================================
    // PROJECT MODAL
    // ================================================================

    function openDirModal() { $dirInput.value = ""; $dirError.classList.add("hidden"); $dirModal.classList.remove("hidden"); $dirInput.focus(); }
    function closeDirModal() { $dirModal.classList.add("hidden"); }

    function submitDir() {
        var path = $dirInput.value.trim(); if (!path) return;
        $dirOpen.disabled = true; $dirOpen.textContent = "Opening\u2026"; $dirError.classList.add("hidden");
        return fetch("/api/set-directory", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: path }) })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data.ok) {
                    closeDirModal();
                    if (BX.ws) BX.ws.close();
                    $workingDir.textContent = data.path;
                    $chatMessages.innerHTML = "";
                    openTabs.forEach(function (info) { info.model.dispose(); });
                    openTabs.clear(); $tabBar.innerHTML = ""; BX.activeTab = null;
                    if (BX.monacoInstance) { BX.monacoInstance.setModel(null); }
                    if (BX.diffEditorInstance) { BX.diffEditorInstance.dispose(); BX.diffEditorInstance = null; }
                    $editorWelcome.classList.remove("hidden");
                    modifiedFiles.clear();
                    if (typeof BX.terminalDisconnect === "function" && typeof BX.terminalConnect === "function") {
                        BX.terminalDisconnect(true);
                        if ($terminalPanel && !$terminalPanel.classList.contains("hidden")) BX.terminalConnect();
                    }
                    BX.showToast("Opened: " + data.path);
                } else {
                    $dirError.textContent = data.error || "Failed"; $dirError.classList.remove("hidden");
                }
            })
            .catch(function (e) { $dirError.textContent = "Error: " + e.message; $dirError.classList.remove("hidden"); })
            .then(function () { $dirOpen.disabled = false; $dirOpen.textContent = "Open"; });
    }

    // ================================================================
    // SEARCH PANEL
    // ================================================================

    function performSearch() {
        var pattern = $searchInput.value.trim();
        if (!pattern) return;

        var searchPattern = pattern;
        if (!searchUseRegex) {
            searchPattern = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        }
        if (!searchCaseSensitive) {
            searchPattern = "(?i)" + searchPattern;
        }

        $searchStatus.textContent = "Searching...";
        $searchResults.innerHTML = "";
        lastSearchFiles = [];

        var params = new URLSearchParams({ pattern: searchPattern });
        var include = $searchInclude.value.trim();
        if (include) params.append("include", include);

        return fetch("/api/search?" + params.toString())
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data.error) {
                    $searchStatus.textContent = "Error: " + data.error;
                    return;
                }

                var results = data.results || [];
                var fileSet = {};
                results.forEach(function (r) { fileSet[r.file] = true; });
                var fileCount = Object.keys(fileSet).length;
                $searchStatus.textContent = results.length === 0
                    ? "No results found"
                    : data.count + " match" + (data.count === 1 ? "" : "es") + " in " + fileCount + " file" + (fileCount === 1 ? "" : "s");

                var grouped = {};
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    if (!grouped[r.file]) grouped[r.file] = [];
                    grouped[r.file].push(r);
                }
                lastSearchFiles = Object.keys(grouped);

                Object.keys(grouped).forEach(function (file) {
                    var matches = grouped[file];
                    var group = document.createElement("div");
                    group.className = "search-file-group";

                    var header = document.createElement("div");
                    header.className = "search-file-name";
                    header.innerHTML = "<span>" + BX.escapeHtml(file) + "</span><span class=\"match-count\">" + matches.length + "</span>";
                    header.addEventListener("click", function () { BX.openFile(file); });
                    group.appendChild(header);

                    matches.forEach(function (m) {
                        var line = document.createElement("div");
                        line.className = "search-match";

                        var displayText = BX.escapeHtml(m.text);
                        try {
                            var escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                            var re = new RegExp("(" + (searchUseRegex ? pattern : escaped) + ")", searchCaseSensitive ? "g" : "gi");
                            displayText = m.text.replace(re, '<span class="match-highlight">$1</span>');
                        } catch (e) { /* keep plain text */ }

                        line.innerHTML = '<span class="line-num">' + m.line + "</span>" + displayText;
                        line.addEventListener("click", (function (targetFile, targetLine) {
                            return function () {
                                BX.openFile(targetFile).then(function () {
                                    if (BX.monacoInstance) {
                                        BX.monacoInstance.revealLineInCenter(targetLine);
                                        BX.monacoInstance.setPosition({ lineNumber: targetLine, column: 1 });
                                        BX.monacoInstance.focus();
                                    }
                                });
                            };
                        })(file, m.line));
                        group.appendChild(line);
                    });
                    $searchResults.appendChild(group);
                });
            })
            .catch(function (e) {
                $searchStatus.textContent = "Search failed: " + e.message;
            });
    }

    // ================================================================
    // WELCOME SCREEN
    // ================================================================

    function timeAgo(isoStr) {
        if (!isoStr) return "";
        var d = new Date(isoStr);
        var now = new Date();
        var secs = Math.floor((now - d) / 1000);
        if (secs < 60) return "just now";
        var mins = Math.floor(secs / 60);
        if (mins < 60) return mins + "m ago";
        var hrs = Math.floor(mins / 60);
        if (hrs < 24) return hrs + "h ago";
        var days = Math.floor(hrs / 24);
        if (days < 30) return days + "d ago";
        return d.toLocaleDateString();
    }

    function formatAgentOptionLabel(session) {
        var name = String(session && session.name ? session.name : "default");
        var age = timeAgo(session && session.updated_at ? session.updated_at : "");
        return age ? name + " (" + age + ")" : name;
    }

    function loadAgentSessions() {
        if (!$agentSelect) return Promise.resolve();
        return fetch("/api/sessions")
            .then(function (res) {
                if (!res.ok) throw new Error("Failed to load sessions");
                return res.json();
            })
            .then(function (sessions) {
                var list = Array.isArray(sessions) ? sessions : [];

                BX.suppressAgentSwitch = true;
                $agentSelect.innerHTML = "";
                for (var i = 0; i < list.length; i++) {
                    var s = list[i];
                    var opt = document.createElement("option");
                    opt.value = s.session_id || "";
                    opt.textContent = formatAgentOptionLabel(s);
                    $agentSelect.appendChild(opt);
                }

                var hasCurrent = !!BX.currentSessionId && list.some(function (s) { return s.session_id === BX.currentSessionId; });
                if (!hasCurrent && list.length > 0 && !BX.ws) {
                    BX.currentSessionId = list[0].session_id;
                    BX.persistSessionId(BX.currentSessionId);
                }
                if (BX.currentSessionId) {
                    $agentSelect.value = BX.currentSessionId;
                }
                $agentSelect.disabled = list.length === 0;
                BX.suppressAgentSwitch = false;
            })
            .catch(function () {
                BX.suppressAgentSwitch = true;
                $agentSelect.innerHTML = "";
                var opt = document.createElement("option");
                opt.value = "";
                opt.textContent = "No agents";
                $agentSelect.appendChild(opt);
                $agentSelect.disabled = true;
                BX.suppressAgentSwitch = false;
            });
    }

    function createNewAgentSession() {
        var name = (window.prompt("New agent name (optional):", "") || "").trim();
        return fetch("/api/sessions/new", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name }),
        })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (!data.ok) {
                    BX.showToast("Failed to create agent");
                    return;
                }
                BX.currentSessionId = data.session_id || null;
                BX.persistSessionId(BX.currentSessionId);
                return loadAgentSessions().then(function () {
                    BX.disconnectWs();
                    BX.connect();
                    BX.showToast("New agent: " + (data.name || "agent"));
                });
            })
            .catch(function (e) {
                BX.showToast("Error: " + (e && e.message ? e.message : "unable to create agent"));
            });
    }

    function loadRecentProjects() {
        return fetch("/api/projects")
            .then(function (res) { return res.json(); })
            .then(function (projects) {
                if (!projects || projects.length === 0) {
                    $projectList.innerHTML = '<div class="welcome-no-projects">No recent projects. Open a local folder or connect via SSH to get started.</div>';
                    return;
                }

                $projectList.innerHTML = "";
                projects.forEach(function (p) {
                    var el = document.createElement("div");
                    el.className = "welcome-project";
                    var icon = p.is_ssh ? "\uD83D\uDDA5\uFE0F" : "\uD83D\uDCC1";
                    var badge = p.is_ssh ? '<span class="welcome-project-badge ssh">SSH</span>' : "";
                    var displayPath = p.path;
                    var sshMeta = getSshProjectInfo(p);
                    if (p.is_ssh && sshMeta) {
                        displayPath = sshMeta.user + "@" + sshMeta.host + ":" + sshMeta.directory;
                    }
                    el.innerHTML =
                        '<div class="welcome-project-icon">' + icon + '</div>' +
                        '<div class="welcome-project-info">' +
                            '<span class="welcome-project-name">' + BX.escapeHtml(p.name) + " " + badge + '</span>' +
                            '<span class="welcome-project-path">' + BX.escapeHtml(displayPath) + '</span>' +
                        '</div>' +
                        '<div class="welcome-project-meta">' +
                            (p.session_name ? '<span class="welcome-project-session" title="' + BX.escapeHtml(p.session_name) + '">' + BX.escapeHtml(p.session_name) + '</span>' : "") +
                            '<span class="welcome-project-time">' + timeAgo(p.updated_at) + '</span>' +
                            '<span class="welcome-project-stats">' + p.message_count + ' msgs</span>' +
                        '</div>' +
                        '<button type="button" class="welcome-project-remove" title="Remove from recents" aria-label="Remove from recents">\u2715</button>';
                    el.addEventListener("click", function (e) { if (!e.target.closest(".welcome-project-remove")) openProject(p); });
                    var removeBtn = el.querySelector(".welcome-project-remove");
                    removeBtn.addEventListener("click", function (e) {
                        e.stopPropagation();
                        fetch("/api/projects/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: p.path }) })
                            .then(function (res) { return res.json(); })
                            .then(function (data) {
                                if (data.ok) { el.remove(); if ($projectList.children.length === 0) $projectList.innerHTML = '<div class="welcome-no-projects">No recent projects. Open a local folder or connect via SSH to get started.</div>'; }
                                else BX.showToast(data.error || "Failed to remove");
                            })
                            .catch(function () { BX.showToast("Failed to remove from recents"); });
                    });
                    $projectList.appendChild(el);
                });
            })
            .catch(function () {
                $projectList.innerHTML = '<div class="welcome-loading">Failed to load projects</div>';
            });
    }

    function loadRecentSessions() {
        var $sessionList = document.getElementById("welcome-session-list");
        if (!$sessionList) return;
        fetch("/api/sessions")
            .then(function (res) { return res.json(); })
            .then(function (sessions) {
                var list = Array.isArray(sessions) ? sessions : [];
                if (list.length === 0) {
                    $sessionList.innerHTML = '<div class="welcome-no-projects">No recent sessions.</div>';
                    return;
                }
                $sessionList.innerHTML = "";
                list.slice(0, 8).forEach(function (s) {
                    var el = document.createElement("div");
                    el.className = "welcome-project";
                    var name = s.name || s.session_id || "Unnamed session";
                    var age = s.updated_at ? timeAgo(s.updated_at) : "";
                    el.innerHTML =
                        '<div class="welcome-project-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></div>' +
                        '<div class="welcome-project-info">' +
                            '<span class="welcome-project-name">' + BX.escapeHtml(name) + '</span>' +
                            '<span class="welcome-project-path">' + BX.escapeHtml(s.session_id || "") + '</span>' +
                        '</div>' +
                        '<div class="welcome-project-meta">' +
                            '<span class="welcome-project-time">' + BX.escapeHtml(age) + '</span>' +
                        '</div>';
                    el.addEventListener("click", function () {
                        BX.currentSessionId = s.session_id;
                        BX.persistSessionId(s.session_id);
                        if (BX.ws) {
                            BX.disconnectWs();
                            BX.connect();
                        }
                        BX.showToast("Switched to session: " + name);
                    });
                    $sessionList.appendChild(el);
                });
            })
            .catch(function () {
                $sessionList.innerHTML = '<div class="welcome-loading">Failed to load sessions</div>';
            });
    }

    function parseSshCompositePath(path) {
        var raw = String(path || "").trim();
        var m = raw.match(/^([^@:\s]+)@([^:\s]+):(\d+):(.*)$/);
        if (!m) return null;
        var port = parseInt(m[3], 10);
        return {
            user: m[1].trim(),
            host: m[2].trim(),
            port: Number.isFinite(port) ? port : 22,
            key_path: "",
            directory: (m[4] || "").trim() || "/",
        };
    }

    function getSshProjectInfo(project) {
        var fromSaved = (project && typeof project.ssh_info === "object" && project.ssh_info) ? project.ssh_info : {};
        var fromPath = parseSshCompositePath(project && project.path);
        var merged = {
            user: String(fromSaved.user || (fromPath && fromPath.user) || "").trim(),
            host: String(fromSaved.host || (fromPath && fromPath.host) || "").trim(),
            port: Number(fromSaved.port || (fromPath && fromPath.port) || 22) || 22,
            key_path: String(fromSaved.key_path || "").trim(),
            directory: String(fromSaved.directory || (fromPath && fromPath.directory) || "").trim(),
        };
        if (merged.host.startsWith("ssh://")) merged.host = merged.host.slice("ssh://".length).trim();
        if (merged.host.includes("@") && !merged.user) {
            var parts = merged.host.split("@");
            merged.user = (parts[0] || "").trim();
            merged.host = (parts[1] || "").trim();
        }
        if (!merged.directory) merged.directory = "/";
        if (!merged.user || !merged.host) return null;
        return merged;
    }

    function openProject(project) {
        if (project.is_ssh) {
            BX.showToast("Reconnecting via SSH...");
            var info = getSshProjectInfo(project);
            if (!info) {
                BX.showToast("SSH reconnect failed: missing host/user. Reconnect manually.");
                return Promise.resolve();
            }
            return fetch("/api/ssh-connect", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    host: info.host,
                    user: info.user,
                    port: info.port || 22,
                    key_path: info.key_path || "",
                    directory: info.directory,
                }),
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (!data.ok) {
                        BX.showToast("SSH reconnect failed: " + (data.error || "unknown error"));
                        return;
                    }
                    transitionToIDE(data.path || (info.user + "@" + info.host + ":" + info.directory));
                })
                .catch(function (e) {
                    BX.showToast("Error: " + e.message);
                });
        } else {
            return fetch("/api/set-directory", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: project.path }),
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (!data.ok) {
                        BX.showToast("Failed to open: " + (data.error || "unknown error"));
                        return;
                    }
                    transitionToIDE(data.path);
                })
                .catch(function (e) {
                    BX.showToast("Error: " + e.message);
                });
        }
    }

    function transitionToIDE(dirPath) {
        $welcomeScreen.classList.add("hidden");
        $ideWrapper.classList.remove("hidden");
        $workingDir.textContent = dirPath || "";

        if (!BX.monacoInstance) BX.initMonaco();

        BX.disconnectWs();
        BX.connect();
        if (typeof BX.terminalDisconnect === "function" && typeof BX.terminalConnect === "function") {
            BX.terminalDisconnect(true);
            if ($terminalPanel && !$terminalPanel.classList.contains("hidden")) {
                BX.terminalConnect();
            }
        }
        $input.focus();
    }

    function hasUnsavedWork() {
        return modifiedFiles.size > 0 || fileChangesThisSession.size > 0;
    }

    window.addEventListener("beforeunload", function (e) {
        if (hasUnsavedWork()) {
            e.preventDefault();
            e.returnValue = "";
        }
    });

    function showWelcome() {
        if (hasUnsavedWork() && !confirm("You have pending file changes that haven't been kept or reverted. Leave anyway?")) {
            return;
        }
        BX.disconnectWs();
        if (typeof BX.terminalDisconnect === "function") BX.terminalDisconnect(true);

        $chatMessages.innerHTML = "";
        BX.clearAllDiffDecorations();
        openTabs.forEach(function (info) { try { info.model.dispose(); } catch (e) {} });
        openTabs.clear();
        $tabBar.innerHTML = "";
        BX.activeTab = null;
        if (BX.monacoInstance) BX.monacoInstance.setModel(null);
        if (BX.diffEditorInstance) { BX.diffEditorInstance.dispose(); BX.diffEditorInstance = null; }
        $editorWelcome.classList.remove("hidden");
        modifiedFiles.clear();
        fileChangesThisSession.clear();
        BX.currentChecklistItems = [];
        if ($stickyTodoBar) $stickyTodoBar.classList.add("hidden");
        if ($chatComposerStats) $chatComposerStats.classList.add("hidden");
        if ($statusStrip) $statusStrip.classList.add("hidden");
        BX.clearPendingImages();
        BX.setRunning(false);
        BX.currentSessionId = null;
        BX.persistSessionId(null);
        if ($agentSelect) {
            BX.suppressAgentSwitch = true;
            $agentSelect.innerHTML = "";
            var opt = document.createElement("option");
            opt.value = "";
            opt.textContent = "No agents";
            $agentSelect.appendChild(opt);
            $agentSelect.disabled = true;
            BX.suppressAgentSwitch = false;
        }

        $ideWrapper.classList.add("hidden");
        $welcomeScreen.classList.remove("hidden");

        loadRecentProjects();
        loadRecentSessions();
    }

    // ── Welcome: Open Local ─────────────────────────────────────

    function submitLocalProject() {
        var path = $localPath.value.trim();
        if (!path) return;
        $localOpen.disabled = true;
        $localOpen.textContent = "Opening\u2026";
        $localError.classList.add("hidden");
        return fetch("/api/set-directory", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: path }),
        })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data.ok) {
                    $localModal.classList.add("hidden");
                    transitionToIDE(data.path);
                } else {
                    $localError.textContent = data.error || "Failed to open directory";
                    $localError.classList.remove("hidden");
                }
            })
            .catch(function (e) {
                $localError.textContent = "Error: " + e.message;
                $localError.classList.remove("hidden");
            })
            .then(function () {
                $localOpen.disabled = false;
                $localOpen.textContent = "Open Project";
            });
    }

    // ── Welcome: SSH Connect ────────────────────────────────────

    function submitSSH() {
        var host = $sshHost.value.trim();
        var user = $sshUser.value.trim();
        var port = $sshPort.value.trim() || "22";
        var key = $sshKey.value.trim();
        var dir = $sshDir.value.trim();

        if (!host || !user || !dir) {
            $sshError.textContent = "Host, user, and remote directory are required.";
            $sshError.classList.remove("hidden");
            return;
        }

        $sshOpen.disabled = true;
        $sshOpen.textContent = "Connecting\u2026";
        $sshError.classList.add("hidden");

        return fetch("/api/ssh-connect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ host: host, user: user, port: parseInt(port), key_path: key, directory: dir }),
        })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data.ok) {
                    $sshModal.classList.add("hidden");
                    transitionToIDE(data.path || (user + "@" + host + ":" + dir));
                } else {
                    $sshError.textContent = data.error || "SSH connection failed";
                    $sshError.classList.remove("hidden");
                }
            })
            .catch(function (e) {
                $sshError.textContent = "Error: " + e.message;
                $sshError.classList.remove("hidden");
            })
            .then(function () {
                $sshOpen.disabled = false;
                $sshOpen.textContent = "Connect";
            });
    }

    // ── SSH browse remote folder ────────────────────────────────

    function loadSshBrowseDir(directory) {
        if (!$sshBrowseList) return Promise.resolve();
        var host = $sshHost.value.trim();
        var user = $sshUser.value.trim();
        var port = $sshPort.value.trim() || "22";
        var key = $sshKey.value.trim();
        if (!host || !user) {
            $sshBrowseList.innerHTML = '<div class="ssh-browse-error">Enter host and user first.</div>';
            return Promise.resolve();
        }
        $sshBrowseList.innerHTML = '<div class="ssh-browse-loading">Loading\u2026</div>';
        $sshBrowseCurrent.textContent = "";
        return fetch("/api/ssh-list-dir", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                host: host,
                user: user,
                port: parseInt(port, 10) || 22,
                key_path: key || "",
                directory: directory || "~",
            }),
        })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (!data.ok) {
                    $sshBrowseList.innerHTML = '<div class="ssh-browse-error">' + BX.escapeHtml(data.error || "Failed to list directory") + '</div>';
                    return;
                }
                sshBrowseCurrentPath = data.path || directory || "~";
                var entries = data.entries || [];
                var parent = data.parent;

                var pathParts = sshBrowseCurrentPath.replace(/\/$/, "").split("/").filter(Boolean);
                if (!sshBrowseCurrentPath.startsWith("/") && sshBrowseCurrentPath !== "") pathParts.unshift(sshBrowseCurrentPath);
                if (sshBrowseCurrentPath === "/" && pathParts.length === 0) pathParts = ["/"];
                var breadcrumbHtml = "";
                if (parent) {
                    breadcrumbHtml += '<button type="button" class="ssh-browse-up" data-dir="' + BX.escapeHtml(parent) + '" title="Parent">\u21A9</button> ';
                }
                var isAbsolute = sshBrowseCurrentPath.startsWith("/");
                breadcrumbHtml += pathParts.map(function (p, i) {
                    var segPath = (isAbsolute && pathParts[0] !== "/")
                        ? "/" + pathParts.slice(0, i + 1).join("/")
                        : pathParts.slice(0, i + 1).join("/");
                    var isLast = i === pathParts.length - 1;
                    return isLast
                        ? '<span class="ssh-browse-seg current">' + BX.escapeHtml(p) + '</span>'
                        : '<button type="button" class="ssh-browse-seg" data-dir="' + BX.escapeHtml(segPath) + '">' + BX.escapeHtml(p) + '</button> / ';
                }).join("");
                $sshBrowseBreadcrumb.innerHTML = breadcrumbHtml || BX.escapeHtml(sshBrowseCurrentPath);
                $sshBrowseCurrent.textContent = sshBrowseCurrentPath;

                var dirs = entries.filter(function (e) { return e.type === "directory"; });
                var files = entries.filter(function (e) { return e.type === "file"; });
                var listHtml = "";
                dirs.forEach(function (e) {
                    var nextPath = sshBrowseCurrentPath.replace(/\/?$/, "") + "/" + e.name;
                    listHtml += '<button type="button" class="ssh-browse-entry dir" data-dir="' + BX.escapeHtml(nextPath) + '"><span class="ssh-browse-icon">\uD83D\uDCC1</span> ' + BX.escapeHtml(e.name) + '</button>';
                });
                files.forEach(function (e) {
                    listHtml += '<div class="ssh-browse-entry file"><span class="ssh-browse-icon">\uD83D\uDCC4</span> ' + BX.escapeHtml(e.name) + '</div>';
                });
                if (listHtml === "") listHtml = '<div class="ssh-browse-empty">No entries</div>';
                $sshBrowseList.innerHTML = listHtml;
            })
            .catch(function (e) {
                $sshBrowseList.innerHTML = '<div class="ssh-browse-error">' + BX.escapeHtml(e.message || "Network error") + '</div>';
            });
    }

    function updateModifiedFilesBar() {
        if (typeof BX.updateModifiedFilesBar === "function") BX.updateModifiedFilesBar();
    }

    // ================================================================
    // INIT
    // ================================================================

    function init() {
        BX.initMonaco();

        // ── Project modal listeners ─────────────────────────────
        $openBtn.addEventListener("click", openDirModal);
        $dirCancel.addEventListener("click", closeDirModal);
        $dirModal.querySelector(".modal-overlay").addEventListener("click", closeDirModal);
        $dirOpen.addEventListener("click", submitDir);
        $dirInput.addEventListener("keydown", function (e) { if (e.key === "Enter") submitDir(); if (e.key === "Escape") closeDirModal(); });

        // ── Search panel listeners ──────────────────────────────
        $searchToggle.addEventListener("click", function () {
            $searchPanel.classList.toggle("hidden");
            if (!$searchPanel.classList.contains("hidden")) {
                $searchInput.focus();
            }
        });

        $searchRegex.addEventListener("click", function () {
            searchUseRegex = !searchUseRegex;
            $searchRegex.classList.toggle("active", searchUseRegex);
        });
        $searchCase.addEventListener("click", function () {
            searchCaseSensitive = !searchCaseSensitive;
            $searchCase.classList.toggle("active", searchCaseSensitive);
        });
        $replaceToggle.addEventListener("click", function () {
            $replaceRow.classList.toggle("hidden");
            if (!$replaceRow.classList.contains("hidden")) $replaceInput.focus();
        });

        $searchGoBtn.addEventListener("click", performSearch);
        $searchInput.addEventListener("keydown", function (e) { if (e.key === "Enter") performSearch(); });

        $replaceAllBtn.addEventListener("click", function () {
            var pattern = $searchInput.value.trim();
            var replacement = $replaceInput.value;
            if (!pattern || lastSearchFiles.length === 0) return;

            var count = lastSearchFiles.length;
            if (!confirm("Replace in " + count + " file" + (count === 1 ? "" : "s") + "?")) return;

            $replaceAllBtn.disabled = true;
            $replaceAllBtn.textContent = "Replacing...";

            fetch("/api/replace", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    pattern: pattern,
                    replacement: replacement,
                    files: lastSearchFiles,
                    regex: searchUseRegex,
                }),
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (data.error) {
                        BX.showToast("Replace error: " + data.error, true);
                    } else {
                        var changed = data.changed || [];
                        var totalReplacements = changed.reduce(function (s, c) { return s + c.replacements; }, 0);
                        BX.showToast("Replaced " + totalReplacements + " occurrence" + (totalReplacements === 1 ? "" : "s") + " in " + changed.length + " file" + (changed.length === 1 ? "" : "s"));
                        var reloadChain = Promise.resolve();
                        changed.forEach(function (c) {
                            reloadChain = reloadChain.then(function () { return BX.reloadFileInEditor(c.file); });
                        });
                        return reloadChain.then(function () { performSearch(); });
                    }
                })
                .catch(function (e) {
                    BX.showToast("Replace failed: " + e.message, true);
                })
                .then(function () {
                    $replaceAllBtn.disabled = false;
                    $replaceAllBtn.textContent = "Replace All";
                });
        });

        // ── Welcome: Open Local listeners ───────────────────────
        $welcomeOpenLocal.addEventListener("click", function () {
            $localPath.value = "";
            $localError.classList.add("hidden");
            $localModal.classList.remove("hidden");
            $localPath.focus();
        });
        $localCancel.addEventListener("click", function () { $localModal.classList.add("hidden"); });
        $localModal.querySelector(".welcome-modal-overlay").addEventListener("click", function () { $localModal.classList.add("hidden"); });
        $localOpen.addEventListener("click", submitLocalProject);
        $localPath.addEventListener("keydown", function (e) {
            if (e.key === "Enter") submitLocalProject();
            if (e.key === "Escape") $localModal.classList.add("hidden");
        });

        // ── Welcome: SSH Connect listeners ──────────────────────
        $welcomeSshBtn.addEventListener("click", function () {
            $sshError.classList.add("hidden");
            $sshModal.classList.remove("hidden");
            $sshHost.focus();
        });
        $sshCancel.addEventListener("click", function () { $sshModal.classList.add("hidden"); });
        $sshModal.querySelector(".welcome-modal-overlay").addEventListener("click", function () { $sshModal.classList.add("hidden"); });
        $sshOpen.addEventListener("click", submitSSH);
        $sshDir.addEventListener("keydown", function (e) {
            if (e.key === "Enter") submitSSH();
            if (e.key === "Escape") $sshModal.classList.add("hidden");
        });

        // ── SSH browse listeners ────────────────────────────────
        if ($sshBrowseBtn) {
            $sshBrowseBtn.addEventListener("click", function () {
                if (!$sshBrowseModal) return;
                $sshBrowseModal.classList.remove("hidden");
                sshBrowseCurrentPath = $sshDir.value.trim() || "~";
                loadSshBrowseDir(sshBrowseCurrentPath);
            });
        }
        if ($sshBrowseList) {
            $sshBrowseList.addEventListener("click", function (e) {
                var btn = e.target.closest(".ssh-browse-entry.dir[data-dir]");
                if (btn && btn.dataset.dir) {
                    e.preventDefault();
                    loadSshBrowseDir(btn.dataset.dir);
                }
            });
        }
        if ($sshBrowseBreadcrumb) {
            $sshBrowseBreadcrumb.addEventListener("click", function (e) {
                var btn = e.target.closest(".ssh-browse-up[data-dir], .ssh-browse-seg[data-dir]");
                if (btn && btn.dataset.dir) {
                    e.preventDefault();
                    loadSshBrowseDir(btn.dataset.dir);
                }
            });
        }
        if ($sshBrowseSelect) {
            $sshBrowseSelect.addEventListener("click", function () {
                $sshDir.value = sshBrowseCurrentPath;
                if ($sshBrowseModal) $sshBrowseModal.classList.add("hidden");
            });
        }
        if ($sshBrowseCancel) {
            $sshBrowseCancel.addEventListener("click", function () {
                if ($sshBrowseModal) $sshBrowseModal.classList.add("hidden");
            });
        }
        if ($sshBrowseModal && $sshBrowseModal.querySelector(".welcome-modal-overlay")) {
            $sshBrowseModal.querySelector(".welcome-modal-overlay").addEventListener("click", function () {
                $sshBrowseModal.classList.add("hidden");
            });
        }

        // ── Logo click → back to welcome ────────────────────────
        if ($logoHome) {
            $logoHome.addEventListener("click", showWelcome);
        }

        // ── Boot: decide welcome vs IDE ─────────────────────────
        fetch("/api/info")
            .then(function (res) { return res.json(); })
            .then(function (info) {
                if (info.show_welcome === false) {
                    transitionToIDE(info.working_directory || ".");
                } else {
                    BX.disconnectWs();
                    loadRecentProjects();
                    loadRecentSessions();
                }
            })
            .catch(function () {
                loadRecentProjects();
                loadRecentSessions();
            });
    }

    // ── Exports ─────────────────────────────────────────────────
    BX.openDirModal           = openDirModal;
    BX.closeDirModal          = closeDirModal;
    BX.submitDir              = submitDir;
    BX.performSearch          = performSearch;
    BX.timeAgo                = timeAgo;
    BX.formatAgentOptionLabel = formatAgentOptionLabel;
    BX.loadAgentSessions      = loadAgentSessions;
    BX.createNewAgentSession  = createNewAgentSession;
    BX.loadRecentProjects     = loadRecentProjects;
    BX.loadRecentSessions     = loadRecentSessions;
    BX.parseSshCompositePath  = parseSshCompositePath;
    BX.getSshProjectInfo      = getSshProjectInfo;
    BX.openProject            = openProject;
    BX.transitionToIDE        = transitionToIDE;
    BX.hasUnsavedWork         = hasUnsavedWork;
    BX.showWelcome            = showWelcome;
    BX.submitLocalProject     = submitLocalProject;
    BX.submitSSH              = submitSSH;
    BX.loadSshBrowseDir       = loadSshBrowseDir;
    BX.init                   = init;
    BX.updateModifiedFilesBar_welcome = updateModifiedFilesBar;

    init();
})(window.BX);
