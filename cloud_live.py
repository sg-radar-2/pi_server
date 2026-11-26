"""Asynchronous Raspberry Pi -> RAD-2 Render Cloud LIVE sender.

Intentionally separate from cloud.CloudClient:
* Existing Event/JPEG REST uploads remain durable in SQLite outbox.
* LIVE frames are ephemeral: only current x/y is streamed (~5 FPS).
* Cloud interruption never blocks radar receive, judging, or REST uploads.

Python 3.11+ (server.py uses tomllib); websockets >= 12.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

try:
    # websockets >=13: new asyncio implementation; modern versions need additional_headers.
    from websockets.asyncio.client import connect as ws_connect
    HEADER_KWARG = "additional_headers"
except ImportError:
    # websockets 12: legacy asyncio implementation; needs extra_headers.
    from websockets.legacy.client import connect as ws_connect
    HEADER_KWARG = "extra_headers"

log = logging.getLogger("cloud.live")


class CloudLiveClient:
    def __init__(
        self,
        cfg: dict[str, Any],
        get_frame: Callable[[], dict[str, Any]],
        get_status: Callable[[], dict[str, Any]],
    ) -> None:
        self.device_id = str(cfg.get("device_id") or "rad2pi")
        self.key = str(cfg.get("device_key") or "")
        self.base_url = str(cfg.get("url") or "").strip().rstrip("/")
        self.fps = max(0.5, min(float(cfg.get("live_fps", 5.0)), 10.0))
        self.status_interval = max(1.0, float(cfg.get("live_status_interval_s", 5.0)))
        self.enabled = bool(cfg.get("live_enabled", True)) and bool(self.base_url) and bool(self.key)
        self.get_frame = get_frame
        self.get_status = get_status

        self.ws_url: str | None = None
        if self.base_url:
            parts = urlsplit(self.base_url)
            scheme = {"https": "wss", "http": "ws"}.get(parts.scheme)
            if not scheme or not parts.netloc or parts.path not in ("", "/"):
                raise ValueError("[cloud] url must be http(s)://hostname without an API path")
            self.ws_url = (
                f"{scheme}://{parts.netloc}/ws/device/live?"
                + urlencode({"device_id": self.device_id})
            )

    @staticmethod
    def _encode(message: dict[str, Any]) -> str:
        # Compact JSON, but keep Korean situation labels readable.
        # Reject NaN/Infinity rather than sending a malformed Cloud frame.
        return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    async def _connected(self, ws: Any) -> None:
        frame_period = 1.0 / self.fps
        next_tick = time.monotonic()
        next_status = 0.0
        frames_sent = 0

        while True:
            frame = self.get_frame()             # already used by Pi's local /ws/live
            await asyncio.wait_for(ws.send(self._encode(frame)), timeout=3.0)
            frames_sent += 1

            now = time.monotonic()
            if now >= next_status:
                # store.counts() can access disk. Keep it off the async event loop.
                try:
                    state = await asyncio.wait_for(
                        asyncio.to_thread(self.get_status), timeout=5.0
                    )
                    payload = {"type": "status", "device_id": self.device_id, **state}
                    await asyncio.wait_for(ws.send(self._encode(payload)), timeout=3.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # LIVE frames can still flow if a storage/CPU status sample fails.
                    log.warning("LIVE status sample failed (%s)", type(exc).__name__)
                next_status = time.monotonic() + self.status_interval

            next_tick += frame_period
            now = time.monotonic()
            if next_tick <= now:
                # Do not burst/replay old frames when the network is slow.
                next_tick = now + frame_period
            await asyncio.sleep(max(0.0, next_tick - now))

    async def run(self) -> None:
        """Call as a background asyncio.Task in FastAPI lifespan; cancel at shutdown."""
        if not self.enabled:
            log.info("Cloud LIVE disabled (missing cloud url/key or live_enabled=false)")
            return
        assert self.ws_url is not None
        log.info("Cloud LIVE enabled: %s (%.1f FPS)", self.ws_url, self.fps)

        delay = 2.0
        while True:
            try:
                headers = {"X-Device-Key": self.key}  # Never put key in URL/logs.
                opts = {
                    HEADER_KWARG: headers,
                    "open_timeout": 40,
                    "close_timeout": 3,
                    "ping_interval": 20,
                    "ping_timeout": 20,
                    "max_size": 64 * 1024,
                }
                async with ws_connect(self.ws_url, **opts) as ws:
                    log.info("Cloud LIVE connected (%s)", self.device_id)
                    delay = 2.0
                    await self._connected(ws)
            except asyncio.CancelledError:
                log.info("Cloud LIVE stopped")
                raise
            except Exception as exc:
                # Do not log key or full request headers.
                log.warning(
                    "Cloud LIVE disconnected (%s); retry in %.0fs",
                    type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 30.0)
