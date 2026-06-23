# Phantom Lyrics 🎵

A "ghost-like" desktop overlay that displays synchronized song lyrics in the bottom-left corner of your screen while you play games or code. Works with YouTube Music in Firefox.

## How It Works

```
YouTube (Firefox) ──► Firefox Extension ──WebSocket──► Python App
       │                                                    │
       └── Window Title ──► Browser Monitor ──► Lyrics Fetch (LRCLib)
                                                            │
                                               PySide6 Overlay Window ◄──
```

1. **Browser Monitor** reads the Firefox window title to detect the current song.
2. **Firefox Extension** sends the exact video timestamp via WebSocket.
3. **LRCLib API** provides synchronized (LRC) lyrics.
4. **PySide6 Overlay** displays lyrics with a transparent, always-on-top, free-draggable window.

## Project Structure

```
Phantom Lyrics/
├── phantom_lyrics.py      # Main app — orchestrates everything
├── overlay.py             # PySide6 transparent overlay window
├── websocket_server.py    # Local WebSocket server (receives timestamps)
├── browser_monitor.py     # Firefox window title polling + song detection
├── lyrics_fetcher.py      # LRCLib API client + LRC parser
├── requirements.txt       # Python dependencies
└── firefox_extension/
    ├── manifest.json      # Firefox add-on manifest
    └── content.js         # Content script — sends timestamps to Python
```

## Setup Instructions

### 1. Install Python Dependencies

```powershell
pip install -r requirements.txt
```

### 2. Load the Firefox Extension

1. Open Firefox.
2. Go to `about:debugging#/runtime/this-firefox`.
3. Click **"Load Temporary Add-on..."**.
4. Select the file: `firefox_extension/manifest.json`.
5. The extension icon won't appear in the toolbar — that's normal. It runs silently on YouTube pages.

### 3. Run the Desktop App

```powershell
python phantom_lyrics.py
```

### 4. Play a Song

1. Open a YouTube music video in Firefox.
2. The overlay should appear in the bottom-left corner of your screen.
3. Lyrics will highlight in sync with the music — automatically.

## How to Use While Gaming

- Set your game to **Borderless Windowed** or **Windowed Fullscreen** mode.
- The overlay sits above the game window because it's "Always on Top."
- The overlay never steals focus, so your game keeps keyboard/mouse input.
- The overlay has no title bar, no taskbar icon, and doesn't appear in Alt+Tab.

## Repositioning the Overlay

The overlay is **free-draggable** — no lock, no hotkey, no toggle.

- **Click and drag** the overlay anywhere on your screen, anytime.
- Release the mouse to drop it; the position is saved automatically.
- The position is persisted to `~/.phantom_lyrics/overlay_position.json`
  and restored on the next launch.

> Note: because the overlay is always grabbable, clicks *on* the overlay
> won't pass through to the game. The overlay is small, so just drag it
> out of the way if you need to click something behind it.

## UI Design

| Feature | Detail |
|---------|--------|
| Position | Bottom-left corner |
| Background | 100% transparent |
| Active line | ~86% opacity white |
| Other lines | ~43% opacity white (fades with distance) |
| Song info | ~31% opacity white (very subtle) |
| Font | Segoe UI, 14pt |
| Click behavior | Grabbable (drag to move); never steals focus |

## Configuration

Edit the constants at the top of each file:

| Constant | File | Default | Description |
|----------|------|---------|-------------|
| `OVERLAY_WIDTH` | `overlay.py` | 600 | Width of the overlay in pixels |
| `MAX_VISIBLE_LINES` | `overlay.py` | 3 | How many lyric lines to show (previous/current/next) |
| `FONT_SIZE` | `overlay.py` | 14 | Font size in points |
| `ACTIVE_LINE_ALPHA` | `overlay.py` | 220 | Opacity of the active line (0-255) |
| `INACTIVE_LINE_ALPHA` | `overlay.py` | 110 | Opacity of inactive lines (0-255) |
| `POLL_INTERVAL` | `browser_monitor.py` | 2.0 | How often to check Firefox title (seconds) |
| `WS_HOST / WS_PORT` | `websocket_server.py` | localhost:8765 | WebSocket bind address |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No overlay visible | Check that the Python app started without errors in the terminal |
| Overlay shows but no lyrics | Make sure you're on a YouTube **video** page (not homepage/search). The Firefox tab title must contain "YouTube" + "Mozilla Firefox". |
| Lyrics not syncing | Check the terminal for "Client connected" — if not, reload the Firefox extension at `about:debugging` |
| Can't click things behind overlay | The overlay is always grabbable, so clicks on it don't pass through. Drag it out of the way first. |
| "No lyrics found" | The song might not be in LRCLib. Try a more popular song to test. LRCLib: https://lrclib.net |

## Tech Stack

- **Python 3.x** — Application logic
- **PySide6** — Transparent overlay GUI
- **pywin32** — Windows API (layered window, no-focus, window enumeration)
- **websockets** — Async WebSocket server
- **requests** — LRCLib API client
- **Firefox WebExtension** — YouTube timestamp extraction

## Credits

- Lyrics data from [LRCLib](https://lrclib.net) — a free, open-source lyrics database.
- Inspired by the desire to read lyrics without alt-tabbing during games.
