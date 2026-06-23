"""
Phantom Lyrics - Browser Monitor
==================================
Periodically checks the active Firefox browser window title to detect
the currently playing YouTube song.

When a new song is detected, the monitor calls a callback so the main
application can fetch the lyrics and update the overlay.

Title Cleaning
--------------
YouTube titles typically look like:
    "Artist - Song Name (Official Music Video) - YouTube"
    "Song Name - Artist (Lyrics) - YouTube Mozilla Firefox"

We strip:
  - " - YouTube" and " - YouTube Mozilla Firefox" suffixes
  - Common video type tags: (Official Video), (Lyrics), [MV], etc.
  - Leading/trailing whitespace and extra spaces

The result is a clean "Artist - Song Name" string.
"""

import logging
import re
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Lazy import — win32gui is only available on Windows
_win32gui = None
_win32process = None


def _ensure_win32():
    """Lazy-load the win32 modules."""
    global _win32gui, _win32process
    if _win32gui is None:
        import win32gui as wg
        import win32process as wp

        _win32gui = wg
        _win32process = wp


# ─── Title Cleaning Patterns ───────────────────────────────────

# Patterns to remove from YouTube titles.
# These are compiled once and applied in order.
_CLEANUP_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Remove Firefox browser suffix first: " — Mozilla Firefox"
    # (Firefox uses an em-dash U+2014; also handle en-dash/hyphen variants)
    (re.compile(r"\s*[-\u2013\u2014]\s*Mozilla\s*Firefox\s*$", re.IGNORECASE), ""),
    # Remove YouTube page suffix: " - YouTube" (hyphen, en-dash, or em-dash)
    (re.compile(r"\s*[-\u2013\u2014]\s*YouTube\s*$", re.IGNORECASE), ""),
    # Remove common video-type tags (in parentheses)
    (re.compile(r"\s*\(Official\s*(Music\s*)?Video\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(Official\s*(Lyric\s*)?Video\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(Lyrics?\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(Audio\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(Official\s*Audio\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(Visuali[sz]er\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(Music\s*Video\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(HQ\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(HD\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(High\s*Quality\)", re.IGNORECASE), ""),
    (re.compile(r"\s*\(With\s*Lyrics\)", re.IGNORECASE), ""),
    # Remove bracketed tags
    (re.compile(r"\s*\[Official\s*(Music\s*)?Video\]", re.IGNORECASE), ""),
    (re.compile(r"\s*\[Lyrics?\]", re.IGNORECASE), ""),
    (re.compile(r"\s*\[MV\]", re.IGNORECASE), ""),
    (re.compile(r"\s*\[Audio\]", re.IGNORECASE), ""),
    # Remove common prefixes added by YouTube Music
    # (usually the title is clean, but sometimes there's extra)
    # Remove leading numeric prefix like "(226)" (tab counter / playlist position)
    (re.compile(r"^\s*\(\d+\)\s*"), ""),
    # Collapse multiple spaces
    (re.compile(r"\s{2,}"), " "),
]


def clean_youtube_title(raw_title: str) -> str:
    """
    Clean a YouTube / Firefox window title into a usable
    "Artist - Song Name" string.

    Args:
        raw_title: The raw window title from win32gui.

    Returns:
        Cleaned title, or empty string if it doesn't look like a song title.
    """
    title = raw_title.strip()

    for pattern, replacement in _CLEANUP_PATTERNS:
        title = pattern.sub(replacement, title)

    title = title.strip()

    # Remove stray hyphens at the end
    title = re.sub(r"\s*-\s*$", "", title)

    return title


# Separator between artist and title: hyphen, en-dash (U+2013), or em-dash (U+2014)
_ARTIST_TITLE_SEPARATOR_RE = re.compile(r"\s*[-\u2013\u2014]\s*")


def split_artist_title(cleaned_title: str) -> tuple[str, str]:
    """
    Split a cleaned title like "Artist - Song Name" into (artist, title).

    Uses the first dash (hyphen "-", en-dash "–", or em-dash "—") as the
    delimiter, since YouTube titles use various dash styles.

    Args:
        cleaned_title: Cleaned title string.

    Returns:
        Tuple of (artist, song_title).
    """
    match = _ARTIST_TITLE_SEPARATOR_RE.search(cleaned_title)
    if match:
        artist = cleaned_title[: match.start()].strip()
        title = cleaned_title[match.end() :].strip()
        return artist, title
    else:
        # Can't split reliably — return the whole thing as the title
        return "", cleaned_title.strip()


# ─── Browser Monitor ────────────────────────────────────────────


class BrowserMonitor:
    """
    Periodically polls Firefox window titles to detect YouTube song changes.

    Runs in a background daemon thread.

    Usage:
        monitor = BrowserMonitor(on_song_change=my_callback)
        monitor.start()
        ...
        monitor.stop()
    """

    # How often to poll window titles (seconds)
    POLL_INTERVAL = 2.0

    def __init__(
        self,
        on_song_change: Optional[Callable[[str, str], None]] = None,
        poll_interval: float = 2.0,
    ):
        """
        Args:
            on_song_change: Callback invoked as on_song_change(artist, title)
                            when a new song is detected.
            poll_interval: Seconds between title polls.
        """
        self.on_song_change = on_song_change
        self.poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._last_song_key: Optional[str] = None

    # ─── Public API ──────────────────────────────────────────

    def start(self) -> None:
        """Start the browser monitor in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Browser monitor is already running.")
            return

        self._running.set()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="browser-monitor-thread",
            daemon=True,
        )
        self._thread.start()
        logger.info("Browser monitor started (polling every %.1fs)", self.poll_interval)

    def stop(self) -> None:
        """Stop the browser monitor thread."""
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Browser monitor stopped.")

    # ─── Internals ───────────────────────────────────────────

    def _monitor_loop(self) -> None:
        """Main loop: poll Firefox title, detect changes, fire callback."""
        while self._running.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("Error in browser monitor loop")
            self._running.wait(self.poll_interval)

    def _poll_once(self) -> None:
        """Check the current Firefox window title and act on changes."""
        _ensure_win32()
        raw_title = self._get_firefox_youtube_title()
        if not raw_title:
            return  # No Firefox YouTube window found

        cleaned = clean_youtube_title(raw_title)
        if not cleaned:
            logger.debug(f"Title cleaned to empty: '{raw_title}'")
            return

        artist, title = split_artist_title(cleaned)
        song_key = f"{artist}|{title}"

        if song_key != self._last_song_key:
            self._last_song_key = song_key
            logger.info(f"Song detected: '{artist}' - '{title}' (raw: '{raw_title}')")

            if self.on_song_change:
                try:
                    self.on_song_change(artist, title)
                except Exception:
                    logger.exception("Error in on_song_change callback")

    @staticmethod
    def _get_firefox_youtube_title() -> Optional[str]:
        """
        Enumerate all top-level windows and find the first Firefox
        window whose title contains 'YouTube'.

        Returns:
            Window title string, or None if not found.
        """
        _ensure_win32()

        result: Optional[str] = None

        def _enum_callback(hwnd, _extra):
            nonlocal result
            if not _win32gui.IsWindowVisible(hwnd):
                return True  # Continue enumeration

            title = _win32gui.GetWindowText(hwnd)
            if not title:
                return True

            # Check if this is a Firefox window with YouTube in the title
            # Also check for "Mozilla Firefox" in the title
            lower_title = title.lower()
            if ("youtube" in lower_title) and ("mozilla firefox" in lower_title
                                               or "firefox" in lower_title):
                result = title
                return False  # Stop enumeration — we found one

            return True

        try:
            _win32gui.EnumWindows(_enum_callback, None)
        except Exception:
            logger.exception("Error enumerating windows")
            return None

        return result
