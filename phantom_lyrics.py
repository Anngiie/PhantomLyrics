"""
Phantom Lyrics - Main Application
===================================
Entry point for the Phantom Lyrics desktop overlay application.

Architecture
------------
  ┌──────────────────────────────────────────────────────┐
  │                    phantom_lyrics.py                  │
  │  (Main Thread — PySide6 Event Loop)                  │
  │                                                      │
  │  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
  │  │  Overlay     │  │  WebSocket   │  │  Browser   │ │
  │  │  (PySide6)   │  │  Server      │  │  Monitor   │ │
  │  │              │  │  (Thread)    │  │  (Thread)  │ │
  │  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘ │
  │         │                 │                │         │
  │         │    timestamp    │   song change  │         │
  │         │◄────────────────┤◄───────────────┘         │
  │         │                 │                          │
  │         │         ┌──────┴────────┐                 │
  │         │         │ Lyrics Fetcher │                 │
  │         │         │ (LRCLib API)   │                 │
  │         │         └───────────────┘                 │
  └──────────────────────────────────────────────────────┘

  ┌──────────────────┐
  │  Firefox Add-on  │
  │  (content.js)    │── WebSocket ──► ws://localhost:8765
  └──────────────────┘

Usage
-----
    python phantom_lyrics.py

    Then load the Firefox extension manually:
    1. Open Firefox → about:debugging#/runtime/this-firefox
    2. "Load Temporary Add-on"
    3. Select firefox_extension/manifest.json
    4. Navigate to any YouTube music video
    5. The overlay will show lyrics automatically
"""

import logging
import signal
import sys
import threading
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from overlay import LyricsOverlay
from websocket_server import LyricsWebSocketServer
from browser_monitor import BrowserMonitor, clean_youtube_title, split_artist_title
from lyrics_fetcher import search_lyrics

# ─── Logging ────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("phantom_lyrics")


# ─── Main Application Controller ─────────────────────────────────


class PhantomLyricsApp:
    """
    Orchestrates all components of the Phantom Lyrics application.

    Responsibilities:
      - Creates and manages the overlay window.
      - Starts/stops the WebSocket server.
      - Starts/stops the browser title monitor.
      - Bridges data between threads safely (WebSocket → overlay,
        monitor → lyrics fetch → overlay).
    """

    def __init__(self) -> None:
        self._qt_app = QApplication(sys.argv)
        self._qt_app.setApplicationName("Phantom Lyrics")

        self._overlay = LyricsOverlay()
        self._ws_server: Optional[LyricsWebSocketServer] = None
        self._browser_monitor: Optional[BrowserMonitor] = None
        self._pending_song: Optional[tuple[str, str]] = None
        self._fetch_lock = threading.Lock()
        self._current_artist: str = ""
        self._current_title: str = ""

    # ─── Lifecycle ──────────────────────────────────────────

    def run(self) -> int:
        """Start everything and enter the Qt event loop."""
        logger.info("=" * 50)
        logger.info("  Phantom Lyrics — Ghost Overlay for YouTube Music")
        logger.info("=" * 50)

        # 1. Show the overlay window (empty, waiting for lyrics)
        self._overlay.show()
        logger.info("Overlay window shown.")

        # 2. Start the WebSocket server for timestamp data
        self._ws_server = LyricsWebSocketServer(
            host="localhost",
            port=8765,
            on_timestamp=self._on_timestamp,
        )
        self._ws_server.start()

        # 3. Start the browser title monitor
        self._browser_monitor = BrowserMonitor(
            on_song_change=self._on_song_change,
            poll_interval=2.0,
        )
        self._browser_monitor.start()

        # 4. Periodic health check (update connection status)
        self._health_timer = QTimer()
        self._health_timer.timeout.connect(self._health_check)
        self._health_timer.start(2000)

        # 5. Handle Ctrl+C gracefully
        signal.signal(signal.SIGINT, self._handle_sigint)
        # On Windows, Qt needs a timer to process Python signals
        self._sig_timer = QTimer()
        self._sig_timer.timeout.connect(lambda: None)  # No-op, just lets signals through
        self._sig_timer.start(200)

        # 6. Enter Qt event loop (blocks until quit)
        exit_code = self._qt_app.exec()

        # 7. Cleanup
        self._shutdown()
        return exit_code

    def _shutdown(self) -> None:
        """Gracefully stop all background services."""
        logger.info("Shutting down...")
        if self._browser_monitor:
            self._browser_monitor.stop()
        if self._ws_server:
            self._ws_server.stop()
        logger.info("Phantom Lyrics exited cleanly.")

    def _handle_sigint(self, signum, frame) -> None:
        """Handle Ctrl+C by quitting the Qt event loop."""
        logger.info("Ctrl+C received, quitting...")
        self._qt_app.quit()

    # ─── Event Handlers ─────────────────────────────────────

    def _on_timestamp(self, data: dict) -> None:
        """
        Called from the WebSocket server thread when the extension
        sends a new timestamp.

        The extension sends the YouTube page title every second, even when
        the YouTube tab isn't the active Firefox tab. We use that title to
        detect the current song — this means lyrics load automatically on
        app startup without needing to focus the YouTube tab.

        Args:
            data: JSON payload from the browser extension:
                  {currentTime, duration, paused, title}
        """
        current_time = data.get("currentTime", 0)
        is_paused = data.get("paused", False)
        ext_title = data.get("title", "")

        # Detect the song from the extension's page title. This works even
        # when the YouTube tab is in the background (the extension runs on
        # the page regardless and sends document.title every second).
        if ext_title:
            cleaned = clean_youtube_title(ext_title)
            if cleaned:
                artist, title = split_artist_title(cleaned)
                if title:
                    self._on_song_change(artist, title)

        if is_paused:
            return  # Don't advance lyrics while paused

        # Forward to overlay (thread-safe via Qt signal)
        self._overlay.set_timestamp(current_time)

    def _on_song_change(self, artist: str, title: str) -> None:
        """
        Called from the browser monitor thread when a new song is detected
        in the Firefox window title.

        Triggers a background lyrics fetch.

        Args:
            artist: Detected artist name.
            title: Detected song title.
        """
        if not title:
            return

        # Avoid re-fetching the same song
        if artist == self._current_artist and title == self._current_title:
            return

        self._current_artist = artist
        self._current_title = title

        # Fetch lyrics in a background thread to avoid blocking Qt
        fetch_thread = threading.Thread(
            target=self._fetch_and_apply_lyrics,
            args=(artist, title),
            name=f"fetch-{artist}-{title}",
            daemon=True,
        )
        fetch_thread.start()

    def _fetch_and_apply_lyrics(self, artist: str, title: str) -> None:
        """
        Background thread: query LRCLib and push results to the overlay.

        Uses a lock to prevent concurrent fetches from stepping on
        each other (in case of rapid title changes).
        """
        with self._fetch_lock:
            result = search_lyrics(artist, title)

        if result is None:
            logger.info(f"No lyrics found for: {artist} - {title}")
            self._overlay.show_no_lyrics(artist, title)
            return

        if not result.has_synced_lyrics and not result.plain_lyrics:
            logger.info(f"Empty lyrics result for: {artist} - {title}")
            self._overlay.show_no_lyrics(result.artist, result.title)
            return

        # If we have synced lyrics, push them to the overlay
        if result.has_synced_lyrics:
            lyric_tuples = [(line.timestamp, line.text) for line in result.synced_lines]
            logger.info(
                f"Applying {len(lyric_tuples)} synced lines for '{result.title}'"
            )
            self._overlay.set_lyrics(result.artist, result.title, lyric_tuples)
        elif result.plain_lyrics:
            # Fallback: display unsynced lyrics as static lines
            # We fake timestamps (spaced 5 seconds apart) so the scroll
            # window still works.
            lines = [l.strip() for l in result.plain_lyrics.splitlines() if l.strip()]
            fake_tuples = [(i * 5.0, line) for i, line in enumerate(lines)]
            logger.info(
                f"Applying {len(fake_tuples)} unsynced lines for '{result.title}' (fallback)"
            )
            self._overlay.set_lyrics(result.artist, result.title, fake_tuples)

    # ─── Health / Status ─────────────────────────────────────

    def _health_check(self) -> None:
        """Periodically check the health of background services."""
        if self._ws_server:
            connected = self._ws_server.client_count > 0
            self._overlay.set_connected(connected)

            # Log status changes
            if connected:
                pass  # All good, nothing to log
            # (Silent — we don't want to spam the console)


# ─── Entry Point ─────────────────────────────────────────────────


def main() -> int:
    """Application entry point."""
    app = PhantomLyricsApp()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
