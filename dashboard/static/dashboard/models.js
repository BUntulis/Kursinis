/* Models page: browse the public Ollama library (Available) and manage local models (Installed).
 *
 *   - Available: cards scraped from ollama.com (server-cached), searchable + paginated. Each card
 *     is a link to that model's own detail page (/models/library/<name>/) — install happens there.
 *   - Installed: `ollama list` with size/age, paginated, each row linking to the model's detail
 *     page, plus a Remove button. In-flight downloads show a live progress row; the installed list
 *     is polled only while a pull is actually running.
 */
(function () {
  "use strict";

  var root = document.getElementById("models-page");
  if (!root) return;

  var libraryUrl = root.dataset.libraryUrl;
  var pageUrlTemplate = root.dataset.pageUrlTemplate; // detail page, "__NAME__" placeholder
  var installedUrl = root.dataset.installedUrl;
  var removeUrl = root.dataset.removeUrl;
  var csrf = (root.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";

  // The Available grid pages by whole ROWS: the page size follows however many columns the grid
  // actually laid out, so the last row is never a ragged half-row with empty space beside it.
  // ~24 cards per page either way — rounded UP to a full row, never below.
  var AVAILABLE_TARGET = 24;
  var availPageSize = AVAILABLE_TARGET;   // until the grid has been measured (see syncPageSize)
  var INSTALLED_PAGE_SIZE = 12;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function detailHref(name) {
    return pageUrlTemplate.replace("__NAME__", encodeURIComponent(name));
  }
  function baseName(tag) { return String(tag || "").split(":", 1)[0]; }
  // The detail route only accepts library-charset names (no slashes). `ollama list` can include
  // registry-qualified names like `hf.co/TheBloke/Model:Q4` whose base has slashes — those have no
  // ollama.com library page, so we must NOT linkify them (the link would deterministically 404).
  var LIBRARY_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._\-]*$/;
  function post(url, params) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrf,
      },
      body: new URLSearchParams(params || {}).toString(),
    });
  }

  // ---- shared pagination control ------------------------------------------
  // Renders Prev / windowed page numbers / Next into `pagerEl`; calls onGo(page) on click.
  function renderPager(pagerEl, totalItems, page, pageSize, onGo) {
    var pages = Math.max(1, Math.ceil(totalItems / pageSize));
    pagerEl.innerHTML = "";
    if (pages <= 1) { pagerEl.hidden = true; return; }
    pagerEl.hidden = false;

    function btn(label, target, opts) {
      opts = opts || {};
      var b = el("button", "mpager-btn" + (opts.active ? " active" : ""), label);
      b.type = "button";
      if (opts.disabled) b.disabled = true;
      else b.addEventListener("click", function () { onGo(target); });
      return b;
    }
    pagerEl.appendChild(btn("‹ Prev", page - 1, { disabled: page <= 1 }));

    // windowed page numbers: first, last, and ±2 around the current page, with ellipses
    var win = [];
    for (var i = 1; i <= pages; i++) {
      if (i === 1 || i === pages || (i >= page - 2 && i <= page + 2)) win.push(i);
    }
    var prev = 0;
    win.forEach(function (i) {
      if (prev && i - prev > 1) pagerEl.appendChild(el("span", "mpager-gap", "…"));
      pagerEl.appendChild(btn(String(i), i, { active: i === page }));
      prev = i;
    });
    pagerEl.appendChild(btn("Next ›", page + 1, { disabled: page >= pages }));
  }

  // ---- tabs (WAI-ARIA tablist: aria-selected + roving tabindex + arrow keys) ----
  var tabs = root.querySelectorAll(".mtab");
  var panes = root.querySelectorAll(".mtab-pane");
  function showTab(name) {
    Array.prototype.forEach.call(tabs, function (t) {
      var on = t.dataset.mtab === name;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
    });
    Array.prototype.forEach.call(panes, function (p) { p.hidden = p.dataset.mpane !== name; });
    if (name === "installed") refreshInstalled();
    else if (syncPageSize()) renderGrid();   // resized while the grid was hidden
  }
  Array.prototype.forEach.call(tabs, function (t, i) {
    t.addEventListener("click", function () { showTab(t.dataset.mtab); });
    t.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      var next = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
      showTab(next.dataset.mtab);
      next.focus();
    });
  });

  // ---- Available: library grid (searchable, paginated) --------------------
  var grid = document.getElementById("mlib-grid");
  var statusEl = document.getElementById("mlib-status");
  var searchEl = document.getElementById("mlib-search");
  var pagerEl = document.getElementById("mlib-pager");
  var allModels = [];
  var availPage = 1;

  function filteredModels() {
    var q = (searchEl.value || "").trim().toLowerCase();
    if (!q) return allModels;
    return allModels.filter(function (m) {
      return (m.name + " " + (m.description || "")).toLowerCase().indexOf(q) >= 0;
    });
  }

  function modelCard(m) {
    var card = el("a", "mlib-card");
    card.href = detailHref(m.name);
    var head = el("div", "mlib-card-head");
    head.appendChild(el("span", "mlib-card-name", m.name));
    if (m.installed) head.appendChild(el("span", "mlib-badge installed", "installed"));
    card.appendChild(head);
    card.appendChild(el("p", "mlib-card-desc", m.description || ""));
    var chips = el("div", "mlib-chips");
    (m.capabilities || []).forEach(function (c) { chips.appendChild(el("span", "mlib-chip cap", c)); });
    (m.sizes || []).forEach(function (s) { chips.appendChild(el("span", "mlib-chip size", s)); });
    card.appendChild(chips);
    var meta = [];
    if (m.pulls) meta.push(m.pulls + " pulls");
    if (m.tag_count) meta.push(m.tag_count + " tags");
    if (m.updated) meta.push(m.updated);
    card.appendChild(el("div", "mlib-card-meta", meta.join(" · ")));
    return card;
  }

  // How many columns the CSS grid resolved to. `auto-fill` keeps the empty tracks in the computed
  // value, so this works on an empty grid too — but not on a hidden one (returns 0 = "don't know").
  function gridColumns() {
    if (!grid.offsetWidth) return 0;
    var tracks = getComputedStyle(grid).gridTemplateColumns || "";
    if (!tracks || tracks === "none") return 0;
    return tracks.split(" ").filter(Boolean).length;
  }
  // Returns true when the page size actually changed (so the caller can re-render).
  function syncPageSize() {
    var cols = gridColumns();
    if (!cols) return false;
    var size = cols * Math.ceil(AVAILABLE_TARGET / cols);
    if (size === availPageSize) return false;
    availPageSize = size;
    return true;
  }

  function renderGrid() {
    syncPageSize();
    var models = filteredModels();
    var pages = Math.max(1, Math.ceil(models.length / availPageSize));
    if (availPage > pages) availPage = pages;
    var start = (availPage - 1) * availPageSize;
    var slice = models.slice(start, start + availPageSize);

    grid.innerHTML = "";
    slice.forEach(function (m) { grid.appendChild(modelCard(m)); });
    renderPager(pagerEl, models.length, availPage, availPageSize, function (p) {
      availPage = p; renderGrid(); grid.scrollIntoView({ block: "start", behavior: "smooth" });
    });

    if (!allModels.length) { statusEl.textContent = "No models loaded."; return; }
    var last = Math.min(start + availPageSize, models.length);
    statusEl.textContent = models.length
      ? "Showing " + (models.length ? start + 1 : 0) + "–" + last + " of " + models.length +
        (models.length !== allModels.length ? " (filtered from " + allModels.length + ")" : "")
      : "No models match your search.";
  }

  function loadLibrary(refresh) {
    statusEl.textContent = "Loading the model library…";
    fetch(libraryUrl + (refresh ? "?refresh=1" : ""))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        allModels = d.models || [];
        availPage = 1;
        renderGrid();
        if (d.error) statusEl.textContent = d.error + (allModels.length ? " (showing cached list)" : "");
      })
      .catch(function () { statusEl.textContent = "Could not load the model library."; });
  }
  searchEl.addEventListener("input", function () { availPage = 1; renderGrid(); });
  // A resize can change the column count — refill the rows, but only when it really changed.
  var resizeTimer = 0;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { if (syncPageSize()) renderGrid(); }, 150);
  });
  document.getElementById("mlib-refresh").addEventListener("click", function () { loadLibrary(true); });

  // ---- Installed tab (paginated) + pull progress --------------------------
  var instBody = document.getElementById("minst-body");
  var instStatus = document.getElementById("minst-status");
  var instPagerEl = document.getElementById("minst-pager");
  var pullsEl = document.getElementById("minst-pulls");
  var pullsSnapshot = {};
  var installedModels = [];
  var instPage = 1;
  var pollTimer = 0;

  function installedRow(m) {
    var tr = el("tr");
    var nameTd = el("td");
    var base = baseName(m.tag);
    if (LIBRARY_NAME_RE.test(base)) {
      var link = el("a", "minst-link", m.tag);
      link.href = detailHref(base);
      nameTd.appendChild(link);
    } else {
      nameTd.textContent = m.tag; // registry-qualified (e.g. hf.co/…) — no library page to link to
    }
    tr.appendChild(nameTd);
    tr.appendChild(el("td", "", m.size));
    tr.appendChild(el("td", "", m.modified));
    var td = el("td", "actions");
    var rm = el("button", "button small danger", "Remove");
    rm.type = "button";
    rm.addEventListener("click", function () {
      if (!window.confirm("Remove " + m.tag + " from this machine?")) return;
      rm.disabled = true; rm.textContent = "Removing…";
      post(removeUrl, { tag: m.tag }).then(function (r) {
        if (r.ok) { refreshInstalled(); loadLibrary(false); }
        else {
          r.json().then(function (d) { instStatus.textContent = (d && d.message) || "Remove failed."; })
            .catch(function () { instStatus.textContent = "Remove failed."; });
          rm.disabled = false; rm.textContent = "Remove";
        }
      }).catch(function () { rm.disabled = false; rm.textContent = "Remove"; });
    });
    td.appendChild(rm);
    tr.appendChild(td);
    return tr;
  }

  function renderInstalled() {
    instBody.innerHTML = "";
    if (!installedModels.length) {
      var tr = el("tr");
      var td = el("td", "empty", "No models installed.");
      td.colSpan = 4;
      tr.appendChild(td);
      instBody.appendChild(tr);
      instPagerEl.hidden = true;
      return;
    }
    var pages = Math.max(1, Math.ceil(installedModels.length / INSTALLED_PAGE_SIZE));
    if (instPage > pages) instPage = pages;
    var start = (instPage - 1) * INSTALLED_PAGE_SIZE;
    installedModels.slice(start, start + INSTALLED_PAGE_SIZE).forEach(function (m) {
      instBody.appendChild(installedRow(m));
    });
    renderPager(instPagerEl, installedModels.length, instPage, INSTALLED_PAGE_SIZE, function (p) {
      instPage = p; renderInstalled();
    });
  }

  function renderPulls() {
    pullsEl.innerHTML = "";
    Object.keys(pullsSnapshot).forEach(function (tag) {
      var st = pullsSnapshot[tag];
      var row = el("div", "minst-pull " + (st.status || ""));
      row.appendChild(el("span", "minst-pull-tag", tag));
      row.appendChild(el("span", "minst-pull-line",
        (st.status === "pulling" ? "downloading — " : st.status === "failed" ? "failed — " : "") + (st.line || "")));
      pullsEl.appendChild(row);
    });
  }

  function anyActivePull() {
    return Object.keys(pullsSnapshot).some(function (t) { return pullsSnapshot[t].status === "pulling"; });
  }

  function refreshInstalled() {
    fetch(installedUrl)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var hadActive = anyActivePull();
        pullsSnapshot = d.pulls || {};
        installedModels = d.models || [];
        renderInstalled();
        renderPulls();
        instStatus.textContent = d.error || "";
        if (hadActive && !anyActivePull()) loadLibrary(false); // a pull just finished → badges change
        if (anyActivePull()) startPolling(); else stopPolling();
      })
      .catch(function () { /* next poll retries */ });
  }
  function startPolling() { if (!pollTimer) pollTimer = window.setInterval(refreshInstalled, 2500); }
  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = 0; } }

  // ---- boot ---------------------------------------------------------------
  loadLibrary(false);
  refreshInstalled(); // also resumes progress polling if a pull is already running
})();
