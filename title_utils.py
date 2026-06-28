"""
Phantom Lyrics - Title Utilities
=================================
Functions for cleaning YouTube page titles and splitting them into
(artist, title) pairs.

YouTube titles typically look like:
    "Artist - Song Name (Official Music Video) - YouTube"
    "Song Name - Artist (Lyrics) - YouTube — Mozilla Firefox"

We strip:
  - " — Mozilla Firefox" and " - YouTube" suffixes
  - Common video type tags: (Official Video), (Lyrics), [MV], etc.
  - Leading numeric prefixes like "(226)" (tab counter / playlist position)
  - Extra whitespace

The result is a clean "Artist - Song Name" string.
"""

import re

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
    # Remove leading numeric prefix like "(226)" (tab counter / playlist position)
    (re.compile(r"^\s*\(\d+\)\s*"), ""),
    # Collapse multiple spaces
    (re.compile(r"\s{2,}"), " "),
]

# Separator between artist and title: hyphen, en-dash (U+2013), or em-dash (U+2014)
_ARTIST_TITLE_SEPARATOR_RE = re.compile(r"\s*[-\u2013\u2014]\s*")

# YouTube auto-generated artist channels are named "Artist - Topic".
_TOPIC_SUFFIX_RE = re.compile(r"\s*[-\u2013\u2014]\s*Topic\s*$", re.IGNORECASE)


def clean_youtube_title(raw_title: str) -> str:
    """
    Clean a YouTube page title into a usable "Artist - Song Name" string.

    Args:
        raw_title: The raw page title (e.g. from document.title).

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
        # Can't split reliably, return the whole thing as the title
        return "", cleaned_title.strip()


def clean_artist(artist: str) -> str:
    """Tidy an artist string from MediaSession (drops YouTube's '- Topic')."""
    return _TOPIC_SUFFIX_RE.sub("", artist.strip()).strip()


def resolve_song(artist: str, title: str, has_metadata: bool) -> tuple[str, str]:
    """
    Work out (artist, title) for the playing track.

    With MediaSession metadata and a real artist, use them directly and only
    strip video tags from the title (no dash-splitting guesswork). Otherwise
    treat `title` as a combined page title and parse it the old way.
    """
    if has_metadata and artist.strip():
        return clean_artist(artist), clean_youtube_title(title)
    cleaned = clean_youtube_title(title)
    if not cleaned:
        return "", ""
    return split_artist_title(cleaned)
