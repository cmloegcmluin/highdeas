/* The inbox list: auto-save, submit, trash, bulk actions, grouping, drag-to-reorder,
   and the poll that streams in recordings arriving while the app is open. A row's
   transcript is a preview — clicking it hands the note to the editor dialog
   (editor.js), which reports edits back here to be saved.

   The actions that change a row without the user typing into it are recorded on the
   undo stack (history.js) as the pair of steps that walk them back and forward again;
   the ones that take a row out for good empty it. A step names the rows it touched
   rather than holding them, since grouping rebuilds the list from the server. */
(function () {
  'use strict';

  var content = document.getElementById('content');
  if (!content) return;

  var undoStack = window.HighdeasHistory;

  // Rows this window has already submitted or trashed. A poll's snapshot can
  // still list one as pending (it was taken before the POST landed), so we skip
  // re-adding anything here — otherwise an optimistically-removed row would flash
  // back in.
  var retired = {};
  var countEl = document.getElementById('count');
  var notice = document.getElementById('notice');
  var noticeText = document.getElementById('notice-text');

  // A submit/trash only leaves the list once the server confirms it; on failure the
  // row stays and we surface why here, so a note that never sent can't silently vanish.
  // The text goes in its own element: the notice also holds the button that dismisses it.
  function notify(msg) { if (notice) { noticeText.textContent = msg; notice.hidden = false; } }
  function clearNotice() { if (notice) { noticeText.textContent = ''; notice.hidden = true; } }
  function describe(err) { return err && err.message ? ' (' + err.message + ')' : ''; }

  var clipboard = window.HighdeasClip;

  // It reports what has already happened, so nothing done next need clear it. The reader
  // who has read it says so.
  var noticeClose = document.getElementById('notice-close');
  if (noticeClose) noticeClose.addEventListener('click', clearNotice);

  // The sentence is someone else's — the status a destination refused the note with — and
  // the notice holds the only copy of it, so it can be taken whole as well as selected by
  // hand. Alone among the app's copy buttons this one says nothing when it fails: clearing
  // the notice first, the way a row's does to make room for its own complaint, or reporting
  // a refused clipboard into it, would wipe the very words the press was reaching for. A
  // copy that doesn't land leaves them sitting there to be selected instead.
  var noticeCopy = notice && notice.querySelector('[data-copy="message"]');
  if (noticeCopy) {
    noticeCopy.addEventListener('click', function () {
      clipboard.copy(noticeCopy, noticeText.textContent).catch(function () {});
    });
  }

  function rows() { return Array.prototype.slice.call(content.querySelectorAll('.memo')); }

  // Everything the list draws a line between: its notes, and the outlines standing in for
  // the recordings still being transcribed. An outline is a member of the list only for
  // that — everything a row is asked to do (counted, submitted, trashed, dragged, saved
  // into an order) goes through rows(), which is notes alone.
  function listed() { return Array.prototype.slice.call(content.querySelectorAll('.memo, .transcribing')); }

  // Grouping and its undo take the whole list back from the server, so a row element is
  // only ever on loan. A step that has to touch a row again names it and looks it up when
  // it runs, and shrugs if the row has since left the list.
  function rowFor(file) {
    return rows().filter(function (memo) { return memo.dataset.file === file; })[0] || null;
  }

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

  // Separators sit between rows, so they describe the current order: rebuild them from
  // the DOM after anything is added, removed, or dragged into a new place. They run
  // between the outlines at the top too, so what is coming reads as part of the list.
  function resync() {
    var grid = content.querySelector('.grid');
    if (grid) {
      grid.querySelectorAll('.sep').forEach(function (el) { el.remove(); });
      listed().forEach(function (el, i) { if (i) grid.insertBefore(sep(), el); });
    }
    updateCount();
  }

  function urlFor(prefix, memo) { return prefix + encodeURIComponent(memo.dataset.file); }

  function previewOf(memo) { return memo.querySelector('.transcript'); }
  function nameField(memo) { return memo.querySelector('input[name=name]'); }
  function parentField(memo) { return memo.querySelector('select.asana-parent'); }
  function surfaceField(memo) { return memo.querySelector('select.claude-surface'); }
  function modelField(memo) { return memo.querySelector('select.claude-model'); }
  // The note as written — markers and all — which the row carries in an attribute. The
  // preview beside it draws those markers as a real list, the way the editor does, so
  // the cell's own text no longer reads the note back: "- milk" shows as a bullet whose
  // text is "milk", and saving that would strip the list on the first auto-save.
  function transcriptOf(memo) { return memo.dataset.transcript; }
  function nameOf(memo) { return nameField(memo).value; }

  // Redraw the preview from what the row is holding. An empty note draws nothing at all,
  // leaving the cell :empty for the CSS placeholder.
  function drawPreview(memo) {
    var text = transcriptOf(memo);
    var cell = previewOf(memo);
    if (text) cell.replaceChildren(window.HighdeasNote.render(text));
    else cell.replaceChildren();
  }
  function textOf(memo) { return { name: nameOf(memo), transcript: transcriptOf(memo) }; }

  // Where the note is bound: the lit icon, plus whatever that destination asks for on
  // top of it — the task Asana files it under, which Claude it opens and on what model.
  // They travel together, saved and undone as one.
  function destinationOf(memo) {
    var parent = parentField(memo);
    var surface = surfaceField(memo);
    var model = modelField(memo);
    return {
      route: memo.querySelector('input.route:checked').value,
      parent: parent ? parent.value : '',
      surface: surface ? surface.value : '',
      model: model ? model.value : '',
    };
  }

  // Each extra dropdown only matters, and only shows, while its own icon is lit —
  // and the model list narrows further, since only a chat can be opened on one.
  function setDestination(memo, chosen) {
    memo.querySelectorAll('input.route').forEach(function (radio) {
      radio.checked = radio.value === chosen.route;
    });
    var parent = parentField(memo);
    if (parent) {
      parent.value = chosen.parent;
      parent.hidden = chosen.route !== 'asana';
    }
    var surface = surfaceField(memo);
    var model = modelField(memo);
    if (surface) {
      surface.value = chosen.surface || 'code';
      surface.hidden = chosen.route !== 'claude';
    }
    if (model && surface) {
      model.value = chosen.model;
      model.hidden = surface.hidden || chosen.surface !== 'chat';
    }
    flush(memo);
  }

  function bindTo(file, chosen) {
    var memo = rowFor(file);
    if (memo) setDestination(memo, chosen);
  }

  function fields(memo) {
    var chosen = destinationOf(memo);
    return new URLSearchParams({
      name: nameOf(memo),
      transcript: transcriptOf(memo),
      route: chosen.route,
      asana_parent: chosen.parent,
      claude_surface: chosen.surface,
      claude_model: chosen.model,
    });
  }

  function post(url, data) { return fetch(url, { method: 'POST', body: data }); }

  function save(memo) { return post(urlFor('/edit/', memo), fields(memo)); }

  // _timer holds the pending auto-save and doubles as the "mid-edit" signal
  // the poll's busy() reads, so it must be cleared the moment the save fires —
  // a stale id would leave the row repaint-proof forever.
  function scheduleSave(memo) {
    clearTimeout(memo._timer);
    memo._timer = setTimeout(function () { memo._timer = null; save(memo); }, 400);
  }

  function flush(memo) {
    clearTimeout(memo._timer);
    memo._timer = null;
    return save(memo);
  }

  // A button whose face is a glyph has no text to name it, so its name lives where the
  // pointer and the screen reader will each find it.
  function label(button, name) {
    button.title = name;
    button.setAttribute('aria-label', name);
  }

  // ---- The button between Transcript and Name --------------------------------
  // It always points the way the text is about to travel: right while the transcript
  // has something to give, left once it's empty and the name is the one holding it.
  // Deriving that from the two cells rather than remembering which way it was last
  // clicked is what stops the chevron from ever offering a move that isn't there.
  function movesBack(memo) { return !transcriptOf(memo).trim(); }

  function syncMove(memo) {
    var btn = memo.querySelector('.move');
    var back = movesBack(memo);
    btn.classList.toggle('back', back);
    label(btn, back ? 'Move name into Transcript' : 'Move transcript into Name');
    btn.disabled = back && !nameOf(memo).trim();
  }

  function setText(memo, text) {
    nameField(memo).value = text.name;
    memo.dataset.transcript = text.transcript;
    drawPreview(memo);
    syncMove(memo);
  }

  function apply(memo, text) {
    setText(memo, text);
    flush(memo);
  }

  function applyTo(file, text) {
    var memo = rowFor(file);
    if (memo) apply(memo, text);
  }

  function moveText(memo) {
    var file = memo.dataset.file;
    var was = textOf(memo);
    var now = movesBack(memo)
      ? { name: '', transcript: was.name }
      : { name: was.transcript, transcript: '' };
    apply(memo, now);
    undoStack.did({
      undo: function () { applyTo(file, was); },
      redo: function () { applyTo(file, now); },
    });
  }

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

  // A submitted note is in Notesnook, a trashed one is in the bin. Neither comes back, so
  // no step recorded before it left has a row to be walked back onto. A list still holding
  // outlines is not an empty inbox — notes are on their way into it.
  function removeRow(memo) {
    var grid = memo.closest('.grid');
    undoStack.clear();
    memo.remove();
    if (grid && !grid.querySelector('.memo, .transcribing')) showEmpty();
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
    label(go, 'Sending…');
    return retireOnOk(memo, post(urlFor('/submit/', memo), fields(memo)))
      .catch(function (err) { label(go, 'Submit'); throw err; });
  }

  function trashRow(memo) {
    clearTimeout(memo._timer);
    return retireOnOk(memo, post(urlFor('/delete/', memo)));
  }

  // ---- Grouping: fold several notes into one bulleted memo. --------------------
  // A group's row absorbs the others' text, so exactly one survivor must be obvious:
  // group at least two notes, and at most one of them may already be a group. The gesture
  // is a drag — let go of one row over another row's middle — so there is no selection to
  // keep controls in step with; the drag alone says what joins what (see dragover). The
  // group takes a name of its own: the one name among the notes, or the one the namer asks
  // for when several are named (see groupPicked).
  function isGroup(memo) { return memo.dataset.kind === 'group'; }

  // A merge changes several rows at once — some leave, some come back — so the server
  // answers with the inbox it now holds and the page takes the list whole. The rows are
  // built afresh, which is why no step may hold one (see rowFor).
  function showRows(html) {
    var were = orderOf();
    content.innerHTML = html;
    rows().forEach(wire);
    var here = {};
    rows().forEach(function (memo) { here[memo.dataset.file] = true; });
    // A poll snapshot older than this list must not splice back a row that has left it.
    were.forEach(function (file) { if (!here[file]) retired[file] = true; });
    resync();
  }

  function readRows(response) {
    if (!response.ok) return response.text().then(function (t) { throw new Error(t || 'Failed'); });
    return response.text().then(showRows);
  }

  // A merge answers with the rows and with the group's name, which changes every time its
  // recording does — the app joins its members' end to end, and names the file by what is
  // in it. Only the server can say what the group is called now.
  function readGroup(response) {
    if (!response.ok) return response.text().then(function (t) { throw new Error(t || 'Failed'); });
    return response.json().then(function (result) {
      showRows(result.rows);
      return result.target;
    });
  }

  // The server folds the notes as it holds them, not as the page shows them, so every
  // row being grouped hands over what it is holding first. Otherwise a name typed a
  // moment ago is still sitting behind the auto-save's timer, and it is that older,
  // nameless note the merge writes into the bullets.
  function flushEdits(memos) {
    return Promise.all(memos.map(function (memo) { return flush(memo); }));
  }

  // The merge is server-side, and answers with the inbox it leaves behind. The files it
  // folds are named by the drag that started it (or by Undo/Redo replaying one), so there
  // is no live selection to guard here. A group founded from several named notes carries
  // the name the namer settled on; every other merge leaves `name` out and lets the server
  // name the group.
  function mergeFiles(files, name) {
    var picks = rows().filter(function (memo) { return files.indexOf(memo.dataset.file) >= 0; });
    var data = new URLSearchParams();
    files.forEach(function (file) { data.append('files', file); });
    if (name != null) data.append('name', name);
    return flushEdits(picks).then(function () {
      return post('/group', data);
    }).then(readGroup);
  }

  // Walk the last merge back out of a group: the notes it took in return, and the group
  // reads as it did before it — gone, if that merge is what made it. Answers with what it
  // is called now, since its recording is rejoined out of the members it has left.
  function unmergeRow(file) {
    return post('/unmerge/' + encodeURIComponent(file)).then(readGroup);
  }

  // Nothing has moved when a merge is refused, so the notice says so. A step that fails
  // leaves the stack where history.js already moved it — the page and the stack disagree,
  // and the notice is what says which.
  function groupFailed(err) {
    notify("Couldn't group those notes — they're unchanged." + describe(err));
  }

  function unmergeFailed(err) {
    notify("Couldn't walk that merge back — the group is unchanged." + describe(err));
  }

  // A group is a moving target. Its recording is its members' joined end to end, and the
  // file is named by what is in it, so every merge and every walk back renames the group.
  // A step that remembered the name it saw would post it back long after the group had
  // grown out of it. The page keeps a cell per group instead, rewritten each time, and
  // the steps read the cell. A merge into a group is spotted the same way: the group's
  // name of the moment is among the files being folded.
  var groupNames = {};
  var groupsMade = 0;

  function cellOf(files) {
    for (var id in groupNames) {
      if (files.indexOf(groupNames[id]) >= 0) return id;
    }
    return null;
  }

  // The chosen name rides along so redo re-founds the same group: re-posting the files
  // alone would let the server fall back to untitled, since two named notes have no sole
  // name to take. A merge that needed no name (grown group, or at most one named note)
  // passes none, and redo asks the server for the same name it gave the first time.
  function groupFiles(files, name) {
    var id = cellOf(files) || 'group-' + (groupsMade += 1);
    // What this merge takes in, which is everything but the group it takes them into.
    var notes = files.filter(function (file) { return file !== groupNames[id]; });
    function folding() {
      return groupNames[id] ? [groupNames[id]].concat(notes) : notes;
    }
    return mergeFiles(files, name).then(function (target) {
      groupNames[id] = target;
      undoStack.did({
        undo: function () {
          return unmergeRow(groupNames[id]).then(function (left) {
            if (left) groupNames[id] = left;
            else delete groupNames[id];  // that merge is what made it: the group is gone
          }, unmergeFailed);
        },
        redo: function () {
          return mergeFiles(folding(), name).then(function (target) {
            groupNames[id] = target;
          }, groupFailed);
        },
      });
    }).catch(groupFailed);
  }

  // The names among a set of rows, deduped and in row order — what the namer offers when
  // a group founded from them could take more than one.
  function namesAmong(memos) {
    var seen = {};
    return memos.reduce(function (names, memo) {
      var name = nameOf(memo).trim();
      if (name && !seen[name]) { seen[name] = true; names.push(name); }
      return names;
    }, []);
  }

  // Fold the picked notes together. Founding a fresh group from two-or-more named notes,
  // the group can take only one name, so ask which before posting. Growing an existing
  // group keeps the name it already has, and founding from a single name has nothing to
  // choose between — both let the server settle the name, unasked.
  function groupPicked(picks) {
    var files = picks.map(function (memo) { return memo.dataset.file; });
    var names = namesAmong(picks);
    if (!picks.some(isGroup) && names.length > 1 && window.HighdeasNameGroup) {
      window.HighdeasNameGroup(names).then(function (name) {
        if (name != null) groupFiles(files, name);
      });
      return;
    }
    groupFiles(files);
  }

  // The badge walks every merge back at once, past however many steps the stack still
  // holds for this group. Nothing it left behind can be walked back onto the list.
  function ungroupRow(memo) {
    return post('/ungroup/' + encodeURIComponent(memo.dataset.file))
      .then(readRows)
      .then(function () { undoStack.clear(); })
      .catch(function (err) {
        notify("Couldn't break up that group — it's unchanged." + describe(err));
      });
  }

  // A .memo is display:contents, so it has no box of its own to grab, hit-test, or
  // photograph. Its grip cell is the handle; the row under the pointer is reached
  // through whichever of its cells the pointer happens to be over; and the picture the
  // cursor carries has to be painted by hand (dragImage).
  var dragged = null;
  // Set when a drag ended on a group rather than between rows: the dragged note is
  // about to leave the list, so dragend must not save an order that still contains it.
  var joining = false;
  var hovered = null;
  var orderBefore = null;  // the order the drag started from, so dragend can record the move

  // Dropping a note ONTO another note groups them; dropping it BETWEEN notes reorders. So a
  // whole note is one big "join this" target — the pointer only has to be somewhere on it,
  // not in a band — and nothing moves while you're over it, so the note you're aiming at
  // can't dodge out from under the drop. Reordering is left to the gaps, where there is no
  // note to join. Two groups have no obvious survivor, so a group over a group is never a
  // join — it falls through to a reorder.
  function canGroup(over) {
    return !!over && over !== dragged && !(isGroup(dragged) && isGroup(over));
  }

  // Light up the whole row a drop would fold into. hovered holds it, so drop knows the
  // gesture was a join and dragend knows the dragged row is about to leave the list.
  function highlight(row) {
    if (hovered === row) return;
    if (hovered) hovered.classList.remove('grouping');
    hovered = row;
    if (hovered) hovered.classList.add('grouping');
  }

  function nextRow(memo) {
    var el = memo.nextElementSibling;
    while (el && !el.classList.contains('memo')) el = el.nextElementSibling;
    return el;
  }

  // A row occupies one grid line across several cells of differing height; the line's
  // extent is their union, so a drop reads the same wherever the pointer crosses it.
  function rowBox(memo) {
    var top = Infinity;
    var bottom = -Infinity;
    Array.prototype.forEach.call(memo.children, function (cell) {
      var box = cell.getBoundingClientRect();
      top = Math.min(top, box.top);
      bottom = Math.max(bottom, box.bottom);
    });
    return { top: top, bottom: bottom };
  }

  function midpoint(memo) {
    var box = rowBox(memo);
    return (box.top + box.bottom) / 2;
  }

  // What the cursor carries while a row is in the air. The row has a box of its own now,
  // so the browser could photograph it — but that snapshot is the whole full-width strip,
  // live audio player and all, caught just as the .dragging fade lands on it. Hand over a
  // clean clone instead: the same cells in a grid of the same shape, off-screen (it must be
  // rendered to be photographed), bordered and un-faded, so what you're moving reads as a
  // tidy card. The browser rasterizes it during dragstart, so it can be dropped as soon as
  // the call stack unwinds.
  //
  // It wears `memo` and the row's data-kind, because a row's cells are styled through those
  // (`.memo audio`, `[data-kind=note] .group-badge`) and a clone outside them renders as
  // something else entirely — an audio element at its intrinsic width, overflowing its
  // column. `.drag-ghost` gives the clone the row's grid back, and lives outside #content,
  // so it never answers to rows().
  var GHOST_PAD = 8;

  function dragImage(memo, event) {
    var grid = memo.closest('.grid');
    var gridBox = grid.getBoundingClientRect();
    var ghost = document.createElement('div');
    ghost.className = 'grid inbox memo drag-ghost';
    ghost.dataset.kind = memo.dataset.kind;
    ghost.style.width = gridBox.width + 'px';
    Array.prototype.forEach.call(memo.children, function (cell) {
      ghost.appendChild(cell.cloneNode(true));
    });
    // A radio group is every radio of that name in the document, so putting the clone's
    // checked one on the page evicts the row's own: the lit destination went dark the
    // moment the row was picked up, and every save after it found no route at all. The
    // picture is a picture — its inputs answer to no name.
    ghost.querySelectorAll('input[type=radio]').forEach(function (radio) {
      radio.removeAttribute('name');
    });
    document.body.appendChild(ghost);
    // Hold the picture where the cursor took hold of the row, so it doesn't jump.
    event.dataTransfer.setDragImage(
      ghost,
      event.clientX - gridBox.left + GHOST_PAD,
      event.clientY - rowBox(memo).top + GHOST_PAD,
    );
    setTimeout(function () { ghost.remove(); }, 0);
  }

  function orderOf() { return rows().map(function (memo) { return memo.dataset.file; }); }

  function saveOrder() {
    var data = new URLSearchParams();
    orderOf().forEach(function (file) { data.append('order', file); });
    post('/reorder', data).then(function (r) {
      if (!r.ok) throw new Error('Failed');
    }).catch(function () {
      notify("Couldn't save the new order — the inbox will read back in recorded order next time you open it.");
    });
  }

  // Re-seat the rows in a remembered order. A note that arrived from the poll since then
  // was never in it, and leads, where the server puts an unplaced memo — this saves the
  // order it lands in, so sweeping it to the end would pin it there.
  function applyOrder(files) {
    var grid = content.querySelector('.grid');
    if (!grid) return;
    var rank = {};
    files.forEach(function (file, i) { rank[file] = i; });
    function placeOf(memo) {
      var place = rank[memo.dataset.file];
      return place === undefined ? -1 : place;  // unranked: a note that arrived since
    }
    rows().sort(function (a, b) { return placeOf(a) - placeOf(b); })
      .forEach(function (memo) { grid.appendChild(memo); });
    resync();
    saveOrder();
  }

  // Slide the dragged row to the boundary nearest the pointer. Only ever called with the
  // pointer off every joinable note — in a gap, or over the dragged row itself — so a note
  // being grouped onto never has to move out of the way and can't dodge the drop.
  function reorderToward(y) {
    var others = rows().filter(function (memo) { return memo !== dragged; });
    var before = null;
    for (var i = 0; i < others.length; i++) {
      if (y < midpoint(others[i])) { before = others[i]; break; }
    }
    if (nextRow(dragged) === before) return;  // already there — don't churn the list
    content.querySelector('.grid').insertBefore(dragged, before);
    resync();
  }

  // Over a note you can join, the drag is a group: that note lights up whole, the cursor
  // turns to a copy, and nothing moves. Everywhere else — the gaps between notes — it's a
  // reorder, and the dragged note slides to the boundary you're pointing at.
  content.addEventListener('dragover', function (event) {
    if (!dragged) return;  // dragging a text selection, not a row by its handle
    event.preventDefault();
    var over = event.target.closest('.memo');
    if (canGroup(over)) {
      highlight(over);
      event.dataTransfer.dropEffect = 'copy';
      return;
    }
    highlight(null);
    event.dataTransfer.dropEffect = 'move';
    reorderToward(event.clientY);
  });
  content.addEventListener('drop', function (event) {
    if (!dragged) return;
    event.preventDefault();
    var target = hovered;  // the note we'd fold into, set whenever the pointer is over one
    var note = dragged;
    highlight(null);
    if (!target) return;  // an ordinary reorder drop; dragend saves the order it left
    joining = true;
    clearNotice();
    // Route the pair through the namer like any other merge: two named notes folded into a
    // fresh group have one name to settle between, and the drop is asked which just the same.
    groupPicked([target, note]);
  });

  // The transcriber's word timings, [[startSeconds, word], …], ride along on the row
  // so opening the editor costs no extra request. Older memos carry none.
  function wordsOf(memo) {
    try { return memo.dataset.words ? JSON.parse(memo.dataset.words) : []; }
    catch (err) { return []; }
  }

  // The row the editor is open on. It reports edits through a closure over this node, so
  // the poll must not swap the node out from under it: focus while the dialog is open
  // sits outside the row, and between debounce flushes nothing else marks the row busy,
  // which left every later edit landing in a row no longer on the page.
  var editing = null;

  // Where this row plays its recording from, as the server named it. A cut rewrites the
  // recording under the same filename, and a player handed a URL it is already holding
  // plays what it has rather than what is on disk — so the server counts the cuts into
  // the URL and the page never builds one of its own.
  function playable(memo) { return memo.querySelector('audio').getAttribute('src'); }

  // The editor cuts the words out of the text it is showing; the recording they were read
  // from is the server's to cut, and it answers with the timings the cut left and the URL
  // the shortened recording plays from. The row keeps both: reopening the note before the
  // next poll must not light its words by where they used to be, and the row's own player
  // must stop playing the stretch that has just gone.
  function cutAudio(memo, span) {
    var data = new URLSearchParams({ from: span.from, to: span.to });
    return post(urlFor('/cut/', memo), data).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t || 'Failed'); });
      return r.json();
    }).then(function (result) {
      memo.dataset.words = result.words;
      memo.querySelector('audio').src = result.audio;
      return { audioUrl: result.audio, words: wordsOf(memo) };
    });
  }

  function openEditor(memo) {
    if (memo.classList.contains('sending') || !window.HighdeasEditor) return;
    editing = memo;
    clearNotice();
    window.HighdeasEditor.open({
      audioUrl: playable(memo),
      name: nameOf(memo),
      transcript: transcriptOf(memo),
      words: wordsOf(memo),
      onChange: function (note) {
        setText(memo, note);
        scheduleSave(memo);
      },
      onCut: function (span) {
        return cutAudio(memo, span).catch(function (err) {
          // Whichever way round the cut was asked for, what survives it is the recording:
          // the waveform's gesture hasn't touched the text yet, and the text's has already
          // taken the words. So the sentence is about the sound, which is still all there.
          notify("Couldn't cut that from the recording — it still plays that stretch." + describe(err));
          throw err;
        });
      },
      onClose: function () { editing = null; },
    });
  }

  // Which of a row's two fields its copy button lifts, by the name the button carries.
  // A row has the notice to complain into, so a clipboard that won't take the text says
  // so there — and clears whatever the notice held first, since that is now stale.
  var COPY_SOURCES = { transcript: transcriptOf, name: nameOf };

  function copyCell(btn, memo) {
    clearNotice();
    clipboard.copy(btn, COPY_SOURCES[btn.dataset.copy](memo)).catch(function (err) {
      notify("Couldn't copy that to the clipboard." + describe(err));
    });
  }

  function wire(memo) {
    // The row as the server rendered it. The poll compares fresh server rows
    // against this — never against the live DOM, which drifts as the user
    // types and classes flicker — to see if another machine changed the memo.
    memo._served = memo.outerHTML;
    var preview = previewOf(memo);
    var name = nameField(memo);
    // The server ships the note as written, in the cell and in the attribute both; the
    // list markers become a real list here, after _served has the server's own shape.
    drawPreview(memo);
    syncMove(memo);
    memo.querySelector('.ungroup').addEventListener('click', function () {
      clearNotice();
      ungroupRow(memo);
    });
    preview.addEventListener('click', function () { openEditor(memo); });
    preview.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      openEditor(memo);
    });
    // Typing a name is the one thing that can wake a button left with nothing to move.
    name.addEventListener('input', function () { syncMove(memo); scheduleSave(memo); });
    name.addEventListener('blur', function () { flush(memo); });
    // Lighting a destination icon saves it, and so does answering whatever that
    // destination then asks: one recorded step either way.
    var was = destinationOf(memo);
    function rebind() {
      var file = memo.dataset.file;
      var chosen = destinationOf(memo);
      var before = was;
      was = chosen;
      setDestination(memo, chosen);
      undoStack.did({
        undo: function () { was = before; bindTo(file, before); },
        redo: function () { was = chosen; bindTo(file, chosen); },
      });
    }
    memo.querySelectorAll('input.route').forEach(function (radio) {
      radio.addEventListener('change', rebind);
    });
    memo.querySelectorAll('.route-cell select').forEach(function (select) {
      select.addEventListener('change', rebind);
    });
    // The whole row is draggable (rows.html marks the .memo), so the note is picked up
    // anywhere along it — the grip, the timestamp, the transcript, the space between. The
    // controls that need the press for themselves keep it: a drag begun on the audio scrubber
    // or in the name field cancels the row drag, so scrubbing and selecting still work.
    memo.addEventListener('dragstart', function (event) {
      if (event.target.closest('input, select, audio, button, a')) {
        event.preventDefault();  // this press is scrubbing or selecting, not moving the note
        return;
      }
      dragged = memo;
      orderBefore = orderOf();
      // A row drops two ways — reordered (move) or grouped (copy) — so both effects have to
      // be allowed up front. A move-only effectAllowed makes the browser discard the copy
      // dropEffect the group band asks for, and the "+" cursor that marks a merge never shows.
      event.dataTransfer.effectAllowed = 'copyMove';
      event.dataTransfer.setData('text/plain', memo.dataset.file);
      dragImage(memo, event);
      memo.classList.add('dragging');
    });
    memo.addEventListener('dragend', function () {
      memo.classList.remove('dragging');
      dragged = null;
      highlight(null);
      var was = orderBefore;
      orderBefore = null;
      // The dropped note is being folded into a group, so it is leaving the inbox: the
      // rows it passed on the way there keep the order they already had on the server.
      if (joining) { joining = false; return; }
      var now = orderOf();
      if (now.join() === was.join()) return;  // let go where it was picked up
      saveOrder();
      undoStack.did({
        undo: function () { applyOrder(was); },
        redo: function () { applyOrder(now); },
      });
    });
    memo.querySelector('.move').addEventListener('click', function () { moveText(memo); });
    memo.querySelectorAll('.clip').forEach(function (btn) {
      btn.addEventListener('click', function () { copyCell(btn, memo); });
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

  // The bin on an outline throws the recording away before anything has read it — the
  // point of putting its audio there in the first place: a recording left running by
  // accident is recognisable by its length alone, and this drops it without the model
  // spending itself on it. No memo exists yet, so it can't go through /delete; it is
  // named by the key it would have been stored under. The window remembers it as gone
  // for the same reason a trashed row is: a poll snapshot taken before this landed still
  // lists it, and would otherwise put the outline straight back.
  function wireOutline(outline) {
    var del = outline.querySelector('.del');
    if (!del) return;
    del.addEventListener('click', function () {
      clearNotice();
      del.disabled = true;
      post('/discard/' + encodeURIComponent(outline.dataset.file)).then(function (r) {
        if (!r.ok) throw new Error('Failed');
        retired[outline.dataset.file] = true;
        removeRow(outline);
      }).catch(function () {
        del.disabled = false;
        notify("Couldn't throw that recording away — it's still in your inbox.");
      });
    });
  }

  rows().forEach(wire);
  outlines().forEach(wireOutline);
  resync();

  // Run an action over the rows one at a time — not a 20-wide burst at the local
  // server and Notesnook — tallying failures so the outcome is reported once at the end.
  function runEach(memos, action) {
    // Count failures AND keep the first real reason: "3 couldn't be sent"
    // with the cause hidden turns a bad API key into a network goose chase.
    var failures = 0;
    var firstReason = '';
    return memos.reduce(function (chain, memo) {
      return chain.then(function () {
        return action(memo).catch(function (err) {
          failures += 1;
          if (!firstReason && err && err.message) firstReason = err.message;
        });
      });
    }, Promise.resolve()).then(function () {
      return { failures: failures, reason: firstReason };
    });
  }

  var submitAll = document.getElementById('submit-all');
  if (submitAll) submitAll.addEventListener('click', function () {
    var memos = rows();
    if (!memos.length) return;
    clearNotice();
    runEach(memos, submitRow).then(function (result) {
      if (result.failures) notify(result.failures + ' of ' + memos.length + ' note' + (memos.length === 1 ? '' : 's') +
        " couldn't be sent and are still in your inbox." +
        (result.reason ? ' ' + result.reason : ''));
    });
  });
  var trashAll = document.getElementById('trash-all');
  if (trashAll) trashAll.addEventListener('click', function () {
    var memos = rows();
    if (!memos.length) return;
    var question = 'Trash all ' + memos.length + ' memo' + (memos.length === 1 ? '' : 's') + '? They go to the bin.';
    window.HighdeasAsk(question, true).then(function (yes) {
      if (!yes) return;
      clearNotice();
      runEach(memos, trashRow).then(function (result) {
        if (result.failures) notify(result.failures + ' of ' + memos.length +
          " couldn't be moved to the bin and are still in your inbox." +
          (result.reason ? ' ' + result.reason : ''));
      });
    });
  });

  // Keep the list current with the rest of the world: recordings that arrive
  // while the app is open, and — now that another machine shares this store —
  // memos renamed, edited, or retired from the other desk. New rows splice in,
  // changed rows are repainted from the server's render, gone rows leave. A row
  // the user is in the middle of touching is left alone until the next pass:
  // an unsaved edit here beats a stale repaint, and will win server-side too.
  var POLL_MS = 5000;

  function busy(memo) {
    if (memo._timer) return true;  // an edit is waiting on the auto-save timer
    if (memo === editing) return true;  // its editor is open, and edits into it
    if (memo.contains(document.activeElement)) return true;
    var audio = memo.querySelector('audio');
    return !!(audio && !audio.paused);
  }

  function outlines() {
    return Array.prototype.slice.call(content.querySelectorAll('.transcribing'));
  }

  // The outlines of recordings not yet taken in, reconciled against the server's the same
  // way the notes are: by the recording each names. They used to be swapped as a set
  // whenever their number changed, which costs nothing while an outline is a few grey
  // boxes and everything now one holds a player — a swap mid-scrub takes the audio out
  // from under the listen that is deciding whether to keep the recording at all. So an
  // outline whose recording has become a memo goes, one that has just landed joins, and
  // every one that is still waiting is left exactly as it stands. They stand where rows
  // will be, so they join at the top, above every real row.
  function mergeOutlines(incoming, grid) {
    var arriving = {};
    incoming.querySelectorAll('.transcribing').forEach(function (el) {
      if (!retired[el.dataset.file]) arriving[el.dataset.file] = el;
    });
    var changed = false;
    outlines().forEach(function (el) {
      if (arriving[el.dataset.file]) { delete arriving[el.dataset.file]; return; }
      el.remove();
      changed = true;
    });
    var lead = grid.firstChild;
    Object.keys(arriving).forEach(function (file) {
      grid.insertBefore(arriving[file], lead);
      wireOutline(arriving[file]);
      changed = true;
    });
    return changed;
  }

  function merge(html) {
    // Never repaint the list out from under a drag in progress. A row this poll would
    // replace or remove could be the very one in the air — the other machine retired or
    // renamed it mid-drag — and swapping it leaves `dragged` pointing at a detached node
    // that the next dragover splices back in, duplicating the row. The drag is brief; the
    // next poll after it ends reconciles whatever changed.
    if (dragged) return;
    var incoming = document.createElement('div');
    incoming.innerHTML = html;
    var grid = content.querySelector('.grid');
    var changed = false;
    if (!grid && incoming.querySelector('.transcribing')) { location.reload(); return; }
    if (grid && mergeOutlines(incoming, grid)) changed = true;
    var arriving = {};
    incoming.querySelectorAll('.memo').forEach(function (memo) {
      arriving[memo.dataset.file] = memo;
    });
    rows().forEach(function (memo) {
      var file = memo.dataset.file;
      var next = arriving[file];
      delete arriving[file];
      if (!next) {
        // Gone from the server: retired on the other machine (or another tab).
        if (!busy(memo)) { memo.remove(); changed = true; }
        return;
      }
      if (next.outerHTML !== memo._served && !busy(memo)) {
        memo.replaceWith(next);
        wire(next);
        changed = true;
      }
    });
    var fresh = Object.keys(arriving).filter(function (file) { return !retired[file]; });
    if (fresh.length) {
      if (!grid) { location.reload(); return; }  // empty page: reload to build the grid + frozen header
      // Fresh notes join the top, matching where the server sorts an unplaced memo — and
      // right under the outline that stood for them a moment ago. They go in above the row
      // that led until now, so they keep the server's order among themselves and a
      // hand-arranged inbox is added to rather than reshuffled.
      var first = rows()[0] || null;
      fresh.forEach(function (file) {
        grid.insertBefore(arriving[file], first);
        wire(arriving[file]);
      });
      changed = true;
    }
    // A list left with nothing in it says so, rather than standing as a bare grid under its
    // column headers: the last note can be retired from the other machine, and the last
    // waiting recording can go without ever becoming one.
    if (changed) { if (listed().length) resync(); else showEmpty(); }
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

  // Every 5 seconds, except when it matters most. A page the OS has hidden — the window
  // minimized, or another app covering it — has its timers throttled by the engine to as
  // little as once a minute, and coming back to Highdeas to check that a note landed is
  // exactly the moment a stale list is frightening. Looking at the window asks the server
  // then and there instead of waiting out whatever is left of a stretched timer. Both
  // signals, because they are different moments: uncovering the window is a visibility
  // change, and clicking into an already-visible one is only a focus.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) check();
  });
  window.addEventListener('focus', check);

  // The app keeps itself current: when new code lands on main, the page
  // pulls-and-relaunches — but only once the user has left the window alone
  // for a minute (or it's hidden). Never mid-thought: a restart under a
  // half-typed name would cost more than any update is worth. Launch itself
  // also fast-forwards (app._become_current), so this only covers code that
  // lands while a window sits open.
  var VERSION_POLL_MS = 5 * 60 * 1000;
  var IDLE_BEFORE_UPDATE_MS = 60 * 1000;
  var lastTouch = Date.now();
  ['pointerdown', 'keydown', 'wheel'].forEach(function (kind) {
    document.addEventListener(kind, function () { lastTouch = Date.now(); }, true);
  });

  function maybeSelfUpdate() {
    fetch('/version', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (v) {
        if (!(v && v.behind > 0)) return;
        if (!document.hidden && Date.now() - lastTouch < IDLE_BEFORE_UPDATE_MS) return;
        notify('Updating Highdeas to the latest…');
        post('/update').catch(function () {});
      })
      .catch(function () {});
  }

  setInterval(maybeSelfUpdate, VERSION_POLL_MS);

  // Manual "check now": the poll only paints what's already stored, so this is the one
  // place that asks for a scan on demand — /rescan, which runs it off the request
  // thread. The check that follows returns almost instantly, so hold the button's
  // arrows spinning for a beat — even when nothing new turns up yet — so the click
  // visibly does something and can't double-fire. Whatever the scan finds streams in
  // through merge on the polls that follow.
  var REFRESH_FEEDBACK_MS = 700;
  var refreshBtn = document.getElementById('refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', function () {
    if (refreshBtn.disabled) return;
    refreshBtn.disabled = true;
    refreshBtn.classList.add('spinning');
    var held = new Promise(function (done) { setTimeout(done, REFRESH_FEEDBACK_MS); });
    post('/rescan').catch(function () {});
    Promise.all([check(), held]).then(function () {
      refreshBtn.classList.remove('spinning');
      refreshBtn.disabled = false;
    });
  });
})();
