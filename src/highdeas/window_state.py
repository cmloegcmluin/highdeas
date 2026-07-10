"""Remember the native window's size, position, and maximized state across launches."""
import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path


@dataclass(frozen=True)
class WindowGeometry:
    """Where the window sits when it is *not* maximized, plus whether it was.

    ``x``/``y`` are None until the window has been placed somewhere; pywebview reads
    that as "center it on the primary screen".
    """

    width: int = 1360
    height: int = 900
    x: int | None = None
    y: int | None = None
    maximized: bool = True

    def window_kwargs(self):
        """The geometry as pywebview's ``create_window`` keyword arguments."""
        return asdict(self)

    def reachable_on(self, screens):
        """This geometry, minus a position none of the connected monitors covers.

        Unplug the monitor the window was last closed on and its saved coordinates
        point into nowhere: Windows would happily open the window off-screen, out of
        the user's reach. Dropping the position reopens it centered instead.
        """
        if self.x is None or self.y is None:
            return self
        if any(_covers(screen, self.x, self.y) for screen in screens):
            return self
        return replace(self, x=None, y=None)


def _covers(screen, x, y):
    return (screen.x <= x < screen.x + screen.width
            and screen.y <= y < screen.y + screen.height)


def load_geometry(path):
    """The last saved geometry, or the maximized default if nothing usable is saved."""
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WindowGeometry()
    if not isinstance(saved, dict):
        return WindowGeometry()
    known = {f.name for f in fields(WindowGeometry)}
    return WindowGeometry(**{k: v for k, v in saved.items() if k in known})


def save_geometry(path, geometry):
    """Write the geometry, replacing the file atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(geometry), indent=2), encoding="utf-8")
    tmp.replace(path)


class WindowGeometryTracker:
    """Follow a pywebview window's geometry and save it when the window closes."""

    def __init__(self, path, geometry):
        self._path = path
        self._geometry = geometry
        self._window = None

    def attach(self, window):
        self._window = window
        window.events.resized += self._resized
        window.events.moved += self._moved
        window.events.maximized += self._maximized
        window.events.restored += self._restored
        window.events.closing += self._closing

    def _is_normal(self):
        """Whether the window is reporting the geometry worth remembering.

        A maximized window reports the whole screen at (-8, -8) and a minimized one is
        parked at (-32000, -32000), so only a Normal window's geometry may be recorded.
        The native window is asked directly rather than inferred from the maximized and
        minimized events, because pywebview emits ``moved`` *before* either of them and
        dispatches every event on its own thread — a flag those events set is always a
        step behind. The native window already holds the new state when ``moved`` fires.
        """
        return str(self._window.native.WindowState) == "Normal"

    def _resized(self, width, height):
        if self._is_normal():
            self._geometry = replace(self._geometry, width=width, height=height)

    def _moved(self, x, y):
        if self._is_normal():
            self._geometry = replace(self._geometry, x=x, y=y)

    def _maximized(self):
        self._geometry = replace(self._geometry, maximized=True)

    def _restored(self):
        # Restoring from *minimized* also lands here, which is right: a window that was
        # maximized before being minimized comes back maximized and re-fires `maximized`.
        self._geometry = replace(self._geometry, maximized=False)

    def _closing(self):
        save_geometry(self._path, self._geometry)
