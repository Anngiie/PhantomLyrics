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
import time
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

# Message shown when LRCLib has no lyrics for the current song
NO_LYRICS_MESSAGE = "No lyrics found for this song"

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

# Persist the overlay position across runs
CONFIG_DIR = Path.home() / ".phantom_lyrics"
POSITION_FILE = CONFIG_DIR / "overlay_position.json"

# Gaming mode hotkey — toggles click-through so clicks pass through the
# overlay to the game behind it. Uses a global hotkey (pynput) so it works
# even when the game has keyboard focus.
GAMING_TOGGLE_HOTKEY = '<ctrl>+<alt>+space'


# ─── The Overlay Widget ────────────────────────────────────────


class LyricsOverlay(QWidget):
    """
    The main overlay widget. Renders lyrics text directly via paintEvent
    for maximum control over transparency and positioning.
    """

    # Signals to safely update UI state from any thread
    update_requested = Signal()
    sync_offset_changed = Signal(str, str, float)  # (artist, title, new_offset)
    gaming_toggle_requested = Signal()             # Global hotkey → Qt thread

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
        self._last_activity_time: float = 0.0             # Last WS message time (monotonic)
        self._target_opacity: float = AUTO_SHOWN_OPACITY  # Fade target
        self._sync_offset: float = 0.0                    # User-adjusted lyric offset (seconds)
        self._hovered: bool = False                       # Mouse is over the overlay?
        self._sync_btn_rects: dict[str, QRect] = {}       # Button hit-test rects (set in paintEvent)
        self._pressed_btn: str | None = None              # Which button was just pressed (flash effect)
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
        """
        Replace the current lyrics with a new song.

        Thread-safe — can be called from any thread.

        Args:
            artist: Artist name (for the subtle header).
            title: Song title (for the subtle header).
            lyric_lines: List of (timestamp_seconds, lyric_text) tuples,
                         sorted by timestamp.
            sync_offset: Saved lyric sync offset for this song (seconds).
        """
        self._song_artist = artist
        self._song_title = title
        # Filter out empty-text lines (instrumental breaks) so they don't
        # create blank rows — the last sung lyric stays in focus instead.
        self._lyric_lines = [line for line in lyric_lines if line[1].strip()]
        self._current_line_index = -1
        self._current_time = 0.0
        self._no_lyrics = False
        self._sync_offset = sync_offset
        self.update_requested.emit()

    def set_sync_offset(self, offset: float) -> None:
        """Set the lyric sync offset (seconds). Called from the Qt thread."""
        self._sync_offset = offset
        self.update()

    def set_timestamp(self, current_time: float) -> None:
        """
        Update the current playback position.

        Thread-safe — can be called from any thread.

        Args:
            current_time: Current playback position in seconds.
        """
        self._current_time = current_time
        self._last_activity_time = time.monotonic()
        self._target_opacity = AUTO_SHOWN_OPACITY
        self.update_requested.emit()

    def mark_activity(self) -> None:
        """
        Note that a WebSocket message arrived (music is playing).
        Thread-safe — can be called from any thread.
        """
        self._last_activity_time = time.monotonic()
        self._target_opacity = AUTO_SHOWN_OPACITY

    def set_visible(self, visible: bool) -> None:
        """Show or hide the overlay (for the tray icon toggle)."""
        if visible:
            self.show()
        else:
            self.hide()

    def apply_config(self, config: Config) -> None:
        """Apply updated config settings at runtime (from the settings dialog)."""
        self._cfg = config
        # Recalculate window size for the new font/layout settings
        font = QFont(config.font_family, config.font_size)
        fm = QFontMetrics(font)
        line_height = fm.height() + config.line_spacing_px
        buttons_space = SYNC_BTN_SIZE + SYNC_BTN_MARGIN
        window_height = (
            (config.max_visible_lines * line_height)
            + line_height
            + buttons_space
            + config.side_padding_px
        )
        self.setFixedSize(config.overlay_width, window_height)
        self.update()
        logger.info("Overlay config applied and resized.")

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
        font = QFont(self._cfg.font_family, self._cfg.font_size)
        fm = QFontMetrics(font)
        line_height = fm.height() + self._cfg.line_spacing_px
        # Height: song info header + lyric lines + space for sync buttons below
        buttons_space = SYNC_BTN_SIZE + SYNC_BTN_MARGIN
        window_height = (
            (self._cfg.max_visible_lines * line_height)
            + line_height
            + buttons_space
            + self._cfg.side_padding_px
        )

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

        # Connect signals for cross-thread updates
        self.update_requested.connect(self._on_update_requested)
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

        # ── Sync nudge buttons (when hovering, or during feedback) ───
        show_buttons = self._hovered or (
            self._feedback_text and time.monotonic() < self._feedback_until
        )
        if show_buttons:
            self._draw_sync_buttons(painter, fm, y_offset)

        painter.end()

    # ─── Internals ────────────────────────────────────────────

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

        # Clear the pressed-flash after 150ms
        pressed = self._pressed_btn
        if pressed and time.monotonic() >= self._feedback_until - 1.85:
            pressed = None
            self._pressed_btn = None

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
        """
        Determine which lyric lines should be visible based on the
        current active line index. Shows lines around the active one.

        Returns:
            List of (original_index, text) tuples to display.
        """
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
        """
        Find the lyric line whose timestamp is <= (current_time + sync_offset)
        and is the most recent one.

        Returns:
            Index of the active line, or -1 if no line is active yet.
        """
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

        # ── Active line advancement ──
        new_index = self._find_active_line()
        if new_index != self._current_line_index:
            self._current_line_index = new_index
        self.update()  # Repaint every tick (opacity fade needs it)

    @Slot()
    def _on_update_requested(self) -> None:
        """Handle cross-thread update signal."""
        new_index = self._find_active_line()
        if new_index != self._current_line_index:
            self._current_line_index = new_index
        self.update()

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
            # Check if the click landed on a sync button
            for btn_id, rect in self._sync_btn_rects.items():
                if rect.contains(pos):
                    self._handle_sync_button(btn_id)
                    return
            # Not on a button — start dragging
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
        self._pressed_btn = btn_id
        self._feedback_text = f"Sync: {self._sync_offset:+.1f}s"
        self._feedback_until = time.monotonic() + 2.0  # toast visible for 2s

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
        """Apply transparency / no-focus styles after the window is shown."""
        super().showEvent(event)
        self._apply_window_styles(click_through=False)

    def _apply_window_styles(self, click_through: bool = False) -> None:
        """
        Set Win32 extended styles for the overlay.

        WS_EX_LAYERED    → required for per-pixel alpha (transparent background).
        WS_EX_NOACTIVATE → window never steals focus (your game keeps focus
                           even when you click/drag the overlay).
        WS_EX_TRANSPARENT → click-through: mouse events pass through to the
                            game behind the overlay (gaming mode).

        Args:
            click_through: True for gaming mode (clicks pass through);
                           False for draggable mode (overlay is grabbable).
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

            if click_through:
                ex_style |= win32con.WS_EX_TRANSPARENT
            else:
                ex_style &= ~win32con.WS_EX_TRANSPARENT

            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

            # Force Windows to re-read the extended style and re-evaluate
            # hit-testing. Without this, the click-through change may not
            # take effect (especially noticeable in fullscreen games like LoL).
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                win32con.SWP_NOMOVE
                | win32con.SWP_NOSIZE
                | win32con.SWP_NOZORDER
                | win32con.SWP_NOACTIVATE
                | win32con.SWP_FRAMECHANGED,
            )

            logger.debug(f"Window styles applied (click_through={click_through})")

        except ImportError:
            logger.warning("pywin32 not available — window styles not applied.")
        except Exception:
            logger.exception("Failed to apply window styles.")
