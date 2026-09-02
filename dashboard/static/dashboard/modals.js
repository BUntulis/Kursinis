/* Project-chat overlay modals: the PLAN modal (Implement / Edit) and the TASK-LIST modal.
 *
 * The app had no overlay/modal system — every surface was an inline chat card. This file adds one
 * tiny reusable overlay component (fixed backdrop + centered card) and drives two modals from the
 * normal logs-API poll:
 *   - plan modal  → opens when a planning turn produced a plan (`data.plan.ready`); buttons let the
 *                   user Implement it (build in this chat) or Edit it (inline text edit + AI refine).
 *   - task modal  → mirrors the live `update_tasks` list during an implementation build.
 * Reopenable any time via the topbar "View plan" / "Tasks" chips.
 */
(function () {
  "use strict";

  // ---- tiny overlay component --------------------------------------------------------------
  function createModal(opts) {
    opts = opts || {};
    var overlay = document.createElement("div");
    overlay.className = "km-overlay" + (opts.className ? " " + opts.className : "");
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="km-modal" role="dialog" aria-modal="true" aria-labelledby="km-title">' +
      '  <div class="km-modal-head">' +
      '    <span class="km-modal-title" id="km-title"></span>' +
      '    <button type="button" class="km-modal-x" aria-label="Close">✕</button>' +
      "  </div>" +
      '  <div class="km-modal-body"></div>' +
      '  <div class="km-modal-foot"></div>' +
      "</div>";
    document.body.appendChild(overlay);

    var titleEl = overlay.querySelector(".km-modal-title");
    var bodyEl = overlay.querySelector(".km-modal-body");
    var footEl = overlay.querySelector(".km-modal-foot");

    function close() {
      overlay.hidden = true;
      document.removeEventListener("keydown", onKey);
    }
    function open() {
      overlay.hidden = false;
      document.addEventListener("keydown", onKey);
    }
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); close(); }
    }
    // Backdrop click closes (clicks inside the card do not bubble to here).
    overlay.addEventListener("mousedown", function (e) { if (e.target === overlay) close(); });
    overlay.querySelector(".km-modal-x").addEventListener("click", close);

    return {
      el: overlay,
      open: open,
      close: close,
      isOpen: function () { return !overlay.hidden; },
      setTitle: function (t) { titleEl.textContent = t; },
      body: bodyEl,
      foot: footEl,
    };
  }

  // ---- minimal, XSS-safe markdown → HTML (headings / bold / bullets / paragraphs) ----------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function inline(s) { return esc(s).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>"); }
  function mdToHtml(text) {
    var lines = String(text || "").split(/\r?\n/);
    var out = [], list = null;
    function closeList() { if (list) { out.push("<ul>" + list.join("") + "</ul>"); list = null; } }
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      var m;
      if ((m = ln.match(/^\s*#{1,2}\s+(.*)$/))) { closeList(); out.push("<h3>" + inline(m[1]) + "</h3>"); }
      else if ((m = ln.match(/^\s*#{3,6}\s+(.*)$/))) { closeList(); out.push("<h4>" + inline(m[1]) + "</h4>"); }
      else if ((m = ln.match(/^\s*[-*]\s+(.*)$/))) { (list = list || []).push("<li>" + inline(m[1]) + "</li>"); }
      else if (ln.trim() === "") { closeList(); }
      else { closeList(); out.push("<p>" + inline(ln) + "</p>"); }
    }
    closeList();
    return out.join("") || "<p class=\"km-muted\">(empty)</p>";
  }

  // ---- the project modals controller -------------------------------------------------------
  var cfg = null;          // { planUrl, refineUrl, implementUrl, csrfToken }
  var planModal = null;
  var taskModal = null;
  var planText = "";       // latest known plan text (for the View-plan chip)
  var planReady = false;   // is the plan current + actionable (latest turn ok, not yet approved)?
  var autoOpenedKey = "";  // the plan text we already auto-opened, so polling doesn't reopen it
  var taskAutoOpened = false;
  var renderTasks = null;  // injected from chat.js so the task modal matches the inline card

  function post(url, params) {
    var headers = { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest" };
    if (cfg && cfg.csrfToken) headers["X-CSRFToken"] = cfg.csrfToken;
    return fetch(url, { method: "POST", headers, body: new URLSearchParams(params || {}).toString() });
  }

  // ---- plan modal ----
  function renderPlanView() {
    planModal.setTitle("Your plan");
    planModal.body.className = "km-modal-body km-plan-body";
    planModal.body.innerHTML = mdToHtml(planText);
    // Implement is offered only while the plan is current + unapproved; once it's approved (build
    // under way / done) or stale (a failed refine), only Edit remains.
    var foot = '<button type="button" class="km-btn" data-act="edit">Edit plan</button>';
    if (planReady) foot += '<button type="button" class="km-btn km-btn-primary" data-act="implement">Implement this plan ▶</button>';
    planModal.foot.innerHTML = foot;
    planModal.foot.querySelector('[data-act="edit"]').onclick = renderPlanEdit;
    var impl = planModal.foot.querySelector('[data-act="implement"]');
    if (impl) impl.onclick = function (e) {
      var btn = e.currentTarget; btn.disabled = true; btn.textContent = "Starting…";
      post(cfg.implementUrl, {}).then(function (r) {
        if (r.ok) planModal.close();
        else { btn.disabled = false; btn.textContent = "Implement this plan ▶"; }
      }).catch(function () { btn.disabled = false; btn.textContent = "Implement this plan ▶"; });
    };
  }
  function renderPlanEdit() {
    planModal.setTitle("Edit plan");
    planModal.body.className = "km-modal-body km-plan-edit-wrap";
    var ta = document.createElement("textarea");
    ta.className = "km-plan-edit";
    ta.value = planText;
    planModal.body.innerHTML = "";
    planModal.body.appendChild(ta);
    planModal.foot.innerHTML =
      '<button type="button" class="km-btn" data-act="cancel">Cancel</button>' +
      '<button type="button" class="km-btn" data-act="refine">Refine with AI ✦</button>' +
      '<button type="button" class="km-btn km-btn-primary" data-act="save">Save</button>';
    planModal.foot.querySelector('[data-act="cancel"]').onclick = renderPlanView;
    planModal.foot.querySelector('[data-act="save"]').onclick = function (e) {
      var btn = e.currentTarget; btn.disabled = true;
      post(cfg.planUrl, { plan: ta.value }).then(function (r) {
        if (r.ok) { planText = ta.value; autoOpenedKey = planText; renderPlanView(); }
        else btn.disabled = false;
      }).catch(function () { btn.disabled = false; });
    };
    planModal.foot.querySelector('[data-act="refine"]').onclick = function (e) {
      var btn = e.currentTarget; btn.disabled = true; btn.textContent = "Refining…";
      function fail() { btn.disabled = false; btn.textContent = "Refine with AI ✦"; }
      post(cfg.refineUrl, { plan: ta.value }).then(function (r) {
        if (!r.ok) { fail(); return; }
        // The model re-presents an updated plan, which re-opens this modal on a later poll (its
        // text differs from autoOpenedKey). Mark current edits as seen only on a successful start.
        autoOpenedKey = ta.value;
        planModal.close();
      }).catch(fail);
    };
    ta.focus();
  }
  function openPlan() {
    if (!planModal) planModal = createModal({ className: "km-plan" });
    renderPlanView();
    planModal.open();
  }

  // ---- inline plan card (rendered into the chat feed, NOT a popup) ----
  // A plan is presented as a designed card in the conversation flow instead of a modal that
  // hijacks the screen. Implement/Edit live on the card; Edit still uses the modal textarea.
  var planCardKey = "";
  function renderPlanCard() {
    var feed = document.getElementById("chat-feed");
    if (!feed) return;
    var key = (planReady ? "1|" : "0|") + planText;
    var card = document.getElementById("plan-card");
    // Skip the rebuild when nothing changed, so the 2s poll doesn't flicker / drop focus.
    if (card && key === planCardKey) return;
    planCardKey = key;
    if (!card) {
      card = document.createElement("div");
      card.id = "plan-card";
      card.className = "plan-card";
    }
    card.innerHTML =
      '<div class="plan-card-head">' +
      '  <span class="plan-card-icon" aria-hidden="true">◆</span>' +
      '  <span class="plan-card-title">Proposed plan</span>' +
      "</div>" +
      '<div class="plan-card-body km-plan-body"></div>' +
      '<div class="plan-card-foot"></div>';
    card.querySelector(".plan-card-body").innerHTML = mdToHtml(planText);
    var foot = card.querySelector(".plan-card-foot");
    var html = '<button type="button" class="km-btn" data-act="edit">Edit plan</button>';
    if (planReady) html += '<button type="button" class="km-btn km-btn-primary" data-act="implement">Implement this plan ▶</button>';
    foot.innerHTML = html;
    foot.querySelector('[data-act="edit"]').onclick = openPlan;
    var impl = foot.querySelector('[data-act="implement"]');
    if (impl) impl.onclick = function (e) {
      var btn = e.currentTarget; btn.disabled = true; btn.textContent = "Starting…";
      post(cfg.implementUrl, {}).then(function (r) {
        if (!r.ok) { btn.disabled = false; btn.textContent = "Implement this plan ▶"; }
        // On success the poll flips plan.ready → false; renderPlanCard drops the button.
      }).catch(function () { btn.disabled = false; btn.textContent = "Implement this plan ▶"; });
    };
    // Keep the card at the bottom of the conversation, just above the live activity line.
    var activity = feed.querySelector(".activity");
    if (activity) feed.insertBefore(card, activity);
    else feed.appendChild(card);
  }

  // ---- task modal ----
  function openTasks(tasks) {
    if (!taskModal) taskModal = createModal({ className: "km-tasks" });
    taskModal.setTitle("Tasks");
    taskModal.body.className = "km-modal-body km-task-body";
    if (renderTasks) renderTasks(taskModal.body, tasks || []);
    else taskModal.body.textContent = (tasks || []).map(function (t) { return t.content; }).join("\n");
    taskModal.foot.innerHTML = '<button type="button" class="km-btn km-btn-primary" data-act="ok">Close</button>';
    taskModal.foot.querySelector('[data-act="ok"]').onclick = function () { taskModal.close(); };
    taskModal.open();
  }

  // ---- chips (topbar) ----
  function setChip(id, label, onClick) {
    var chip = document.getElementById(id);
    if (!chip) return;
    chip.hidden = false;
    if (label != null) chip.textContent = label;
    if (!chip._kmWired) { chip.addEventListener("click", onClick); chip._kmWired = true; }
  }

  window.ProjectModals = {
    init: function (config) { cfg = config || {}; renderTasks = config && config.renderTasks; },
    // Called every poll with the logs-API payload.
    ingest: function (data) {
      if (!cfg) return;
      var plan = data && data.plan;
      if (plan && plan.text) {
        planText = plan.text;
        planReady = !!plan.ready;
        setChip("view-plan-chip", null, openPlan);
        // Present the plan as a designed card INLINE in the conversation — never a popup.
        renderPlanCard();
        // Keep the open modal (if the user opened it via the chip / Edit) in sync.
        if (planModal && planModal.isOpen()) renderPlanView();
      }
      var tasks = data && data.tasks;
      if (tasks && tasks.length) {
        var done = 0; tasks.forEach(function (t) { if (t.status === "completed") done++; });
        setChip("tasks-chip", "Tasks " + done + "/" + tasks.length, function () { openTasks(lastTasks); });
        lastTasks = tasks;
        if (taskModal && taskModal.isOpen()) { if (renderTasks) renderTasks(taskModal.body, tasks); }
        // Tasks render inline in the feed (the `tasks` checklist card) — never auto-open a popup.
      }
    },
  };
  var lastTasks = [];
})();
