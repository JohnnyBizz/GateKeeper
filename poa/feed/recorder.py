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


# The messages in which the page says which chart it is showing. Without one
# of these a recording is a stream of prices belonging to nothing: the replay
# never learns which instrument is being followed, so it builds no candles at
# all, however many ticks arrive.
NAMING_EVENTS = (
    "changeSymbol",
    "saveCharts",
    "updateCharts",
    "loadHistoryPeriod",
    "loadHistoryPeriodFast",
    "changeTimeFrame",
    "updateHistoryNewFast",
)

# How long to wait for the page to name its chart before asking it to reload,
# and how many times to ask. Matched to the live source, which has always done
# this. The recorder did not, so a tab that had been open a while — the normal
# case, since the chart is opened long before anybody thinks to record it —
# captured half an hour of anonymous ticks and produced an empty file.
NAMING_GRACE_SECONDS = 8.0
MAX_RELOADS = 2
RELOAD_COOLDOWN_SECONDS = 45.0


@dataclass
class Capture:
    """What a recording session produced."""

    frames: list[Frame]
    summary: Summary
    sockets: list[str]
    #: Whether the page ever said which chart it was showing. False means the
    #: frames cannot become candles, however much else went right.
    named_a_chart: bool = False

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
    # Whether the page has said which chart it is showing, and what has been
    # done about it if not. A chart is opened long before anybody thinks to
    # record it, so by default those messages are already in the past.
    named = False
    reloads = 0
    reloaded_at: float | None = None

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

                started = loop.time()
                seen = 0
                while loop.time() < deadline and len(frames) < max_frames:
                    # Ask the page to load itself again when it has not said
                    # what it is showing. The live source has always done this;
                    # without it a recording taken against a tab that was
                    # already open collects nothing but anonymous prices, and
                    # the user finds out half an hour later from an empty file.
                    now = loop.time()
                    if (
                        not named
                        # At least one message has to have been read first.
                        # Judging the page before hearing from it would reload
                        # a tab that was in the middle of naming its chart.
                        and seen
                        and reloads < MAX_RELOADS
                        and now - started > NAMING_GRACE_SECONDS
                        and (
                            reloaded_at is None
                            or now - reloaded_at > RELOAD_COOLDOWN_SECONDS
                        )
                    ):
                        reloads += 1
                        reloaded_at = now
                        log.info(
                            "the page has not named its chart; asking it to reload"
                        )
                        await connection.send(
                            json.dumps({"id": 2, "method": "Page.enable"})
                        )
                        await connection.send(
                            json.dumps({"id": 3, "method": "Page.reload", "params": {}})
                        )

                    remaining = deadline - loop.time()
                    raw = await asyncio.wait_for(
                        connection.recv(), timeout=max(remaining, 0.1)
                    )

                    seen += 1
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
                    name = frame.event or frame.announces
                    if name in NAMING_EVENTS:
                        named = True
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

    return Capture(
        frames=frames, summary=summary, sockets=sockets, named_a_chart=named
    )


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
