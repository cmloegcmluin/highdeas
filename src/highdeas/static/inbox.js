/* The inbox list: auto-save, submit, trash, bulk actions, drag-to-reorder, and the
   poll that streams in recordings arriving while the app is open. A row's transcript
   is a preview — clicking it hands the note to the editor dialog (editor.js), which
   reports edits back here to be saved. */
(function () {
  'use strict';

  var content = document.getElementById('content');
  if (!content) return;

  // Rows this window has already submitted or trashed. A poll's snapshot can
  // still list one as pending (it was taken before the POST landed), so we skip
  // re-adding anything here — otherwise an optimistically-removed row would flash
  // back in.
  var retired = {};
  var countEl = document.getElementById('count');
  var notice = document.getElementById('notice');

  // A submit/trash only leaves the list once the server confirms it; on failure the
  // row stays and we surface why here, so a note that never sent can't silently vanish.
  function notify(msg) { if (notice) { notice.textContent = msg; notice.hidden = false; } }
  function clearNotice() { if (notice) { notice.textContent = ''; notice.hidden = true; } }
  function describe(err) { return err && err.message ? ' (' + err.message + ')' : ''; }

  function rows() { return Array.prototype.slice.call(content.querySelectorAll('.memo')); }

  function updateCount() {
    if (!countEl) return;
    var n = rows().length;
    countEl.textContent = '— ' + n + ' item' + (n === 1 ? '' : 's');
  }

  function sep() {
    var el = document.createElement('div');
    el.className = 'sep';
    return el;
  }

  // Separators and row numbers both describe the current order — numbers are a
  // spreadsheet-style anchor, not IDs, so they always run 1..N down the page. Rebuild
  // both from the DOM after anything is added, removed, or dragged into a new place.
  function resync() {
    var grid = content.querySelector('.grid');
    if (grid) {
      grid.querySelectorAll('.sep').forEach(function (el) { el.remove(); });
      rows().forEach(function (memo, i) {
        if (i) grid.insertBefore(sep(), memo);
        memo.querySelector('.num').textContent = i + 1;
      });
    }
    updateCount();
  }

  function urlFor(prefix, memo) { return prefix + encodeURIComponent(memo.dataset.file); }

  function transcriptOf(memo) { return memo.querySelector('.transcript').textContent; }

  function fields(memo) {
    return new URLSearchParams({
      name: memo.querySelector('input[name=name]').value,
      transcript: transcriptOf(memo),
      route: memo.querySelector('input[name=route]').checked ? 'drive' : 'notesnook',
    });
  }

  function post(url, data) { return fetch(url, { method: 'POST', body: data }); }

  function save(memo) { return post(urlFor('/edit/', memo), fields(memo)); }

  function scheduleSave(memo) {
    clearTimeout(memo._timer);
    memo._timer = setTimeout(function () { save(memo); }, 400);
  }

  function flush(memo) { clearTimeout(memo._timer); return save(memo); }

  function showEmpty() {
    var p = document.createElement('p');
    p.className = 'empty';
    p.textContent = "Your inbox is empty. Record a memo and it'll show up here.";
    content.innerHTML = '';
    content.appendChild(p);
    var headrow = document.querySelector('.frozen .headrow');
    if (headrow) headrow.remove();
    resync();
  }

  function removeRow(memo) {
    var grid = memo.closest('.grid');
    memo.remove();
    if (grid && !grid.querySelector('.memo')) showEmpty();
    else resync();
  }

  // Dim and lock a row while its request is in flight, so a bulk run visibly works
  // through the list one row at a time instead of rows just vanishing without warning.
  function setBusy(memo, busy) {
    memo.classList.toggle('sending', busy);
    ['.go', '.del'].forEach(function (sel) {
      var btn = memo.querySelector(sel);
      if (btn) btn.disabled = busy;
    });
  }

  // Remove the row only after a 2xx: the memo is retired server-side only on success,
  // so mirror that here. A non-ok response (routing failed, memo still pending) leaves
  // the row in place, flagged, and rejects, so callers can report the failure.
  function retireOnOk(memo, response) {
    memo.classList.remove('failed');
    setBusy(memo, true);
    return Promise.resolve(response).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t || 'Failed'); });
      retired[memo.dataset.file] = true;
      removeRow(memo);
    }).catch(function (err) {
      setBusy(memo, false);
      memo.classList.add('failed');
      throw err;
    });
  }

  function submitRow(memo) {
    clearTimeout(memo._timer);
    var go = memo.querySelector('.go');
    if (go) go.textContent = 'Sending…';
    return retireOnOk(memo, post(urlFor('/submit/', memo), fields(memo)))
      .catch(function (err) { if (go) go.textContent = 'Submit'; throw err; });
  }

  function trashRow(memo) {
    clearTimeout(memo._timer);
    return retireOnOk(memo, post(urlFor('/delete/', memo)));
  }

  // A .memo is display:contents, so it has no box of its own to grab or hit-test. Its
  // number cell is the handle, and the row under the pointer is reached through
  // whichever of its cells the pointer happens to be over.
  var dragged = null;

  function nextRow(memo) {
    var el = memo.nextElementSibling;
    while (el && !el.classList.contains('memo')) el = el.nextElementSibling;
    return el;
  }

  // A row occupies one grid line across several cells of differing height; the line's
  // extent is their union, so a drop reads the same wherever the pointer crosses it.
  function midpoint(memo) {
    var top = Infinity;
    var bottom = -Infinity;
    Array.prototype.forEach.call(memo.children, function (cell) {
      var box = cell.getBoundingClientRect();
      top = Math.min(top, box.top);
      bottom = Math.max(bottom, box.bottom);
    });
    return (top + bottom) / 2;
  }

  function saveOrder() {
    var data = new URLSearchParams();
    rows().forEach(function (memo) { data.append('order', memo.dataset.file); });
    post('/reorder', data).then(function (r) {
      if (!r.ok) throw new Error('Failed');
    }).catch(function () {
      notify("Couldn't save the new order — the inbox will read back in recorded order next time you open it.");
    });
  }

  // Rows move as you drag over them, so the list you let go of is the list you keep.
  content.addEventListener('dragover', function (event) {
    if (!dragged) return;  // dragging a text selection, not a row by its handle
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    var over = event.target.closest('.memo');
    if (!over || over === dragged) return;
    var below = event.clientY > midpoint(over);
    if (below ? nextRow(over) === dragged : nextRow(dragged) === over) return;
    over.parentElement.insertBefore(dragged, below ? nextRow(over) : over);
    resync();
  });
  content.addEventListener('drop', function (event) { if (dragged) event.preventDefault(); });

  // The transcriber's word timings, [[startSeconds, word], …], ride along on the row
  // so opening the editor costs no extra request. Older memos carry none.
  function wordsOf(memo) {
    try { return memo.dataset.words ? JSON.parse(memo.dataset.words) : []; }
    catch (err) { return []; }
  }

  function openEditor(memo) {
    if (memo.classList.contains('sending') || !window.HighdeasEditor) return;
    var name = memo.querySelector('input[name=name]');
    var preview = memo.querySelector('.transcript');
    window.HighdeasEditor.open({
      audioUrl: urlFor('/audio/', memo),
      name: name.value,
      transcript: transcriptOf(memo),
      words: wordsOf(memo),
      onChange: function (note) {
        name.value = note.name;
        preview.textContent = note.transcript;
        scheduleSave(memo);
      },
    });
  }

  function wire(memo) {
    var preview = memo.querySelector('.transcript');
    var name = memo.querySelector('input[name=name]');
    var route = memo.querySelector('input[name=route]');
    var handle = memo.querySelector('.num');
    preview.addEventListener('click', function () { openEditor(memo); });
    preview.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      openEditor(memo);
    });
    name.addEventListener('input', function () { scheduleSave(memo); });
    name.addEventListener('blur', function () { flush(memo); });
    route.addEventListener('change', function () { flush(memo); });
    handle.addEventListener('dragstart', function (event) {
      dragged = memo;
      memo.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', memo.dataset.file);
    });
    handle.addEventListener('dragend', function () {
      memo.classList.remove('dragging');
      dragged = null;
      saveOrder();
    });
    memo.querySelector('.copy').addEventListener('click', function () {
      name.value = transcriptOf(memo);
      preview.textContent = '';
      flush(memo);
    });
    memo.querySelector('.go').addEventListener('click', function () {
      clearNotice();
      submitRow(memo).catch(function (err) {
        notify("Couldn't send that note — it's still in your inbox." + describe(err));
      });
    });
    memo.querySelector('.del').addEventListener('click', function () {
      clearNotice();
      trashRow(memo).catch(function () {
        notify("Couldn't move that note to the bin — it's still in your inbox.");
      });
    });
  }

  rows().forEach(wire);
  resync();

  // Run an action over the rows one at a time — not a 20-wide burst at the local
  // server and Notesnook — tallying failures so the outcome is reported once at the end.
  function runEach(memos, action) {
    var failures = 0;
    return memos.reduce(function (chain, memo) {
      return chain.then(function () { return action(memo).catch(function () { failures += 1; }); });
    }, Promise.resolve()).then(function () { return failures; });
  }

  var submitAll = document.getElementById('submit-all');
  if (submitAll) submitAll.addEventListener('click', function () {
    var memos = rows();
    if (!memos.length) return;
    clearNotice();
    runEach(memos, submitRow).then(function (failures) {
      if (failures) notify(failures + ' of ' + memos.length + ' note' + (memos.length === 1 ? '' : 's') +
        " couldn't be sent and are still in your inbox. Check that Notesnook is reachable, then try again.");
    });
  });
  var trashAll = document.getElementById('trash-all');
  if (trashAll) trashAll.addEventListener('click', function () {
    var memos = rows();
    if (!memos.length) return;
    if (!confirm('Trash all ' + memos.length + ' memo' + (memos.length === 1 ? '' : 's') + '? They go to the bin.')) return;
    clearNotice();
    runEach(memos, trashRow).then(function (failures) {
      if (failures) notify(failures + ' of ' + memos.length +
        " couldn't be moved to the bin and are still in your inbox.");
    });
  });

  // Keep the list current with recordings that arrive while the app is open.
  // Poll the server (it rescans the inbox) and splice in only memos we're not
  // already showing, leaving existing rows — their edits, focus, and playback —
  // untouched.
  var POLL_MS = 5000;

  function merge(html) {
    var incoming = document.createElement('div');
    incoming.innerHTML = html;
    var shown = {};
    rows().forEach(function (m) { shown[m.dataset.file] = true; });
    var fresh = [];
    incoming.querySelectorAll('.memo').forEach(function (memo) {
      var file = memo.dataset.file;
      if (!shown[file] && !retired[file]) fresh.push(memo);
    });
    if (!fresh.length) return;
    var grid = content.querySelector('.grid');
    if (!grid) { location.reload(); return; }  // empty page: reload to build the grid + frozen header
    // Fresh notes join the end, matching where the server sorts an unplaced memo, so a
    // hand-arranged inbox isn't reshuffled by a recording that lands mid-session.
    fresh.forEach(function (memo) {
      grid.appendChild(memo);
      wire(memo);
    });
    resync();
  }

  function check() {
    return fetch('/pending')
      .then(function (r) { return r.text(); })
      .then(merge)
      .catch(function () {});
  }

  function poll() {
    check().then(function () { setTimeout(poll, POLL_MS); });
  }

  setTimeout(poll, POLL_MS);

  // Manual "check now": the same inbox rescan the poll runs, on demand. A local check
  // returns almost instantly, so hold a "Loading…" label on the button for a beat —
  // even when nothing new turns up — so the click visibly does something and can't
  // double-fire. Whatever it finds still streams in through merge as usual.
  var REFRESH_FEEDBACK_MS = 700;
  var refreshBtn = document.getElementById('refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', function () {
    if (refreshBtn.disabled) return;
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Loading…';
    var held = new Promise(function (done) { setTimeout(done, REFRESH_FEEDBACK_MS); });
    Promise.all([check(), held]).then(function () {
      refreshBtn.textContent = 'Refresh';
      refreshBtn.disabled = false;
    });
  });
})();
