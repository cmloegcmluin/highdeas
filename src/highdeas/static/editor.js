/* The note editor dialog: play the recording back as a scrubbable waveform, light
   up each word as it's spoken, and give the note's title and body room to be
   worked on — so a rough transcription gets massaged here rather than shipped to
   Notesnook half-finished just to get it out of the inbox.

   The caller owns the memo and its persistence; this file owns the dialog. Open it
   with HighdeasEditor.open({audioUrl, name, transcript, words, onChange}) and it
   reports edits back through onChange({name, transcript}).

   Notes are stored as plain text, so a list is just its Markdown line ("- x",
   "1. x"). The dialog renders those lines as real <ul>/<ol> to edit, and reads
   them back out as lines when it reports a change — the routers turn the same
   lines into HTML for Notesnook and styled paragraphs for a Drive .docx. */
(function () {
  'use strict';

  var dialog = document.getElementById('editor');
  if (!dialog || !dialog.showModal) return;

  var nameEl = document.getElementById('editor-name');
  var bodyEl = document.getElementById('editor-body');
  var audio = document.getElementById('editor-audio');
  var canvas = document.getElementById('editor-wave');
  var playBtn = document.getElementById('editor-play');
  var timeEl = document.getElementById('editor-time');
  var waveNote = document.getElementById('editor-wave-note');
  var moveBtn = dialog.querySelector('.editor-move');

  var SAVE_MS = 400;      // debounce before an edit is reported to the caller
  var ALIGN_MS = 150;     // debounce before edited text is re-matched to the audio
  var PEAK_BINS = 2048;   // amplitude bins kept per recording, resampled when drawn
  // The colour of the sound being heard. The same declaration lights the word being
  // spoken (::highlight(spoken) in app.css), so the bars and the text can't drift apart.
  var SPOKEN = getComputedStyle(document.documentElement).getPropertyValue('--spoken').trim();
  // How far past the last matched word we'll hunt for the next one. Edits shift the
  // text away from what was spoken; this is how much drift the highlight survives.
  var LOOKAHEAD = 20;

  var current = null;    // the open note: {audioUrl, onChange}
  var words = [];        // [[startSeconds, word], …] as the transcriber heard them
  var marks = [];        // those words matched onto the live text, in time order
  var painted = null;    // the mark currently lit, so a frame that changes nothing costs nothing
  var peaks = null;      // normalized amplitudes, or null when the audio won't decode
  var following = true;  // scroll the spoken word into view until the user takes over
  var selection = null;  // {from, to} seconds selected across the waveform, or null
  var press = null;      // {x, at} while a pointer is held down on the waveform
  var saveTimer = null;
  var alignTimer = null;
  var frame = null;
  var context = null;

  // The Custom Highlight API paints a range without touching the DOM or the
  // selection: the spoken word lights up while the caret and any real selection
  // stay exactly where the user left them.
  var highlight = null;
  if (window.CSS && CSS.highlights && window.Highlight) {
    highlight = new Highlight();
    CSS.highlights.set('spoken', highlight);
  }

  // Chromium wraps each line of a contenteditable in <div> unless told otherwise;
  // <p> is what the rest of the note pipeline speaks.
  try { document.execCommand('defaultParagraphSeparator', false, 'p'); } catch (err) { /* older engine */ }

  /* ---- Markdown lines <-> editable blocks --------------------------------- */

  var BULLET = /^\s*[-*•]\s+(.*)$/;
  var NUMBER = /^\s*\d+[.)]\s+(.*)$/;

  function renderNote(text) {
    var frag = document.createDocumentFragment();
    var list = null;
    var listTag = null;
    text.split('\n').forEach(function (line) {
      var bullet = BULLET.exec(line);
      var number = bullet ? null : NUMBER.exec(line);
      var tag = bullet ? 'UL' : (number ? 'OL' : null);
      if (tag !== listTag) {
        list = tag ? frag.appendChild(document.createElement(tag)) : null;
        listTag = tag;
      }
      if (list) {
        var item = document.createElement('li');
        item.textContent = (bullet || number)[1];
        list.appendChild(item);
      } else {
        var paragraph = document.createElement('p');
        if (line) paragraph.textContent = line;
        else paragraph.appendChild(document.createElement('br'));
        frag.appendChild(paragraph);
      }
    });
    if (!frag.childNodes.length) frag.appendChild(document.createElement('p'));
    return frag;
  }

  function flatten(element) {
    var text = '';
    Array.prototype.forEach.call(element.childNodes, function (node) {
      if (node.nodeType === Node.TEXT_NODE) text += node.nodeValue;
      else if (node.nodeType === Node.ELEMENT_NODE) text += node.tagName === 'BR' ? '\n' : flatten(node);
    });
    // Chromium parks a filler <br> at the end of an otherwise empty block.
    return text.replace(/\n$/, '');
  }

  function readNote(root) {
    var lines = [];
    Array.prototype.forEach.call(root.childNodes, function (node) {
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.nodeValue.trim()) lines.push(node.nodeValue);
      } else if (node.nodeType !== Node.ELEMENT_NODE) {
        return;
      } else if (node.tagName === 'UL' || node.tagName === 'OL') {
        var ordered = node.tagName === 'OL';
        var index = 0;
        Array.prototype.forEach.call(node.children, function (item) {
          if (item.tagName !== 'LI') return;
          index += 1;
          lines.push((ordered ? index + '. ' : '- ') + flatten(item).replace(/\n/g, ' '));
        });
      } else {
        flatten(node).split('\n').forEach(function (line) { lines.push(line); });
      }
    });
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    return lines.join('\n');
  }

  /* ---- Matching the spoken words onto the (possibly edited) text ----------- */

  function normalize(word) { return word.toLowerCase().replace(/[^a-z0-9']/g, ''); }

  function textTokens(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var tokens = [];
    var node;
    while ((node = walker.nextNode())) {
      var pattern = /\S+/g;
      var match;
      while ((match = pattern.exec(node.nodeValue))) {
        tokens.push({ node: node, from: match.index, to: pattern.lastIndex, key: normalize(match[0]) });
      }
    }
    return tokens;
  }

  // Walk the spoken words and the on-screen words together, letting the on-screen
  // side skip ahead when the user has inserted text. An unmatched word simply gets
  // no highlight; the word before it stays lit until the next match comes due.
  function align() {
    var tokens = textTokens(bodyEl);
    var next = 0;
    marks = [];
    painted = null;
    words.forEach(function (word) {
      var key = normalize(word[1]);
      if (!key) return;
      var limit = Math.min(tokens.length, next + LOOKAHEAD);
      for (var i = next; i < limit; i++) {
        if (tokens[i].key !== key) continue;
        marks.push({ token: tokens[i], at: word[0] });
        next = i + 1;
        return;
      }
    });
  }

  function spokenAt(seconds) {
    var low = 0;
    var high = marks.length - 1;
    var found = -1;
    while (low <= high) {
      var middle = (low + high) >> 1;
      if (marks[middle].at <= seconds) { found = middle; low = middle + 1; } else { high = middle - 1; }
    }
    return found < 0 ? null : marks[found];
  }

  function paint(seconds) {
    if (!highlight) return;
    var mark = spokenAt(seconds);
    if (mark === painted) return;
    painted = mark;
    highlight.clear();
    if (!mark || !mark.token.node.isConnected) return;
    var range = document.createRange();
    try {
      range.setStart(mark.token.node, mark.token.from);
      range.setEnd(mark.token.node, mark.token.to);
    } catch (err) {
      return;  // the node was edited out from under us; the next align() fixes it
    }
    highlight.add(range);
    if (following) reveal(range);
  }

  function reveal(range) {
    var box = bodyEl.getBoundingClientRect();
    var word = range.getBoundingClientRect();
    if (!word.height) return;
    if (word.top < box.top + 8) bodyEl.scrollTop += word.top - box.top - 8;
    else if (word.bottom > box.bottom - 8) bodyEl.scrollTop += word.bottom - box.bottom + 8;
  }

  /* ---- Waveform ----------------------------------------------------------- */

  function amplitudes(buffer) {
    var samples = buffer.getChannelData(0);
    var bins = new Float32Array(PEAK_BINS);
    var width = samples.length / PEAK_BINS;
    var loudest = 0;
    for (var bin = 0; bin < PEAK_BINS; bin++) {
      var start = Math.floor(bin * width);
      var end = Math.min(samples.length, Math.floor((bin + 1) * width));
      var peak = 0;
      for (var i = start; i < end; i++) {
        var level = samples[i] < 0 ? -samples[i] : samples[i];
        if (level > peak) peak = level;
      }
      bins[bin] = peak;
      if (peak > loudest) loudest = peak;
    }
    if (loudest > 0) for (var j = 0; j < PEAK_BINS; j++) bins[j] /= loudest;
    return bins;
  }

  function loadWaveform(url) {
    peaks = null;
    waveNote.hidden = true;
    drawWave();
    var Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) { waveNote.hidden = false; return; }
    if (!context) context = new Context();
    fetch(url)
      .then(function (response) { return response.arrayBuffer(); })
      .then(function (encoded) { return context.decodeAudioData(encoded); })
      .then(function (decoded) {
        if (!current || current.audioUrl !== url) return;  // a different note is open now
        peaks = amplitudes(decoded);
        drawWave();
      })
      .catch(function () { waveNote.hidden = false; });
  }

  function drawWave() {
    var ratio = window.devicePixelRatio || 1;
    var width = Math.round(canvas.clientWidth * ratio);
    var height = Math.round(canvas.clientHeight * ratio);
    if (!width || !height) return;
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    var pen = canvas.getContext('2d');
    pen.clearRect(0, 0, width, height);
    var barWidth = Math.max(1, Math.round(2 * ratio));
    var gap = Math.max(1, Math.round(ratio));
    var bars = Math.max(1, Math.floor(width / (barWidth + gap)));
    var played = audio.duration ? audio.currentTime / audio.duration : 0;
    var middle = height / 2;
    for (var bar = 0; bar < bars; bar++) {
      // A flat placeholder line when the audio wouldn't decode: still scrubbable.
      var level = peaks ? peaks[Math.floor(bar * peaks.length / bars)] : 0.1;
      var tall = Math.max(2 * ratio, level * (height - 6 * ratio));
      pen.fillStyle = bar / bars < played ? SPOKEN : 'rgba(128,128,128,.45)';
      pen.fillRect(bar * (barWidth + gap), middle - tall / 2, barWidth, tall);
    }
    if (selection && audio.duration) {
      var from = selection.from / audio.duration * width;
      var to = selection.to / audio.duration * width;
      pen.fillStyle = 'rgba(59,130,246,.28)';  // the focus blue, translucent over the bars
      pen.fillRect(from, 0, Math.max(1, to - from), height);
    }
  }

  function clock(seconds) {
    if (!isFinite(seconds)) seconds = 0;
    var minutes = Math.floor(seconds / 60);
    var rest = Math.floor(seconds % 60);
    return minutes + ':' + (rest < 10 ? '0' : '') + rest;
  }

  function tick() {
    frame = null;
    drawWave();
    paint(audio.currentTime);
    timeEl.textContent = clock(audio.currentTime) + ' / ' + clock(audio.duration || 0);
    if (!audio.paused) frame = requestAnimationFrame(tick);
  }

  function timeAt(event) {
    if (!audio.duration) return 0;
    var box = canvas.getBoundingClientRect();
    var ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    return ratio * audio.duration;
  }

  function seek(event) {
    if (!audio.duration) return;
    audio.currentTime = timeAt(event);
    tick();
  }

  function setSelection(sel) { selection = sel; drawWave(); }

  // The words whose spoken span overlaps the selection, in text order. Each mark runs
  // until the next one begins; the last runs to the end of the recording.
  function selectedMarks() {
    var chosen = [];
    for (var i = 0; i < marks.length; i++) {
      var end = (i + 1 < marks.length) ? marks[i + 1].at : (audio.duration || marks[i].at);
      if (marks[i].at < selection.to && end > selection.from) chosen.push(marks[i]);
    }
    return chosen;
  }

  // Delete the transcript under the waveform selection: the recording is the source of
  // truth, so cutting a stretch of sound cuts the words it produced. Re-align first so
  // the marks point at live text nodes, then cut from the first selected word to the
  // last — sweeping up any unmatched words caught between — and one bordering space.
  function deleteSelection() {
    align();
    var chosen = selectedMarks();
    setSelection(null);
    if (!chosen.length) return;  // a selection over silence has no words to cut
    var first = chosen[0].token;
    var last = chosen[chosen.length - 1].token;
    var range = document.createRange();
    try {
      range.setStart(first.node, first.from);
      range.setEnd(last.node, last.to);
      eatSpace(range);
      range.deleteContents();
      bodyEl.normalize();
    } catch (err) { return; }  // the text moved under us; leave it be
    align();
    paint(audio.currentTime);
    scheduleSave();
  }

  // Widen a range by one bordering space so a deletion doesn't leave a double gap.
  function eatSpace(range) {
    var end = range.endContainer;
    var start = range.startContainer;
    if (end.nodeType === Node.TEXT_NODE && /\s/.test(end.nodeValue.charAt(range.endOffset))) {
      range.setEnd(end, range.endOffset + 1);
    } else if (start.nodeType === Node.TEXT_NODE && range.startOffset > 0 &&
               /\s/.test(start.nodeValue.charAt(range.startOffset - 1))) {
      range.setStart(start, range.startOffset - 1);
    }
  }

  /* ---- Open, edit, close -------------------------------------------------- */

  function snapshot() { return { name: nameEl.value, transcript: readNote(bodyEl) }; }
  function report() { if (current && current.onChange) current.onChange(snapshot()); }
  function scheduleSave() { clearTimeout(saveTimer); saveTimer = setTimeout(report, SAVE_MS); }
  function flush() { clearTimeout(saveTimer); report(); }

  function scheduleAlign() {
    clearTimeout(alignTimer);
    alignTimer = setTimeout(function () { align(); paint(audio.currentTime); }, ALIGN_MS);
  }

  /* ---- The field controls: copy each field, and move text between them ------ */

  // A glyph button carries its name where the pointer and the screen reader each find it.
  function label(button, name) {
    button.title = name;
    button.setAttribute('aria-label', name);
  }

  function transcriptText() { return readNote(bodyEl); }

  // The chevron always points the way the text is about to travel: into the Name while
  // the transcript has something to give, back into the transcript once it's empty and
  // the name is the one holding it. Derived from the two fields each time rather than
  // remembered, so it can never offer a move that isn't there.
  function movesBack() { return !transcriptText().trim(); }

  function syncMove() {
    var back = movesBack();
    moveBtn.classList.toggle('back', back);
    label(moveBtn, back ? 'Move name into Transcript' : 'Move transcript into Name');
    moveBtn.disabled = back && !nameEl.value.trim();
  }

  function moveText() {
    if (movesBack()) {
      var name = nameEl.value;
      nameEl.value = '';
      bodyEl.replaceChildren(renderNote(name));
    } else {
      nameEl.value = transcriptText();
      bodyEl.replaceChildren(renderNote(''));
    }
    align();
    paint(audio.currentTime);
    syncMove();
    flush();  // a move is a whole edit, not a keystroke: report it now, not on the timer
  }

  // Copy a field to the clipboard and hold a check on its button for a beat — the
  // clipboard gives no sign of its own that the copy landed.
  var COPIED_MS = 1200;

  function writeClipboard(text) {
    try {
      return navigator.clipboard.writeText(text);
    } catch (err) {
      return Promise.reject(err);  // no Clipboard API at all (insecure origin, old webview)
    }
  }

  function copyField(btn) {
    var text = btn.dataset.copy === 'name' ? nameEl.value : transcriptText();
    writeClipboard(text).then(function () {
      btn.classList.add('copied');
      clearTimeout(btn._copied);
      btn._copied = setTimeout(function () { btn.classList.remove('copied'); }, COPIED_MS);
    }).catch(function () { /* the clipboard wouldn't take it; the editor has no bar to say so */ });
  }

  // showModal makes the page behind the dialog inert to clicks and keys, but not to
  // sound. A row player left running kept playing under the modal, doubling the very
  // recording the editor autoplays a beat behind it. Taking the page over means taking
  // its sound too — every player but this one, which is about to start.
  function hushPage() {
    document.querySelectorAll('audio').forEach(function (el) {
      if (el !== audio) el.pause();
    });
  }

  function open(note) {
    hushPage();
    current = note;
    words = note.words || [];
    following = true;
    selection = null;
    press = null;
    nameEl.value = note.name || '';
    bodyEl.replaceChildren(renderNote(note.transcript || ''));
    audio.src = note.audioUrl;
    dialog.showModal();
    // Focus the waveform, not the transcript: the recording is already playing, so Space
    // should pause it at once. Editing waits for a click into the text, as any real edit
    // does. (showModal would otherwise focus the Name field — a text field that eats Space.)
    canvas.focus();
    align();
    syncMove();
    loadWaveform(note.audioUrl);
    // The click that opened the dialog is the gesture autoplay needs.
    audio.play().catch(function () { /* the user can still press play */ });
    tick();
  }

  // Stop the audio and report the last edit. Idempotent, because both ways out of the
  // dialog come through here: a button (which tears down, then closes) and Esc (which
  // fires `cancel` on its way to closing). The final save must never hang on the
  // `close` event alone — that one is dispatched from a queued task, so an edit made
  // in the moment before closing would be left waiting on it.
  function teardown() {
    if (!current) return;
    audio.pause();
    audio.removeAttribute('src');
    if (frame) cancelAnimationFrame(frame);
    frame = null;
    if (highlight) highlight.clear();
    flush();
    current = null;
    words = [];
    marks = [];
    painted = null;
    selection = null;
    press = null;
  }

  function closeEditor() {
    teardown();
    if (dialog.open) dialog.close();
  }

  dialog.addEventListener('cancel', teardown);  // Esc, before the dialog closes
  document.getElementById('editor-close').addEventListener('click', closeEditor);
  document.getElementById('editor-done').addEventListener('click', closeEditor);

  // A click on the dim margin around the dialog closes it too, the same as Done — a
  // modal you can dismiss by clicking off it, not only through the × in its corner. The
  // backdrop belongs to the <dialog>, so its clicks land on the element itself; the box
  // is measured because the element also owns the padding and the gaps between rows, and
  // a click there is inside the dialog and must not close it. Requiring the press to
  // start off the dialog as well keeps a selection dragged out past the edge from being
  // read as a click off it and tearing the note down mid-select.
  function isOffDialog(event) {
    var box = dialog.getBoundingClientRect();
    return event.clientX < box.left || event.clientX > box.right ||
           event.clientY < box.top || event.clientY > box.bottom;
  }
  var pressedOff = false;
  dialog.addEventListener('pointerdown', function (event) { pressedOff = isOffDialog(event); });
  dialog.addEventListener('click', function (event) {
    if (pressedOff && isOffDialog(event)) closeEditor();
  });

  nameEl.addEventListener('input', function () { scheduleSave(); syncMove(); });
  bodyEl.addEventListener('input', function () { scheduleSave(); scheduleAlign(); syncMove(); });
  moveBtn.addEventListener('click', moveText);
  dialog.querySelectorAll('.editor-clip').forEach(function (btn) {
    btn.addEventListener('click', function () { copyField(btn); });
  });
  // Once the user is working in the text, stop yanking it around under them.
  ['pointerdown', 'wheel', 'keydown'].forEach(function (event) {
    bodyEl.addEventListener(event, function () { following = false; });
  });

  dialog.querySelectorAll('.tool').forEach(function (button) {
    button.addEventListener('mousedown', function (event) { event.preventDefault(); });  // keep the caret
    button.addEventListener('click', function () {
      bodyEl.focus();
      // execCommand is deprecated but remains the only way to ask the engine for a
      // real list — and to undo one — without reimplementing block editing.
      document.execCommand(button.dataset.cmd);
      align();
      syncMove();
      scheduleSave();
    });
  });

  playBtn.addEventListener('click', function () { if (audio.paused) audio.play(); else audio.pause(); });
  audio.addEventListener('play', function () { playBtn.textContent = 'Pause'; tick(); });
  audio.addEventListener('pause', function () { playBtn.textContent = 'Play'; tick(); });
  audio.addEventListener('loadedmetadata', tick);

  // A click lands the playhead; dragging past a small wobble paints a selection over
  // that stretch of sound instead. Focusing the canvas moves focus off the text body,
  // so the Space and Delete keys below act on the audio rather than the transcript.
  var DRAG_PX = 4;
  canvas.addEventListener('pointerdown', function (event) {
    canvas.setPointerCapture(event.pointerId);
    canvas.focus();
    press = { x: event.clientX, at: timeAt(event) };
    setSelection(null);
    seek(event);
  });
  canvas.addEventListener('pointermove', function (event) {
    if (!press || !canvas.hasPointerCapture(event.pointerId) || !audio.duration) return;
    if (Math.abs(event.clientX - press.x) < DRAG_PX) return;
    var here = timeAt(event);
    setSelection({ from: Math.min(press.at, here), to: Math.max(press.at, here) });
  });
  canvas.addEventListener('lostpointercapture', function () { press = null; });

  // Space plays or pauses, and Delete (Backspace, on a Mac) cuts the words under a
  // selection — but only when focus isn't in a text field, where those keys still type
  // and edit as normal. A focused button keeps its own Space so Done and Close still fire.
  function isTyping(el) {
    return !!el && (el.isContentEditable || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA');
  }
  dialog.addEventListener('keydown', function (event) {
    if (isTyping(event.target)) return;
    if (event.key === ' ' || event.key === 'Spacebar') {
      if (event.target.tagName === 'BUTTON') return;
      event.preventDefault();
      if (audio.paused) audio.play(); else audio.pause();
    } else if (selection && (event.key === 'Delete' || event.key === 'Backspace')) {
      event.preventDefault();
      deleteSelection();
    }
  });

  if (window.ResizeObserver) new ResizeObserver(drawWave).observe(canvas);

  window.HighdeasEditor = { open: open };
})();
