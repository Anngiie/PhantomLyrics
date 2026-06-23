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
import time
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from overlay import LyricsOverlay
from websocket_server import LyricsWebSocketServer
from title_utils import clean_youtube_title, split_artist_title
from lyrics_fetcher import search_lyrics, init_cache, save_sync_offset
from tray import TrayController
from config import load_config, Config

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
        # Keep the app running when the overlay is hidden via the tray icon
        self._qt_app.setQuitOnLastWindowClosed(False)

        self._config = load_config()
        self._overlay = LyricsOverlay(self._config)
        # Persist sync offset changes to the lyrics cache
        self._overlay.sync_offset_changed.connect(self._on_sync_offset_changed)
        self._ws_server: Optional[LyricsWebSocketServer] = None
        self._tray: Optional[TrayController] = None
        self._fetch_lock = threading.Lock()
        self._current_artist: str = ""
        self._current_title: str = ""
        # Lock-on + time-advance verification:
        # Only one tab drives lyrics at a time (no flicker), and only a tab
        # whose currentTime is actually advancing can claim/hold the lock
        # (no frozen lyrics from glitchy background tabs).
        self._active_player_id: Optional[int] = None
        self._last_current_time: float = 0.0
        self._last_advance_time: float = 0.0   # monotonic time of last currentTime advance
        self._player_lock = threading.Lock()   # guards the three fields above

    # ─── Lifecycle ──────────────────────────────────────────

    def run(self) -> int:
        """Start everything and enter the Qt event loop."""
        logger.info("=" * 50)
        logger.info("  Phantom Lyrics — Ghost Overlay for YouTube Music")
        logger.info("=" * 50)

        # 0. Load cached lyrics from disk (instant load for known songs)
        init_cache()

        # 1. Show the overlay window (empty, waiting for lyrics)
        self._overlay.show()
        logger.info("Overlay window shown.")

        # 2. Start the WebSocket server for timestamp data
        self._ws_server = LyricsWebSocketServer(
            host=self._config.ws_host,
            port=self._config.ws_port,
            on_timestamp=self._on_timestamp,
            on_disconnect=self._on_disconnect,
        )
        self._ws_server.start()

        # 3. System tray icon (visibility toggle, reset position, settings, quit)
        self._tray = TrayController(self._overlay, self._config, on_quit=self._qt_app.quit)
        self._tray.setup()

        # 4. Handle Ctrl+C gracefully
        signal.signal(signal.SIGINT, self._handle_sigint)
        # On Windows, Qt needs a timer to process Python signals
        self._sig_timer = QTimer()
        self._sig_timer.timeout.connect(lambda: None)  # No-op, just lets signals through
        self._sig_timer.start(200)

        # 5. Enter Qt event loop (blocks until quit)
        exit_code = self._qt_app.exec()

        # 6. Cleanup
        self._shutdown()
        return exit_code

    def _shutdown(self) -> None:
        """Gracefully stop all background services."""
        logger.info("Shutting down...")
        if self._ws_server:
            self._ws_server.stop()
        logger.info("Phantom Lyrics exited cleanly.")

    def _handle_sigint(self, signum, frame) -> None:
        """Handle Ctrl+C by quitting the Qt event loop."""
        logger.info("Ctrl+C received, quitting...")
        self._qt_app.quit()

    # ─── Event Handlers ─────────────────────────────────────

    def _on_timestamp(self, data: dict, client_id: int) -> None:
        """
        Called from the WebSocket server thread when the extension
        sends a new timestamp.

        Uses lock-on + time-advance verification to handle multiple YouTube
        tabs cleanly:

          - Lock-on: only one tab drives lyrics at a time (no flicker between
            songs when several tabs report paused:false).
          - Time-advance: a tab can only claim or hold the lock if its
            currentTime is actually moving forward — this filters out glitchy
            background tabs that briefly report paused:false without playing,
            and prevents frozen lyrics.
          - Stale eviction: if the locked tab's currentTime stops advancing
            for _STALE_LOCK_TIMEOUT_S seconds, the lock is released so a
            genuinely-playing tab can take over.
          - Disconnect: if the locked tab's connection closes (SPA navigation,
            tab closed), the lock releases immediately.

        Args:
            data: JSON payload from the browser extension:
                  {currentTime, duration, paused, title}
            client_id: Unique ID of the WebSocket connection (identifies the tab).
        """
        current_time = data.get("currentTime", 0)
        is_paused = data.get("paused", False)
        ext_title = data.get("title", "")

        # Any WebSocket message means the extension is alive — mark activity
        # so the overlay doesn't auto-hide (even while paused).
        self._overlay.mark_activity()

        now = time.monotonic()
        is_advancing = False

        stale_timeout = self._config.stale_lock_timeout_s
        advance_epsilon = self._config.time_advance_epsilon

        with self._player_lock:
            # Determine whether this tab's playback is genuinely advancing.
            if not is_paused and (current_time - self._last_current_time) > advance_epsilon:
                is_advancing = True

            if self._active_player_id is None:
                # No active player — only claim the lock if this tab is
                # genuinely playing (currentTime advancing, not paused).
                if is_paused or not is_advancing:
                    return
                self._active_player_id = client_id
                self._last_current_time = current_time
                self._last_advance_time = now
                logger.info(f"Locked onto player tab {client_id}")
            elif self._active_player_id != client_id:
                # A different tab — ignore it entirely while we have a lock.
                return
            else:
                # This is the active player.
                if is_paused:
                    # Active player paused — release the lock so another tab
                    # can take over.
                    logger.info(f"Active player {client_id} paused — releasing lock")
                    self._active_player_id = None
                    self._last_current_time = 0.0
                    self._overlay.show_loading()
                    return

                # Evict if currentTime hasn't advanced for too long (stuck/glitchy).
                if is_advancing:
                    self._last_current_time = current_time
                    self._last_advance_time = now
                elif (now - self._last_advance_time) >= stale_timeout:
                    logger.info(
                        f"Active player {client_id} stale for {stale_timeout}s — "
                        f"releasing lock"
                    )
                    self._active_player_id = None
                    self._last_current_time = 0.0
                    self._overlay.show_loading()
                    return

        # From here on, this message is from the active player (playing).
        # Detect the song from the playing tab's page title.
        if ext_title:
            cleaned = clean_youtube_title(ext_title)
            if cleaned:
                artist, title = split_artist_title(cleaned)
                if title:
                    self._on_song_change(artist, title)

        # Forward to overlay (thread-safe via Qt signal)
        self._overlay.set_timestamp(current_time)

    def _on_disconnect(self, client_id: int) -> None:
        """
        Called from the WebSocket server thread when a client disconnects.

        If the disconnected client was the active player, release the lock so
        a new tab can claim it. Handles YouTube SPA navigation (the extension
        disconnects/reconnects on video change) and tab closure.
        """
        with self._player_lock:
            if self._active_player_id == client_id:
                logger.info(f"Active player {client_id} disconnected — releasing lock")
                self._active_player_id = None
                self._last_current_time = 0.0
                self._overlay.show_loading()

    def _on_song_change(self, artist: str, title: str) -> None:
        """
        Called when a new song is detected (via the WebSocket title).

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

        # Show "Loading..." immediately so the user knows lyrics are being fetched
        self._overlay.show_loading()

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
            self._overlay.set_lyrics(
                result.artist, result.title, lyric_tuples, result.sync_offset
            )
        elif result.plain_lyrics:
            # Fallback: display unsynced lyrics as static lines
            # We fake timestamps (spaced 5 seconds apart) so the scroll
            # window still works.
            lines = [l.strip() for l in result.plain_lyrics.splitlines() if l.strip()]
            fake_tuples = [(i * 5.0, line) for i, line in enumerate(lines)]
            logger.info(
                f"Applying {len(fake_tuples)} unsynced lines for '{result.title}' (fallback)"
            )
            self._overlay.set_lyrics(
                result.artist, result.title, fake_tuples, result.sync_offset
            )

    def _on_sync_offset_changed(self, artist: str, title: str, offset: float) -> None:
        """Persist a user-adjusted sync offset to the lyrics cache."""
        save_sync_offset(artist, title, offset)


# ─── Entry Point ─────────────────────────────────────────────────


def main() -> int:
    """Application entry point."""
    app = PhantomLyricsApp()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
