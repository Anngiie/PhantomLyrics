/**
 * Phantom Lyrics - YouTube Timestamp Content Script
 *
 * Runs on YouTube video pages. Connects to the local Python WebSocket
 * server and sends the current video timestamp every second so the
 * desktop app can sync lyrics in real-time.
 *
 * Load this as a temporary add-on in Firefox via:
 *   about:debugging#/runtime/this-firefox → "Load Temporary Add-on"
 *   Select the firefox_extension/manifest.json file.
 */

(function () {
    "use strict";

    // ─── Configuration ───────────────────────────────────────────

    const WS_URL = "ws://localhost:8765";
    const SEND_INTERVAL_MS = 1000; // Send timestamp every second

    // ─── State ───────────────────────────────────────────────────

    let socket = null;
    let intervalId = null;
    let reconnectTimeout = null;
    let wasConnected = false;

    // ─── Helpers ─────────────────────────────────────────────────

    /**
     * Find the active video element on the page.
     * YouTube sometimes has multiple <video> tags (ads, thumbnails, etc.).
     * We want the main content video.
     */
    function findVideoElement() {
        const videos = document.querySelectorAll("video");
        // Return the first video that has a non-zero duration and is visible
        for (const v of videos) {
            if (v.duration && v.duration > 0 && v.offsetParent !== null) {
                return v;
            }
        }
        // Fallback: just return the first video with duration
        for (const v of videos) {
            if (v.duration && v.duration > 0) {
                return v;
            }
        }
        return null;
    }

    /**
     * Get the current track info from the page title.
     * We send this so the Python app can detect title changes
     * without polling (optional optimization).
     */
    function getCurrentTitle() {
        return document.title || "";
    }

    // ─── WebSocket Lifecycle ─────────────────────────────────────

    function connect() {
        // Avoid double-connect
        if (socket && (socket.readyState === WebSocket.CONNECTING ||
                       socket.readyState === WebSocket.OPEN)) {
            return;
        }

        try {
            socket = new WebSocket(WS_URL);
        } catch (e) {
            console.error("[Phantom Lyrics] WebSocket creation failed:", e);
            scheduleReconnect();
            return;
        }

        socket.onopen = function () {
            console.log("[Phantom Lyrics] Connected to Python server.");
            wasConnected = true;
            startSending();
        };

        socket.onclose = function (event) {
            console.log("[Phantom Lyrics] Disconnected:", event.code, event.reason);
            stopSending();
            socket = null;
            scheduleReconnect();
        };

        socket.onerror = function (e) {
            console.error("[Phantom Lyrics] WebSocket error:", e);
            // The onclose handler will fire after this, handling cleanup.
        };
    }

    function disconnect() {
        if (reconnectTimeout) {
            clearTimeout(reconnectTimeout);
            reconnectTimeout = null;
        }
        stopSending();
        if (socket) {
            socket.onclose = null; // Prevent reconnect loop
            socket.close(1000, "Page unload");
            socket = null;
        }
    }

    function scheduleReconnect() {
        if (reconnectTimeout) return;
        console.log("[Phantom Lyrics] Will retry connection in 3 seconds...");
        reconnectTimeout = setTimeout(function () {
            reconnectTimeout = null;
            connect();
        }, 3000);
    }

    // ─── Timestamp Sending ───────────────────────────────────────

    function sendTimestamp() {
        if (!socket || socket.readyState !== WebSocket.OPEN) return;

        const video = findVideoElement();
        if (!video) return; // No video on page (e.g., browsing homepage)

        const payload = {
            currentTime: video.currentTime,
            duration: video.duration,
            paused: video.paused,
            title: getCurrentTitle(),
        };

        try {
            socket.send(JSON.stringify(payload));
        } catch (e) {
            console.error("[Phantom Lyrics] Send failed:", e);
        }
    }

    function startSending() {
        stopSending(); // Clear any existing interval
        intervalId = setInterval(sendTimestamp, SEND_INTERVAL_MS);
        sendTimestamp(); // Send immediately on connect
    }

    function stopSending() {
        if (intervalId) {
            clearInterval(intervalId);
            intervalId = null;
        }
    }

    // ─── Initialization ──────────────────────────────────────────

    connect();

    // Clean up on page unload
    window.addEventListener("beforeunload", function () {
        disconnect();
    });

    // YouTube is an SPA — navigation between videos doesn't reload the page.
    // We re-check on URL changes (yt-navigate-finish is a custom event YouTube fires).
    window.addEventListener("yt-navigate-finish", function () {
        console.log("[Phantom Lyrics] YouTube navigation detected, reconnecting...");
        disconnect();
        // Small delay to let the new page's DOM settle
        setTimeout(connect, 1500);
    });

})();
