"""Code-driven capture of a single window's client area (Windows only).

Captures one named window, never the desktop. A full-screen grab would pick up
whatever else the user has open, which is both a privacy problem and a
correctness one — an overlapping window would land in the training data.

Two backends, tried in order:

``PrintWindow``
    Asks the window to render itself into an off-screen bitmap. Works when the
    window is partially covered or off-screen, which is what a background
    automation run needs. Chrome composites WebGL on the GPU, so this can come
    back blank; the result is checked rather than trusted.

``mss`` over the window rect
    Grabs the screen where the window sits. Requires the window to be visible
    and unobscured, so it is the fallback, not the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: PrintWindow flag that includes GPU-composited (DirectComposition) content.
PW_RENDERFULLCONTENT = 0x00000002

#: A capture whose pixels are nearly all one colour is a failed render, not a
#: real screen. Chrome returns a uniform white or black bitmap when PrintWindow
#: cannot reach its compositor.
BLANK_UNIQUE_COLOURS = 4


class WindowNotFoundError(RuntimeError):
    """No visible window matched; the caller must stop rather than guess."""


class WindowCaptureError(RuntimeError):
    """Both backends failed to produce a usable image."""


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    rect: tuple[int, int, int, int]

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


def find_window(title_contains: str) -> WindowInfo:
    """Return the single visible window whose title contains ``title_contains``.

    Raises when nothing matches, and when more than one does: capturing the
    wrong window would silently feed another page into the parser.
    """
    import win32gui

    matches: list[WindowInfo] = []

    def _visit(handle: int, _acc: object) -> None:
        if not win32gui.IsWindowVisible(handle):
            return
        title = win32gui.GetWindowText(handle)
        if title_contains.lower() not in title.lower():
            return
        rect = win32gui.GetWindowRect(handle)
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return
        matches.append(WindowInfo(handle=handle, title=title, rect=rect))

    win32gui.EnumWindows(_visit, None)
    if not matches:
        raise WindowNotFoundError(f"no visible window matching {title_contains!r}")
    if len(matches) > 1:
        titles = ", ".join(repr(m.title) for m in matches)
        raise WindowNotFoundError(f"{title_contains!r} matched more than one window: {titles}")
    return matches[0]


def capture_window(window: WindowInfo) -> Any:
    """Return a Pillow image of the window, or raise if neither backend works."""
    image = _print_window(window)
    if image is not None and not _is_blank(image):
        return image
    fallback = _grab_rect(window)
    if fallback is None or _is_blank(fallback):
        raise WindowCaptureError(
            f"captured a blank image for {window.title!r}; "
            "the window may be minimised or rendered on a surface neither backend can read"
        )
    return fallback


def _print_window(window: WindowInfo) -> Any | None:
    import ctypes

    import win32gui
    import win32ui
    from PIL import Image

    width, height = window.width, window.height
    window_dc = win32gui.GetWindowDC(window.handle)
    source = win32ui.CreateDCFromHandle(window_dc)
    target = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        target.SelectObject(bitmap)
        # ctypes.windll exists only on Windows. Reached dynamically so the
        # module still type-checks on the Linux CI runner, where mypy sees a
        # ctypes without that attribute.
        user32 = getattr(ctypes, "windll").user32
        result = user32.PrintWindow(window.handle, target.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if result != 1:
            return None
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        return Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        )
    finally:
        target.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(window.handle, window_dc)
        win32gui.DeleteObject(bitmap.GetHandle())


def _grab_rect(window: WindowInfo) -> Any | None:
    import mss
    from PIL import Image

    left, top, right, bottom = window.rect
    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def _is_blank(image: Any) -> bool:
    colours = image.convert("RGB").getcolors(maxcolors=BLANK_UNIQUE_COLOURS)
    # getcolors() returns None once the image has more distinct colours than
    # the cap, which is what a real screenshot looks like.
    return colours is not None
