"""
Phantom Lyrics — Spotify Monitor
==================================
Connects to the Spotify Web API via OAuth 2.0 PKCE (authorization code with
proof key for code exchange) to read the currently playing track and its
playback position. No browser extension needed — Spotify's own API reports
both the track metadata and the exact progress_ms.

Requirements
------------
- Spotify Premium account (the /me/player endpoint requires Premium).
- A Spotify Developer application registered at https://developer.spotify.com/dashboard
  with redirect URI set to http://localhost:8766/callback.
- The Client ID from that application, passed when creating the monitor.

Flow
----
1. On first run, the user is asked to connect via the tray or a button.
2. A browser tab opens for Spotify OAuth login (PKCE, no client secret needed).
3. A tiny local HTTP server catches the redirect and extracts the auth code.
4. The code is exchanged for access + refresh tokens.
5. The refresh token is persisted to disk so the user only logs in once.
6. The monitor polls GET /me/player/currently-playing every 1–2 seconds.
7. Track + timestamp data feeds into the same lyrics pipeline as YouTube.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Callable

import requests

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────

# Spotify API endpoints
_SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_API_URL = "https://api.spotify.com/v1/me/player/currently-playing"

# OAuth redirect URI (must match what's registered in the Spotify dashboard)
_REDIRECT_URI = "http://localhost:8766/callback"
_REDIRECT_PORT = 8766

# Spotify scopes needed (only what we actually use)
_SCOPES = "user-read-currently-playing user-read-playback-state"

# Token storage
_CONFIG_DIR = Path.home() / ".phantom_lyrics"
_TOKEN_FILE = _CONFIG_DIR / "spotify_token.json"


# ─── OAuth PKCE Helpers ──────────────────────────────────────────


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate a code_verifier and its SHA-256 code_challenge (PKCE)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")
    return verifier, challenge


def _build_auth_url(client_id: str, code_challenge: str) -> str:
    """Build the Spotify OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": _REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "scope": _SCOPES,
    }
    return f"{_SPOTIFY_AUTH_URL}?{urllib.parse.urlencode(params)}"


# ─── Local HTTP server to catch the OAuth redirect ───────────────


class _OAuthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that grabs the 'code' query param and shuts down."""

    # Shared state (set before the server starts)
    auth_code: str | None = None
    error: str | None = None
    received: threading.Event | None = None

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _OAuthHandler.auth_code = params["code"][0]
            self._respond("Connected! You can close this tab.")
        elif "error" in params:
            _OAuthHandler.error = params.get("error", ["unknown"])[0]
            self._respond(f"Error: {_OAuthHandler.error}")
        else:
            self._respond("Waiting for Spotify...")

        if _OAuthHandler.received:
            _OAuthHandler.received.set()

    def _respond(self, message: str) -> None:
        body = f"<html><body style='font-family:sans-serif;text-align:center;padding-top:80px;'><h2>🎵 Phantom Lyrics</h2><p>{message}</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args) -> None:
        """Suppress HTTP server log noise."""
        pass


class _CallbackServer:
    """Tiny HTTP server that waits for the OAuth redirect and returns the code."""

    def __init__(self):
        self._server = HTTPServer(("localhost", _REDIRECT_PORT), _OAuthHandler)
        self._thread: Optional[threading.Thread] = None

    def wait_for_code(self, timeout: float = 120.0) -> str | None:
        """Start the server, wait for the callback, return the auth code."""
        _OAuthHandler.auth_code = None
        _OAuthHandler.error = None
        _OAuthHandler.received = threading.Event()

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        if _OAuthHandler.received.wait(timeout):
            if _OAuthHandler.error:
                logger.error("Spotify OAuth error: %s", _OAuthHandler.error)
                return None
            return _OAuthHandler.auth_code

        logger.error("Timed out waiting for Spotify OAuth callback.")
        return None

    def _run(self) -> None:
        try:
            self._server.handle_request()  # Handle one request, then stop
        except Exception:
            logger.exception("OAuth callback server error")

    def close(self) -> None:
        try:
            self._server.server_close()
        except Exception:
            pass


# ─── Token Management ────────────────────────────────────────────


def _save_tokens(access_token: str, refresh_token: str, expires_in: int) -> None:
    """Persist tokens to disk so we don't need to re-auth on each launch."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in - 60,  # 60s safety margin
    }
    _TOKEN_FILE.write_text(json.dumps(payload))


def _load_tokens() -> dict | None:
    """Load persisted tokens from disk, or None if not found / expired."""
    try:
        if not _TOKEN_FILE.exists():
            return None
        return json.loads(_TOKEN_FILE.read_text())
    except Exception:
        logger.debug("Could not load Spotify tokens.", exc_info=True)
        return None


def _refresh_access_token(client_id: str, refresh_token: str) -> dict | None:
    """Exchange a refresh token for a new access token."""
    try:
        resp = requests.post(
            _SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data
    except Exception:
        logger.exception("Failed to refresh Spotify access token.")
        return None


# ─── Spotify Monitor ─────────────────────────────────────────────


class SpotifyMonitor:
    """
    Polls the Spotify Web API for the currently playing track.

    Usage:
        monitor = SpotifyMonitor(
            client_id="...",
            on_track_change=lambda artist, title: ...,
        )
        monitor.connect()    # Opens browser for OAuth if no saved token
        monitor.start()      # Begins polling
    """

    POLL_INTERVAL = 1.5  # seconds between API calls

    def __init__(
        self,
        client_id: str,
        on_track_change: Optional[Callable[[str, str], None]] = None,
        on_timestamp: Optional[Callable[[float, bool], None]] = None,
    ):
        """
        Args:
            client_id: Your Spotify app's Client ID.
            on_track_change: Called as on_track_change(artist, title) when the
                             track changes.
            on_timestamp: Called as on_timestamp(progress_seconds, is_playing)
                          on every poll.
        """
        self._client_id = client_id
        self._on_track_change = on_track_change
        self._on_timestamp = on_timestamp
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._current_track_id: str | None = None

    # ─── Public API ──────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Whether we have a valid access token."""
        return self._access_token is not None

    def connect(self) -> bool:
        """
        Obtain an access token, either from saved tokens or via OAuth login.
        Returns True on success.

        Opens a browser tab for the user to log in to Spotify on first use.
        Subsequent launches reuse the persisted refresh token silently.
        """
        # 1. Try loading saved tokens
        saved = _load_tokens()
        if saved and saved.get("refresh_token"):
            self._refresh_token = saved["refresh_token"]
            # If the access token is still valid, use it
            if saved.get("expires_at", 0) > time.time():
                self._access_token = saved["access_token"]
                logger.info("Spotify: using cached access token.")
                return True
            # Otherwise, refresh it
            logger.info("Spotify: refreshing access token...")
            refreshed = _refresh_access_token(self._client_id, self._refresh_token)
            if refreshed:
                self._access_token = refreshed["access_token"]
                new_refresh = refreshed.get("refresh_token")
                if new_refresh:
                    self._refresh_token = new_refresh
                _save_tokens(
                    self._access_token,
                    self._refresh_token,
                    refreshed.get("expires_in", 3600),
                )
                logger.info("Spotify: token refreshed.")
                return True
            # Refresh failed — fall through to full re-auth
            logger.warning("Spotify: token refresh failed, re-authenticating.")

        # 2. Full PKCE OAuth flow
        verifier, challenge = _generate_pkce_pair()
        auth_url = _build_auth_url(self._client_id, challenge)

        logger.info("Spotify: opening browser for OAuth login...")
        webbrowser.open(auth_url)

        server = _CallbackServer()
        try:
            code = server.wait_for_code(timeout=120)
        finally:
            server.close()

        if not code:
            return False

        # Exchange code for tokens
        try:
            resp = requests.post(
                _SPOTIFY_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _REDIRECT_URI,
                    "code_verifier": verifier,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("Spotify: failed to exchange auth code for tokens.")
            return False

        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        _save_tokens(
            self._access_token,
            self._refresh_token or "",
            data.get("expires_in", 3600),
        )
        logger.info("Spotify: connected successfully.")
        return True

    def start(self) -> None:
        """Begin polling Spotify in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Spotify monitor is already running.")
            return
        if not self._access_token:
            logger.warning("Spotify: no access token. Call connect() first.")
            return

        self._running.set()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="spotify-monitor-thread",
            daemon=True,
        )
        self._thread.start()
        logger.info("Spotify monitor started (polling every %.1fs)", self.POLL_INTERVAL)

    def stop(self) -> None:
        """Stop the polling thread."""
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info("Spotify monitor stopped.")

    # ─── Internals ───────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Main loop: poll Spotify API for the current track."""
        while self._running.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("Spotify poll error")
            self._running.wait(self.POLL_INTERVAL)

    def _poll_once(self) -> None:
        """Make one API call and process the result."""
        resp = requests.get(
            _SPOTIFY_API_URL,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=5,
        )

        # 401/403 → token expired, try refresh and retry once
        if resp.status_code in (401, 403):
            logger.debug("Spotify: token expired, refreshing...")
            if self._refresh_token:
                refreshed = _refresh_access_token(self._client_id, self._refresh_token)
                if refreshed:
                    self._access_token = refreshed["access_token"]
                    new_refresh = refreshed.get("refresh_token")
                    if new_refresh:
                        self._refresh_token = new_refresh
                    _save_tokens(
                        self._access_token,
                        self._refresh_token,
                        refreshed.get("expires_in", 3600),
                    )
                    resp = requests.get(
                        _SPOTIFY_API_URL,
                        headers={"Authorization": f"Bearer {self._access_token}"},
                        timeout=5,
                    )
            if resp.status_code in (401, 403):
                logger.error("Spotify: token refresh failed, stopping monitor.")
                self._running.clear()
                return

        # 204 → nothing playing
        if resp.status_code == 204:
            return

        if not resp.ok:
            logger.debug("Spotify API returned %d", resp.status_code)
            return

        data = resp.json()
        if not data or not data.get("item"):
            return  # Nothing playing

        item = data["item"]
        track_id = item.get("id", "")
        track_name = item.get("name", "")
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
        progress_ms = data.get("progress_ms", 0)
        is_playing = data.get("is_playing", False)

        # Detect track change
        if track_id and track_id != self._current_track_id:
            self._current_track_id = track_id
            logger.info("🎵 Spotify: %s — %s", artists, track_name)
            if self._on_track_change and track_name:
                self._on_track_change(artists, track_name)

        # Forward timestamp + play state
        if self._on_timestamp:
            self._on_timestamp(progress_ms / 1000.0, is_playing)
