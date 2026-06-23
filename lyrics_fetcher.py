"""
Phantom Lyrics - Lyrics Fetcher
=================================
Handles querying the LRCLib API to fetch synchronized lyrics (LRC format),
parsing the LRC timestamps, and storing the result for the UI to display.

LRCLib API: https://lrclib.net
  - Search: GET https://lrclib.net/api/search?q=artist+title
  - Get by ID: GET https://lrclib.net/api/get/{id}

LRC Format (example):
    [00:12.00] First line of the song
    [00:15.50] Second line of the song
    [00:18.20] Third line with more words

Each line is stored as a tuple: (timestamp_seconds, text)
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ─── Data Structures ───────────────────────────────────────────


@dataclass
class LyricLine:
    """A single line of lyrics with its timestamp in seconds."""

    timestamp: float  # Time in seconds when this line should appear
    text: str         # The lyric text (empty string for instrumental breaks)


@dataclass
class LyricsResult:
    """The result of a lyrics fetch, including parsed lines and metadata."""

    title: str = ""
    artist: str = ""
    synced_lines: list[LyricLine] = field(default_factory=list)
    plain_lyrics: str = ""  # Fallback: unsynced lyrics
    source_url: str = ""    # URL of the LRCLib entry (useful for debugging)
    fetched_at: float = 0.0  # Unix timestamp of when this was fetched

    @property
    def has_synced_lyrics(self) -> bool:
        """Whether we have timestamped (synced) lyrics."""
        return len(self.synced_lines) > 0

    @property
    def duration_hint(self) -> float:
        """The timestamp of the last line — useful as a rough song duration."""
        if self.synced_lines:
            return self.synced_lines[-1].timestamp
        return 0.0


# ─── LRC Parser ────────────────────────────────────────────────

# Matches LRC timestamp tags like [00:12.50] or [01:23.45]
_LRC_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]")


def parse_lrc(lrc_text: str) -> list[LyricLine]:
    """
    Parse an LRC (LyRiCs) formatted string into a list of LyricLine objects.

    Handles:
      - Standard [mm:ss.xx] timestamps
      - Multiple timestamps per line (the line is duplicated for each)
      - Empty lines / instrumental markers
      - Metadata tags ([ti:...], [ar:...], etc.) are ignored

    Args:
        lrc_text: Raw LRC string, one line per timestamp group.

    Returns:
        Sorted list of LyricLine objects by timestamp.
    """
    lines: list[LyricLine] = []

    for raw_line in lrc_text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        # Find all timestamp tags on this line
        tags = list(_LRC_TAG_RE.finditer(raw_line))

        if not tags:
            continue  # Metadata line or blank

        # Extract the lyric text (everything after the last tag)
        last_tag_end = tags[-1].end()
        text = raw_line[last_tag_end:].strip()

        # If multiple tags, create a line for each tag
        for tag in tags:
            minutes = int(tag.group(1))
            seconds = int(tag.group(2))
            centis = tag.group(3)
            centiseconds = int(centis.ljust(2, "0")[:2]) if centis else 0
            timestamp = minutes * 60.0 + seconds + centiseconds / 100.0
            lines.append(LyricLine(timestamp=timestamp, text=text))

    # Sort by timestamp — important for correct display order
    lines.sort(key=lambda l: l.timestamp)
    return lines


# ─── LRCLib API Client ─────────────────────────────────────────


# Use a session for connection reuse across requests
_session = requests.Session()
_session.headers.update(
    {
        "User-Agent": "PhantomLyrics/1.0 (Desktop Overlay; +https://github.com/phantom-lyrics)",
        "Accept": "application/json",
    }
)

# Simple in-memory cache to avoid re-fetching the same song
_cache: dict[str, LyricsResult] = {}
_MAX_CACHE_SIZE = 100

# Disk cache — persists across runs so previously played songs load instantly
# and work offline.
_CACHE_DIR = Path.home() / ".phantom_lyrics"
_CACHE_FILE = _CACHE_DIR / "lyrics_cache.json"


def _serialize_result(result: LyricsResult) -> dict:
    """Serialize a LyricsResult to a JSON-compatible dict."""
    return {
        "title": result.title,
        "artist": result.artist,
        "synced_lines": [{"timestamp": l.timestamp, "text": l.text} for l in result.synced_lines],
        "plain_lyrics": result.plain_lyrics,
        "source_url": result.source_url,
        "fetched_at": result.fetched_at,
    }


def _deserialize_result(data: dict) -> LyricsResult:
    """Reconstruct a LyricsResult from a dict (disk cache load)."""
    return LyricsResult(
        title=data.get("title", ""),
        artist=data.get("artist", ""),
        synced_lines=[LyricLine(timestamp=l["timestamp"], text=l["text"]) for l in data.get("synced_lines", [])],
        plain_lyrics=data.get("plain_lyrics", ""),
        source_url=data.get("source_url", ""),
        fetched_at=data.get("fetched_at", 0.0),
    )


def _save_cache_to_disk() -> None:
    """Write the in-memory cache to disk so it survives restarts."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {key: _serialize_result(r) for key, r in _cache.items()}
        _CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.debug("Could not write lyrics cache to disk.", exc_info=True)


def _load_cache_from_disk() -> None:
    """Load the disk cache into memory on startup."""
    try:
        if _CACHE_FILE.exists():
            payload = json.loads(_CACHE_FILE.read_text())
            for key, data in payload.items():
                _cache[key] = _deserialize_result(data)
            logger.info(f"Loaded {len(_cache)} cached lyrics from disk.")
    except Exception:
        logger.debug("Could not load lyrics cache from disk.", exc_info=True)


def init_cache() -> None:
    """Load the disk cache into memory. Call once on app startup."""
    _load_cache_from_disk()


def _cache_key(artist: str, title: str) -> str:
    """Normalize artist + title into a cache key."""
    return f"{artist.lower().strip()}|{title.lower().strip()}"


def search_lyrics(artist: str, title: str) -> Optional[LyricsResult]:
    """
    Search LRCLib for synchronized lyrics matching the given artist and title.

    Strategy:
      1. Check in-memory cache.
      2. Search LRCLib: GET /api/search?q=artist+title
      3. Pick the best result (preferring synced lyrics).
      4. If the search result has synced lyrics inline, use them.
         Otherwise, fetch by ID: GET /api/get/{id}
      5. Cache and return.

    Args:
        artist: Artist name (e.g., "Linkin Park").
        title: Song title (e.g., "In the End").

    Returns:
        LyricsResult if found, None otherwise.
    """
    key = _cache_key(artist, title)

    # 1. Cache check
    if key in _cache:
        logger.debug(f"Cache hit: {artist} - {title}")
        return _cache[key]

    logger.info(f"Searching LRCLib for: {artist} - {title}")
    query = f"{artist} {title}"

    try:
        resp = _session.get(
            "https://lrclib.net/api/search",
            params={"q": query},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.error(f"LRCLib search failed: {e}")
        return None

    if not results:
        logger.info(f"No results found for: {artist} - {title}")
        return None

    # 2. Pick the best result
    best = _pick_best_result(results, title, artist)

    # 3. Build the result
    synced = best.get("syncedLyrics") or ""
    plain = best.get("plainLyrics") or ""

    lyric_lines = parse_lrc(synced) if synced else []

    result = LyricsResult(
        title=best.get("trackName", title),
        artist=best.get("artistName", artist),
        synced_lines=lyric_lines,
        plain_lyrics=plain,
        source_url=f"https://lrclib.net/api/get/{best.get('id', '')}",
        fetched_at=time.time(),
    )

    # 4. Handle edge case: search returned no synced lyrics, but has an ID
    #    → try the direct GET endpoint which sometimes has more data.
    if not result.has_synced_lyrics and best.get("id"):
        logger.debug("Search returned plain lyrics only, trying direct fetch...")
        direct = _fetch_by_id(best["id"])
        if direct and direct.has_synced_lyrics:
            result = direct
            result.source_url = f"https://lrclib.net/api/get/{best['id']}"

    # 5. Cache (in-memory + disk)
    if len(_cache) >= _MAX_CACHE_SIZE:
        # Evict oldest entry (simple FIFO)
        oldest = next(iter(_cache))
        del _cache[oldest]
    _cache[key] = result
    _save_cache_to_disk()

    if result.has_synced_lyrics:
        logger.info(
            f"Got synced lyrics for '{result.title}': "
            f"{len(result.synced_lines)} lines"
        )
    else:
        logger.info(f"Got plain (unsynced) lyrics for '{result.title}'")

    return result


def _pick_best_result(results: list[dict], title: str, artist: str) -> dict:
    """
    From the search results, pick the entry most likely to be correct.

    Heuristics (in order of importance):
      1. Has synced lyrics (syncedLyrics is non-empty).
      2. Title match quality (exact vs partial).
      3. Artist match quality (exact vs partial).
    """

    def score(r: dict) -> tuple:
        has_synced = bool(r.get("syncedLyrics"))
        title_match = r.get("trackName", "").lower() == title.lower()
        artist_match = r.get("artistName", "").lower() == artist.lower()
        # Sort key: (no_synced=higher-penalty, not_exact_title=penalty, not_exact_artist=penalty)
        return (not has_synced, not title_match, not artist_match)

    return min(results, key=score)


def _fetch_by_id(lrclib_id: int) -> Optional[LyricsResult]:
    """Fetch lyrics directly from LRCLib by ID."""
    try:
        resp = _session.get(
            f"https://lrclib.net/api/get/{lrclib_id}",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.error(f"LRCLib direct fetch failed (ID={lrclib_id}): {e}")
        return None

    synced = data.get("syncedLyrics") or ""
    return LyricsResult(
        title=data.get("trackName", ""),
        artist=data.get("artistName", ""),
        synced_lines=parse_lrc(synced) if synced else [],
        plain_lyrics=data.get("plainLyrics", ""),
        source_url=f"https://lrclib.net/api/get/{lrclib_id}",
        fetched_at=time.time(),
    )
