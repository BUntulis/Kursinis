/**
 * Agent workbench — the right-hand IDE-style panel of the benchmark execution page.
 *
 * ARCHITECTURE (see the long-form notes at the bottom of this file):
 *
 *   AgentWorkbench.create(root)
 *     ├─ Store        coarse *reactive flags* (tab visibility, active tab, sandbox phase)
 *     ├─ Bus          granular *change events* (files:add, files:content, preview:state, …)
 *     ├─ model        normalized entity state (files Map, resource buffer, preview, …)
 *     ├─ Ingestor     pure translation of polling payloads → model mutations + events
 *     └─ RightPanel   tab orchestration + lazy mounting
 *          ├─ FilesPanel       file tree + streaming line-diff viewer   (isolated)
 *          ├─ ResourcesPanel   CPU/GPU/RAM/VRAM/temp/throughput + stats  (isolated)
 *          └─ PreviewPanel     sandbox lifecycle + iframe                (isolated)
 *
 * The page poller (app.js) never touches right-panel DOM. It only feeds the workbench:
 *   workbench.setRun(ctx) · ingestLogs(data) · ingestResources(data) · ingestStatus(data)
 *
 * Two complementary reactivity channels keep rerenders surgical:
 *   • Store  — selector subscriptions fire only when a *scalar flag* changes (Object.is).
 *              Used for visibility / which-tab decisions. Never carries entity payloads.
 *   • Bus    — fine-grained "this one thing changed" events carrying a path/sample/etc.
 *              Panels patch exactly the affected DOM node; no innerHTML blow-away.
 */
(function () {
  "use strict";

  // =====================================================================
  // 0. Utilities (self-contained — no dependency on app.js)
  // =====================================================================
  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  var LANG_BY_EXT = {
    py: "python", html: "xml", htm: "xml", css: "css", js: "javascript",
    json: "json", yaml: "yaml", yml: "yaml", md: "markdown", toml: "ini",
    ini: "ini", cfg: "ini", txt: "plaintext",
  };
  function extOf(path) {
    return (String(path).split(".").pop() || "").toLowerCase();
  }
  function langFromPath(path) {
    return LANG_BY_EXT[extOf(path)] || "plaintext";
  }

  function byteLength(text) {
    try {
      return new TextEncoder().encode(text || "").length;
    } catch (e) {
      return (text || "").length;
    }
  }

  function splitLines(content) {
    // Trim one trailing newline so a file does not render a phantom blank last line.
    return String(content == null ? "" : content)
      .replace(/\r\n/g, "\n")
      .replace(/\n$/, "")
      .split("\n");
  }

  function arraysEqual(a, b) {
    if (a === b) return true;
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  function highlightLine(text, lang) {
    // Per-line highlighting keeps diff patching cheap (we re-tokenize only the changed
    // lines, never the whole document). Trade-off: a token spanning multiple physical
    // lines — e.g. a Python triple-quoted string — is highlighted line-locally, so its
    // colouring may be imperfect. Acceptable for a live generation view; correctness of
    // text is never affected because we fall back to escaped text on any hljs error.
    if (text === "") return "";
    if (window.hljs && lang && lang !== "plaintext") {
      try {
        return window.hljs.highlight(text, { language: lang, ignoreIllegals: true }).value || escapeHtml(text);
      } catch (e) {
        return escapeHtml(text);
      }
    }
    return escapeHtml(text);
  }

  // The coder writes files as fenced blocks whose first line is `# <relative_path>`.
  // This is the only "stream" the polling backend exposes, so we mine generated content
  // straight from the Coder's stdout (available before the file is even committed).
  function extractFileWrites(stdout) {
    var fence = /```[a-zA-Z0-9_+\-]*\n([\s\S]*?)```/g;
    var out = [];
    var match;
    while ((match = fence.exec(stdout)) !== null) {
      var body = match[1];
      var nl = body.indexOf("\n");
      var firstLine = (nl >= 0 ? body.slice(0, nl) : body).trim();
      var pathMatch = firstLine.match(/^#\s*([\w./-]+\.\w+)$/);
      if (!pathMatch) continue;
      var content = nl >= 0 ? body.slice(nl + 1) : "";
      out.push({ path: pathMatch[1], content: content.replace(/\s+$/, ""), lang: langFromPath(pathMatch[1]) });
    }
    return out;
  }

  function isRunnablePath(path) {
    return path === "manage.py" || path.endsWith("/manage.py");
  }

  // =====================================================================
  // 1. Event bus (mitt-style) — granular "what changed" notifications
  // =====================================================================
  function createBus() {
    var map = new Map();
    return {
      on: function (type, cb) {
        if (!map.has(type)) map.set(type, new Set());
        map.get(type).add(cb);
        return function () {
          var set = map.get(type);
          if (set) set.delete(cb);
        };
      },
      emit: function (type, payload) {
        var set = map.get(type);
        if (!set) return;
        set.forEach(function (cb) {
          try { cb(payload); } catch (e) { /* one listener's failure must not break the rest */ }
        });
      },
      clear: function () { map.clear(); },
    };
  }

  // =====================================================================
  // 2. Store — immutable shallow merge + per-selector subscriptions.
  //    A subscriber only runs when its selected slice changes by Object.is,
  //    which is what prevents needless work / cross-tab rerenders.
  // =====================================================================
  function createStore(initial) {
    var state = initial;
    var subs = new Set();
    var queue = [];
    var draining = false;

    function applyPatch(patch) {
      var next = typeof patch === "function" ? patch(state) : patch;
      if (!next) return false;
      var merged = Object.assign({}, state, next);
      var changed = false;
      for (var k in next) {
        if (!Object.is(state[k], merged[k])) { changed = true; break; }
      }
      state = merged;
      return changed;
    }

    function notify() {
      subs.forEach(function (entry) {
        var sel = entry.selector(state);
        if (!Object.is(sel, entry.last)) {
          entry.last = sel;
          try { entry.cb(sel, state); } catch (e) { /* isolate */ }
        }
      });
    }

    // Re-entrancy-safe: a subscriber that calls setState during notify() does NOT
    // mutate the snapshot the in-flight loop is iterating. Its patch is queued and
    // applied (then notified) after the current pass settles, so every subscriber
    // always sees a consistent state. Updates are serialized in call order.
    function setState(patch) {
      queue.push(patch);
      if (draining) return;
      draining = true;
      try {
        while (queue.length) {
          if (applyPatch(queue.shift())) notify();
        }
      } finally {
        draining = false;
      }
    }
    function subscribe(selector, cb) {
      var entry = { selector: selector, cb: cb, last: selector(state) };
      subs.add(entry);
      return function () { subs.delete(entry); };
    }
    return {
      getState: function () { return state; },
      setState: setState,
      subscribe: subscribe,
    };
  }

  // =====================================================================
  // 3. The workbench instance
  // =====================================================================
  function create(root) {
    if (!root) return null;

    var bus = createBus();
    var store = createStore({
      filesAvailable: false,
      previewAvailable: false,
      databaseAvailable: false,
      tasksAvailable: false,
      benchmarksAvailable: false,
      // The Resources tab is opt-in (hidden until the user enables it; remembered per chat). Seeded
      // from localStorage in setRun once the chat id is known.
      resourcesEnabled: false,
      // Loose (standalone "New chat") workspaces keep the right panel empty until a preview is ready —
      // Files/Tasks/Database tabs stay hidden until then. Set from setRun.
      loose: false,
      activeTab: "empty",
      userPickedTab: false,
    });

    // --- Normalized entity state. Mutated only by the ingestor; broadcast via the bus.
    var model = {
      run: null, // { id, fileUrl, previewUrlTemplate, csrfToken }
      files: new Map(), // path -> { path, type, sizeBytes, content|null, source:'log'|'index' }
      order: [], // [path] kept sorted for the tree
      lastModelLogId: 0,
      preview: null, // { status, url, error }
      tasks: [], // [{ content, status }] — model-maintained todo list
      resources: { samples: [], lastSampleId: 0, modelStats: null, benchmark: null },
    };

    // -----------------------------------------------------------------
    // 3a. Ingestor — the ONLY writer of `model`. Pure translation of API
    //     payloads into entity mutations + granular bus events + flags.
    // -----------------------------------------------------------------
    function sortedInsert(path) {
      // keep model.order sorted so tree insertion is deterministic
      var lo = 0, hi = model.order.length;
      while (lo < hi) {
        var mid = (lo + hi) >> 1;
        if (model.order[mid] < path) lo = mid + 1; else hi = mid;
      }
      model.order.splice(lo, 0, path);
      return lo;
    }

    // Emit the full write lifecycle for a file whose content we have. The right-panel
    // FilesPanel consumes the internal `files:*` events; the chat timeline consumes the
    // canonical `file:*` lifecycle events (carrying the originating coder log id so the
    // chat can attach the embedded code card to the right step). Because polling hands us
    // the whole grown content per tick, start/chunk/end fire together and the CONSUMERS
    // animate the reveal — there is no token stream from the backend.
    // Cheap line-diff counts (common prefix/suffix) so the chat can show "+N −M".
    function lineCounts(prev, next) {
      var o = prev ? prev.replace(/\n$/, "").split("\n") : [];
      var n = next ? next.replace(/\n$/, "").split("\n") : [];
      if (!prev) return { added: n.length, removed: 0 };
      var s = 0, min = Math.min(o.length, n.length);
      while (s < min && o[s] === n[s]) s++;
      var eo = o.length, en = n.length;
      while (eo > s && en > s && o[eo - 1] === n[en - 1]) { eo--; en--; }
      return { added: en - s, removed: eo - s };
    }

    function applyWrite(path, content, source, logId) {
      var existing = model.files.get(path);
      if (!existing) {
        var index = sortedInsert(path);
        var file = { path: path, type: extOf(path), sizeBytes: byteLength(content), content: content, source: source };
        model.files.set(path, file);
        var c0 = lineCounts("", content);
        bus.emit("files:add", { file: file, index: index });
        bus.emit("files:content", { path: path, content: content, prev: "", kind: "create" });
        bus.emit("code:changed", { path: path, runnable: isRunnablePath(path) });
        // canonical lifecycle (chat)
        bus.emit("file:create", { path: path, action: "created", source: source, logId: logId });
        bus.emit("file:write:start", { path: path, logId: logId });
        bus.emit("file:write:chunk", { path: path, chunk: content, content: content, logId: logId });
        bus.emit("file:write:end", { path: path, content: content, action: "created", added: c0.added, removed: 0, logId: logId });
        return true;
      }
      if (existing.content === content) return false;
      var prev = existing.content || "";
      existing.content = content;
      existing.sizeBytes = byteLength(content);
      existing.source = source;
      var append = prev && content.indexOf(prev) === 0;
      var kind = append ? "append" : "overwrite";
      var c = lineCounts(prev, content);
      bus.emit("files:content", { path: path, content: content, prev: prev, kind: kind });
      bus.emit("files:meta", { path: path, sizeBytes: existing.sizeBytes });
      bus.emit("code:changed", { path: path, runnable: isRunnablePath(path) });
      // canonical lifecycle (chat)
      bus.emit("file:write:start", { path: path, logId: logId });
      bus.emit("file:write:chunk", { path: path, chunk: append ? content.slice(prev.length) : content, content: content, logId: logId });
      bus.emit("file:write:end", { path: path, content: content, action: append ? "updated" : "overwritten", added: c.added, removed: c.removed, logId: logId });
      return true;
    }

    function applyDelete(path) {
      if (!model.files.has(path)) return;
      model.files.delete(path);
      var i = model.order.indexOf(path);
      if (i >= 0) model.order.splice(i, 1);
      bus.emit("files:remove", { path: path });
      bus.emit("file:delete", { path: path });
    }

    function emitIndexCreate(gf) {
      var index = sortedInsert(gf.path);
      var file = { path: gf.path, type: gf.file_type, sizeBytes: gf.size_bytes, content: null, source: "index" };
      model.files.set(gf.path, file);
      bus.emit("files:add", { file: file, index: index });
      // Tool-protocol coders write files without emitting fenced code blocks, so a
      // committed file may be the only signal that code changed. Emit it here too; the
      // PreviewPanel debounces these (and is not yet subscribed during the initial
      // backfill, which happens before its first mount), so population never storms it.
      bus.emit("code:changed", { path: gf.path, runnable: isRunnablePath(gf.path) });
      // canonical lifecycle (chat) — committed file with no streamed content yet;
      // logId is null so the chat attaches it to the most recent coder step.
      bus.emit("file:create", { path: gf.path, action: "created", source: "index", logId: null });
    }

    function reconcileIndex(list) {
      var seen = new Set();
      var added = [];
      list.forEach(function (gf) {
        seen.add(gf.path);
        var existing = model.files.get(gf.path);
        if (!existing) {
          added.push(gf);
        } else if (existing.source === "index") {
          // refresh committed size/type for a file we have not streamed content for
          if (existing.sizeBytes !== gf.size_bytes || existing.type !== gf.file_type) {
            existing.sizeBytes = gf.size_bytes;
            existing.type = gf.file_type;
            bus.emit("files:meta", { path: gf.path, sizeBytes: gf.size_bytes });
            bus.emit("code:changed", { path: gf.path, runnable: isRunnablePath(gf.path) });
            bus.emit("file:update", { path: gf.path, sizeBytes: gf.size_bytes });
          }
        }
      });

      // Conservative deletion: only prune *committed* (index-sourced) files the backend
      // no longer reports. Log-derived files persist (they reflect what the agent wrote,
      // even pre-commit). Never prune on an empty list — that is a transient between
      // re-index passes, not a real deletion.
      var removed = [];
      if (list.length) {
        model.order.forEach(function (path) {
          var f = model.files.get(path);
          if (f && f.source === "index" && !seen.has(path)) removed.push(path);
        });
      }

      // Rename detection: exactly one committed add + one committed remove of equal size
      // in the same pass is the signature of a rename. The polling index gives no explicit
      // rename signal, so this is best-effort; a false positive is cosmetic (a card briefly
      // labelled "renamed") and self-heals on the next reconcile.
      if (added.length === 1 && removed.length === 1) {
        var fromPath = removed[0];
        var fromFile = model.files.get(fromPath);
        var toGf = added[0];
        if (fromFile && fromFile.sizeBytes === toGf.size_bytes) {
          model.files.delete(fromPath);
          var oi = model.order.indexOf(fromPath);
          if (oi >= 0) model.order.splice(oi, 1);
          var ni = sortedInsert(toGf.path);
          var nf = { path: toGf.path, type: toGf.file_type, sizeBytes: toGf.size_bytes, content: fromFile.content, source: "index" };
          model.files.set(toGf.path, nf);
          bus.emit("files:remove", { path: fromPath }); // FilesPanel: drop old tree node
          bus.emit("files:add", { file: nf, index: ni }); // FilesPanel: add new tree node
          bus.emit("file:rename", { from: fromPath, to: toGf.path }); // chat: relabel card
          return;
        }
      }

      added.forEach(emitIndexCreate);
      removed.forEach(applyDelete);
    }

    function ingestLogs(data) {
      var logs = data.logs || [];
      logs.forEach(function (log) {
        if (log.kind !== "model" || log.id <= model.lastModelLogId) return;
        model.lastModelLogId = Math.max(model.lastModelLogId, log.id);
        extractFileWrites(log.stdout || "").forEach(function (w) {
          applyWrite(w.path, w.content, "log", log.id);
        });
      });
      reconcileIndex(data.generated_files || []);

      ingestTasks(data.tasks);
      store.setState({
        filesAvailable: model.files.size > 0,
        previewAvailable: !!data.preview_available,
        databaseAvailable: !!data.database_available,
        tasksAvailable: model.tasks.length > 0,
      });
      ingestPreviewState(data.preview);
    }

    function ingestTasks(tasks) {
      if (!Array.isArray(tasks)) return;
      if (JSON.stringify(tasks) === JSON.stringify(model.tasks)) return; // no change → no churn
      model.tasks = tasks;
      bus.emit("tasks:update", { tasks: tasks });
    }

    function ingestPreviewState(preview) {
      var norm = preview
        ? { status: preview.status, url: preview.url || "", error: preview.error_message || "",
            links: preview.links || [] }
        : null;
      var prevJson = JSON.stringify(model.preview);
      var nextJson = JSON.stringify(norm);
      if (prevJson === nextJson) return;
      model.preview = norm;
      bus.emit("preview:state", { preview: norm });
    }

    function ingestResources(data) {
      var incoming = data.samples || [];
      var fresh = [];
      incoming.forEach(function (s) {
        if (s.id <= model.resources.lastSampleId) return;
        model.resources.lastSampleId = Math.max(model.resources.lastSampleId, s.id);
        model.resources.samples.push(s);
        fresh.push(s);
      });
      var cap = 600;
      if (model.resources.samples.length > cap) {
        model.resources.samples.splice(0, model.resources.samples.length - cap);
      }
      if (data.model_run) model.resources.modelStats = data.model_run;
      if (data.project_totals) model.resources.projectTotals = data.project_totals;
      if (fresh.length || data.model_run || data.project_totals) {
        bus.emit("resources:samples", { fresh: fresh, modelStats: model.resources.modelStats });
      }
    }

    function ingestStatus(data) {
      model.resources.benchmark = {
        status: data.status,
        passed: data.passed_tests,
        failed: data.failed_tests,
        total: data.total_tests,
        successRate: data.success_rate,
      };
      bus.emit("status:update", { benchmark: model.resources.benchmark });
    }

    // -----------------------------------------------------------------
    // 3b. Run lifecycle
    // -----------------------------------------------------------------
    function setRun(ctx) {
      reset();
      model.run = ctx || null;
      // Per-chat opt-in: reveal the Resources tab only if the user enabled it for THIS chat.
      var resEnabled = false;
      try {
        var rid = ctx && ctx.id;
        resEnabled = rid ? localStorage.getItem("kursinis-resources-tab-" + rid) === "1" : false;
      } catch (e) {}
      store.setState({ resourcesEnabled: resEnabled, loose: !!(ctx && ctx.loose) });
    }

    function reset() {
      model.files.clear();
      model.order = [];
      model.lastModelLogId = 0;
      model.preview = null;
      model.tasks = [];
      model.resources = { samples: [], lastSampleId: 0, modelStats: null, benchmark: null, projectTotals: null };
      bus.emit("workbench:reset", {});
      store.setState({
        filesAvailable: false,
        previewAvailable: false,
        databaseAvailable: false,
        tasksAvailable: false,
        benchmarksAvailable: false,
        userPickedTab: false,
        activeTab: "empty",
      });
    }

    // =================================================================
    // 4. FilesPanel — file tree + streaming line-diff code viewer.
    //    Completely isolated: never renders preview iframes or metrics.
    // =================================================================
    function createFilesPanel(el) {
      var subs = [];
      var nodeByPath = new Map(); // path -> tree <button>
      var treeEl, headEl, codeEl, emptyEl;

      // viewer state
      var viewerPath = null;
      var displayedLines = [];
      var lineEls = [];
      var userOpened = false; // did the user manually pick a file?
      var caretEl = null;
      var nearBottom = true;
      var fetchAbort = null;
      var writingTimers = new Map();
      var pump = { raf: 0, target: null };

      // ---------- tree ----------
      function makeTreeNode(file) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "wb-node";
        btn.dataset.path = file.path;
        btn.innerHTML =
          '<span class="wb-node-dot"></span>' +
          '<span class="wb-node-path"></span>' +
          '<span class="wb-node-size"></span>';
        btn.querySelector(".wb-node-path").textContent = file.path;
        btn.querySelector(".wb-node-size").textContent = file.sizeBytes + " B";
        btn.addEventListener("click", function () { openFile(file.path, true); });
        return btn;
      }

      function insertTreeNode(file, index) {
        if (emptyEl) { emptyEl.remove(); emptyEl = null; }
        var btn = makeTreeNode(file);
        nodeByPath.set(file.path, btn);
        var ref = treeEl.children[index] || null;
        treeEl.insertBefore(btn, ref);
      }

      function setActiveNode(path) {
        nodeByPath.forEach(function (node, p) { node.classList.toggle("active", p === path); });
      }

      function markWriting(path) {
        var node = nodeByPath.get(path);
        if (node) node.classList.add("writing");
        if (writingTimers.has(path)) clearTimeout(writingTimers.get(path));
        writingTimers.set(path, setTimeout(function () {
          var n = nodeByPath.get(path);
          if (n) n.classList.remove("writing");
          writingTimers.delete(path);
        }, 1400));
      }

      // ---------- viewer (line-diff patching) ----------
      function makeLineNode(num, text, lang) {
        var line = document.createElement("div");
        line.className = "wb-line";
        var g = document.createElement("span");
        g.className = "wb-gutter";
        g.textContent = String(num);
        var c = document.createElement("span");
        c.className = "wb-line-code";
        // ​ (zero-width space) keeps an empty line selectable; the row height comes
        // from the gutter regardless, so this is purely a nicety for caret placement.
        c.innerHTML = highlightLine(text, lang) || "​";
        line.appendChild(g);
        line.appendChild(c);
        return line;
      }

      function currentLang() { return viewerPath ? langFromPath(viewerPath) : "plaintext"; }

      function clearCaret() {
        if (caretEl && caretEl.parentNode) caretEl.parentNode.removeChild(caretEl);
        caretEl = null;
      }
      function setCaret(lineNode) {
        clearCaret();
        if (!lineNode) return;
        caretEl = document.createElement("span");
        caretEl.className = "wb-caret";
        lineNode.querySelector(".wb-line-code").appendChild(caretEl);
      }

      function trackScroll() {
        nearBottom = codeEl.scrollHeight - codeEl.scrollTop - codeEl.clientHeight < 60;
      }
      function autoscroll() {
        if (nearBottom) codeEl.scrollTop = codeEl.scrollHeight;
      }

      function lineDiff(oldL, newL) {
        var start = 0;
        var min = Math.min(oldL.length, newL.length);
        while (start < min && oldL[start] === newL[start]) start++;
        var endOld = oldL.length, endNew = newL.length;
        while (endOld > start && endNew > start && oldL[endOld - 1] === newL[endNew - 1]) {
          endOld--; endNew--;
        }
        return { start: start, endOld: endOld, endNew: endNew };
      }

      var flashTimers = new Set();
      function flash(nodes) {
        nodes.forEach(function (n) { n.classList.add("wb-line--changed"); });
        var t = setTimeout(function () {
          nodes.forEach(function (n) { n.classList.remove("wb-line--changed"); });
          flashTimers.delete(t);
        }, 1300);
        flashTimers.add(t);
      }

      // Patch only the changed region; the unchanged prefix DOM is never touched.
      function patchLines(target, doFlash) {
        var lang = currentLang();
        var d = lineDiff(displayedLines, target);
        for (var i = d.endOld - 1; i >= d.start; i--) {
          if (lineEls[i]) lineEls[i].remove();
        }
        lineEls.splice(d.start, d.endOld - d.start);
        var frag = document.createDocumentFragment();
        var added = [];
        for (var j = d.start; j < d.endNew; j++) {
          var node = makeLineNode(j + 1, target[j], lang);
          added.push(node);
          frag.appendChild(node);
        }
        var refNode = lineEls[d.start] || null;
        codeEl.insertBefore(frag, refNode);
        var spliceArgs = [d.start, 0].concat(added);
        Array.prototype.splice.apply(lineEls, spliceArgs);
        // renumber the shifted suffix gutters (text-only writes, no node churn)
        for (var k = d.start; k < lineEls.length; k++) {
          var g = lineEls[k].querySelector(".wb-gutter");
          var want = String(k + 1);
          if (g.textContent !== want) g.textContent = want;
        }
        displayedLines = target.slice();
        if (doFlash && added.length) flash(added);
      }

      function resetViewer(path) {
        cancelPump();
        clearCaret();
        codeEl.innerHTML = "";
        lineEls = [];
        displayedLines = [];
        viewerPath = path;
        headEl.textContent = path || "Select a file to view its contents.";
        headEl.classList.toggle("placeholder", !path);
      }

      // ---------- streaming animation (typing reveal of the growing tail) ----------
      function cancelPump() {
        if (pump.raf) cancelAnimationFrame(pump.raf);
        pump.raf = 0;
        pump.target = null;
      }
      function startPump() {
        if (pump.raf) return;
        var lang = currentLang();
        var step = function () {
          pump.raf = 0;
          var target = pump.target;
          if (!target) { clearCaret(); return; }
          if (displayedLines.length >= target.length) { clearCaret(); pump.target = null; return; }
          var frag = document.createDocumentFragment();
          var last = null;
          var limit = Math.min(displayedLines.length + 3, target.length);
          while (displayedLines.length < limit) {
            var idx = displayedLines.length;
            var node = makeLineNode(idx + 1, target[idx], lang);
            frag.appendChild(node);
            lineEls.push(node);
            displayedLines.push(target[idx]);
            last = node;
          }
          codeEl.appendChild(frag);
          setCaret(last);
          autoscroll();
          pump.raf = requestAnimationFrame(step);
        };
        pump.raf = requestAnimationFrame(step);
      }

      // Apply a content update to the viewer. `stream` enables the typing reveal for the
      // common "tail grew" case; mid-file edits are patched + flashed immediately.
      function applyContent(path, content, stream) {
        if (path !== viewerPath) resetViewer(path);
        var target = splitLines(content);
        var isPrefix = displayedLines.length <= target.length &&
          (function () {
            for (var i = 0; i < displayedLines.length; i++) if (displayedLines[i] !== target[i]) return false;
            return true;
          })();
        if (stream && isPrefix && target.length > displayedLines.length) {
          pump.target = target;
          startPump();
        } else if (!arraysEqual(displayedLines, target)) {
          cancelPump();
          patchLines(target, stream);
          clearCaret();
          autoscroll();
        }
      }

      function showMessage(msg) {
        cancelPump();
        clearCaret();
        codeEl.innerHTML = '<div class="wb-viewer-msg"></div>';
        codeEl.firstChild.textContent = msg;
        lineEls = [];
        displayedLines = [];
      }

      function fetchFileContent(path) {
        if (!model.run || !model.run.fileUrl) return Promise.resolve(null);
        if (fetchAbort) fetchAbort.abort();
        fetchAbort = new AbortController();
        return fetch(model.run.fileUrl + "?path=" + encodeURIComponent(path), { signal: fetchAbort.signal })
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
          .then(function (res) {
            if (!res.ok) return { error: res.d.error || "Unable to load file." };
            if (res.d.binary) return { binary: true };
            return { content: res.d.content || "" };
          })
          .catch(function (e) {
            if (e && e.name === "AbortError") return { aborted: true };
            return { error: "Unable to load file." };
          });
      }

      function openFile(path, manual) {
        if (manual) { userOpened = true; }
        setActiveNode(path);
        var file = model.files.get(path);
        if (file && file.content != null) {
          resetViewer(path);
          applyContent(path, file.content, false);
          return;
        }
        // index-only file: lazy fetch its content
        resetViewer(path);
        headEl.textContent = path + " · loading…";
        fetchFileContent(path).then(function (res) {
          if (viewerPath !== path) return; // user switched away mid-flight
          if (res.aborted) return;
          if (res.error) { headEl.textContent = path; showMessage(res.error); return; }
          if (res.binary) { headEl.textContent = path; showMessage("Binary file — preview not supported."); return; }
          headEl.textContent = path;
          var f = model.files.get(path);
          if (f) f.content = res.content;
          applyContent(path, res.content, false);
        });
      }

      // ---------- hydrate from current model (lazy-mount correctness) ----------
      function hydrate() {
        treeEl.innerHTML = "";
        nodeByPath.clear();
        if (!model.order.length) {
          emptyEl = document.createElement("div");
          emptyEl.className = "wb-tree-empty";
          emptyEl.textContent = "No files generated yet.";
          treeEl.appendChild(emptyEl);
        } else {
          model.order.forEach(function (path, i) { insertTreeNode(model.files.get(path), i); });
        }
        // Auto-follow the most recently known file with a typing reveal, unless the
        // user has already chosen a file to look at.
        if (!userOpened && model.order.length) {
          var follow = model.order[model.order.length - 1];
          setActiveNode(follow);
          var f = model.files.get(follow);
          if (f && f.content != null) { resetViewer(follow); applyContent(follow, f.content, true); }
          else openFile(follow, false);
        } else if (!viewerPath) {
          resetViewer(null);
        }
      }

      function subscribe() {
        subs.push(bus.on("files:add", function (e) {
          insertTreeNode(e.file, e.index);
        }));
        subs.push(bus.on("files:meta", function (e) {
          var node = nodeByPath.get(e.path);
          if (node) node.querySelector(".wb-node-size").textContent = e.sizeBytes + " B";
        }));
        subs.push(bus.on("files:remove", function (e) {
          var node = nodeByPath.get(e.path);
          if (node) node.remove();
          nodeByPath.delete(e.path);
          if (viewerPath === e.path) resetViewer(null);
          if (!nodeByPath.size) {
            emptyEl = document.createElement("div");
            emptyEl.className = "wb-tree-empty";
            emptyEl.textContent = "No files generated yet.";
            treeEl.appendChild(emptyEl);
          }
        }));
        subs.push(bus.on("files:content", function (e) {
          markWriting(e.path);
          if (e.path === viewerPath) {
            applyContent(e.path, e.content, true);
          } else if (!userOpened) {
            setActiveNode(e.path);
            applyContent(e.path, e.content, true); // follow the active write
          }
        }));
        subs.push(bus.on("workbench:reset", function () {
          cancelPump();
          clearCaret();
          if (fetchAbort) fetchAbort.abort();
          writingTimers.forEach(function (t) { clearTimeout(t); });
          writingTimers.clear();
          userOpened = false;
          nodeByPath.clear();
          treeEl.innerHTML = "";
          emptyEl = document.createElement("div");
          emptyEl.className = "wb-tree-empty";
          emptyEl.textContent = "No files generated yet.";
          treeEl.appendChild(emptyEl);
          resetViewer(null);
        }));
      }

      return {
        mount: function () {
          el.innerHTML =
            '<div class="wb-files">' +
            '  <div class="wb-tree" role="tree"></div>' +
            '  <div class="wb-viewer">' +
            '    <div class="wb-viewer-head placeholder">Select a file to view its contents.</div>' +
            '    <div class="wb-code" tabindex="0"></div>' +
            '  </div>' +
            '</div>';
          treeEl = el.querySelector(".wb-tree");
          headEl = el.querySelector(".wb-viewer-head");
          codeEl = el.querySelector(".wb-code");
          codeEl.addEventListener("scroll", trackScroll);
          hydrate();
          subscribe();
        },
        onShow: function () { autoscroll(); },
        onHide: function () { /* stays subscribed so the tree keeps updating live */ },
        destroy: function () {
          subs.forEach(function (off) { off(); });
          subs = [];
          cancelPump();
          if (fetchAbort) fetchAbort.abort();
          writingTimers.forEach(function (t) { clearTimeout(t); });
          flashTimers.forEach(function (t) { clearTimeout(t); });
        },
      };
    }

    // =================================================================
    // 5. ResourcesPanel — runtime metrics only. Eager-mounted so the
    //    monitor keeps streaming while another tab is focused.
    // =================================================================
    function createResourcesPanel(el) {
      var subs = [];
      var charts = {};
      var labels = [];
      var statusEl, statsEl, totalsEl;

      function fmtDuration(sec) {
        sec = Math.round(sec || 0);
        if (sec < 60) return sec + "s";
        var m = Math.floor(sec / 60), s = sec % 60;
        if (m < 60) return m + "m " + s + "s";
        var h = Math.floor(m / 60);
        return h + "h " + (m % 60) + "m";
      }
      function fmtBytes(b) {
        b = b || 0;
        if (b < 1024) return b + " B";
        if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
        return (b / 1048576).toFixed(1) + " MB";
      }
      function renderTotals() {
        if (!totalsEl) return;
        var t = model.resources.projectTotals;
        if (!t) { totalsEl.innerHTML = ""; return; }
        var rows = [
          ["Chats", String(t.chats || 0)],
          ["Messages", String(t.messages || 0)],
          ["Tokens", (t.prompt_tokens || 0) + " in / " + (t.completion_tokens || 0) + " out"],
          ["Agent time", fmtDuration(t.agent_seconds)],
          ["Files", String(t.files || 0)],
          ["Sandbox size", fmtBytes(t.bytes)],
        ];
        totalsEl.innerHTML =
          '<div class="wb-res-totals-title">Project totals</div>' +
          '<div class="wb-res-totals-grid">' +
          rows.map(function () { return '<div class="wb-total"><span></span><strong></strong></div>'; }).join("") +
          '</div>';
        var nodes = totalsEl.querySelectorAll(".wb-total");
        rows.forEach(function (r, i) {
          nodes[i].querySelector("span").textContent = r[0];
          nodes[i].querySelector("strong").textContent = r[1];
        });
      }

      function chartOptions(yMax) {
        return {
          animation: false,
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { labels: { color: "#888", boxWidth: 10, font: { size: 10 } } } },
          scales: {
            x: { ticks: { color: "#555", maxTicksLimit: 6, font: { size: 9 } }, grid: { color: "#222" } },
            y: { beginAtZero: true, suggestedMax: yMax, ticks: { color: "#555", font: { size: 9 } }, grid: { color: "#222" } },
          },
          elements: { point: { radius: 0 }, line: { borderWidth: 1.5, tension: 0.25 } },
        };
      }
      function ds(label, color) {
        return { label: label, borderColor: color, backgroundColor: color + "22", data: [], spanGaps: true };
      }

      function ensureCharts() {
        if (charts.util || !window.Chart) return !!charts.util;
        var mk = function (sel, datasets, yMax) {
          var canvas = el.querySelector(sel);
          if (!canvas) return null;
          return new window.Chart(canvas.getContext("2d"), {
            type: "line",
            data: { labels: labels, datasets: datasets },
            options: chartOptions(yMax),
          });
        };
        var util = mk('[data-chart="util"]', [ds("CPU %", "#4287f5"), ds("GPU %", "#3fb950")], 100);
        if (util) charts.util = util;
        var mem = mk('[data-chart="mem"]', [ds("RAM GB", "#888"), ds("VRAM GB", "#d29922")]);
        if (mem) charts.mem = mem;
        var temp = mk('[data-chart="temp"]', [ds("GPU °C", "#f85149"), ds("CPU °C", "#888")]);
        if (temp) charts.temp = temp;
        var tps = mk('[data-chart="tps"]', [ds("tok/s", "#4287f5")]);
        if (tps) charts.tps = tps;
        return !!charts.util;
      }

      function pushPoints(chart, values) {
        if (!chart) return;
        values.forEach(function (v, i) { chart.data.datasets[i].data.push(v); });
      }

      function repaint() {
        // The monitor keeps painting even when this tab is not focused (the requirement),
        // not just buffering data. At ~0.5 Hz a no-animation update of a possibly-hidden
        // canvas is effectively free; onShow() additionally resize()s to fix a canvas that
        // was first created at 0×0 while hidden.
        Object.keys(charts).forEach(function (k) { charts[k].update("none"); });
      }

      function applySamples(fresh) {
        if (!ensureCharts()) return;
        fresh.forEach(function (s) {
          labels.push(Math.round(s.t) + "s");
          pushPoints(charts.util, [s.cpu, s.gpu]);
          pushPoints(charts.mem, [
            s.ram_used != null ? s.ram_used / 1024 : null,
            s.vram_used != null ? s.vram_used / 1024 : null,
          ]);
          pushPoints(charts.temp, [s.gpu_temp, s.cpu_temp]);
          pushPoints(charts.tps, [s.tps]);
        });
        if (labels.length > 400) {
          var drop = labels.length - 400;
          labels.splice(0, drop);
          Object.keys(charts).forEach(function (k) {
            charts[k].data.datasets.forEach(function (d) { d.data.splice(0, drop); });
          });
        }
        repaint();
      }

      function statusLine(last, modelStats) {
        var bits = [];
        if (last) {
          if (last.cpu != null) bits.push("CPU " + Math.round(last.cpu) + "%");
          if (last.gpu != null) bits.push("GPU " + Math.round(last.gpu) + "%");
          if (last.vram_used != null && last.vram_total) {
            bits.push("VRAM " + (last.vram_used / 1024).toFixed(1) + "/" + (last.vram_total / 1024).toFixed(1) + "GB");
          }
          if (last.gpu_temp != null) bits.push(Math.round(last.gpu_temp) + "°C");
        }
        if (modelStats && modelStats.tokens_per_second) bits.push(modelStats.tokens_per_second.toFixed(1) + " tok/s");
        return bits.length ? bits.join(" · ") : "Waiting for samples…";
      }

      function renderStats() {
        var m = model.resources.modelStats;
        var b = model.resources.benchmark;
        var rows = [];
        if (b) {
          rows.push(["Tests", (b.passed || 0) + " / " + (b.total || 0) + " passed"]);
          rows.push(["Success", Math.round((b.successRate || 0) * 100) + "%"]);
        }
        if (m) {
          if (m.tokens_per_second) rows.push(["Throughput", m.tokens_per_second.toFixed(1) + " tok/s"]);
          if (m.prompt_tokens != null) rows.push(["Tokens", (m.prompt_tokens || 0) + " in / " + (m.completion_tokens || 0) + " out"]);
          if (m.gpu_percent_peak != null) rows.push(["GPU peak", Math.round(m.gpu_percent_peak) + "%"]);
          if (m.vram_used_mb_peak) rows.push(["VRAM peak", Math.round(m.vram_used_mb_peak) + " / " + Math.round(m.vram_total_mb || 0) + " MB"]);
          if (m.gpu_temp_c_peak != null) rows.push(["GPU temp", Math.round(m.gpu_temp_c_peak) + " °C"]);
          if (m.disk_free_gb != null) rows.push(["Disk free", Number(m.disk_free_gb).toFixed(1) + " GB"]);
        }
        statsEl.innerHTML = rows.length
          ? rows.map(function (r) {
              return '<div class="wb-stat"><span></span><strong></strong></div>';
            }).join("")
          : '<div class="wb-stat-empty">Model statistics will appear once the run produces metrics.</div>';
        if (rows.length) {
          var nodes = statsEl.querySelectorAll(".wb-stat");
          rows.forEach(function (r, i) {
            nodes[i].querySelector("span").textContent = r[0];
            nodes[i].querySelector("strong").textContent = r[1];
          });
        }
      }

      function hydrate() {
        labels.length = 0;
        if (model.resources.samples.length && ensureCharts()) {
          Object.keys(charts).forEach(function (k) {
            charts[k].data.datasets.forEach(function (d) { d.data = []; });
          });
          applySamples(model.resources.samples);
        }
        var last = model.resources.samples[model.resources.samples.length - 1];
        statusEl.textContent = statusLine(last, model.resources.modelStats);
        renderStats();
        renderTotals();
      }

      function subscribe() {
        subs.push(bus.on("resources:samples", function (e) {
          applySamples(e.fresh || []);
          var last = model.resources.samples[model.resources.samples.length - 1];
          statusEl.textContent = statusLine(last, e.modelStats);
          renderStats();
          renderTotals();
        }));
        subs.push(bus.on("status:update", function () { renderStats(); renderTotals(); }));
        subs.push(bus.on("workbench:reset", function () {
          labels.length = 0;
          Object.keys(charts).forEach(function (k) {
            charts[k].data.datasets.forEach(function (d) { d.data = []; });
            charts[k].update("none");
          });
          statusEl.textContent = "Waiting for samples…";
          renderStats();
          renderTotals();
        }));
      }

      return {
        mount: function () {
          el.innerHTML =
            '<div class="wb-res">' +
            '  <div class="wb-res-totals"></div>' +
            '  <div class="wb-res-status">Waiting for samples…</div>' +
            '  <div class="wb-res-stats"></div>' +
            '  <div class="wb-res-charts">' +
            chartBlock("CPU / GPU utilization (%)", "util") +
            chartBlock("Memory used (GB)", "mem") +
            chartBlock("Temperature (°C)", "temp") +
            chartBlock("Throughput (tokens/sec)", "tps") +
            '  </div>' +
            '  <div class="wb-res-note">Live time-series from the autonomous resource monitor ' +
            '(nvidia-smi + psutil). GPU metrics require an NVIDIA GPU; CPU temperature may be ' +
            'unavailable on Windows.</div>' +
            '</div>';
          statusEl = el.querySelector(".wb-res-status");
          statsEl = el.querySelector(".wb-res-stats");
          totalsEl = el.querySelector(".wb-res-totals");
          hydrate();
          subscribe();
        },
        onShow: function () {
          // a hidden Chart.js canvas initialises at 0×0; resize + repaint on reveal
          Object.keys(charts).forEach(function (k) { charts[k].resize(); });
          repaint();
        },
        onHide: function () { /* charts keep updating via repaint(); nothing to pause */ },
        destroy: function () {
          subs.forEach(function (off) { off(); });
          subs = [];
          Object.keys(charts).forEach(function (k) { try { charts[k].destroy(); } catch (e) {} });
          charts = {};
        },
      };

      function chartBlock(title, key) {
        return (
          '<div class="wb-res-chart">' +
          '<div class="wb-res-chart-title">' + title + "</div>" +
          '<div class="wb-res-canvas"><canvas data-chart="' + key + '"></canvas></div>' +
          "</div>"
        );
      }
    }

    // =================================================================
    // 6. PreviewPanel — sandbox runtime lifecycle + iframe. Isolated:
    //    no file tree, no metrics. Owns a small lifecycle state machine.
    // =================================================================
    function createPreviewPanel(el) {
      var subs = [];
      var rootEl, stageEl, emptyEl, frameEl, overlayEl, overlayTextEl, openEl, srEl, linksEl;
      var phase = "idle"; // idle | starting | running | restarting | stopping | stopped | failed
      var chosenPath = null;  // page the USER picked in the links bar (wins over the auto pick)
      var lastAutoPath = null;
      var autoAttempted = false; // guard so a failed auto-start does not loop
      var lastActionAt = 0;
      var actionInFlight = null; // AbortController
      var visible = false;
      var RESTART_DEBOUNCE = 1500;
      var RESTART_MIN_INTERVAL = 6000;

      // The sandbox lifecycle is fully automatic (no Start/Stop/Restart buttons): every code
      // change restarts a running preview, or starts one that is not running yet.
      var debouncedRestart = (function () {
        var t = null;
        var run = function () {
          t = null;
          if (actionInFlight || phase === "starting" || phase === "restarting" || phase === "stopping") { schedule(); return; }
          var since = Date.now() - lastActionAt;
          if (since < RESTART_MIN_INTERVAL) { schedule(RESTART_MIN_INTERVAL - since); return; }
          if (phase === "running") { doAction("restart"); return; }
          // idle/stopped — and FAILED too: reaching here means the code changed since the
          // failure, which is new input worth exactly one fresh attempt (the agent fixing its
          // own broken code must revive the preview). Repeat failures on unchanged code still
          // stop here, because only a code:changed event schedules this.
          if (store.getState().previewAvailable) { autoAttempted = true; doAction("start"); }
        };
        function schedule(wait) {
          if (t) clearTimeout(t);
          t = setTimeout(run, wait == null ? RESTART_DEBOUNCE : wait);
        }
        schedule.cancel = function () { if (t) { clearTimeout(t); t = null; } };
        return schedule;
      })();

      function setPhase(next) {
        phase = next;
        if (rootEl) rootEl.dataset.phase = next;
        syncOverlay();
        announce();
      }

      // Screen-reader feedback goes through a PERMANENT visually-hidden live region (text swaps
      // in an always-rendered region announce reliably; the visual pill toggles display, which
      // aria-live implementations often skip, so the pill itself is aria-hidden).
      function announce() {
        if (!srEl) return;
        var msg = {
          starting: "Starting the preview.",
          restarting: "Updating the preview.",
          stopping: "Stopping the preview.",
          running: "The preview is running.",
          failed: "The preview failed. A Retry button is available in the preview panel.",
          stopped: "The preview is stopped.",
          idle: "",
        }[phase];
        if (msg != null && srEl.textContent !== msg) srEl.textContent = msg;
      }

      // Busy feedback: a floating pill over the stage (centered while nothing is on screen yet,
      // top-of-frame while old content keeps showing through a restart — like Claude artifacts).
      function syncOverlay() {
        if (!overlayEl) return;
        var busy = phase === "starting" || phase === "restarting" || phase === "stopping";
        overlayEl.hidden = !busy;
        if (busy) {
          overlayTextEl.textContent =
            phase === "restarting" ? "Updating preview…" :
            phase === "stopping" ? "Stopping preview…" : "Starting preview…";
          if (emptyEl) emptyEl.hidden = true; // never stack the idle text under the pill
        }
      }

      function loadFrame(url) {
        if (!url) return;
        // Cache-bust so a restarted server is actually re-fetched, not served stale.
        var bust = url + (url.indexOf("?") >= 0 ? "&" : "?") + "_wb=" + Date.now();
        frameEl.hidden = false;
        if (emptyEl) emptyEl.hidden = true;
        if (stageEl) stageEl.classList.add("has-frame");
        frameEl.setAttribute("src", bust);
      }

      // ---- discovered pages bar ----
      function bestPath(links) {
        for (var i = 0; i < links.length; i++) if (links[i].path === "/") return "/";
        return links.length ? links[0].path : "/";
      }
      function joinUrl(base, path) {
        return String(base).replace(/\/$/, "") + (path || "/");
      }
      function renderLinks(preview) {
        if (!linksEl) return;
        var links = (preview && preview.links) || [];
        if (!links.length || preview.status !== "running") { linksEl.hidden = true; linksEl.innerHTML = ""; return; }
        linksEl.hidden = false;
        linksEl.innerHTML = "";
        var active = chosenPath != null ? chosenPath : bestPath(links);
        links.forEach(function (lk) {
          var b = document.createElement("button");
          b.type = "button";
          b.className = "wb-prev-link" + (lk.path === active ? " active" : "");
          b.textContent = lk.path;
          if (lk.name) b.title = lk.name;
          b.addEventListener("click", function () {
            chosenPath = lk.path;
            renderLinks(model.preview);
            if (model.preview && model.preview.url) loadFrame(joinUrl(model.preview.url, lk.path));
          });
          linksEl.appendChild(b);
        });
      }

      function showEmpty(message, withRetry) {
        if (!emptyEl) return;
        if (linksEl) { linksEl.hidden = true; linksEl.innerHTML = ""; }
        frameEl.hidden = true;
        if (stageEl) stageEl.classList.remove("has-frame");
        emptyEl.hidden = false;
        emptyEl.textContent = "";
        var msg = document.createElement("div");
        msg.textContent = message;
        emptyEl.appendChild(msg);
        if (withRetry) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "ghost-btn small wb-prev-retry";
          btn.textContent = "Retry";
          btn.addEventListener("click", function () {
            autoAttempted = true; // manual intent; a second failure still must not auto-loop
            // Starting hides this button's container — park focus on the stage so it does
            // not silently drop to <body>.
            if (stageEl) stageEl.focus();
            doAction("start");
          });
          emptyEl.appendChild(btn);
        }
      }

      function showFailure(message) {
        setPhase("failed");
        showEmpty(message || "The preview failed to start.", true);
      }

      function syncOpenLink(url) {
        if (!openEl) return;
        if (url) { openEl.hidden = false; openEl.setAttribute("href", url); }
        else { openEl.hidden = true; openEl.removeAttribute("href"); }
      }

      function render(preview) {
        // `phase` always tracks the rendered display. While an action is in flight the
        // optimistic phase (starting/restarting/stopping) wins; otherwise we derive the
        // phase from the authoritative preview state so it can never get stuck.
        if (!preview) {
          syncOpenLink(null);
          if (!actionInFlight) {
            setPhase("idle");
            showEmpty("The preview starts automatically once the project is runnable.");
          }
          return;
        }
        syncOpenLink(preview.url || null);
        if (preview.status === "running" && preview.url) {
          setPhase("running");
          renderLinks(preview);
          // Open a REAL page by default: "/" only when it is actually routed — otherwise the
          // first discovered page (an unrouted "/" just shows Django's default rocket page).
          var auto = bestPath(preview.links || []);
          var desired = chosenPath != null ? chosenPath : auto;
          var src = frameEl.getAttribute("src");
          // (re)point the frame if it is not already showing this server (or a better auto page)
          if (frameEl.hidden || !src || src.indexOf(preview.url) !== 0 ||
              (chosenPath == null && lastAutoPath !== auto)) {
            lastAutoPath = auto;
            // brief delay lets runserver finish binding the port before first fetch
            setTimeout(function () { if (phase === "running") loadFrame(joinUrl(preview.url, desired)); }, 400);
          }
        } else if (preview.status === "failed") {
          // while an action is mid-flight its optimistic phase wins (same as the branches below)
          if (!actionInFlight) showFailure(preview.error);
        } else {
          // stopped / unknown: resolve to stopped unless an action is still resolving it
          if (!actionInFlight) {
            setPhase("stopped");
            showEmpty("Preview is stopped — it restarts automatically on the next change.");
          }
        }
      }

      function actionUrl(action) {
        var tpl = model.run && model.run.previewUrlTemplate;
        return tpl ? tpl.replace("__ACTION__", action) : null;
      }

      function doAction(action) {
        var url = actionUrl(action);
        if (!url) return;
        if (actionInFlight) actionInFlight.abort();
        // Handlers compare against this local: a superseded action (its abort rejection runs
        // AFTER the newer controller is assigned) must neither clear the newer action's
        // in-flight guard nor apply its stale response.
        var ctrl = new AbortController();
        actionInFlight = ctrl;
        lastActionAt = Date.now();
        if (action === "start") setPhase("starting");
        else if (action === "restart") setPhase("restarting");
        else if (action === "stop") setPhase("stopping");
        var headers = { "X-Requested-With": "XMLHttpRequest" };
        var token = model.run && model.run.csrfToken;
        if (token) headers["X-CSRFToken"] = token;
        fetch(url, { method: "POST", headers: headers, signal: ctrl.signal })
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
          .then(function (res) {
            if (actionInFlight !== ctrl) return; // superseded — a newer action owns the panel
            actionInFlight = null;
            if (!res.ok) {
              autoAttempted = true; // do not auto-retry a failure
              showFailure(res.d && res.d.error);
              return;
            }
            var d = res.d;
            // feed the model so every consumer stays consistent
            ingestPreviewState({ status: d.status, url: d.url, error_message: d.error_message, links: d.links });
            if (d.status === "failed") { autoAttempted = true; showFailure(d.error_message); return; }
            // A restart often comes back on the SAME url, which render() skips as "already
            // showing" — re-point the (cache-busted) frame explicitly so new code shows up.
            if (d.status === "running" && d.url) {
              setTimeout(function () { if (phase === "running") loadFrame(d.url); }, 400);
            }
          })
          .catch(function (e) {
            if (actionInFlight !== ctrl) return;
            actionInFlight = null;
            if (e && e.name === "AbortError") return;
            autoAttempted = true;
            showFailure("Network error while talking to the preview sandbox.");
          });
      }

      function maybeAutoStart() {
        if (!visible) return;
        if (!store.getState().previewAvailable) return;
        if (autoAttempted) return;
        var p = model.preview;
        if (p && p.status === "running") return;
        // A failure stays on screen (message + Retry) — showing the tab must not fire over it.
        if (phase === "failed" || (p && p.status === "failed")) return;
        if (phase === "starting" || phase === "restarting") return;
        autoAttempted = true;
        doAction("start");
      }

      function subscribe() {
        subs.push(bus.on("preview:state", function (e) { render(e.preview); }));
        subs.push(bus.on("code:changed", function () { debouncedRestart(); }));
        // The moment the project becomes runnable while the tab is open, start the sandbox.
        subs.push(store.subscribe(function (s) { return s.previewAvailable; }, function () { maybeAutoStart(); }));
        subs.push(bus.on("workbench:reset", function () {
          debouncedRestart.cancel();
          if (actionInFlight) actionInFlight.abort();
          actionInFlight = null;
          autoAttempted = false;
          lastActionAt = 0;
          chosenPath = null;
          lastAutoPath = null;
          frameEl.removeAttribute("src");
          setPhase("idle");
          syncOpenLink(null);
          showEmpty("The preview starts automatically once the project is runnable.");
        }));
      }

      return {
        mount: function () {
          el.innerHTML =
            '<div class="wb-prev" data-phase="idle">' +
            '  <div class="wb-prev-links" hidden></div>' +
            '  <div class="wb-prev-stage" tabindex="-1">' +
            '    <div class="wb-prev-empty">The preview starts automatically once the project is runnable.</div>' +
            '    <iframe class="wb-prev-frame" title="Project preview" hidden></iframe>' +
            '    <div class="wb-prev-overlay" hidden aria-hidden="true">' +
            '      <span class="wb-prev-overlay-spinner"></span>' +
            '      <span class="wb-prev-overlay-text">Starting preview…</span>' +
            '    </div>' +
            '    <a class="wb-prev-open" target="_blank" rel="noreferrer" title="Open in a new tab" aria-label="Open the preview in a new tab" hidden>&#8599;</a>' +
            '  </div>' +
            '  <span class="wb-prev-sr" role="status" aria-live="polite"></span>' +
            '</div>';
          rootEl = el.querySelector(".wb-prev");
          linksEl = el.querySelector(".wb-prev-links");
          stageEl = el.querySelector(".wb-prev-stage");
          emptyEl = el.querySelector(".wb-prev-empty");
          frameEl = el.querySelector(".wb-prev-frame");
          overlayEl = el.querySelector(".wb-prev-overlay");
          overlayTextEl = el.querySelector(".wb-prev-overlay-text");
          openEl = el.querySelector(".wb-prev-open");
          srEl = el.querySelector(".wb-prev-sr");
          render(model.preview);
          subscribe();
        },
        onShow: function () { visible = true; maybeAutoStart(); },
        onHide: function () { visible = false; },
        destroy: function () {
          subs.forEach(function (off) { off(); });
          subs = [];
          debouncedRestart.cancel();
          if (actionInFlight) actionInFlight.abort();
        },
      };
    }

    // =================================================================
    // 6b. DatabasePanel — inspect the built project's SQLite DB: list tables,
    //     browse rows, and add a row. Reads the model-run database API (which
    //     builds the runnable project + db on demand the first time it is hit).
    // =================================================================
    function createDatabasePanel(el) {
      var subs = [];
      var loadedOnce = false;
      var currentTable = null;
      var els = {};

      function mkEl(tag, cls) { var n = document.createElement(tag); if (cls) n.className = cls; return n; }
      function dbUrl() { return model.run && model.run.databaseUrl; }
      function csrf() { return model.run && model.run.csrfToken; }
      function runId() { return model.run && model.run.id; }
      function stale(id) { return runId() !== id; } // a run switch happened while a fetch was in flight
      function setStatus(msg, isErr) {
        if (!els.status) return;
        els.status.textContent = msg || "";
        els.status.classList.toggle("error", !!isErr);
      }

      function loadTables() {
        var url = dbUrl();
        if (!url) { setStatus("No database endpoint for this run."); return; }
        var id = runId();
        setStatus("Building project & reading database…");
        fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (stale(id)) return; // a different run was selected while this was loading
            if (!d.available) { renderTables([]); clearMain(); setStatus(d.error || "No database has been built yet.", true); return; }
            var tables = d.tables || [];
            renderTables(tables);
            var keep = currentTable && tables.some(function (t) { return t.name === currentTable; });
            if (keep) loadRows(currentTable);
            else if (tables.length) loadRows(tables[0].name);
            else { clearMain(); setStatus("The database has no tables yet."); }
          })
          .catch(function () { setStatus("Failed to read the database.", true); });
      }

      function renderTables(list) {
        els.tables.innerHTML = "";
        if (!list.length) { els.tables.innerHTML = '<div class="wb-db-empty">No tables.</div>'; return; }
        list.forEach(function (t) {
          var b = mkEl("button", "wb-db-table" + (t.name === currentTable ? " active" : ""));
          b.type = "button";
          b.innerHTML = '<span class="wb-db-tname"></span><span class="wb-db-tcount"></span>';
          b.querySelector(".wb-db-tname").textContent = t.name;
          b.querySelector(".wb-db-tcount").textContent = t.count;
          b.addEventListener("click", function () { loadRows(t.name); });
          els.tables.appendChild(b);
        });
      }

      function setActiveTable(name) {
        currentTable = name;
        Array.prototype.forEach.call(els.tables.querySelectorAll(".wb-db-table"), function (b) {
          b.classList.toggle("active", b.querySelector(".wb-db-tname").textContent === name);
        });
      }

      function loadRows(table) {
        var url = dbUrl();
        if (!url) return;
        var id = runId();
        setActiveTable(table);
        setStatus("Loading " + table + "…");
        fetch(url + "?table=" + encodeURIComponent(table) + "&limit=200", { headers: { "X-Requested-With": "XMLHttpRequest" } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (stale(id)) return; // a different run was selected while this was loading
            if (d.error) { clearMain(); setStatus(d.error, true); return; }
            renderGrid(d);
            renderAddForm(d);
            var shown = (d.rows || []).length;
            setStatus(d.total + " row" + (d.total === 1 ? "" : "s") + (d.total > shown ? " (showing first " + shown + ")" : ""));
          })
          .catch(function () { setStatus("Failed to load rows.", true); });
      }

      function clearMain() { if (els.grid) els.grid.innerHTML = ""; if (els.add) els.add.innerHTML = ""; }

      function renderGrid(d) {
        var cols = d.columns || [];
        var html = '<div class="wb-db-grid-scroll"><table class="wb-db-tbl"><thead><tr>';
        cols.forEach(function (c) {
          html += '<th title="' + escapeHtml(c.type + (c.pk ? " · PK" : "")) + '">' + escapeHtml(c.name) + "</th>";
        });
        html += "</tr></thead><tbody>";
        if (!(d.rows || []).length) {
          html += '<tr><td class="wb-db-norows" colspan="' + Math.max(1, cols.length) + '">No rows yet.</td></tr>';
        }
        (d.rows || []).forEach(function (row) {
          html += "<tr>";
          row.forEach(function (cell) {
            html += "<td>" + (cell == null ? '<span class="wb-db-null">NULL</span>' : escapeHtml(String(cell))) + "</td>";
          });
          html += "</tr>";
        });
        html += "</tbody></table></div>";
        els.grid.innerHTML = html;
      }

      function renderAddForm(d) {
        var cols = (d.columns || []).filter(function (c) { return !(c.pk && /INT/.test(c.type)); });
        var html = '<details class="wb-db-add-wrap"><summary>+ Add row to ' + escapeHtml(d.table) + "</summary>" +
          '<div class="wb-db-form">';
        cols.forEach(function (c) {
          var req = c.notnull && c.default == null ? " *" : "";
          html += '<label class="wb-db-field"><span>' + escapeHtml(c.name) + req + "</span>" +
            '<input data-col="' + escapeHtml(c.name) + '" placeholder="' + escapeHtml(c.type) + '"></label>';
        });
        html += '<div class="wb-db-form-actions"><button type="button" class="ghost-btn small" data-act="insert">Insert row</button></div>';
        html += "</div></details>";
        els.add.innerHTML = html;
        var btn = els.add.querySelector('[data-act="insert"]');
        if (btn) btn.addEventListener("click", function () { insertRow(d.table); });
      }

      function insertRow(table) {
        var url = dbUrl();
        if (!url) return;
        var values = {};
        Array.prototype.forEach.call(els.add.querySelectorAll("input[data-col]"), function (inp) {
          values[inp.dataset.col] = inp.value;
        });
        var headers = { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" };
        var token = csrf();
        if (token) headers["X-CSRFToken"] = token;
        setStatus("Inserting…");
        fetch(url, { method: "POST", headers: headers, body: JSON.stringify({ table: table, values: values }) })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!d.ok) { setStatus("Insert failed: " + (d.error || "unknown error"), true); return; }
            setStatus("Row inserted.");
            loadTables();
          })
          .catch(function () { setStatus("Insert failed (network).", true); });
      }

      function subscribe() {
        subs.push(bus.on("workbench:reset", function () {
          loadedOnce = false; currentTable = null;
          if (els.tables) els.tables.innerHTML = "";
          clearMain();
          setStatus("");
        }));
      }

      return {
        mount: function () {
          el.innerHTML =
            '<div class="wb-db">' +
            '  <div class="wb-db-bar">' +
            '    <span class="wb-db-title">Database · SQLite</span>' +
            '    <button type="button" class="ghost-btn small" data-act="refresh">Refresh</button>' +
            "  </div>" +
            '  <div class="wb-db-body">' +
            '    <div class="wb-db-tables"></div>' +
            '    <div class="wb-db-main"><div class="wb-db-grid"></div><div class="wb-db-add"></div></div>' +
            "  </div>" +
            '  <div class="wb-db-status"></div>' +
            "</div>";
          els.tables = el.querySelector(".wb-db-tables");
          els.grid = el.querySelector(".wb-db-grid");
          els.add = el.querySelector(".wb-db-add");
          els.status = el.querySelector(".wb-db-status");
          el.querySelector('[data-act="refresh"]').addEventListener("click", function () { loadedOnce = true; loadTables(); });
          subscribe();
        },
        onShow: function () { if (!loadedOnce) { loadedOnce = true; loadTables(); } },
        onHide: function () { /* nothing to pause */ },
        destroy: function () { subs.forEach(function (off) { off(); }); subs = []; },
      };
    }

    // =================================================================
    // 6c. TasksPanel — the model's live todo list (todo / doing / done).
    //     Reads model.tasks (refreshed each poll via the tasks:update event).
    // =================================================================
    function createTasksPanel(el) {
      var subs = [];
      var listEl, summaryEl;

      var GLYPH = { completed: "✓", in_progress: "▶", pending: "○" };
      var LABEL = { completed: "done", in_progress: "doing", pending: "todo" };

      function render() {
        var tasks = model.tasks || [];
        if (!tasks.length) {
          listEl.innerHTML = '<div class="wb-tasks-empty">No task list yet. The agent adds one when it plans multi-step work.</div>';
          summaryEl.textContent = "";
          return;
        }
        var done = 0;
        var html = "";
        tasks.forEach(function (t) {
          var status = GLYPH[t.status] ? t.status : "pending";
          if (status === "completed") done++;
          html +=
            '<div class="wb-task wb-task--' + status + '">' +
            '<span class="wb-task-glyph">' + GLYPH[status] + "</span>" +
            '<span class="wb-task-text"></span>' +
            '<span class="wb-task-tag">' + LABEL[status] + "</span>" +
            "</div>";
        });
        listEl.innerHTML = html;
        // textContent (not innerHTML) for the task body so model text can never inject markup.
        var nodes = listEl.querySelectorAll(".wb-task-text");
        tasks.forEach(function (t, i) { if (nodes[i]) nodes[i].textContent = t.content; });
        summaryEl.textContent = done + " / " + tasks.length + " done";
      }

      function subscribe() {
        subs.push(bus.on("tasks:update", function () { render(); }));
        subs.push(bus.on("workbench:reset", function () { render(); }));
      }

      return {
        mount: function () {
          el.innerHTML =
            '<div class="wb-tasks">' +
            '  <div class="wb-tasks-bar"><span class="wb-tasks-title">Tasks</span>' +
            '    <span class="wb-tasks-summary"></span></div>' +
            '  <div class="wb-tasks-list"></div>' +
            "</div>";
          listEl = el.querySelector(".wb-tasks-list");
          summaryEl = el.querySelector(".wb-tasks-summary");
          render();
          subscribe();
        },
        onShow: function () { render(); },
        onHide: function () {},
        destroy: function () { subs.forEach(function (off) { off(); }); subs = []; },
      };
    }

    // =================================================================
    // 6b. BenchmarksPanel — list + launch benchmarks scoped to this project
    // =================================================================
    function createBenchmarksPanel(el) {
      var listEl, runBtn, statusEl, timer = 0;

      function setStatus(m) { if (statusEl) statusEl.textContent = m || ""; }
      function stateLabel(s) {
        return { queued: "Queued", running: "Running", succeeded: "Done", failed: "Failed", cancelled: "Cancelled" }[s] || s || "";
      }

      function render(list) {
        if (!listEl) return;
        if (!list.length) {
          listEl.innerHTML = '<div class="wb-bm-empty">No benchmarks yet — run one to score the active models on this project.</div>';
          return;
        }
        listEl.innerHTML = list.map(function () {
          return '<a class="wb-bm-row" target="_blank" rel="noreferrer">' +
            '<span class="wb-bm-title"></span><span class="wb-bm-meta"></span><span class="wb-bm-state"></span></a>';
        }).join("");
        var rows = listEl.querySelectorAll(".wb-bm-row");
        list.forEach(function (b, i) {
          var row = rows[i];
          row.href = b.detail_url || "#";
          row.querySelector(".wb-bm-title").textContent = b.title || ("Benchmark #" + b.id);
          var meta = [];
          if (b.total) meta.push(b.passed + "/" + b.total + " passed");
          if (b.created) meta.push(b.created);
          row.querySelector(".wb-bm-meta").textContent = meta.join(" · ");
          var st = row.querySelector(".wb-bm-state");
          st.textContent = stateLabel(b.status);
          st.dataset.state = b.status || "";
        });
      }

      async function load() {
        if (!model.run || !model.run.benchmarksUrl) return;
        try {
          var r = await fetch(model.run.benchmarksUrl);
          if (!r.ok) return;
          var data = await r.json();
          render(data.benchmarks || []);
        } catch (e) { /* keep the last render */ }
      }

      async function runNew() {
        if (!model.run || !model.run.newBenchmarkUrl) return;
        if (runBtn) runBtn.disabled = true;
        setStatus("Starting benchmark…");
        try {
          var headers = { "X-Requested-With": "XMLHttpRequest" };
          if (model.run.csrfToken) headers["X-CSRFToken"] = model.run.csrfToken;
          var r = await fetch(model.run.newBenchmarkUrl, { method: "POST", headers });
          var data = {};
          try { data = await r.json(); } catch (e) {}
          setStatus((!r.ok || !data.ok) ? (data.error || "Could not start benchmark.") : "Benchmark queued.");
        } catch (e) { setStatus("Network error — try again."); }
        if (runBtn) runBtn.disabled = false;
        load();
      }

      return {
        mount: function () {
          el.innerHTML =
            '<div class="wb-bm">' +
            '  <div class="wb-bm-head">' +
            '    <button type="button" class="button primary wb-bm-run">Run benchmark on this project</button>' +
            '    <span class="wb-bm-status"></span>' +
            '  </div>' +
            '  <div class="wb-bm-list"></div>' +
            '  <div class="wb-bm-note">Runs the active models against this project’s prompt with auto-generated tests, then scores them. Each run opens on the benchmark page.</div>' +
            '</div>';
          listEl = el.querySelector(".wb-bm-list");
          runBtn = el.querySelector(".wb-bm-run");
          statusEl = el.querySelector(".wb-bm-status");
          if (runBtn) runBtn.addEventListener("click", runNew);
          load();
          timer = window.setInterval(load, 4000); // keep run statuses fresh
        },
        onShow: function () { load(); },
        onHide: function () { /* keep polling so statuses stay current */ },
        destroy: function () { if (timer) { clearInterval(timer); timer = 0; } },
      };
    }

    // =================================================================
    // 7. RightPanel — tab orchestration, lazy mounting, reactive visibility
    // =================================================================
    // Neutral placeholder shown when no real tab has content yet and Resources is opted-out.
    function createEmptyPanel(el) {
      return {
        mount: function () {
          if (el) el.innerHTML = '<div class="wb-empty">Files, preview &amp; database will appear here as the agent works.</div>';
        },
        onShow: function () {}, onHide: function () {}, destroy: function () {},
      };
    }

    function createRightPanel() {
      var tabBar = root.querySelector(".preview-tabs");
      var tabBtns = {
        files: tabBar.querySelector('[data-tab="files"]'),
        tasks: tabBar.querySelector('[data-tab="tasks"]'),
        resources: tabBar.querySelector('[data-tab="resources"]'),
        preview: tabBar.querySelector('[data-tab="preview"]'),
        database: tabBar.querySelector('[data-tab="database"]'),
      };
      var panes = {
        files: root.querySelector('[data-pane="files"]'),
        tasks: root.querySelector('[data-pane="tasks"]'),
        resources: root.querySelector('[data-pane="resources"]'),
        preview: root.querySelector('[data-pane="preview"]'),
        database: root.querySelector('[data-pane="database"]'),
        empty: root.querySelector('[data-pane="empty"]'),
      };
      var panels = {
        files: createFilesPanel(panes.files),
        tasks: createTasksPanel(panes.tasks),
        resources: createResourcesPanel(panes.resources),
        preview: createPreviewPanel(panes.preview),
        database: createDatabasePanel(panes.database),
        empty: createEmptyPanel(panes.empty),
      };
      var mounted = { files: false, tasks: false, resources: false, preview: false, database: false, empty: false };

      function ensureMounted(name) {
        if (!mounted[name]) { panels[name].mount(); mounted[name] = true; }
      }

      function isAvailable(name) {
        var s = store.getState();
        if (name === "files") return s.filesAvailable;
        if (name === "preview") return s.previewAvailable;
        if (name === "database") return s.databaseAvailable;
        if (name === "tasks") return s.tasksAvailable;
        if (name === "resources") return s.resourcesEnabled; // opt-in (per-chat toggle)
        if (name === "empty") return true;                   // neutral fallback, always available
        return false;
      }

      // Whether a tab may be shown right now: available AND (for loose chats) past the "wait for a
      // preview" gate — Files/Tasks/Database stay hidden in a standalone chat until a preview exists.
      function tabAllowed(name) {
        if (!isAvailable(name)) return false;
        var s = store.getState();
        if (s.loose && !s.previewAvailable && (name === "files" || name === "tasks" || name === "database")) return false;
        return true;
      }

      // First real tab that may be shown; else Resources if the user enabled it; else the placeholder.
      function firstAvailable() {
        var order = ["files", "preview", "database", "tasks"];
        for (var i = 0; i < order.length; i++) if (tabAllowed(order[i])) return order[i];
        if (store.getState().resourcesEnabled) return "resources";
        return null;
      }

      function activate(name, userInitiated) {
        if (!isAvailable(name)) name = firstAvailable() || "empty";
        Object.keys(panes).forEach(function (k) {
          var on = k === name;
          if (tabBtns[k]) {  // "empty" has a pane but no tab button
            tabBtns[k].classList.toggle("active", on);
            tabBtns[k].setAttribute("aria-selected", on ? "true" : "false");
          }
          panes[k].hidden = !on;
          if (on) { ensureMounted(k); panels[k].onShow(); }
          else if (mounted[k]) { panels[k].onHide(); }
        });
        store.setState(function (s) {
          return { activeTab: name, userPickedTab: userInitiated ? true : s.userPickedTab };
        });
      }

      Object.keys(tabBtns).forEach(function (name) {
        tabBtns[name].addEventListener("click", function () { activate(name, true); });
      });

      // Default tab: the first real tab with content, else a neutral placeholder (Resources is opt-in).
      activate(firstAvailable() || "empty", false);

      // The whole right pane stays COLLAPSED (the chat takes the full width) until at least one
      // real tab is ready, then slides in (CSS width transition on .chat-body.wb-collapsed).
      // NOTE: the class must NOT be "wb-empty" — that's the empty-pane placeholder's STYLING class
      // (max-width:280px), which would shrink the whole chat body to 280px.
      var chatBody = root.closest(".chat-body");
      function syncCollapsed() {
        if (!chatBody) return;
        var any = ["files", "preview", "database", "tasks"].some(tabAllowed) || store.getState().resourcesEnabled;
        chatBody.classList.toggle("wb-collapsed", !any);
        // Collapsing while maximized would hide BOTH panes (wb-maximized hides .chat-left) —
        // drop maximize first. `maximized` is still undefined on the very first call (the
        // maximize section initialises below), which correctly skips this.
        if (!any && maximized) setMaximized(false);
        // Revealing with nothing selected yet → focus the first ready tab, not the placeholder.
        if (any && store.getState().activeTab === "empty") activate(firstAvailable() || "empty", false);
      }

      // Reactive tab visibility ---------------------------------------
      function fallbackOff(tab) {
        // When `tab` goes unavailable/gated while it's showing, drop to the next sensible pane.
        if (store.getState().activeTab === tab) activate(firstAvailable() || "empty", false);
      }
      function syncTabBtn(name) { if (tabBtns[name]) tabBtns[name].hidden = !tabAllowed(name); }
      store.subscribe(function (s) { return s.filesAvailable; }, function (avail) {
        syncTabBtn("files");
        var s = store.getState();
        if (!tabAllowed("files")) fallbackOff("files");
        else if (!s.userPickedTab) activate("files", false); // first files → focus them (non-loose)
        syncCollapsed();
      });
      store.subscribe(function (s) { return s.previewAvailable; }, function (avail) {
        syncTabBtn("preview");
        // A loose chat keeps Files/Tasks/Database hidden until a preview exists — reveal them now.
        syncTabBtn("files"); syncTabBtn("tasks"); syncTabBtn("database");
        syncCollapsed();
        if (!avail) { fallbackOff("preview"); return; }
        var s = store.getState();
        if (s.loose && s.activeTab === "empty") activate("preview", false); // first thing a loose chat shows
      });
      store.subscribe(function (s) { return s.databaseAvailable; }, function (avail) {
        syncTabBtn("database");
        if (!tabAllowed("database")) fallbackOff("database");
        syncCollapsed();
      });
      store.subscribe(function (s) { return s.tasksAvailable; }, function (avail) {
        syncTabBtn("tasks");
        if (!tabAllowed("tasks")) fallbackOff("tasks");
        syncCollapsed();
      });
      // Resources is opt-in (per-chat). Reveal/hide its button + adjust the active tab accordingly.
      store.subscribe(function (s) { return s.resourcesEnabled; }, function (enabled) {
        tabBtns.resources.hidden = !enabled;
        var s = store.getState();
        if (!enabled && s.activeTab === "resources") activate(firstAvailable() || "empty", false);
        else if (enabled && s.activeTab === "empty") activate("resources", false);
        syncCollapsed();
      });

      bus.on("workbench:reset", function () {
        tabBtns.files.hidden = true;
        tabBtns.tasks.hidden = true;
        tabBtns.preview.hidden = true;
        tabBtns.database.hidden = true;
        activate(firstAvailable() || "empty", false);
        syncCollapsed();
      });

      syncCollapsed(); // initial: collapsed until the first poll reports something ready

      // ---- Resources tab opt-in toggle (hidden by default; remembered per chat) ----
      var resToggle = document.createElement("button");
      resToggle.type = "button";
      resToggle.className = "wb-restoggle";
      resToggle.textContent = "▦";
      function syncResToggle() {
        var on = store.getState().resourcesEnabled;
        resToggle.classList.toggle("on", on);
        resToggle.title = on ? "Hide resource usage" : "Show resource usage";
        resToggle.setAttribute("aria-pressed", on ? "true" : "false");
      }
      resToggle.setAttribute("aria-label", "Toggle resource usage panel");
      resToggle.addEventListener("click", function () {
        var on = !store.getState().resourcesEnabled;
        store.setState({ resourcesEnabled: on });
        try {
          var id = model.run && model.run.id;
          if (id) localStorage.setItem("kursinis-resources-tab-" + id, on ? "1" : "0");
        } catch (e) {}
        if (on) activate("resources", true);  // turning it on explicitly reveals it now
      });
      store.subscribe(function (s) { return s.resourcesEnabled; }, syncResToggle);
      syncResToggle();

      // ---- maximize / full-screen the workbench (the active tab fills the page) ----
      // (chatBody is declared with the collapse logic above — same .chat-body element.)
      var maximized = false;
      var maxBtn = document.createElement("button");
      maxBtn.type = "button";
      maxBtn.className = "wb-maximize";
      maxBtn.textContent = "⤢";
      maxBtn.title = "Maximize (Esc to restore)";
      maxBtn.setAttribute("aria-label", "Maximize panel");
      tabBar.appendChild(maxBtn);
      tabBar.appendChild(resToggle);  // grouped at the right (maxBtn's margin-left:auto pushes the pair)
      function setMaximized(on) {
        maximized = !!on;
        if (chatBody) chatBody.classList.toggle("wb-maximized", maximized);
        maxBtn.textContent = maximized ? "⤡" : "⤢";
        maxBtn.title = maximized ? "Restore" : "Maximize (Esc to restore)";
        var active = store.getState().activeTab; // relayout the visible panel (charts/iframe)
        if (active && mounted[active]) { try { panels[active].onShow(); } catch (e) {} }
      }
      maxBtn.addEventListener("click", function () { setMaximized(!maximized); });
      function onKey(e) { if (e.key === "Escape" && maximized) setMaximized(false); }
      document.addEventListener("keydown", onKey);

      return {
        activate: activate,
        destroy: function () {
          document.removeEventListener("keydown", onKey);
          Object.keys(panels).forEach(function (k) { if (mounted[k]) panels[k].destroy(); });
        },
      };
    }

    var rightPanel = createRightPanel();

    // =================================================================
    // 8. Public API (consumed by app.js)
    // =================================================================
    return {
      setRun: setRun,
      reset: reset,
      ingestLogs: ingestLogs,
      ingestResources: ingestResources,
      ingestStatus: ingestStatus,
      // Programmatically reveal a tab (used to auto-show Preview at a design-approval gate).
      showTab: function (name) { try { rightPanel.activate(name, true); } catch (e) { /* ignore */ } },
      // The event bus and entity model are exposed so sibling surfaces (the chat
      // timeline) can subscribe to the SAME canonical file:* lifecycle events — one
      // ingestion updates both the Files panel and the chat with no duplicate parsing.
      bus: bus,
      model: model,
      destroy: function () {
        rightPanel.destroy();
        bus.clear();
      },
      _debug: { store: store, bus: bus, model: model },
    };
  }

  // Shared pure helpers, reused by chat.js so the CodeDiffHighlighter / syntax
  // highlighting behave identically on both surfaces.
  window.AgentWorkbench = {
    create: create,
    utils: {
      escapeHtml: escapeHtml,
      langFromPath: langFromPath,
      splitLines: splitLines,
      highlightLine: highlightLine,
      extOf: extOf,
    },
  };
})();
