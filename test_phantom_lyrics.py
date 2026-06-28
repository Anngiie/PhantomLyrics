"""
Phantom Lyrics - Unit Tests
============================
Tests for the pure functions: title cleaning, artist/title splitting,
LRC parsing, and NetEase title similarity scoring.

Run with:
    python -m pytest test_phantom_lyrics.py -v

Or without pytest:
    python test_phantom_lyrics.py
"""

from title_utils import clean_youtube_title, split_artist_title, resolve_song, clean_artist
from lyrics_fetcher import parse_lrc, _title_similarity
from phantom_lyrics import decide_lock


# ─── clean_youtube_title ──────────────────────────────────────

def test_clean_basic_youtube_title():
    raw = "Linkin Park - In the End (Official Video) - YouTube"
    assert clean_youtube_title(raw) == "Linkin Park - In the End"

def test_clean_firefox_em_dash_suffix():
    raw = "Motörhead – The Hammer (Official Audio) - YouTube — Mozilla Firefox"
    assert clean_youtube_title(raw) == "Motörhead – The Hammer"

def test_clean_tab_counter_prefix():
    raw = "(226) Motörhead – The Hammer (Official Audio) - YouTube — Mozilla Firefox"
    assert clean_youtube_title(raw) == "Motörhead – The Hammer"

def test_clean_bracketed_tags():
    raw = "Artist - Song [MV] - YouTube"
    assert clean_youtube_title(raw) == "Artist - Song"

def test_clean_no_tags():
    raw = "Artist - Song Name"
    assert clean_youtube_title(raw) == "Artist - Song Name"

def test_clean_empty_string():
    assert clean_youtube_title("") == ""


# ─── split_artist_title ───────────────────────────────────────

def test_split_hyphen():
    artist, title = split_artist_title("Linkin Park - In the End")
    assert artist == "Linkin Park"
    assert title == "In the End"

def test_split_en_dash():
    artist, title = split_artist_title("Motörhead – The Hammer")
    assert artist == "Motörhead"
    assert title == "The Hammer"

def test_split_em_dash():
    artist, title = split_artist_title("Artist — Song")
    assert artist == "Artist"
    assert title == "Song"

def test_split_no_separator():
    artist, title = split_artist_title("Just A Title")
    assert artist == ""
    assert title == "Just A Title"


# ─── parse_lrc ────────────────────────────────────────────────

def test_parse_basic_lrc():
    lrc = "[00:12.00]First line\n[00:15.50]Second line\n[00:18.20]Third line"
    lines = parse_lrc(lrc)
    assert len(lines) == 3
    assert lines[0].timestamp == 12.0
    assert lines[0].text == "First line"
    assert lines[1].timestamp == 15.5
    assert lines[2].timestamp == 18.2

def test_parse_skips_metadata_tags():
    lrc = "[ti:Song Title]\n[ar:Artist]\n[00:12.00]Actual lyric"
    lines = parse_lrc(lrc)
    assert len(lines) == 1
    assert lines[0].text == "Actual lyric"

def test_parse_empty_string():
    assert parse_lrc("") == []

def test_parse_multiple_timestamps_per_line():
    lrc = "[00:12.00][00:45.00]Repeated line"
    lines = parse_lrc(lrc)
    assert len(lines) == 2
    assert lines[0].timestamp == 12.0
    assert lines[1].timestamp == 45.0
    assert lines[0].text == "Repeated line"
    assert lines[1].text == "Repeated line"

def test_parse_variable_fraction_digits():
    lines = parse_lrc("[00:10.5]Tenths\n[00:11.34]Centis\n[00:12.345]Millis")
    assert lines[0].timestamp == 10.5
    assert abs(lines[1].timestamp - 11.34) < 1e-9
    assert abs(lines[2].timestamp - 12.345) < 1e-9


# ─── _title_similarity ────────────────────────────────────────

def test_similarity_exact_match():
    assert _title_similarity("In the End", "In the End") == 1.0

def test_similarity_partial_match():
    assert _title_similarity("In the End (Remix)", "In the End") == 0.8

def test_similarity_no_match():
    assert _title_similarity("Completely Different", "In the End") == 0.0


# ─── resolve_song ─────────────────────────────────────────────

def test_resolve_uses_metadata_when_present():
    assert resolve_song("Linkin Park", "In the End", True) == ("Linkin Park", "In the End")

def test_resolve_strips_topic_suffix():
    assert resolve_song("Adele - Topic", "Hello", True) == ("Adele", "Hello")

def test_resolve_strips_tags_from_metadata_title():
    assert resolve_song("Adele", "Hello (Official Video)", True) == ("Adele", "Hello")

def test_resolve_falls_back_to_title_parsing():
    # No usable metadata: parse a combined "Artist - Title - YouTube" string.
    assert resolve_song("", "Adele - Hello - YouTube", False) == ("Adele", "Hello")

def test_resolve_metadata_without_artist_falls_back():
    assert resolve_song("", "Adele - Hello", True) == ("Adele", "Hello")

def test_clean_artist_plain():
    assert clean_artist("Linkin Park") == "Linkin Park"


# ─── decide_lock ──────────────────────────────────────────────

def test_lock_claim_when_idle_and_playing():
    assert decide_lock(None, 1, True) == ("claim", 1)

def test_lock_ignore_when_idle_and_paused():
    assert decide_lock(None, 1, False) == ("ignore", None)

def test_lock_hold_while_active_plays():
    assert decide_lock(1, 1, True) == ("hold", 1)

def test_lock_release_when_active_pauses():
    assert decide_lock(1, 1, False) == ("release", None)

def test_lock_ignores_other_tab():
    assert decide_lock(1, 2, True) == ("ignore", 1)


# ─── Run without pytest ───────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_clean_basic_youtube_title, test_clean_firefox_em_dash_suffix,
        test_clean_tab_counter_prefix, test_clean_bracketed_tags,
        test_clean_no_tags, test_clean_empty_string,
        test_split_hyphen, test_split_en_dash, test_split_em_dash,
        test_split_no_separator,
        test_parse_basic_lrc, test_parse_skips_metadata_tags,
        test_parse_empty_string, test_parse_multiple_timestamps_per_line,
        test_parse_variable_fraction_digits,
        test_similarity_exact_match, test_similarity_partial_match,
        test_similarity_no_match,
        test_resolve_uses_metadata_when_present, test_resolve_strips_topic_suffix,
        test_resolve_strips_tags_from_metadata_title,
        test_resolve_falls_back_to_title_parsing,
        test_resolve_metadata_without_artist_falls_back, test_clean_artist_plain,
        test_lock_claim_when_idle_and_playing, test_lock_ignore_when_idle_and_paused,
        test_lock_hold_while_active_plays, test_lock_release_when_active_pauses,
        test_lock_ignores_other_tab,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    exit(1 if failed else 0)
