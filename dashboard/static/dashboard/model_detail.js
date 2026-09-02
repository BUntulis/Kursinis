/* Model detail page: drives the Install control for one Ollama library model.
 *
 * The page is server-rendered (description, README with images, the tag <select>). This script
 * only handles: starting a background `ollama pull` for the chosen tag, and polling the installed
 * API to reflect live download progress + flip the button to "Installed" when a pull finishes.
 * Polling runs ONLY while a pull for one of this model's tags is in flight.
 */
(function () {
  "use strict";

  var root = document.getElementById("model-detail-page");
  if (!root) return;

  var installedUrl = root.dataset.installedUrl;
  var installUrl = root.dataset.installUrl;
  var csrf = (root.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";

  var select = document.getElementById("mdl-tag-select");
  var btn = document.getElementById("mdl-install-btn");
  var progress = document.getElementById("mdl-progress");
  if (!select || !btn) return;

  // tags this page owns, so polling can ignore unrelated pulls
  var ownTags = {};
  Array.prototype.forEach.call(select.options, function (o) { ownTags[o.value] = true; });

  var pulls = {};       // tag -> {status, line}  (from the installed poll)
  var pollTimer = 0;

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

  function syncButton() {
    var opt = select.options[select.selectedIndex];
    if (!opt) { btn.disabled = true; progress.textContent = ""; return; }
    var st = pulls[opt.value];
    var pulling = (st && st.status === "pulling") || opt.dataset.pulling === "1";
    if (opt.dataset.installed === "1") { btn.disabled = true; btn.textContent = "Installed"; }
    else if (pulling) { btn.disabled = true; btn.textContent = "Downloading…"; }
    else { btn.disabled = false; btn.textContent = "Install"; }
    progress.textContent = st ? (st.line || "") : "";
  }

  function anyOwnPulling() {
    return Object.keys(pulls).some(function (t) { return ownTags[t] && pulls[t].status === "pulling"; });
  }

  function refresh() {
    fetch(installedUrl)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        pulls = d.pulls || {};
        // Reconcile each option against the LIVE state. The server-rendered data-pulling="1" only
        // seeds the pre-first-poll state (so a reload mid-download resumes); once this poll returns
        // it is authoritative. We must CLEAR data-pulling for tags no longer pulling — otherwise a
        // failed/expired pull would leave the button frozen on "Downloading…" forever (the live
        // `pulls` entry is gone, but the stale attribute would keep syncButton() reporting pulling).
        var installedSet = {};
        (d.models || []).forEach(function (m) { installedSet[m.tag] = true; });
        Array.prototype.forEach.call(select.options, function (o) {
          if (installedSet[o.value]) o.dataset.installed = "1";
          o.dataset.pulling = (pulls[o.value] && pulls[o.value].status === "pulling") ? "1" : "";
        });
        syncButton();
        if (anyOwnPulling()) startPolling(); else stopPolling();
      })
      .catch(function () { /* next poll retries */ });
  }

  function startPolling() { if (!pollTimer) pollTimer = window.setInterval(refresh, 2500); }
  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = 0; } }

  select.addEventListener("change", syncButton);
  btn.addEventListener("click", function () {
    var tag = select.value;
    if (!tag) return;
    btn.disabled = true; btn.textContent = "Starting…";
    post(installUrl, { tag: tag }).then(function (r) {
      if (!r.ok && r.status !== 409) { syncButton(); return; }
      pulls[tag] = { status: "pulling", line: "starting download…" };
      syncButton();
      startPolling();
    }).catch(function () { syncButton(); });
  });

  // initial state (also resumes progress if a pull for this model is already running)
  refresh();
})();
