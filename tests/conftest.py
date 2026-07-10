from types import SimpleNamespace

import pytest


class _Event:
    """Stand-in for a pywebview window event: handlers subscribe with ``+=``."""

    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in self._handlers:
            handler(*args)


class FakeWindow:
    """The slice of a pywebview window the geometry tracker touches.

    ``maximize``/``minimize``/``restore`` replay the event order the winforms backend
    really emits (traced on Windows 11): ``moved`` first, then the state event, then
    ``resized`` — with ``native.WindowState`` already reporting the *new* state. The
    screen-filling and parked-off-screen coordinates are the ones Windows reports.
    """

    def __init__(self, width=1360, height=900, x=0, y=0):
        self.events = SimpleNamespace(
            resized=_Event(), moved=_Event(), maximized=_Event(),
            minimized=_Event(), restored=_Event(), closing=_Event(),
        )
        self.native = SimpleNamespace(WindowState="Normal")
        self._normal = (x, y, width, height)

    def move(self, x, y):
        self._normal = (x, y, *self._normal[2:])
        self.events.moved.fire(x, y)

    def resize(self, width, height):
        self._normal = (*self._normal[:2], width, height)
        self.events.resized.fire(width, height)

    def maximize(self):
        self.native.WindowState = "Maximized"
        self.events.moved.fire(-8, -8)
        self.events.maximized.fire()
        self.events.resized.fire(2576, 1408)

    def minimize(self):
        self.native.WindowState = "Minimized"
        self.events.moved.fire(-32000, -32000)
        self.events.minimized.fire()
        self.events.resized.fire(160, 33)

    def restore(self):
        x, y, width, height = self._normal
        self.native.WindowState = "Normal"
        self.events.moved.fire(x, y)
        self.events.restored.fire()
        self.events.resized.fire(width, height)

    def close(self):
        self.events.closing.fire()


@pytest.fixture
def fake_window():
    return FakeWindow()
