"""RTSP camera stream source lifecycle and frame-boundary tests."""

from __future__ import annotations

import asyncio
import logging
import threading
from fractions import Fraction
from time import monotonic
from unittest.mock import AsyncMock, MagicMock

import numpy as np
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.rtsp_camera_stream import (
    RtspCameraVideoStreamSource,
    redact_rtsp_url,
)
from miloco.perception.types import PerceptionDevice


class _FakeFrame:
    def __init__(self, pixels: np.ndarray, pts: int | None) -> None:
        self.pixels = pixels
        self.pts = pts
        self.time_base = Fraction(1, 1000)
        self.formats: list[str] = []

    def to_ndarray(self, *, format: str) -> np.ndarray:
        self.formats.append(format)
        return self.pixels


class _FakeStreams:
    def __init__(self) -> None:
        self.video = [object()]


class _FakeContainer:
    def __init__(
        self,
        frames: list[_FakeFrame] | None = None,
        decode_error: Exception | None = None,
    ) -> None:
        self.streams = _FakeStreams()
        self.frames = frames or []
        self.decode_error = decode_error
        self.closed = threading.Event()
        self.decode_released = threading.Event()
        self.close_calls = 0
        self.decode_thread_id: int | None = None
        self.close_thread_ids: list[int] = []

    def decode(self, stream: object):
        assert stream is self.streams.video[0]
        self.decode_thread_id = threading.get_ident()
        yield from self.frames
        if self.decode_error is not None:
            raise self.decode_error
        self.decode_released.wait(0.05)

    def close(self) -> None:
        self.close_calls += 1
        self.close_thread_ids.append(threading.get_ident())
        self.closed.set()


class _SequenceOpener:
    def __init__(self, *results: _FakeContainer | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, **kwargs):
        with self._lock:
            self.calls.append((url, kwargs))
            if not self.results:
                raise RuntimeError("no more fake containers")
            result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.005)


async def _discard_frame(*_args) -> None:
    return None


def _camera(did: str = "cam1") -> PerceptionDevice:
    return PerceptionDevice(
        did=did,
        name=did,
        device_type="camera",
        room_id="room",
        room_name="room",
        online=True,
    )


async def test_start_and_stop_own_reader_and_container():
    container = _FakeContainer()
    opener = _SequenceOpener(container)
    source = RtspCameraVideoStreamSource(
        "rtsp://camera.local/live",
        opener=opener,
    )

    registration_id = await source.start("cam1", 0, _discard_frame)
    await _wait_until(lambda: container.decode_thread_id is not None)
    await source.stop("cam1", 0, registration_id)

    assert not source.running
    assert container.closed.is_set()
    assert container.close_thread_ids == [container.decode_thread_id]
    assert opener.calls[0][1]["options"] == {"rtsp_transport": "tcp"}
    assert opener.calls[0][1]["timeout"] == (5.0, 5.0)


async def test_frame_delivery_is_bgr_and_timestamp_is_monotonic():
    first_pixels = np.arange(18, dtype=np.uint8).reshape((2, 3, 3))
    second_pixels = np.full((2, 3, 3), 7, dtype=np.uint8)
    frames = [_FakeFrame(first_pixels, 1000), _FakeFrame(second_pixels, 900)]
    container = _FakeContainer(frames)
    opener = _SequenceOpener(container)
    source = RtspCameraVideoStreamSource(
        "rtsp://camera.local/live",
        opener=opener,
    )
    received: list[tuple] = []

    async def callback(*args) -> None:
        received.append(args)

    registration_id = await source.start("cam1", 2, callback)
    try:
        await _wait_until(lambda: len(received) == 2)
    finally:
        await source.stop("cam1", 2, registration_id)

    assert frames[0].formats == ["bgr24"]
    assert frames[1].formats == ["bgr24"]
    assert np.array_equal(received[0][1], first_pixels)
    assert received[0][1].shape == (2, 3, 3)
    assert received[0][2] == 1000
    assert received[1][2] == 1001
    assert received[0][3] == 2
    assert received[0][4] > 0
    assert received[0][5] >= received[0][4]


async def test_decode_failure_reconnects_and_frames_resume():
    first_frame = _FakeFrame(np.ones((1, 2, 3), dtype=np.uint8), 100)
    failed = _FakeContainer([first_frame], decode_error=RuntimeError("decode failed"))
    recovered_frame = _FakeFrame(np.zeros((1, 2, 3), dtype=np.uint8), 5)
    recovered = _FakeContainer([recovered_frame])
    opener = _SequenceOpener(failed, recovered)
    source = RtspCameraVideoStreamSource(
        "rtsp://camera.local/live",
        reconnect_backoff_seconds=(0.001,),
        opener=opener,
    )
    received: list[tuple[np.ndarray, int]] = []

    async def callback(_did, frame, timestamp, *_rest) -> None:
        received.append((frame, timestamp))

    registration_id = await source.start("cam1", 0, callback)
    try:
        await _wait_until(lambda: len(received) == 2)
    finally:
        await source.stop("cam1", 0, registration_id)

    assert len(opener.calls) == 2
    assert failed.closed.is_set()
    assert recovered.closed.is_set()
    assert np.array_equal(received[1][0], recovered_frame.pixels)
    assert [item[1] for item in received] == [100, 101]


async def test_auto_vaapi_failure_falls_back_to_pyav(monkeypatch, caplog):
    frame = _FakeFrame(np.ones((1, 2, 3), dtype=np.uint8), 100)
    opener = _SequenceOpener(_FakeContainer([frame]))
    source = RtspCameraVideoStreamSource(
        "rtsp://camera.local/live",
        decoder_backend="auto",
        reconnect_backoff_seconds=(10.0,),
    )
    source._opener = opener

    def fail_vaapi() -> None:
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(source, "_read_ffmpeg_vaapi_once", fail_vaapi)
    received = []

    async def callback(*args) -> None:
        received.append(args)

    caplog.set_level(logging.WARNING)
    registration_id = await source.start("cam1", 0, callback)
    try:
        await _wait_until(lambda: bool(received))
    finally:
        await source.stop("cam1", 0, registration_id)

    assert source._vaapi_disabled is True
    assert len(opener.calls) == 1
    assert "falling back to PyAV" in caplog.text


async def test_stop_interrupts_reconnect_backoff(caplog):
    opener = _SequenceOpener(RuntimeError("offline"))
    source = RtspCameraVideoStreamSource(
        "rtsp://camera.local/live",
        reconnect_backoff_seconds=(10.0,),
        opener=opener,
    )
    caplog.set_level(logging.INFO)

    registration_id = await source.start("cam1", 0, _discard_frame)
    await _wait_until(lambda: "Reconnecting RTSP reader" in caplog.text)
    started = monotonic()
    await source.stop("cam1", 0, registration_id)

    assert monotonic() - started < 0.5
    assert not source.running


async def test_stop_cancels_in_flight_callback():
    container = _FakeContainer([_FakeFrame(np.zeros((1, 1, 3), dtype=np.uint8), 1)])
    source = RtspCameraVideoStreamSource(
        "rtsp://camera.local/live",
        opener=_SequenceOpener(container),
    )
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def callback(*_args) -> None:
        callback_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            callback_cancelled.set()
            raise

    registration_id = await source.start("cam1", 0, callback)
    await asyncio.wait_for(callback_started.wait(), timeout=1.0)
    await asyncio.wait_for(source.stop("cam1", 0, registration_id), timeout=1.0)

    assert callback_cancelled.is_set()
    assert not source.running


async def test_duplicate_start_reuses_reader():
    container = _FakeContainer()
    opener = _SequenceOpener(container)
    source = RtspCameraVideoStreamSource(
        "rtsp://camera.local/live",
        opener=opener,
    )

    first_id = await source.start("cam1", 0, _discard_frame)
    second_id = await source.start("cam1", 0, _discard_frame)
    await _wait_until(lambda: bool(opener.calls))
    await source.stop("cam1", 0, first_id)

    assert first_id == second_id
    assert len(opener.calls) == 1


async def test_logs_redact_credentials_and_query_tokens(caplog):
    url = "rtsp://username:password@camera.local:8554/live?token=top-secret"
    safe_url = "rtsp://***:***@camera.local:8554/live"
    opener = _SequenceOpener(RuntimeError("offline"))
    source = RtspCameraVideoStreamSource(
        url,
        reconnect_backoff_seconds=(10.0,),
        opener=opener,
    )
    caplog.set_level(logging.INFO)

    registration_id = await source.start("cam1", 0, _discard_frame)
    await _wait_until(lambda: "Initial RTSP connection failed" in caplog.text)
    await source.stop("cam1", 0, registration_id)

    assert redact_rtsp_url(url) == safe_url
    assert safe_url in caplog.text
    assert "username" not in caplog.text
    assert "password" not in caplog.text
    assert "top-secret" not in caplog.text


async def test_camera_adapter_receives_rtsp_frame_and_stops_source():
    pixels = np.full((2, 2, 3), 11, dtype=np.uint8)
    container = _FakeContainer([_FakeFrame(pixels, 10)])
    source = RtspCameraVideoStreamSource(
        "rtsp://camera.local/live",
        opener=_SequenceOpener(container),
    )
    proxy = MagicMock()
    proxy.start_camera_decode_audio_stream = AsyncMock(return_value=-1)
    proxy.stop_camera_decode_audio_stream = AsyncMock()
    proxy.get_cached_camera = MagicMock(return_value=None)
    adapter = CameraDeviceAdapter(miot_proxy=proxy, video_stream_source=source)

    await adapter.connect_device("cam1", source=_camera())
    await _wait_until(lambda: adapter.peek_latest_frame("cam1") is not None)
    delivered = adapter.peek_latest_frame("cam1")
    await adapter.disconnect_device("cam1")

    assert np.array_equal(delivered, pixels)
    assert container.closed.is_set()
    assert not source.running
