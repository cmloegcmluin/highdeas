/* The note editor dialog: play the recording back as a scrubbable waveform, light
   up each word as it's spoken, and give the note's title and body room to be
   worked on — so a rough transcription gets massaged here rather than shipped to
   Notesnook half-finished just to get it out of the inbox.

   The caller owns the memo and its persistence; this file owns the dialog. Open it
   with HighdeasEditor.open({audioUrl, name, transcript, words, onChange, onCut, onClose})
   and it reports edits back through onChange({name, transcript}), then says once
   through onClose() that it is done with the note — the caller's cue to let go of
   whatever it was holding on the editor's behalf. Deleting a stretch of the waveform
   asks the caller to cut the recording itself, since the recording is the caller's and
   not the dialog's: onCut({from, to}) answers with {audioUrl, words} — the cut recording
   to play from here, and the word timings it left behind.

   Notes are stored as plain text, so a list is just its Markdown line ("- x",
   "1. x"). notes.js is what those lines mean: the dialog renders them as real
   <ul>/<ol> to edit and reads them back out as lines when it reports a change. */
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
  // What the stylesheet says, for the drawing the canvas has to do by hand: the colours a
  // note is read in are declared once, in app.css, and never copied into here.
  function style(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // The colour of the sound being heard. The same declaration lights the word being
  // spoken (::highlight(spoken) in app.css), so the bars and the text can't drift apart.
  var SPOKEN = style('--spoken');
  // The colour of what is chosen, and the strength it is laid on at. The same two
  // declarations wash the words picked in the transcript (.editor-body ::selection in
  // app.css), so a choice can't read as one blue over its sound and another over its words.
  var PICKED = style('--picked');
  var PICKED_WASH = parseFloat(style('--picked-wash')) / 100;
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

  // What a note's lines mean is notes.js's, not the dialog's: the inbox row draws the
  // same note from the same grammar, so a list can't read as bullets here and as dashes
  // there.
  var renderNote = window.HighdeasNote.render;
  var readNote = window.HighdeasNote.read;

  /* ---- The list buttons --------------------------------------------------- */

  // The engine's own insertUnorderedList/insertOrderedList cannot be trusted with this
  // note format, on either desk (Chromium here, WebKit on the Mac). Turning a list OFF,
  // Chromium replaces the item with a bare styled <span> and a <br> — no block at all —
  // which reads back as the line plus a blank one. Turning one ON over a whole body it
  // nests the <ul> inside a <p>, and a <p> is read as a single line, so three bullets
  // saved as "milkeggsbread". So the dialog rebuilds the blocks itself. The cost is that
  // Ctrl+Z no longer walks a list button back — the engine only records its own edits.

  // A note's body is a flat run of blocks, so every line of it is either a <p> of prose
  // or an <li> in a list. This is that run, in order, each line carrying the list it is
  // in — which is the only thing a toggle changes.
  function linesOf() {
    var lines = [];
    Array.prototype.forEach.call(bodyEl.childNodes, function (node) {
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.nodeValue.trim()) lines.push({ node: node, tag: null });
      } else if (node.nodeType !== Node.ELEMENT_NODE) {
        return;
      } else if (node.tagName === 'UL' || node.tagName === 'OL') {
        Array.prototype.forEach.call(node.children, function (item) {
          if (item.tagName === 'LI') lines.push({ node: item, tag: node.tagName });
        });
      } else {
        lines.push({ node: node, tag: null });
      }
    });
    return lines;
  }

  // How far into a line, in characters, a selection boundary sits.
  function offsetIn(line, container, offset) {
    var range = document.createRange();
    range.selectNodeContents(line);
    try { range.setEnd(container, offset); } catch (err) { return 0; }
    return range.toString().length;
  }

  // Where the selection sits, as a line and a character offset at each end. Rebuilding
  // the blocks throws the live selection away, so it is measured against the text — which
  // the rebuild doesn't touch — and put back afterwards.
  function markSelection(lines) {
    var selected = getSelection();
    if (!selected.rangeCount) return null;
    var range = selected.getRangeAt(0);
    function spot(container, offset) {
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].node;
        if (line === container || line.contains(container)) {
          return { line: i, at: offsetIn(line, container, offset) };
        }
      }
      return null;
    }
    var from = spot(range.startContainer, range.startOffset);
    var to = spot(range.endContainer, range.endOffset);
    return from && to ? { from: from, to: to } : null;
  }

  // The text node and offset a character count lands on, for putting the caret back.
  function pointIn(block, at) {
    var walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    var seen = 0;
    var node;
    while ((node = walker.nextNode())) {
      if (seen + node.nodeValue.length >= at) return { node: node, offset: at - seen };
      seen += node.nodeValue.length;
    }
    return { node: block, offset: block.childNodes.length };
  }

  function restoreSelection(lines, mark) {
    if (!mark) return;
    var from = pointIn(lines[mark.from.line].block, mark.from.at);
    var to = pointIn(lines[mark.to.line].block, mark.to.at);
    var range = document.createRange();
    try {
      range.setStart(from.node, from.offset);
      range.setEnd(to.node, to.offset);
    } catch (err) { return; }  // the text moved under us; leave the caret where it fell
    var selected = getSelection();
    selected.removeAllRanges();
    selected.addRange(range);
  }

  // The block a line wants now: its own element when it is already the right kind, so an
  // untouched line keeps its identity, or a new one holding the same contents.
  function blockFor(line) {
    var wanted = line.tag ? 'LI' : 'P';
    var node = line.node;
    if (node.nodeType === Node.ELEMENT_NODE && node.tagName === wanted) return node;
    var block = document.createElement(wanted);
    if (node.nodeType === Node.TEXT_NODE) block.appendChild(node);
    else while (node.firstChild) block.appendChild(node.firstChild);
    return block;
  }

  // Lay the lines back out, gathering each run of same-tagged lines into one list — the
  // same shape notes.js renders from text, so what is rebuilt here reads back unchanged.
  function rebuild(lines) {
    var frag = document.createDocumentFragment();
    var list = null;
    var listTag = null;
    lines.forEach(function (line) {
      if (line.tag !== listTag) {
        list = line.tag ? frag.appendChild(document.createElement(line.tag)) : null;
        listTag = line.tag;
      }
      line.block = blockFor(line);
      (list || frag).appendChild(line.block);
    });
    if (!frag.childNodes.length) frag.appendChild(document.createElement('p'));
    bodyEl.replaceChildren(frag);
  }

  // Pressing a list's own button over that list turns it back into prose; pressing it
  // over anything else makes the whole selection that list. So a selection of mixed
  // lines becomes one list on the first press and prose on the second, and a list is
  // never left half-converted.
  function toggleList(tag) {
    var lines = linesOf();
    var picked = lines.filter(function (line) { return inSelection(line.node); });
    if (!picked.length) return;
    var off = picked.every(function (line) { return line.tag === tag; });
    var mark = markSelection(lines);
    picked.forEach(function (line) { line.tag = off ? null : tag; });
    rebuild(lines);
    restoreSelection(lines, mark);
  }

  function inSelection(node) {
    var selected = getSelection();
    return selected.rangeCount > 0 && selected.getRangeAt(0).intersectsNode(node);
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

  // The recording cut into one chunk per word: each runs from where its word was spoken
  // until the next word begins, and the first reaches back to the start — so every second
  // of sound belongs to exactly one word, and none of it is out of reach. A spoken word
  // the text no longer holds has no mark, so its sound belongs to the chunk before it.
  function chunks() {
    if (!audio.duration) return [];
    return marks.map(function (mark, i) {
      return {
        from: i ? mark.at : 0,
        to: i + 1 < marks.length ? marks[i + 1].at : audio.duration,
        token: mark.token,
        word: wordOf(mark.token),
      };
    });
  }

  // A chunk's word as the transcript now reads it, which is what the chunk stands for.
  function wordOf(token) {
    return token.node.isConnected ? token.node.nodeValue.slice(token.from, token.to) : '';
  }

  // The chunk a moment falls in, or null when the recording has no chunks at all. The
  // very end of the recording is past every chunk's end, so it belongs to the last.
  function chunkAt(list, seconds) {
    for (var i = 0; i < list.length; i++) {
      if (seconds < list[i].to) return i;
    }
    return list.length ? list.length - 1 : null;
  }

  // Room kept at the foot of the waveform for the words, and swept clear either side of a
  // divider so it reads as one: a grey hairline among grey bars is just one more bar,
  // however dark it is drawn, and only the gap around it says otherwise.
  //
  // The dividers stop above that row rather than running the height of the canvas. A
  // divider through the words would be a wall each had to fit inside, and most of them
  // don't; ending it at the row leaves the words a clear band to sit in, so a long one
  // can overrun its chunk and still be read rather than being dropped for want of space.
  var WORD_ROW = 15;
  var DIVIDER_GAP = 3;

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
    var row = Math.round(WORD_ROW * ratio);
    var wave = height - row;  // the bars keep the top; the words have the foot to themselves
    var barWidth = Math.max(1, Math.round(2 * ratio));
    var gap = Math.max(1, Math.round(ratio));
    var bars = Math.max(1, Math.floor(width / (barWidth + gap)));
    var played = audio.duration ? audio.currentTime / audio.duration : 0;
    var middle = wave / 2;
    for (var bar = 0; bar < bars; bar++) {
      // A flat placeholder line when the audio wouldn't decode: still scrubbable.
      var level = peaks ? peaks[Math.floor(bar * peaks.length / bars)] : 0.1;
      var tall = Math.max(2 * ratio, level * (wave - 6 * ratio));
      pen.fillStyle = bar / bars < played ? SPOKEN : 'rgba(128,128,128,.45)';
      pen.fillRect(bar * (barWidth + gap), middle - tall / 2, barWidth, tall);
    }
    drawChunks(pen, width, height, row, ratio);
    if (selection && audio.duration) {
      var from = selection.from / audio.duration * width;
      var to = selection.to / audio.duration * width;
      // The same blue at the same strength as the words picked in the transcript
      // (.editor-body ::selection): one choice, laid over its sound and over its text.
      pen.globalAlpha = PICKED_WASH;
      pen.fillStyle = PICKED;
      // The full height, words and all: a word is taken along with its sound, so it is lit
      // along with it.
      pen.fillRect(from, 0, Math.max(1, to - from), height);
      pen.globalAlpha = 1;
    }
  }

  // Each word's own stretch of the recording, marked off and named: a hairline where it
  // begins, and the word itself under the middle of it. Every word is drawn — one with
  // more to say than its chunk has room for overruns into its neighbours' band rather
  // than being dropped, which is why the dividers stop short of that band.
  function drawChunks(pen, width, height, row, ratio) {
    var list = chunks();
    if (!list.length) return;
    pen.font = Math.round(10 * ratio) + 'px ' + getComputedStyle(canvas).fontFamily;
    pen.textAlign = 'center';
    pen.textBaseline = 'middle';
    // One ink for the dividers and the words: both are the frame the sound is read in.
    pen.fillStyle = 'rgba(128,128,128,.9)';
    list.forEach(function (chunk, i) {
      var from = chunk.from / audio.duration * width;
      var to = chunk.to / audio.duration * width;
      if (i) {
        var rule = Math.max(1, Math.round(ratio));
        var gap = Math.round(DIVIDER_GAP * ratio);
        pen.clearRect(Math.round(from) - gap, 0, gap * 2 + rule, height - row);
        pen.fillRect(Math.round(from), 0, rule, height - row);
      }
      if (chunk.word) pen.fillText(chunk.word, (from + to) / 2, height - row / 2);
    });
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

  // Every caret move in the transcript asks what is picked now, and while the user is
  // typing the answer is "nothing" over and over: a frame that changes nothing costs
  // nothing, the same bargain paint() already strikes for the spoken word.
  function setSelection(sel) {
    if (!sel && !selection) return;
    if (sel && selection && sel.from === selection.from && sel.to === selection.to) return;
    selection = sel;
    drawWave();
  }

  // The words whose spoken span overlaps `span`, in text order. Each mark runs until the
  // next one begins; the last runs to the end of the recording. The server takes the same
  // span out of the word timings by the same overlap, so the words that leave the text
  // are exactly the ones that leave the timings.
  function selectedMarks(span) {
    var chosen = [];
    for (var i = 0; i < marks.length; i++) {
      var end = (i + 1 < marks.length) ? marks[i + 1].at : (audio.duration || marks[i].at);
      if (marks[i].at < span.to && end > span.from) chosen.push(marks[i]);
    }
    return chosen;
  }

  // Cut the words a stretch of sound was spoken over out of the text. Re-align first so
  // the marks point at live text nodes, then cut from the first selected word to the
  // last — sweeping up any unmatched words caught between — and one bordering space.
  function cutWords(span) {
    align();
    var chosen = selectedMarks(span);
    if (!chosen.length) return;  // a stretch of silence spoke no words to cut
    var first = chosen[0].token;
    var last = chosen[chosen.length - 1].token;
    var range = document.createRange();
    try {
      range.setStart(first.node, first.from);
      range.setEnd(last.node, last.to);
      eatSpace(range);
      range.deleteContents();
      bodyEl.normalize();
    } catch (err) { /* the text moved under us; leave it be */ }
  }

  // Whether `range` holds the whole of a word. Half a word deleted leaves letters on the
  // page for its sound to still belong to, so only a word taken whole takes its sound.
  function holdsWord(range, token) {
    var word = document.createRange();
    try {
      word.setStart(token.node, token.from);
      word.setEnd(token.node, token.to);
    } catch (err) {
      return false;  // the node was edited out from under us; the next align() fixes it
    }
    return range.compareBoundaryPoints(Range.START_TO_START, word) <= 0 &&
           range.compareBoundaryPoints(Range.END_TO_END, word) >= 0;
  }

  // Which chunks a text range holds whole, as first and last index, or null when it holds
  // none — a letter taken out of a word is not the word leaving the note.
  function chunksHeld(list, range) {
    var first = -1;
    var last = -1;
    for (var i = 0; i < list.length; i++) {
      if (!range || !holdsWord(range, list[i].token)) continue;
      if (first < 0) first = i;
      last = i;
    }
    return first < 0 ? null : [first, last];
  }

  // The stretch of recording a text range's words were spoken over — the other way round
  // from selectedMarks, which reads a stretch of sound off into words. Whole chunks either
  // way, so a run chosen in the text and one chosen on the waveform are the same thing.
  function spokenOver(range) {
    var list = chunks();
    var held = chunksHeld(list, range);
    return held && { from: list[held[0]].from, to: list[held[1]].to };
  }

  // The transcript's own selection, when there is one and it is in the transcript.
  function bodyRange() {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed) return null;
    var range = sel.getRangeAt(0);
    return bodyEl.contains(range.commonAncestorContainer) ? range : null;
  }

  // What the transcript has picked, laid over the waveform. This is the one place the
  // choice is read from: a click on the waveform makes its choice by selecting those words
  // in the text (see pointerdown), and this reads it back, so the two can't disagree.
  function syncPicked() {
    var range = bodyRange();
    setSelection(range && spokenOver(range));  // no range, no chunks to work out
  }

  // Choose a run of chunks by selecting their words in the transcript; the waveform's band
  // follows from that. Both ends are taken whole, so what comes back out of bodyRange is
  // the same run that went in.
  function pickWords(list, first, last) {
    try {
      window.getSelection().setBaseAndExtent(
        list[first].token.node, list[first].token.from,
        list[last].token.node, list[last].token.to);
    } catch (err) { /* the text moved under us; the next align() fixes it */ }
    syncPicked();
  }

  // Take a stretch out of the recording, and the words spoken over it out of the text.
  // The two are one note in two forms, so both go or neither does — and cutting the file
  // is the caller's to do and the one step that can be refused, which is why the sound
  // goes first. `cutText` is how the words go, for the gesture that hasn't taken them
  // yet: the waveform's cuts them here, where a deletion in the text already has.
  function cutSound(span, cutText) {
    if (!current) return;
    var note = current;
    var at = afterCut(audio.currentTime, span);
    audio.pause();  // the stretch being played is about to stop being there
    Promise.resolve(note.onCut({ from: span.from, to: span.to })).then(function (cut) {
      if (current !== note) return;  // the dialog has moved on to another note
      if (cutText) cutText();     // by the timings as they were: the span is in their time
      words = cut.words || [];    // and from here by the ones the cut left, each earlier
      note.audioUrl = cut.audioUrl;
      align();
      loadRecording(at);
      scheduleSave();
    }, function () {
      // Refused, and the caller has said so. The recording is as it was either way — the
      // waveform's words are still there unasked-for, and the text's are already gone.
    });
  }

  // Where the playhead lands once a span is gone: where it was, if that was before the
  // cut; that much earlier, if it was after; and at the seam if it was inside — which is
  // where the waveform gesture leaves it, the press that began the drag having put it
  // there, and where a deletion in the text leaves it if it cut what was playing.
  function afterCut(at, span) {
    if (at <= span.from) return at;
    return at >= span.to ? at - (span.to - span.from) : span.from;
  }

  // Delete the stretch under the waveform selection. The words go with it here, since
  // this gesture is aimed at the sound and hasn't touched them.
  function deleteSelection() {
    var span = selection;
    // The band isn't cleared here: it is the transcript's selection shown, and that is
    // still standing until the words go. cutWords takes them, and the choice reads as
    // gone because it is.
    if (span) cutSound(span, function () { cutWords(span); });
  }

  // Play the recording the cut left, landing the playhead where the cut left the moment it
  // was at. A cut recording keeps its filename, so the cut answers with a URL of its own —
  // handed a name it is already holding, a player plays what it has rather than what is
  // there — and how long the new sound runs isn't known until its metadata is.
  function loadRecording(at) {
    audio.src = current.audioUrl;
    audio.addEventListener('loadedmetadata', function once() {
      audio.removeEventListener('loadedmetadata', once);
      audio.currentTime = Math.min(at, audio.duration || 0);
      tick();
    });
    loadWaveform(current.audioUrl);
    paint(audio.currentTime);
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

  function copyField(btn) {
    var text = btn.dataset.copy === 'name' ? nameEl.value : transcriptText();
    window.HighdeasClip.copy(btn, text)
      .catch(function () { /* the clipboard wouldn't take it; the editor has no bar to say so */ });
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
    // After the flush, so the last edit reaches the caller while it still holds the note.
    if (current.onClose) current.onClose();
    current = null;
    words = [];
    marks = [];
    painted = null;
    selection = null;
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
  // Deleting words takes the sound they were spoken over with them, the way deleting a
  // stretch of sound takes its words. The range about to go is read here, before the
  // engine edits it — a moment later the marks point at text that no longer exists — and
  // the cut is asked for while the engine takes the text. Deletions only: typing over a
  // selection replaces it, and a correction must not cost the recording.
  bodyEl.addEventListener('beforeinput', function (event) {
    if (event.inputType.indexOf('delete') !== 0) return;
    var target = event.getTargetRanges()[0];
    if (!target) return;
    align();  // against the text as it stands, not as it read at the last match
    var range = document.createRange();
    range.setStart(target.startContainer, target.startOffset);
    range.setEnd(target.endContainer, target.endOffset);
    var span = spokenOver(range);
    if (span) cutSound(span);
  });
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
      toggleList(button.dataset.list.toUpperCase());
      align();
      syncMove();
      scheduleSave();
    });
  });

  playBtn.addEventListener('click', function () { if (audio.paused) audio.play(); else audio.pause(); });
  audio.addEventListener('play', function () { playBtn.textContent = 'Pause'; tick(); });
  audio.addEventListener('pause', function () { playBtn.textContent = 'Play'; tick(); });
  audio.addEventListener('loadedmetadata', tick);

  // A click takes the whole chunk of sound one word was spoken over — never a stretch
  // aimed by hand — and lands the playhead at the top of it, so clicking a word plays from
  // that word. Shift reaches from where the choice already starts to the chunk clicked and
  // takes the run between, in either direction. The choice is made by selecting those
  // words in the transcript, which is what puts the band on the waveform, so a click up
  // here and a selection dragged down there leave the note in the same state.
  //
  // The press is stopped from doing anything of its own: its default is to collapse the
  // document's selection, which is the very thing being set. Focus is taken by hand
  // instead, moving it off the text body so the Space and Delete keys below act on the
  // audio rather than the transcript. A recording with no word timings has no chunks, so
  // a click there only moves the playhead.
  canvas.addEventListener('pointerdown', function (event) {
    event.preventDefault();
    canvas.focus();
    align();  // the words move as the text is edited, and their chunks with them
    var list = chunks();
    var i = chunkAt(list, timeAt(event));
    if (i === null) { syncPicked(); seek(event); return; }
    var held = event.shiftKey && chunksHeld(list, bodyRange());
    var from = held ? held[0] : i;
    pickWords(list, Math.min(from, i), Math.max(from, i));
    if (!event.shiftKey) audio.currentTime = list[i].from;
    tick();
  });

  // A selection made in the transcript lights the chunks of waveform its words were spoken
  // over — the same choice from the other end. Only while a note is open: the event is the
  // document's, and the body still holds the last note's text after the dialog closes.
  document.addEventListener('selectionchange', function () {
    if (current) syncPicked();
  });

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
