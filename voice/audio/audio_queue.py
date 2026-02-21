"""Thread-safe FIFO audio queue for TTS output.

Unlike a ring buffer (fixed-size, lossy), this queue:
- Never drops audio — each sentence's PCM is appended to the back
- Drains from the front in 20ms chunks
- Returns silence when empty
"""

import threading
from collections import deque


class AudioQueue:
    """Unbounded FIFO of PCM byte blobs, read out in fixed-size chunks.

    Producers call enqueue() with variable-length PCM blobs (one per sentence).
    The consumer calls read(n) every 20ms to get exactly n bytes, zero-padded
    if not enough data is available yet.
    """

    def __init__(self):
        self._chunks: deque[bytes] = deque()
        self._current = b""
        self._offset = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> int:
        """Total bytes available for reading."""
        with self._lock:
            total = len(self._current) - self._offset
            for chunk in self._chunks:
                total += len(chunk)
            return total

    def enqueue(self, data: bytes):
        """Append a PCM blob to the queue (thread-safe)."""
        if not data:
            return
        with self._lock:
            self._chunks.append(data)

    def read(self, n: int) -> bytes:
        """Read exactly n bytes, advancing through queued chunks.

        Returns silence (zeros) for any bytes beyond what's available.
        """
        with self._lock:
            result = bytearray(n)
            written = 0

            while written < n:
                if self._offset >= len(self._current):
                    if not self._chunks:
                        break
                    self._current = self._chunks.popleft()
                    self._offset = 0

                remaining_in_chunk = len(self._current) - self._offset
                to_copy = min(remaining_in_chunk, n - written)
                result[written:written + to_copy] = self._current[self._offset:self._offset + to_copy]
                self._offset += to_copy
                written += to_copy

            return bytes(result)

    def clear(self):
        """Discard all queued audio."""
        with self._lock:
            self._chunks.clear()
            self._current = b""
            self._offset = 0
