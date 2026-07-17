import time
from datetime import datetime

from highdeas.store import Memo
from highdeas.transcribe import Transcript
from highdeas.web import create_app


def asset(client, filename):
    """A static asset's source, for the behaviour that lives in CSS and JS."""
    resp = client.get("/static/" + filename)
    assert resp.status_code == 200, filename
    return resp.data.decode()


class FakeService:
    def __init__(self, pending=(), binned=(), incoming=0):
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
        self.reordered = []
        self.grouped = []
        self.group_error = None
        self.ungrouped = []
        self.ungroup_error = None
        self.ungroup_crash = None
        self.unmerged = []
        self.unmerge_error = None

    def refresh(self):
        self.refreshed += 1

    def reorder(self, audio_filenames):
        self.reordered.append(list(audio_filenames))

    def group(self, audio_filenames):
        if self.group_error:
            raise ValueError(self.group_error)
        self.grouped.append(list(audio_filenames))
        # A group's recording is one the app makes, so it answers to a name of its own.
        return Memo(audio_filename=f"group-of-{len(audio_filenames)}.m4a",
                    transcript="- one\n- two", kind="group")

    def ungroup(self, audio_filename):
        if self.ungroup_error:
            raise ValueError(self.ungroup_error)
        if self.ungroup_crash:
            raise self.ungroup_crash
        self.ungrouped.append(audio_filename)
        self._pending = [Memo(audio_filename="a.m4a", transcript="one"),
                         Memo(audio_filename="b.m4a", transcript="two")]

    def unmerge(self, audio_filename):
        if self.unmerge_error:
            raise ValueError(self.unmerge_error)
        self.unmerged.append(audio_filename)
        self._pending = [Memo(audio_filename="a.m4a", transcript="one"),
                         Memo(audio_filename="b.m4a", transcript="two")]
        # The group's recording is rejoined out of what it has left, so it is renamed.
        return f"left-of-{audio_filename}"

    def pending(self):
        return self._pending

    def incoming_count(self):
        return self._incoming

    def get(self, audio_filename):
        for memo in self._pending + self._binned:
            if memo.audio_filename == audio_filename:
                return memo
        return None

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


def test_index_renders_inbox_controls(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi", name="Idea")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data

    # The page is titled "Inbox" (the window chrome already carries the app name).
    assert b"<title>Inbox</title>" in body
    assert b"Inbox" in body
    # Bulk actions live in their column headers (see the column-header test below).
    assert b"Submit all" in body
    assert b"Trash all" in body
    assert b'href="/bin"' in body
    # Each row carries its filename so JS can target /edit, /submit, /delete.
    assert b'data-file="a.m4a"' in body
    # The "move transcript into name" control between Transcript and Name.
    assert b'class="btn icon move"' in body


def test_index_offers_three_destination_icons_with_the_route_checked(tmp_path):
    # The two-way Notesnook⇄Drive toggle can't say "Asana": each row now carries
    # three radio-backed icons and the checked (lit) one is the memo's route.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi", route="asana")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    assert 'class="toggle"' not in body and 'name="route" value="drive"' not in body
    for route in ("notesnook", "drive", "asana"):
        assert f'type="radio" class="route" name="route-a.m4a" value="{route}"' in body
    assert 'value="asana" checked' in body
    assert 'value="notesnook" checked' not in body
    assert "Send to Asana" in body  # each icon labels itself


def test_asana_rows_offer_the_parent_task_dropdown_others_keep_it_hidden(tmp_path):
    # Asana needs one extra decision the other destinations don't: which task the
    # note becomes a subtask of. The dropdown lists the configured parents, keeps
    # the memo's saved choice selected, and hides unless the Asana icon is lit.
    service = FakeService(pending=[
        Memo(audio_filename="a.m4a", transcript="hi", route="asana", asana_parent="222"),
        Memo(audio_filename="b.m4a", transcript="yo", route="notesnook"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"),
                        asana_parents=[("111", "Song ideas"), ("222", "App ideas")]).test_client()

    body = client.get("/").data.decode()

    assert body.count('<option value="111" >Song ideas&nbsp;</option>') == 2
    assert '<option value="222" selected>App ideas&nbsp;</option>' in body
    # a.m4a (asana) shows its dropdown; b.m4a (notesnook) keeps it hidden until picked.
    assert body.count('class="asana-parent"') == 2
    assert body.count('subtask of" hidden>') == 1

    # The polled fragment renders the same rows, so spliced-in memos get it too.
    assert "Song ideas" in client.get("/pending").data.decode()


def test_inbox_js_sends_the_picker_fields_and_toggles_the_dropdown(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # Saves and submits carry the lit icon's route and the chosen parent task…
    assert "input.route:checked" in js
    assert "asana_parent" in js
    # …and the dropdown follows the Asana icon: shown when lit, hidden otherwise. One
    # place puts a row on a destination, so an undone route hides the dropdown too.
    assert "parent.hidden = chosen.route !== 'asana'" in js


def test_unlit_destination_icons_go_greyscale_so_the_lit_one_reads_at_a_glance(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")

    # Opacity alone left the lit icon too close to its dimmed neighbours — especially
    # Notesnook's single dark green, the default route. Unlit icons drop to greyscale
    # too, so the one in brand color is unmistakably the selected one.
    assert "filter: grayscale(1)" in css
    assert "filter: none" in css


def test_asana_dropdown_elides_its_text_before_a_caret_inset_like_the_text(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")
    rule = css.split("select.asana-parent {")[1].split("}")[0]

    # The label must ellipsize before the caret's zone rather than run underneath it…
    assert "text-overflow: ellipsis" in rule
    assert "padding: 4px 20px 4px 6px" in rule
    # …and the caret is ours, not the browser's: the native one hugs the right edge
    # about twice as tight as the text's 6px left inset, and it cannot be moved.
    # Drawing our own chevron at `right 6px` makes the two insets match.
    assert "appearance: none" in rule
    assert "background-position: right 6px center" in rule


def test_asana_dropdown_list_pads_its_right_side_with_a_literal_space(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi", route="asana")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"),
                        asana_parents=[("111", "Song ideas")]).test_client()

    body = client.get("/").data.decode()
    css = asset(client, "app.css")

    # Chromium's open list insets options a hair on the left and not at all on the
    # right, and it IGNORES padding on <option> (styling there is limited to colors
    # and fonts) — the first attempt proved that on screen. A trailing no-break space
    # is literal text, so the popup cannot refuse it; it widens the list by one
    # space's worth and buffers the longest label off the right edge.
    assert "Song ideas&nbsp;</option>" in body
    option_rule = css.split("select.asana-parent option {")[1].split("}")[0]
    assert "padding" not in option_rule  # the ineffective declaration is gone, not kept for show


def test_rows_top_align_so_the_asana_dropdown_grows_downward(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")

    # Centered rows re-center when a cell grows: lighting Asana made its dropdown
    # appear and shoved the icon row (and every neighbour) upward. Top-justified
    # cells keep everything planted; the dropdown just extends the cell downward.
    assert "align-items: start" in css
    assert "align-items: center" not in css.split(".grid {")[1].split("}")[0]


def test_asana_dropdown_list_paints_the_system_palette_not_white(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")

    # Chromium derives the OPEN dropdown list's colors from the select's computed
    # style: a transparent select got a white popup while the options kept the dark
    # theme's light text — white on white. Paint the control and its options with
    # the system palette so the list reads in both themes.
    assert "select.asana-parent option" in css
    assert css.count("background: Canvas") >= 2  # the select and its options
    assert "background: transparent" not in css.split("select.asana-parent")[1].split("}")[0]


def test_the_move_button_points_the_way_the_text_will_travel(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi", name="Idea")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()
    js = asset(client, "inbox.js")

    css = asset(client, "app.css")

    # Which way the chevron points is a fact about what's in the two cells right now, so
    # the row ships one unturned chevron and inbox.js aims it: right while the transcript
    # has something to give, left once it's empty and the name is holding it. Baking a
    # direction into the HTML would let it disagree with the cells after a move.
    assert 'class="btn icon move"' in body
    assert "Move transcript into Name" not in body
    assert "classList.toggle('back', back)" in js
    assert "'Move transcript into Name'" in js and "'Move name into Transcript'" in js
    assert ".move.back svg" in css  # the same chevron, turned around — shared with the editor
    # And with both cells empty there is no move to make, either way, so the chevron is
    # disabled and takes the one fade every spent button in the app takes.
    assert "btn.disabled" in js
    assert "opacity: .4" in css.split(".btn:disabled {")[1].split("}")[0]


def test_inbox_transcript_has_a_copy_to_clipboard_button(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi", name="Idea")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # A button pinned inside the transcript preview puts its text on the clipboard,
    # for pasting the note somewhere the app doesn't route to.
    assert 'data-copy="transcript"' in body
    assert "clipboard.writeText" in asset(client, "inbox.js")


def test_inbox_name_has_a_copy_to_clipboard_button(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi", name="Idea")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # The name field gets the same button, so a title can be lifted out on its own.
    assert 'data-copy="name"' in body


def test_copy_button_confirms_a_copy_and_reports_a_failed_one(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # A copy leaves no trace of its own, so the button holds a check for a beat…
    assert ".clip.copied" in asset(client, "app.css")
    assert "classList.add('copied')" in js
    # …and a clipboard the browser won't hand over says so, rather than looking copied.
    assert "Couldn't copy" in js


def test_index_gives_every_row_a_select_checkbox_under_a_select_all(tmp_path):
    service = FakeService(pending=[
        Memo(audio_filename="a.m4a", transcript="one"),
        Memo(audio_filename="b.m4a", transcript="two"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # A thin leading column of checkboxes, headed by a select-all that ticks them together.
    assert body.count('class="pick"') == 2
    assert 'id="select-all"' in body
    assert body.index('id="select-all"') < body.index('class="pick"')


def test_the_select_column_is_exactly_as_wide_as_the_box_it_holds(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")
    columns = css.split(".grid.inbox")[1].split("grid-template-columns:")[1].split(";")[0].split()
    box = css.split(".pick, #select-all {")[1].split("}")[0]

    # The rule under a column head is drawn across the whole cell, so a column wider than
    # its checkbox underlined empty space either side of it. The column is the checkbox.
    assert "width: 15px" in box
    assert columns[1] == "15px"


def test_a_button_is_faded_only_when_it_is_disabled_and_always_by_the_same_amount(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")

    # The group badge, Submit, the chevron and the bin each carried a fade of their own,
    # so the same button read at one strength in a row and another in the head above it.
    # There is one fade in the app now, and it says the button has nothing to do.
    assert "opacity: .4" in css.split(".btn:disabled {")[1].split("}")[0]
    for rule in (".memo .move, .memo .del {", ".memo .move:disabled {"):
        assert rule not in css, rule
    assert "opacity" not in css.split(".kind svg {")[1].split("}")[0]


def test_the_header_rule_runs_on_one_line_under_every_named_column(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")

    # Each head draws its own rule at its own bottom edge, and a head holding a 34px
    # button is taller than one holding a word — so the underline stepped down under the
    # bulk buttons. Stretch every head to the row's height and the rule is one line again.
    assert "align-items: stretch" in css.split(".grid.headrow {")[1].split("}")[0]


def test_the_waveform_already_played_is_the_colour_of_the_word_being_spoken(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")
    js = asset(client, "editor.js")

    # The stretch of waveform behind the playhead and the word lit up in the text are the
    # same sound, so they are the same yellow — and the same declaration, read out of the
    # stylesheet, so the two can never drift apart.
    assert "--spoken: #facc15" in css.split(":root {")[1].split("}")[0]
    assert "var(--spoken)" in css.split("::highlight(spoken) {")[1].split("}")[0]
    assert "getPropertyValue('--spoken')" in js
    assert "#3b82f6" not in js


def test_a_head_that_holds_a_control_is_as_bright_as_the_column_under_it(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")

    # The head cells were dimmed as a whole, which took the select-all box and the bulk
    # buttons down with them — and no child can climb back out of its parent's opacity,
    # so a select-all read fainter than the very boxes it ticks. Only a head that is
    # nothing but a word is a label, and only a label is dimmed.
    assert "opacity" not in css.split(".grid .head {")[1].split("}")[0]
    assert "opacity: .55" in css.split(".grid .head:not(:has(button, input)) {")[1].split("}")[0]


def test_index_badges_a_group_row_and_leaves_a_plain_note_unbadged(tmp_path):
    service = FakeService(pending=[
        Memo(audio_filename="a.m4a", transcript="a loose note"),
        Memo(audio_filename="g.m4a", transcript="- one\n- two", kind="group"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # A thin column telling groups apart from loose notes, at a glance and to the client.
    assert body.count('class="kind"') == 2
    assert body.count('data-kind="group"') == 1
    assert body.count('data-kind="note"') == 1


def test_a_group_row_wears_its_badge_as_the_button_that_breaks_it_up(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="g.m4a", transcript="- one", kind="group")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()
    css = asset(client, "app.css")

    # The badge carries both faces: whole, and coming apart. Hovering swaps them, so the
    # click reads as what it does before it is made. It is a button that acts on its row,
    # so it takes the same square as the button heading its column — and .danger, because
    # what it does is take something apart: reaching for it reddens, as the bins do.
    assert 'class="btn icon danger group-badge ungroup"' in body
    assert 'class="ic-whole"' in body and 'class="ic-broken"' in body
    assert ".ungroup .ic-broken { display: none" in css
    assert ".ungroup:hover .ic-whole" in css
    assert "post('/ungroup/'" in asset(client, "inbox.js")


def test_the_bin_badge_only_reports_a_group_and_cannot_break_it_up(tmp_path):
    # Nothing in the bin is in the inbox to break apart, so its badge is not a button.
    service = FakeService(binned=[
        Memo(audio_filename="g.m4a", status="deleted", kind="group", processed_at="2026-07-10T03:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    assert 'class="group-badge"' in body
    assert "ungroup" not in body


def test_index_puts_the_group_button_over_the_group_column_and_starts_it_disabled(tmp_path):
    service = FakeService(pending=[
        Memo(audio_filename="a.m4a", transcript="one"),
        Memo(audio_filename="b.m4a", transcript="two"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # Like Submit all and Trash all, it sits in the header of the column it acts on —
    # here the group column, beside the checkboxes that feed it — and wears their chrome,
    # rather than being the one bulk action drawn as a bare glyph.
    assert 'id="group-picked" class="btn icon"' in body
    assert body.index('id="select-all"') < body.index('id="group-picked"') < body.index(">Audio<")
    # Nothing is ticked on load, so there is nothing to group yet.
    opening_tag = body[body.index('id="group-picked"'):]
    assert "disabled" in opening_tag[:opening_tag.index(">")]


def test_the_inbox_posts_its_ticked_notes_to_the_group_endpoint(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # Pins the seam between the page and the route: /group reads request.form.getlist("files").
    assert "post('/group'" in js
    assert "append('files'" in js
    assert "getElementById('select-all')" in js


def test_grouping_hands_over_every_unsaved_edit_before_the_merge(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # The server folds the notes as it holds them. A name typed a moment before the click
    # is still behind the auto-save's timer, and the bullet it belongs to came out bare —
    # "learn to play it" rather than "Theremin lessons: learn to play it".
    body = js.split("function mergeFiles")[1].split("\n  function ")[0]
    assert body.index("flushEdits(picks)") < body.index("post('/group'")


def test_a_note_dragged_onto_a_group_joins_it_instead_of_reordering(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")
    css = asset(client, "app.css")

    # The row drag already reorders; dropping on a group's badge cell means "join this
    # group" instead. Only that cell accepts the drop, and only from a loose note.
    assert "dropTarget" in js
    assert "'dropping'" in js
    assert ".kind.dropping" in css


def test_index_trash_all_asks_for_confirmation(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    # Trashing everything at once is bulk + easy to fat-finger, so it asks first.
    assert "window.HighdeasAsk(" in asset(client, "inbox.js")


def test_pages_load_the_shared_stylesheet_and_the_inbox_loads_its_scripts(tmp_path):
    # The behaviour asserted below lives in these files, so every page has to pull
    # them in — a page that forgets one looks fine in the markup and does nothing.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")],
                          binned=[Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    index = client.get("/").data.decode()
    assert "/static/app.css" in index
    assert "/static/inbox.js" in index
    assert "/static/editor.js" in index
    assert "/static/app.css" in client.get("/bin").data.decode()


def test_the_app_asks_before_it_destroys_and_never_in_the_browsers_voice(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")],
                          binned=[Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    index = client.get("/").data.decode()
    binned = client.get("/bin").data.decode()

    # window.confirm() stamps "127.0.0.1:<port> says" over the top of whatever it is asked
    # to ask, and nothing the page writes can take that line off — so its own question
    # arrives in a stranger's voice. Every page carries our dialog and asks through it.
    assert "confirm(" not in index and "confirm(" not in binned
    assert "confirm(" not in asset(client, "inbox.js")
    assert 'id="ask"' in index and 'id="ask"' in binned
    assert "/static/ask.js" in index and "/static/ask.js" in binned
    assert "showModal()" in asset(client, "ask.js")

    # A form that must ask first carries the question, and says when it cannot be undone.
    assert 'data-confirm="Restore all 1 item to the inbox?"' in binned
    assert 'data-confirm="Permanently delete this recording? This cannot be undone." data-danger' in binned
    assert "window.HighdeasAsk" in asset(client, "inbox.js")


def test_the_notice_can_be_dismissed_by_the_reader_who_has_read_it(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()
    js = asset(client, "inbox.js")

    # It reports something that has already happened, and no later action need clear it,
    # so the only way out of it was to reload the page. Now it carries its own way out.
    assert 'id="notice-text"' in body
    assert 'id="notice-close"' in body
    assert "getElementById('notice-close')" in js
    assert "clearNotice" in js.split("getElementById('notice-close')")[1][:120]


def test_a_failing_route_answers_with_a_sentence_and_never_a_page_of_html(tmp_path):
    # The page prints whatever the server says into its notice bar, so Flask's default
    # 500 — a whole HTML document — arrived there as a paragraph of markup, out of which
    # the reader had to pick the one sentence that meant anything. It says the sentence.
    service = FakeService(pending=[Memo(audio_filename="g.m4a", kind="group")])
    service.ungroup_crash = RuntimeError("the disc is on fire")
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/ungroup/g.m4a")

    assert resp.status_code == 500
    assert resp.data.decode() == "the disc is on fire"
    assert "<!doctype" not in resp.data.decode().lower()


def test_a_request_for_a_page_that_is_not_there_still_answers_as_a_404(tmp_path):
    # Only the app's own failures are flattened to a sentence. The browser's own errors
    # keep the status and the page Flask raises for them.
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    assert client.get("/no-such-page").status_code == 404


def test_the_inbox_offers_undo_and_redo_buttons_that_start_with_nowhere_to_go(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # They lead the topbar, left of the two controls that navigate rather than act, and
    # each names its shortcut where the pointer will find it.
    assert body.index('id="undo"') < body.index('id="redo"') < body.index('id="refresh"')
    assert 'title="Undo (Ctrl+Z)"' in body
    assert 'title="Redo (Ctrl+Shift+Z)"' in body
    # Nothing has been done yet, so both start disabled — and a disabled button
    # has to look it.
    assert 'aria-label="Undo" disabled>' in body
    assert 'aria-label="Redo" disabled>' in body
    assert ".btn:disabled" in asset(client, "app.css")


def test_the_topbar_acts_through_icons_and_names_them_for_a_screen_reader(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # Undo, Redo and Refresh carry a glyph, not a word, so the topbar reads as three
    # things to press rather than a sentence. An icon has no text to name it, so each
    # one says who it is in its title and its accessible label.
    for control in ("undo", "redo", "refresh"):
        assert f'id="{control}" class="btn topbtn icon"' in body
    assert ">Undo<" not in body and ">Redo<" not in body and ">Refresh<" not in body
    assert body.count('aria-label="Undo"') == 1
    assert body.count('aria-label="Redo"') == 1
    assert body.count('aria-label="Refresh"') == 1
    assert body.count("<svg") >= 3


def test_every_button_that_is_only_a_glyph_is_the_same_square(tmp_path):
    service = FakeService(
        pending=[Memo(audio_filename="a.m4a", transcript="hi")],
        binned=[Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00")],
    )
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    inbox = client.get("/").data.decode()
    binned = client.get("/bin").data.decode()
    css = asset(client, "app.css")

    # The topbar's three, the column heads, the row's chevron, its Submit and its bin, and
    # the bin page's Restore and Delete are all a picture and nothing else, so they are all
    # one square — the eye learns the target once and finds it everywhere.
    square = css.split(".icon {")[1].split("}")[0]
    assert "width: 34px" in square and "height: 34px" in square
    for control in ("undo", "redo", "refresh"):
        assert f'id="{control}" class="btn topbtn icon"' in inbox
    assert 'id="group-picked" class="btn icon"' in inbox
    assert 'id="submit-all" class="btn icon"' in inbox
    assert 'id="trash-all" class="btn icon danger"' in inbox
    assert 'class="btn icon move"' in inbox
    assert 'class="btn icon go"' in inbox
    assert 'class="btn icon del danger"' in inbox
    assert 'id="restore-all" class="btn icon" title="Restore all"' in binned
    assert 'id="empty-bin" class="btn icon danger" title="Empty bin"' in binned
    assert 'class="btn icon restore" title="Restore"' in binned
    assert 'class="btn icon purge danger" title="Delete"' in binned
    # No glyph asks for a size of its own any more: .icon svg draws every one of them, and
    # a head no longer needs a chrome of its own to sit in.
    assert ".topbtn svg" not in css and ".head-btn" not in css

    # A label swap is no way to say "checking" once the label is a picture: spin it.
    js = asset(client, "inbox.js")
    css = asset(client, "app.css")
    assert "Loading" not in js
    assert "classList.add('spinning')" in js
    assert "#refresh.spinning svg" in css and "@keyframes spin" in css


def test_the_undo_stack_is_loaded_before_the_page_that_records_into_it(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # inbox.js reaches for the stack as it wires each row, so it has to already be there.
    assert body.index("/static/history.js") < body.index("/static/inbox.js")
    assert "window.HighdeasHistory" in asset(client, "history.js")
    # The bin is a read-only list of what has already left the inbox: nothing to undo.
    assert "/static/history.js" not in client.get("/bin").data.decode()


def test_undo_answers_the_keyboard_except_where_the_browser_has_a_better_one(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "history.js")

    # Ctrl+Z walks back; Ctrl+Shift+Z and Ctrl+Y walk forward again.
    assert "'z'" in js and "'y'" in js and "event.shiftKey" in js
    # But a focused field keeps its own typing history, and the caret is standing in it.
    # The open editor is off limits too: it holds a copy of the note, and walking the
    # row out from under it would leave the two disagreeing.
    assert "[contenteditable]" in js
    assert "dialog[open]" in js


def test_a_walked_back_step_blinks_the_button_it_belongs_to(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "history.js")
    css = asset(client, "app.css")

    # Ctrl+Z leaves no mark on the page — the row it walked back may be scrolled out of
    # sight. Blink the button the shortcut stands for, so the key and the click read as
    # the same action, and a held key blinks once per step rather than sticking lit.
    assert "flash(undoBtn)" in js and "flash(redoBtn)" in js
    assert "offsetWidth" in js  # the reflow that lets the animation restart mid-flight
    # Nothing to walk back, nothing to blink: the step has to have found an action.
    assert "if (!action) return false" in js
    assert ".topbtn.flash" in css and "@keyframes press" in css


def test_the_inbox_records_the_four_actions_that_can_be_walked_back(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # Sending text across the arrow, rebinding a note to a destination, dropping a row
    # somewhere new, and folding notes into a group are the four things that change the
    # list without the user typing into it — the four, therefore, that have nothing else
    # to walk them back.
    assert js.count("undoStack.did(") == 4
    # Undoing has to persist, or the row reads back the way it was before the undo.
    assert "flush(memo)" in js.split("\n  function apply(")[1].split("\n  function ")[0]
    assert "saveOrder()" in js.split("\n  function applyOrder(")[1].split("\n  function ")[0]


def test_a_step_names_the_row_it_touched_rather_than_holding_it(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # Grouping and its undo take the whole list back from the server, so the row elements a
    # step was recorded against are gone by the time it is walked back. A step holding one
    # would write into a row the page has thrown away; naming it and looking it up when the
    # step runs is what lets grouping join the stack at all.
    assert "function rowFor(file)" in js
    for step in ("function applyTo(file", "function bindTo(file"):
        assert step in js
    assert "undoStack.did({\n      undo: function () { applyTo(file, was); }" in js


def test_grouping_is_walked_back_one_merge_at_a_time(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # Undo posts the last merge back out of the group and redo folds the same notes in
    # again. It walks back one merge, not the whole group: a note dragged into a group
    # that already existed must come back out without dissolving what it joined.
    step = js.split("function groupFiles")[1].split("\n  function ")[0]
    assert "undoStack.did(" in step
    assert "unmergeRow(groupNames[id])" in step and "mergeFiles(folding())" in step
    assert "post('/unmerge/'" in js


def test_a_group_is_read_from_a_cell_because_its_name_keeps_changing(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # A group's recording is its members' joined, and the file is named by what is in it,
    # so every merge and every walk back renames the group. A step that remembered the name
    # it saw would post it back long after the group had grown out of it.
    cell = js.split("function groupFiles")[1].split("\n  function ")[0]
    assert "groupNames[id] = target" in cell
    assert "delete groupNames[id]" in cell  # the merge that made it took the group with it
    assert "function cellOf(files)" in js   # a merge into a group names it among its files


def test_an_action_that_takes_a_row_out_for_good_empties_the_stack(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # A submitted note is in Notesnook and a trashed one is in the bin; neither comes back
    # on Ctrl+Z, and a stack that stepped over them to walk back some older, unrelated
    # action would be worse than one that admits it has nothing left to offer. Breaking a
    # group all the way up is a walk back of its own, past however many steps the stack
    # still holds for it. Grouping no longer empties the stack — it joins it.
    assert js.count("undoStack.clear()") == 2
    assert "undoStack.clear()" in js.split("function removeRow")[1].split("\n  function ")[0]
    assert "undoStack.clear()" in js.split("function ungroupRow")[1].split("\n  function ")[0]


def test_a_rows_transcript_is_a_preview_that_opens_the_editor(tmp_path):
    # Not a draggable textarea any more: the row shows the transcript, and clicking
    # it hands the note to the editor dialog, where it has room to be worked on.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hello there")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    assert "<textarea" not in body
    assert 'class="transcript"' in body
    assert ">hello there</div>" in body


def test_a_column_of_submits_and_its_bulk_head_wear_the_same_glyph(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()
    css = asset(client, "app.css")

    # A bulk button and the row buttons beneath it do the same thing, so they say it the
    # same way — one paper plane over a column of them, one bin over a column of bins.
    assert 'id="submit-all" class="btn icon" title="Submit all" aria-label="Submit all"' in body
    assert 'id="trash-all" class="btn icon danger" title="Trash all" aria-label="Trash all"' in body
    # The row's plane rests in a span it swaps for a spinner mid-send; at rest it is the
    # same glyph the head wears.
    assert 'class="btn icon go" title="Submit" aria-label="Submit"><span class="ic-send"><svg' in body
    assert ">Submit all<" not in body and ">Trash all<" not in body
    assert ">Submit</button>" not in body
    # One rule draws every glyph in the app, so a head and its column can't drift.
    assert ".icon svg {" in css
    # The editor's button still speaks: it is the only one left with a word to say.
    assert ">Done</button>" in body


def test_a_rows_submit_wears_the_outline_chrome_its_bulk_head_wears(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()
    css = asset(client, "app.css")

    # A solid blue Submit on every row shouted the same thing down the whole list. It
    # takes the app's one outline chrome, and colors on hover like the head above it.
    assert 'class="btn icon go" title="Submit" aria-label="Submit"' in body
    # One filled button is left in the app: the dialog's single way out.
    filled = [rule for rule in css.split("}") if "background: #3b82f6" in rule]
    assert len(filled) == 1 and ".editor-done" in filled[0], filled


def test_the_action_columns_hand_their_spare_width_to_the_scrubber(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")
    inbox_cols = css.split(".grid.inbox")[1].split(";")[0]
    bin_cols = css.split(".grid.bin")[1].split(";")[0]

    # A 94px column around a 34px glyph starved the audio player beside it — the scrubber
    # was a hairline you could barely take hold of. Each page's two action columns narrow
    # to the buttons they hold, and the width they give up goes to the audio.
    assert "94px" not in inbox_cols and "94px" not in bin_cols
    assert inbox_cols.rstrip().endswith("34px 34px")
    assert bin_cols.rstrip().endswith("34px 34px")
    # The same scrubber on both pages, as wide as the two grids can both afford.
    assert "348px" in inbox_cols and "348px" in bin_cols


def test_every_inbox_row_is_the_same_height_whether_or_not_it_has_a_transcript(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    preview = asset(client, "app.css").split(".memo .transcript {")[1].split("}")[0]

    # A min-height grew the row from one line to three as the note filled up, so the
    # list jumped every time text crossed the arrow. The preview is a fixed three-line
    # box now: the whole note is one click away in the editor, so it never needs more.
    assert "min-height" not in preview
    assert "height: calc(3 * 1.45em" in preview
    assert "-webkit-line-clamp: 3" in preview


def test_a_row_carries_its_word_timings_so_the_editor_can_highlight_along(tmp_path):
    # The timings ride along on the row, so opening the editor costs no extra request.
    service = FakeService(pending=[
        Memo(audio_filename="a.m4a", transcript="hi there", word_times='[[0.5,"hi"],[0.9,"there"]]'),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    assert "[[0.5,&#34;hi&#34;],[0.9,&#34;there&#34;]]" in body  # escaped into data-words


def test_index_renders_the_editor_dialog_once_for_every_row(tmp_path):
    service = FakeService(pending=[
        Memo(audio_filename="a.m4a", transcript="one"),
        Memo(audio_filename="b.m4a", transcript="two"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # One dialog serves every row — the client fills it in on open.
    assert body.count('id="editor"') == 1
    assert 'id="editor-name"' in body       # the whole title, on one line
    assert 'id="editor-wave"' in body       # the scrubbable waveform
    assert 'id="editor-body"' in body       # the big rich-text body
    assert 'contenteditable="true"' in body
    assert 'data-cmd="insertUnorderedList"' in body
    assert 'data-cmd="insertOrderedList"' in body


def test_editor_offers_copy_buttons_for_both_fields_and_the_move_chevron(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # The editor grows the same three controls the inbox row has: a copy button pinned in
    # each field, and the auto-flipping chevron that moves the text from one to the other.
    assert 'class="clip editor-clip" data-copy="name"' in body
    assert 'class="clip editor-clip" data-copy="transcript"' in body
    assert 'class="btn icon move editor-move"' in body


def test_editor_js_copies_a_field_and_flips_the_move_by_which_field_holds_text(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "editor.js")

    # Copy puts the field on the clipboard and holds a check for a beat; the move mirrors
    # the row's chevron — aimed by which field currently holds the text, not by memory.
    assert "navigator.clipboard.writeText" in js
    assert "classList.add('copied')" in js
    assert "movesBack" in js
    assert "'Move transcript into Name'" in js and "'Move name into Transcript'" in js


def test_editor_clip_shares_the_rows_clipboard_chrome(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")

    # The clip chrome is written once and shared by both surfaces — the editor only says
    # where its own copy of the button is pinned.
    assert ".clip.copied" in css
    assert ".editor-clip" in css


def test_the_editor_dialog_stays_hidden_until_something_opens_it(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")
    # The browser hides a closed <dialog> with UA `display: none`, but ANY author
    # `display` on the element overrides that — an unconditional `display: flex`
    # painted the empty editor at the bottom of the page on every load. The flex
    # layout may apply only while the dialog is actually open.
    editor_rule = css.split(".editor {", 1)[1].split("}", 1)[0]
    assert "display" not in editor_rule
    assert ".editor[open]" in css


def test_the_editor_autoplays_and_highlights_without_selecting(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    script = asset(client, "editor.js")
    # It starts playing on open — the click that opened the dialog is the gesture
    # autoplay needs.
    assert "audio.play()" in script
    # And it lights the spoken word with the Custom Highlight API, which paints a
    # range without touching the selection or the caret.
    assert "CSS.highlights.set('spoken'" in script
    assert "::highlight(spoken)" in asset(client, "app.css")


def test_the_editor_saves_on_the_way_out_rather_than_after_it_has_closed(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    script = asset(client, "editor.js")
    # A dialog's `close` event is dispatched from a queued task, so a final save hung
    # on it loses an edit made in the moment before closing. Both exits flush first:
    # the buttons through closeEditor, and Esc through the `cancel` it fires on the
    # way to closing.
    assert "function closeEditor" in script
    assert "dialog.addEventListener('cancel', teardown)" in script


def test_a_click_off_the_editor_closes_it(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    script = asset(client, "editor.js")
    # Clicking the dim margin around the dialog closes it, the same as Done — a modal you
    # can dismiss by clicking off it, not only through the × in its corner. The backdrop
    # belongs to the <dialog>, so its clicks land on the element itself; the dialog is
    # measured so a click off it is told from one in the padding or a gap between rows,
    # which must not close.
    assert "dialog.addEventListener('click'" in script
    assert "getBoundingClientRect" in script
    # And it leaves the same way the buttons do, so the last edit is still flushed.
    handler = script.split("dialog.addEventListener('click'", 1)[1][:400]
    assert "closeEditor" in handler


def test_the_editor_is_not_rendered_on_the_bin_page(tmp_path):
    # Binned notes are read-only; nothing there to edit.
    service = FakeService(binned=[Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    assert 'id="editor"' not in client.get("/bin").data.decode()


def test_index_bulk_controls_sit_in_the_column_headers(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # Moved out of the topbar (no more .bulk container) and into the grid headers,
    # so they line up over the Submit and Trash columns instead of being shoved
    # left by the "Bin →" control.
    assert 'class="bulk"' not in body
    assert 'id="submit-all"' in body and 'id="trash-all"' in body
    assert body.index('grid inbox headrow') < body.index('id="submit-all"')


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


def test_each_inbox_row_is_dragged_by_a_grip_not_by_a_number(tmp_path):
    service = FakeService(pending=[
        Memo(audio_filename="a.m4a", transcript="one"),
        Memo(audio_filename="b.m4a", transcript="two"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # A grip is what a draggable row wears. A row number was a poor stand-in: nothing
    # about a number says "pick me up", and numbering a list you reorder by hand only
    # ever names where a row is sitting this second.
    assert 'class="grip" draggable="true"' in body
    assert 'class="num"' not in body
    # A drop posts the whole on-screen order back.
    assert "/reorder" in asset(client, "inbox.js")


def test_dragging_a_row_carries_a_picture_of_the_whole_row(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="one")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    # A .memo is display:contents and has no box, so the browser would photograph the
    # grip alone. The client paints the row into an off-screen clone and hands that over
    # as the drag image, so what you're moving is visible while you move it.
    assert "setDragImage" in asset(client, "inbox.js")
    assert ".drag-ghost" in asset(client, "app.css")


def test_the_drag_picture_leaves_the_dragged_rows_destination_where_it_was(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")

    # The picture clones the row's cells, its destination radios among them. Radios share
    # a group by name across the whole document, so putting the clone's checked one into
    # the page unchecked the row's own: the lit icon went dark the moment the row was
    # picked up, and every save after it read no route at all. The picture is a picture,
    # so its inputs answer to no name.
    ghost = js.split("function dragImage")[1].split("\n  }")[0]
    assert ghost.index("removeAttribute('name')") < ghost.index("document.body.appendChild(ghost)")


def test_inbox_row_shows_when_the_recording_was_made(tmp_path):
    # Reconciling a row against the recordings on the phone means knowing when it was
    # recorded, so each row carries its recording time under a "Recorded" header.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", recorded_at="2026-07-07T14:23:05")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    assert "Recorded" in body
    assert "Jul 7, 2:23 PM" in body


def test_an_inbox_row_leads_with_grip_select_group_then_recorded(tmp_path):
    # The three controls that act on a row come first, narrow and in reach of each
    # other; the recording time follows as the first thing the row has to *say*.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", recorded_at="2026-07-07T14:23:05")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    row = body.index('class="memo"')
    order = ['class="grip"', 'class="pick"', 'class="kind"', 'class="when"']
    assert [body.index(cell, row) for cell in order] == sorted(body.index(cell, row) for cell in order)
    # The headers run the same way, so each control sits under the head that presses it.
    heads = ['id="select-all"', 'id="group-picked"', "Recorded"]
    assert [body.index(head) for head in heads] == sorted(body.index(head) for head in heads)


def test_a_column_with_no_header_carries_no_underline(tmp_path):
    # The header row's rule marks off the columns that are named. Under the grip and the
    # move chevron there is nothing to name, so the rule breaks rather than underlining
    # a heading that isn't there.
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")

    assert ".grid .head:empty" in css
    rule = css.split(".grid .head:empty {")[1].split("}")[0]
    assert "border-bottom: none" in rule


def test_inbox_row_leaves_the_timestamp_blank_when_the_recording_time_is_unknown(tmp_path):
    # Memos stored before recording times were captured carry no recorded_at; the row
    # still renders, with an empty cell rather than a crash or a bogus date.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", recorded_at="")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    assert '<div class="when"></div>' in body


def test_index_shows_a_transcribing_hint_while_recordings_await(tmp_path):
    # Opened with an empty store but recordings still waiting in the inbox, the page
    # says they're being transcribed rather than the misleading "Your inbox is empty".
    service = FakeService(pending=[], incoming=1)
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data

    # The visible empty-state is the transcribing hint, not the idle message.
    assert b"Transcribing 1 new recording" in body
    assert b'<p class="empty">Your inbox is empty' not in body


def test_a_landed_recording_is_visible_before_transcription_finishes(tmp_path):
    # The nerve-wracking window: the phone's row disappears on delivery, but the
    # desktop showed nothing until transcription finished — for cold starts, model
    # warm-up plus the take itself. The strip names the landed recordings the moment
    # they exist, above the rows, and it rides the /pending fragment so the open
    # page picks it up on the next poll.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")], incoming=2)
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    page = client.get("/").data.decode()
    fragment = client.get("/pending").data.decode()

    for body in (page, fragment):
        assert '<p class="transcribing">Transcribing 2 new recordings…</p>' in body
    assert page.index('class="transcribing"') < page.index('grid inbox body')


def test_index_shows_empty_state_when_the_inbox_is_idle(tmp_path):
    service = FakeService(pending=[], incoming=False)
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data

    assert b'<p class="empty">Your inbox is empty' in body
    assert b"Transcribing" not in body


def test_index_polls_the_pending_endpoint_to_stay_current(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    # The open page keeps itself current by polling the pending fragment.
    assert "/pending" in asset(client, "inbox.js")


def test_index_offers_a_manual_refresh_left_of_the_bin_button_even_when_empty(tmp_path):
    # A manual "check for new notes now" button, for pulling in a note the 5s poll
    # hasn't surfaced yet. It sits just left of the Bin button and lives in the topbar,
    # not the memo list, so it's there even while the page is empty and waiting for
    # the very first note.
    service = FakeService(pending=[], incoming=False)
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    assert 'id="refresh"' in body
    assert body.index('id="refresh"') < body.index('href="/bin"')


def test_topbar_controls_are_buttons_not_text_links(tmp_path):
    # Bare blue text reading "Refresh  Bin →" ran together as one phrase — "refresh
    # bin". Both controls wear the same bordered button chrome so they read as two
    # separate things to click.
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()
    assert 'id="refresh" class="btn topbtn icon"' in body
    assert '<a class="btn topbtn" href="/bin">' in body

    css = asset(client, "app.css")
    # The chrome itself: a bordered control, not an undecorated link.
    assert "border: 1px solid" in css.split(".btn {")[1].split("}")[0]
    # The shared base must precede every variant that resizes it: .btn's `font: inherit`
    # shorthand resets font-size, so at equal specificity a later .btn silently undoes
    # .topbtn's smaller type — the topbar buttons render at 16px instead of 13.6px.
    for variant in (".topbtn {", ".play {", ".tool {"):
        assert css.index(".btn {") < css.index(variant), variant


def test_refresh_button_spins_and_locks_for_a_held_beat_while_it_checks(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "inbox.js")
    # A local check is near-instant, so the spin is held for a beat: a press that
    # surfaces nothing new still visibly reacts, and can't double-fire while it runs.
    assert "REFRESH_FEEDBACK_MS" in js
    assert "refreshBtn.disabled = true" in js
    # The poll no longer scans, so the button kicks one itself via /rescan before it
    # pulls in whatever's ready.
    assert "/rescan" in js


def test_pending_paints_stored_rows_without_scanning_on_the_request_thread(tmp_path):
    # The 5s poll only reads the store; it never runs the scan (and its slow
    # transcription) on the request thread. That is what keeps a stuck decode or a
    # cold model from freezing the page — and from hiding a peer's memo that just
    # synced in, the delay that once needed an app restart to clear.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hello there")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.get("/pending")

    assert service.refreshed == 0
    assert resp.status_code == 200
    # The row markup the client splices in, carrying its filename and transcript.
    assert b'data-file="a.m4a"' in resp.data
    assert b"hello there" in resp.data
    # A bare fragment, not the whole page — no <head>/chrome to re-parse.
    assert b"<title>Inbox</title>" not in resp.data
    assert b"<!doctype" not in resp.data


def test_pending_surfaces_a_recording_once_the_background_scan_takes_it_in(tmp_path):
    from highdeas.service import InboxService
    from highdeas.store import MemoStore

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"

    class StubTranscriber:
        def transcribe(self, path):
            return Transcript("fresh idea")

    service = InboxService(
        inbox_dir=inbox, store=MemoStore(tmp_path / "memos.db"),
        transcriber=StubTranscriber(), bin_dir=bin_dir,
        clock=lambda: "2026-07-07T00:00", recorded_time=lambda path: "2026-07-07T00:00",
    )
    client = create_app(service, inbox_dir=str(inbox), bin_dir=str(bin_dir)).test_client()

    # The app is open; nothing has been recorded yet.
    assert b"Your inbox is empty" in client.get("/pending").data

    # A recording lands in the inbox, as the iOS Shortcut + iCloud would deliver it.
    (inbox / "voice-8.m4a").write_bytes(b"NEW-RECORDING")

    # The poll paints only what's stored, so it does NOT transcribe on the request
    # thread — the recording is still merely "incoming" until a scan takes it in.
    assert b"fresh idea" not in client.get("/pending").data

    # The background scan (here, an explicit refresh) transcribes it; the next poll
    # surfaces the memo without a page reload.
    service.refresh()
    body = client.get("/pending").data
    assert b"fresh idea" in body
    assert b'class="memo"' in body


def test_rescan_kicks_a_scan_off_the_request_thread_and_returns_at_once(tmp_path):
    # The manual "check for new notes now" button. The poll no longer scans, so this
    # is what makes a user-asked check happen now rather than at the next background
    # tick — and it hands the scan to another thread so the click returns immediately,
    # never blocking on transcription. Whatever the scan finds streams in via the poll.
    kicks = []
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"),
                        rescan=lambda: kicks.append(1)).test_client()

    resp = client.post("/rescan")

    assert resp.status_code == 204
    assert kicks == [1]


def test_the_poll_never_kicks_a_scan_only_the_manual_check_does(tmp_path):
    # Guard the decoupling: an automatic /pending poll must not trigger a scan, or the
    # freeze the decoupling removes would sneak back in through the poll's own request.
    kicks = []
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"),
                        rescan=lambda: kicks.append(1)).test_client()

    client.get("/pending")

    assert kicks == []


def test_submit_saves_edits_then_submits_and_returns_204(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/submit/a.m4a", data={
        "name": "My idea", "transcript": "edited text", "route": "asana", "asana_parent": "222",
    })

    # Submit flushes the row's current field values before submitting.
    assert service.edits == [
        ("a.m4a", {"name": "My idea", "transcript": "edited text",
                   "route": "asana", "asana_parent": "222"})
    ]
    assert service.submitted == ["a.m4a"]
    # 204 (no redirect): the client removes the row optimistically, no page reload.
    assert resp.status_code == 204


def test_group_route_consolidates_the_posted_notes_and_names_the_group(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="- one\n- two", kind="group")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/group", data={"files": ["a.m4a", "b.m4a"]})

    assert service.grouped == [["a.m4a", "b.m4a"]]
    assert resp.status_code == 200
    body = resp.get_json()
    # The rows come back whole — the merge changes several of them at once, and the page
    # takes the list the server holds rather than patching its own guess at it.
    assert 'data-file="a.m4a"' in body["rows"]
    assert "<!doctype" not in body["rows"]
    # Only the server can name the group: its recording is one the app makes, named by its
    # content, and undo has to know which row to walk the merge back out of.
    assert body["target"] == "group-of-2.m4a"


def test_unmerge_route_walks_one_merge_back_and_renames_the_group(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="g.m4a", transcript="- one\n- two", kind="group")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/unmerge/g.m4a")

    # This is what Undo posts: one merge back, not the whole group apart.
    assert service.unmerged == ["g.m4a"]
    assert service.ungrouped == []
    assert resp.status_code == 200
    body = resp.get_json()
    assert 'data-file="a.m4a"' in body["rows"] and 'data-file="b.m4a"' in body["rows"]
    assert "<!doctype" not in body["rows"]
    # Its recording was rejoined out of what is left, so the group answers to a new name.
    assert body["target"] == "left-of-g.m4a"


def test_unmerge_route_refuses_a_memo_that_is_not_a_group(tmp_path):
    service = FakeService()
    service.unmerge_error = "Only a group can have a merge walked back."
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/unmerge/a.m4a")

    assert resp.status_code == 400
    assert b"Only a group can have a merge walked back" in resp.data


def test_ungroup_route_breaks_the_group_up_and_answers_with_the_inbox(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="- one\n- two", kind="group")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/ungroup/a.m4a")

    assert service.ungrouped == ["a.m4a"]
    # The restored notes come back where the server sorts them, so it answers with the
    # rows themselves rather than leaving the client to guess where they belong.
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'data-file="a.m4a"' in body and 'data-file="b.m4a"' in body
    assert "<!doctype" not in body


def test_ungroup_route_refuses_a_memo_that_is_not_a_group(tmp_path):
    service = FakeService()
    service.ungroup_error = "Only a group can be broken back up into notes."
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/ungroup/a.m4a")

    assert resp.status_code == 400
    assert b"Only a group can be broken back up" in resp.data


def test_group_route_reports_a_selection_it_cannot_group(tmp_path):
    service = FakeService()
    service.group_error = "Two groups have no obvious survivor"
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/group", data={"files": ["g1.m4a", "g2.m4a"]})

    # The button is disabled for these selections, but a stale page must not silently
    # mangle notes — the server refuses and says why.
    assert resp.status_code == 400
    assert b"Two groups have no obvious survivor" in resp.data


def test_submit_defaults_route_to_notesnook_when_fields_are_missing(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    # A post without the picker fields still routes somewhere sane.
    client.post("/submit/a.m4a", data={"name": "X", "transcript": "Y"})

    assert service.edits == [("a.m4a", {"name": "X", "transcript": "Y",
                                        "route": "notesnook", "asana_parent": ""})]


def test_submit_that_fails_to_route_keeps_the_memo_and_signals_the_client(tmp_path):
    # The "Submit all sent nothing but everything vanished" bug: when routing fails
    # (e.g. Notesnook rejects the key), the memo must stay pending and the response
    # must be an error, so the client keeps the row instead of hiding a note that
    # never actually sent. Uses the real service so the whole seam is exercised.
    from highdeas.service import InboxService
    from highdeas.store import MemoStore

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.m4a").write_bytes(b"PRECIOUS")
    bin_dir = tmp_path / "bin"
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="pending", transcript="precious idea"))

    class StubTranscriber:
        def transcribe(self, path):
            return Transcript("")

    def failing_route(memo):
        raise RuntimeError("HTTP 401 Unauthorized")

    service = InboxService(inbox_dir=inbox, store=store, transcriber=StubTranscriber(),
                            bin_dir=bin_dir, route=failing_route, clock=lambda: "T")
    client = create_app(service, inbox_dir=str(inbox), bin_dir=str(bin_dir)).test_client()

    resp = client.post("/submit/a.m4a", data={"name": "", "transcript": "precious idea", "route": "notesnook"})

    # Failure is signalled, not a false 204.
    assert resp.status_code == 502
    # Nothing lost or half-processed: still pending, still in the inbox, not binned.
    assert [m.audio_filename for m in service.pending()] == ["a.m4a"]
    assert (inbox / "a.m4a").exists()
    assert not (bin_dir / "a.m4a").exists()


def test_index_has_a_region_to_report_submit_failures(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()

    # A notice region the client reveals when a submit fails, so a failed send is
    # visible rather than silently disappearing.
    assert 'id="notice"' in body


def test_submit_js_removes_a_row_only_after_the_server_confirms(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    # The row leaves the list only on a successful (r.ok) response; a failed submit
    # keeps it. Guards against regressing to optimistic removal.
    assert "r.ok" in asset(client, "inbox.js")


def test_index_shows_a_per_row_sending_state_while_a_submit_is_in_flight(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()
    js = asset(client, "inbox.js")
    css = asset(client, "app.css")

    # A row dims/locks and its Submit trades the paper plane for a spinner while its
    # request is in flight, so a click plainly took hold instead of the row seeming to
    # do nothing until it vanishes. The button's face is a glyph, so "Sending…" lives
    # in its label.
    assert "label(go, 'Sending…')" in js
    assert ".memo.sending" in css  # the dim-and-lock style the JS toggles
    # The Submit button carries both the plane and a spinner; .sending hides the plane,
    # shows the spinner, and holds it bright while the rest of the row dims.
    assert 'class="ic-spin"' in body
    assert ".memo.sending .go .ic-send { display: none" in css
    assert ".memo.sending .go .ic-spin svg { animation: spin" in css
    assert ".memo.sending > .go { opacity: 1" in css  # the spinner stays at full strength


def test_reorder_route_persists_the_dropped_order_and_returns_204(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/reorder", data={"order": ["c.m4a", "a.m4a", "b.m4a"]})

    # The client posts every row in its on-screen order after a drop.
    assert service.reordered == [["c.m4a", "a.m4a", "b.m4a"]]
    assert resp.status_code == 204


def test_edit_route_saves_fields_and_returns_204(tmp_path):
    service = FakeService()
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    resp = client.post("/edit/a.m4a", data={
        "name": "New name", "transcript": "New body", "route": "drive", "asana_parent": "111",
    })

    # Auto-save persists the fields without submitting/routing the memo.
    assert service.edits == [
        ("a.m4a", {"name": "New name", "transcript": "New body",
                   "route": "drive", "asana_parent": "111"})
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


def test_a_binned_memo_counts_the_days_it_has_left_rather_than_naming_its_hour(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="old.m4a", status="deleted", processed_at="2026-04-08T03:00"),
        Memo(audio_filename="new.m4a", status="deleted", processed_at="2026-07-07T03:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"),
                        now=lambda: datetime(2026, 7, 10, 9, 0)).test_client()

    body = client.get("/bin").data.decode()

    # The hour a memo was retired says nothing anyone needs; how long it has been in the
    # bin says whether the 90-day sweep is about to take it. The column counts the days.
    assert ">Days in bin</div>" in body
    assert '<div class="age">93</div>' in body
    assert '<div class="age">3</div>' in body
    assert "Jul 7" not in body and "2026-07-07T03:00" not in body


def test_a_binned_memo_with_no_timestamp_counts_no_days(tmp_path):
    # Memos retired before processed_at was captured carry none; the row still renders.
    service = FakeService(binned=[Memo(audio_filename="b.m4a", status="deleted", processed_at="")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    assert '<div class="age"></div>' in client.get("/bin").data.decode()


def test_both_pages_call_the_destination_column_by_the_same_name(tmp_path):
    service = FakeService(pending=[Memo(audio_filename="a.m4a")],
                          binned=[Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    index = client.get("/").data.decode()
    binned = client.get("/bin").data.decode()

    # The inbox picked a "Route" and the bin reported "Where" it went — two names for the
    # one thing the column holds, and neither of them the word the icons themselves use.
    assert ">Destination</div>" in index and ">Destination</div>" in binned
    assert ">Route<" not in index and ">Where<" not in binned


def test_a_bin_rows_destination_stands_in_the_same_square_as_its_buttons(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="n.m4a", status="processed", route="notesnook", processed_at="2026-07-07T02:00"),
        Memo(audio_filename="d.m4a", status="processed", route="drive", processed_at="2026-07-07T01:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()
    css = asset(client, "app.css")

    # A 20px glyph in a cell of its own height sat above the line Restore and Delete draw
    # their glyphs on. The same square holds it — borderless, since it reports rather than
    # acts — so the three of them read straight across the row.
    assert 'class="icon" title="Sent to Notesnook"' in body
    assert 'class="destlink icon"' in body
    square = css.split(".icon {")[1].split("}")[0]
    assert "width: 34px" in square and "height: 34px" in square
    assert "border: none" in css.split(".destlink {")[1].split("}")[0]


def test_bin_shows_destination_icon_instead_of_status_word(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="n.m4a", status="processed", route="notesnook", processed_at="2026-07-07T02:00"),
        Memo(audio_filename="g.m4a", status="processed", route="drive", processed_at="2026-07-07T01:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data

    # A destination icon with a label, not the raw status/badge.
    assert b'class="badge"' not in body
    assert b"Sent to Notesnook" in body
    assert b"Sent to Google Drive" in body


def test_bin_names_no_destination_for_a_memo_that_was_never_sent(tmp_path):
    # The column answers one question: which of the three destinations took this memo. A
    # trashed note and one merged into a group were never sent anywhere, so the column
    # has nothing to say — an icon there is a destination that doesn't exist. Both keep
    # the route they were bound for, so the status has to be read before the route, or
    # the bin claims they went to Notesnook.
    service = FakeService(binned=[
        Memo(audio_filename="d.m4a", status="deleted", route="notesnook", processed_at="2026-07-07T03:00"),
        Memo(audio_filename="m.m4a", status="grouped", route="drive", processed_at="2026-07-08T03:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    assert body.count('<div class="dest"></div>') == 2
    for said in ("Trashed", "Merged into a group", "Sent to Notesnook", "Sent to Google Drive"):
        assert said not in body


def test_bin_row_offers_restore_and_confirmed_permanent_delete(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data

    assert b'action="/restore/b.m4a"' in body
    assert b'action="/purge/b.m4a"' in body
    assert b"data-confirm=" in body  # permanent deletion asks first


def test_bin_bulk_controls_sit_in_the_column_headers_and_confirm(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    # Same pattern as the inbox page: bulk actions live in the grid headers over
    # their columns, not in the topbar.
    assert 'action="/restore-all"' in body
    assert 'action="/empty-bin"' in body
    assert body.index('grid bin headrow') < body.index('action="/restore-all"')
    # Both bulk actions ask first (restore-all is disruptive, empty-bin destroys).
    assert body.count("data-confirm=") >= 2


def test_the_bins_columns_of_buttons_and_their_bulk_heads_wear_the_same_glyphs(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="b.m4a", status="deleted", processed_at="2026-07-07T03:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    # The bin's heads speak the way the inbox's do: a glyph over a column of the same
    # glyph. Restore's arrow doubles back, Delete's is the bin the inbox trashes into.
    assert 'class="btn icon" title="Restore all" aria-label="Restore all"><svg' in body
    assert 'class="btn icon danger" title="Empty bin" aria-label="Empty bin"><svg' in body
    assert 'class="btn icon restore" title="Restore" aria-label="Restore"><svg' in body
    assert 'class="btn icon purge danger" title="Delete" aria-label="Delete"><svg' in body
    for word in (">Restore all<", ">Empty bin<", ">Restore<", ">Delete<"):
        assert word not in body, word
    # The words are gone from the buttons, not from the page: each confirm still says them.
    assert "Restore all 1 item to the inbox?" in body


def test_hovering_a_bulk_head_lights_the_whole_column_it_presses(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    css = asset(client, "app.css")

    # Hovering a column-head "all" button answers with its blast radius: every row
    # button it would press wears its own hover chrome. :hover can't reach from the
    # frozen header into the rows, so the rule runs from body with :has() — which
    # also covers rows the live poll splices in later, with no JS to re-wire. A
    # button with nothing to do (disabled, mid-send) stays out of the preview, just
    # as it sits out the ordinary hover.
    assert "body:has(#submit-all:not(:disabled):hover) .memo .go:not(:disabled)" in css
    assert "body:has(#trash-all:not(:disabled):hover) .memo .del:not(:disabled)" in css
    assert "body:has(#restore-all:not(:disabled):hover) .row .restore:not(:disabled)" in css
    assert "body:has(#empty-bin:not(:disabled):hover) .row .purge:not(:disabled)" in css


def test_a_machine_that_cannot_hear_the_phone_says_so_on_every_page(tmp_path):
    # The condition is invisible from the phone (its pushes just go
    # unanswered) and from the console (pythonw has none) — the page is
    # where this machine must say it. app.py plants the sentence in config
    # when the upload listener dies or never starts.
    app = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"))
    app.config["PHONE_UPLOADS_OFF"] = (
        "Phone uploads are off: HIGHDEAS_UPLOAD_TOKEN is missing from the .env on this machine.")
    client = app.test_client()

    assert "Phone uploads are off" in client.get("/").data.decode()
    assert "Phone uploads are off" in client.get("/bin").data.decode()


def test_a_machine_hearing_the_phone_wears_no_warning(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    assert "Phone uploads are off" not in client.get("/").data.decode()


def test_bin_back_control_is_a_button_not_a_text_link(tmp_path):
    # Same button chrome the inbox topbar uses, so "← Inbox" reads as a control
    # rather than as prose in the title bar.
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    assert '<a class="btn topbtn" href="/">' in body
    # It names where it goes, and nothing else — the mirror of the inbox's "Bin →".
    assert "&larr; Inbox<" in body
    assert "Back to inbox" not in body


def test_bin_rows_carry_no_number_and_no_placeholder_columns(tmp_path):
    # The bin used to hold the inbox's shape open with a row number and two empty cells,
    # so the two grids' columns lined up one for one. Nothing in the bin is reordered, so
    # the number named nothing; the empties were scaffolding for it. Its rows now start
    # at the group badge, and its transcript takes back the width they cost.
    service = FakeService(binned=[
        Memo(audio_filename="a.m4a", status="deleted", processed_at="2026-07-07T03:00"),
        Memo(audio_filename="b.m4a", status="processed", processed_at="2026-07-07T02:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    assert 'class="num"' not in body
    assert "<div></div>" not in body
    # Eight columns now, and the badge leads them.
    assert body.count('<div class="head"') == 8
    row = body.index('class="row"')
    assert body.index('class="kind"', row) < body.index('class="age"', row)


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


def test_bin_drive_memo_icon_posts_to_open_drive(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="g.m4a", name="My Song", status="processed", route="drive", processed_at="2026-07-07T01:00"),
        Memo(audio_filename="d.m4a", status="deleted", processed_at="2026-07-07T02:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    # Only the Drive memo's icon opens Drive (via /open-drive, which launches Chrome
    # in the chosen profile, at the actual Drive folder); trashed/Notesnook icons don't.
    assert body.count('action="/open-drive"') == 1


def test_open_drive_launches_chrome_at_the_configured_drive_folder(tmp_path):
    # A direct link to the real Drive folder — never a Drive search for the memo's
    # name, which is a search-results page, not "opening the memo in Drive".
    launched = []
    folder_url = "https://drive.google.com/drive/folders/1AbCDeFGhIJKlmNOpQRstuVwxYZ01234"
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"),
                        open_link=launched.append, drive_folder_url=folder_url).test_client()

    resp = client.post("/open-drive")

    assert resp.status_code == 204
    assert launched == [folder_url]


def test_open_drive_does_nothing_without_a_configured_folder_url(tmp_path):
    # Nothing configured yet: stay quiet rather than launch a blank/broken link.
    launched = []
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"),
                        open_link=launched.append).test_client()

    resp = client.post("/open-drive")

    assert resp.status_code == 204
    assert launched == []


def test_bin_asana_memo_icon_posts_to_open_asana(tmp_path):
    service = FakeService(binned=[
        Memo(audio_filename="s.m4a", name="Riff", status="processed", route="asana",
             asana_url="https://app.asana.com/0/0/42/f", processed_at="2026-07-09T01:00"),
        Memo(audio_filename="old.m4a", status="processed", route="asana",
             processed_at="2026-07-09T00:30"),  # sent before permalinks were stored
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/bin").data.decode()

    # Like the Drive icon, the Asana icon is the way back to where the note went —
    # but only when a permalink was stored; a linkless row keeps a plain icon.
    assert body.count('action="/open-asana/s.m4a"') == 1
    assert 'action="/open-asana/old.m4a"' not in body
    assert 'title="Sent to Asana — open the task"' in body
    assert 'title="Sent to Asana"' in body  # the linkless row still names its destination


def test_open_asana_opens_the_stored_task_link(tmp_path):
    # The client sends only the filename; the server looks up the permalink Asana
    # returned at submit time — it never launches a client-supplied URL.
    opened = []
    service = FakeService(binned=[
        Memo(audio_filename="s.m4a", status="processed", route="asana",
             asana_url="https://app.asana.com/0/0/42/f", processed_at="2026-07-09T01:00"),
    ])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin"),
                        open_link=opened.append).test_client()

    assert client.post("/open-asana/s.m4a").status_code == 204
    assert client.post("/open-asana/missing.m4a").status_code == 204  # unknown memo: no-op

    assert opened == ["https://app.asana.com/0/0/42/f"]


def test_pages_reserve_the_scrollbar_gutter_so_they_dont_shift(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    # One stylesheet for both pages, so flipping between them doesn't shift sideways.
    assert "scrollbar-gutter: stable" in asset(client, "app.css")


# --- the updater's endpoints -------------------------------------------------


class FakeUpdates:
    def __init__(self, behind=0, refuse=False):
        self._behind = behind
        self._refuse = refuse
        self.pulled = 0
        self.respawned = 0

    def status(self):
        return {"behind": self._behind}

    def pull(self):
        if self._refuse:
            raise RuntimeError("cannot fast-forward")
        self.pulled += 1

    def respawn(self):
        self.respawned += 1


def _update_app(updates):
    return create_app(FakeService(), inbox_dir="inbox", bin_dir="bin",
                      updates=updates, update_respawn_delay=0).test_client()


def test_version_reports_how_far_behind_the_app_is():
    client = _update_app(FakeUpdates(behind=4))

    response = client.get("/version")

    assert response.status_code == 200
    assert response.get_json() == {"behind": 4}


def test_version_is_quietly_current_without_an_updater():
    client = create_app(FakeService(), inbox_dir="inbox", bin_dir="bin").test_client()

    assert client.get("/version").get_json() == {"behind": 0}


def test_update_pulls_answers_then_relaunches():
    # The response must reach the page before the process replaces itself,
    # or a successful update reads as a failed request.
    updates = FakeUpdates(behind=2)
    client = _update_app(updates)

    response = client.post("/update")

    assert response.status_code == 204
    assert updates.pulled == 1
    deadline = time.time() + 2
    while updates.respawned == 0 and time.time() < deadline:
        time.sleep(0.02)
    assert updates.respawned == 1


def test_a_refused_update_reports_and_stays_alive():
    updates = FakeUpdates(refuse=True)
    client = _update_app(updates)

    response = client.post("/update")

    assert response.status_code == 502
    assert b"cannot fast-forward" in response.data
    assert updates.respawned == 0


def test_every_page_carries_the_in_page_find(tmp_path):
    # "A Ctrl+F for each page." The browser's own find can't reach a transcript the
    # three-line preview clips off, nor the bin's scrolled text box — so both pages
    # carry our own find, and it lives in the shared chrome rather than one page's markup.
    service = FakeService(pending=[Memo(audio_filename="a.m4a", transcript="hi")],
                          binned=[Memo(audio_filename="b.m4a", status="deleted",
                                       processed_at="2026-07-07T03:00")])
    client = create_app(service, inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    inbox = client.get("/").data.decode()
    binned = client.get("/bin").data.decode()

    for body in (inbox, binned):
        assert 'id="find"' in body
        assert 'id="find-input"' in body
        assert "/static/find.js" in body
    # It names the page it searches, and starts hidden until Ctrl+F calls it up.
    assert 'placeholder="Find in Inbox…"' in inbox
    assert 'placeholder="Find in Bin…"' in binned
    assert 'id="find" class="find" hidden' in inbox


def test_find_opens_on_ctrl_f_and_steps_aside_for_a_dialog(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "find.js")

    # Ctrl+F — and ⌘F on a Mac — opens ours instead of the browser's, whose match can't
    # reach the clipped preview; it takes the key off the browser so both can't answer it.
    assert "event.ctrlKey || event.metaKey" in js
    assert "'f'" in js
    assert "event.preventDefault()" in js
    # Esc closes ours again.
    assert "'Escape'" in js
    # But an open dialog keeps the keys to itself: the editor's body is a long text the
    # browser's own find is the right tool for, and Esc there closes the editor.
    assert "dialog[open]" in js


def test_find_filters_both_pages_by_a_rows_whole_name_and_transcript(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "find.js")
    css = asset(client, "app.css")

    # One filter serves both pages: it hides the inbox's .memo rows and the bin's .row
    # rows the same way, matching a row by its name and its transcript wherever each
    # lives — an editable field or a plain cell, a .transcript preview or a .text block.
    assert ".memo, .row" in js
    assert "input[name=name]" in js
    assert ".transcript, .text" in js
    # The whole transcript, not the clipped preview: .textContent reads the entire note
    # even where only three lines of it show. Matched case-insensitively as a substring,
    # the way Ctrl+F reads.
    assert "textContent" in js
    assert "toLowerCase()" in js
    assert "indexOf(term) >= 0" in js
    # A missed row is display:none even though .memo/.row are display:contents, so the
    # rule out-specifies them to put the display back.
    rule = css.split(".grid .memo.find-miss")[1].split("}")[0]
    assert ".grid .row.find-miss" in rule and ".grid .sep.find-miss" in rule
    assert "display: none" in rule


def test_find_keeps_the_separators_right_and_re_runs_as_the_list_changes(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    js = asset(client, "find.js")

    # The line between rows is a separate element sitting before each row, so a hidden
    # row would strand its line. The filter hides the separator above a missed row and
    # the one above the first match, so exactly one line sits between any two matches and
    # none leads the list.
    assert "previousElementSibling" in js
    assert "contains('sep')" in js
    # The list changes under an open search — a recording the poll splices in, a merge
    # that rebuilds every row — so the filter re-runs when it does. It toggles only
    # classes, never the child list, so watching childList never trips on its own work.
    assert "MutationObserver" in js
    assert "childList: true" in js


def test_find_tallies_the_matches_out_loud(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()
    js = asset(client, "find.js")

    # The bar says how much of the list it has narrowed to, announced for a screen reader
    # as it changes.
    assert 'id="find-tally"' in body
    assert 'aria-live="polite"' in body
    assert "' of '" in js  # "3 of 42"
    assert "No matches" in js


def test_find_closes_back_to_the_whole_list(tmp_path):
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    body = client.get("/").data.decode()
    js = asset(client, "find.js")

    # Closing clears the query, so the list you come back to is the whole one and not a
    # filter left on and forgotten.
    assert 'id="find-close"' in body
    close = js.split("function close()")[1].split("}")[0]
    assert "input.value = ''" in close
    assert "bar.hidden = true" in close


def test_find_is_the_one_script_both_pages_share_for_it(tmp_path):
    # find.js loads on every page from the shared chrome, so a page can't carry the bar
    # and forget the behaviour. The bin has no inbox.js and no undo stack, but it has this.
    client = create_app(FakeService(), inbox_dir=str(tmp_path), bin_dir=str(tmp_path / "bin")).test_client()

    binned = client.get("/bin").data.decode()

    assert "/static/find.js" in binned
    assert "/static/inbox.js" not in binned
    assert "/static/history.js" not in binned
