/*
 * ComposerMenus — the composer "+" (Upload / Add context) menu and "/" actions palette.
 * Shared by the chat composer (project_chat.html) and the home New-chat composer (home.html).
 *
 * init(form, ctx):
 *   form  — the composer <form>; must contain .cm-add-btn ("+"), .cm-actions-btn ("/"),
 *           and the panels from _composer_menus.html. Action URLs are read from form.dataset
 *           (uploadUrl/filesUrl/clearUrl/settingsUrl); absent ones disable that action.
 *   ctx   — { mode: 'chat' | 'home' }. In 'home' mode model/effort/thinking ride hidden inputs
 *           (no server call); in 'chat' mode they POST to settingsUrl to persist live.
 */
(function () {
  function csrfFrom(form) {
    var i = form.querySelector('input[name=csrfmiddlewaretoken]') ||
            document.querySelector('input[name=csrfmiddlewaretoken]');
    return i ? i.value : '';
  }

  function init(form, ctx) {
    if (!form) return;
    ctx = ctx || {};
    var addBtn = form.querySelector('.cm-add-btn');
    var actBtn = form.querySelector('.cm-actions-btn');
    var addPop = form.querySelector('[data-cm="add"]');
    var palette = form.querySelector('[data-cm="palette"]');
    if (!addBtn || !actBtn || !addPop || !palette) return;

    var filter = palette.querySelector('[data-cm-filter]');
    var mainView = palette.querySelector('[data-cm-view="main"]');
    var subView = palette.querySelector('[data-cm-view="sub"]');
    var subTitle = palette.querySelector('[data-cm-sub-title]');
    var subList = palette.querySelector('[data-cm-sub-list]');
    var backBtn = palette.querySelector('[data-cm-back]');
    var modelData = form.querySelector('[data-cm-models]');
    var fileInput = form.querySelector('[data-cm-file]');
    var seed = form.querySelector('[data-cm-seed]');
    var textarea = form.querySelector('textarea');

    var urls = {
      upload: form.dataset.uploadUrl || '',
      files: form.dataset.filesUrl || '',
      clear: form.dataset.clearUrl || '',
      settings: form.dataset.settingsUrl || '',
    };

    var EFFORTS = ['low', 'medium', 'high'];
    var EFFORT_LABEL = { low: 'Low', medium: 'Medium', high: 'High' };
    var state = {
      model: (seed && seed.dataset.model) || '',
      effort: (seed && seed.dataset.effort) || 'medium',
      thinking: !!(seed && seed.dataset.thinking === '1'),
      planning: !!(seed && seed.dataset.planning === '1'),
      pursue: !!(seed && seed.dataset.pursue === '1'),
      auto: !!(seed && seed.dataset.auto === '1'),
    };

    // home-mode hidden inputs (chat mode persists via settingsUrl instead)
    var hModel = form.querySelector('#model-input');
    var hEffort = form.querySelector('#effort-input');
    var hThinking = form.querySelector('input[name="thinking"][type="hidden"]');
    var hPlanning = form.querySelector('input[name="planning"][type="hidden"]');
    var hPursue = form.querySelector('input[name="pursue_goal"][type="hidden"]');
    var hAuto = form.querySelector('input[name="auto_approve"][type="hidden"]');

    function modelLabel() {
      if (!state.model) return 'Auto';
      if (modelData) {
        var b = modelData.querySelector('[data-value="' + state.model + '"]');
        if (b) return b.textContent;
      }
      return 'Model #' + state.model;  // a stored id no longer in the active list
    }
    function renderValues() {
      var mv = palette.querySelector('[data-cm-val="model"]'); if (mv) mv.textContent = modelLabel();
      var ev = palette.querySelector('[data-cm-val="effort"]'); if (ev) ev.textContent = EFFORT_LABEL[state.effort] || 'Medium';
      var tv = palette.querySelector('[data-cm-val="thinking"]'); if (tv) tv.classList.toggle('on', state.thinking);
      var pv = palette.querySelector('[data-cm-val="planning"]'); if (pv) pv.classList.toggle('on', state.planning);
      var gv = palette.querySelector('[data-cm-val="pursue"]'); if (gv) gv.classList.toggle('on', state.pursue);
      var av = palette.querySelector('[data-cm-val="auto"]'); if (av) av.classList.toggle('on', state.auto);
    }
    function persist(changed, revert) {
      if (ctx.mode === 'home') {
        if (hModel) hModel.value = state.model;
        if (hEffort) hEffort.value = state.effort;
        if (hThinking) hThinking.value = state.thinking ? 'on' : '';
        if (hPlanning) hPlanning.value = state.planning ? 'on' : '';
        if (hPursue) hPursue.value = state.pursue ? 'on' : '';
        if (hAuto) hAuto.value = state.auto ? 'on' : '';
        return;
      }
      if (!urls.settings) return;
      var headers = { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest' };
      var t = csrfFrom(form); if (t) headers['X-CSRFToken'] = t;
      // The UI updated optimistically; if the server rejects it, roll back so the palette never
      // claims a model/effort/thinking the next turn won't actually use.
      function fail() { if (revert) { revert(); renderValues(); } window.alert('Could not update — change reverted.'); }
      fetch(urls.settings, { method: 'POST', headers: headers, body: new URLSearchParams(changed).toString() })
        .then(function (r) { if (!r.ok) fail(); })
        .catch(fail);
    }
    renderValues();

    // ---- open / close ----
    function showMain() { if (subView) subView.hidden = true; if (mainView) mainView.hidden = false; applyFilter(); }
    function closeAll() {
      addPop.hidden = true; palette.hidden = true;
      addBtn.setAttribute('aria-expanded', 'false'); actBtn.setAttribute('aria-expanded', 'false');
      showMain();
    }
    var lastTrigger = null;
    function openPop(pop, btn) {
      var willOpen = pop.hidden;
      closeAll();
      if (willOpen) {
        pop.hidden = false;
        lastTrigger = btn;
        btn.setAttribute('aria-expanded', 'true');
        if (pop === palette && filter) { filter.value = ''; applyFilter(); filter.focus(); }
      }
    }
    addBtn.addEventListener('click', function (e) { e.stopPropagation(); openPop(addPop, addBtn); });
    actBtn.addEventListener('click', function (e) { e.stopPropagation(); openPop(palette, actBtn); });
    document.addEventListener('click', closeAll);
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var wasOpen = !addPop.hidden || !palette.hidden;
      closeAll();
      if (wasOpen && lastTrigger) lastTrigger.focus();  // return focus to the trigger (menu contract)
    });

    // ---- filter ----
    function applyFilter() {
      if (!mainView) return;
      var q = (filter ? filter.value : '').trim().toLowerCase();
      var items = mainView.querySelectorAll('.cm-item');
      for (var i = 0; i < items.length; i++) {
        var lbl = (items[i].getAttribute('data-label') || items[i].textContent || '').toLowerCase();
        items[i].style.display = (!q || lbl.indexOf(q) !== -1) ? '' : 'none';
      }
      var secs = mainView.querySelectorAll('.cm-section');
      for (var s = 0; s < secs.length; s++) {
        var any = false, n = secs[s].nextElementSibling;
        while (n && !n.classList.contains('cm-section')) {
          if (n.classList.contains('cm-item') && n.style.display !== 'none') { any = true; break; }
          n = n.nextElementSibling;
        }
        secs[s].style.display = any ? '' : 'none';
      }
    }
    if (filter) filter.addEventListener('input', applyFilter);
    // Enter in the filter must NOT submit the composer form (it lives inside it); instead activate
    // the first visible action, matching the Claude-style filter-then-Enter behaviour.
    if (filter) filter.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      var items = mainView ? mainView.querySelectorAll('.cm-item') : [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].style.display !== 'none' && !items[i].disabled) { items[i].click(); break; }
      }
    });

    // ---- sub-view (model list / file list) ----
    function showSub(title, items, onPick) {
      subTitle.textContent = title;
      subList.innerHTML = '';
      if (!items.length) {
        var empty = document.createElement('div');
        empty.className = 'cm-empty';
        empty.textContent = 'Nothing to show.';
        subList.appendChild(empty);
      }
      items.forEach(function (it) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'cm-item' + (it.active ? ' active' : '');
        b.textContent = it.label;
        b.addEventListener('click', function (e) { e.stopPropagation(); onPick(it); });
        subList.appendChild(b);
      });
      if (mainView) mainView.hidden = true;
      if (subView) subView.hidden = false;
    }
    if (backBtn) backBtn.addEventListener('click', function (e) { e.stopPropagation(); showMain(); });

    // ---- text insert ----
    function insertText(t) {
      if (!textarea) return;
      var v = textarea.value;
      textarea.value = v + (v && !/\s$/.test(v) ? ' ' : '') + t;
      textarea.dispatchEvent(new Event('input'));
      textarea.focus();
    }

    // ---- actions ----
    function doUpload() {
      if (!urls.upload || !fileInput) return;
      fileInput.click();
      closeAll();
    }
    if (fileInput) fileInput.addEventListener('change', function () {
      if (!fileInput.files || !fileInput.files[0] || !urls.upload) { fileInput.value = ''; return; }
      var fd = new FormData(); fd.append('file', fileInput.files[0]);
      var headers = { 'X-Requested-With': 'XMLHttpRequest' };
      var t = csrfFrom(form); if (t) headers['X-CSRFToken'] = t;
      var ok = false;
      fetch(urls.upload, { method: 'POST', headers: headers, body: fd })
        .then(function (r) { ok = r.ok; return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          if (ok && d && d.ok && d.path) insertText('@' + d.path);
          else window.alert((d && d.error) || 'Upload failed.');
        })
        .catch(function () { window.alert('Upload failed — network error.'); });
      fileInput.value = '';
    });
    function doMention() {
      if (!urls.files) return;
      addPop.hidden = true; palette.hidden = false;
      lastTrigger = actBtn;
      actBtn.setAttribute('aria-expanded', 'true');
      if (filter) filter.value = '';  // so returning via Back shows the main list unfiltered
      // show a real loading row (NOT the empty-state) while the file list is fetched
      if (mainView) mainView.hidden = true;
      if (subView) subView.hidden = false;
      subTitle.textContent = 'Mention file';
      subList.innerHTML = '<div class="cm-empty">Loading files…</div>';
      fetch(urls.files).then(function (r) { return r.json(); }).then(function (d) {
        var files = (d && d.files) || [];
        showSub('Mention file', files.map(function (p) { return { label: p, value: p }; }), function (it) {
          insertText('@' + it.value); closeAll();
        });
      }).catch(function () { subList.innerHTML = '<div class="cm-empty">Could not load files.</div>'; });
    }
    function doClear() {
      if (!urls.clear) return;
      if (!window.confirm('Clear this conversation? Files in the project are kept.')) return;
      var headers = { 'X-Requested-With': 'XMLHttpRequest' };
      var t = csrfFrom(form); if (t) headers['X-CSRFToken'] = t;
      fetch(urls.clear, { method: 'POST', headers: headers }).then(function (r) {
        if (r.ok) { window.location.reload(); return; }
        r.json().then(function (d) { window.alert((d && d.error) || 'Could not clear the conversation.'); })
                .catch(function () { window.alert('Could not clear the conversation.'); });
      }).catch(function () { window.alert('Could not clear the conversation.'); });
    }
    function doSwitchModel() {
      var items = [];
      if (modelData) {
        var spans = modelData.querySelectorAll('[data-value]');
        for (var i = 0; i < spans.length; i++) {
          var val = spans[i].getAttribute('data-value');
          items.push({ label: spans[i].textContent, value: val, active: val === state.model });
        }
      }
      showSub('Switch model', items, function (it) {
        var prev = state.model;
        state.model = it.value; renderValues();
        persist({ model: state.model }, function () { state.model = prev; });
        showMain();
      });
    }
    function cycleEffort() {
      var prev = state.effort;
      var i = EFFORTS.indexOf(state.effort);
      state.effort = EFFORTS[(i + 1) % EFFORTS.length];
      renderValues(); persist({ effort: state.effort }, function () { state.effort = prev; });
    }
    function toggleThinking() {
      var prev = state.thinking;
      state.thinking = !state.thinking;
      renderValues(); persist({ thinking: state.thinking ? 'on' : '' }, function () { state.thinking = prev; });
    }
    function togglePlanning() {
      var prev = state.planning;
      state.planning = !state.planning;
      renderValues(); persist({ planning: state.planning ? 'on' : '' }, function () { state.planning = prev; });
    }
    function togglePursue() {
      var prev = state.pursue;
      state.pursue = !state.pursue;
      renderValues(); persist({ pursue_goal: state.pursue ? 'on' : '' }, function () { state.pursue = prev; });
    }
    function toggleAuto() {
      var prev = state.auto;
      state.auto = !state.auto;
      renderValues(); persist({ auto_approve: state.auto ? 'on' : '' }, function () { state.auto = prev; });
    }

    function runAction(action) {
      if (action === 'upload') doUpload();
      else if (action === 'mention') doMention();
      else if (action === 'clear') doClear();
      else if (action === 'switch-model') doSwitchModel();
      else if (action === 'effort') cycleEffort();
      else if (action === 'thinking') toggleThinking();
      else if (action === 'planning') togglePlanning();
      else if (action === 'pursue') togglePursue();
      else if (action === 'auto') toggleAuto();
    }

    palette.addEventListener('click', function (e) {
      e.stopPropagation();                                  // interior clicks keep it open
      var it = e.target.closest('.cm-item');
      if (!it || it.disabled || it.closest('[data-cm-view="sub"]')) return;  // sub items: own handlers
      var action = it.getAttribute('data-action');
      if (action) runAction(action);
    });
    addPop.addEventListener('click', function (e) {
      e.stopPropagation();
      var it = e.target.closest('.cm-item');
      if (!it || it.disabled) return;
      var action = it.getAttribute('data-action');
      if (action) runAction(action);
    });
  }

  window.ComposerMenus = { init: init };
})();
