"""
Phantom Lyrics - System Tray Icon
==================================
A system tray icon that lets the user control the app without the terminal:
  - Toggle overlay visibility (left-click the tray icon or menu)
  - Reset overlay position to the bottom-left default
  - Settings dialog (font, opacity, layout, auto-hide)
  - Quit the application

The icon is drawn programmatically (a white music note on transparent) so
there is no .ico file dependency — works in dev and in a PyInstaller build.
"""

import logging
import os
import sys

from PySide6.QtCore import QObject, QSize
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap, QColor, QPainterPath
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from overlay import LyricsOverlay
from config import Config
from settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)

# Tray icon size (pixels) — drawn at high resolution for crisp scaling
_ICON_SIZE = 64


def _get_icon_path() -> str:
    """
    Find notes.ico — works both when running from source (project folder)
    and when running as a PyInstaller exe (bundled in _internal/).
    """
    # When running from source, the icon is in the project root
    local_icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.ico")
    if os.path.exists(local_icon):
        return local_icon

    # When running as a PyInstaller exe, it's bundled in _internal/
    if hasattr(sys, "_MEIPASS"):
        bundled = os.path.join(sys._MEIPASS, "notes.ico")
        if os.path.exists(bundled):
            return bundled

    return ""


def _make_tray_icon() -> QIcon:
    """
    Load the tray icon from notes.ico. Falls back to a programmatically
    drawn music-note icon if the file isn't found.
    """
    icon_path = _get_icon_path()
    if icon_path:
        icon = QIcon(icon_path)
        if not icon.isNull():
            return icon
        logger.debug(f"notes.ico found but couldn't be loaded: {icon_path}")

    # Fallback: draw a simple music-note icon programmatically
    logger.info("Using fallback programmatic tray icon (notes.ico not found)")
    pixmap = QPixmap(QSize(_ICON_SIZE, _ICON_SIZE))
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(30, 30, 30, 230))
    painter.setPen(QColor(0, 0, 0, 0))
    painter.drawRoundedRect(4, 4, _ICON_SIZE - 8, _ICON_SIZE - 8, 14, 14)
    painter.setBrush(QColor(255, 255, 255, 255))
    painter.setPen(QColor(0, 0, 0, 0))
    painter.drawRect(34, 16, 4, 26)
    path = QPainterPath()
    path.moveTo(38, 16)
    path.cubicTo(50, 20, 48, 30, 40, 32)
    path.lineTo(38, 32)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawEllipse(24, 40, 12, 9)
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

    def __init__(self, overlay: LyricsOverlay, config: Config, on_quit) -> None:
        super().__init__()
        self._overlay = overlay
        self._config = config
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

        self._gaming_action = QAction("Lock Overlay", menu)
        self._gaming_action.setCheckable(True)
        self._gaming_action.triggered.connect(self._toggle_gaming_mode)
        menu.addAction(self._gaming_action)

        settings_action = QAction("Settings...", menu)
        settings_action.triggered.connect(self._open_settings)
        menu.addAction(settings_action)

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

    def _toggle_gaming_mode(self) -> None:
        """Toggle click-through gaming mode from the tray menu."""
        self._overlay.toggle_gaming_mode()
        self._gaming_action.setChecked(self._overlay.gaming_mode)

    def _open_settings(self) -> None:
        """Open the settings dialog and apply changes to the overlay."""
        dialog = SettingsDialog(self._config, parent=None)
        if dialog.exec():
            new_config = dialog.result_config()
            if new_config:
                self._config = new_config
                self._overlay.apply_config(new_config)
