"""
Phantom Lyrics - Transparent Overlay Window
=============================================
A frameless, always-on-top, transparent PySide6 window
that displays song lyrics in a "ghost-like" style.

Features:
  - 100% transparent background (only the text is visible).
  - Drag-and-drop: click and drag the overlay anywhere on screen, anytime.
    The position is saved and restored on the next launch.
  - Always-on-top: sits above League of Legends, IDEs, etc.
  - Active lyric line is brighter (~85% opacity), others are dimmer (~40%).
  - Auto-positions to the bottom-left corner of the screen on first run.
  - Resizes vertically to fit the number of visible lyric lines.

Windows-specific:
  - Uses win32gui to set WS_EX_LAYERED and WS_EX_NOACTIVATE
    (per-pixel transparency + no taskbar/Alt+Tab entry, no focus stealing).
"""

import json
import logging
import sys
from pathlib import Path

from PySide6.QtCore import (
    Qt,
    QTimer,
    QRect,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QFont,
    QColor,
    QPainter,
    QPen,
    QBrush,
    QPainterPath,
    QFontMetrics,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
)

logger = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────

# Overlay dimensions and layout
OVERLAY_WIDTH = 600            # Fixed width in pixels
MAX_VISIBLE_LINES = 3         # Lyric rows shown: previous, current, next
LINE_SPACING_PX = 6           # Extra space between lines (added to font height)
TITLE_GAP_PX = 12             # Extra space between the song info and the first lyric line
SIDE_PADDING_PX = 20          # Horizontal padding from the window edge
BOTTOM_PADDING_PX = 40        # Vertical padding from the screen bottom
FONT_FAMILY = "Segoe UI"      # Clean, modern font available on Windows
FONT_SIZE = 14                # Base font size in points

# Opacity values (0-255 for QColor alpha)
ACTIVE_LINE_ALPHA = 220       # ~86% opacity — stands out clearly
INACTIVE_LINE_ALPHA = 110     # ~43% opacity — ghost-like, barely visible
SONG_INFO_ALPHA = 80          # ~31% opacity — very subtle song info

# Colors
TEXT_COLOR = QColor(255, 255, 255)  # Pure white, alpha applied per-line
SHADOW_COLOR = QColor(0, 0, 0)      # Black outline for readability on light bg
OUTLINE_WIDTH_PX = 3                # Stroke width around each letter (subtitle-style)

# Grab handle: a near-invisible fill (alpha 1) painted across the whole window
# so Windows delivers mouse events to every pixel (layered windows hit-test by
# pixel alpha; fully transparent areas would otherwise ignore clicks, forcing
# you to grab the letters precisely). Alpha 1/255 is invisible to the eye.
GRAB_FILL_COLOR = QColor(0, 0, 0, 1)

# Message shown when LRCLib has no lyrics for the current song
NO_LYRICS_MESSAGE = "No lyrics found for this song"

# Update frequency for UI interpolation (ms)
TICK_INTERVAL_MS = 100

# Persist the overlay position across runs
CONFIG_DIR = Path.home() / ".phantom_lyrics"
POSITION_FILE = CONFIG_DIR / "overlay_position.json"


# ─── The Overlay Widget ────────────────────────────────────────


class LyricsOverlay(QWidget):
    """
    The main overlay widget. Renders lyrics text directly via paintEvent
    for maximum control over transparency and positioning.
    """

    # Signal to safely update UI state from any thread
    update_requested = Signal()

    def __init__(self) -> None:
        super().__init__()

        # ── Window state ──────────────────────────────────────
        self._lyric_lines: list[tuple[float, str]] = []  # [(timestamp, text), ...]
        self._current_line_index: int = -1               # Which line is active
        self._song_artist: str = ""
        self._song_title: str = ""
        self._current_time: float = 0.0                   # Latest timestamp from WS
        self._no_lyrics: bool = False                     # Show "no lyrics" message?

        # ── Drag state ───────────────────────────────────────
        self._drag_offset = None  # QPoint: cursor-to-window-origin offset while dragging

        # ── Setup ────────────────────────────────────────────
        self._init_window()
        self._init_timer()

        # The whole overlay is grabbable — hint with a move cursor
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        logger.info("Lyrics overlay initialized.")

    # ─── Public API ──────────────────────────────────────────

    def set_lyrics(
        self,
        artist: str,
        title: str,
        lyric_lines: list[tuple[float, str]],
    ) -> None:
        """
        Replace the current lyrics with a new song.

        Thread-safe — can be called from any thread.

        Args:
            artist: Artist name (for the subtle header).
            title: Song title (for the subtle header).
            lyric_lines: List of (timestamp_seconds, lyric_text) tuples,
                         sorted by timestamp.
        """
        self._song_artist = artist
        self._song_title = title
        # Filter out empty-text lines (instrumental breaks) so they don't
        # create blank rows — the last sung lyric stays in focus instead.
        self._lyric_lines = [line for line in lyric_lines if line[1].strip()]
        self._current_line_index = -1
        self._current_time = 0.0
        self._no_lyrics = False
        self.update_requested.emit()

    def set_timestamp(self, current_time: float) -> None:
        """
        Update the current playback position.

        Thread-safe — can be called from any thread.

        Args:
            current_time: Current playback position in seconds.
        """
        self._current_time = current_time
        self.update_requested.emit()

    def clear(self) -> None:
        """Clear all lyrics from the overlay."""
        self._lyric_lines = []
        self._current_line_index = -1
        self._song_artist = ""
        self._song_title = ""
        self._no_lyrics = False
        self.update_requested.emit()

    def show_no_lyrics(self, artist: str, title: str) -> None:
        """
        Show a "No lyrics found" message for the given song.

        Thread-safe — can be called from any thread.

        Args:
            artist: Artist name (for the subtle header).
            title: Song title (for the subtle header).
        """
        self._song_artist = artist
        self._song_title = title
        self._lyric_lines = []
        self._current_line_index = -1
        self._current_time = 0.0
        self._no_lyrics = True
        self.update_requested.emit()

    # ─── Initialization ───────────────────────────────────────

    def _init_window(self) -> None:
        """Configure the window flags and geometry for the ghost overlay."""
        # Frameless, always-on-top, no taskbar entry
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint          # No title bar / borders
            | Qt.WindowType.WindowStaysOnTopHint       # Above everything
            | Qt.WindowType.Tool                        # Hides from taskbar
            | Qt.WindowType.NoDropShadowWindowHint      # No shadow on frameless
        )

        # Transparent background — we only paint the text
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        # Position at bottom-left of the primary screen
        screen = QApplication.primaryScreen()
        if screen:
            screen_geom: QRect = screen.availableGeometry()
        else:
            screen_geom = QRect(0, 0, 1920, 1080)

        # Calculate window height: song info header + visible lyric lines
        font = QFont(FONT_FAMILY, FONT_SIZE)
        fm = QFontMetrics(font)
        line_height = fm.height() + LINE_SPACING_PX
        window_height = (MAX_VISIBLE_LINES * line_height) + line_height + SIDE_PADDING_PX

        x = SIDE_PADDING_PX
        y = screen_geom.bottom() - window_height - BOTTOM_PADDING_PX

        self.setGeometry(x, y, OVERLAY_WIDTH, window_height)
        self.setFixedSize(OVERLAY_WIDTH, window_height)

        # Restore the last saved position (overrides the default bottom-left)
        self._load_position()

    def _init_timer(self) -> None:
        """Set up the refresh timer for smooth UI updates."""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(TICK_INTERVAL_MS)
        self._timer.start()

        # Connect the signal for cross-thread updates
        self.update_requested.connect(self._on_update_requested)

    # ─── Event Overrides ──────────────────────────────────────

    def paintEvent(self, event) -> None:
        """Custom paint: draw lyrics text with varying opacity."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Near-invisible fill across the whole window so every pixel is
        # grabbable for drag-and-drop (layered windows hit-test by pixel alpha;
        # fully transparent areas would otherwise ignore mouse clicks).
        painter.fillRect(self.rect(), GRAB_FILL_COLOR)

        font = QFont(FONT_FAMILY, FONT_SIZE)
        fm = QFontMetrics(font)
        line_height = fm.height() + LINE_SPACING_PX

        def center_x(text: str) -> int:
            """Horizontal x so text is centered within the overlay width."""
            return (OVERLAY_WIDTH - fm.horizontalAdvance(text)) // 2

        # ── Song info line (very subtle) ──────────────────
        if self._song_title:
            painter.setFont(font)
            info_text = f"{self._song_artist} — {self._song_title}" if self._song_artist else self._song_title
            baseline = fm.ascent() + 4
            self._draw_outlined_text(painter, center_x(info_text), baseline, info_text, SONG_INFO_ALPHA)

        # ── "No lyrics found" message ─────────────────────
        if self._no_lyrics:
            painter.setFont(font)
            y_offset = line_height + TITLE_GAP_PX  # gap below song info
            self._draw_outlined_text(
                painter, center_x(NO_LYRICS_MESSAGE), y_offset + fm.ascent(), NO_LYRICS_MESSAGE, INACTIVE_LINE_ALPHA
            )
            painter.end()
            return

        if not self._lyric_lines:
            painter.end()
            return

        # ── Lyric lines ───────────────────────────────────
        visible_lines = self._get_visible_window()

        y_offset = line_height + TITLE_GAP_PX  # gap below song info

        for idx, line_text in visible_lines:
            painter.setFont(font)

            # Determine opacity based on whether this is the active line
            if idx == self._current_line_index:
                alpha = ACTIVE_LINE_ALPHA
            else:
                alpha = INACTIVE_LINE_ALPHA
                # Optional: gradient fade for lines further from active
                distance = abs(idx - self._current_line_index)
                if distance > 0:
                    # Reduce alpha by 15 per step away, floor at 25
                    fade = max(25, INACTIVE_LINE_ALPHA - (distance - 1) * 15)
                    alpha = fade

            self._draw_outlined_text(painter, center_x(line_text), y_offset + fm.ascent(), line_text, alpha)
            y_offset += line_height

        painter.end()

    # ─── Internals ────────────────────────────────────────────

    def _draw_outlined_text(
        self,
        painter: QPainter,
        x: int,
        baseline: int,
        text: str,
        alpha: int,
    ) -> None:
        """
        Draw text with a black outline (stroke) and white fill — like TV
        subtitles. The outline wraps every letter evenly (no offset shadow),
        so the white text stays readable on any background.

        Implementation: add the text to a QPainterPath, then stroke the path
        with a thick black pen (the outline) and fill it with white (the text).
        """
        path = QPainterPath()
        path.addText(x, baseline, painter.font(), text)

        # Outline (stroke) — black, thick
        outline_color = QColor(SHADOW_COLOR)
        outline_color.setAlpha(alpha)
        outline_pen = QPen(outline_color)
        outline_pen.setWidthF(OUTLINE_WIDTH_PX)
        outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        outline_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(outline_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        # Fill (the actual letters) — white
        fill_color = QColor(TEXT_COLOR)
        fill_color.setAlpha(alpha)
        painter.setPen(QPen(fill_color))
        painter.setBrush(QBrush(fill_color))
        painter.drawPath(path)

    def _get_visible_window(self) -> list[tuple[int, str]]:
        """
        Determine which lyric lines should be visible based on the
        current active line index. Shows lines around the active one.

        Returns:
            List of (original_index, text) tuples to display.
        """
        if not self._lyric_lines:
            return []

        total = len(self._lyric_lines)
        if self._current_line_index < 0:
            # No active line yet — show the first few lines
            end = min(MAX_VISIBLE_LINES, total)
            return [(i, self._lyric_lines[i][1]) for i in range(end)]

        # Show a window of lines centered on the active line
        half = MAX_VISIBLE_LINES // 2
        start = max(0, self._current_line_index - half)
        end = min(total, start + MAX_VISIBLE_LINES)

        # Adjust start if we're near the end
        if end - start < MAX_VISIBLE_LINES:
            start = max(0, end - MAX_VISIBLE_LINES)

        return [(i, self._lyric_lines[i][1]) for i in range(start, end)]

    def _find_active_line(self) -> int:
        """
        Find the lyric line whose timestamp is <= current_time
        and is the most recent one.

        Returns:
            Index of the active line, or -1 if no line is active yet.
        """
        if not self._lyric_lines:
            return -1

        active = -1
        for i, (ts, _text) in enumerate(self._lyric_lines):
            if ts <= self._current_time:
                active = i
            else:
                break  # Lines are sorted by timestamp

        return active

    # ─── Slots ───────────────────────────────────────────────

    @Slot()
    def _tick(self) -> None:
        """Called by the timer to refresh the active line and repaint."""
        new_index = self._find_active_line()
        if new_index != self._current_line_index:
            self._current_line_index = new_index
            self.update()  # Schedule a repaint

    @Slot()
    def _on_update_requested(self) -> None:
        """Handle cross-thread update signal."""
        new_index = self._find_active_line()
        if new_index != self._current_line_index:
            self._current_line_index = new_index
        self.update()

    # ─── Drag-and-Drop ───────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        """Start dragging on left mouse press."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:
        """Move the overlay with the cursor while the left button is held."""
        if self._drag_offset is not None and (
            event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:
        """Stop dragging and persist the new position."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            self._save_position()

    def _save_position(self) -> None:
        """Persist the overlay's current position so it survives restarts."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            POSITION_FILE.write_text(
                json.dumps({"x": self.x(), "y": self.y()})
            )
        except Exception:
            logger.debug("Could not save overlay position.", exc_info=True)

    def _load_position(self) -> None:
        """Restore the saved overlay position, if any."""
        try:
            if POSITION_FILE.exists():
                data = json.loads(POSITION_FILE.read_text())
                self.move(int(data["x"]), int(data["y"]))
                logger.info(
                    "Restored overlay position: (%d, %d)", data["x"], data["y"]
                )
        except Exception:
            logger.debug("Could not load overlay position.", exc_info=True)

    def closeEvent(self, event) -> None:
        """Save the position on close so the next launch restores it."""
        self._save_position()
        super().closeEvent(event)

    # ─── Window styles (platform-specific) ───────────────────

    def showEvent(self, event) -> None:
        """Apply transparency / no-focus styles after the window is shown."""
        super().showEvent(event)
        self._apply_window_styles()

    def _apply_window_styles(self) -> None:
        """
        Set Win32 extended styles for the overlay.

        WS_EX_LAYERED    → required for per-pixel alpha (transparent background).
        WS_EX_NOACTIVATE → window never steals focus (your game keeps focus
                           even when you click/drag the overlay).

        Note: WS_EX_TRANSPARENT (click-through) is intentionally NOT set, so
        the overlay stays grabbable for drag-and-drop at all times.
        """
        if sys.platform != "win32":
            logger.debug("Window styles are only applied on Windows.")
            return

        try:
            import win32gui
            import win32con

            hwnd = int(self.winId())

            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style |= win32con.WS_EX_LAYERED
            ex_style |= win32con.WS_EX_NOACTIVATE
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

        except ImportError:
            logger.warning("pywin32 not available — window styles not applied.")
        except Exception:
            logger.exception("Failed to apply window styles.")
