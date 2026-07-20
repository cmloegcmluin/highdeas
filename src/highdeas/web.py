"""Local Flask app: the inbox and bin pages for editing and routing memos.

The pages themselves live in `templates/`, their behaviour in `static/`; this
module is the routes and the glue that binds them to the templates.
"""
import threading
from datetime import datetime
from urllib.parse import quote

from flask import Flask, redirect, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException


def _format_when(iso):
    """A stored ISO timestamp as a scannable "Jul 7, 2:23 PM".

    The inbox reconciles a row against the recordings on the phone, so it wants the
    moment the memo was recorded and not a raw `2026-07-07T14:23:05`. Empty when the
    timestamp is missing, as on memos stored before recording times were captured."""
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    return f"{when:%b} {when.day}, {when.hour % 12 or 12}:{when:%M} {when:%p}"


def _days_since(iso, now):
    """Whole days from a stored ISO timestamp to now, as a bare number.

    The hour a memo was retired says nothing anyone needs. How long it has sat in the
    bin says whether the retention sweep (InboxService.purge_expired, 90 days) is about
    to take it, and that is the one thing a binned row wants of its timestamp. Empty
    when there is none, as on memos retired before processed_at was captured."""
    try:
        since = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    return str(max(0, (now() - since).days))


def _audio_url(memo):
    """Where a memo's recording is played from.

    The cut count rides in the query because a cut rewrites the recording under the same
    filename, and a player handed a URL it is already holding plays what it has rather
    than what is on disk — however the response is labelled, and even in a player built
    fresh. Every render names the recording as it now is, so the row rebuilt by the poll
    can't put back the sound a cut just removed."""
    return f"/audio/{quote(memo.audio_filename)}?cut={memo.cuts or 0}"


def _submitted_fields():
    """Editable field values shared by auto-save (/edit) and Submit (/submit)."""
    return {
        "name": request.form["name"],
        "transcript": request.form["transcript"],
        "route": request.form.get("route", "notesnook"),
        "asana_parent": request.form.get("asana_parent", ""),
        "claude_surface": request.form.get("claude_surface", ""),
        "claude_model": request.form.get("claude_model", ""),
    }


def create_app(service, inbox_dir, bin_dir, open_link=None, asana_parents=(), claude_models=(),
               drive_folder_url="", drive_link_for=None, now=datetime.now, updates=None,
               update_respawn_delay=0.7, rescan=None):
    app = Flask(__name__)
    app.jinja_env.filters["when"] = _format_when
    app.jinja_env.filters["playable"] = _audio_url
    # The bin's ages are read against the wall clock, so the clock is injectable.
    app.jinja_env.filters["days_in_bin"] = lambda iso: _days_since(iso, now)

    def rows_now():
        """Everything it takes to draw the inbox: the memos in it, the recordings still
        becoming memos, and the choices a row's dropdowns offer.

        Three views render these rows — the page, the poll that streams new ones in, and
        the fragment a merge rebuilds — and each of them replaces what it renders whole,
        so anything handed to only one of them goes missing from the other two: a dropdown
        renders empty, and a merge wipes the outlines of the recordings being transcribed
        off the page. One call answers all three."""
        return {"memos": service.pending(), "incoming": service.incoming(),
                "asana_parents": asana_parents, "claude_models": claude_models}

    def _inbox_rows():
        """The inbox as it now reads: a row per memo, an outline per recording still
        becoming one. Grouping and its undo change several rows at once — some leave, some
        come back into the place the server sorts them — so the page takes the whole list
        rather than patching its own guess at it."""
        return render_template("rows.html", **rows_now())

    @app.errorhandler(Exception)
    def unhandled(exc):
        """Answer a failure with the sentence that explains it, not a page of markup.

        The client prints whatever the server says straight into the inbox's notice bar,
        so Flask's default 500 — a whole HTML document — landed there as a paragraph of
        tags with one readable sentence buried in it. Only the app's own failures are
        flattened; a 404 is the browser's business, and keeps the page Flask raises."""
        if isinstance(exc, HTTPException):
            return exc
        app.logger.exception("Unhandled error")
        return (str(exc), 500)

    @app.get("/")
    def index():
        # No rescan here: the page must paint instantly from what's already stored.
        # The app's background scan transcribes waiting recordings and the /pending
        # poll streams them in, so the first frame never waits on the model.
        return render_template("inbox.html", **rows_now())

    @app.get("/pending")
    def pending():
        """The inbox rows alone — polled by the open page to pick up memos that
        arrive after load: recordings the background scan has transcribed, and —
        now that another machine shares this store — memos the peer created,
        edited, or retired.

        It only reads the store and counts what's still incoming; it never runs the
        scan itself. Ingesting, and its slow transcription, belongs to the background
        thread alone, so this poll can't block on the model or stall behind a stuck
        decode — the bug where a peer's freshly-synced memo sat unseen (and same-
        machine notes hung) until the app was restarted. The "check now" button asks
        for an out-of-band scan through /rescan."""
        return _inbox_rows()

    @app.post("/rescan")
    def rescan_now():
        """The manual "check for new notes now" button. The poll no longer scans, so
        this is what makes a user-asked check happen now rather than at the next
        background tick. The scan runs off the request thread (see app._refresh_when_free),
        so the click returns at once and never blocks on transcription; whatever it
        finds streams in through the next poll."""
        if rescan is not None:
            rescan()
        return ("", 204)

    @app.get("/version")
    def version():
        """How far behind origin/main this running app is — the page shows an
        "Update & restart" button when the answer isn't zero. Never cacheable:
        a cached "behind" from before an update would resurrect the button
        forever after every restart."""
        payload = updates.status() if updates is not None else {"behind": 0}
        response = app.make_response(payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/update")
    def update():
        """Fast-forward the checkout and relaunch into it. The pull happens
        before the response (a refusal must reach the user as words), the
        respawn after it (a success must not read as a dead request)."""
        if updates is None:
            return ("This run has no updater.", 501)
        try:
            updates.pull()
        except RuntimeError as exc:
            return (f"Couldn't update: {exc}", 502)
        threading.Timer(update_respawn_delay, updates.respawn).start()
        return ("", 204)

    @app.get("/audio/<path:filename>")
    def audio(filename):
        """A pending memo's recording. Cached freely: a cut rewrites the bytes under the
        same filename, but every page that asks for a cut recording asks by a URL that
        carries the cut count (see _audio_url), so one URL always means one recording."""
        return send_from_directory(inbox_dir, filename)

    @app.post("/edit/<path:filename>")
    def edit(filename):
        service.edit(filename, **_submitted_fields())
        return ("", 204)

    @app.post("/cut/<path:filename>")
    def cut(filename):
        """Take a stretch of seconds out of a memo's recording — what deleting a
        selection dragged over the editor's waveform posts.

        The page cuts the words out of the text it is showing and this cuts the sound
        they were read from, so it answers with what the page can't work out for itself:
        the word timings the cut left behind — every word after it is now that much
        earlier — and the URL to play the shortened recording from."""
        try:
            memo = service.cut(filename, float(request.form["from"]),
                               float(request.form["to"]))
        except ValueError as exc:
            return (str(exc), 400)
        return {"words": memo.word_times, "audio": _audio_url(memo)}

    @app.post("/submit/<path:filename>")
    def submit(filename):
        service.edit(filename, **_submitted_fields())
        try:
            service.submit(filename)
        except Exception as exc:  # noqa: BLE001 — any routing failure must reach the client
            # Routing failed (e.g. Notesnook rejected the key), so the memo is still
            # pending and its audio still in the inbox. Signal the failure instead of a
            # false 204 so the client keeps the row rather than hiding a note that never
            # sent — the "Submit all vanished everything but sent nothing" bug.
            return (f"Submit failed: {exc}", 502)
        return ("", 204)

    @app.post("/reorder")
    def reorder():
        """Persist the order a drag-and-drop left the inbox rows in, top to bottom."""
        service.reorder(request.form.getlist("order"))
        return ("", 204)

    @app.post("/group")
    def group():
        """Consolidate the posted notes into one group memo.

        A group's recording is one the app makes, named by its content, so only the
        server knows the filename it answers to — and Undo has to know which row to walk
        the merge back out of. The optional `name` is the title the page settled on when
        several picked notes were named and it asked which the group should take; absent,
        the server takes the sole name among them, or leaves the group untitled.
        The filename is returned alongside the rows the merge rebuilt."""
        try:
            grouped = service.group(request.form.getlist("files"),
                                    name=request.form.get("name"))
        except ValueError as exc:
            return (str(exc), 400)
        return {"target": grouped.audio_filename, "rows": _inbox_rows()}

    @app.post("/unmerge/<path:filename>")
    def unmerge(filename):
        """Walk back the last merge a group swallowed — what Undo posts.

        The group answers to a new name afterwards, since its recording is rejoined out of
        the members it has left; "" when that merge is what made it and it is gone."""
        try:
            target = service.unmerge(filename)
        except ValueError as exc:
            return (str(exc), 400)
        return {"target": target, "rows": _inbox_rows()}

    @app.post("/ungroup/<path:filename>")
    def ungroup(filename):
        """Break a group all the way back into its notes — what its badge posts."""
        try:
            service.ungroup(filename)
        except ValueError as exc:
            return (str(exc), 400)
        return _inbox_rows()

    @app.post("/delete/<path:filename>")
    def delete(filename):
        service.delete(filename)
        return ("", 204)

    @app.post("/discard/<path:filename>")
    def discard(filename):
        """Throw away a recording that has landed but has no memo yet — what the bin on
        a still-transcribing row posts. Named by the key it will be stored under, not by
        the name it currently sits on disk as: that name is about to change."""
        service.discard(filename)
        return ("", 204)

    @app.get("/bin")
    def bin_view():
        return render_template("bin.html", memos=service.binned())

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

    @app.post("/open-drive/<path:filename>")
    def open_drive(filename):
        """Open the Drive folder this memo's audio actually landed in. A link can't
        choose which Chrome profile opens it, so the app launches the browser itself
        (open_link) — at that memo's own dated subfolder when drive_link_for can
        resolve it, else the static top-level folder, so the icon still does
        something useful rather than nothing. Never a Drive search."""
        memo = service.get(filename)
        if memo is None:
            return ("", 204)
        link = ""
        if memo.drive_subfolder and drive_link_for is not None:
            link = drive_link_for(memo.drive_subfolder)
        link = link or drive_folder_url
        if open_link is not None and link:
            open_link(link)
        return ("", 204)

    @app.post("/open-asana/<path:filename>")
    def open_asana(filename):
        """Open the Asana task a memo became. The client names only the memo; the
        server opens the permalink Asana returned at submit time — never a
        client-supplied URL — via the same chosen-profile launch as Drive links."""
        memo = service.get(filename)
        if open_link is not None and memo is not None and memo.asana_url:
            open_link(memo.asana_url)
        return ("", 204)

    return app
