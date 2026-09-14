from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from miloco.miot import ws as ws_module
from miloco.miot.ws import MIoTVideoStreamManager
from starlette.websockets import WebSocketState


class _WebSocket:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.text: list[str] = []
        self.binary: list[bytes] = []

    async def send_text(self, value: str) -> None:
        self.text.append(value)

    async def send_bytes(self, value: bytes) -> None:
        self.binary.append(value)

    async def close(self) -> None:
        self.client_state = WebSocketState.DISCONNECTED


async def test_external_rtsp_preview_is_passthrough_and_recorder_stays_decoded(
    monkeypatch,
):
    service = SimpleNamespace(
        supports_encoded_video_stream=lambda did, channel: did == "rtsp" and channel == 0,
        start_encoded_video_stream=AsyncMock(return_value=31),
        stop_encoded_video_stream=AsyncMock(),
        start_video_stream=AsyncMock(return_value=41),
        stop_video_stream=AsyncMock(),
    )
    monkeypatch.setattr(ws_module, "manager", SimpleNamespace(miot_service=service))
    stream_manager = MIoTVideoStreamManager()
    socket = _WebSocket()

    connection_id = await stream_manager.new_connection(
        socket, "user", "token", "rtsp", 0
    )

    service.start_encoded_video_stream.assert_awaited_once()
    service.start_video_stream.assert_not_awaited()
    assert "rtsp.0" not in stream_manager._camera_encoder

    callback = stream_manager._MIoTVideoStreamManager__encoded_video_stream_callback
    await callback(
        "rtsp",
        b"\x00\x00\x00\x01\x26frame",
        1234,
        1,
        0,
        "h265",
        True,
    )

    assert json.loads(socket.text[0])["codec"] == "h265"
    assert socket.binary[0][0] == 1
    assert socket.binary[0][16:] == b"\x00\x00\x00\x01\x26frame"

    recorder = SimpleNamespace(feed_bgr=AsyncMock())
    await stream_manager.register_recorder("rtsp", 0, recorder)
    service.start_video_stream.assert_awaited_once()
    assert "rtsp.0" not in stream_manager._camera_encoder

    await stream_manager.unregister_recorder("rtsp", 0, recorder)
    service.stop_video_stream.assert_awaited_once_with("rtsp", 0, 41)
    service.stop_encoded_video_stream.assert_not_awaited()

    await stream_manager.close_connection(
        "user", "token", "rtsp", 0, connection_id
    )
    service.stop_encoded_video_stream.assert_awaited_once_with("rtsp", 0, 31)
