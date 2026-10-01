"""Bounded incremental SSE observer for usage telemetry.

Proxies raw bytes unchanged. Retains only small parser state needed to
identify usage metadata. Never stores generated text beyond the active
event buffer bound.
"""

from __future__ import annotations

import json
from typing import Any

from app.runtime.usage import TokenUsage, extract_token_usage

# Cap incomplete-event buffer so a pathological stream cannot grow memory.
DEFAULT_MAX_EVENT_BUFFER_BYTES = 64 * 1024


class SseUsageObserver:
    """Observe SSE byte chunks and capture the last valid usage object."""

    def __init__(self, *, max_event_buffer_bytes: int = DEFAULT_MAX_EVENT_BUFFER_BYTES) -> None:
        self._buf = bytearray()
        self._max = max(1024, int(max_event_buffer_bytes))
        self._usage: TokenUsage | None = None
        self._drop_current = False

    @property
    def usage(self) -> TokenUsage | None:
        return self._usage

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf.extend(chunk)
        while True:
            sep = self._find_event_separator(self._buf)
            if sep is None:
                if len(self._buf) > self._max:
                    # Drop telemetry for the oversized incomplete event; keep
                    # trailing bytes that may start the next event once we
                    # find a separator in a later chunk.
                    self._drop_current = True
                    # Retain only the last few bytes in case a separator is
                    # split across the bound (e.g. trailing "\\r").
                    keep = min(8, len(self._buf))
                    if keep:
                        del self._buf[:-keep]
                    else:
                        self._buf.clear()
                break
            event_bytes = bytes(self._buf[: sep[0]])
            del self._buf[: sep[1]]
            if self._drop_current:
                self._drop_current = False
                continue
            if len(event_bytes) > self._max:
                continue
            self._consume_event(event_bytes)

    def finish(self) -> TokenUsage | None:
        """Flush any trailing event without a final blank line."""
        if self._buf and not self._drop_current and len(self._buf) <= self._max:
            self._consume_event(bytes(self._buf))
        self._buf.clear()
        return self._usage

    @staticmethod
    def _find_event_separator(buf: bytearray) -> tuple[int, int] | None:
        """Return (content_end, consume_end) for the earliest SSE event boundary.

        Mixed LF / CRLF streams must choose the first boundary in buffer order,
        not prefer CRLF globally (which can merge earlier LF-terminated events).
        """
        crlf = buf.find(b"\r\n\r\n")
        lf = buf.find(b"\n\n")
        candidates: list[tuple[int, int]] = []
        if crlf >= 0:
            candidates.append((crlf, crlf + 4))
        if lf >= 0:
            candidates.append((lf, lf + 2))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])

    def _consume_event(self, event_bytes: bytes) -> None:
        data_parts: list[str] = []
        try:
            text = event_bytes.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return
        for raw_line in text.splitlines():
            line = raw_line.strip("\r")
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_parts.append(line[5:].lstrip(" "))
            # Ignore event:/id:/retry: — usage lives in data JSON.
        if not data_parts:
            return
        payload_text = "\n".join(data_parts).strip()
        if not payload_text or payload_text == "[DONE]":
            return
        try:
            payload: Any = json.loads(payload_text)
        except Exception:  # noqa: BLE001
            return
        usage = extract_token_usage(payload, missing_output_is_zero=False)
        if usage is not None:
            self._usage = usage
