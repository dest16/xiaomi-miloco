"""Encoded RTSP packet source used by the Web live-view passthrough path."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, Future, TimeoutError
from typing import Any

import av

from miloco.perception.collect.camera_stream import EncodedVideoCallback
from miloco.perception.collect.rtsp_camera_stream import (
    _DEFAULT_OPEN_TIMEOUT_SECONDS,
    _DEFAULT_READ_TIMEOUT_SECONDS,
    _DEFAULT_RECONNECT_BACKOFF_SECONDS,
    _DEFAULT_RTSP_OPTIONS,
    redact_rtsp_url,
)

logger = logging.getLogger(__name__)


class RtspEncodedVideoStreamSource:
    """Demux one RTSP stream and deliver compressed Annex-B access units."""

    def __init__(
        self,
        url: str,
        *,
        reconnect_backoff_seconds: Sequence[float] = (
            _DEFAULT_RECONNECT_BACKOFF_SECONDS
        ),
        open_timeout_seconds: float = _DEFAULT_OPEN_TIMEOUT_SECONDS,
        read_timeout_seconds: float = _DEFAULT_READ_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._url = url
        self._safe_url = redact_rtsp_url(url)
        self._backoff = tuple(reconnect_backoff_seconds)
        self._open_timeout = open_timeout_seconds
        self._read_timeout = read_timeout_seconds
        self._opener = opener or av.open
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._container: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callback: EncodedVideoCallback | None = None
        self._camera_id: str | None = None
        self._channel = 0
        self._registration_id = -1
        self._next_registration_id = 0
        self._sequence = 0
        self._last_timestamp_ms = -1
        self._pending_callbacks: set[Future[None]] = set()

    async def start(
        self, camera_id: str, channel: int, callback: EncodedVideoCallback
    ) -> int:
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                if (camera_id, channel) != (self._camera_id, self._channel):
                    raise RuntimeError("RTSP source is already assigned to another camera")
                return self._registration_id
            self._stop_event.clear()
            self._loop = loop
            self._callback = callback
            self._camera_id = camera_id
            self._channel = channel
            self._registration_id = self._next_registration_id
            self._next_registration_id += 1
            self._sequence = 0
            self._last_timestamp_ms = -1
            registration_id = self._registration_id
            self._thread = threading.Thread(
                target=self._reader_main,
                name=f"rtsp-passthrough-{camera_id}",
                daemon=True,
            )
            self._thread.start()
        return registration_id

    async def stop(
        self, camera_id: str, channel: int, registration_id: int
    ) -> None:
        with self._state_lock:
            if (
                self._thread is None
                or (camera_id, channel) != (self._camera_id, self._channel)
                or registration_id != self._registration_id
            ):
                return
            self._stop_event.set()
            thread = self._thread
        if thread is not threading.current_thread():
            await asyncio.to_thread(thread.join)
        with self._state_lock:
            pending = tuple(self._pending_callbacks)
            self._thread = None
            self._container = None
            self._loop = None
            self._callback = None
            self._camera_id = None
            self._registration_id = -1
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in pending),
                return_exceptions=True,
            )

    def _reader_main(self) -> None:
        backoff_index = 0
        while not self._stop_event.is_set():
            container: Any | None = None
            try:
                container = self._opener(
                    self._url,
                    mode="r",
                    options=dict(_DEFAULT_RTSP_OPTIONS),
                    timeout=(self._open_timeout, self._read_timeout),
                )
                with self._state_lock:
                    self._container = container
                stream = container.streams.video[0]
                codec = self._codec_name(stream)
                parameter_sets = self._annexb_extradata(stream, codec)
                backoff_index = 0
                logger.info("RTSP preview passthrough connected to %s codec=%s", self._safe_url, codec)
                for packet in container.demux(stream):
                    if self._stop_event.is_set():
                        break
                    data = self._to_annexb(bytes(packet))
                    if not data:
                        continue
                    is_keyframe = bool(getattr(packet, "is_keyframe", False))
                    if is_keyframe and parameter_sets and not self._has_parameter_set(data, codec):
                        data = parameter_sets + data
                    self._deliver_packet(packet, data, codec, is_keyframe)
            except Exception as exc:  # noqa: BLE001
                if not self._stop_event.is_set():
                    logger.warning(
                        "RTSP preview passthrough disconnected from %s (%s)",
                        self._safe_url,
                        type(exc).__name__,
                    )
            finally:
                if container is not None:
                    with self._state_lock:
                        if self._container is container:
                            self._container = None
                    try:
                        container.close()
                    except Exception:  # noqa: BLE001
                        pass
            if self._stop_event.is_set():
                break
            delay = self._backoff[min(backoff_index, len(self._backoff) - 1)]
            backoff_index += 1
            if self._stop_event.wait(delay):
                break

    @staticmethod
    def _codec_name(stream: Any) -> str:
        name = str(getattr(stream.codec_context, "name", "h264")).lower()
        if name in {"hevc", "h265"}:
            return "h265"
        if name == "h264":
            return "h264"
        raise ValueError(f"Unsupported browser passthrough codec: {name}")

    @staticmethod
    def _to_annexb(data: bytes) -> bytes:
        if data.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
            return data
        # Some demuxers expose length-prefixed NAL units. RTSP normally emits
        # Annex-B, but converting this common representation is inexpensive.
        output = bytearray()
        offset = 0
        while offset + 4 <= len(data):
            size = int.from_bytes(data[offset : offset + 4], "big")
            offset += 4
            if size <= 0 or offset + size > len(data):
                return b""
            output.extend(b"\x00\x00\x00\x01")
            output.extend(data[offset : offset + size])
            offset += size
        return bytes(output) if offset == len(data) else b""

    @classmethod
    def _annexb_extradata(cls, stream: Any, codec: str) -> bytes:
        data = bytes(getattr(stream.codec_context, "extradata", b"") or b"")
        annexb = cls._to_annexb(data)
        return annexb if annexb and cls._has_parameter_set(annexb, codec) else b""

    @staticmethod
    def _has_parameter_set(data: bytes, codec: str) -> bool:
        starts: list[int] = []
        for index in range(max(0, len(data) - 3)):
            if data[index : index + 4] == b"\x00\x00\x00\x01":
                starts.append(index + 4)
            elif data[index : index + 3] == b"\x00\x00\x01":
                starts.append(index + 3)
        for start in starts:
            if start >= len(data):
                continue
            nal_type = data[start] & 0x1F if codec == "h264" else (data[start] >> 1) & 0x3F
            if (codec == "h264" and nal_type == 7) or (codec == "h265" and nal_type == 33):
                return True
        return False

    def _deliver_packet(
        self, packet: Any, data: bytes, codec: str, is_keyframe: bool
    ) -> None:
        pts = getattr(packet, "pts", None)
        time_base = getattr(packet, "time_base", None)
        try:
            timestamp_ms = int(pts * time_base * 1000)
        except (TypeError, ValueError, OverflowError):
            timestamp_ms = time.monotonic_ns() // 1_000_000
        with self._state_lock:
            if timestamp_ms <= self._last_timestamp_ms:
                timestamp_ms = self._last_timestamp_ms + 1
            self._last_timestamp_ms = timestamp_ms
            loop = self._loop
            callback = self._callback
            camera_id = self._camera_id
            channel = self._channel
            sequence = self._sequence
            self._sequence += 1
        if loop is None or loop.is_closed() or callback is None or camera_id is None:
            return
        callback_coro = callback(
            camera_id, data, timestamp_ms, sequence, channel, codec, is_keyframe
        )
        try:
            future = asyncio.run_coroutine_threadsafe(callback_coro, loop)
        except RuntimeError:
            callback_coro.close()
            return
        with self._state_lock:
            self._pending_callbacks.add(future)
        future.add_done_callback(self._callback_done)
        while not self._stop_event.is_set():
            try:
                future.result(timeout=0.1)
                return
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001
                return
        future.cancel()

    def _callback_done(self, future: Future[None]) -> None:
        with self._state_lock:
            self._pending_callbacks.discard(future)
        try:
            error = future.exception()
        except CancelledError:
            return
        if error is not None:
            logger.warning("RTSP passthrough callback failed (%s)", type(error).__name__)
