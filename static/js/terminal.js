/* ============================================================
   Bedrock Codex — terminal.js
   Integrated terminal (xterm.js + PTY WebSocket)
   ============================================================ */
(function (BX) {
    "use strict";

    // DOM ref aliases (immutable — safe to alias)
    var $terminalPanel = BX.$terminalPanel;
    var $terminalXtermContainer = BX.$terminalXtermContainer;
    var $terminalToggleBtn = BX.$terminalToggleBtn;
    var $terminalCloseBtn = BX.$terminalCloseBtn;
    var $terminalClearBtn = BX.$terminalClearBtn;
    var $resizeTerminal = BX.$resizeTerminal;

    var TERMINAL_DEFAULT_HEIGHT = 220;
    var TERMINAL_MIN_HEIGHT = 100;

    function terminalFlushOutput() {
        BX.terminalFlushRaf = 0;
        if (BX.terminalOutputBuffer.length && BX.terminalXterm) {
            BX.terminalXterm.write(BX.terminalOutputBuffer);
            BX.terminalOutputBuffer = "";
        }
    }

    function terminalScheduleFlush() {
        if (BX.terminalFlushRaf) return;
        BX.terminalFlushRaf = requestAnimationFrame(terminalFlushOutput);
    }

    function terminalProcessNextBlob(wsRef) {
        if (BX.terminalBlobProcessing || BX.terminalBlobQueue.length === 0 || BX.terminalWs !== wsRef) return;
        BX.terminalBlobProcessing = true;
        var blob = BX.terminalBlobQueue.shift();
        blob.arrayBuffer().then(function (buf) {
            if (BX.terminalWs === wsRef) {
                BX.terminalOutputBuffer += new TextDecoder().decode(buf);
                terminalScheduleFlush();
            }
            BX.terminalBlobProcessing = false;
            if (BX.terminalBlobQueue.length > 0) terminalProcessNextBlob(wsRef);
        }).catch(function () {
            BX.terminalBlobProcessing = false;
            if (BX.terminalBlobQueue.length > 0) terminalProcessNextBlob(wsRef);
        });
    }

    function terminalDisconnect(clearDisplay) {
        if (BX.terminalWs) {
            try { BX.terminalWs.close(); } catch (e) {}
            BX.terminalWs = null;
            setTerminalStatus("", "");
        }
        if (BX.terminalFlushRaf) {
            cancelAnimationFrame(BX.terminalFlushRaf);
            BX.terminalFlushRaf = 0;
        }
        BX.terminalOutputBuffer = "";
        BX.terminalBlobQueue = [];
        if (clearDisplay && BX.terminalXterm) {
            BX.terminalXterm.clear();
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
        if (BX.terminalWs && BX.terminalWs.readyState === WebSocket.CONNECTING) return;
        terminalDisconnect();
        setTerminalStatus("Connecting…", "");
        var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        var wsUrl = protocol + "//" + window.location.host + "/ws/terminal";
        var ws = new WebSocket(wsUrl);
        BX.terminalWs = ws;

        ws.onopen = function () {};
        ws.onmessage = function (ev) {
            if (ev.data instanceof Blob) {
                BX.terminalBlobQueue.push(ev.data);
                terminalProcessNextBlob(ws);
                return;
            }
            try {
                var obj = JSON.parse(ev.data);
                if (obj.type === "error") {
                    if (BX.terminalXterm && BX.terminalWs === ws) {
                        var msg = obj.message || obj.content || "Connection error.";
                        BX.terminalXterm.writeln("\r\n\u001b[31mTerminal: " + msg + "\u001b[0m");
                    }
                    terminalDisconnect();
                } else if (obj.type === "ready" && BX.terminalFitAddon && BX.terminalXterm && BX.terminalWs === ws) {
                    setTerminalStatus("Connected", "connected");
                    requestAnimationFrame(function () {
                        BX.terminalFitAddon.fit();
                        terminalSendResize(BX.terminalXterm.rows, BX.terminalXterm.cols);
                        setTimeout(function () { if (BX.terminalFocusInput) BX.terminalFocusInput(); }, 50);
                    });
                }
            } catch (e) {}
        };
        ws.onclose = function () {
            if (BX.terminalWs === ws) { BX.terminalWs = null; setTerminalStatus("Disconnected", ""); }
        };
        ws.onerror = function () {
            setTerminalStatus("Error", "error");
            if (BX.terminalXterm && BX.terminalWs === ws) {
                BX.terminalXterm.writeln("\r\n\u001b[31mConnection failed. Open a local project for full terminal.\u001b[0m");
            }
            terminalDisconnect();
        };
    }

    function terminalInit() {
        if (!window.Terminal || !$terminalXtermContainer) return;
        if (BX.terminalXterm) return;
        $terminalXtermContainer.innerHTML = "";
        var term = new window.Terminal({
            cursorBlink: true, fontSize: 12, fontFamily: "ui-monospace, monospace",
            theme: { background: "#1a1a2e", foreground: "#e8eaf0", cursor: "#6c9fff", cursorAccent: "#1a1a2e", selectionBackground: "#264f78" },
        });
        var FitAddonCtor = window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon);
        var fitAddon = FitAddonCtor ? new FitAddonCtor() : null;
        if (fitAddon) term.loadAddon(fitAddon);
        term.open($terminalXtermContainer);
        BX.terminalXterm = term;
        BX.terminalFitAddon = fitAddon;

        function terminalSendKeys(data) {
            if (BX.terminalWs && BX.terminalWs.readyState === WebSocket.OPEN) BX.terminalWs.send(data);
        }

        var bodyWrap = document.getElementById("terminal-body-wrap");
        BX.terminalFocusInput = function () { if (bodyWrap) bodyWrap.focus(); };

        if ($terminalPanel) {
            $terminalPanel.addEventListener("mousedown", function focusTerminalOnClick(e) {
                if (e.target.closest && e.target.closest("button")) return;
                if ($terminalPanel.contains(e.target)) { e.preventDefault(); if (bodyWrap) bodyWrap.focus(); }
            });
            $terminalPanel.addEventListener("keydown", function terminalPanelKeydown(e) {
                var panelHasFocus = $terminalPanel.contains(document.activeElement);
                var targetInPanel = e.target && $terminalPanel.contains(e.target);
                var panelVisible = $terminalPanel && !$terminalPanel.classList.contains("hidden");
                if (!panelVisible || (!panelHasFocus && !targetInPanel)) return;
                if (targetInPanel && !panelHasFocus && bodyWrap) bodyWrap.focus();
                var key = e.key;
                var toSend = null;
                if (key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { toSend = key; }
                else if (e.ctrlKey && !e.metaKey && !e.altKey) {
                    if (key === "c") { var sel = (BX.terminalXterm && typeof BX.terminalXterm.getSelection === "function") ? BX.terminalXterm.getSelection() : ""; if (sel && sel.length > 0) return; toSend = "\x03"; }
                    else if (key === "d") { toSend = "\x04"; }
                    else if (key === "z") { toSend = "\x1a"; } else if (key === "l") { toSend = "\x0c"; }
                    else if (key === "a") { toSend = "\x01"; } else if (key === "e") { toSend = "\x05"; }
                    else if (key === "k") { toSend = "\x0b"; } else if (key === "u") { toSend = "\x15"; }
                    else if (key === "w") { toSend = "\x17"; } else if (key === "\\") { toSend = "\x1c"; }
                    else if (key >= "a" && key <= "z") { toSend = String.fromCharCode(key.charCodeAt(0) - 96); }
                    else if (key >= "@" && key <= "_") { toSend = String.fromCharCode(key.charCodeAt(0) - 64); }
                }
                else if (key === "Enter") { toSend = "\r"; } else if (key === "Backspace") { toSend = "\x7f"; }
                else if (key === "Tab") { toSend = "\t"; } else if (key === "Escape") { toSend = "\x1b"; }
                else if (key === "ArrowUp") { toSend = "\x1b[A"; } else if (key === "ArrowDown") { toSend = "\x1b[B"; }
                else if (key === "ArrowRight") { toSend = "\x1b[C"; } else if (key === "ArrowLeft") { toSend = "\x1b[D"; }
                if (toSend !== null) {
                    e.preventDefault(); e.stopPropagation();
                    if (BX.terminalWs && BX.terminalWs.readyState === WebSocket.OPEN) terminalSendKeys(toSend);
                    else if (BX.terminalXterm) BX.terminalXterm.writeln("\r\n\u001b[33mTerminal not connected. Open a project first.\u001b[0m");
                }
            }, true);
            $terminalPanel.addEventListener("paste", function terminalPanelPaste(e) {
                var panelVisible = $terminalPanel && !$terminalPanel.classList.contains("hidden");
                var inPanel = $terminalPanel.contains(document.activeElement) || (e.target && $terminalPanel.contains(e.target));
                if (!bodyWrap || !panelVisible || !inPanel) return;
                e.preventDefault();
                var text = (e.clipboardData || window.clipboardData).getData("text");
                if (text && BX.terminalWs && BX.terminalWs.readyState === WebSocket.OPEN) terminalSendKeys(text);
                else if (text && BX.terminalXterm) BX.terminalXterm.writeln("\r\n\u001b[33mTerminal not connected. Open a project first.\u001b[0m");
            }, true);
        }

        terminalConnect();
        term.onBinary(function (data) { if (BX.terminalWs && BX.terminalWs.readyState === WebSocket.OPEN) BX.terminalWs.send(data); });
    }

    function terminalSendResize(rows, cols) {
        if (BX.terminalWs && BX.terminalWs.readyState === WebSocket.OPEN) BX.terminalWs.send(JSON.stringify({ resize: [rows, cols] }));
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
                if (BX.terminalFitAddon) { BX.terminalFitAddon.fit(); if (BX.terminalXterm) terminalSendResize(BX.terminalXterm.rows, BX.terminalXterm.cols); }
                if (BX.terminalXterm && (!BX.terminalWs || BX.terminalWs.readyState !== WebSocket.OPEN)) terminalConnect();
                if (BX.terminalFocusInput) BX.terminalFocusInput();
                setTimeout(function () { if (BX.terminalFocusInput) BX.terminalFocusInput(); }, 50);
                setTimeout(function () { if (BX.terminalFocusInput) BX.terminalFocusInput(); }, 300);
                if (BX.monacoInstance) BX.monacoInstance.layout();
                if (BX.diffEditorInstance) BX.diffEditorInstance.layout();
            });
        } else {
            $terminalPanel.classList.add("hidden");
            $resizeTerminal.classList.add("hidden");
            if ($terminalToggleBtn) $terminalToggleBtn.classList.remove("active");
            terminalDisconnect();
        }
        requestAnimationFrame(function () {
            if (BX.monacoInstance) BX.monacoInstance.layout();
            if (BX.diffEditorInstance) BX.diffEditorInstance.layout();
        });
    }

    function toggleTerminalPanel() {
        var isHidden = $terminalPanel && $terminalPanel.classList.contains("hidden");
        setTerminalPanelVisible(!!isHidden);
    }

    if ($terminalToggleBtn) $terminalToggleBtn.addEventListener("click", toggleTerminalPanel);
    if ($terminalCloseBtn) $terminalCloseBtn.addEventListener("click", function () { setTerminalPanelVisible(false); });
    if ($terminalClearBtn) $terminalClearBtn.addEventListener("click", function () { if (BX.terminalXterm) BX.terminalXterm.clear(); });

    // Terminal resize handle
    if ($resizeTerminal && $terminalPanel) {
        $resizeTerminal.addEventListener("mousedown", function (e) {
            e.preventDefault();
            var startY = e.clientY;
            var startHeight = $terminalPanel.offsetHeight;
            $resizeTerminal.classList.add("dragging");
            document.body.style.cursor = "row-resize";
            document.body.style.userSelect = "none";
            function onMove(ev) {
                var dy = ev.clientY - startY;
                var newHeight = Math.max(TERMINAL_MIN_HEIGHT, startHeight - dy);
                $terminalPanel.style.height = newHeight + "px";
                $terminalPanel.dataset.height = newHeight;
                if (BX.monacoInstance) BX.monacoInstance.layout();
                if (BX.diffEditorInstance) BX.diffEditorInstance.layout();
                if (BX.terminalFitAddon) {
                    BX.terminalFitAddon.fit();
                    if (BX.terminalXterm && BX.terminalWs && BX.terminalWs.readyState === WebSocket.OPEN) terminalSendResize(BX.terminalXterm.rows, BX.terminalXterm.cols);
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
        if (BX.terminalFitAddon && $terminalPanel && !$terminalPanel.classList.contains("hidden")) {
            BX.terminalFitAddon.fit();
            if (BX.terminalXterm && BX.terminalWs && BX.terminalWs.readyState === WebSocket.OPEN) terminalSendResize(BX.terminalXterm.rows, BX.terminalXterm.cols);
        }
    });

    // ── Exports ──────────────────────────────────────────────
    BX.terminalFlushOutput = terminalFlushOutput;
    BX.terminalScheduleFlush = terminalScheduleFlush;
    BX.terminalProcessNextBlob = terminalProcessNextBlob;
    BX.terminalDisconnect = terminalDisconnect;
    BX.setTerminalStatus = setTerminalStatus;
    BX.terminalConnect = terminalConnect;
    BX.terminalInit = terminalInit;
    BX.terminalSendResize = terminalSendResize;
    BX.setTerminalPanelVisible = setTerminalPanelVisible;
    BX.toggleTerminalPanel = toggleTerminalPanel;

})(window.BX);
