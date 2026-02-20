/* ============================================================
   Bedrock Codex — Utility Functions
   Pure helpers, image handling, file change tracking, UI state
   ============================================================ */
(function (BX) {
    "use strict";

    // DOM ref aliases (immutable — safe to alias)
    var $chatMessages = BX.$chatMessages;
    var $imagePreviewStrip = BX.$imagePreviewStrip;
    var $imageInput = BX.$imageInput;
    var $tokenCount = BX.$tokenCount;
    var $cancelBtn = BX.$cancelBtn;
    var $sendBtn = BX.$sendBtn;
    var $input = BX.$input;
    var $actionBar = BX.$actionBar;
    var $actionBtns = BX.$actionBtns;
    var $statusStrip = BX.$statusStrip;
    var $stickyTodoBar = BX.$stickyTodoBar;
    var $chatSessionStats = BX.$chatSessionStats;
    var $chatSessionTime = BX.$chatSessionTime;
    var $chatSessionTotals = BX.$chatSessionTotals;
    var $chatComposerStats = BX.$chatComposerStats;
    var $chatComposerTotals = BX.$chatComposerTotals;
    var $chatComposerFiles = BX.$chatComposerFiles;
    var $chatComposerFilesDropup = BX.$chatComposerFilesDropup;

    // Reference-type state aliases (safe — same underlying object)
    var pendingImages = BX.pendingImages;
    var fileChangesThisSession = BX.fileChangesThisSession;
    var modifiedFiles = BX.modifiedFiles;

    // ── Smart auto-scroll ──────────────────────────────────────
    $chatMessages.addEventListener("scroll", function () {
        var el = $chatMessages;
        BX._isUserScrolledUp = (el.scrollTop + el.clientHeight < el.scrollHeight - 60);
        var btn = document.getElementById("scroll-to-bottom-btn");
        if (btn) btn.style.display = BX._isUserScrolledUp ? "flex" : "none";
    });
    (function createScrollBtn() {
        var btn = document.createElement("button");
        btn.id = "scroll-to-bottom-btn";
        btn.innerHTML = "&darr;";
        btn.title = "Scroll to bottom";
        btn.style.display = "none";
        btn.addEventListener("click", function () {
            BX._isUserScrolledUp = false;
            $chatMessages.scrollTop = $chatMessages.scrollHeight;
            btn.style.display = "none";
        });
        $chatMessages.parentElement.style.position = "relative";
        $chatMessages.parentElement.appendChild(btn);
    })();

    function escapeHtml(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
    function scrollChat() { if (!BX._isUserScrolledUp) requestAnimationFrame(function () { $chatMessages.scrollTop = $chatMessages.scrollHeight; }); }
    function showToast(text) {
        var t = document.createElement("div"); t.className = "toast"; t.textContent = text;
        document.body.appendChild(t); setTimeout(function () { t.remove(); }, 2200);
    }
    function copyText(text) { navigator.clipboard.writeText(text).then(function () { showToast("Copied"); }, function () { showToast("Copy failed"); }); }
    function makeCopyBtn(getText) {
        var btn = document.createElement("button"); btn.className = "copy-btn"; btn.title = "Copy";
        btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
        btn.addEventListener("click", function (e) { e.stopPropagation(); copyText(typeof getText === "function" ? getText() : getText); btn.classList.add("copied"); setTimeout(function () { btn.classList.remove("copied"); }, 1500); });
        return btn;
    }
    function renderMarkdown(text) {
        if (typeof marked !== "undefined") {
            var html = marked.parse(text);
            return typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(html) : html;
        }
        return escapeHtml(text).replace(/\n/g, "<br>");
    }
    function formatTokens(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n); }
    function updateTokenDisplay(data) {
        if (data.input_tokens !== undefined && data.output_tokens !== undefined) {
            var parts = ["In: " + formatTokens(data.input_tokens), "Out: " + formatTokens(data.output_tokens)];
            if (data.cache_read) parts.push("Cache: " + formatTokens(data.cache_read));
            $tokenCount.textContent = parts.join(" | ");
            $tokenCount.title = "Input: " + (data.input_tokens ? data.input_tokens.toLocaleString() : 0) + " | Output: " + (data.output_tokens ? data.output_tokens.toLocaleString() : 0) + " | Cache read: " + (data.cache_read || 0).toLocaleString();
        } else if (data.tokens !== undefined) {
            $tokenCount.textContent = formatTokens(data.tokens) + " tokens";
        }
        if (data.context_usage_pct !== undefined) {
            var pct = Math.min(data.context_usage_pct, 100);
            var fill = document.getElementById("context-gauge-fill");
            var gauge = document.getElementById("context-gauge");
            if (fill) {
                fill.style.width = pct + "%";
                fill.className = "context-gauge-fill" + (pct > 75 ? " danger" : pct > 50 ? " warn" : "");
            }
            if (gauge) {
                gauge.title = "Context: " + pct + "% used";
                gauge.setAttribute("data-pct", Math.round(pct) + "%");
            }
        }
    }
    function truncate(text, max) { return (!text || text.length <= max) ? text : text.slice(0, max) + "\n... (" + (text.length - max) + " more chars)"; }
    function basename(path) { return path.split("/").pop(); }
    function formatBytes(bytes) {
        if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
        var units = ["B", "KB", "MB", "GB"];
        var size = bytes;
        var i = 0;
        while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
        return size.toFixed(size >= 100 || i === 0 ? 0 : 1) + " " + units[i];
    }
    function mediaTypeFromName(name) {
        var low = String(name || "").toLowerCase();
        if (low.endsWith(".png")) return "image/png";
        if (low.endsWith(".jpg") || low.endsWith(".jpeg")) return "image/jpeg";
        if (low.endsWith(".webp")) return "image/webp";
        if (low.endsWith(".gif")) return "image/gif";
        return "application/octet-stream";
    }
    function imageSrcForMessage(img) {
        if (img && img.previewUrl) return img.previewUrl;
        if (img && img.data && img.media_type) return "data:" + img.media_type + ";base64," + img.data;
        return "";
    }
    function renderImagePreviewStrip() {
        if (!$imagePreviewStrip) return;
        $imagePreviewStrip.innerHTML = "";
        if (pendingImages.length === 0) { $imagePreviewStrip.classList.add("hidden"); return; }
        pendingImages.forEach(function (img) {
            var chip = document.createElement("div");
            chip.className = "image-preview-chip";
            chip.innerHTML = '<img src="' + escapeHtml(img.previewUrl) + '" alt="' + escapeHtml(img.name) + '"><button class="image-preview-remove" title="Remove image" data-id="' + escapeHtml(img.id) + '">\u00d7</button>';
            chip.querySelector(".image-preview-remove").addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); removePendingImage(img.id); });
            $imagePreviewStrip.appendChild(chip);
        });
        var meta = document.createElement("div");
        meta.className = "image-preview-meta";
        var totalBytes = pendingImages.reduce(function (acc, i) { return acc + (i.size || 0); }, 0);
        meta.textContent = pendingImages.length + " image" + (pendingImages.length === 1 ? "" : "s") + " \u2022 " + formatBytes(totalBytes);
        $imagePreviewStrip.appendChild(meta);
        $imagePreviewStrip.classList.remove("hidden");
    }
    function removePendingImage(id) {
        var idx = pendingImages.findIndex(function (x) { return x.id === id; });
        if (idx === -1) return;
        var removed = pendingImages.splice(idx, 1)[0];
        if (removed && removed.previewUrl) URL.revokeObjectURL(removed.previewUrl);
        renderImagePreviewStrip();
    }
    function clearPendingImages() {
        while (pendingImages.length) {
            var img = pendingImages.pop();
            if (img && img.previewUrl) URL.revokeObjectURL(img.previewUrl);
        }
        if ($imageInput) $imageInput.value = "";
        renderImagePreviewStrip();
    }
    function addPendingImageFiles(files) {
        if (!files || files.length === 0) return;
        var MAX_COUNT = 3;
        var MAX_BYTES = 2 * 1024 * 1024;
        for (var i = 0; i < files.length; i++) {
            var file = files[i];
            if (pendingImages.length >= MAX_COUNT) { BX.showInfo("Max " + MAX_COUNT + " images per message."); break; }
            if (!file.type || !file.type.startsWith("image/")) { BX.showInfo("Skipped non-image file: " + file.name); continue; }
            if (file.size > MAX_BYTES) { BX.showInfo("Skipped " + file.name + ": exceeds " + formatBytes(MAX_BYTES) + "."); continue; }
            var previewUrl = URL.createObjectURL(file);
            pendingImages.push({
                id: Date.now() + "-" + Math.random().toString(36).slice(2, 8),
                file: file, previewUrl: previewUrl, name: file.name, size: file.size,
                media_type: file.type || mediaTypeFromName(file.name),
            });
        }
        renderImagePreviewStrip();
    }
    function fileToBase64Data(file) {
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onload = function () {
                var result = String(reader.result || "");
                var comma = result.indexOf(",");
                if (comma < 0) { reject(new Error("Invalid file encoding")); return; }
                resolve(result.slice(comma + 1));
            };
            reader.onerror = function () { reject(new Error("Failed to read image")); };
            reader.readAsDataURL(file);
        });
    }
    function serializePendingImages() {
        var payload = [];
        var chain = Promise.resolve();
        pendingImages.forEach(function (img) {
            chain = chain.then(function () {
                return fileToBase64Data(img.file).then(function (b64) {
                    payload.push({ name: img.name, media_type: img.media_type || mediaTypeFromName(img.name), data: b64, size: img.size || 0 });
                });
            });
        });
        return chain.then(function () { return payload; });
    }

    function langFromExt(ext) {
        var map = {
            js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
            py: "python", rb: "ruby", rs: "rust", go: "go", java: "java",
            c: "c", cpp: "cpp", h: "c", hpp: "cpp", cs: "csharp",
            html: "html", htm: "html", css: "css", scss: "scss", less: "less",
            json: "json", yaml: "yaml", yml: "yaml", toml: "toml",
            md: "markdown", txt: "plaintext", sh: "shell", bash: "shell",
            sql: "sql", xml: "xml", svg: "xml", dockerfile: "dockerfile",
            makefile: "makefile", env: "plaintext", gitignore: "plaintext",
        };
        return map[(ext || "").toLowerCase()] || "plaintext";
    }

    var _updateModifiedFilesDebounce = null;
    function trackFileChange(path, linesAdded, linesDeleted) {
        if (!path) return;
        linesAdded = linesAdded || 0;
        linesDeleted = linesDeleted || 0;
        var current = fileChangesThisSession.get(path) || { edits: 0, deletions: 0 };
        fileChangesThisSession.set(path, { edits: current.edits + linesAdded, deletions: current.deletions + linesDeleted });
        updateFileChangesDropdown();
        // Debounce API calls when agent makes rapid edits to avoid token waste
        clearTimeout(_updateModifiedFilesDebounce);
        _updateModifiedFilesDebounce = setTimeout(function() {
            BX.updateModifiedFilesBar();
        }, 500);
    }
    function untrackFileChange(path) {
        if (!path) return;
        var hadEntry = fileChangesThisSession.has(path);
        fileChangesThisSession.delete(path);
        modifiedFiles.delete(path);
        if (hadEntry) { updateFileChangesDropdown(); BX.updateModifiedFilesBar(); }
    }
    function detectFileDeletesFromBash(command, output) {
        if (!command) return;
        var cmd = command.trim();
        var rmMatch = cmd.match(/\brm\s+(?:-[rfiv]+\s+)*(.+)/);
        if (rmMatch) {
            var targets = rmMatch[1].split(/\s+/).filter(function (t) { return t && !t.startsWith("-"); });
            for (var ti = 0; ti < targets.length; ti++) {
                var cleaned = targets[ti].replace(/["']/g, "");
                fileChangesThisSession.forEach(function (_, trackedPath) {
                    if (trackedPath.endsWith(cleaned) || trackedPath.endsWith("/" + cleaned)) {
                        untrackFileChange(trackedPath);
                    }
                });
            }
        }
    }

    function getFileIcon(path) {
        var ext = path.split('.').pop();
        if (ext) ext = ext.toLowerCase();
        var iconMap = {
            js: "\ud83d\udcc4", jsx: "\u269b\ufe0f", ts: "\ud83d\udcd8", tsx: "\u269b\ufe0f",
            py: "\ud83d\udc0d", java: "\u2615", cpp: "\u26a1", c: "\u26a1",
            html: "\ud83c\udf10", css: "\ud83c\udfa8", scss: "\ud83c\udfa8",
            json: "\ud83d\udccb", xml: "\ud83d\udcc4", md: "\ud83d\udcdd",
            txt: "\ud83d\udcc4", log: "\ud83d\udcdc", yaml: "\u2699\ufe0f", yml: "\u2699\ufe0f",
            png: "\ud83d\uddbc\ufe0f", jpg: "\ud83d\uddbc\ufe0f", jpeg: "\ud83d\uddbc\ufe0f", gif: "\ud83d\uddbc\ufe0f", svg: "\ud83c\udfa8"
        };
        return iconMap[ext] || "\ud83d\udcc4";
    }

    function formatSessionDuration(ms) {
        if (!ms || ms < 0) return "";
        var sec = Math.floor(ms / 1000);
        if (sec < 60) return sec + "s";
        var min = Math.floor(sec / 60);
        if (min < 60) return min + "m";
        var hr = Math.floor(min / 60);
        return hr + "h";
    }

    function updateFileChangesDropdown() {
        var changes = [];
        fileChangesThisSession.forEach(function (stats, path) {
            if (stats.edits > 0 || stats.deletions > 0) changes.push([path, stats]);
        });
        changes.sort(function (a, b) { return a[0].localeCompare(b[0]); });

        var totalAdd = 0, totalDel = 0;
        changes.forEach(function (entry) { totalAdd += entry[1].edits; totalDel += entry[1].deletions; });

        var hasFileEdits = changes.length > 0;
        var showHeaderStats = BX.sessionStartTime || hasFileEdits;
        var elapsed = BX.sessionStartTime ? Date.now() - BX.sessionStartTime : 0;
        var timeStr = formatSessionDuration(elapsed);

        if ($chatSessionStats) {
            if (showHeaderStats) {
                $chatSessionStats.classList.remove("hidden");
                if ($chatSessionTime) $chatSessionTime.textContent = timeStr;
                if ($chatSessionTotals) {
                    var addEl = $chatSessionTotals.querySelector(".add");
                    var delEl = $chatSessionTotals.querySelector(".del");
                    if (addEl) addEl.textContent = "+" + totalAdd;
                    if (delEl) delEl.textContent = "\u2212" + totalDel;
                }
            } else { $chatSessionStats.classList.add("hidden"); }
        }

        // Composer stats pill + dropup are now driven by updateModifiedFilesBar()
        // which fetches real git diff data. Just ensure strip visibility is updated.
        updateStripVisibility();
    }

    function updateStripVisibility() {
        if (!$statusStrip) return;
        var todoVisible = $stickyTodoBar && !$stickyTodoBar.classList.contains("hidden");
        // Note: chat-composer-stats (file edit counts) are now permanently hidden
        var filesVisible = false;
        $statusStrip.classList.toggle("hidden", !todoVisible && !filesVisible);
    }

    function setRunning(running) {
        BX.isRunning = running;
        if ($cancelBtn) $cancelBtn.classList.toggle("hidden", !running);
        if ($sendBtn) $sendBtn.classList.toggle("hidden", false);
        if ($input) {
            $input.disabled = false;
            if (running) {
                $input.placeholder = "Guide the agent\u2026 (Enter to send correction)";
                var wrapper = $input.closest(".chat-input-wrapper");
                if (wrapper) wrapper.classList.add("guidance-mode");
            } else {
                $input.placeholder = "Ask anything\u2026 (@ to mention files) (Enter to send)";
                var wrapper2 = $input.closest(".chat-input-wrapper");
                if (wrapper2) wrapper2.classList.remove("guidance-mode");
                $input.focus();
            }
        }
    }

    function showActionBar(buttons) {
        $actionBtns.innerHTML = "";
        buttons.forEach(function (b) {
            var btn = document.createElement("button");
            btn.className = "action-btn " + b.cls;
            btn.textContent = b.label;
            btn.addEventListener("click", b.onClick);
            $actionBtns.appendChild(btn);
        });
        $actionBar.classList.remove("hidden");
    }
    function hideActionBar() { $actionBar.classList.add("hidden"); $actionBtns.innerHTML = ""; }

    // ── Export to BX ───────────────────────────────────────────
    BX.escapeHtml = escapeHtml;
    BX.scrollChat = scrollChat;
    BX.showToast = showToast;
    BX.copyText = copyText;
    BX.makeCopyBtn = makeCopyBtn;
    BX.renderMarkdown = renderMarkdown;
    BX.formatTokens = formatTokens;
    BX.updateTokenDisplay = updateTokenDisplay;
    BX.truncate = truncate;
    BX.basename = basename;
    BX.formatBytes = formatBytes;
    BX.mediaTypeFromName = mediaTypeFromName;
    BX.imageSrcForMessage = imageSrcForMessage;
    BX.renderImagePreviewStrip = renderImagePreviewStrip;
    BX.removePendingImage = removePendingImage;
    BX.clearPendingImages = clearPendingImages;
    BX.addPendingImageFiles = addPendingImageFiles;
    BX.fileToBase64Data = fileToBase64Data;
    BX.serializePendingImages = serializePendingImages;
    BX.langFromExt = langFromExt;
    BX.trackFileChange = trackFileChange;
    BX.untrackFileChange = untrackFileChange;
    BX.detectFileDeletesFromBash = detectFileDeletesFromBash;
    BX.getFileIcon = getFileIcon;
    BX.formatSessionDuration = formatSessionDuration;
    BX.updateFileChangesDropdown = updateFileChangesDropdown;
    BX.updateStripVisibility = updateStripVisibility;
    BX.setRunning = setRunning;
    BX.showActionBar = showActionBar;
    BX.hideActionBar = hideActionBar;

})(window.BX);
