/**
 * Phantom Lyrics - YouTube content script (Firefox)
 *
 * Reports the playing track to the local Phantom Lyrics app over a WebSocket
 * and accepts playback commands back from it. Track info comes from the page's
 * MediaSession metadata when available, with the page title as a fallback.
 */

(function () {
    "use strict";

    const WS_URL = "ws://localhost:8765";
    const SEND_INTERVAL_MS = 1000;

    let socket = null;
    let intervalId = null;
    let reconnectTimeout = null;

    // Pick the main content video (YouTube also has ad/thumbnail videos).
    function findVideoElement() {
        const videos = document.querySelectorAll("video");
        for (const v of videos) {
            if (v.duration && v.duration > 0 && v.offsetParent !== null) return v;
        }
        for (const v of videos) {
            if (v.duration && v.duration > 0) return v;
        }
        return null;
    }

    // Structured track info from the MediaSession API (YouTube and YT Music set
    // this). Falls back to the page title when metadata is missing.
    function getMetadata() {
        const md = navigator.mediaSession && navigator.mediaSession.metadata;
        if (md && md.title) {
            let artwork = "";
            if (md.artwork && md.artwork.length) {
                artwork = md.artwork[md.artwork.length - 1].src || "";
            }
            return {
                title: md.title || "",
                artist: md.artist || "",
                album: md.album || "",
                artwork: artwork,
                hasMetadata: true,
            };
        }
        return { title: document.title || "", artist: "", album: "", artwork: "", hasMetadata: false };
    }

    function connect() {
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
            console.log("[Phantom Lyrics] Connected.");
            startSending();
        };
        socket.onmessage = function (event) {
            handleCommand(event.data);
        };
        socket.onclose = function () {
            stopSending();
            socket = null;
            scheduleReconnect();
        };
        socket.onerror = function () {
            // onclose fires right after and does the cleanup.
        };
    }

    function disconnect() {
        if (reconnectTimeout) {
            clearTimeout(reconnectTimeout);
            reconnectTimeout = null;
        }
        stopSending();
        if (socket) {
            socket.onclose = null;
            socket.close(1000, "Page unload");
            socket = null;
        }
    }

    function scheduleReconnect() {
        if (reconnectTimeout) return;
        reconnectTimeout = setTimeout(function () {
            reconnectTimeout = null;
            connect();
        }, 3000);
    }

    // Playback commands from the desktop app drive the YouTube player.
    function handleCommand(raw) {
        let msg;
        try { msg = JSON.parse(raw); } catch (e) { return; }
        const cmd = msg && msg.command;
        if (!cmd) return;
        const video = findVideoElement();
        switch (cmd) {
            case "playpause":
                if (video) { video.paused ? video.play() : video.pause(); }
                break;
            case "next":
                clickButton(".ytp-next-button");
                break;
            case "prev":
                clickButton(".ytp-prev-button");
                break;
            case "seek":
                if (video && typeof msg.time === "number") { video.currentTime = msg.time; }
                break;
        }
        sendState(); // reflect the change without waiting for the next tick
    }

    function clickButton(selector) {
        const btn = document.querySelector(selector);
        if (btn) btn.click();
    }

    function sendState() {
        if (!socket || socket.readyState !== WebSocket.OPEN) return;
        const video = findVideoElement();
        if (!video) return;
        const meta = getMetadata();
        // Send only when something is actually playing: any /watch page (normal
        // YouTube videos included) or a page with MediaSession data. This skips
        // the search results and home feed, where nothing is playing.
        if (!meta.hasMetadata && !location.pathname.startsWith("/watch")) return;
        const payload = {
            currentTime: video.currentTime,
            duration: video.duration,
            paused: video.paused,
            title: meta.title,
            artist: meta.artist,
            album: meta.album,
            artwork: meta.artwork,
            hasMetadata: meta.hasMetadata,
        };
        try { socket.send(JSON.stringify(payload)); } catch (e) {}
    }

    function startSending() {
        stopSending();
        intervalId = setInterval(sendState, SEND_INTERVAL_MS);
        sendState();
    }

    function stopSending() {
        if (intervalId) {
            clearInterval(intervalId);
            intervalId = null;
        }
    }

    connect();

    window.addEventListener("beforeunload", disconnect);

    // YouTube is a single-page app; reconnect when it navigates to a new video.
    window.addEventListener("yt-navigate-finish", function () {
        disconnect();
        setTimeout(connect, 1500);
    });
})();
