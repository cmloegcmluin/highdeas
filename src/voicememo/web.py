"""Local Flask app for reviewing, editing, and routing memos."""
import os

from flask import Flask, redirect, render_template_string, request, send_from_directory

# Inline, self-contained brand icons for the route toggle (no external assets).
NOTESNOOK_SVG = (
    '<svg viewBox="162 160 700 700" xmlns="http://www.w3.org/2000/svg" fill="#008837" aria-label="Notesnook">'
    '<path d="M724.985 682.919C707.73 733.33 673.15 775.984 627.397 803.291C581.645 830.598 527.687 840.787 '
    "475.128 832.044C422.568 823.301 374.814 796.194 340.365 755.546C305.916 714.898 287.006 663.347 287 "
    "610.064V499.814L366.121 532.867V610.019C366.114 630.798 370.555 651.337 379.145 670.256C387.735 689.176 "
    "400.276 706.037 415.925 719.707C418.895 722.294 421.978 724.814 425.161 727.166C448.518 744.554 476.563 "
    "754.518 505.655 755.763C506.645 755.763 507.601 755.842 508.58 755.864C509.559 755.887 510.83 755.864 "
    "511.955 755.864C513.08 755.864 514.205 755.864 515.33 755.864C516.455 755.864 517.265 755.864 518.255 "
    "755.763C547.336 754.515 575.371 744.56 598.726 727.188C601.899 724.837 604.981 722.328 607.963 "
    '719.741C628.519 701.761 643.619 678.375 651.545 652.241L724.985 682.919Z"/>'
    '<path d="M737 414V610.065C737 612.596 737 615.139 736.842 617.67L657.879 584.651V414C657.866 376.316 '
    "643.272 340.099 617.154 312.934C591.035 285.77 555.419 269.766 517.765 268.274C480.11 266.782 443.339 "
    "279.918 415.154 304.931C386.968 329.944 369.554 364.893 366.56 402.457C366.279 406.26 366.121 410.119 "
    '366.121 414V462.712L287 429.637V189H512C571.674 189 628.903 212.705 671.099 254.901C713.295 297.097 737 354.326 737 414Z"/>'
    "</svg>"
)
DRIVE_SVG = (
    '<svg viewBox="0 0 87.3 78" xmlns="http://www.w3.org/2000/svg">'
    '<path d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8h-27.5c0 1.55.4 3.1 1.2 4.5z" fill="#0066da"/>'
    '<path d="m43.65 25-13.75-23.8c-1.35.8-2.5 1.9-3.3 3.3l-25.4 44a9.06 9.06 0 0 0 -1.2 4.5h27.5z" fill="#00ac47"/>'
    '<path d="m73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5h-27.502l5.852 11.5z" fill="#ea4335"/>'
    '<path d="m43.65 25 13.75-23.8c-1.35-.8-2.9-1.2-4.5-1.2h-18.5c-1.6 0-3.15.45-4.5 1.2z" fill="#00832d"/>'
    '<path d="m59.8 53h-32.3l-13.75 23.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" fill="#2684fc"/>'
    '<path d="m73.4 26.5-12.7-22c-.8-1.4-1.95-2.5-3.3-3.3l-13.75 23.8 16.15 28h27.45c0-1.55-.4-3.1-1.2-4.5z" fill="#ffba00"/>'
    "</svg>"
)
TRASH_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>'
)

# One stylesheet shared by the review and bin pages so their chrome is identical —
# same title bar, top-right link, and header row — and nothing jumps when you flip
# between them. Their grids share widths too: 300 | flex | a 334px middle band |
# two 104px action columns, so only the middle band's contents differ per page.
_STYLE = """<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif; max-width: 1300px;
         margin: 0 auto; padding: 24px; line-height: 1.45; }
  h1 { font-size: 1.35rem; margin: 0; }
  .topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; }
  .topbar a { color: #3b82f6; text-decoration: none; font-size: .9rem; }
  .empty { opacity: .7; padding: 48px 0; text-align: center; }
  .grid { display: grid; gap: 14px 18px; align-items: center; }
  .grid.review { grid-template-columns: 300px minmax(220px, 1fr) 34px 200px 100px 104px 104px; }
  .grid.bin    { grid-template-columns: 300px minmax(220px, 1fr) 170px 56px 108px 104px 104px; }
  .grid .head { font-size: .7rem; text-transform: uppercase; letter-spacing: .04em; opacity: .55;
                display: flex; align-items: flex-end; min-height: 32px;
                padding-bottom: 4px; border-bottom: 1px solid rgba(128,128,128,.25); }
  .grid .sep { grid-column: 1 / -1; border-top: 1px solid rgba(128,128,128,.18); }
  /* Bulk actions live in their own column headers so they sit directly over the
     column they act on, instead of being pushed around by the topbar link. */
  .head form { width: 100%; margin: 0; }
  .head-btn { font: inherit; font-size: .72rem; text-transform: none; letter-spacing: normal;
              width: 100%; padding: 4px 9px; border-radius: 7px; cursor: pointer; background: transparent;
              color: inherit; border: 1px solid rgba(128,128,128,.4);
              transition: color .15s, border-color .15s; }
  #submit-all:hover, .restore-all:hover { border-color: #3b82f6; color: #3b82f6; }
  #trash-all:hover, .empty-bin:hover { border-color: #e5484d; color: #e5484d; }

  /* Review rows */
  .memo { display: contents; }
  .memo audio { width: 100%; }
  .memo textarea, .memo input[type=text] {
    width: 100%; box-sizing: border-box; padding: 8px; font: inherit;
    border: 1px solid rgba(128,128,128,.4); border-radius: 8px; background: transparent; color: inherit; }
  .memo textarea { min-height: 60px; resize: vertical; }
  .memo .copy { font: inherit; font-size: 1.4rem; line-height: 1; padding: 0; height: 40px; width: 100%;
                display: flex; align-items: center; justify-content: center; cursor: pointer;
                background: transparent; color: inherit; opacity: .45; border-radius: 8px;
                border: 1px solid rgba(128,128,128,.35);
                transition: opacity .15s, color .15s, border-color .15s; }
  .memo .copy:hover { opacity: 1; color: #3b82f6; border-color: #3b82f6; }
  .memo .go { font: inherit; padding: 9px 0; width: 100%; border-radius: 8px; border: none;
              background: #3b82f6; color: #fff; cursor: pointer; }
  .memo .del { padding: 9px 0; width: 100%; border-radius: 8px; cursor: pointer;
               background: transparent; color: inherit; opacity: .4;
               border: 1px solid rgba(128,128,128,.35);
               transition: color .15s, opacity .15s, border-color .15s; }
  .memo .del:hover { opacity: 1; color: #e5484d; border-color: #e5484d; }
  .memo .del svg { width: 16px; height: 16px; display: block; margin: 0 auto; }
  .toggle { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
  .toggle input { position: absolute; width: 0; height: 0; opacity: 0; }
  .toggle .ic { width: 18px; height: 18px; opacity: .35; transition: opacity .15s; }
  .toggle .ic svg { width: 100%; height: 100%; display: block; }
  .toggle .ns { opacity: 1; }
  .toggle input:checked ~ .ns { opacity: .35; }
  .toggle input:checked ~ .dr { opacity: 1; }
  .toggle .track { position: relative; flex: none; width: 40px; height: 22px; border-radius: 999px;
                   background: rgba(128,128,128,.4); transition: background .15s; }
  .toggle .track::after { content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px;
                          border-radius: 50%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.35); transition: transform .15s; }
  .toggle input:checked ~ .track { background: #2684fc; }
  .toggle input:checked ~ .track::after { transform: translateX(18px); }

  /* Bin rows */
  .row { display: contents; }
  .row audio { width: 100%; }
  .row .text { font-size: .9rem; white-space: pre-wrap; max-height: 5.5em; overflow: auto; opacity: .85; }
  .row .name { font-weight: 600; }
  .row .dest { display: flex; align-items: center; }
  .row .dest svg { width: 20px; height: 20px; display: block; }
  .row .destlink { background: transparent; border: none; padding: 0; cursor: pointer;
                   display: flex; align-items: center; }
  .row .when { font-size: .8rem; opacity: .6; }
  .binbtn { font: inherit; padding: 8px 0; width: 100%; border-radius: 8px; cursor: pointer;
            background: transparent; color: inherit; border: 1px solid rgba(128,128,128,.4);
            transition: color .15s, border-color .15s; }
  .binbtn.restore:hover { border-color: #3b82f6; color: #3b82f6; }
  .binbtn.purge:hover { border-color: #e5484d; color: #e5484d; }
  .row form { width: 100%; margin: 0; }
</style>"""


_PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Highdeas</title>
""" + _STYLE + """
</head>
<body>
  <div class="topbar">
    <h1>Highdeas</h1>
    <a href="/bin">Bin →</a>
  </div>
  <main id="content">"""


# The reviewable-memo list on its own, so it can be rendered both inside the full
# page and alone for the client's /pending poll (which splices in new rows).
CONTENT_HTML = """{% if not memos %}
    {% if incoming %}
    <p class="empty">Transcribing your memos…</p>
    {% else %}
    <p class="empty">Nothing to review. Record a memo and it'll show up here.</p>
    {% endif %}
  {% else %}
  <div class="grid review">
    <div class="head">Audio</div>
    <div class="head">Transcript</div>
    <div class="head"></div>
    <div class="head">Name</div>
    <div class="head">Route</div>
    <div class="head"><button type="button" id="submit-all" class="head-btn">Submit all</button></div>
    <div class="head"><button type="button" id="trash-all" class="head-btn">Trash all</button></div>
    {% for m in memos %}
    {% if not loop.first %}<div class="sep"></div>{% endif %}
    <div class="memo" data-file="{{ m.audio_filename }}">
      <audio controls src="/audio/{{ m.audio_filename }}"></audio>
      <textarea name="transcript" aria-label="Transcript">{{ m.transcript }}</textarea>
      <button type="button" class="copy" title="Move transcript into Name" aria-label="Move transcript into Name">&rsaquo;</button>
      <input type="text" name="name" value="{{ m.name }}" placeholder="Name…" autocomplete="off" aria-label="Name">
      <label class="toggle" title="Left = Notesnook, right = Google Drive">
        <input type="checkbox" name="route" value="drive" {{ 'checked' if m.route == 'drive' }}>
        <span class="ic ns" aria-label="Notesnook">""" + NOTESNOOK_SVG + """</span>
        <span class="track"></span>
        <span class="ic dr" aria-label="Google Drive">""" + DRIVE_SVG + """</span>
      </label>
      <button type="button" class="go">Submit</button>
      <button type="button" class="del" title="Delete" aria-label="Delete">""" + TRASH_SVG + """</button>
    </div>
    {% endfor %}
  </div>
  {% endif %}"""


_PAGE_TAIL = """  </main>
<script>
(function () {
  var content = document.getElementById('content');
  if (!content) return;

  // Rows this window has already submitted or trashed. A poll's snapshot can
  // still list one as pending (it was taken before the POST landed), so we skip
  // re-adding anything here — otherwise an optimistically-removed row would flash
  // back in.
  var retired = {};

  function urlFor(prefix, memo) { return prefix + encodeURIComponent(memo.dataset.file); }

  function fields(memo) {
    return new URLSearchParams({
      name: memo.querySelector('input[name=name]').value,
      transcript: memo.querySelector('textarea[name=transcript]').value,
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
    p.textContent = "Nothing to review. Record a memo and it'll show up here.";
    content.innerHTML = '';
    content.appendChild(p);
  }

  function removeRow(memo) {
    var grid = memo.closest('.grid');
    var prev = memo.previousElementSibling;
    if (prev && prev.classList.contains('sep')) {
      prev.remove();
    } else {
      var next = memo.nextElementSibling;
      if (next && next.classList.contains('sep')) next.remove();
    }
    memo.remove();
    if (grid && !grid.querySelector('.memo')) showEmpty();
  }

  function submitRow(memo) {
    clearTimeout(memo._timer);
    var data = fields(memo);
    var url = urlFor('/submit/', memo);
    retired[memo.dataset.file] = true;
    removeRow(memo);
    post(url, data);
  }

  function trashRow(memo) {
    clearTimeout(memo._timer);
    var url = urlFor('/delete/', memo);
    retired[memo.dataset.file] = true;
    removeRow(memo);
    post(url);
  }

  function wire(memo) {
    var transcript = memo.querySelector('textarea[name=transcript]');
    var name = memo.querySelector('input[name=name]');
    var route = memo.querySelector('input[name=route]');
    [transcript, name].forEach(function (el) {
      el.addEventListener('input', function () { scheduleSave(memo); });
      el.addEventListener('blur', function () { flush(memo); });
    });
    route.addEventListener('change', function () { flush(memo); });
    memo.querySelector('.copy').addEventListener('click', function () {
      name.value = transcript.value;
      transcript.value = '';
      flush(memo);
    });
    memo.querySelector('.go').addEventListener('click', function () { submitRow(memo); });
    memo.querySelector('.del').addEventListener('click', function () { trashRow(memo); });
  }

  content.querySelectorAll('.memo').forEach(wire);

  var submitAll = document.getElementById('submit-all');
  if (submitAll) submitAll.addEventListener('click', function () {
    content.querySelectorAll('.memo').forEach(submitRow);
  });
  var trashAll = document.getElementById('trash-all');
  if (trashAll) trashAll.addEventListener('click', function () {
    var memos = content.querySelectorAll('.memo');
    if (!memos.length) return;
    if (!confirm('Trash all ' + memos.length + ' memo' + (memos.length === 1 ? '' : 's') + '? They go to the bin.')) return;
    memos.forEach(trashRow);
  });

  // Keep the list current with recordings that arrive while the app is open.
  // Poll the server (it rescans the inbox) and splice in only memos we're not
  // already showing, leaving existing rows — their edits, focus, and playback —
  // untouched.
  var POLL_MS = 5000;

  function sep() {
    var el = document.createElement('div');
    el.className = 'sep';
    return el;
  }

  function merge(html) {
    var incoming = document.createElement('div');
    incoming.innerHTML = html;
    var shown = {};
    content.querySelectorAll('.memo').forEach(function (m) { shown[m.dataset.file] = true; });
    var fresh = [];
    incoming.querySelectorAll('.memo').forEach(function (memo) {
      var file = memo.dataset.file;
      if (!shown[file] && !retired[file]) fresh.push(memo);
    });
    if (!fresh.length) return;
    var grid = content.querySelector('.grid');
    if (!grid) { location.reload(); return; }  // empty page: reload to build the grid + bulk controls
    fresh.forEach(function (memo) {
      grid.appendChild(sep());
      grid.appendChild(memo);
      wire(memo);
    });
  }

  function poll() {
    fetch('/pending')
      .then(function (r) { return r.text(); })
      .then(merge)
      .catch(function () {})
      .then(function () { setTimeout(poll, POLL_MS); });
  }

  setTimeout(poll, POLL_MS);
})();
</script>
</body>
</html>
"""


INDEX_HTML = _PAGE_HEAD + CONTENT_HTML + _PAGE_TAIL


BIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bin — Highdeas</title>
""" + _STYLE + """
</head>
<body>
  <div class="topbar">
    <h1>Bin — {{ memos|length }} item{{ 's' if memos|length != 1 else '' }}</h1>
    <a href="/">← Back to review</a>
  </div>
  <main id="content">
  {% if not memos %}
    <p class="empty">Nothing in the bin. Submitted and deleted memos land here (kept for 90 days).</p>
  {% else %}
  <div class="grid bin">
    <div class="head">Audio</div>
    <div class="head">Transcript</div>
    <div class="head">Name</div>
    <div class="head">Where</div>
    <div class="head">When</div>
    <div class="head"><form method="post" action="/restore-all" onsubmit="return confirm('Restore all {{ memos|length }} item{{ 's' if memos|length != 1 else '' }} to the review page?');"><button class="head-btn restore-all" type="submit">Restore all</button></form></div>
    <div class="head"><form method="post" action="/empty-bin" onsubmit="return confirm('Permanently delete all {{ memos|length }} item{{ 's' if memos|length != 1 else '' }}? This cannot be undone.');"><button class="head-btn empty-bin" type="submit">Empty bin</button></form></div>
    {% for m in memos %}
    {% if not loop.first %}<div class="sep"></div>{% endif %}
    <div class="row">
      <audio controls src="/bin-audio/{{ m.audio_filename }}"></audio>
      <div class="text">{{ m.transcript }}</div>
      <div class="name">{{ m.name or m.audio_filename }}</div>
      <div class="dest">{% if m.status == 'deleted' %}<span title="Trashed" aria-label="Trashed">""" + TRASH_SVG + """</span>{% elif m.route == 'drive' %}<form method="post" action="/open-drive"><button class="destlink" type="submit" title="Sent to Google Drive — open the Drive folder" aria-label="Sent to Google Drive — open the Drive folder">""" + DRIVE_SVG + """</button></form>{% else %}<span title="Sent to Notesnook" aria-label="Sent to Notesnook">""" + NOTESNOOK_SVG + """</span>{% endif %}</div>
      <div class="when">{{ m.processed_at }}</div>
      <div><form method="post" action="/restore/{{ m.audio_filename }}"><button class="binbtn restore" type="submit">Restore</button></form></div>
      <div><form method="post" action="/purge/{{ m.audio_filename }}" onsubmit="return confirm('Permanently delete this recording? This cannot be undone.');"><button class="binbtn purge" type="submit">Delete</button></form></div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
  </main>
</body>
</html>
"""


def _submitted_fields():
    """Editable field values shared by auto-save (/edit) and Submit (/submit)."""
    return {
        "name": request.form["name"],
        "transcript": request.form["transcript"],
        "route": request.form.get("route", "notesnook"),
    }


def create_app(service, inbox_dir, bin_dir, drive_dir=None, open_folder=None):
    app = Flask(__name__)
    open_folder = open_folder or os.startfile  # Windows: opens the folder in Explorer

    @app.get("/")
    def index():
        # No rescan here: the page must paint instantly from what's already stored.
        # The background catch-up transcribes waiting recordings and the /pending
        # poll streams them in, so the first frame never waits on the model.
        return render_template_string(
            INDEX_HTML, memos=service.pending(), incoming=service.has_incoming()
        )

    @app.get("/pending")
    def pending():
        """The review list alone — polled by the open page to pick up recordings
        that arrive after load, so the app stays current without a manual reload."""
        service.refresh()
        return render_template_string(CONTENT_HTML, memos=service.pending())

    @app.get("/audio/<path:filename>")
    def audio(filename):
        return send_from_directory(inbox_dir, filename)

    @app.post("/edit/<path:filename>")
    def edit(filename):
        service.edit(filename, **_submitted_fields())
        return ("", 204)

    @app.post("/submit/<path:filename>")
    def submit(filename):
        service.edit(filename, **_submitted_fields())
        service.submit(filename)
        return ("", 204)

    @app.post("/delete/<path:filename>")
    def delete(filename):
        service.delete(filename)
        return ("", 204)

    @app.get("/bin")
    def bin_view():
        return render_template_string(BIN_HTML, memos=service.binned())

    @app.get("/bin-audio/<path:filename>")
    def bin_audio(filename):
        return send_from_directory(bin_dir, filename)

    @app.post("/restore/<path:filename>")
    def restore(filename):
        service.restore(filename)
        return redirect("/bin")

    @app.post("/purge/<path:filename>")
    def purge(filename):
        service.purge(filename)
        return redirect("/bin")

    @app.post("/empty-bin")
    def empty_bin():
        service.empty_bin()
        return redirect("/bin")

    @app.post("/restore-all")
    def restore_all():
        service.restore_all()
        return redirect("/bin")

    @app.post("/open-drive")
    def open_drive():
        """Open the local Google Drive folder where music memos are filed."""
        if drive_dir:
            open_folder(drive_dir)
        return ("", 204)

    return app
