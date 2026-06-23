"""
Phantom Lyrics - System Tray Icon
==================================
A system tray icon that lets the user control the app without the terminal:
  - Toggle overlay visibility (left-click the tray icon or menu)
  - Reset overlay position to the bottom-left default
  - Quit the application

The icon is drawn programmatically (a white music note on transparent) so
there is no .ico file dependency — works in dev and in a PyInstaller build.
"""

import logging

from PySide6.QtCore import QObject, QSize
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap, QColor, QPainterPath
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from overlay import LyricsOverlay

logger = logging.getLogger(__name__)

# Tray icon size (pixels) — drawn at high resolution for crisp scaling
_ICON_SIZE = 64


def _make_tray_icon() -> QIcon:
    """
    Draw a simple music-note icon programmatically.
    White note on a dark circular background — readable in both light
    and dark system tray areas.
    """
    pixmap = QPixmap(QSize(_ICON_SIZE, _ICON_SIZE))
    pixmap.fill(QColor(0, 0, 0, 0))  # Transparent

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Dark rounded-square backdrop (so the note is visible on light trays)
    painter.setBrush(QColor(30, 30, 30, 230))
    painter.setPen(QColor(0, 0, 0, 0))
    painter.drawRoundedRect(4, 4, _ICON_SIZE - 8, _ICON_SIZE - 8, 14, 14)

    # Music note (two note heads + stems) in white
    painter.setBrush(QColor(255, 255, 255, 255))
    painter.setPen(QColor(0, 0, 0, 0))

    # Stem (vertical bar)
    painter.drawRect(34, 16, 4, 26)
    # Upper note flag
    path = QPainterPath()
    path.moveTo(38, 16)
    path.cubicTo(50, 20, 48, 30, 40, 32)
    path.lineTo(38, 32)
    path.closeSubpath()
    painter.drawPath(path)
    # Lower note head (ellipse)
    painter.drawEllipse(24, 40, 12, 9)
    # Upper note head (ellipse)
    painter.drawEllipse(34, 36, 12, 9)

    painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    """
    Owns the QSystemTrayIcon and wires its menu to the overlay + app quit.

    Args:
        overlay: The LyricsOverlay to control (show/hide/reset position).
        on_quit: Callback invoked when the user selects "Quit".
    """

    def __init__(self, overlay: LyricsOverlay, on_quit) -> None:
        super().__init__()
        self._overlay = overlay
        self._on_quit = on_quit
        self._tray: QSystemTrayIcon | None = None

    def setup(self) -> bool:
        """
        Create and show the tray icon. Returns False if the system tray
        is unavailable (e.g. headless), in which case the caller continues
        without a tray.
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("System tray not available — running without tray icon.")
            return False

        self._tray = QSystemTrayIcon(_make_tray_icon(), parent=self)
        self._tray.setToolTip("Phantom Lyrics")

        # ── Context menu (right-click) ──
        menu = QMenu()

        toggle_action = QAction("Hide overlay", menu)
        toggle_action.triggered.connect(self._toggle_visibility)
        menu.addAction(toggle_action)

        reset_action = QAction("Reset position", menu)
        reset_action.triggered.connect(self._overlay.reset_position)
        menu.addAction(reset_action)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._on_quit)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)

        # Left-click also toggles visibility
        self._tray.activated.connect(self._on_activated)

        self._tray.show()
        logger.info("System tray icon ready.")
        return True

    def _on_activated(self, reason) -> None:
        """Left-click toggles visibility; other reasons use the context menu."""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visibility()

    def _toggle_visibility(self) -> None:
        """Show or hide the overlay and update the menu label."""
        if self._overlay.isVisible():
            self._overlay.hide()
        else:
            self._overlay.show()
