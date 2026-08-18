"""Recording a sample of the platform's WebSocket traffic.

Step one of reading the feed instead of the pixels: capture a minute of frames,
strip anything secret out of them, and summarise what is in there. The
platform's message format is undocumented, so it has to be observed before a
parser can be written for it.

Nothing is sent to the platform. This subscribes to the browser's own network
events and listens.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .cdp import BrowserError, Target, list_targets, pick_target
from .frames import Frame, Summary, decode_frame

log = get_logger(__name__)

try:  # pragma: no cover - optional
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]


@dataclass
class Capture:
    """What a recording session produced."""

    frames: list[Frame]
    summary: Summary
    sockets: list[str]

    def write(self, path: Path) -> Path:
        """Write the redacted frames as JSON lines."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for frame in self.frames:
                handle.write(json.dumps(frame.to_dict(), default=str) + "\n")
        return path


async def record(
    target: Target,
    seconds: float = 60.0,
    max_frames: int = 4000,
    on_frame: Any = None,
) -> Capture:
    """Listen to one page's WebSocket traffic for a while.

    ``on_frame`` turns this from a sample into a session. Given one, each
    frame is handed over as it arrives and not kept, so a recording is bounded
    by disk rather than by memory and can run for hours — which is what it
    takes to capture a real market rather than a glimpse of one. Without it
    the frames are collected and returned, which is all a protocol sample
    needs.
    """
    if websockets is None:  # pragma: no cover - dependency guaranteed by requirements
        raise BrowserError("The 'websockets' package is required to read the feed.")

    frames: list[Frame] = []
    summary = Summary()
    sockets: list[str] = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + seconds
    attempts = 0

    # Reconnect until the time is up. A three-hour capture is worth far more
    # than a thirty-minute one, and over three hours a DevTools socket
    # dropping at least once is close to certain — the page reloads, the
    # network hiccups, the platform reconnects on its own. Stopping at the
    # first of those turned "record for three hours" into "record until
    # something twitches", and the user would only find out afterwards.
    #
    # Nothing already captured is at risk: frames are written to disk as they
    # arrive, so a reconnection resumes rather than restarts.
    while loop.time() < deadline and len(frames) < max_frames:
        try:
            async with websockets.connect(
                target.websocket_url, max_size=32 * 1024 * 1024, ping_interval=None
            ) as connection:
                await connection.send(json.dumps({"id": 1, "method": "Network.enable"}))
                if attempts:
                    log.info("reconnected to the page, still recording")

                while loop.time() < deadline and len(frames) < max_frames:
                    remaining = deadline - loop.time()
                    raw = await asyncio.wait_for(
                        connection.recv(), timeout=max(remaining, 0.1)
                    )

                    try:
                        message = json.loads(raw)
                    except ValueError:  # pragma: no cover - malformed
                        continue

                    method = message.get("method")
                    params = message.get("params") or {}

                    if method == "Network.webSocketCreated":
                        url = str(params.get("url", ""))
                        if url and url not in sockets:
                            sockets.append(url)
                        continue

                    if method not in (
                        "Network.webSocketFrameReceived",
                        "Network.webSocketFrameSent",
                    ):
                        continue

                    response = params.get("response") or {}
                    frame = decode_frame(
                        str(response.get("payloadData", "")),
                        direction="in" if method.endswith("Received") else "out",
                        opcode=int(response.get("opcode", 1)),
                    )
                    if on_frame is None:
                        frames.append(frame)
                    else:
                        on_frame(frame)
                    summary.add(frame)
        except asyncio.TimeoutError:
            # Nothing arrived before the deadline: the run is simply over.
            break
        except Exception as exc:  # pragma: no cover - socket churn
            log.warning("devtools connection ended: %s", exc)

        if loop.time() >= deadline or len(frames) >= max_frames:
            break
        attempts += 1
        # The tab may be mid-reload. Waiting a moment beats hammering it.
        await asyncio.sleep(min(2.0, max(0.1, deadline - loop.time())))

    return Capture(frames=frames, summary=summary, sockets=sockets)


def record_platform(
    port: int,
    seconds: float = 60.0,
    needle: str = "pocketoption",
    max_frames: int = 4000,
    on_frame: Any = None,
) -> Capture:
    """Find the platform's tab and record it. Synchronous wrapper."""
    targets = list_targets(port)
    target = pick_target(targets, needle)
    if target is None:
        titles = ", ".join(f"{t.title!r}" for t in targets) or "none"
        raise BrowserError(
            f"No tab matching {needle!r} is open in the debugged browser. "
            f"Open the platform in that window first. Tabs seen: {titles}."
        )
    log.info("recording %s", target.url)
    return asyncio.run(
        record(target, seconds=seconds, max_frames=max_frames, on_frame=on_frame)
    )
