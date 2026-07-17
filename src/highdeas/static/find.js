/* The page's own find, in place of the browser's. Ctrl+F reveals a bar that filters the
   list down to the rows whose name or transcript holds what you type — and it reaches
   the whole transcript, including the part the three-line preview clips off and the part
   the bin's scrolling text box hides, neither of which the browser's own find can see.
   Esc closes it and brings the whole list back.

   Loaded from the shared chrome, so the inbox and the bin both have it. It knows nothing
   about what a row is beyond its name and its text: it filters the inbox's .memo rows and
   the bin's .row rows the same way, and re-runs itself whenever the list changes under an
   open search — a recording the poll splices in, a merge that rebuilds the rows — so a
   note that arrives mid-search is filtered like the rest. */
(function () {
  'use strict';

  var bar = document.getElementById('find');
  var content = document.getElementById('content');
  if (!bar || !content) return;

  var input = document.getElementById('find-input');
  var tally = document.getElementById('find-tally');
  var closeBtn = document.getElementById('find-close');

  function rows() {
    return Array.prototype.slice.call(content.querySelectorAll('.memo, .row'));
  }

  // What a row is searched by: its name and its transcript, wherever each of them lives.
  // The inbox keeps the name in an editable field and the bin in a plain cell; the
  // transcript is a .transcript preview on one page and a .text block on the other. The
  // whole of it, not the clipped preview — .textContent reads the entire note even where
  // only three lines of it show, which is the whole point of not leaving this to the
  // browser's find.
  function haystack(row) {
    var parts = [];
    var field = row.querySelector('input[name=name]');
    if (field) parts.push(field.value);
    var name = row.querySelector('.name');
    if (name) parts.push(name.textContent);
    var body = row.querySelector('.transcript, .text');
    if (body) parts.push(body.textContent);
    return parts.join(' ').toLowerCase();
  }

  function query() { return input.value.trim().toLowerCase(); }

  // The line between two rows is a separate element sitting before the lower one, so a
  // hidden row would strand its line. Show the separator above a row only when that row
  // shows AND a row already showed above it: no line leads the first match, and exactly
  // one sits between any two. Only classes are toggled here — never the child list — so
  // the observer below never trips on this work.
  function apply() {
    var term = query();
    var filtering = term.length > 0;
    var all = rows();
    var shown = 0;
    var seen = false;
    all.forEach(function (row) {
      var hit = !filtering || haystack(row).indexOf(term) >= 0;
      row.classList.toggle('find-miss', !hit);
      var before = row.previousElementSibling;
      if (before && before.classList.contains('sep')) {
        before.classList.toggle('find-miss', !(hit && seen));
      }
      if (hit) { shown += 1; seen = true; }
    });
    report(filtering, shown, all.length);
  }

  function report(filtering, shown, total) {
    if (!tally) return;
    if (!filtering) tally.textContent = '';
    else if (total === 0) tally.textContent = 'Nothing to find';
    else if (shown === 0) tally.textContent = 'No matches';
    else tally.textContent = shown + ' of ' + total;
  }

  // Not named open(): that would shadow window.open, and this only ever uncovers the bar.
  function reveal() {
    bar.hidden = false;
    input.focus();
    input.select();
  }

  // Closing clears the query, so the list you come back to is the whole one and not a
  // filter left on and forgotten behind a hidden bar.
  function close() {
    input.value = '';
    apply();
    bar.hidden = true;
  }

  input.addEventListener('input', apply);
  if (closeBtn) closeBtn.addEventListener('click', close);

  // An open dialog keeps the keyboard to itself: the editor's body is a long text the
  // browser's own find is the right tool for, and Esc there closes the editor.
  function dialogOpen() { return !!document.querySelector('dialog[open]'); }

  document.addEventListener('keydown', function (event) {
    var find = (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey
      && event.key.toLowerCase() === 'f';
    if (find) {
      if (dialogOpen()) return;  // let the browser's find work the note being edited
      event.preventDefault();    // take Ctrl+F off the browser: ours reaches the clipped text
      reveal();
    } else if (event.key === 'Escape' && !bar.hidden && !dialogOpen()) {
      event.preventDefault();
      close();
    }
  });

  // The list changes under an open search, so the filter re-runs when it does — otherwise
  // a row the poll splices in arrives unfiltered, and a merge that rebuilds every row
  // comes back with the search forgotten. A MutationObserver's callback is a microtask
  // delivered once after the mutations settle, so a whole merge's worth of adds and
  // removes collapses into one re-run, timed after the list is whole and before the next
  // paint. Watching only childList (rows and separators coming and going) while this
  // toggles only classes means it never sees its own work and loops; nothing to re-filter
  // while the box is empty, so it stays out of the poll's way until a search is on.
  if (window.MutationObserver) {
    new MutationObserver(function () {
      if (query()) apply();
    }).observe(content, { childList: true, subtree: true });
  }
}());
