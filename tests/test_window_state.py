import pytest

from highdeas.window_state import (
    WindowGeometry,
    WindowGeometryTracker,
    load_geometry,
    save_geometry,
)


def test_a_window_with_no_remembered_geometry_opens_maximized():
    # The app is meant to fill the screen, so a first launch (nothing saved yet)
    # opens maximized, centered at the fallback size it un-maximizes to.
    geometry = WindowGeometry()

    assert geometry.maximized is True
    assert (geometry.x, geometry.y) == (None, None)


def test_window_kwargs_are_the_pywebview_create_window_arguments():
    geometry = WindowGeometry(width=800, height=600, x=10, y=20, maximized=False)

    assert geometry.window_kwargs() == {
        "width": 800, "height": 600, "x": 10, "y": 20, "maximized": False,
    }


def test_loading_a_missing_file_gives_the_maximized_default(tmp_path):
    assert load_geometry(tmp_path / "window.json") == WindowGeometry()


@pytest.mark.parametrize("contents", ["{not json", "[1, 2]", "null"])
def test_loading_a_corrupt_file_gives_the_maximized_default(tmp_path, contents):
    # A half-written or hand-edited file must never stop the app from opening.
    path = tmp_path / "window.json"
    path.write_text(contents, encoding="utf-8")

    assert load_geometry(path) == WindowGeometry()


def test_loading_ignores_keys_the_geometry_no_longer_has(tmp_path):
    # A file written by an older Highdeas must still yield the fields it does have.
    path = tmp_path / "window.json"
    path.write_text('{"width": 800, "fullscreen": true}', encoding="utf-8")

    assert load_geometry(path) == WindowGeometry(width=800)


def test_saving_then_loading_round_trips_the_geometry(tmp_path):
    path = tmp_path / "window.json"
    geometry = WindowGeometry(width=800, height=600, x=-1920, y=40, maximized=False)

    save_geometry(path, geometry)

    assert load_geometry(path) == geometry


class _Screen:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


LAPTOP = _Screen(0, 0, 1920, 1080)
SECOND_MONITOR = _Screen(-1920, -200, 1920, 1080)


def test_a_position_a_monitor_still_covers_is_kept():
    geometry = WindowGeometry(x=-1900, y=0)

    assert geometry.reachable_on([LAPTOP, SECOND_MONITOR]) == geometry


def test_a_position_no_monitor_covers_anymore_is_forgotten():
    # The second monitor is unplugged: reopening at its coordinates would strand the
    # window off-screen, where it can't be dragged back. Fall back to centered.
    geometry = WindowGeometry(width=800, height=600, x=-1900, y=0, maximized=False)

    recentered = geometry.reachable_on([LAPTOP])

    assert recentered == WindowGeometry(width=800, height=600, maximized=False)


def test_a_never_placed_window_stays_centered():
    geometry = WindowGeometry()

    assert geometry.reachable_on([LAPTOP]) == geometry


def _track(fake_window, tmp_path, geometry=WindowGeometry(maximized=False)):
    WindowGeometryTracker(tmp_path / "window.json", geometry).attach(fake_window)
    return fake_window


def _reopened(tmp_path):
    return load_geometry(tmp_path / "window.json")


def test_closing_persists_the_size_and_position_the_window_was_left_at(tmp_path, fake_window):
    window = _track(fake_window, tmp_path)

    window.resize(1024, 768)
    window.move(150, 75)
    window.close()

    assert _reopened(tmp_path) == WindowGeometry(1024, 768, 150, 75, maximized=False)


def test_a_window_closed_maximized_reopens_maximized(tmp_path, fake_window):
    window = _track(fake_window, tmp_path)

    window.maximize()
    window.close()

    assert _reopened(tmp_path).maximized is True


def test_a_window_closed_un_maximized_reopens_un_maximized(tmp_path, fake_window):
    window = _track(fake_window, tmp_path, WindowGeometry(maximized=True))

    window.maximize()
    window.restore()
    window.close()

    assert _reopened(tmp_path).maximized is False


def test_maximizing_does_not_overwrite_the_geometry_to_un_maximize_back_to(tmp_path, fake_window):
    # Windows reports a maximized window as screen-filling at (-8, -8), and it emits
    # that move *before* the maximized event. Remembering either would leave nothing
    # sane to un-maximize back to — and (-8, -8) can even sit on the wrong monitor,
    # which is where the next launch would then maximize.
    window = _track(fake_window, tmp_path)
    window.resize(1024, 768)
    window.move(150, 75)

    window.maximize()
    window.close()

    assert _reopened(tmp_path) == WindowGeometry(1024, 768, 150, 75, maximized=True)


def test_minimizing_does_not_overwrite_the_geometry_with_the_off_screen_park(tmp_path, fake_window):
    # Windows parks a minimized window at (-32000, -32000). Closing it from the
    # taskbar while minimized must not strand the next launch out there.
    window = _track(fake_window, tmp_path)
    window.resize(1024, 768)
    window.move(150, 75)

    window.minimize()
    window.close()

    assert _reopened(tmp_path) == WindowGeometry(1024, 768, 150, 75, maximized=False)


def test_un_minimizing_a_window_lets_it_be_tracked_again(tmp_path, fake_window):
    window = _track(fake_window, tmp_path)

    window.minimize()
    window.restore()
    window.move(150, 75)
    window.close()

    assert _reopened(tmp_path).x == 150


def test_a_maximized_window_closed_from_the_taskbar_still_reopens_maximized(tmp_path, fake_window):
    window = _track(fake_window, tmp_path)

    window.maximize()
    window.minimize()
    window.close()

    assert _reopened(tmp_path).maximized is True
