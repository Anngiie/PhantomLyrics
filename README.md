# Phantom Lyrics 🎵

A "ghost-like" desktop overlay that displays synchronized song lyrics anywhere on your screen while you play games or code. Works with YouTube Music in Firefox, Chrome, Brave, Edge, and Opera.

![Phantom Lyrics Demo](assets/app-demo.gif)

## In-Game Demo

The overlay sits on top of your game with click-through gaming mode — you see the lyrics, but mouse clicks pass straight through to the game.

![Gaming with Phantom Lyrics](assets/game-screen1.png)
![Lyrics overlay during gameplay](assets/game-screen2.png)

## How It Works

```
YouTube ──► Browser Extension ──WebSocket──► Python App
    │                                            │
    └── Page Title ──► Title Parser ──► Lyrics Fetch (LRCLib → NetEase)
                                                 │
                                    PySide6 Overlay Window ◄──
```

1. **Browser Extension** (Firefox or Chromium) sends the video timestamp and track info via WebSocket, and accepts playback commands back (works even with the YouTube tab in the background).
2. **Track info** comes from the page's MediaSession metadata (artist, title, album) when the browser provides it, falling back to parsing the page title.
3. **Lyrics Fetcher** queries LRCLib first, then falls back to NetEase Cloud Music for tracks LRCLib doesn't have. Results are cached to disk.
4. **PySide6 Overlay** displays lyrics in a transparent, always-on-top, draggable window, with hover controls for sync offset and playback.

## Project Structure

```
Phantom Lyrics/
├── phantom_lyrics.py      # Main app — orchestrates everything
├── overlay.py             # PySide6 transparent overlay window
├── websocket_server.py    # Local WebSocket server (receives timestamps)
├── title_utils.py         # YouTube title cleaning + artist/title splitting
├── lyrics_fetcher.py      # LRCLib + NetEase API client, LRC parser, disk cache
├── config.py              # User settings (load/save from ~/.phantom_lyrics/config.json)
├── settings_dialog.py     # Settings dialog (font, opacity, layout, auto-hide)
├── tray.py                # System tray icon (toggle, reset, settings, quit)
├── test_phantom_lyrics.py # Unit tests for pure functions
├── phantom_lyrics.spec    # PyInstaller build config
├── requirements.txt       # Runtime Python dependencies
├── requirements-dev.txt   # Dev dependencies (includes PyInstaller)
├── firefox_extension/
│   ├── manifest.json      # Firefox add-on manifest (MV2)
│   └── content.js         # Content script — sends timestamps to Python
└── chrome_extension/
    ├── manifest.json      # Chrome/Brave/Edge/Opera manifest (MV3)
    └── content.js         # Same content script — works on all Chromium browsers
```

## Setup Instructions

### 1. Install Python Dependencies

```powershell
pip install -r requirements.txt
```

Runs on Windows and Linux. On Windows you also get win32-based click-through and window management via `pywin32`; on Linux the overlay uses Qt's cross-platform transparency and click-through, and `pywin32` is skipped automatically.

### 2. Load the Browser Extension

The extension is **required** — it's how the app detects the current song and syncs lyrics. Without it, no lyrics will appear. Pick your browser:

#### Firefox

1. Open Firefox.
2. Go to `about:debugging#/runtime/this-firefox`.
3. Click **"Load Temporary Add-on..."**.
4. Select `firefox_extension/manifest.json`.
5. The extension icon won't appear in the toolbar — that's normal. It runs silently on YouTube pages.

#### Chrome / Brave / Edge / Opera (Chromium)

Works the same on all Chromium-based browsers — just use the correct URL:

1. Open your browser's extensions page:

   | Browser | URL |
   |---------|-----|
   | Chrome  | `chrome://extensions` |
   | Brave   | `brave://extensions` |
   | Edge    | `edge://extensions` |
   | Opera   | `opera://extensions` |

2. Toggle **"Developer mode"** on (top-right corner).
3. Click **"Load unpacked"**.
4. Select the `chrome_extension/` folder (not a single file — the whole folder).
5. The extension will appear in the list. It runs silently on YouTube pages — no toolbar icon.

### 3. Run the Desktop App

```powershell
python phantom_lyrics.py
```

### 4. Play a Song

1. Open a YouTube music video in your browser.
2. The overlay should appear — lyrics load automatically, even if the YouTube tab isn't focused.
3. Lyrics highlight in sync with the music.

### 5. Build a Standalone .exe (optional)

```powershell
pip install -r requirements-dev.txt
pyinstaller phantom_lyrics.spec --noconfirm
```

The executable is created in `dist/PhantomLyrics/PhantomLyrics.exe`. Run it directly — no Python install needed. The Firefox extension still needs to be loaded separately.

### 6. Run Tests (optional)

```powershell
python test_phantom_lyrics.py
```

Tests cover the pure functions: title cleaning, artist/title splitting, LRC parsing, and title similarity scoring.

## System Tray

When the app is running, a tray icon appears in your system tray:

- **Left-click** — toggle the overlay visibility (show/hide).
- **Right-click → Reset position** — move the overlay back to the bottom-left corner.
- **Right-click → Settings...** — open the settings dialog (font, opacity, layout, auto-hide).
- **Right-click → Quit** — exit the app.

## Settings

All settings are configurable via the tray icon → **Settings...** dialog and persisted to `~/.phantom_lyrics/config.json`:

| Setting | Default | Description |
|---------|---------|-------------|
| Font family | Segoe UI | Font for lyrics (dropdown: Segoe UI, Consolas, Georgia) |
| Font size | 14 | Font size in points |
| Overlay width | 600 | Width of the overlay in pixels |
| Visible lines | 3 | How many lyric lines to show (previous/current/next) |
| Outline width | 3 | Black stroke width around each letter (subtitle style) |
| Active line opacity | 220 | Opacity of the active line (0-255) |
| Inactive line opacity | 110 | Opacity of inactive lines (0-255) |
| Auto-hide timeout | 10s | Seconds without any data from the extension before the overlay fades out |

## Sync Offset

If the synced lyrics are slightly ahead or behind the audio, you can nudge them:

1. **Hover** over the overlay — three buttons appear below the lyrics: `[−] [0] [+]`
2. **`[−]`** — nudge lyrics 0.5s earlier (for LRC that's delayed)
3. **`[+]`** — nudge lyrics 0.5s later
4. **`[0]`** — reset offset to 0
5. A `Sync: +1.0s` toast appears for 2 seconds after each press so you see the current offset.
6. The offset is **saved per-song** in the lyrics cache — next time you play the same song, the offset is restored automatically.

## Playback Controls

Hover the overlay to reveal transport buttons below the lyrics: previous, play/pause, and next. Pressing one controls the YouTube tab that is currently driving the lyrics, so you can skip or pause without switching back to the browser. The overlay only appears while a song is playing, and hides itself when playback stops.

## Auto-hide

The overlay fades out after ~10 seconds without any data from the extension — i.e. when the YouTube tab is **closed or navigated away** and the extension stops reporting. It fades back in the moment a tab starts reporting again.

While a video is simply **paused** the overlay stays put — pausing doesn't hide your lyrics. The extension keeps reporting in the background, so the overlay only disappears once there's genuinely nothing connected.

## Multi-Tab Support

If you have multiple YouTube tabs open, only one drives the lyrics at a time:

- The first tab that reports it is playing claims the lock.
- Other tabs are ignored, so the lyrics don't flicker between songs.
- The lock releases when the active tab pauses, navigates to a new video, or closes, letting another playing tab take over.

## Lyrics Sources

The app queries lyrics APIs in order and uses the first hit:

1. **Disk cache** (`~/.phantom_lyrics/lyrics_cache.json`) — previously played songs load instantly, even offline. Holds up to 100 songs.
2. **[LRCLib](https://lrclib.net)** — free, open-source lyrics database (primary source).
3. **[NetEase Cloud Music](https://music.163.com)** — large database, strong for Asian music and niche tracks where LRCLib has gaps (fallback source, no API key needed).

If no source has the song, the overlay shows "No lyrics found for this song."

## How to Use While Gaming

- Set your game to **Borderless Windowed** or **Windowed Fullscreen** mode.
- The overlay sits above the game window because it's "Always on Top."
- The overlay never steals focus, so your game keeps keyboard/mouse input.
- The overlay has no title bar, no taskbar icon, and doesn't appear in Alt+Tab.

### Gaming Mode (Click-Through)

When you're playing a game, the overlay can block mouse clicks in its area since it's grabbable for dragging. **Gaming mode** fixes this:

- Press **`Ctrl+Alt+Space`** to toggle **gaming mode**.
  - The overlay becomes **click-through** — mouse clicks pass straight through to the game.
  - You can still see the lyrics, but can't interact with the overlay.
- Press **`Ctrl+Alt+Space`** again to exit gaming mode.
  - The overlay becomes draggable again.

The hotkey works system-wide, even when the game has keyboard focus.

## Repositioning the Overlay

The overlay is **free-draggable** — no lock, no hotkey, no toggle.

- **Click and drag** the overlay anywhere on your screen, anytime.
- Release the mouse to drop it; the position is saved automatically.
- The position is persisted to `~/.phantom_lyrics/overlay_position.json` and restored on the next launch.

> Note: because the overlay is always grabbable, clicks *on* the overlay won't pass through to the game. The overlay is small, so just drag it out of the way if you need to click something behind it.

## UI Design

| Feature | Detail |
|---------|--------|
| Position | Free-draggable anywhere (saved across runs) |
| Background | 100% transparent — no box or frame |
| Text outline | Black stroke around every letter (subtitle-style, readable on any background) |
| Active line | ~86% opacity white |
| Other lines | ~43% opacity white (fades with distance) |
| Song info | ~31% opacity white (very subtle) |
| Font | Segoe UI, 14pt (configurable) |
| Click behavior | Grabbable (drag to move); never steals focus |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No overlay visible | Check that the Python app started without errors in the terminal |
| Overlay shows but no lyrics | Make sure you're on a YouTube **video** page (not homepage/search). For Firefox the tab title must contain "YouTube" + "Mozilla Firefox". |
| Lyrics not syncing | Check the terminal for "Client connected" — if not, reload the extension (Firefox: `about:debugging`, Chrome: `chrome://extensions`) |
| Lyrics slightly off | Hover over the overlay and use the `[−]` / `[+]` buttons to nudge sync. The offset is saved per-song. |
| Can't click things behind overlay | The overlay is always grabbable, so clicks on it don't pass through. Drag it out of the way first. |
| "No lyrics found" | The song isn't in LRCLib or NetEase. The app queries both automatically. |
| Multiple tabs cause flicker | The app locks onto the actively playing tab. Make sure only one tab is actually playing audio. |

## Tech Stack

- **Python 3.x** — Application logic
- **PySide6** — Transparent overlay GUI
- **pywin32** — Windows API (layered window, no-focus, window enumeration)
- **websockets** — Async WebSocket server
- **requests** — LRCLib + NetEase API client
- **pynput** — Global hotkey for gaming-mode toggle
- **Firefox + Chromium WebExtension** — YouTube timestamp extraction (supports all major browsers)

## Credits

- Lyrics data from [LRCLib](https://lrclib.net) — a free, open-source lyrics database.
- Fallback lyrics from [NetEase Cloud Music](https://music.163.com).
- Inspired by the desire to read lyrics without alt-tabbing during games.
