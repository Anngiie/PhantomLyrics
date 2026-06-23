"""
Phantom Lyrics - WebSocket Server
=================================
Runs a local asyncio-based WebSocket server that receives YouTube video
timestamp data from the Firefox browser extension.

Data received from the extension (JSON):
    {
        "currentTime": 42.5,   // Current playback position in seconds
        "duration": 240.0,     // Total video duration in seconds
        "paused": false,       // Whether the video is paused
        "title": "Artist - Song Name"  // Page title (optional, for redundancy)
    }

This runs in its own thread and communicates with the main app through
a thread-safe queue or callback.
"""

import asyncio
import json
import logging
import threading
from typing import Optional, Callable

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)


class LyricsWebSocketServer:
    """
    A local WebSocket server that listens for timestamp data from the
    Firefox extension. Runs in a background thread using asyncio.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8765,
        on_timestamp: Optional[Callable[[dict], None]] = None,
    ):
        """
        Args:
            host: Bind address (localhost only for security).
            port: Listening port.
            on_timestamp: Callback invoked with the parsed JSON payload
                          whenever a message arrives from the extension.
        """
        self.host = host
        self.port = port
        self.on_timestamp = on_timestamp
        self._server: Optional[websockets.WebSocketServer] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected_clients: set = set()

    # ─── Public API ──────────────────────────────────────────

    def start(self) -> None:
        """Start the WebSocket server in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("WebSocket server is already running.")
            return

        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="ws-server-thread",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"WebSocket server starting on ws://{self.host}:{self.port}")

    def stop(self) -> None:
        """Gracefully shut down the WebSocket server."""
        if self._loop and self._server:
            # asyncio/websockets objects must be closed from the loop's own
            # thread; close() may be sync (asyncio.Server) or a coroutine
            # (legacy websockets), so we handle both inside a coroutine.
            async def _shutdown() -> None:
                close_result = self._server.close()
                if asyncio.iscoroutine(close_result):
                    await close_result
                try:
                    await self._server.wait_closed()
                except Exception:
                    pass

            future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            try:
                future.result(timeout=3)
            except Exception:
                logger.debug("Shutdown coroutine timed out or errored (ok during exit).")

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info("WebSocket server stopped.")

    @property
    def client_count(self) -> int:
        """Number of currently connected WebSocket clients."""
        return len(self._connected_clients)

    # ─── Internals ───────────────────────────────────────────

    def _run_event_loop(self) -> None:
        """Create and run the asyncio event loop for the server."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._serve())
        except (RuntimeError, asyncio.CancelledError):
            # Expected when the loop is stopped during shutdown
            logger.info("WebSocket server event loop stopped.")
        except Exception:
            logger.exception("WebSocket server crashed.")
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        """Start the websocket server and keep it alive."""
        self._server = await websockets.serve(
            self._handle_connection,
            self.host,
            self.port,
            ping_interval=10,   # Keep the connection alive
            ping_timeout=5,
            max_size=2**16,    # 64 KiB — more than enough for small JSON
        )
        logger.info(f"WebSocket server listening on ws://{self.host}:{self.port}")
        # Run forever (until stop() cancels this)
        await self._server.wait_closed()

    async def _handle_connection(self, ws: WebSocketServerProtocol) -> None:
        """
        Handle a single WebSocket client connection.
        Receives JSON messages and forwards them via the callback.
        """
        self._connected_clients.add(ws)
        client_addr = ws.remote_address
        logger.info(f"Client connected: {client_addr} (total: {len(self._connected_clients)})")

        try:
            async for raw_message in ws:
                try:
                    data = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning(f"Received non-JSON message: {raw_message[:100]}")
                    continue

                if self.on_timestamp:
                    self.on_timestamp(data)

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Client disconnected: {client_addr}")
        except Exception:
            logger.exception(f"Error handling client {client_addr}")
        finally:
            self._connected_clients.discard(ws)
