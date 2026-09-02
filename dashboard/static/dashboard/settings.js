/* Settings page: instant tab switching (Profile / Preferences / Help) + the live, persisted
 * interface preferences. Both prefs are stored in localStorage and applied globally by the tiny
 * bootstrap script in base.html's <head>, so they take effect on every page immediately. */
(function () {
  "use strict";

  var root = document.getElementById("settings-page");
  if (!root) return;

  // ---- tab switching (links still work without JS: they reload with ?tab=) ----
  // Full WAI-ARIA tablist: aria-selected + roving tabindex + ArrowLeft/Right/Home/End.
  var navItems = Array.prototype.slice.call(root.querySelectorAll(".settings-nav-item"));
  var panels = root.querySelectorAll(".settings-panel");
  function show(tab, focusTab) {
    navItems.forEach(function (n) {
      var on = n.dataset.tab === tab;
      n.classList.toggle("active", on);
      n.setAttribute("aria-selected", on ? "true" : "false");
      n.tabIndex = on ? 0 : -1;
      if (on && focusTab) n.focus();
    });
    Array.prototype.forEach.call(panels, function (p) { p.hidden = p.dataset.panel !== tab; });
    try { history.replaceState(null, "", "?tab=" + tab); } catch (e) {}
  }
  navItems.forEach(function (n, i) {
    n.addEventListener("click", function (e) { e.preventDefault(); show(n.dataset.tab); });
    n.addEventListener("keydown", function (e) {
      var idx = -1;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") idx = (i + 1) % navItems.length;
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") idx = (i - 1 + navItems.length) % navItems.length;
      else if (e.key === "Home") idx = 0;
      else if (e.key === "End") idx = navItems.length - 1;
      else return;
      e.preventDefault();
      show(navItems[idx].dataset.tab, true);
    });
  });

  // ---- preferences (persisted; applied globally via the <head> bootstrap) ----
  var docEl = document.documentElement;
  function setPref(key, on) { try { localStorage.setItem(key, on ? "1" : "0"); } catch (e) {} }

  var sidebar = document.getElementById("pref-sidebar");
  if (sidebar) {
    var open = false;
    try { open = localStorage.getItem("kursinis-sidebar") === "open"; } catch (e) {}
    sidebar.checked = open;
    sidebar.addEventListener("change", function () {
      var on = sidebar.checked;
      try { localStorage.setItem("kursinis-sidebar", on ? "open" : "closed"); } catch (e) {}
      docEl.classList.toggle("side-open", on);
    });
  }

  var motion = document.getElementById("pref-reduce-motion");
  if (motion) {
    var reduce = false;
    try { reduce = localStorage.getItem("kursinis-no-anim") === "1"; } catch (e) {}
    motion.checked = reduce;
    motion.addEventListener("change", function () {
      setPref("kursinis-no-anim", motion.checked);
      docEl.classList.toggle("no-anim", motion.checked);
    });
  }
})();
