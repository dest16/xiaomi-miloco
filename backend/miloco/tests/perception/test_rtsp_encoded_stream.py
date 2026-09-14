from __future__ import annotations

import asyncio
import threading
from fractions import Fraction
from types import SimpleNamespace

from miloco.perception.collect.rtsp_encoded_stream import (
    RtspEncodedVideoStreamSource,
)


class _Packet:
    pts = 90
    time_base = Fraction(1, 90)
    is_keyframe = True

    def __bytes__(self) -> bytes:
        return b"\x00\x00\x00\x01\x26\x01payload"


class _Container:
    def __init__(self) -> None:
        codec_context = SimpleNamespace(name="hevc", extradata=b"")
        self.stream = SimpleNamespace(codec_context=codec_context)
        self.streams = SimpleNamespace(video=[self.stream])
        self.closed = threading.Event()

    def demux(self, stream):
        assert stream is self.stream
        yield _Packet()
        self.closed.wait(0.05)

    def close(self) -> None:
        self.closed.set()


async def _wait_until(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met")


async def test_encoded_rtsp_delivers_hevc_without_decoding():
    container = _Container()
    source = RtspEncodedVideoStreamSource(
        "rtsp://camera.local/live", opener=lambda *_args, **_kwargs: container
    )
    received = []

    async def callback(*args) -> None:
        received.append(args)

    registration_id = await source.start("camera", 0, callback)
    try:
        await _wait_until(lambda: bool(received))
    finally:
        await source.stop("camera", 0, registration_id)

    assert received[0][0] == "camera"
    assert received[0][1] == bytes(_Packet())
    assert received[0][2] == 1000
    assert received[0][5:] == ("h265", True)
    assert container.closed.is_set()


def test_length_prefixed_packet_is_converted_to_annexb():
    payload = b"\x67sps"
    packet = len(payload).to_bytes(4, "big") + payload
    assert RtspEncodedVideoStreamSource._to_annexb(packet) == (
        b"\x00\x00\x00\x01" + payload
    )
