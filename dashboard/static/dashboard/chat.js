/**
 * Agent chat timeline — the LEFT pane of the benchmark execution page.
 *
 * Turns the raw CommandLog stream + the workbench's canonical file:* events into an
 * IDE-like, live timeline (think Cursor / Replit / Claude Artifacts):
 *   • assistant/step text streams in with a typing animation (no instant dumps),
 *   • code the model writes appears as embedded, file-aware cards
 *     (`models.py — created`, syntax-highlighted, collapsible, changed lines flashed),
 *   • a small ActivityTimeline shows safe high-level statuses (Thinking…, Cooking…,
 *     Writing models.py…, Running tests…, Done.) that collapse into a clean trail.
 *
 * COMPONENTS (vanilla-JS factories, each `{el, …, destroy}`):
 *   ChatTimeline   — orchestrates the feed; owns the steps Map + ActivityTimeline.
 *   AgentStatusMessage (StepCard) — one structured agent step (head + streamed body
 *                    + embedded code/files + result).
 *   StreamingMessage / TypingAnimation — progressive text reveal via requestAnimationFrame.
 *   FileWriteCard  — filename + action badge + embedded FileCodeBlock.
 *   FileCodeBlock / CodeDiffHighlighter — line-based viewer with diff patching, a typing
 *                    tail, and changed-line highlighting.
 *   ActivityTimeline — live status pill + collapsing chronological trail.
 *
 * INTEGRATION: app.js feeds `ingestLogs(data)` (creates/updates step cards) and the chat
 * SUBSCRIBES to the workbench bus (`file:create`, `file:write:start|chunk|end`,
 * `file:update`, `file:delete`, `file:rename`). app.js calls chat.ingestLogs BEFORE
 * workbench.ingestLogs, so a step exists by the time its file:* events fire and the card
 * attaches to the right step (events carry the originating coder logId).
 *
 * PERFORMANCE: every step renders ONCE (memoized in a Map keyed by log id); subsequent
 * polls patch only the changed text node / changed code lines. Streaming is frame-batched
 * (rAF), so updates are inherently debounced and never trigger a full feed rerender.
 *
 * SAFETY: statuses are high-level labels derived from the *kind* of activity only. No
 * private chain-of-thought is ever rendered (the optional thinking_trace is intentionally
 * NOT surfaced here).
 */
(function () {
  "use strict";

  // ---- task list (the `tasks` tool emits its full list as JSON in stdout) ----
  // Defined at module level (NOT inside create()) because it is exported on window.AgentChat
  // below — referencing it there from inside create()'s scope would throw a ReferenceError at
  // load time and silently kill the whole chat feed.
  var TASK_GLYPH = { completed: "✓", in_progress: "▶", pending: "○" };
  var TASK_TAG = { completed: "done", in_progress: "doing", pending: "todo" };
  function parseTasks(stdout) {
    try {
      var data = JSON.parse(stdout || "[]");
      return Array.isArray(data) ? data : [];
    } catch (e) { return []; }
  }
  function renderTaskList(container, tasks) {
    if (!tasks.length) { container.innerHTML = '<div class="task-empty">Task list cleared.</div>'; return; }
    var done = 0;
    var html = '<div class="task-summary"></div>';
    tasks.forEach(function (t) {
      var status = TASK_GLYPH[t.status] ? t.status : "pending";
      if (status === "completed") done++;
      html +=
        '<div class="task-row task--' + status + '">' +
        '<span class="task-glyph">' + TASK_GLYPH[status] + "</span>" +
        '<span class="task-text"></span>' +
        '<span class="task-tag">' + TASK_TAG[status] + "</span></div>";
    });
    container.innerHTML = html;
    container.querySelector(".task-summary").textContent = done + " / " + tasks.length + " done";
    var nodes = container.querySelectorAll(".task-text");
    tasks.forEach(function (t, i) { if (nodes[i]) nodes[i].textContent = t.content; });
  }

  // ---- minimal, XSS-safe markdown → HTML for conversational bubbles -------------------------
  // Everything is escaped FIRST; only our own tags are introduced. Fenced code blocks are
  // stripped upstream (they render as dedicated code cards), so this covers headings, bold,
  // italics, inline code, bullet/numbered lists and paragraphs.
  function mdEsc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function mdInline(s) {
    return mdEsc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  }
  function mdToHtml(text) {
    var lines = String(text || "").split(/\r?\n/);
    var out = [], ul = null, ol = null;
    function closeLists() {
      if (ul) { out.push("<ul>" + ul.join("") + "</ul>"); ul = null; }
      if (ol) { out.push("<ol>" + ol.join("") + "</ol>"); ol = null; }
    }
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      var m;
      if ((m = ln.match(/^\s*#{1,2}\s+(.*)$/))) { closeLists(); out.push("<h3>" + mdInline(m[1]) + "</h3>"); }
      else if ((m = ln.match(/^\s*#{3,6}\s+(.*)$/))) { closeLists(); out.push("<h4>" + mdInline(m[1]) + "</h4>"); }
      else if ((m = ln.match(/^\s*[-*•]\s+(.*)$/))) { if (ol) closeLists(); (ul = ul || []).push("<li>" + mdInline(m[1]) + "</li>"); }
      else if ((m = ln.match(/^\s*\d+[.)]\s+(.*)$/))) { if (ul) closeLists(); (ol = ol || []).push("<li>" + mdInline(m[1]) + "</li>"); }
      else if (ln.trim() === "") { closeLists(); }
      else { closeLists(); out.push("<p>" + mdInline(ln) + "</p>"); }
    }
    closeLists();
    return out.join("");
  }

  function create(feed, opts) {
    if (!feed) return null;
    opts = opts || {};
    var bus = opts.bus || null;
    var model = opts.model || null;

    // Shared pure helpers from workbench.js (with safe fallbacks).
    var U = (window.AgentWorkbench && window.AgentWorkbench.utils) || {};
    var escapeHtml = U.escapeHtml || function (s) { return String(s == null ? "" : s); };
    var langFromPath = U.langFromPath || function () { return "plaintext"; };
    var splitLines = U.splitLines || function (c) { return String(c == null ? "" : c).replace(/\n$/, "").split("\n"); };
    var highlightLine = U.highlightLine || function (t) { return escapeHtml(t); };

    // ---- agent identity (mirrors app.js / the backend CommandLog kinds) ----
    // No emojis anywhere in the UI — icons are plain monochrome glyphs only.
    var AGENT_ROLES = {
      model: { name: "Coder", role: "CODING", icon: "▸", cls: "role-coder", verb: "Cooking" },
      test: { name: "Executor", role: "TESTING", icon: "▶", cls: "role-executor", verb: "Running tests" },
      preview: { name: "Preview", role: "PREVIEW", icon: "▣", cls: "role-preview", verb: "Preparing preview" },
      system: { name: "System", role: "SYSTEM", icon: "•", cls: "role-system", verb: "Working" },
    };
    // `cmd` is the imperative verb shown in a step's tool-call row (e.g. "Read blog/models.py").
    var AGENT_BY_NAME = {
      // legacy fixed-pipeline identities (kept so historical runs still render)
      planner: { name: "Planner", role: "PLANNING", icon: "◆", cls: "role-planner", verb: "Planning", cmd: "Plan" },
      retriever: { name: "Retriever", role: "RETRIEVAL", icon: "◎", cls: "role-retriever", verb: "Retrieving context", cmd: "Retrieve" },
      coder: { name: "Coder", role: "CODING", icon: "▸", cls: "role-coder", verb: "Cooking", cmd: "Write" },
      executor: { name: "Executor", role: "TESTING", icon: "▶", cls: "role-executor", verb: "Running tests", cmd: "Test" },
      critic: { name: "Critic", role: "CRITIQUE", icon: "◇", cls: "role-critic", verb: "Reviewing changes", cmd: "Review" },
      // dynamic, model-routed tool steps (the model picks one per turn)
      router: { name: "Agent", role: "THINKING", icon: "✶", cls: "role-router", verb: "Thinking", cmd: "" },
      tasks: { name: "Tasks", role: "PLAN", icon: "✓", cls: "role-tasks", verb: "Updating tasks", cmd: "" },
      plan: { name: "Plan", role: "PLANNING", icon: "◆", cls: "role-planner", verb: "Planning", cmd: "" },
      retrieve: { name: "Docs", role: "RETRIEVAL", icon: "◎", cls: "role-retriever", verb: "Retrieving docs", cmd: "Retrieve" },
      list: { name: "List", role: "LIST", icon: "≡", cls: "role-list", verb: "Listing files", cmd: "List" },
      read: { name: "Read", role: "READ", icon: "¶", cls: "role-read", verb: "Reading", cmd: "Read" },
      grep: { name: "Grep", role: "SEARCH", icon: "◎", cls: "role-grep", verb: "Searching", cmd: "Grep" },
      write: { name: "Write", role: "CODING", icon: "▸", cls: "role-coder", verb: "Writing", cmd: "Write" },
      edit: { name: "Edit", role: "CODING", icon: "✎", cls: "role-edit", verb: "Editing", cmd: "Edit" },
      shell: { name: "Shell", role: "SHELL", icon: "❯", cls: "role-shell", verb: "Running", cmd: "Run" },
      test: { name: "Tests", role: "TESTING", icon: "▶", cls: "role-executor", verb: "Running tests", cmd: "Test" },
      finish: { name: "Done", role: "DONE", icon: "✓", cls: "role-system", verb: "Finishing", cmd: "" },
      system: { name: "System", role: "SYSTEM", icon: "•", cls: "role-system", verb: "Working", cmd: "" },
      // The user's own message in a project chat (rendered as a distinct bubble).
      user: { name: "You", role: "", icon: "●", cls: "role-user", verb: "", cmd: "" },
      // Resolved-interaction + post-turn record cards (the live cards come from `pending`).
      question: { name: "Question", role: "ASKED", icon: "?", cls: "role-question", verb: "", cmd: "" },
      approval: { name: "Approval", role: "GATED", icon: "!", cls: "role-question", verb: "", cmd: "" },
      database: { name: "Database", role: "DB", icon: "▤", cls: "role-list", verb: "", cmd: "Migrate" },
      venv: { name: "Environment", role: "VENV", icon: "▢", cls: "role-list", verb: "", cmd: "" },
      memory: { name: "Memory", role: "MEMORY", icon: "◰", cls: "role-list", verb: "", cmd: "" },
      error: { name: "Error", role: "FAILED", icon: "!", cls: "role-error", verb: "", cmd: "" },
      links: { name: "Pages", role: "LINKS", icon: "↗", cls: "role-list", verb: "", cmd: "" },
    };
    function resolveRole(log) {
      var explicit = log.agent && AGENT_BY_NAME[String(log.agent).toLowerCase()];
      return explicit || AGENT_ROLES[log.kind] || AGENT_ROLES.system;
    }

    // ---- small utilities ----
    function el(tag, cls) { var n = document.createElement(tag); if (cls) n.className = cls; return n; }
    function arraysEqual(a, b) {
      if (a === b) return true;
      if (a.length !== b.length) return false;
      for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
      return true;
    }
    function formatTime(iso) {
      if (!iso) return "";
      try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
      catch (e) { return ""; }
    }
    // Strip fenced code blocks: the model's code becomes embedded cards, not prose.
    function prose(stdout) {
      return String(stdout == null ? "" : stdout)
        .replace(/```[\s\S]*?```/g, "")
        .replace(/[ \t]+\n/g, "\n")
        .replace(/\n{3,}/g, "\n\n")
        .trim();
    }
    // Fenced blocks shown inline as generic snippets. For a model (write/edit) step we skip the
    // `# <path>` fence because the embedded file card already renders it; for inspection steps
    // (read/grep/list/shell — keepFileFences=true) we render EVERY fence, so an inspected file
    // whose first line is a `# foo.py` comment is not mistaken for a write fence and hidden.
    function nonFileFences(stdout, keepFileFences) {
      var fence = /```([a-zA-Z0-9_+\-]*)\n([\s\S]*?)```/g;
      var out = [];
      var m;
      while ((m = fence.exec(stdout)) !== null) {
        var body = m[2];
        var first = (body.split("\n", 1)[0] || "").trim();
        if (!keepFileFences && /^#\s*[\w./\-]+\.\w+$/.test(first)) continue;
        out.push({ lang: m[1] || "plaintext", code: body.replace(/\s+$/, "") });
      }
      return out;
    }

    // ---- scroll stability ----
    var stick = true;
    feed.addEventListener("scroll", function () {
      stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
    });
    function onGrow() { if (stick) feed.scrollTop = feed.scrollHeight; }

    var reduceMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

    // =================================================================
    // TypingAnimation / StreamingMessage — progressive text reveal.
    // Writes into a single Text node (never innerHTML) so it can't flicker.
    // Reveal speed scales with remaining length, so a 5-line plan and a
    // 500-line test log both finish in ~0.4s.
    // =================================================================
    function createTyper(cls) {
      var wrap = el("div", cls || "step-prose");
      var textNode = document.createTextNode("");
      var caret = el("span", "type-caret");
      caret.style.display = "none"; // hidden until there is text to reveal (no lone caret)
      wrap.appendChild(textNode);
      wrap.appendChild(caret);
      var target = "";
      var shown = 0;
      var raf = 0;
      function tick() {
        raf = 0;
        if (shown >= target.length) { caret.style.display = "none"; return; }
        var remaining = target.length - shown;
        shown = Math.min(target.length, shown + Math.max(2, Math.floor(remaining / 24)));
        textNode.nodeValue = target.slice(0, shown);
        onGrow();
        raf = requestAnimationFrame(tick);
      }
      return {
        el: wrap,
        write: function (full) {
          full = full || "";
          if (full === target) return;
          // If the new text no longer starts with what we've already revealed, restart.
          if (full.indexOf(target.slice(0, shown)) !== 0) shown = 0;
          target = full;
          if (!target) { textNode.nodeValue = ""; caret.style.display = "none"; return; }
          caret.style.display = "";
          if (!raf) raf = requestAnimationFrame(tick);
        },
        finish: function () {
          if (raf) cancelAnimationFrame(raf);
          raf = 0; shown = target.length; textNode.nodeValue = target; caret.style.display = "none";
        },
        // Render the full text instantly with no animation (used when backfilling
        // historical messages on reopen).
        set: function (full) {
          if (raf) cancelAnimationFrame(raf);
          raf = 0; target = full || ""; shown = target.length;
          textNode.nodeValue = target; caret.style.display = "none";
        },
        isEmpty: function () { return !target; },
        destroy: function () { if (raf) cancelAnimationFrame(raf); },
      };
    }

    // =================================================================
    // FileCodeBlock + CodeDiffHighlighter — line-based code view that
    // patches ONLY changed lines, types out a growing tail, and flashes
    // changed lines. Same algorithm as the workbench viewer, scoped to a card.
    // =================================================================
    function createCodeBlock(lang) {
      var wrap = el("div", "cb");
      var codeEl = el("div", "cb-code");
      wrap.appendChild(codeEl);
      var displayed = [];
      var lineEls = [];
      var pump = { raf: 0, target: null };
      var caret = null;
      var flashTimers = new Set();

      function makeLine(num, text) {
        var line = el("div", "cb-line");
        var g = el("span", "cb-gutter");
        g.textContent = String(num);
        var c = el("span", "cb-line-code");
        c.innerHTML = highlightLine(text, lang) || "​";
        line.appendChild(g);
        line.appendChild(c);
        return line;
      }
      function lineDiff(o, n) {
        var s = 0;
        var min = Math.min(o.length, n.length);
        while (s < min && o[s] === n[s]) s++;
        var eo = o.length, en = n.length;
        while (eo > s && en > s && o[eo - 1] === n[en - 1]) { eo--; en--; }
        return { s: s, eo: eo, en: en };
      }
      function clearCaret() { if (caret && caret.parentNode) caret.parentNode.removeChild(caret); caret = null; }
      function setCaret(node) {
        clearCaret();
        if (!node) return;
        caret = el("span", "cb-caret");
        node.querySelector(".cb-line-code").appendChild(caret);
      }
      function flash(nodes) {
        nodes.forEach(function (n) { n.classList.add("cb-line--changed"); });
        var t = setTimeout(function () {
          nodes.forEach(function (n) { n.classList.remove("cb-line--changed"); });
          flashTimers.delete(t);
        }, 1300);
        flashTimers.add(t);
      }
      function patch(target, doFlash) {
        var d = lineDiff(displayed, target);
        for (var i = d.eo - 1; i >= d.s; i--) { if (lineEls[i]) lineEls[i].remove(); }
        lineEls.splice(d.s, d.eo - d.s);
        var frag = document.createDocumentFragment();
        var added = [];
        for (var j = d.s; j < d.en; j++) {
          var node = makeLine(j + 1, target[j]);
          added.push(node);
          frag.appendChild(node);
        }
        codeEl.insertBefore(frag, lineEls[d.s] || null);
        Array.prototype.splice.apply(lineEls, [d.s, 0].concat(added));
        for (var k = d.s; k < lineEls.length; k++) {
          var g = lineEls[k].querySelector(".cb-gutter");
          var w = String(k + 1);
          if (g.textContent !== w) g.textContent = w;
        }
        displayed = target.slice();
        if (doFlash && added.length) flash(added);
      }
      function cancelPump() { if (pump.raf) cancelAnimationFrame(pump.raf); pump.raf = 0; pump.target = null; }
      function startPump() {
        if (pump.raf) return;
        var step = function () {
          pump.raf = 0;
          var target = pump.target;
          if (!target) { clearCaret(); return; }
          if (displayed.length >= target.length) { clearCaret(); pump.target = null; return; }
          var frag = document.createDocumentFragment();
          var last = null;
          var limit = Math.min(displayed.length + 3, target.length);
          while (displayed.length < limit) {
            var idx = displayed.length;
            var node = makeLine(idx + 1, target[idx]);
            frag.appendChild(node);
            lineEls.push(node);
            displayed.push(target[idx]);
            last = node;
          }
          codeEl.appendChild(frag);
          setCaret(last);
          onGrow();
          pump.raf = requestAnimationFrame(step);
        };
        pump.raf = requestAnimationFrame(step);
      }
      return {
        el: wrap,
        set: function (content) { cancelPump(); clearCaret(); patch(splitLines(content), false); },
        stream: function (content) {
          var target = splitLines(content);
          var isPrefix = displayed.length <= target.length && (function () {
            for (var i = 0; i < displayed.length; i++) if (displayed[i] !== target[i]) return false;
            return true;
          })();
          if (isPrefix && target.length > displayed.length) { pump.target = target; startPump(); }
          else if (!arraysEqual(displayed, target)) { cancelPump(); patch(target, true); clearCaret(); onGrow(); }
        },
        lineCount: function () { return displayed.length; },
        destroy: function () { cancelPump(); flashTimers.forEach(function (t) { clearTimeout(t); }); },
      };
    }

    // =================================================================
    // FileWriteCard — a tool-call row (`Write blog/models.py  +24`) with an
    // embedded, collapsible code block. The bold verb is the tool used.
    // =================================================================
    var VERB_BY_ACTION = {
      created: "Write", updated: "Edit", overwritten: "Edit",
      deleted: "Delete", renamed: "Move", writing: "Write", read: "Read",
    };
    function actionKey(a) { return String(a == null ? "" : a).replace(/[^a-z]/gi, "").toLowerCase(); }
    function diffStat(added, removed, key) {
      if (key === "deleted" || added == null) return "";
      if (key === "created") return added ? "+" + added : "";
      var parts = [];
      if (added) parts.push("+" + added);
      if (removed) parts.push("−" + removed);
      return parts.join(" ");
    }

    function getFileUrl() { return model && model.run && model.run.fileUrl; }
    function createFileWriteCard(path, action) {
      var card = el("div", "fcard tool-row");
      card.dataset.path = path;
      var head = el("button", "fcard-head");
      head.type = "button";
      head.innerHTML =
        '<span class="tool-verb"></span>' +
        '<span class="tool-target"></span>' +
        '<span class="tool-stat"></span>' +
        '<span class="fcard-toggle">▾</span>';
      var verbEl = head.querySelector(".tool-verb");
      var targetEl = head.querySelector(".tool-target");
      var statEl = head.querySelector(".tool-stat");
      var toggleEl = head.querySelector(".fcard-toggle");
      targetEl.textContent = path;
      var body = el("div", "fcard-body");
      var block = createCodeBlock(langFromPath(path));
      body.appendChild(block.el);
      card.appendChild(head);
      card.appendChild(body);

      var committed = false;
      var loaded = false;
      function applyAction(a, stat) {
        var key = actionKey(a) || "created";
        card.dataset.action = key;
        verbEl.textContent = VERB_BY_ACTION[key] || "Edit";
        if (stat !== undefined) statEl.textContent = stat;
      }
      function collapse(state) {
        card.classList.toggle("collapsed", state);
        toggleEl.textContent = state ? "▸" : "▾";
        if (!state && committed && !loaded) loadCommitted();
      }
      function loadCommitted() {
        var url = getFileUrl();
        if (!url) return;
        loaded = true;
        fetch(url + "?path=" + encodeURIComponent(path))
          .then(function (r) { return r.json(); })
          .then(function (d) { if (d && d.content != null && !d.binary) block.set(d.content); })
          .catch(function () { loaded = false; });
      }
      head.addEventListener("click", function () { collapse(!card.classList.contains("collapsed")); });
      applyAction(action || "created", "");

      return {
        el: card,
        start: function () { card.classList.add("writing"); collapse(false); statEl.textContent = "writing…"; },
        stream: function (content) { card.classList.add("writing"); block.stream(content); },
        end: function (a, content, added, removed) {
          card.classList.remove("writing");
          if (content != null && block.lineCount() === 0) block.set(content);
          var key = actionKey(a) || "created";
          applyAction(key, diffStat(added, removed, key));
          // long writes start collapsed to keep the timeline tidy
          if (block.lineCount() > 30) collapse(true);
        },
        setAction: function (a) { applyAction(a, ""); },
        markCommitted: function () { committed = true; collapse(true); },
        rename: function (to) { path = to; targetEl.textContent = to; card.dataset.path = to; },
        destroy: function () { block.destroy(); },
      };
    }

    // =================================================================
    // AgentStatusMessage (StepCard) — one structured agent step.
    // =================================================================
    function createStepCard(log) {
      var role = resolveRole(log);
      var card = el("div", "step-card " + role.cls);
      card.dataset.logId = log.id;
      card.innerHTML =
        '<div class="step-head">' +
        '  <span class="agent-icon ' + role.cls + '">' + role.icon + "</span>" +
        '  <span class="agent-name">' + role.name + "</span>" +
        '  <span class="role-badge ' + role.cls + '">' + role.role + "</span>" +
        '  <span class="step-action"></span>' +
        '  <span class="step-time">' + formatTime(log.created_at) + "</span>" +
        "</div>" +
        '<div class="step-body"></div>';
      var body = card.querySelector(".step-body");
      var actionEl = card.querySelector(".step-action");
      var agentKey = String(log.agent || "").toLowerCase();
      var noopFileCard = {
        start: function () {}, stream: function () {}, end: function () {},
        setAction: function () {}, markCommitted: function () {}, rename: function () {}, destroy: function () {},
      };

      // A resolved Q&A renders as a messenger pair: the model's question (left) + the user's answer
      // (right), instead of one mixed card.
      if (agentKey === "question") {
        card.className = "qa-pair";
        return {
          el: card,
          role: role,
          isCoder: false,
          update: function (log) {
            var s = String(log.stdout || "");
            var i = s.indexOf("\nYou:");
            var q = (i >= 0 ? s.slice(0, i) : s).replace(/^Q:\s*/, "").trim();
            var a = i >= 0 ? s.slice(i + "\nYou:".length).trim() : "";
            card.innerHTML = "";
            var qb = el("div", "chat-msg chat-msg--in");
            var qp = el("div", "step-prose step-md"); qp.innerHTML = mdToHtml(q); qb.appendChild(qp);
            card.appendChild(qb);
            if (a) {
              var ab = el("div", "chat-msg chat-msg--out");
              var ap = el("div", "step-prose"); ap.textContent = a; ab.appendChild(ap);
              card.appendChild(ab);
            }
          },
          ensureFileCard: function () { return noopFileCard; },
          renameFileCard: function () { return noopFileCard; },
          destroy: function () {},
        };
      }

      // `tasks` steps render the model's todo list as a checklist, not prose/code.
      var isTasks = agentKey === "tasks";
      if (isTasks) {
        var taskListEl = el("div", "task-list");
        body.appendChild(taskListEl);
        return {
          el: card,
          role: role,
          isCoder: false,
          update: function (log) { renderTaskList(taskListEl, parseTasks(log.stdout || "")); },
          ensureFileCard: function () { return noopFileCard; },
          renameFileCard: function () { return noopFileCard; },
          destroy: function () {},
        };
      }

      // Messenger bubbles: the user's own message (right) and the model's final answer (left). All
      // other steps (thinking / plan / read / write / test …) keep the inline rail look.
      if (agentKey === "user") card.classList.add("chat-msg", "chat-msg--out");
      else if (agentKey === "finish") card.classList.add("chat-msg", "chat-msg--in");

      var commandEl = null;
      // The model's final answer renders as markdown (headings/bold/lists/inline code); all other
      // steps keep the plain typing animation. Fenced code still becomes dedicated code cards.
      var isFinish = agentKey === "finish";
      var mdEl = null;
      var proseTyper = createTyper("step-prose");
      if (isFinish) {
        mdEl = el("div", "step-prose step-md");
        body.appendChild(mdEl);
      } else {
        body.appendChild(proseTyper.el);
      }
      var codesEl = el("div", "step-codes");
      body.appendChild(codesEl);
      var filesEl = el("div", "step-files");
      body.appendChild(filesEl);
      var stderrTyper = null;
      var resultEl = el("div", "step-result");
      body.appendChild(resultEl);

      var fileCards = new Map();
      var genericCount = 0; // how many non-file (generic) code fences we've rendered

      function ensureStderr() {
        if (!stderrTyper) {
          stderrTyper = createTyper("step-stderr");
          body.insertBefore(stderrTyper.el, resultEl);
        }
        return stderrTyper;
      }
      function setResult(log) {
        var bits = [];
        if (log.exit_code !== null && log.exit_code !== undefined) bits.push("exit " + log.exit_code);
        if (log.duration_seconds != null) bits.push(Number(log.duration_seconds).toFixed(1) + "s");
        resultEl.textContent = bits.join(" · ");
        resultEl.classList.toggle("error", log.exit_code != null && log.exit_code !== 0);
      }

      return {
        el: card,
        role: role,
        isCoder: role.role === "CODING",
        update: function (log, instant) {
          // write/edit steps (kind="model") surface their action as the embedded file card,
          // so we hide their raw invocation command. Every other tool step shows its call as a
          // tool-row whose bold verb comes from the agent identity (Read / Grep / Run / Test…).
          if (log.command && !commandEl && log.kind !== "model") {
            commandEl = el("div", "tool-row step-command");
            commandEl.innerHTML = '<span class="tool-verb"></span><span class="tool-target"></span>';
            commandEl.querySelector(".tool-verb").textContent = (role && role.cmd) || "Bash";
            commandEl.querySelector(".tool-target").textContent = log.command;
            body.insertBefore(commandEl, proseTyper.el);
          }
          // Backfilled (historical) messages render instantly; live ones type out.
          // Final answers render as markdown (arrive complete in one log, so no typing needed).
          if (isFinish) mdEl.innerHTML = mdToHtml(prose(log.stdout || ""));
          else if (instant) proseTyper.set(prose(log.stdout || ""));
          else proseTyper.write(prose(log.stdout || ""));
          // Render only generic (non-file) code fences we haven't rendered yet, so a log
          // whose stdout grows across polls adds new snippets instead of being frozen
          // after the first render.
          var fences = nonFileFences(log.stdout || "", log.kind !== "model");
          for (var gi = genericCount; gi < fences.length; gi++) {
            var cb = createCodeBlock(fences[gi].lang || "plaintext");
            cb.set(fences[gi].code);
            var wrap = el("div", "step-code");
            wrap.appendChild(cb.el);
            codesEl.appendChild(wrap);
          }
          genericCount = fences.length;
          if (log.stderr) { var st = ensureStderr(); if (instant) st.set(log.stderr); else st.write(log.stderr); }
          setResult(log);
        },
        ensureFileCard: function (path, action) {
          var c = fileCards.get(path);
          if (!c) {
            c = createFileWriteCard(path, action);
            fileCards.set(path, c);
            filesEl.appendChild(c.el);
            actionEl.textContent = "editing files";
          } else if (action) {
            c.setAction(action);
          }
          return c;
        },
        renameFileCard: function (from, to) {
          var c = fileCards.get(from);
          if (!c) return null;
          fileCards.delete(from);
          c.rename(to);
          c.setAction("renamed");
          fileCards.set(to, c);
          return c;
        },
        destroy: function () {
          proseTyper.destroy();
          if (stderrTyper) stderrTyper.destroy();
          fileCards.forEach(function (c) { c.destroy(); });
        },
      };
    }

    // =================================================================
    // ActivityTimeline — Claude-style live spinner: a cycling glyph + a
    // whimsical gerund + elapsed seconds while the run works, settling to
    // a solid Done./Failed./Stopped. line. Safe, generic labels only —
    // never any private reasoning.
    // =================================================================
    var SPINNER_WORDS = [
      "Accomplishing", "Baking", "Brewing", "Calculating", "Cerebrating", "Churning",
      "Coalescing", "Cogitating", "Computing", "Conjuring", "Considering", "Cooking",
      "Crafting", "Creating", "Crunching", "Deliberating", "Determining", "Doing",
      "Effecting", "Elucidating", "Envisioning", "Finagling", "Forging", "Forming",
      "Generating", "Hatching", "Herding", "Hustling", "Ideating", "Inferring",
      "Manifesting", "Marinating", "Moseying", "Mulling", "Mustering", "Musing",
      "Noodling", "Percolating", "Pondering", "Processing", "Puttering", "Reticulating",
      "Ruminating", "Schlepping", "Shucking", "Simmering", "Smooshing", "Spinning",
      "Stewing", "Synthesizing", "Thinking", "Transmuting", "Vibing", "Working",
    ];
    var SPINNER_FRAMES = ["✶", "✸", "✹", "✺", "✹", "✷"];

    function createActivity() {
      var wrap = el("div", "activity");
      wrap.hidden = true;
      var live = el("div", "activity-live");
      live.innerHTML = '<span class="activity-glyph"></span><span class="activity-label"></span>';
      var glyphEl = live.querySelector(".activity-glyph");
      var labelEl = live.querySelector(".activity-label");
      wrap.appendChild(live);

      var timer = 0, frame = 0, ticks = 0, word = "", completed = false, blocked = false;
      // Elapsed is anchored to a server-reported baseline (baseElapsed @ baseAt) so a page
      // reload continues counting from the run's real start instead of resetting to 0. While
      // the run is live the local clock advances from the anchor; once complete it freezes.
      var baseElapsed = 0, baseAt = 0, tokens = 0, doneStatus = null;

      function pickWord() { return SPINNER_WORDS[Math.floor(Math.random() * SPINNER_WORDS.length)]; }
      function elapsed() {
        var live = (!completed && baseAt) ? (Date.now() - baseAt) / 1000 : 0;
        return Math.max(0, Math.round(baseElapsed + live));
      }
      function tokenSuffix() { return tokens ? " · " + tokens.toLocaleString() + " tokens" : ""; }
      function tick() {
        if (blocked) { glyphEl.textContent = "?"; labelEl.textContent = "Waiting for your response…"; return; }
        ticks++;
        frame = (frame + 1) % SPINNER_FRAMES.length;
        glyphEl.textContent = SPINNER_FRAMES[frame];
        if (ticks % 30 === 0) word = pickWord(); // ~4s
        labelEl.textContent = word + "… (" + elapsed() + "s)" + tokenSuffix();
      }
      function startSpinner() {
        if (timer || completed) return;
        wrap.hidden = false;
        wrap.classList.remove("complete");
        wrap.dataset.phase = "running";
        // Do NOT self-anchor here: only sync() (with the server's started_at) anchors the
        // clock, so a not-yet-started (queued) run shows a steady 0s instead of ticking up.
        ticks = 0;
        word = pickWord();
        if (reduceMotion) {
          glyphEl.textContent = SPINNER_FRAMES[0];
          labelEl.textContent = word + "… (" + elapsed() + "s)" + tokenSuffix();
          return; // no interval — honour reduced motion
        }
        glyphEl.textContent = SPINNER_FRAMES[0];
        labelEl.textContent = word + "… (" + elapsed() + "s)" + tokenSuffix();
        timer = window.setInterval(tick, 130);
      }
      function stopSpinner() { if (timer) { clearInterval(timer); timer = 0; } }
      function renderDone(status) {
        var label = "Done.", phase = "done", glyph = "✓";
        if (status === "failed") { label = "Failed."; phase = "failed"; glyph = "✕"; }
        else if (status === "cancelled" || status === "stopped") { label = "Stopped."; phase = "ended"; glyph = "■"; }
        wrap.hidden = false;
        wrap.classList.add("complete");
        wrap.dataset.phase = phase;
        glyphEl.textContent = glyph;
        var meta = [];
        if (baseElapsed || baseAt) meta.push(elapsed() + "s");
        if (tokens) meta.push(tokens.toLocaleString() + " tokens");
        labelEl.textContent = label + (meta.length ? " · " + meta.join(" · ") : "");
      }

      return {
        el: wrap,
        // Specific actions render as timeline tool rows; the spinner stays whimsical.
        push: function () {},
        ensureRunning: function () {
          // A NEW turn can start after a previous one completed (follow-up message, Implement
          // plan…). Without clearing the latch the spinner can never restart: complete() set
          // `completed`, startSpinner() early-returns on it, and sync() keeps repainting the
          // stale "Done." line with LIVE time/tokens — "shows done even if it's not done".
          if (completed) {
            completed = false;
            doneStatus = null;
            wrap.classList.remove("complete");
            baseElapsed = 0; baseAt = 0; // fresh clock; the next sync() re-anchors to the new turn
          }
          startSpinner();
        },
        // While the agent is blocked on a question/approval, show a clear "waiting on you" state
        // instead of the cycling gerund (so the user knows their input is required).
        setBlocked: function (b) {
          blocked = !!b && !completed;
          if (blocked) {
            wrap.hidden = false;
            wrap.classList.remove("complete");
            glyphEl.textContent = "?";
            labelEl.textContent = "Waiting for your response…";
          }
        },
        // Anchor the timer + token counter to the server's reload-safe values.
        sync: function (elapsedSeconds, tok, startedAt) {
          // Only a run that has actually started (server reports started_at) anchors the clock.
          // baseElapsed always tracks the server value (so a finished run on reload shows its
          // real duration), but baseAt advances only while running — once complete the time is
          // pinned to the server's elapsed and never ticks or jumps around.
          if (startedAt && elapsedSeconds != null && !isNaN(elapsedSeconds)) {
            baseElapsed = Number(elapsedSeconds);
            if (!completed) baseAt = Date.now();
          }
          if (tok != null && !isNaN(tok)) tokens = Number(tok);
          if (completed && doneStatus) renderDone(doneStatus);            // refresh the static line
          else if (!timer && reduceMotion && baseAt) labelEl.textContent = word + "… (" + elapsed() + "s)" + tokenSuffix();
        },
        complete: function (status) {
          var phase = status === "failed" ? "failed" : (status === "cancelled" || status === "stopped") ? "ended" : "done";
          if (completed && wrap.dataset.phase === phase) { renderDone(status); return; } // idempotent, but refresh tokens
          stopSpinner();
          if (!completed) baseElapsed = elapsed(); // freeze at the exact displayed value (no dip before next sync)
          completed = true;
          doneStatus = status;
          renderDone(status);
          onGrow();
        },
        // End the turn WITHOUT any status line: the run finished but produced no final summary,
        // so a "Done." tag would overstate what happened — the line is removed entirely.
        // doneStatus stays null so sync() never repaints it; ensureRunning() still restarts cleanly.
        dismiss: function () {
          stopSpinner();
          completed = true;
          doneStatus = null;
          wrap.classList.remove("complete");
          wrap.hidden = true;
          wrap.dataset.phase = "";
        },
        reset: function () {
          stopSpinner();
          completed = false;
          blocked = false;
          doneStatus = null;
          baseElapsed = 0; baseAt = 0; tokens = 0;
          wrap.classList.remove("complete");
          wrap.hidden = true;
          wrap.dataset.phase = "";
          glyphEl.textContent = "";
          labelEl.textContent = "";
        },
      };
    }

    // =================================================================
    // ChatTimeline — orchestrator
    // =================================================================
    var steps = new Map();          // logId -> StepCard
    var lastCoderStepId = null;
    var lastStepId = null;
    var currentEl = null;           // the latest step (its rail dot is highlighted)
    var ingestCount = 0;            // poll counter for this run
    var hydrating = true;           // first poll = historical backfill → render instantly
    var subs = [];
    var activity = createActivity();
    feed.appendChild(activity.el);

    function placeholder(show) {
      var existing = feed.querySelector("#chat-empty");
      if (show && !existing) {
        var p = el("div", "chat-empty chat-loading");
        p.id = "chat-empty";
        p.setAttribute("role", "status");
        p.setAttribute("aria-live", "polite");
        var spin = el("span", "chat-loading-spinner");
        spin.setAttribute("aria-hidden", "true");
        var text = el("span", "chat-loading-text");
        text.textContent = "Waiting for the first agent step";
        var dots = el("span", "chat-loading-dots");
        dots.innerHTML = "<i></i><i></i><i></i>";
        text.appendChild(dots);
        p.appendChild(spin);
        p.appendChild(text);
        feed.insertBefore(p, activity.el);
      } else if (!show && existing) {
        existing.remove();
      }
    }

    function resolveStep(logId) {
      if (logId != null && steps.has(logId)) return steps.get(logId);
      if (lastCoderStepId != null && steps.has(lastCoderStepId)) return steps.get(lastCoderStepId);
      if (lastStepId != null && steps.has(lastStepId)) return steps.get(lastStepId);
      return null;
    }

    function ingestLogs(data) {
      // The first poll after a run is selected is a historical backfill — render those
      // messages instantly (no typing). `hydrating` stays true through that poll's file:*
      // events too (they fire right after this call, before the next ingest), then the
      // next poll flips it off so genuinely-live messages animate.
      ingestCount++;
      hydrating = ingestCount === 1;
      var logs = (data && data.logs) || [];
      logs.forEach(function (log) {
        var step = steps.get(log.id);
        if (!step) {
          placeholder(false);
          step = createStepCard(log);
          steps.set(log.id, step);
          feed.insertBefore(step.el, activity.el);
          lastStepId = log.id;
          if (step.isCoder) lastCoderStepId = log.id;
          if (currentEl) currentEl.classList.remove("current");
          step.el.classList.add("current");
          currentEl = step.el;
        }
        step.update(log, hydrating);
      });
      onGrow();
    }

    // ---- file:* lifecycle from the workbench bus → embedded chat cards ----
    function bind(type, fn) { if (bus) subs.push(bus.on(type, fn)); }
    bind("file:create", function (e) {
      var step = resolveStep(e.logId);
      if (!step) return; // nothing to attach to yet; the Files panel still shows it
      var card = step.ensureFileCard(e.path, e.action || "created");
      if (e.source === "index") card.markCommitted();
    });
    bind("file:write:start", function (e) {
      if (hydrating) return; // backfill: no writing animation; end() renders instantly
      var step = resolveStep(e.logId);
      if (step) step.ensureFileCard(e.path).start();
    });
    bind("file:write:chunk", function (e) {
      if (hydrating) return; // backfill: skip streaming; end() sets full content at once
      var step = resolveStep(e.logId);
      if (step) step.ensureFileCard(e.path).stream(e.content);
    });
    bind("file:write:end", function (e) {
      var step = resolveStep(e.logId);
      if (step) step.ensureFileCard(e.path).end(e.action, e.content, e.added, e.removed);
    });
    bind("file:update", function (e) {
      var step = resolveStep(null);
      if (step) step.ensureFileCard(e.path).setAction("updated");
    });
    bind("file:delete", function (e) {
      var step = resolveStep(null);
      if (step) { var c = step.ensureFileCard(e.path, "deleted"); c.setAction("deleted"); }
    });
    bind("file:rename", function (e) {
      var step = resolveStep(null);
      if (step && !step.renameFileCard(e.from, e.to)) step.ensureFileCard(e.to, "renamed");
    });

    // =================================================================
    // Interactive cards: the agent's open questions (A/B/C/D + Other) and
    // command-approval (Accept/Deny) prompts, driven by chat_logs_api `pending`.
    // =================================================================
    var pendingCards = new Map(); // interaction id -> card el (approvals)
    var qWizard = null;           // the one paginated card for the current batch of questions
    var qWizKey = "";             // joined pending-question ids the wizard is currently showing

    function respond(item, body) {
      var headers = { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest" };
      var token = model && model.run && model.run.csrfToken;
      if (token) headers["X-CSRFToken"] = token;
      // Resolves to a boolean: did the answer actually reach the server? Callers MUST handle
      // false by unlocking their card — a lost POST (server restart, network blip) used to
      // leave the card disabled while the agent waited 30 minutes for an answer that never came.
      return fetch(item.respond_url, { method: "POST", headers: headers, body: new URLSearchParams(body).toString() })
        .then(function (r) { return r.ok; })
        .catch(function () { return false; });
    }

    function createInteractionCard(item) {
      var card = el("div", "interaction-card chat-msg chat-msg--in " + (item.kind === "approval" ? "iact-approval" : "iact-question"));
      var head = el("div", "step-head");
      head.innerHTML = '<span class="agent-icon role-question">' + (item.kind === "approval" ? "!" : "?") +
        '</span><span class="agent-name">' + (item.kind === "approval" ? "Approval needed" : "Question") + "</span>";
      card.appendChild(head);
      var body = el("div", "iact-body");
      card.appendChild(body);
      var errEl = el("div", "iact-error");
      errEl.hidden = true;
      card.appendChild(errEl);

      function lock() {
        Array.prototype.forEach.call(card.querySelectorAll("button,input"), function (n) { n.disabled = true; });
        card.classList.add("iact-sent");
        errEl.hidden = true;
      }
      function unlock() {
        Array.prototype.forEach.call(card.querySelectorAll("button,input"), function (n) { n.disabled = false; });
        card.classList.remove("iact-sent");
        errEl.textContent = "Could not send your answer — is the server running? Try again.";
        errEl.hidden = false;
      }
      function send(payload) {
        lock();
        respond(item, payload).then(function (ok) { if (!ok) unlock(); });
      }

      if (item.kind === "approval") {
        var p = el("div", "iact-prompt");
        p.textContent = item.prompt || "The agent wants to run a command:";
        body.appendChild(p);
        if (item.command) {
          var pre = el("pre", "iact-command");
          pre.textContent = item.command;
          body.appendChild(pre);
        }
        var actions = el("div", "iact-actions");
        var accept = el("button", "iact-accept"); accept.type = "button"; accept.textContent = "Accept";
        var deny = el("button", "iact-deny"); deny.type = "button"; deny.textContent = "Deny";
        accept.addEventListener("click", function () { send({ decision: "approve" }); });
        deny.addEventListener("click", function () { send({ decision: "deny" }); });
        actions.appendChild(accept); actions.appendChild(deny);
        body.appendChild(actions);
      } else {
        var q = el("div", "iact-prompt step-md");
        q.innerHTML = mdToHtml(item.prompt || "");
        body.appendChild(q);
        var opts = el("div", "iact-options");
        (item.options || []).forEach(function (opt) {
          var b = el("button", "iact-opt"); b.type = "button";
          b.innerHTML = '<b>' + escapeHtml(opt.key || "") + "</b> " + escapeHtml(opt.label || "");
          b.addEventListener("click", function () { lock(); respond(item, { answer: opt.label || opt.key || "" }); });
          opts.appendChild(b);
        });
        body.appendChild(opts);
        var other = el("div", "iact-other");
        var input = el("input", "iact-other-input");
        input.setAttribute("placeholder", "Other — type your own answer…");
        var sendBtn = el("button", "iact-other-send"); sendBtn.type = "button"; sendBtn.textContent = "Send";
        function sendOther() {
          var v = (input.value || "").trim();
          if (!v) return;
          send({ answer: v });
        }
        sendBtn.addEventListener("click", sendOther);
        input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); sendOther(); } });
        other.appendChild(input); other.appendChild(sendBtn);
        body.appendChild(other);
      }
      return card;
    }

    // A paginated card for a BATCH of questions asked at once (planning). Shows one question at a
    // time (‹ X of N ›), numbered options with optional ⓘ descriptions + (Recommended), and submits
    // every answer together on Continue. Dismiss/ESC skips them all.
    function createQuestionWizard(questions) {
      var card = el("div", "interaction-card chat-msg chat-msg--in iact-wizard");
      var idx = 0, sent = false;
      // pre-select the Recommended option per question (if any)
      var sel = questions.map(function (q) {
        var r = -1;
        (q.options || []).forEach(function (o, i) { if (r < 0 && o.recommended) r = i; });
        return r >= 0 ? { type: "opt", i: r } : null;
      });

      var head = el("div", "wiz-q-head");
      var title = el("div", "wiz-q-title step-md");
      var pager = el("div", "wiz-q-pager");
      head.appendChild(title); head.appendChild(pager); card.appendChild(head);
      var optsWrap = el("div", "wiz-q-options"); card.appendChild(optsWrap);
      var otherWrap = el("div", "wiz-q-other");
      var otherInput = el("input", "wiz-q-other-input");
      otherInput.setAttribute("placeholder", "Other — type your own answer…");
      otherWrap.appendChild(otherInput); card.appendChild(otherWrap);
      var foot = el("div", "wiz-q-foot");
      var dismiss = el("button", "wiz-q-dismiss"); dismiss.type = "button"; dismiss.innerHTML = 'Dismiss <kbd>ESC</kbd>';
      var cont = el("button", "wiz-q-continue button primary"); cont.type = "button";
      foot.appendChild(dismiss); foot.appendChild(cont); card.appendChild(foot);
      var wizErr = el("div", "iact-error");
      wizErr.hidden = true;
      card.appendChild(wizErr);

      function render() {
        var q = questions[idx];
        title.innerHTML = mdToHtml(q.prompt || "");
        pager.innerHTML = "";
        if (questions.length > 1) {
          var prev = el("button", "wiz-pg-btn"); prev.type = "button"; prev.textContent = "‹"; prev.disabled = idx === 0;
          var lbl = el("span", "wiz-pg-lbl"); lbl.textContent = (idx + 1) + " of " + questions.length;
          var next = el("button", "wiz-pg-btn"); next.type = "button"; next.textContent = "›"; next.disabled = idx === questions.length - 1;
          prev.addEventListener("click", function () { if (idx > 0) { idx--; render(); } });
          next.addEventListener("click", function () { if (idx < questions.length - 1) { idx++; render(); } });
          pager.appendChild(prev); pager.appendChild(lbl); pager.appendChild(next);
        }
        optsWrap.innerHTML = "";
        (q.options || []).forEach(function (o, i) {
          var row = el("button", "wiz-opt" + (sel[idx] && sel[idx].type === "opt" && sel[idx].i === i ? " selected" : ""));
          row.type = "button";
          var num = el("span", "wiz-opt-num"); num.textContent = (i + 1) + ".";
          var lab = el("span", "wiz-opt-label"); lab.textContent = o.label || o.key || "";
          row.appendChild(num); row.appendChild(lab);
          if (o.description) {
            var info = el("span", "wiz-opt-info"); info.textContent = "ⓘ"; info.title = o.description;
            info.setAttribute("aria-label", o.description);
            row.appendChild(info);
          }
          row.addEventListener("click", function () { sel[idx] = { type: "opt", i: i }; otherInput.value = ""; render(); });
          optsWrap.appendChild(row);
        });
        otherInput.value = (sel[idx] && sel[idx].type === "other") ? sel[idx].text : "";
        cont.innerHTML = (idx < questions.length - 1 ? "Continue" : "Submit") + ' <kbd>⏎</kbd>';
      }

      otherInput.addEventListener("input", function () {
        var v = (otherInput.value || "").trim();
        sel[idx] = v ? { type: "other", text: v } : null;
        Array.prototype.forEach.call(optsWrap.querySelectorAll(".wiz-opt"), function (r) { r.classList.remove("selected"); });
      });

      function answerFor(i) {
        var s = sel[i];
        if (!s) return "";
        if (s.type === "other") return s.text;
        var o = (questions[i].options || [])[s.i];
        return o ? (o.label || o.key || "") : "";
      }
      function submitAll(skip) {
        if (sent) return;
        sent = true;
        wizErr.hidden = true;
        Array.prototype.forEach.call(card.querySelectorAll("button,input"), function (n) { n.disabled = true; });
        card.classList.add("iact-sent");
        var posts = questions.map(function (q, i) {
          return respond(q, { answer: skip ? "(skipped)" : (answerFor(i) || "(no answer)") });
        });
        Promise.all(posts).then(function (oks) {
          if (oks.every(Boolean)) { if (card._cleanup) card._cleanup(); return; }
          // A lost answer must NEVER leave a dead, disabled card while the agent waits 30
          // minutes server-side — re-enable everything so the user can simply submit again
          // (already-delivered answers are idempotent: the API answers {ok, already} for them).
          sent = false;
          card.classList.remove("iact-sent");
          Array.prototype.forEach.call(card.querySelectorAll("button,input"), function (n) { n.disabled = false; });
          render();
          wizErr.textContent = "Could not send your answers — is the server running? Try again.";
          wizErr.hidden = false;
        });
      }
      function advance() { if (idx < questions.length - 1) { idx++; render(); } else submitAll(false); }
      cont.addEventListener("click", advance);
      dismiss.addEventListener("click", function () { submitAll(true); });

      function onKey(e) {
        if (sent || !card.isConnected) return;
        // Don't act if the wizard isn't actually visible (e.g. it lives in a CLOSED Prompt Writer
        // drawer) — otherwise Escape elsewhere on the page would silently skip the hidden interview.
        var drawer = card.closest(".pw-drawer");
        if (drawer && !drawer.classList.contains("open")) return;
        var ae = document.activeElement;
        var inCard = ae ? card.contains(ae) : false;
        // Focus is on some OTHER control/field (a composer, a menu, …) → never hijack its keys.
        if (ae && ae !== document.body && !inCard) return;
        // Exactly one wizard owns page-level (body-focused) keys at a time: if the Prompt Writer
        // drawer is open, its wizard wins, so a main-chat wizard defers (no double-skip/advance).
        var openDrawer = document.querySelector(".pw-drawer.open");
        if (openDrawer && !openDrawer.contains(card)) return;
        if (ae === otherInput) {  // typing a free-form answer: commit on Enter, just blur on Escape
          if (e.key === "Enter") { e.preventDefault(); advance(); }
          else if (e.key === "Escape") { e.preventDefault(); otherInput.blur(); }
          return;
        }
        if (e.key === "Escape") { e.preventDefault(); submitAll(true); return; }
        if (e.key === "Enter") { e.preventDefault(); advance(); return; }
        var opts = questions[idx].options || [];
        var cur = (sel[idx] && sel[idx].type === "opt") ? sel[idx].i : -1;
        if (e.key === "ArrowDown") { e.preventDefault(); sel[idx] = { type: "opt", i: Math.min(opts.length - 1, cur + 1) }; render(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); sel[idx] = { type: "opt", i: Math.max(0, (cur < 0 ? opts.length : cur) - 1) }; render(); }
        else if (e.key === "ArrowLeft") { if (idx > 0) { idx--; render(); } }
        else if (e.key === "ArrowRight") { if (idx < questions.length - 1) { idx++; render(); } }
        else if (/^[1-9]$/.test(e.key)) { var n = parseInt(e.key, 10) - 1; if (n < opts.length) { sel[idx] = { type: "opt", i: n }; render(); } }
      }
      document.addEventListener("keydown", onKey);
      card._cleanup = function () { document.removeEventListener("keydown", onKey); };

      render();
      return card;
    }

    function clearWizard() {
      if (qWizard) { if (qWizard._cleanup) qWizard._cleanup(); qWizard.remove(); qWizard = null; qWizKey = ""; }
    }

    function setPending(list) {
      list = list || [];
      var questions = list.filter(function (i) { return i.kind === "question"; });
      var approvals = list.filter(function (i) { return i.kind !== "question"; });

      // approvals — one card each (unchanged)
      var ids = new Set();
      approvals.forEach(function (item) {
        ids.add(item.id);
        if (!pendingCards.has(item.id)) {
          placeholder(false);
          var card = createInteractionCard(item);
          pendingCards.set(item.id, card);
          feed.insertBefore(card, activity.el);
          onGrow();
        }
      });
      pendingCards.forEach(function (card, id) {
        if (!ids.has(id)) { card.remove(); pendingCards.delete(id); }
      });

      // questions — one paginated wizard for the whole pending batch
      var key = questions.map(function (q) { return q.id; }).sort(function (a, b) { return a - b; }).join(",");
      if (questions.length) {
        if (key !== qWizKey) {
          clearWizard();
          placeholder(false);
          qWizard = createQuestionWizard(questions);
          qWizKey = key;
          feed.insertBefore(qWizard, activity.el);
          onGrow();
        }
      } else {
        clearWizard();
      }
      activity.setBlocked(pendingCards.size > 0 || !!qWizard);
    }

    function setRunStatus(status, opts) {
      var finished = status === "succeeded" || status === "failed" || status === "cancelled" || status === "stopped";
      if (finished) {
        if (currentEl) { currentEl.classList.remove("current"); currentEl = null; }
        // "Done." is shown ONLY when the turn really ended with a final summary (a finish card
        // saying what was done). A succeeded turn without one gets NO status tag at all.
        // Failed/Stopped always show — those are important signals, not celebrations.
        if (status === "succeeded" && opts && opts.hasSummary === false) activity.dismiss();
        else activity.complete(status);
      } else {
        activity.ensureRunning();
      }
    }

    // Reload-safe run progress: the backend reports the run's real elapsed time and token
    // totals each poll; the activity timer anchors to them so a refresh keeps counting.
    function setProgress(p) {
      if (!p) return;
      activity.sync(p.elapsedSeconds, p.tokens, p.startedAt);
    }

    function reset() {
      steps.forEach(function (s) { s.el.remove(); s.destroy(); });
      steps.clear();
      pendingCards.forEach(function (c) { c.remove(); });
      pendingCards.clear();
      clearWizard();
      lastCoderStepId = null;
      lastStepId = null;
      currentEl = null;
      ingestCount = 0;
      hydrating = true;
      activity.reset();
      // keep activity element + intro; (re)show the waiting placeholder
      if (activity.el.parentNode !== feed) feed.appendChild(activity.el);
      placeholder(true);
    }

    function destroy() {
      subs.forEach(function (off) { off(); });
      subs = [];
      steps.forEach(function (s) { s.destroy(); });
      steps.clear();
      clearWizard();
      activity.reset(); // stops the spinner interval
    }

    return {
      ingestLogs: ingestLogs,
      setRunStatus: setRunStatus,
      setProgress: setProgress,
      setPending: setPending,
      reset: reset,
      destroy: destroy,
      _debug: { steps: steps, activity: activity },
    };
  }

  // Exposed so the task-list modal (modals.js) renders the list identically to the inline card.
  window.AgentChat = { create: create, renderTaskList: renderTaskList };
})();
