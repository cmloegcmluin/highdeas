"""Local Flask app: the inbox and bin pages for editing and routing memos.

The pages themselves live in `templates/`, their behaviour in `static/`; this
module is the routes and the glue that binds them to the templates.
"""
from datetime import datetime
from urllib.parse import quote

from flask import Flask, redirect, render_template, request, send_from_directory


def _format_when(iso):
    """A stored ISO timestamp as a scannable "Jul 7, 2:23 PM".

    Both pages show a moment in time — when a memo was recorded, and when it was
    sent or trashed — and both are read by eye against a list of recordings on the
    phone, so neither wants a raw `2026-07-07T14:23:05`. Empty when the timestamp is
    missing, as it is on memos stored before recording times were captured."""
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    return f"{when:%b} {when.day}, {when.hour % 12 or 12}:{when:%M} {when:%p}"


def _submitted_fields():
    """Editable field values shared by auto-save (/edit) and Submit (/submit)."""
    return {
        "name": request.form["name"],
        "transcript": request.form["transcript"],
        "route": request.form.get("route", "notesnook"),
        "asana_parent": request.form.get("asana_parent", ""),
    }


def create_app(service, inbox_dir, bin_dir, open_link=None, asana_parents=()):
    app = Flask(__name__)
    app.jinja_env.filters["when"] = _format_when

    @app.get("/")
    def index():
        # No rescan here: the page must paint instantly from what's already stored.
        # The app's background scan transcribes waiting recordings and the /pending
        # poll streams them in, so the first frame never waits on the model.
        return render_template(
            "inbox.html", memos=service.pending(), incoming=service.has_incoming(),
            asana_parents=asana_parents,
        )

    @app.get("/pending")
    def pending():
        """The inbox rows alone — polled by the open page to pick up recordings
        that arrive after load, so the app stays current without a manual reload.

        It rescans rather than reading the store the background scan keeps current,
        because this is also what the page's "check for new notes now" button calls:
        a scan the user asked for shouldn't wait on the next tick of one they didn't."""
        service.refresh()
        return render_template("rows.html", memos=service.pending(),
                               asana_parents=asana_parents)

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
        """Consolidate the posted notes into one group memo, answering with the row that
        survives and the fields it now holds, so the page can patch itself."""
        try:
            grouped = service.group(request.form.getlist("files"))
        except ValueError as exc:
            return (str(exc), 400)
        return {"target": grouped.audio_filename, "transcript": grouped.transcript,
                "name": grouped.name}

    @app.post("/ungroup/<path:filename>")
    def ungroup(filename):
        """Break a group back into its notes, answering with the inbox as it now reads.

        Several rows come back at once, each into the place the server sorts it, so the
        page takes the whole list rather than guessing where to splice them in."""
        try:
            service.ungroup(filename)
        except ValueError as exc:
            return (str(exc), 400)
        return render_template("rows.html", memos=service.pending(),
                               asana_parents=asana_parents)

    @app.post("/delete/<path:filename>")
    def delete(filename):
        service.delete(filename)
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

    @app.post("/open-drive")
    def open_drive():
        """Open a memo in Google Drive. A link can't choose which Chrome profile
        opens it, so the app launches the browser itself (open_link) at a Drive
        search for the memo — the server builds the URL so only Drive can be opened."""
        if open_link is not None:
            open_link("https://drive.google.com/drive/u/0/search?q=" + quote(request.form.get("q", "")))
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
