/* ============================================================
   Bedrock Codex — Shared State & DOM Refs
   Sets up the BX namespace used by all module files.
   ============================================================ */
(function () {
    "use strict";
    var BX = window.BX = {};

    // ── DOM refs — Welcome Screen ──────────────────────────────
    BX.$welcomeScreen    = document.getElementById("welcome-screen");
    BX.$ideWrapper       = document.getElementById("ide-wrapper");
    BX.$welcomeOpenLocal = document.getElementById("welcome-open-local");
    BX.$welcomeSshBtn    = document.getElementById("welcome-ssh-connect");
    BX.$projectList      = document.getElementById("welcome-project-list");
    BX.$localModal       = document.getElementById("welcome-local-modal");
    BX.$localPath        = document.getElementById("welcome-local-path");
    BX.$localError       = document.getElementById("welcome-local-error");
    BX.$localOpen        = document.getElementById("welcome-local-open");
    BX.$localCancel      = document.getElementById("welcome-local-cancel");
    BX.$sshModal         = document.getElementById("welcome-ssh-modal");
    BX.$sshHost          = document.getElementById("ssh-host");
    BX.$sshUser          = document.getElementById("ssh-user");
    BX.$sshPort          = document.getElementById("ssh-port");
    BX.$sshKey           = document.getElementById("ssh-key");
    BX.$sshDir           = document.getElementById("ssh-dir");
    BX.$sshError         = document.getElementById("welcome-ssh-error");
    BX.$sshOpen          = document.getElementById("welcome-ssh-open");
    BX.$sshCancel        = document.getElementById("welcome-ssh-cancel");
    BX.$sshBrowseBtn     = document.getElementById("ssh-browse-btn");
    BX.$sshBrowseModal   = document.getElementById("ssh-browse-modal");
    BX.$sshBrowseList    = document.getElementById("ssh-browse-list");
    BX.$sshBrowseBreadcrumb = document.getElementById("ssh-browse-breadcrumb");
    BX.$sshBrowseCurrent = document.getElementById("ssh-browse-current");
    BX.$sshBrowseSelect  = document.getElementById("ssh-browse-select");
    BX.$sshBrowseCancel  = document.getElementById("ssh-browse-cancel");

    // ── DOM refs — IDE ─────────────────────────────────────────
    BX.$fileTree      = document.getElementById("file-tree");
    BX.$fileFilter    = document.getElementById("file-filter-input");
    BX.$refreshTree   = document.getElementById("refresh-tree-btn");
    BX.$tabBar        = document.getElementById("tab-bar");
    BX.$editorWelcome = document.getElementById("editor-welcome");
    BX.$monacoEl      = document.getElementById("monaco-container");
    BX.$chatMessages  = document.getElementById("chat-messages");
    BX.$input         = document.getElementById("user-input");
    BX.$attachImageBtn = document.getElementById("attach-image-btn");
    BX.$imageInput    = document.getElementById("image-input");
    BX.$imagePreviewStrip = document.getElementById("image-preview-strip");
    BX.$sendBtn       = document.getElementById("send-btn");
    BX.$cancelBtn     = document.getElementById("cancel-btn");
    BX.$actionBar     = document.getElementById("action-bar");
    BX.$actionBtns    = document.getElementById("action-buttons");
    BX.$modelName     = document.getElementById("model-name");
    BX.$tokenCount    = document.getElementById("token-count");
    BX.$connStatus    = document.getElementById("connection-status");
    BX.$conversationTitle = document.getElementById("conversation-title");
    BX.$agentSelect   = document.getElementById("agent-select");
    BX.$newAgentBtn   = document.getElementById("new-agent-btn");
    BX.$workingDir    = document.getElementById("working-dir");
    BX.$resetBtn      = document.getElementById("reset-btn");
    BX.$chatSessionStats = document.getElementById("chat-session-stats");
    BX.$chatSessionTime = document.getElementById("chat-session-time");
    BX.$chatSessionTotals = document.getElementById("chat-session-totals");
    BX.$chatMenuBtn = document.getElementById("chat-menu-btn");
    BX.$chatMenuDropdown = document.getElementById("chat-menu-dropdown");
    BX.$statusStrip = document.getElementById("status-strip");
    BX.$stickyTodoBar = document.getElementById("sticky-todo-bar");
    BX.$stickyTodoToggle = document.getElementById("sticky-todo-toggle");
    BX.$stickyTodoCount = document.getElementById("sticky-todo-count");
    BX.$stickyTodoList = document.getElementById("sticky-todo-list");
    BX.$stickyTodoDropdown = document.getElementById("sticky-todo-dropdown");
    BX.$stickyTodoAddInput = document.getElementById("sticky-todo-add-input");
    BX.$stickyTodoAddBtn = document.getElementById("sticky-todo-add-btn");
    BX.$chatComposerStats = document.getElementById("chat-composer-stats");
    BX.$chatComposerStatsToggle = document.getElementById("chat-composer-stats-toggle");
    BX.$chatComposerFilesDropup = document.getElementById("chat-composer-files-dropup");
    BX.$chatComposerTotals = document.getElementById("chat-composer-totals");
    BX.$chatComposerFiles = document.getElementById("chat-composer-files");
    BX.$openBtn       = document.getElementById("open-project-btn");
    BX.$logoHome      = document.getElementById("logo-home");
    BX.$searchToggle  = document.getElementById("search-toggle-btn");
    BX.$searchPanel   = document.getElementById("search-panel");
    BX.$searchInput   = document.getElementById("search-input");
    BX.$searchInclude = document.getElementById("search-include");
    BX.$searchGoBtn   = document.getElementById("search-go-btn");
    BX.$searchResults = document.getElementById("search-results");
    BX.$searchStatus  = document.getElementById("search-status");
    BX.$searchRegex   = document.getElementById("search-regex-btn");
    BX.$searchCase    = document.getElementById("search-case-btn");
    BX.$replaceRow    = document.getElementById("replace-row");
    BX.$replaceInput  = document.getElementById("replace-input");
    BX.$replaceAllBtn = document.getElementById("replace-all-btn");
    BX.$replaceToggle = document.getElementById("search-replace-toggle");
    BX.$saveFileBtn = document.getElementById("save-file-btn");
    BX.$terminalToggleBtn = document.getElementById("terminal-toggle-btn");
    BX.$terminalPanel = document.getElementById("terminal-panel");
    BX.$terminalXtermContainer = document.getElementById("terminal-xterm-container");
    BX.$terminalCloseBtn = document.getElementById("terminal-close-btn");
    BX.$terminalClearBtn = document.getElementById("terminal-clear-btn");
    BX.$resizeTerminal = document.getElementById("resize-terminal");
    BX.$sourceControlList = document.getElementById("source-control-list");
    BX.$sourceControlRefreshBtn = document.getElementById("source-control-refresh-btn");
    BX.$modifiedFilesBar = document.getElementById("modified-files-bar");
    BX.$modifiedFilesToggle = document.getElementById("modified-files-toggle");
    BX.$modifiedFilesList = document.getElementById("modified-files-list");
    BX.$modifiedFilesDropdown = document.getElementById("modified-files-dropdown");

    // ── Mutable state ──────────────────────────────────────────
    BX.terminalXterm = null;
    BX.terminalFitAddon = null;
    BX.terminalWs = null;
    BX.terminalFocusInput = null;
    BX.terminalOutputBuffer = "";
    BX.terminalFlushRaf = 0;
    BX.terminalBlobQueue = [];
    BX.terminalBlobProcessing = false;

    BX.ws = null;
    BX.isRunning = false;
    BX.isReplaying = true;
    BX.monacoInstance = null;
    BX.diffEditorInstance = null;
    BX.activeTab = null;
    BX.openTabs = new Map();
    BX.modifiedFiles = new Set();
    BX.gitStatus = new Map();
    BX.fileChangesThisSession = new Map();
    BX.sessionCumulativeStats = new Map();
    BX.currentThinkingEl = null;
    BX.currentTextEl = null;
    BX.currentTextBuffer = "";
    BX._textDirty = false;
    BX._textRenderRAF = null;
    BX.lastToolBlock = null;
    BX.toolRunById = new Map();
    BX.scoutEl = null;
    BX.pendingImages = [];
    BX.currentSessionId = null;
    BX.sessionStartTime = null;
    BX.suppressAgentSwitch = false;
    BX._isUserScrolledUp = false;
    BX._reconnectAttempts = 0;

    // ── Session persistence ────────────────────────────────────
    BX.BEDROCK_SESSION_KEY = "bedrock_session_" + (location.host || "default");
    BX.persistSessionId = function (id) {
        try {
            if (id) localStorage.setItem(BX.BEDROCK_SESSION_KEY, id);
            else localStorage.removeItem(BX.BEDROCK_SESSION_KEY);
        } catch (_) {}
    };
    BX.loadPersistedSessionId = function () {
        try { return localStorage.getItem(BX.BEDROCK_SESSION_KEY) || null; }
        catch (_) { return null; }
    };

    // ── Markdown setup ─────────────────────────────────────────
    if (typeof marked !== "undefined") {
        var markedOpts = { breaks: true, gfm: true };
        if (typeof marked.use === "function") {
            // marked v5+ — use extensions API for highlight
            marked.use({
                ...markedOpts,
                extensions: typeof hljs !== "undefined" ? [{
                    name: "code",
                    renderer: function (token) {
                        var lang = (token.lang || "").trim();
                        if (lang && hljs.getLanguage(lang)) {
                            try { return '<pre><code class="hljs language-' + lang + '">' + hljs.highlight(token.text, { language: lang }).value + '</code></pre>'; } catch (e) {}
                        }
                        return false; // fall back to default
                    }
                }] : []
            });
        } else {
            // marked v4 and earlier — legacy highlight option
            markedOpts.highlight = function (code, lang) {
                if (typeof hljs !== "undefined" && lang && hljs.getLanguage(lang))
                    try { return hljs.highlight(code, { language: lang }).value; } catch (e) {}
                return code;
            };
            marked.setOptions(markedOpts);
        }
    }
})();
