"""
Phantom Lyrics - Main Application

Wires the pieces together: the transparent overlay, the local WebSocket server
that talks to the browser extension, and the background lyrics fetch. The
extension reports the playing track and accepts playback commands back.

Run with:  python phantom_lyrics.py
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
from spotify_monitor import SpotifyMonitor
from websocket_server import LyricsWebSocketServer
from title_utils import resolve_song
from lyrics_fetcher import search_lyrics, init_cache, save_sync_offset
from tray import TrayController
from config import load_config, Config

# ─── Logging ────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Also log to a file for persistent debugging
_log_file = logging.FileHandler("phantom_lyrics_debug.log", mode="w", encoding="utf-8")
_log_file.setLevel(logging.DEBUG)
_log_file.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(_log_file)

logger = logging.getLogger("phantom_lyrics")


# ─── Playback Helpers ────────────────────────────────────────────


def decide_lock(active_id, client_id, is_playing):
    """
    Decide what to do with a status message under the single-active-tab lock.

    The extension reports the real play/pause state, so we trust it directly
    instead of inferring playback from how fast currentTime moves. That keeps
    looping a song from ever looking like a stall.

    Returns (action, new_active_id) where action is:
      "claim"   no tab held the lock and this one is playing, so it takes it
      "hold"    this is the active tab and still playing, keep driving lyrics
      "release" this is the active tab but now paused, drop the lock (keep lyrics)
      "ignore"  another tab holds the lock, or nothing is playing yet
    """
    if active_id is None:
        return ("claim", client_id) if is_playing else ("ignore", None)
    if active_id == client_id:
        return ("hold", client_id) if is_playing else ("release", None)
    return ("ignore", active_id)


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
        # Transport buttons on the overlay drive the active browser tab
        self._overlay.transport_requested.connect(self._on_transport)
        self._overlay.seek_requested.connect(self._on_seek)
        self._ws_server: Optional[LyricsWebSocketServer] = None
        self._spotify_monitor: Optional[SpotifyMonitor] = None
        self._tray: Optional[TrayController] = None
        self._fetch_lock = threading.Lock()
        self._current_artist: str = ""
        self._current_title: str = ""
        # A newly-detected song must repeat once before we switch to it, so a
        # one-poll title blip (an ad, or a brief metadata change) can't tear the
        # current lyrics down mid-song.
        self._pending_song: Optional[tuple[str, str]] = None
        # Only one tab drives lyrics at a time (no flicker between tabs). The
        # tab's reported play/pause state decides who holds the lock.
        self._active_player_id: Optional[int] = None
        self._last_player_id: Optional[int] = None  # Persists after lock release
        self._player_lock = threading.Lock()   # guards _active_player_id
    # ─── Lifecycle ──────────────────────────────────────────

    def run(self) -> int:
        """Start everything and enter the Qt event loop."""
        logger.info("=" * 50)
        logger.info("  Phantom Lyrics — Ghost Overlay for YouTube Music")
        logger.info("=" * 50)

        # 0. Load cached lyrics from disk (instant load for known songs)
        init_cache()

        # 1. The overlay stays hidden until a song is actually playing, so an
        #    idle desktop shows no overlay (and no stray cursor or click area).
        logger.info("Overlay ready (hidden until a song plays).")

        # 2. Start the WebSocket server for timestamp data
        self._ws_server = LyricsWebSocketServer(
            host=self._config.ws_host,
            port=self._config.ws_port,
            on_timestamp=self._on_timestamp,
            on_disconnect=self._on_disconnect,
        )
        self._ws_server.start()

        # 3. Spotify (optional — only starts if a Client ID is configured)
        client_id = (self._config.spotify_client_id or "").strip()
        if client_id:
            logger.info("Spotify Client ID configured, attempting connection...")
            self._spotify_monitor = SpotifyMonitor(
                client_id=client_id,
                on_track_change=self._on_song_change,
                on_timestamp=self._on_spotify_timestamp,
            )
            if self._spotify_monitor.connect():
                self._spotify_monitor.start()
            else:
                logger.warning("Spotify connection failed — running YouTube-only.")
                self._spotify_monitor = None

        # 4. System tray icon (visibility toggle, reset position, settings, quit)
        self._tray = TrayController(
            self._overlay, self._config,
            on_quit=self._qt_app.quit,
            on_connect_spotify=self._connect_spotify,
            spotify_connected=bool(self._spotify_monitor),
        )
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
        if self._spotify_monitor:
            self._spotify_monitor.stop()
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
        Handle a status message from a browser tab (runs on the WS thread).

        Only one tab drives the lyrics at a time. The tab's reported play/pause
        state decides the lock (see decide_lock): the first playing tab claims
        it, a different tab is ignored, and the active tab releases the lock
        when it pauses (lyrics stay up) or disconnects.

        Payload: {currentTime, duration, paused, title, artist, album,
                  artwork, hasMetadata}
        """
        current_time = data.get("currentTime", 0.0)
        is_playing = not data.get("paused", False)

        # Any message means the extension is alive, so keep the overlay awake.
        self._overlay.mark_activity()

        with self._player_lock:
            action, self._active_player_id = decide_lock(
                self._active_player_id, client_id, is_playing
            )
            if action in ("claim", "hold"):
                self._last_player_id = client_id

        if action == "claim":
            logger.info("Locked onto tab %d", client_id)
        elif action == "hold":
            # Same active tab, still playing — keep driving lyrics.
            pass
        elif action == "release":
            # Active tab paused — push state so icon flips to play ▸,
            # then release lock but keep lyrics visible.
            self._overlay.set_playback_state(is_playing)
            return
        elif action == "ignore":
            # Another tab holds the lock, or nothing is playing yet.
            # Don't push playback state — only the locked tab controls the icon.
            return

        # Only the locked, playing tab updates the icon and timestamp.
        self._overlay.set_playback_state(is_playing)

        # Active, playing tab: detect the song and push the timestamp.
        artist, title = resolve_song(
            data.get("artist", "") or "",
            data.get("title", "") or "",
            bool(data.get("hasMetadata", False)),
        )
        if title:
            self._on_song_change(artist, title)

        self._overlay.set_timestamp(current_time)

    def _on_disconnect(self, client_id: int) -> None:
        """
        Release the lock if the active tab's connection closed (SPA navigation
        or tab closed), so another tab can take over.
        """
        with self._player_lock:
            if self._active_player_id == client_id:
                logger.info("Active tab %d disconnected, releasing lock", client_id)
                self._active_player_id = None
                self._overlay.show_loading()

    def _on_transport(self, command: str) -> None:
        """Forward a transport button press to the active tab's browser."""
        with self._player_lock:
            target = self._active_player_id or self._last_player_id
        if target is None or not self._ws_server:
            logger.debug("Transport '%s' ignored (no active tab)", command)
            return
        logger.info("Transport: %s -> tab %d", command, target)
        self._ws_server.send_command(target, {"command": command})

    def _on_seek(self, timestamp: float) -> None:
        """Seek the active tab's video to the given timestamp (seconds)."""
        with self._player_lock:
            target = self._active_player_id or self._last_player_id
        if target is None or not self._ws_server:
            logger.debug("Seek %.1fs ignored (no active tab)", timestamp)
            return
        logger.info("Seek: %.1fs -> tab %d", timestamp, target)
        self._ws_server.send_command(target, {"command": "seek", "time": timestamp})

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

        logger.debug("_on_song_change: artist=%r title=%r (current=%r/%r)", artist, title, self._current_artist, self._current_title)

        # Avoid re-fetching the same song
        if artist == self._current_artist and title == self._current_title:
            logger.debug("  → same song, skipping")
            self._pending_song = None
            return

        # Debounce: require a new song to be detected twice in a row before we
        # tear down the current lyrics. This absorbs transient title blips (an
        # ad, or YouTube momentarily rewriting document.title) that would
        # otherwise flip a playing song to a different/garbage one and flash
        # "No lyrics found" mid-song. The first song ever detected switches
        # immediately — there's nothing to protect yet.
        if self._current_title and (artist, title) != self._pending_song:
            logger.debug("  → first sighting, pending next poll")
            self._pending_song = (artist, title)
            return

        self._pending_song = None
        logger.info("🎵 New song: %s - %s", artist, title)
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

        # The song may have changed while this fetch was running (threading.Lock
        # isn't FIFO, so a slow fetch can finish last) — drop stale results so
        # they don't clobber the current song's lyrics.
        if (artist, title) != (self._current_artist, self._current_title):
            logger.debug(f"Discarding stale lyrics for: {artist} - {title}")
            return

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

    # ─── Spotify ─────────────────────────────────────────────

    def _on_spotify_timestamp(self, progress_seconds: float, is_playing: bool) -> None:
        """Handle a timestamp update from Spotify (runs on the monitor thread)."""
        self._overlay.mark_activity()
        self._overlay.set_timestamp(progress_seconds)
        self._overlay.set_playback_state(is_playing)

    def _connect_spotify(self) -> None:
        """Connect (or reconnect) to Spotify from the tray menu."""
        client_id = (self._config.spotify_client_id or "").strip()
        if not client_id:
            logger.warning("No Spotify Client ID configured — add it in Settings.")
            return
        if self._spotify_monitor and self._spotify_monitor.is_connected:
            logger.info("Spotify already connected.")
            return
        self._spotify_monitor = SpotifyMonitor(
            client_id=client_id,
            on_track_change=self._on_song_change,
            on_timestamp=self._on_spotify_timestamp,
        )
        if self._spotify_monitor.connect():
            self._spotify_monitor.start()
            logger.info("Spotify connected and monitoring started.")
        else:
            logger.warning("Spotify connection failed.")
            self._spotify_monitor = None


# ─── Entry Point ─────────────────────────────────────────────────


def main() -> int:
    """Application entry point."""
    app = PhantomLyricsApp()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
