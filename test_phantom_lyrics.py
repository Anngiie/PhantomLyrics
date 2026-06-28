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

from title_utils import clean_youtube_title, split_artist_title
from lyrics_fetcher import parse_lrc, LyricLine, _title_similarity
from phantom_lyrics import playback_is_advancing


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


# ─── playback_is_advancing ────────────────────────────────────

def test_advancing_forward_playback():
    assert playback_is_advancing(10.0, 11.0, is_paused=False, epsilon=0.4) is True

def test_not_advancing_when_paused():
    assert playback_is_advancing(10.0, 11.0, is_paused=True, epsilon=0.4) is False

def test_not_advancing_when_frozen():
    assert playback_is_advancing(10.0, 10.1, is_paused=False, epsilon=0.4) is False

def test_advancing_on_loop_restart():
    assert playback_is_advancing(200.0, 0.0, is_paused=False, epsilon=0.4) is True

def test_advancing_on_seek_back():
    assert playback_is_advancing(120.0, 30.0, is_paused=False, epsilon=0.4) is True

def test_not_advancing_on_backward_jitter():
    assert playback_is_advancing(50.0, 49.9, is_paused=False, epsilon=0.4) is False


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
        test_advancing_forward_playback, test_not_advancing_when_paused,
        test_not_advancing_when_frozen, test_advancing_on_loop_restart,
        test_advancing_on_seek_back, test_not_advancing_on_backward_jitter,
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
