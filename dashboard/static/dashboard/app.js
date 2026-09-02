(function () {
  function confirmDestructiveForms() {
    document.querySelectorAll("form[data-confirm]").forEach((form) => {
      form.addEventListener("submit", (event) => {
        if (form.dataset.kmConfirmed === "1") { form.dataset.kmConfirmed = ""; return; } // already confirmed
        event.preventDefault();
        const ask = window.KmConfirm
          ? window.KmConfirm({
              title: form.dataset.confirmTitle || "Are you sure?",
              body: form.dataset.confirm,
              confirmLabel: form.dataset.confirmLabel || "Delete",
              danger: true,
            })
          : Promise.resolve(window.confirm(form.dataset.confirm));
        ask.then((ok) => {
          if (!ok) return;
          form.dataset.kmConfirmed = "1";
          if (form.requestSubmit) form.requestSubmit();
          else form.submit();
        });
      });
    });
  }

  const STATUS_LABELS = {
    queued: "Laukia",
    running: "Vykdoma",
    succeeded: "Baigta",
    failed: "Nepavyko",
    cancelled: "Atšaukta",
    stopped: "Sustabdyta",
  };

  function pollBenchmarkTimeline() {
    const layout = document.querySelector("[data-benchmark-id]");
    if (!layout) return;
    const benchmarkId = layout.dataset.benchmarkId;
    const statusNode = document.getElementById("benchmark-status");

    async function refresh() {
      const response = await fetch(`/api/benchmarks/${benchmarkId}/status/`);
      if (!response.ok) return;
      const data = await response.json();
      if (statusNode) {
        statusNode.textContent = data.status_label || STATUS_LABELS[data.status] || data.status;
        statusNode.className = `badge ${data.status}`;
      }
      data.model_runs.forEach((run) => {
        const row = document.querySelector(`[data-model-run-id="${run.id}"]`);
        if (!row) return;
        const badge = row.querySelector(".badge");
        const dot = row.querySelector(".timeline-dot");
        if (badge) {
          badge.textContent = run.status_label || STATUS_LABELS[run.status] || run.status;
          badge.className = `badge ${run.status}`;
        }
        if (dot) {
          dot.className = `timeline-dot ${run.status}`;
        }
      });
    }

    refresh();
    window.setInterval(refresh, 2500);
  }

  function pollLogs() {
    const viewer = document.getElementById("log-viewer");
    if (!viewer) return;
    const modelRunId = viewer.dataset.modelRunId;
    let after = Number(viewer.querySelector("[data-log-id]:last-of-type")?.dataset.logId || 0);

    async function refresh() {
      const response = await fetch(`/api/model-runs/${modelRunId}/logs/?after=${after}`);
      if (!response.ok) return;
      const data = await response.json();
      updateModelRunSummary(data.model_run);
      const empty = viewer.querySelector(".empty");
      if (empty && data.logs.length) empty.remove();
      data.logs.forEach((log) => {
        after = Math.max(after, log.id);
        upsertLog(viewer, log);
      });
      if (data.logs.length) {
        applyLogFilters();
        viewer.scrollTop = viewer.scrollHeight;
      }
    }

    refresh();
    window.setInterval(refresh, 1800);
  }

  function updateModelRunSummary(run) {
    if (!run) return;
    const status = document.getElementById("model-run-status");
    const duration = document.getElementById("model-run-duration");
    const returnCode = document.getElementById("model-run-return-code");
    const fileCount = document.getElementById("model-run-file-count");
    if (status) {
      status.textContent = run.status_label || STATUS_LABELS[run.status] || run.status;
      status.className = `badge ${run.status}`;
    }
    if (duration) {
      duration.textContent = run.duration_seconds == null ? "–" : `${Number(run.duration_seconds).toFixed(1)} s`;
    }
    if (returnCode) {
      returnCode.textContent = run.return_code == null ? "–" : String(run.return_code);
    }
    if (fileCount) {
      fileCount.textContent = String(run.generated_files_count ?? 0);
    }
  }

  function upsertLog(viewer, log) {
    const existing = viewer.querySelector(`[data-log-id="${log.id}"]`);
    const rendered = renderLog(log);
    if (existing) {
      existing.replaceWith(rendered);
      return;
    }
    viewer.appendChild(rendered);
  }

  function renderLog(log) {
    const entry = document.createElement("div");
    entry.className = "log-entry";
    entry.dataset.logId = log.id;
    entry.dataset.kind = log.kind;
    entry.innerHTML = [
      `<div class="log-meta">${kindLabel(log.kind)} · kodas ${log.exit_code ?? "–"}</div>`,
      `<pre>$ ${escapeHtml(log.command || "")}</pre>`,
      log.stdout ? `<pre>${escapeHtml(log.stdout)}</pre>` : "",
      log.stderr ? `<pre class="stderr">${escapeHtml(log.stderr)}</pre>` : "",
    ].join("");
    return entry;
  }

  async function setupCharts() {
    const performance = document.getElementById("performance-chart");
    const duration = document.getElementById("duration-chart");
    if (!performance && !duration) return;
    const response = await fetch("/api/analytics/");
    if (!response.ok) return;
    const data = await response.json();
    const rows = data.model_runs || [];
    if (!window.Chart) {
      renderFallback("performance-fallback", rows, "success", "%");
      renderFallback("duration-fallback", rows, "duration", " s");
      return;
    }
    if (performance) {
      new Chart(performance, {
        type: "bar",
        data: {
          labels: rows.map((row) => row.model),
          datasets: [{ label: "Sėkmės procentas", data: rows.map((row) => row.success), backgroundColor: "#38bdf8" }],
        },
        options: chartOptions("%"),
      });
    }
    if (duration) {
      new Chart(duration, {
        type: "bar",
        data: {
          labels: rows.map((row) => row.model),
          datasets: [{ label: "Trukmė sekundėmis", data: rows.map((row) => row.duration), backgroundColor: "#22c55e" }],
        },
        options: chartOptions(" s"),
      });
    }
  }

  function renderFallback(targetId, rows, key, suffix) {
    const target = document.getElementById(targetId);
    if (!target) return;
    const max = Math.max(...rows.map((row) => Number(row[key]) || 0), 1);
    target.innerHTML = rows.length
      ? rows
          .map((row) => {
            const value = Number(row[key]) || 0;
            const width = Math.max((value / max) * 100, 2);
            return `<div class="bar-row"><span>${escapeHtml(row.model)}</span><strong style="width:${width}%"></strong><em>${value}${suffix}</em></div>`;
          })
          .join("")
      : '<div class="empty">Diagramai dar nėra duomenų.</div>';
  }

  function chartOptions(suffix) {
    return {
      responsive: true,
      plugins: { legend: { labels: { color: "#edf2ff" } } },
      scales: {
        x: { ticks: { color: "#94a3b8" }, grid: { color: "#243044" } },
        y: { ticks: { color: "#94a3b8", callback: (value) => `${value}${suffix}` }, grid: { color: "#243044" } },
      },
    };
  }

  function kindLabel(kind) {
    return { model: "Generavimas", test: "Testai", preview: "Peržiūra", system: "Sistema" }[kind] || kind;
  }

  function setupLogTools() {
    const search = document.getElementById("log-search");
    const filter = document.getElementById("log-filter");
    if (search) search.addEventListener("input", applyLogFilters);
    if (filter) filter.addEventListener("change", applyLogFilters);
  }

  function applyLogFilters() {
    const search = document.getElementById("log-search");
    const filter = document.getElementById("log-filter");
    const term = (search?.value || "").toLowerCase();
    const kind = filter?.value || "";
    document.querySelectorAll(".log-entry").forEach((entry) => {
      const matchesKind = !kind || entry.dataset.kind === kind;
      const matchesText = !term || entry.textContent.toLowerCase().includes(term);
      entry.hidden = !(matchesKind && matchesText);
    });
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  // CSRF token for fetch-based POSTs (preview lifecycle). Prefer a rendered form input,
  // fall back to the cookie Django sets when CSRF_COOKIE_HTTPONLY is False (the default).
  function getCsrfToken() {
    const input = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (input && input.value) return input.value;
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  // ===================================================================
  // Chat-style benchmark execution page (/benchmarks/<id>/).
  // Self-contained: activates only when [data-chat-benchmark-id] is present,
  // so the legacy pollers above stay dormant here and untouched elsewhere.
  // It reuses the existing endpoints:
  //   GET /api/benchmarks/<id>/status/      -> overall status + per-run tests
  //   GET /api/model-runs/<id>/logs/        -> chat bubbles, files, preview
  //   GET /api/model-runs/<id>/file/?path=  -> file viewer content
  // ===================================================================
  const STATUS_EN = {
    queued: "Queued",
    running: "Running…",
    succeeded: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
    stopped: "Stopped",
  };

  // The chat feed (agent timeline) is rendered by the dedicated chat.js module; the
  // role maps, rich-text/code-block renderers and streaming live there now.
  function nowClock() {
    try {
      return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch (err) {
      return "--:--:--";
    }
  }

  function setupBenchmarkChat() {
    const workspace = document.querySelector("[data-chat-benchmark-id]");
    if (!workspace) return;

    const statusUrl = workspace.dataset.statusUrl;
    const chips = Array.from(workspace.querySelectorAll(".model-chip"));
    const feed = document.getElementById("chat-feed");
    const goalPill = document.getElementById("goal-pill");
    const statusState = document.getElementById("status-state");
    const statusStateText = document.getElementById("status-state-text");
    const statusUpdated = document.getElementById("status-updated");
    const thinkingToggle = document.getElementById("thinking-toggle");

    // The entire right-hand panel (Files / Resources / Preview) is owned by the
    // self-contained workbench module. This page only feeds it polling deltas.
    const workbenchRoot = document.getElementById("agent-workbench");
    const workbench =
      window.AgentWorkbench && workbenchRoot ? window.AgentWorkbench.create(workbenchRoot) : null;

    // The chat feed (left) is its own component module. It renders the live agent
    // timeline — streamed step text, embedded file-aware code cards, and the activity
    // status timeline — and subscribes to the SAME workbench bus, so a single ingest
    // updates both the Files panel and the chat from one set of file:* events.
    const chat =
      window.AgentChat && feed
        ? window.AgentChat.create(feed, {
            bus: workbench ? workbench.bus : null,
            model: workbench ? workbench.model : null,
          })
        : null;

    let activeRunId = null;
    let activeLogsUrl = null;
    let lastLogId = 0;
    let benchmarkStatus = workspace.dataset.status || "queued";
    let iteration = 0;
    const runCounts = {};
    // Live resource polling (autonomous mode); samples are forwarded to the workbench.
    let activeResourcesUrl = null;
    let lastSampleId = 0;

    // Derive a short size label (e.g. "7B") from the model name for each chip.
    chips.forEach((chip) => {
      const sizeNode = chip.querySelector(".chip-size");
      const match = (chip.dataset.runName || "").match(/(\d+(?:\.\d+)?\s*[bB])\b/);
      if (sizeNode) {
        if (match) sizeNode.textContent = match[1].replace(/\s+/g, "").toUpperCase();
        else sizeNode.remove();
      }
    });

    function updateGoalPill() {
      const counts = runCounts[activeRunId] || { passed: 0, failed: 0, total: 0, status: benchmarkStatus };
      const total = counts.total || 0;
      const passed = counts.passed || 0;
      goalPill.textContent = `● ${passed} / ${total} tests · Iteration ${iteration || 0}`;
      let state = "running";
      if ((counts.failed || 0) > 0 || counts.status === "failed" || benchmarkStatus === "failed") {
        state = "fail";
      } else if (counts.status === "succeeded" && total > 0 && (counts.failed || 0) === 0) {
        state = "pass";
      }
      goalPill.dataset.state = state;
    }

    function updateStatusBar() {
      let state = "running";
      if (benchmarkStatus === "succeeded") state = "done";
      else if (benchmarkStatus === "failed" || benchmarkStatus === "cancelled") state = "fail";
      else if (benchmarkStatus === "stopped") state = "fail";
      statusState.dataset.state = state;
      statusStateText.textContent = STATUS_EN[benchmarkStatus] || benchmarkStatus;
      statusUpdated.textContent = `Updated ${nowClock()}`;
    }

    async function refreshStatus() {
      if (!statusUrl) return;
      let data;
      try {
        const response = await fetch(statusUrl);
        if (!response.ok) return;
        data = await response.json();
      } catch (err) {
        return;
      }
      benchmarkStatus = data.status;
      (data.model_runs || []).forEach((run) => {
        runCounts[String(run.id)] = {
          passed: run.passed || 0,
          failed: run.failed || 0,
          total: run.total || 0,
          status: run.status,
        };
        const chip = chips.find((candidate) => candidate.dataset.runId === String(run.id));
        if (chip) {
          chip.dataset.status = run.status;
          chip.classList.remove("queued", "running", "succeeded", "failed", "cancelled");
          chip.classList.add(run.status);
        }
      });
      updateStatusBar();
      updateGoalPill();
      if (chat) chat.setRunStatus(benchmarkStatus);
      if (workbench) workbench.ingestStatus(data);
    }

    async function refreshLogs() {
      if (!activeLogsUrl) return;
      let data;
      try {
        const response = await fetch(`${activeLogsUrl}?after=${lastLogId + 1}`);
        if (!response.ok) return;
        data = await response.json();
      } catch (err) {
        return;
      }
      if (data.model_run) iteration = data.model_run.iteration || 0;
      // Order matters: the chat creates a step card per new log FIRST, so that the
      // file:* events the workbench emits next can attach their code cards to the
      // matching step (events carry the originating coder log id).
      if (chat) { try { chat.ingestLogs(data); } catch (err) { /* keep polling */ } }
      if (chat && data.model_run) {
        // Reload-safe activity timer + used-tokens counter (server-anchored elapsed time).
        chat.setProgress({
          elapsedSeconds: data.model_run.elapsed_seconds,
          startedAt: data.model_run.started_at,
          tokens: data.model_run.total_tokens,
        });
      }
      if (workbench) { try { workbench.ingestLogs(data); } catch (err) { /* keep polling */ } }
      (data.logs || []).forEach((log) => {
        lastLogId = Math.max(lastLogId, log.id);
      });
      updateGoalPill();
      statusUpdated.textContent = `Updated ${nowClock()}`;
    }

    function selectRun(chip) {
      if (!chip) return;
      activeRunId = chip.dataset.runId;
      activeLogsUrl = chip.dataset.logsUrl;
      activeResourcesUrl = chip.dataset.resourcesUrl;
      lastLogId = 0;
      lastSampleId = 0;
      iteration = 0;
      // Hand the new run's context to the workbench; setRun() resets all its panels.
      if (workbench) {
        workbench.setRun({
          id: chip.dataset.runId,
          fileUrl: chip.dataset.fileUrl,
          previewUrlTemplate: chip.dataset.previewUrlTemplate,
          databaseUrl: chip.dataset.databaseUrl,
          csrfToken: getCsrfToken(),
        });
      }
      chips.forEach((other) => other.classList.toggle("selected", other === chip));
      // Reset the chat timeline (clears dynamic step cards, keeps the pinned Task
      // bubble, re-shows the waiting placeholder, resets the activity timeline).
      if (chat) chat.reset();
      refreshLogs();
    }

    chips.forEach((chip) => chip.addEventListener("click", () => selectRun(chip)));

    if (thinkingToggle) {
      thinkingToggle.addEventListener("change", () => {
        document.body.classList.toggle("thinking-on", thinkingToggle.checked);
      });
    }

    // Pick the active run: the one currently running, otherwise the last in order.
    const initial = chips.find((chip) => chip.dataset.status === "running") || chips[chips.length - 1] || null;
    updateStatusBar();
    updateGoalPill();
    if (initial) selectRun(initial);

    // ---- live resource polling (autonomous mode) ----
    // This poller owns timing only; the workbench's ResourcesPanel owns the charts.
    // It runs continuously regardless of which tab is focused, so the resource buffer
    // keeps advancing in the background.
    async function refreshResources() {
      if (!activeResourcesUrl) return;
      let data;
      try {
        const response = await fetch(`${activeResourcesUrl}?after=${lastSampleId}`);
        if (!response.ok) return;
        data = await response.json();
      } catch (err) {
        return;
      }
      (data.samples || []).forEach((sample) => {
        lastSampleId = Math.max(lastSampleId, sample.id);
      });
      if (workbench) workbench.ingestResources(data);
    }

    refreshStatus();
    window.setInterval(refreshStatus, 2000);
    window.setInterval(refreshLogs, 2000);
    window.setInterval(refreshResources, 2000);
  }

  // The project-chat page: multi-turn conversation against a project sandbox. Reuses the
  // same workbench + chat timeline, but polls chat-scoped APIs and adds a message input box.
  function setupProjectChat() {
    const workspace = document.querySelector("[data-project-chat]");
    if (!workspace) return;

    const feed = document.getElementById("chat-feed");
    const statusUpdated = document.getElementById("status-updated");
    const logsUrl = workspace.dataset.logsUrl;
    const resourcesUrl = workspace.dataset.resourcesUrl;
    const sendUrl = workspace.dataset.sendUrl;
    const retryUrl = workspace.dataset.retryUrl;
    const modelsUrl = workspace.dataset.modelsUrl;

    const workbenchRoot = document.getElementById("agent-workbench");
    // Defensive: a workbench construction error must NEVER take down the chat feed + polling
    // (otherwise the page is stuck on "Starting the agent…"). Degrade to a chat-only page instead.
    let workbench = null;
    try {
      workbench = window.AgentWorkbench && workbenchRoot ? window.AgentWorkbench.create(workbenchRoot) : null;
    } catch (err) {
      workbench = null;
    }
    const chat = window.AgentChat && feed
      ? window.AgentChat.create(feed, { bus: workbench ? workbench.bus : null, model: workbench ? workbench.model : null })
      : null;

    if (workbench) {
      try {
        workbench.setRun({
          id: workspace.dataset.chatId,
          fileUrl: workspace.dataset.fileUrl,
          previewUrlTemplate: workspace.dataset.previewUrlTemplate,
          databaseUrl: workspace.dataset.databaseUrl,
          loose: workspace.dataset.loose === "1",
          csrfToken: getCsrfToken(),
        });
      } catch (err) { /* keep the chat usable even if the workbench misbehaves */ }
    }

    let lastLogId = 0;
    let previewAutoShown = false;
    let lastUserPrompt = ""; // the most recent user message, for the retry bar's "Edit prompt"

    // Plan modal (Implement / Edit) + task-list modal, driven by the logs-API poll below.
    // Wrapped: an optional-UI init error must never abort setup before the feed polling starts.
    try {
      if (window.ProjectModals) {
        window.ProjectModals.init({
          planUrl: workspace.dataset.planUrl,
          refineUrl: workspace.dataset.planRefineUrl,
          implementUrl: workspace.dataset.implementUrl,
          csrfToken: getCsrfToken(),
          renderTasks: window.AgentChat && window.AgentChat.renderTaskList,
        });
      }
    } catch (err) { /* keep the chat feed working */ }

    async function refreshLogs() {
      let data;
      try {
        const response = await fetch(`${logsUrl}?after=${lastLogId + 1}`);
        if (!response.ok) return;
        data = await response.json();
      } catch (err) {
        return;
      }
      if (chat) {
        // Contain rendering errors: one bad log must never abort the poll loop (lastLogId would
        // stop advancing and the feed would be stuck on the placeholder forever).
        try { chat.ingestLogs(data); } catch (err) { /* keep polling */ }
        try { chat.setPending(data.pending || []); } catch (err) { /* keep polling */ }
        if (data.model_run) {
          const iter = data.model_run.iteration || 0;
          // A freshly-created "New chat" has no turns yet: keep the activity/spinner line hidden
          // (setRunStatus("succeeded") would render a misleading "Done.") and show a ready state.
          if (iter > 0) {
            // "Done." only when the turn delivered a final summary; otherwise no status tag.
            chat.setRunStatus(data.model_run.status, { hasSummary: data.model_run.has_summary !== false });
            chat.setProgress({
              elapsedSeconds: data.model_run.elapsed_seconds,
              startedAt: data.model_run.started_at,
              tokens: data.model_run.total_tokens,
            });
          } else {
            const empty = document.getElementById("chat-empty");
            if (empty) empty.textContent = "New chat — send a message below to start building.";
          }
          // Status pill (was previously never updated on the project chat → stuck on "Running…").
          const stEl = document.getElementById("status-state");
          const stText = document.getElementById("status-state-text");
          if (stEl && stText) {
            let label = "Ready", state = "idle";
            if (iter > 0) {
              const s = data.model_run.status;
              if (s === "running" || s === "queued") { label = "Running…"; state = "running"; }
              else if (s === "failed") { label = "Failed"; state = "fail"; }
              else if (s === "cancelled" || s === "stopped") { label = "Stopped"; state = "fail"; }
              else { label = "Completed"; state = "done"; }
            }
            stEl.dataset.state = state;
            stText.textContent = label;
          }
          // One turn at a time: while the agent works, disable the box, hide Send, show Stop.
          isBusy = data.model_run.status === "running" || data.model_run.status === "queued";
          updateActions();
        }
      }
      if (workbench) { try { workbench.ingestLogs(data); } catch (err) { /* keep polling */ } }
      // Design-approval gate: when the agent is asking and a preview is available, reveal it once
      // so the user can review the design before answering.
      const hasPending = (data.pending || []).length > 0;
      if (hasPending && data.preview_available && workbench && !previewAutoShown) {
        workbench.showTab("preview");
        previewAutoShown = true;
      }
      if (!hasPending) previewAutoShown = false;
      // Prompt Writer: reveal the Create project / Create benchmark hand-off once the model has
      // produced a structured prompt.
      const handoff = document.getElementById("draft-handoff");
      if (handoff) handoff.hidden = !(data.draft && data.draft.ready);
      // Context meter: show how full the chat's context window is + offer manual compaction.
      if (data.context) updateContextMeter(data.context);
      // Planning toggle: open the plan modal (Implement / Edit) + keep the task modal in sync.
      if (window.ProjectModals) { try { window.ProjectModals.ingest(data); } catch (err) { /* keep polling */ } }
      // Failure recovery: reveal the Retry / Edit / Change-model bar when the last turn failed.
      const retryBar = document.getElementById("retry-bar");
      if (retryBar) {
        const failed = !!(data.model_run && data.model_run.status === "failed");
        retryBar.hidden = !failed;
        if (failed) loadRetryModels(); // lazily fill the model picker the first time it's shown
      }
      (data.logs || []).forEach((log) => {
        lastLogId = Math.max(lastLogId, log.id);
        if (log.agent === "user" && log.stdout) lastUserPrompt = log.stdout;
      });
      if (statusUpdated) statusUpdated.textContent = `Updated ${nowClock()}`;
    }

    async function refreshResources() {
      if (!resourcesUrl) return;
      try {
        const response = await fetch(resourcesUrl);
        if (!response.ok) return;
        const data = await response.json();
        if (workbench) workbench.ingestResources(data);
      } catch (err) { /* ignore */ }
    }

    // ---- message input box ----
    const form = document.getElementById("chat-input");
    const textarea = document.getElementById("chat-input-text");
    const sendBtn = document.getElementById("chat-send-btn");
    const stopBtn = document.getElementById("chat-stop-btn");
    let isBusy = false;
    function autoGrow() {
      if (!textarea) return;
      textarea.style.height = "auto";
      textarea.style.height = Math.min(160, textarea.scrollHeight) + "px";
    }
    // Send appears (animated) only with text + idle; Stop shows while the model runs.
    function updateActions() {
      const hasText = !!(textarea && textarea.value.trim());
      if (textarea) textarea.disabled = isBusy;
      if (sendBtn) sendBtn.classList.toggle("is-shown", hasText && !isBusy);
      if (stopBtn) stopBtn.hidden = !isBusy;
    }
    async function send() {
      if (!textarea || isBusy) return;
      const prompt = textarea.value.trim();
      if (!prompt) return;
      textarea.value = "";
      autoGrow();
      if (chat) chat.setRunStatus("running");
      isBusy = true; updateActions();   // optimistic: hide Send, show Stop immediately
      try {
        const headers = { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest" };
        const token = getCsrfToken();
        if (token) headers["X-CSRFToken"] = token;
        await fetch(sendUrl, { method: "POST", headers, body: new URLSearchParams({ prompt }).toString() });
      } catch (err) { /* polling reflects state */ }
      refreshLogs();
    }
    async function stopRun() {
      const stopUrl = form && form.dataset.stopUrl;
      if (!stopUrl) return;
      if (stopBtn) stopBtn.disabled = true;
      try {
        const headers = { "X-Requested-With": "XMLHttpRequest" };
        const token = getCsrfToken();
        if (token) headers["X-CSRFToken"] = token;
        await fetch(stopUrl, { method: "POST", headers });
      } catch (err) { /* polling reflects state */ }
      if (stopBtn) stopBtn.disabled = false;
      refreshLogs();
    }
    if (form) form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
    if (textarea) {
      textarea.addEventListener("input", () => { autoGrow(); updateActions(); });
      textarea.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
      });
    }
    if (stopBtn) stopBtn.addEventListener("click", stopRun);
    updateActions();

    // composer "+" (Upload / Add context) menu and "/" actions palette
    try { if (window.ComposerMenus && form) window.ComposerMenus.init(form, { mode: "chat" }); }
    catch (err) { /* keep the chat feed working */ }

    // ---- context meter (how full the chat's context window is) + manual compaction ----
    const ctxMeter = document.getElementById("ctx-meter");
    const ctxFill = document.getElementById("ctx-meter-fill");
    const ctxText = document.getElementById("ctx-meter-text");
    const ctxAction = document.getElementById("ctx-meter-action");
    // optimistic "just clicked compact" flag; auto-clears so the pill can't get stuck
    let pendingCompact = false, pendingCompactTimer = 0;
    function updateContextMeter(ctx) {
      if (!ctxMeter || !ctx || !ctx.window_tokens) return;
      // the server's flag is authoritative once it reports back; drop the optimistic flag then
      if (ctx.compacting) { pendingCompact = false; if (pendingCompactTimer) { clearTimeout(pendingCompactTimer); pendingCompactTimer = 0; } }
      const compacting = !!ctx.compacting || pendingCompact;
      const remaining = Math.max(0, Math.min(100, ctx.remaining_pct));
      const usedPct = 100 - remaining;
      // Keep it out of the way until the context is actually filling (no noise on a fresh chat).
      if (usedPct < 15 && !compacting && !ctx.has_memory) { ctxMeter.hidden = true; return; }
      ctxMeter.hidden = false;
      if (ctxFill) {
        ctxFill.style.width = usedPct + "%";
        ctxFill.classList.toggle("warn", remaining <= ctx.auto_compact_pct);
      }
      if (ctxText) {
        ctxText.textContent = compacting
          ? "Compacting context into memory…"
          : remaining + "% of context remaining until auto-compact";
      }
      if (ctxAction) ctxAction.style.display = compacting ? "none" : "";
      ctxMeter.classList.toggle("is-compacting", compacting);
    }
    async function compactNow() {
      if (pendingCompact) return;
      const url = form && form.dataset.compactUrl;
      if (!url) return;
      pendingCompact = true;
      if (pendingCompactTimer) clearTimeout(pendingCompactTimer);
      pendingCompactTimer = window.setTimeout(() => { pendingCompact = false; refreshLogs(); }, 8000); // safety: never stick
      if (ctxText) ctxText.textContent = "Compacting context into memory…";
      if (ctxAction) ctxAction.style.display = "none";
      ctxMeter.classList.add("is-compacting");
      try {
        const headers = { "X-Requested-With": "XMLHttpRequest" };
        const token = getCsrfToken();
        if (token) headers["X-CSRFToken"] = token;
        const r = await fetch(url, { method: "POST", headers });
        if (!r.ok) { pendingCompact = false; } // 409 busy / error — let the next poll repaint
      } catch (err) { pendingCompact = false; }
      refreshLogs();
    }
    // the whole pill is clickable, like Claude Code's "Click to compact now"
    if (ctxMeter) ctxMeter.addEventListener("click", function () { compactNow(); });

    // ---- chat switcher (list project chats + open a new one) ----
    const switchBtn = document.getElementById("chat-switch-btn");
    const switchMenu = document.getElementById("chat-switch-menu");
    if (switchBtn && switchMenu) {
      switchBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const open = switchMenu.hidden;
        switchMenu.hidden = !open;
        switchBtn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      document.addEventListener("click", (e) => {
        if (!switchMenu.hidden && !switchMenu.contains(e.target) && e.target !== switchBtn) {
          switchMenu.hidden = true;
          switchBtn.setAttribute("aria-expanded", "false");
        }
      });
    }

    // ---- Prompt Writer drawer: a docked refiner chat that drafts a prompt into the composer ----
    (function setupPromptWriterDrawer() {
      const drawer = document.getElementById("pw-drawer");
      const openBtn = document.getElementById("pw-open-btn");
      if (!drawer || !openBtn) return;
      const closeBtn = document.getElementById("pw-close");
      const pwFeed = document.getElementById("pw-feed");
      const pwForm = document.getElementById("pw-input");
      const pwText = document.getElementById("pw-input-text");
      const pwSend = document.getElementById("pw-send");
      const useWrap = document.getElementById("pw-use");
      const useBtn = document.getElementById("pw-use-btn");
      const pwUrl = drawer.dataset.pwUrl;

      let pwChat = null, pwLogsUrl = null, pwSendUrl = null, pwLastLogId = 0, pwTimer = 0;
      let structuredPrompt = "", booted = false;

      async function boot() {
        if (booted) return;
        booted = true;
        let info;
        try {
          const r = await fetch(pwUrl);
          if (!r.ok) { booted = false; return; }
          info = await r.json();
        } catch (e) { booted = false; return; }
        pwLogsUrl = info.logs_url;
        pwSendUrl = info.send_url;
        structuredPrompt = info.structured_prompt || "";
        if (window.AgentChat && pwFeed) pwChat = window.AgentChat.create(pwFeed, { bus: null, model: null });
        pwPoll();
        pwTimer = window.setInterval(pwPoll, 1500);
      }

      async function pwPoll() {
        if (!pwLogsUrl || !pwChat) return;
        let data;
        try {
          const r = await fetch(`${pwLogsUrl}?after=${pwLastLogId + 1}`);
          if (!r.ok) return;
          data = await r.json();
        } catch (e) { return; }
        pwChat.ingestLogs(data);
        pwChat.setPending(data.pending || []);
        if (data.model_run) {
          const iter = data.model_run.iteration || 0;
          if (iter > 0) {
            pwChat.setRunStatus(data.model_run.status, { hasSummary: data.model_run.has_summary !== false });
            pwChat.setProgress({
              elapsedSeconds: data.model_run.elapsed_seconds,
              startedAt: data.model_run.started_at,
              tokens: data.model_run.total_tokens,
            });
          }
          const busy = data.model_run.status === "running" || data.model_run.status === "queued";
          if (pwText) pwText.disabled = busy;
          if (pwSend) pwSend.disabled = busy;
        }
        if (data.draft && data.draft.structured_prompt) structuredPrompt = data.draft.structured_prompt;
        const ready = !!(data.draft && data.draft.ready) && !!structuredPrompt;
        if (useWrap) useWrap.hidden = !ready;
        (data.logs || []).forEach((log) => { pwLastLogId = Math.max(pwLastLogId, log.id); });
      }

      async function pwSendMsg() {
        if (!pwText || !pwSendUrl) return;
        const prompt = pwText.value.trim();
        if (!prompt) return;
        pwText.value = "";
        if (pwChat) pwChat.setRunStatus("running");
        try {
          const headers = { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest" };
          const token = getCsrfToken();
          if (token) headers["X-CSRFToken"] = token;
          await fetch(pwSendUrl, { method: "POST", headers, body: new URLSearchParams({ prompt }).toString() });
        } catch (e) { /* poll reflects state */ }
        pwPoll();
      }

      function openDrawer() { drawer.classList.add("open"); boot(); if (pwText) pwText.focus(); }
      function closeDrawer() { drawer.classList.remove("open"); }

      openBtn.addEventListener("click", () => {
        if (drawer.classList.contains("open")) closeDrawer(); else openDrawer();
      });
      if (closeBtn) closeBtn.addEventListener("click", closeDrawer);
      if (pwForm) pwForm.addEventListener("submit", (e) => { e.preventDefault(); pwSendMsg(); });
      if (pwText) pwText.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); pwSendMsg(); }
      });
      if (useBtn) useBtn.addEventListener("click", () => {
        if (!structuredPrompt) return;
        const main = document.getElementById("chat-input-text");
        if (main) { main.value = structuredPrompt; main.dispatchEvent(new Event("input")); main.focus(); }
        closeDrawer();
      });
    })();

    // ---- failure recovery bar: Retry / Edit prompt / Change model ----
    const retryBarEl = document.getElementById("retry-bar");
    if (retryBarEl && retryUrl) {
      const retryBtn = document.getElementById("retry-btn");
      const editBtn = document.getElementById("retry-edit-btn");
      const retryPrompt = document.getElementById("retry-prompt");
      const retryModel = document.getElementById("retry-model");
      const retryStatus = document.getElementById("retry-status");
      function setRetryStatus(msg) {
        if (!retryStatus) return;
        retryStatus.textContent = msg || "";
        retryStatus.hidden = !msg;
      }
      if (editBtn) editBtn.addEventListener("click", () => {
        const reveal = retryPrompt.hidden;
        retryPrompt.hidden = !reveal;
        if (reveal) { retryPrompt.value = lastUserPrompt || ""; retryPrompt.focus(); }
      });
      async function doRetry() {
        const body = { model: retryModel ? retryModel.value : "" };
        if (retryPrompt && !retryPrompt.hidden && retryPrompt.value.trim()) body.prompt = retryPrompt.value.trim();
        if (retryBtn) retryBtn.disabled = true;
        setRetryStatus("");
        try {
          const headers = { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest" };
          const token = getCsrfToken();
          if (token) headers["X-CSRFToken"] = token;
          const r = await fetch(retryUrl, { method: "POST", headers, body: new URLSearchParams(body).toString() });
          if (!r.ok) {
            let msg = "Retry failed.";
            try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (e) { /* ignore */ }
            setRetryStatus(msg);                 // keep the bar visible + tell the user why
            if (retryBtn) retryBtn.disabled = false;
            return;
          }
          // Success: a new turn is queued — hide the bar + show the running spinner.
          retryBarEl.hidden = true;
          if (retryPrompt) retryPrompt.hidden = true;
          if (chat) chat.setRunStatus("running");
        } catch (err) {
          setRetryStatus("Network error — please try again.");
        }
        if (retryBtn) retryBtn.disabled = false;
        refreshLogs();
      }
      if (retryBtn) retryBtn.addEventListener("click", doRetry);
    }

    // Fill the retry bar's model picker once, lazily (only when a failure reveals the bar) so a
    // slow/hung Ollama never blocks the page render.
    let retryModelsLoaded = false;
    async function loadRetryModels() {
      if (retryModelsLoaded || !modelsUrl) return;
      retryModelsLoaded = true; // attempt once; failure leaves Auto + the current tag available
      const sel = document.getElementById("retry-model");
      if (!sel) return;
      try {
        const r = await fetch(modelsUrl);
        if (!r.ok) return;
        const data = await r.json();
        const have = new Set(Array.prototype.map.call(sel.options, (o) => o.value));
        (data.models || []).forEach((tag) => {
          if (have.has(tag)) return;
          const opt = document.createElement("option");
          opt.value = tag; opt.textContent = tag;
          sel.appendChild(opt);
        });
        if (data.current) sel.value = data.current;
      } catch (e) { retryModelsLoaded = false; /* allow a later retry */ }
    }

    refreshLogs();
    window.setInterval(refreshLogs, 2000);
    window.setInterval(refreshResources, 2000);
  }

  // Drag the divider between the chat (left) and the workbench (right) to resize; persists.
  function setupSplitter() {
    const splitter = document.getElementById("chat-splitter");
    if (!splitter) return;
    const body = splitter.closest(".chat-body");
    const left = body && body.querySelector(".chat-left");
    const right = body && body.querySelector(".chat-right");
    if (!body || !left || !right) return;
    try {
      const saved = parseFloat(localStorage.getItem("kursinis-chat-left-pct"));
      if (saved && saved > 20 && saved < 85) { left.style.width = saved + "%"; right.style.width = (100 - saved) + "%"; }
    } catch (e) { /* ignore */ }
    let dragging = false;
    function onMove(e) {
      if (!dragging) return;
      const rect = body.getBoundingClientRect();
      const x = Math.max(320, Math.min(e.clientX - rect.left, rect.width - 360));
      const pct = (x / rect.width) * 100;
      left.style.width = pct + "%";
      right.style.width = (100 - pct) + "%";
    }
    function onUp() {
      if (!dragging) return;
      dragging = false;
      body.classList.remove("is-resizing");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      try {
        const pct = (left.getBoundingClientRect().width / body.getBoundingClientRect().width) * 100;
        localStorage.setItem("kursinis-chat-left-pct", pct.toFixed(1));
      } catch (e) { /* ignore */ }
      window.dispatchEvent(new Event("resize")); // let Chart.js / iframe relayout
    }
    splitter.addEventListener("mousedown", (e) => {
      dragging = true;
      body.classList.add("is-resizing");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      e.preventDefault();
    });
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  confirmDestructiveForms();
  pollBenchmarkTimeline();
  setupLogTools();
  pollLogs();
  setupCharts().catch(() => undefined);
  setupBenchmarkChat();
  setupProjectChat();
  setupSplitter();
})();
