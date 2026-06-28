"""
Phantom Lyrics - Transparent Overlay Window

A frameless, always-on-top, transparent window that paints the lyrics directly,
with hover controls for sync offset and playback. The whole window is draggable.
Transparency and click-through use win32 on Windows and Qt flags elsewhere.
"""

import json
import logging
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    Qt,
    QTimer,
    QRect,
    QRectF,
    QPointF,
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
    QPolygonF,
    QFontMetrics,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
)

from config import Config

logger = logging.getLogger(__name__)

# ─── Internal Constants (not user-tweakable) ───────────────────

# Colors
TEXT_COLOR = QColor(255, 255, 255)  # Pure white, alpha applied per-line
SHADOW_COLOR = QColor(0, 0, 0)      # Black outline for readability on light bg

# Grab handle: a near-invisible fill (alpha 1) painted across the whole window
# so Windows delivers mouse events to every pixel (layered windows hit-test by
# pixel alpha; fully transparent areas would otherwise ignore clicks, forcing
# you to grab the letters precisely). Alpha 1/255 is invisible to the eye.
GRAB_FILL_COLOR = QColor(0, 0, 0, 1)

# Messages shown on the overlay
NO_LYRICS_MESSAGE = "No lyrics found for this song"
LOADING_MESSAGE = "Loading..."

# Update frequency for UI interpolation (ms)
TICK_INTERVAL_MS = 100

# Auto-hide fade parameters
AUTO_HIDE_FADE_STEP = 0.05    # Opacity delta per tick (smooth fade)
AUTO_HIDDEN_OPACITY = 0.0     # Fully hidden when faded out
AUTO_SHOWN_OPACITY = 1.0      # Fully visible when faded in

# Sync offset nudge buttons (shown on hover)
SYNC_NUDGE_STEP = 0.5         # Seconds per +/- press
SYNC_BTN_SIZE = 22            # Button side length in pixels
SYNC_BTN_SPACING = 6          # Gap between buttons
SYNC_BTN_MARGIN = 8           # Margin from the overlay's top-right edge

# Toast text shown briefly after a transport button press
_TRANSPORT_LABELS = {
    "prev": "Previous",
    "playpause": "Play / Pause",
    "next": "Next",
}

# Persist the overlay position across runs
CONFIG_DIR = Path.home() / ".phantom_lyrics"
POSITION_FILE = CONFIG_DIR / "overlay_position.json"

# Gaming mode hotkey — toggles click-through so clicks pass through the
# overlay to the game behind it. Uses a global hotkey (pynput) so it works
# even when the game has keyboard focus.
GAMING_TOGGLE_HOTKEY = '<ctrl>+<alt>+<space>'


# ─── The Overlay Widget ────────────────────────────────────────


class LyricsOverlay(QWidget):
    """
    The main overlay widget. Renders lyrics text directly via paintEvent
    for maximum control over transparency and positioning.
    """

    # Signals to safely update UI state from worker threads. Each public
    # mutator below just emits; the connected @Slot applies the change on the
    # Qt thread (a queued connection), so a repaint never sees half-applied
    # state (e.g. new lyric lines against a stale highlight index).
    lyrics_received = Signal(str, str, object, float)  # artist, title, lines, offset
    loading_requested = Signal()
    no_lyrics_requested = Signal(str, str)             # artist, title
    timestamp_received = Signal(float)                 # current playback time (s)
    playback_state_received = Signal(bool)             # True when video is playing
    activity_pinged = Signal()
    sync_offset_changed = Signal(str, str, float)      # (artist, title, new_offset)
    gaming_toggle_requested = Signal()                 # global hotkey to Qt thread
    transport_requested = Signal(str)                  # "prev" / "playpause" / "next"

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._cfg = config

        # ── Window state ──────────────────────────────────────
        self._lyric_lines: list[tuple[float, str]] = []  # [(timestamp, text), ...]
        self._current_line_index: int = -1               # Which line is active
        self._song_artist: str = ""
        self._song_title: str = ""
        self._current_time: float = 0.0                   # Latest timestamp from WS
        self._no_lyrics: bool = False                     # Show "no lyrics" message?
        self._loading: bool = False                        # Show "loading..." message?
        self._last_activity_time: float = 0.0             # Last WS message time (monotonic)
        self._target_opacity: float = AUTO_SHOWN_OPACITY  # Fade target
        self._paused: bool = False                         # Video playback state (for button icon)
        self._sync_offset: float = 0.0                    # User-adjusted lyric offset (seconds)
        self._hovered: bool = False                       # Mouse is over the overlay?
        self._sync_btn_rects: dict[str, QRect] = {}       # Button hit-test rects (set in paintEvent)
        self._transport_btn_rects: dict[str, QRect] = {}  # Transport button hit-test rects
        self._pressed_btn: str | None = None              # Which button is flashing as pressed
        self._pressed_until: float = 0.0                  # Monotonic time the press flash ends
        self._feedback_text: str = ""                     # Temporary toast text (e.g. "Sync: +1.0s")
        self._feedback_until: float = 0.0                 # Monotonic time when the toast expires
        self._gaming_mode: bool = False                   # Click-through lock for gaming
        self._hotkey_listener = None                      # pynput global hotkey listener

        # ── Drag state ───────────────────────────────────────
        self._drag_offset = None  # QPoint: cursor-to-window-origin offset while dragging

        # ── Setup ────────────────────────────────────────────
        self._init_window()
        self._init_timer()
        self._init_hotkey()

        # The whole overlay is grabbable — hint with a move cursor
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        logger.info("Lyrics overlay initialized.")

    # ─── Public API ──────────────────────────────────────────

    def set_lyrics(
        self,
        artist: str,
        title: str,
        lyric_lines: list[tuple[float, str]],
        sync_offset: float = 0.0,
    ) -> None:
        """Replace the current lyrics with a new song (thread-safe). lyric_lines
        is a list of (timestamp_seconds, text) tuples sorted by timestamp."""
        self.lyrics_received.emit(artist, title, lyric_lines, sync_offset)

    def set_sync_offset(self, offset: float) -> None:
        """Set the lyric sync offset (seconds). Called from the Qt thread."""
        self._sync_offset = offset
        self.update()

    def set_timestamp(self, current_time: float) -> None:
        """Update the current playback position, in seconds (thread-safe)."""
        self.timestamp_received.emit(current_time)

    def set_playback_state(self, playing: bool) -> None:
        """Update whether the video is playing (thread-safe). Controls the transport icon."""
        self.playback_state_received.emit(playing)

    def mark_activity(self) -> None:
        """Note an extension message arrived, to keep the overlay awake (thread-safe)."""
        self.activity_pinged.emit()

    def set_visible(self, visible: bool) -> None:
        """Show or hide the overlay (for the tray icon toggle)."""
        if visible:
            self.show()
        else:
            self.hide()

    def apply_config(self, config: Config) -> None:
        """Apply updated settings from the settings dialog and resize."""
        self._cfg = config
        self.setFixedSize(config.overlay_width, self._compute_height(config))
        self.update()
        logger.info("Overlay config applied and resized.")

    def _compute_height(self, cfg: Config) -> int:
        """Window height: title row + lyric lines + transport row + sync row + toast."""
        fm = QFontMetrics(QFont(cfg.font_family, cfg.font_size))
        line_height = fm.height() + cfg.line_spacing_px
        button_rows = 2 * (SYNC_BTN_SIZE + SYNC_BTN_MARGIN) + line_height
        return (
            (cfg.max_visible_lines * line_height)
            + line_height
            + button_rows
            + cfg.side_padding_px
        )

    def reset_position(self) -> None:
        """Move the overlay back to its default bottom-left position."""
        screen = QApplication.primaryScreen()
        if screen:
            screen_geom: QRect = screen.availableGeometry()
        else:
            screen_geom = QRect(0, 0, 1920, 1080)
        x = self._cfg.side_padding_px
        y = screen_geom.bottom() - self.height() - self._cfg.bottom_padding_px
        self.move(x, y)
        self._save_position()
        logger.info("Overlay position reset to bottom-left.")

    def show_no_lyrics(self, artist: str, title: str) -> None:
        """Show a "No lyrics found" message for the given song (thread-safe)."""
        self.no_lyrics_requested.emit(artist, title)

    def show_loading(self) -> None:
        """Show a "Loading..." message while lyrics are fetched (thread-safe)."""
        self.loading_requested.emit()

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

        window_height = self._compute_height(self._cfg)

        x = self._cfg.side_padding_px
        y = screen_geom.bottom() - window_height - self._cfg.bottom_padding_px

        self.setGeometry(x, y, self._cfg.overlay_width, window_height)
        self.setFixedSize(self._cfg.overlay_width, window_height)

        # Restore the last saved position (overrides the default bottom-left)
        self._load_position()

    def _init_timer(self) -> None:
        """Set up the refresh timer for smooth UI updates."""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(TICK_INTERVAL_MS)
        self._timer.start()

        # Connect signals for cross-thread updates. Worker threads emit; these
        # slots run on the Qt thread (queued), so state is applied atomically
        # with respect to painting.
        self.lyrics_received.connect(self._apply_lyrics)
        self.loading_requested.connect(self._apply_loading)
        self.no_lyrics_requested.connect(self._apply_no_lyrics)
        self.timestamp_received.connect(self._apply_timestamp)
        self.playback_state_received.connect(self._apply_playback_state)
        self.activity_pinged.connect(self._apply_activity)
        self.gaming_toggle_requested.connect(self._on_gaming_toggle)

    def _init_hotkey(self) -> None:
        """Register a global hotkey to toggle gaming (click-through) mode."""
        try:
            from pynput import keyboard
        except ImportError:
            logger.warning(
                "pynput not installed — gaming-mode hotkey disabled. "
                "Install with: pip install pynput"
            )
            return

        try:
            self._hotkey_listener = keyboard.GlobalHotKeys(
                {GAMING_TOGGLE_HOTKEY: self._on_hotkey_pressed}
            )
            self._hotkey_listener.start()
            logger.info("Gaming toggle hotkey registered: %s", GAMING_TOGGLE_HOTKEY)
        except Exception:
            logger.exception("Could not register global hotkey.")
            self._hotkey_listener = None

    def _on_hotkey_pressed(self) -> None:
        """Runs on the pynput thread — marshal to the Qt thread via signal."""
        self.gaming_toggle_requested.emit()

    def toggle_gaming_mode(self) -> None:
        """Public toggle — callable from the tray icon (no hotkey needed)."""
        self._on_gaming_toggle()

    @property
    def gaming_mode(self) -> bool:
        """Whether gaming (click-through) mode is currently active."""
        return self._gaming_mode

    @Slot()
    def _on_gaming_toggle(self) -> None:
        """Toggle between draggable and click-through (gaming) modes."""
        self._gaming_mode = not self._gaming_mode
        self._apply_window_styles(click_through=self._gaming_mode)

        if self._gaming_mode:
            self.unsetCursor()
            self._feedback_text = "Gaming mode ON — click-through active"
        else:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self._feedback_text = "Gaming mode OFF — draggable"

        self._feedback_until = time.monotonic() + 2.0
        logger.info(self._feedback_text)
        self.update()

    # ─── Event Overrides ──────────────────────────────────────

    def paintEvent(self, event) -> None:
        """Custom paint: draw lyrics text with varying opacity."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Near-invisible fill across the whole window so every pixel is
        # grabbable for drag-and-drop (layered windows hit-test by pixel alpha;
        # fully transparent areas would otherwise ignore mouse clicks).
        painter.fillRect(self.rect(), GRAB_FILL_COLOR)

        font = QFont(self._cfg.font_family, self._cfg.font_size)
        fm = QFontMetrics(font)
        line_height = fm.height() + self._cfg.line_spacing_px

        overlay_width = self._cfg.overlay_width
        side_padding = self._cfg.side_padding_px

        def center_x(text: str) -> int:
            """Horizontal x so text is centered within the overlay width."""
            return (overlay_width - fm.horizontalAdvance(text)) // 2

        # ── Song info line (only on hover, for a clean minimalist look) ──
        # Space for the title is always reserved so lyrics don't shift when
        # it appears/disappears — only the text is painted on hover.
        # The title uses a smaller font than the lyrics.
        if self._hovered and self._song_title:
            title_font = QFont(self._cfg.font_family, max(self._cfg.font_size - 3, 8))
            title_fm = QFontMetrics(title_font)
            painter.setFont(title_font)
            info_text = f"{self._song_artist} — {self._song_title}" if self._song_artist else self._song_title
            # Center the smaller title vertically in the reserved line_height space
            title_baseline = 4 + (line_height - title_fm.height()) // 2 + title_fm.ascent()
            title_x = (overlay_width - title_fm.horizontalAdvance(info_text)) // 2
            self._draw_outlined_text(painter, title_x, title_baseline, info_text, self._cfg.song_info_alpha)

        # Lyrics always start at the same fixed position (title space reserved)
        lyrics_top = line_height + self._cfg.title_gap_px

        # ── "No lyrics found" message ─────────────────────
        if self._no_lyrics:
            painter.setFont(font)
            y_offset = lyrics_top
            self._draw_outlined_text(
                painter, center_x(NO_LYRICS_MESSAGE), y_offset + fm.ascent(), NO_LYRICS_MESSAGE, self._cfg.inactive_line_alpha
            )
            painter.end()
            return

        # ── "Loading..." message (waiting for lock-on or lyrics fetch) ──
        if self._loading:
            painter.setFont(font)
            y_offset = lyrics_top
            self._draw_outlined_text(
                painter, center_x(LOADING_MESSAGE), y_offset + fm.ascent(), LOADING_MESSAGE, self._cfg.inactive_line_alpha
            )
            painter.end()
            return

        if not self._lyric_lines:
            painter.end()
            return

        # ── Lyric lines ───────────────────────────────────
        visible_lines = self._get_visible_window()

        y_offset = lyrics_top

        for idx, line_text in visible_lines:
            painter.setFont(font)

            # Determine opacity based on whether this is the active line
            if idx == self._current_line_index:
                alpha = self._cfg.active_line_alpha
            else:
                alpha = self._cfg.inactive_line_alpha
                # Optional: gradient fade for lines further from active
                distance = abs(idx - self._current_line_index)
                if distance > 0:
                    # Reduce alpha by 15 per step away, floor at 25
                    fade = max(25, self._cfg.inactive_line_alpha - (distance - 1) * 15)
                    alpha = fade

            self._draw_outlined_text(painter, center_x(line_text), y_offset + fm.ascent(), line_text, alpha)
            y_offset += line_height

        # ── Transport + sync buttons (when hovering, or during feedback) ───
        show_buttons = self._hovered or (
            self._feedback_text and time.monotonic() < self._feedback_until
        )
        if show_buttons:
            transport_bottom = self._draw_transport_buttons(painter, y_offset)
            self._draw_sync_buttons(painter, fm, transport_bottom)
        else:
            self._transport_btn_rects.clear()
            self._sync_btn_rects.clear()

        painter.end()

    # ─── Internals ────────────────────────────────────────────

    def _draw_transport_buttons(self, painter: QPainter, top_y: int) -> int:
        """
        Draw previous / play-pause / next centered below the lyrics. Icons are
        drawn as vector shapes so they look the same regardless of system fonts.
        A just-pressed button flashes brighter. Returns the row's bottom y.
        """
        size = SYNC_BTN_SIZE
        spacing = SYNC_BTN_SPACING
        ids = ["prev", "playpause", "next"]
        total_width = len(ids) * size + (len(ids) - 1) * spacing
        start_x = (self._cfg.overlay_width - total_width) // 2
        btn_y = top_y + SYNC_BTN_MARGIN
        pressed = self._pressed_btn if time.monotonic() < self._pressed_until else None

        self._transport_btn_rects.clear()
        for i, btn_id in enumerate(ids):
            x = start_x + i * (size + spacing)
            rect = QRect(x, btn_y, size, size)
            self._transport_btn_rects[btn_id] = rect
            if btn_id == pressed:
                painter.setPen(QPen(QColor(255, 255, 255, 140)))
                painter.setBrush(QBrush(QColor(255, 255, 255, 90)))
            else:
                painter.setPen(QPen(QColor(255, 255, 255, 40)))
                painter.setBrush(QBrush(QColor(0, 0, 0, 120)))
            painter.drawRoundedRect(rect, 4, 4)
            self._draw_transport_icon(painter, rect, btn_id, pressed=(btn_id == pressed))
        return btn_y + size

    def _draw_transport_icon(self, painter: QPainter, rect: QRect, kind: str, pressed: bool = False) -> None:
        """Paint a transport glyph (play/pause/next/prev) as vector shapes."""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 255 if pressed else 220)))
        cx = rect.center().x() + 0.5
        cy = rect.center().y() + 0.5
        u = rect.width() * 0.16  # icon unit

        def triangle(points: list[tuple[float, float]]) -> None:
            painter.drawPolygon(QPolygonF([QPointF(px, py) for px, py in points]))

        def bar(x0: float) -> None:
            painter.drawRect(QRectF(x0, cy - 1.3 * u, 0.5 * u, 2.6 * u))

        if kind == "playpause":
            if self._paused:
                # Paused — draw a play triangle ▸
                triangle([(cx - 1.1 * u, cy - 1.3 * u), (cx - 1.1 * u, cy + 1.3 * u), (cx + 1.3 * u, cy)])
            else:
                # Playing — draw two pause bars ▍▍
                bar(cx - 0.7 * u)
                bar(cx + 0.2 * u)
        elif kind == "next":
            triangle([(cx - 1.3 * u, cy - 1.3 * u), (cx - 1.3 * u, cy + 1.3 * u), (cx + 0.7 * u, cy)])
            bar(cx + 0.9 * u)
        elif kind == "prev":
            triangle([(cx + 1.3 * u, cy - 1.3 * u), (cx + 1.3 * u, cy + 1.3 * u), (cx - 0.7 * u, cy)])
            bar(cx - 1.4 * u)

        painter.restore()

    def _draw_sync_buttons(self, painter: QPainter, fm: QFontMetrics, lyrics_bottom: int) -> None:
        """
        Draw small [−] [0] [+] buttons centered below the lyrics, plus a sync
        offset indicator. Shown when hovering or during the 2s feedback window
        after a press. Button rects are stored for hit-testing in mousePressEvent.

        Args:
            lyrics_bottom: Y coordinate of the bottom of the last lyric line.
        """
        btn_font = QFont(self._cfg.font_family, max(self._cfg.font_size - 4, 8))
        size = SYNC_BTN_SIZE
        spacing = SYNC_BTN_SPACING

        # Three buttons: [−] [0] [+]
        labels = [("minus", "\u2212"), ("reset", "0"), ("plus", "+")]
        total_width = len(labels) * size + (len(labels) - 1) * spacing
        start_x = (self._cfg.overlay_width - total_width) // 2
        btn_y = lyrics_bottom + SYNC_BTN_MARGIN

        self._sync_btn_rects.clear()

        painter.setFont(btn_font)

        pressed = self._pressed_btn if time.monotonic() < self._pressed_until else None

        for i, (btn_id, label) in enumerate(labels):
            x = start_x + i * (size + spacing)
            rect = QRect(x, btn_y, size, size)
            self._sync_btn_rects[btn_id] = rect

            # Button background — brighter when pressed
            if btn_id == pressed:
                painter.setPen(QPen(QColor(255, 255, 255, 120)))
                painter.setBrush(QBrush(QColor(255, 255, 255, 80)))
            else:
                painter.setPen(QPen(QColor(255, 255, 255, 40)))
                painter.setBrush(QBrush(QColor(0, 0, 0, 120)))
            painter.drawRoundedRect(rect, 4, 4)

            # Button label — brighter when pressed
            label_alpha = 255 if btn_id == pressed else 200
            painter.setPen(QPen(QColor(255, 255, 255, label_alpha)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # Sync offset toast — shows for 2s after a press (even if not hovering)
        if self._feedback_text and time.monotonic() < self._feedback_until:
            indicator_font = QFont(self._cfg.font_family, max(self._cfg.font_size - 4, 8))
            painter.setFont(indicator_font)
            painter.setPen(QPen(QColor(255, 255, 255, 220)))
            ind_fm = QFontMetrics(indicator_font)
            text_width = ind_fm.horizontalAdvance(self._feedback_text)
            ix = (self._cfg.overlay_width - text_width) // 2
            iy = btn_y + size + 4 + ind_fm.ascent()
            painter.drawText(ix, iy, self._feedback_text)
        elif self._hovered and abs(self._sync_offset) > 0.01:
            # While hovering with a non-zero offset, show it subtly
            offset_text = f"Sync: {self._sync_offset:+.1f}s"
            indicator_font = QFont(self._cfg.font_family, max(self._cfg.font_size - 4, 8))
            painter.setFont(indicator_font)
            painter.setPen(QPen(QColor(255, 255, 255, 160)))
            ind_fm = QFontMetrics(indicator_font)
            text_width = ind_fm.horizontalAdvance(offset_text)
            ix = (self._cfg.overlay_width - text_width) // 2
            iy = btn_y + size + 4 + ind_fm.ascent()
            painter.drawText(ix, iy, offset_text)

    def _draw_outlined_text(
        self,
        painter: QPainter,
        x: int,
        baseline: int,
        text: str,
        alpha: int,
    ) -> None:
        """Draw white text with a thick black outline (subtitle style) by stroking
        and filling a QPainterPath, so it stays readable on any background."""
        path = QPainterPath()
        path.addText(x, baseline, painter.font(), text)

        # Outline (stroke) — black, thick
        outline_color = QColor(SHADOW_COLOR)
        outline_color.setAlpha(alpha)
        outline_pen = QPen(outline_color)
        outline_pen.setWidthF(self._cfg.outline_width_px)
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
        """Lyric lines to show as (original_index, text), windowed around the active line."""
        if not self._lyric_lines:
            return []

        total = len(self._lyric_lines)
        max_lines = self._cfg.max_visible_lines
        if self._current_line_index < 0:
            # No active line yet — show the first few lines
            end = min(max_lines, total)
            return [(i, self._lyric_lines[i][1]) for i in range(end)]

        # Show a window of lines centered on the active line
        half = max_lines // 2
        start = max(0, self._current_line_index - half)
        end = min(total, start + max_lines)

        # Adjust start if we're near the end
        if end - start < max_lines:
            start = max(0, end - max_lines)

        return [(i, self._lyric_lines[i][1]) for i in range(start, end)]

    def _find_active_line(self) -> int:
        """Index of the latest lyric line at or before (current_time + sync_offset), or -1."""
        if not self._lyric_lines:
            return -1

        effective_time = self._current_time + self._sync_offset
        active = -1
        for i, (ts, _text) in enumerate(self._lyric_lines):
            if ts <= effective_time:
                active = i
            else:
                break  # Lines are sorted by timestamp

        return active

    # ─── Slots ───────────────────────────────────────────────

    @Slot()
    def _tick(self) -> None:
        """Called by the timer to refresh the active line, auto-hide, and repaint."""
        # ── Auto-hide: fade out if no activity for auto_hide_timeout_s ──
        if self._last_activity_time > 0:
            idle = time.monotonic() - self._last_activity_time
            if idle >= self._cfg.auto_hide_timeout_s:
                self._target_opacity = AUTO_HIDDEN_OPACITY

        # Smoothly approach the target opacity
        current = self.windowOpacity()
        if current < self._target_opacity:
            current = min(self._target_opacity, current + AUTO_HIDE_FADE_STEP)
            self.setWindowOpacity(current)
        elif current > self._target_opacity:
            current = max(self._target_opacity, current - AUTO_HIDE_FADE_STEP)
            self.setWindowOpacity(current)

        # Once fully faded out, hide the window so it leaves no cursor or
        # click-catching area behind (no song means no overlay).
        if self._target_opacity <= 0.0 and current <= 0.0 and self.isVisible():
            self.hide()

        # ── Active line advancement ──
        new_index = self._find_active_line()
        if new_index != self._current_line_index:
            self._current_line_index = new_index
        self.update()  # Repaint every tick (opacity fade needs it)

    def _has_content(self) -> bool:
        """Whether there's anything to show (lyrics, loading, or no-lyrics)."""
        return bool(self._lyric_lines or self._loading or self._no_lyrics)

    def _ensure_visible(self) -> None:
        """Show the overlay and fade it in. Called when content arrives."""
        self._target_opacity = AUTO_SHOWN_OPACITY
        self._last_activity_time = time.monotonic()
        if not self.isVisible():
            self.show()

    @Slot(str, str, object, float)
    def _apply_lyrics(
        self,
        artist: str,
        title: str,
        lyric_lines: list[tuple[float, str]],
        sync_offset: float,
    ) -> None:
        """Apply a fetched song's lyrics (runs on the Qt thread)."""
        self._song_artist = artist
        self._song_title = title
        # Filter out empty-text lines (instrumental breaks) so they don't
        # create blank rows — the last sung lyric stays in focus instead.
        self._lyric_lines = [line for line in lyric_lines if line[1].strip()]
        self._current_time = 0.0
        self._no_lyrics = False
        self._loading = False
        self._sync_offset = sync_offset
        self._current_line_index = self._find_active_line()
        self._ensure_visible()
        self.update()

    @Slot()
    def _apply_loading(self) -> None:
        """Show the 'Loading...' message (runs on the Qt thread)."""
        self._lyric_lines = []
        self._current_line_index = -1
        self._current_time = 0.0
        self._no_lyrics = False
        self._loading = True
        self._ensure_visible()
        self.update()

    @Slot(str, str)
    def _apply_no_lyrics(self, artist: str, title: str) -> None:
        """Show the 'No lyrics found' message (runs on the Qt thread)."""
        self._song_artist = artist
        self._song_title = title
        self._lyric_lines = []
        self._current_line_index = -1
        self._current_time = 0.0
        self._no_lyrics = True
        self._loading = False
        self._ensure_visible()
        self.update()

    @Slot(float)
    def _apply_timestamp(self, current_time: float) -> None:
        """Apply a new playback position (runs on the Qt thread)."""
        self._current_time = current_time
        self._last_activity_time = time.monotonic()
        self._target_opacity = AUTO_SHOWN_OPACITY
        if self._has_content() and not self.isVisible():
            self.show()
        new_index = self._find_active_line()
        if new_index != self._current_line_index:
            self._current_line_index = new_index
        self.update()

    @Slot(bool)
    def _apply_playback_state(self, playing: bool) -> None:
        """Apply the video play/pause state (runs on the Qt thread)."""
        self._paused = not playing
        self.update()

    @Slot()
    def _apply_activity(self) -> None:
        """Keep the overlay awake when an extension message arrives."""
        self._last_activity_time = time.monotonic()
        self._target_opacity = AUTO_SHOWN_OPACITY
        if self._has_content() and not self.isVisible():
            self.show()

    # ─── Drag-and-Drop + Hover Buttons ──────────────────────

    def enterEvent(self, event) -> None:
        """Mouse entered the overlay — show sync buttons."""
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        """Mouse left the overlay — hide sync buttons."""
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        """Handle sync button clicks, or start dragging if not on a button."""
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            # Transport button?
            for btn_id, rect in self._transport_btn_rects.items():
                if rect.contains(pos):
                    self.transport_requested.emit(btn_id)
                    now = time.monotonic()
                    self._pressed_btn = btn_id
                    self._pressed_until = now + 0.18
                    self._feedback_text = _TRANSPORT_LABELS.get(btn_id, btn_id)
                    self._feedback_until = now + 1.2
                    self.update()
                    return
            # Sync nudge button?
            for btn_id, rect in self._sync_btn_rects.items():
                if rect.contains(pos):
                    self._handle_sync_button(btn_id)
                    return
            # Not on a button, start dragging
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

    def _handle_sync_button(self, btn_id: str) -> None:
        """Handle a sync nudge button click with visual feedback."""
        if btn_id == "minus":
            self._sync_offset -= SYNC_NUDGE_STEP
        elif btn_id == "plus":
            self._sync_offset += SYNC_NUDGE_STEP
        elif btn_id == "reset":
            self._sync_offset = 0.0
        else:
            return

        # Flash the pressed button + show a temporary toast
        now = time.monotonic()
        self._pressed_btn = btn_id
        self._pressed_until = now + 0.18
        self._feedback_text = f"Sync: {self._sync_offset:+.1f}s"
        self._feedback_until = now + 2.0  # toast visible for 2s

        # Notify the main app to persist the offset in the cache
        self.sync_offset_changed.emit(
            self._song_artist, self._song_title, self._sync_offset
        )
        logger.info(f"Sync offset set to {self._sync_offset:+.1f}s")
        self.update()

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
        """Save the position and stop the hotkey listener on close."""
        self._save_position()
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
        super().closeEvent(event)

    # ─── Window styles (platform-specific) ───────────────────

    def showEvent(self, event) -> None:
        """Re-apply platform window styles after the window is shown."""
        super().showEvent(event)
        self._apply_window_styles(click_through=self._gaming_mode)

    def _apply_window_styles(self, click_through: bool = False) -> None:
        """Apply click-through (gaming mode). win32 on Windows, Qt flags elsewhere."""
        if sys.platform == "win32":
            self._apply_win32_styles(click_through)
        else:
            self._apply_qt_click_through(click_through)

    def _apply_qt_click_through(self, click_through: bool) -> None:
        """Cross-platform click-through via Qt (Linux/macOS)."""
        flag = Qt.WindowType.WindowTransparentForInput
        if bool(self.windowFlags() & flag) == click_through:
            return  # already in the desired state; avoid a needless re-show
        geo = self.geometry()
        self.setWindowFlag(flag, click_through)
        self.setGeometry(geo)
        self.show()  # re-show so the flag change takes effect

    def _apply_win32_styles(self, click_through: bool) -> None:
        """
        Windows extended styles: WS_EX_LAYERED (per-pixel alpha), WS_EX_NOACTIVATE
        (never steal focus), and WS_EX_TRANSPARENT for click-through. This path is
        kept on Windows because it behaves best over fullscreen games.
        """
        try:
            import win32gui
            import win32con

            hwnd = int(self.winId())
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style |= win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE
            if click_through:
                ex_style |= win32con.WS_EX_TRANSPARENT
            else:
                ex_style &= ~win32con.WS_EX_TRANSPARENT
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

            # Force Windows to re-read the style and re-evaluate hit-testing.
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
                | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED,
            )
            logger.debug("win32 styles applied (click_through=%s)", click_through)
        except ImportError:
            logger.warning("pywin32 not available, window styles not applied.")
        except Exception:
            logger.exception("Failed to apply win32 window styles.")
