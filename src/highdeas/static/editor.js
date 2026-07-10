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

  var SAVE_MS = 400;      // debounce before an edit is reported to the caller
  var ALIGN_MS = 150;     // debounce before edited text is re-matched to the audio
  var PEAK_BINS = 2048;   // amplitude bins kept per recording, resampled when drawn
  // How far past the last matched word we'll hunt for the next one. Edits shift the
  // text away from what was spoken; this is how much drift the highlight survives.
  var LOOKAHEAD = 20;

  var current = null;    // the open note: {audioUrl, onChange}
  var words = [];        // [[startSeconds, word], …] as the transcriber heard them
  var marks = [];        // those words matched onto the live text, in time order
  var painted = null;    // the mark currently lit, so a frame that changes nothing costs nothing
  var peaks = null;      // normalized amplitudes, or null when the audio won't decode
  var following = true;  // scroll the spoken word into view until the user takes over
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
      pen.fillStyle = bar / bars < played ? '#3b82f6' : 'rgba(128,128,128,.45)';
      pen.fillRect(bar * (barWidth + gap), middle - tall / 2, barWidth, tall);
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

  function seek(event) {
    if (!audio.duration) return;
    var box = canvas.getBoundingClientRect();
    var ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    audio.currentTime = ratio * audio.duration;
    tick();
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

  function open(note) {
    current = note;
    words = note.words || [];
    following = true;
    nameEl.value = note.name || '';
    bodyEl.replaceChildren(renderNote(note.transcript || ''));
    audio.src = note.audioUrl;
    dialog.showModal();
    bodyEl.focus();
    align();
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
  }

  function closeEditor() {
    teardown();
    if (dialog.open) dialog.close();
  }

  dialog.addEventListener('cancel', teardown);  // Esc, before the dialog closes
  document.getElementById('editor-close').addEventListener('click', closeEditor);
  document.getElementById('editor-done').addEventListener('click', closeEditor);

  nameEl.addEventListener('input', scheduleSave);
  bodyEl.addEventListener('input', function () { scheduleSave(); scheduleAlign(); });
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
      scheduleSave();
    });
  });

  playBtn.addEventListener('click', function () { if (audio.paused) audio.play(); else audio.pause(); });
  audio.addEventListener('play', function () { playBtn.textContent = 'Pause'; tick(); });
  audio.addEventListener('pause', function () { playBtn.textContent = 'Play'; tick(); });
  audio.addEventListener('loadedmetadata', tick);

  canvas.addEventListener('pointerdown', function (event) {
    canvas.setPointerCapture(event.pointerId);
    seek(event);
  });
  canvas.addEventListener('pointermove', function (event) {
    if (canvas.hasPointerCapture(event.pointerId)) seek(event);
  });

  if (window.ResizeObserver) new ResizeObserver(drawWave).observe(canvas);

  window.HighdeasEditor = { open: open };
})();
