"""
Phantom Lyrics - Configuration
================================
Loads and saves user settings from ~/.phantom_lyrics/config.json.

All user-tweakable values (font, opacity, layout, timeouts) live here
instead of being hardcoded constants scattered across modules. The overlay
and other components read from the Config dataclass at init time.
"""

import json
import logging
from dataclasses import dataclass, asdict, fields
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".phantom_lyrics"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Config:
    """User-tweakable settings. Defaults match the original hardcoded values."""

    # ── Overlay layout ──
    overlay_width: int = 600
    max_visible_lines: int = 3
    line_spacing_px: int = 6
    title_gap_px: int = 12
    side_padding_px: int = 20
    bottom_padding_px: int = 40

    # ── Font ──
    font_family: str = "Segoe UI"
    font_size: int = 14

    # ── Opacity (0-255) ──
    active_line_alpha: int = 220
    inactive_line_alpha: int = 110
    song_info_alpha: int = 80

    # ── Text outline ──
    outline_width_px: float = 3.0

    # ── Auto-hide ──
    auto_hide_timeout_s: float = 10.0

    # ── WebSocket ──
    ws_host: str = "localhost"
    ws_port: int = 8765

    def save(self) -> None:
        """Write the current config to disk."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))
        except Exception:
            logger.debug("Could not save config to disk.", exc_info=True)


def load_config() -> Config:
    """
    Load config from disk, falling back to defaults for any missing or
    invalid fields. Returns a Config instance.
    """
    config = Config()
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            valid_keys = {f.name for f in fields(Config)}
            for key, value in data.items():
                if key in valid_keys:
                    setattr(config, key, value)
            logger.info(f"Loaded config from {CONFIG_FILE}")
    except Exception:
        logger.debug("Could not load config from disk.", exc_info=True)
    return config
