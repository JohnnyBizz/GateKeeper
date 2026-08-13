"""Decoding WebSocket frames captured from the browser.

The Chrome DevTools Protocol hands back a frame's payload as a string: text
frames verbatim, binary frames base64-encoded. What is inside is the platform's
business, and it is usually one of a small number of shapes:

* plain JSON;
* Socket.IO, which prefixes JSON with a small numeric packet type — ``42["ticks",
  [...]]`` is an event, ``0``/``40`` are handshakes, ``2``/``3`` are heartbeats;
* something binary, which is reported as such rather than guessed at.

Nothing here knows what a candle looks like. Its job is to turn a capture into
something readable, so the shape of the feed can be worked out from a sample
rather than assumed.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .redact import redact, redact_text

# Socket.IO packets: an engine.io digit, optionally a socket.io digit, then the
# payload. "42" is by far the most common — an engine.io MESSAGE carrying a
# socket.io EVENT.
_SOCKET_IO = re.compile(r"^(\d{1,2})(.*)$", re.DOTALL)

# "451-[\"updateStream\",{\"_placeholder\":true,...}]" — engine.io MESSAGE,
# socket.io BINARY_EVENT, one attachment to follow.
_BINARY_EVENT = re.compile(r"^45(\d+)-(.*)$", re.DOTALL)

# engine.io packet types, for describing a frame that carries no payload.
_ENGINE_IO = {
    "0": "open", "1": "close", "2": "ping", "3": "pong",
    "4": "message", "5": "upgrade", "6": "noop",
}


@dataclass
class Frame:
    """One decoded frame, with the payload already redacted."""

    direction: str  # "in" | "out"
    opcode: int
    kind: str  # "json" | "socket.io" | "text" | "binary" | "empty"
    event: str | None = None
    payload: Any = None
    raw_length: int = 0
    note: str = ""
    # Set on a binary-event header: the name the next frame's payload owns.
    announces: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "opcode": self.opcode,
            "kind": self.kind,
            "event": self.event,
            "payload": self.payload,
            "raw_length": self.raw_length,
            "note": self.note,
            "announces": self.announces,
        }


def _try_json(text: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(text)
    except (ValueError, TypeError):
        return False, None


def decode_frame(payload: str, direction: str = "in", opcode: int = 1) -> Frame:
    """Turn a CDP frame payload into something readable and safe to share."""
    raw_length = len(payload or "")

    if opcode == 2:  # binary
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            data = b""
        # Binary frames are sometimes just JSON in disguise.
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return Frame(
                direction=direction,
                opcode=opcode,
                kind="binary",
                raw_length=raw_length,
                note=f"{len(data)} bytes, not UTF-8",
            )
        return _decode_text(text, direction, opcode, raw_length)

    if not payload:
        return Frame(direction=direction, opcode=opcode, kind="empty")

    return _decode_text(payload, direction, opcode, raw_length)


def _decode_text(text: str, direction: str, opcode: int, raw_length: int) -> Frame:
    # Heartbeats first. A bare "2" is a ping, but it is also valid JSON, and
    # letting the JSON branch claim it buries the socket's keepalive traffic
    # among the data as an unnamed event.
    stripped = text.strip()
    if len(stripped) <= 2 and stripped.isdigit():
        return Frame(
            direction=direction,
            opcode=opcode,
            kind="socket.io",
            event=_ENGINE_IO.get(stripped[0], f"packet {stripped}"),
            raw_length=raw_length,
        )

    ok, parsed = _try_json(text)
    if ok:
        return Frame(
            direction=direction,
            opcode=opcode,
            kind="json",
            event=_event_name(parsed),
            payload=redact(parsed),
            raw_length=raw_length,
        )

    # Socket.IO's binary-event header: "451-" is a MESSAGE / BINARY_EVENT
    # announcing one attachment, whose payload lands in the *next* frame. The
    # name lives here and the data lives there, so remember it to reunite them.
    binary_header = _BINARY_EVENT.match(text)
    if binary_header:
        ok, parsed = _try_json(binary_header.group(2))
        name = _event_name(parsed) if ok else None
        return Frame(
            direction=direction,
            opcode=opcode,
            kind="socket.io",
            event=name,
            raw_length=raw_length,
            note=f"header for {binary_header.group(1)} binary attachment(s)",
            announces=name,
        )

    match = _SOCKET_IO.match(text)
    if match:
        prefix, rest = match.group(1), match.group(2)
        if not rest.strip():
            return Frame(
                direction=direction,
                opcode=opcode,
                kind="socket.io",
                event=_ENGINE_IO.get(prefix[0], f"packet {prefix}"),
                raw_length=raw_length,
            )
        ok, parsed = _try_json(rest)
        if ok:
            return Frame(
                direction=direction,
                opcode=opcode,
                kind="socket.io",
                event=_event_name(parsed),
                payload=redact(parsed),
                raw_length=raw_length,
                note=f"engine.io prefix {prefix}",
            )

    return Frame(
        direction=direction,
        opcode=opcode,
        kind="text",
        payload=redact_text(text[:400]),
        raw_length=raw_length,
    )


def _event_name(parsed: Any) -> str | None:
    """Socket.IO events arrive as ``[name, payload...]``."""
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
        return parsed[0][:60]
    if isinstance(parsed, dict):
        for key in ("event", "type", "action", "name", "cmd"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value[:60]
    return None


def describe_shape(value: Any, _depth: int = 0) -> str:
    """A short signature of a payload's structure, ignoring its values.

    Two frames with the same shape are the same message; two with different
    shapes are different messages even when the platform names neither. This
    is what separates a price tick from a block of history when both arrive as
    anonymous JSON arrays.
    """
    if _depth > 3:
        return "…"
    if isinstance(value, dict):
        keys = sorted(str(k) for k in value)[:6]
        return "{" + ",".join(keys) + "}" if keys else "{}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        return f"[{len(value)}x {describe_shape(value[0], _depth + 1)}]"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


@dataclass
class Summary:
    """What a capture contained, in a form worth pasting into a message."""

    total: int = 0
    by_event: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    samples: dict[str, Any] = field(default_factory=dict)
    _pending_name: str | None = field(default=None, repr=False)

    def add(self, frame: Frame) -> None:
        # A binary attachment inherits the name from the header before it,
        # which is the only place that name appears.
        if self._pending_name and frame.event is None and frame.payload is not None:
            frame.event = self._pending_name
        self._pending_name = frame.announces

        self.total += 1
        self.by_kind[frame.kind] = self.by_kind.get(frame.kind, 0) + 1
        # Unnamed frames are grouped by their *shape*, not lumped together.
        # Keeping one example per name hid the message that mattered most: a
        # capture of 518 unnamed price ticks and one unnamed history block
        # showed a tick and threw the history away.
        name = frame.event or f"({frame.kind}) {describe_shape(frame.payload)}"
        self.by_event[name] = self.by_event.get(name, 0) + 1
        if name not in self.samples and frame.payload is not None:
            self.samples[name] = frame.payload

    def render(self, max_events: int = 25) -> str:
        lines = [f"{self.total} frames captured", ""]
        lines.append("Frame kinds: " + ", ".join(
            f"{kind} x{count}" for kind, count in sorted(self.by_kind.items())
        ))
        lines.append("")
        lines.append("Events, most frequent first:")
        ranked = sorted(self.by_event.items(), key=lambda kv: -kv[1])
        for name, count in ranked[:max_events]:
            lines.append(f"  {count:6d}  {name}")
        lines.append("")
        lines.append("One example of each (secrets already removed):")
        for name, _count in ranked[:max_events]:
            sample = self.samples.get(name)
            if sample is None:
                continue
            text = json.dumps(sample, default=str)
            if len(text) > 600:
                text = text[:600] + " …"
            lines.append(f"  {name}: {text}")
        return "\n".join(lines)
