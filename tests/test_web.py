from highdeas.store import Memo
from highdeas.web import create_app


class FakeService:
    def __init__(self, pending=(), binned=(), incoming=False):
        self._pending = list(pending)
        self._binned = list(binned)
        self._incoming = incoming
        self.refreshed = 0
        self.edits = []
        self.submitted = []
        self.deleted = []
        self.restored = []
        self.purged = []
        self.emptied = 0
        self.restored_all = 0

    def refresh(self):
        self.refreshed += 1

    def pending(self):
        return self._pending

    def has_incoming(self):
        return self._incoming

    def edit(self, audio_filename, **fields):
        self.edits.append((audio_filename, fields))

    def submit(self, audio_filename):
        self.submitted.append(audio_filename)

    def delete(self, audio_filename):
        self.deleted.append(audio_filename)

    def binned(self):
        return self._binned

    def restore(self, audio_filename):
        self.restored.append(audio_filename)

    def purge(self, audio_filename):
        self.purged.append(audio_filename)

    def empty_bin(self):
        self.emptied += 1

    def restore_all(self):
        self.restored_all += 1


def test_index_lists_pending_without_blocking_on_a_refresh(tmp_path):
    # The window opens instantly: the first paint renders whatever is already in the
    # store and never blocks on a rescan or transcription. New recordings stream in
    # afterwards via the background catch-up and the /pending poll.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hello there")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.get("/")

    assert service.refreshed == 0
    assert resp.status_code == 200
    assert b"a.m4a" in resp.data
    assert b"hello there" in resp.data


def test_index_renders_highdeas_controls(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi", name="Idea")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data

    # Rebranded title + header.
    assert b"<title>Highdeas</title>" in body
    assert b"Highdeas" in body
    # Bulk actions live in their column headers (see the column-header test below).
    assert b"Submit all" in body
    assert b"Trash all" in body
    assert b'href="/bin"' in body
    # Each row carries its filename so JS can target /edit, /submit, /delete.
    assert b'data-file="a.m4a"' in body
    # The "copy transcript into name" control between Transcript and Name.
    assert b'class="copy"' in body


def test_index_trash_all_asks_for_confirmation(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data

    # Trashing everything at once is bulk + easy to fat-finger, so it confirms first.
    assert b"confirm(" in body


def test_index_bulk_controls_sit_in_the_column_headers(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # Moved out of the topbar (no more .bulk container) and into the grid headers,
    # so they line up over the Submit and Trash columns instead of being shoved
    # left by the "Bin →" link.
    assert 'class="bulk"' not in body
    assert 'id="submit-all"' in body and 'id="trash-all"' in body
    assert body.index('grid review headrow') < body.index('id="submit-all"')


def test_index_shows_the_live_item_count(tmp_path):
    service = FakeService(pending=[
        Memo(audio_filename="a.m4a", transcript="one"),
        Memo(audio_filename="b.m4a", transcript="two"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data
    # Same "— N items" the bin shows, in a span the client keeps current as rows change.
    assert b'id="count"' in body
    assert b"2 items" in body


def test_review_rows_are_numbered(tmp_path):
    service = FakeService(pending=[
        Memo(audio_filename="a.m4a", transcript="one"),
        Memo(audio_filename="b.m4a", transcript="two"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data
    assert b'class="num">1</div>' in body
    assert b'class="num">2</div>' in body


def test_index_shows_a_transcribing_hint_while_recordings_await(tmp_path):
    # Opened with an empty store but recordings still waiting in the inbox, the page
    # says they're being transcribed rather than the misleading "Nothing to review".
    service = FakeService(pending=[], incoming=True)
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data

    # The visible empty-state is the transcribing hint, not the idle message.
    assert b"Transcribing your memos" in body
    assert b'<p class="empty">Nothing to review' not in body


def test_index_shows_nothing_to_review_when_the_inbox_is_idle(tmp_path):
    service = FakeService(pending=[], incoming=False)
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data

    assert b'<p class="empty">Nothing to review' in body
    assert b"Transcribing" not in body


def test_index_polls_the_pending_endpoint_to_stay_current(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data

    # The open page keeps itself current by polling the pending fragment.
    assert b"/pending" in body


def test_index_offers_a_manual_refresh_left_of_the_bin_link_even_when_empty(tmp_path):
    # A manual "check for new notes now" button, for pulling in a note the 5s poll
    # hasn't surfaced yet. It sits just left of the Bin link and lives in the topbar,
    # not the memo list, so it's there even while the page is empty and waiting for
    # the very first note.
    service = FakeService(pending=[], incoming=False)
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    assert 'id="refresh"' in body
    assert body.index('id="refresh"') < body.index('href="/bin"')


def test_refresh_button_shows_a_loading_label_while_it_checks(tmp_path):
    service = FakeService(pending=[])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # Clicking Refresh swaps the label to a held "Loading…" then restores it, so a check
    # that surfaces nothing new still visibly reacts (the fetch is otherwise instant).
    assert "Loading" in body


def test_pending_refreshes_and_renders_just_the_memo_rows(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hello there")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.get("/pending")

    # Polling rescans the inbox, same as loading the page does.
    assert service.refreshed == 1
    assert resp.status_code == 200
    # The row markup the client splices in, carrying its filename and transcript.
    assert b'data-file="a.m4a"' in resp.data
    assert b"hello there" in resp.data
    # A bare fragment, not the whole page — no <head>/chrome to re-parse.
    assert b"<title>Highdeas</title>" not in resp.data
    assert b"<!doctype" not in resp.data


def test_pending_surfaces_a_recording_that_arrives_after_the_page_loads(tmp_path):
    from highdeas.service import ReviewService
    from highdeas.store import MemoStore

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"

    class StubTranscriber:
        def transcribe(self, path):
            return "fresh idea"

    service = ReviewService(
        inbox_dir=inbox, store=MemoStore(tmp_path / "memos.db"),
        transcriber=StubTranscriber(), bin_dir=bin_dir,
        clock=lambda: "2026-07-07T00:00", recorded_time=lambda path: "2026-07-07T00:00",
    )
    client = create_app(service, inbox_dir=str(inbox), bin_dir=str(bin_dir)).test_client()

    # The app is open; nothing has been recorded yet.
    assert b"Nothing to review" in client.get("/pending").data

    # A recording lands in the inbox, as the iOS Shortcut + iCloud would deliver it.
    (inbox / "voice-8.m4a").write_bytes(b"NEW-RECORDING")

    # The next poll rescans and surfaces the new memo without a page reload.
    body = client.get("/pending").data
    assert b"fresh idea" in body
    assert b'class="memo"' in body


def test_submit_saves_edits_then_submits_and_returns_204(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/submit/a.m4a", data={
        "name": "My idea", "transcript": "edited text", "route": "drive",
    })

    # Submit flushes the row's current field values before submitting.
    assert service.edits == [
        ("a.m4a", {"name": "My idea", "transcript": "edited text", "route": "drive"})
    ]
    assert service.submitted == ["a.m4a"]
    # 204 (no redirect): the client removes the row optimistically, no page reload.
    assert resp.status_code == 204


def test_submit_defaults_route_to_notesnook_when_toggle_off(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    # An unchecked checkbox toggle submits no "route" field.
    client.post("/submit/a.m4a", data={"name": "X", "transcript": "Y"})

    assert service.edits == [("a.m4a", {"name": "X", "transcript": "Y", "route": "notesnook"})]


def test_edit_route_saves_fields_and_returns_204(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/edit/a.m4a", data={
        "name": "New name", "transcript": "New body", "route": "drive",
    })

    # Auto-save persists the fields without submitting/routing the memo.
    assert service.edits == [
        ("a.m4a", {"name": "New name", "transcript": "New body", "route": "drive"})
    ]
    assert service.submitted == []
    assert resp.status_code == 204


def test_audio_serves_file_from_inbox(tmp_path):
    (tmp_path / "a.m4a").write_bytes(b"AUDIODATA")
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.get("/audio/a.m4a")

    assert resp.status_code == 200
    assert resp.data == b"AUDIODATA"


def test_delete_route_discards_and_returns_204(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/delete/a.m4a")

    assert service.deleted == ["a.m4a"]
    # 204 (no redirect): the trash button removes the row optimistically.
    assert resp.status_code == 204


def test_bin_lists_binned_items(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="b.m4a", name="Old note", transcript="bin body",
             status="deleted", processed_at="2026-07-07T03:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.get("/bin")

    assert resp.status_code == 200
    assert b"Old note" in resp.data
    assert b"bin body" in resp.data
    assert b"b.m4a" in resp.data


def test_bin_shows_destination_icon_instead_of_status_word(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="d.m4a", status="deleted", processed_at="2026-07-07T03:00"),
        Memo(audio_filename="n.m4a", status="processed", route="notesnook", processed_at="2026-07-07T02:00"),
        Memo(audio_filename="g.m4a", status="processed", route="drive", processed_at="2026-07-07T01:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data

    # A destination icon with a label, not the raw status/badge.
    assert b'class="badge"' not in body
    assert b"Trashed" in body
    assert b"Sent to Notesnook" in body
    assert b"Sent to Google Drive" in body


def test_bin_row_offers_restore_and_confirmed_permanent_delete(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data

    assert b'action="/restore/b.m4a"' in body
    assert b'action="/purge/b.m4a"' in body
    assert b"confirm(" in body  # permanent deletion asks first


def test_bin_bulk_controls_sit_in_the_column_headers_and_confirm(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    # Same pattern as the review page: bulk actions live in the grid headers over
    # their columns, not in the topbar.
    assert 'action="/restore-all"' in body
    assert 'action="/empty-bin"' in body
    assert body.index('grid bin headrow') < body.index('action="/restore-all"')
    # Both bulk actions confirm first (restore-all is disruptive, empty-bin destroys).
    assert body.count("confirm(") >= 2


def test_bin_rows_are_numbered(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="a.m4a", status="deleted", processed_at="2026-07-07T03:00"),
        Memo(audio_filename="b.m4a", status="processed", processed_at="2026-07-07T02:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data
    assert b'class="num">1</div>' in body
    assert b'class="num">2</div>' in body


def test_purge_route_permanently_deletes_and_redirects(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/purge/b.m4a")

    assert service.purged == ["b.m4a"]
    assert resp.status_code == 302


def test_empty_bin_route_empties_and_redirects(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/empty-bin")

    assert service.emptied == 1
    assert resp.status_code == 302


def test_restore_all_route_restores_and_redirects(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/restore-all")

    assert service.restored_all == 1
    assert resp.status_code == 302


def test_restore_route_restores_and_redirects(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/restore/b.m4a")

    assert service.restored == ["b.m4a"]
    assert resp.status_code == 302


def test_bin_audio_serves_from_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "b.m4a").write_bytes(b"BINAUDIO")
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(bin_dir)).test_client()

    resp = client.get("/bin-audio/b.m4a")

    assert resp.status_code == 200
    assert resp.data == b"BINAUDIO"


def test_bin_drive_memo_icon_posts_to_open_drive_with_the_memo_name(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="g.m4a", name="My Song", status="processed", route="drive", processed_at="2026-07-07T01:00"),
        Memo(audio_filename="d.m4a", status="deleted", processed_at="2026-07-07T02:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    # Only the Drive memo's icon opens Drive (via /open-drive, which launches Chrome
    # in the chosen profile), carrying the memo name; trashed/Notesnook icons don't.
    assert body.count('action="/open-drive"') == 1
    assert 'name="q" value="My Song"' in body


def test_open_drive_launches_chrome_at_a_drive_search_for_the_memo(tmp_path):
    launched = []
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"),
                        launch_drive=launched.append).test_client()

    resp = client.post("/open-drive", data={"q": "Korok Dance"})

    assert resp.status_code == 204
    assert launched == ["https://drive.google.com/drive/u/0/search?q=Korok%20Dance"]


def test_pages_reserve_the_scrollbar_gutter_so_they_dont_shift(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")],
                          binned=[Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    # Same gutter reserved on both, so flipping between them doesn't shift sideways.
    assert b"scrollbar-gutter: stable" in client.get("/").data
    assert b"scrollbar-gutter: stable" in client.get("/bin").data
