"""
Phantom Lyrics - Transparent Overlay Window
=============================================
A frameless, always-on-top, click-through, transparent PySide6 window
that displays song lyrics in a "ghost-like" style.

Features:
  - 100% transparent background (only the text is visible).
  - Click-through: mouse events pass through to whatever is behind.
  - Always-on-top: sits above League of Legends, IDEs, etc.
  - Active lyric line is brighter (~85% opacity), others are dimmer (~40%).
  - Auto-positions to the bottom-left corner of the screen.
  - Resizes vertically to fit the number of visible lyric lines.

Windows-specific:
  - Uses win32gui to set WS_EX_TRANSPARENT, WS_EX_LAYERED, and
    WS_EX_TOOLWINDOW (hides from taskbar/Alt+Tab).
"""

import logging
import sys
from typing import Optional

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
    QFontMetrics,
    QScreen,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
)

logger = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────

# Overlay dimensions and layout
OVERLAY_WIDTH = 600            # Fixed width in pixels
MAX_VISIBLE_LINES = 8         # How many lyric lines to show at once
LINE_SPACING_PX = 14          # Extra space between lines (added to font height)
SIDE_PADDING_PX = 20          # Horizontal padding from the window edge
BOTTOM_PADDING_PX = 40        # Vertical padding from the screen bottom
FONT_FAMILY = "Segoe UI"      # Clean, modern font available on Windows
FONT_SIZE = 16                # Base font size in points

# Opacity values (0-255 for QColor alpha)
ACTIVE_LINE_ALPHA = 220       # ~86% opacity — stands out clearly
INACTIVE_LINE_ALPHA = 110     # ~43% opacity — ghost-like, barely visible
SONG_INFO_ALPHA = 80          # ~31% opacity — very subtle song info

# Colors
TEXT_COLOR = QColor(255, 255, 255)  # Pure white, alpha applied per-line

# Update frequency for UI interpolation (ms)
TICK_INTERVAL_MS = 100


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
        self._connected: bool = False                     # WebSocket client connected?
        self._current_time: float = 0.0                   # Latest timestamp from WS

        # ── Setup ────────────────────────────────────────────
        self._init_window()
        self._init_timer()

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
        self._lyric_lines = lyric_lines
        self._current_line_index = -1
        self._current_time = 0.0
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

    def set_connected(self, connected: bool) -> None:
        """Update the WebSocket connection status indicator."""
        self._connected = connected

    def clear(self) -> None:
        """Clear all lyrics from the overlay."""
        self._lyric_lines = []
        self._current_line_index = -1
        self._song_artist = ""
        self._song_title = ""
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

        # Calculate window height based on max visible lines + song info
        font = QFont(FONT_FAMILY, FONT_SIZE)
        fm = QFontMetrics(font)
        line_height = fm.height() + LINE_SPACING_PX
        window_height = (MAX_VISIBLE_LINES * line_height) + SIDE_PADDING_PX  # extra for song info

        x = SIDE_PADDING_PX
        y = screen_geom.bottom() - window_height - BOTTOM_PADDING_PX

        self.setGeometry(x, y, OVERLAY_WIDTH, window_height)
        self.setFixedSize(OVERLAY_WIDTH, window_height)

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

        font = QFont(FONT_FAMILY, FONT_SIZE)
        fm = QFontMetrics(font)
        line_height = fm.height() + LINE_SPACING_PX

        # ── Song info line (very subtle) ──────────────────
        if self._song_title:
            painter.setFont(font)
            color = QColor(TEXT_COLOR)
            color.setAlpha(SONG_INFO_ALPHA)
            painter.setPen(QPen(color))
            info_text = f"{self._song_artist} — {self._song_title}" if self._song_artist else self._song_title
            # Elide if too long
            elided = fm.elidedText(info_text, Qt.TextElideMode.ElideRight, OVERLAY_WIDTH - 2 * SIDE_PADDING_PX)
            painter.drawText(SIDE_PADDING_PX, fm.ascent() + 4, elided)

        if not self._lyric_lines:
            painter.end()
            return

        # ── Lyric lines ───────────────────────────────────
        # Calculate which lines to show around the active line
        visible_lines = self._get_visible_window()

        y_offset = SIDE_PADDING_PX + line_height  # Start below song info

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

            color = QColor(TEXT_COLOR)
            color.setAlpha(alpha)
            painter.setPen(QPen(color))

            # Elide text if it overflows
            elided = fm.elidedText(line_text, Qt.TextElideMode.ElideRight, OVERLAY_WIDTH - 2 * SIDE_PADDING_PX)

            painter.drawText(SIDE_PADDING_PX, y_offset + fm.ascent(), elided)
            y_offset += line_height

        painter.end()

    # ─── Internals ────────────────────────────────────────────

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

    # ─── Click-through (platform-specific) ───────────────────

    def showEvent(self, event) -> None:
        """Apply click-through after the window is shown."""
        super().showEvent(event)
        self._apply_click_through()

    def _apply_click_through(self) -> None:
        """
        Make the window click-through on Windows using the Win32 API.

        WS_EX_TRANSPARENT  → mouse events pass through
        WS_EX_LAYERED      → required for per-pixel alpha / transparency
        WS_EX_TOOLWINDOW   → hides from Alt+Tab (already set via Qt.Tool)

        We also set WS_EX_NOACTIVATE to prevent the window from stealing
        focus when it becomes visible.
        """
        if sys.platform != "win32":
            logger.debug("Click-through is only supported on Windows.")
            return

        try:
            import win32gui
            import win32con

            hwnd = int(self.winId())

            # Get current extended styles
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)

            # Add click-through and layered styles
            ex_style |= win32con.WS_EX_TRANSPARENT
            ex_style |= win32con.WS_EX_LAYERED
            ex_style |= win32con.WS_EX_NOACTIVATE

            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

            logger.debug("Click-through applied (WS_EX_TRANSPARENT | WS_EX_LAYERED).")

        except ImportError:
            logger.warning("pywin32 not available — click-through not applied.")
        except Exception:
            logger.exception("Failed to apply click-through.")
