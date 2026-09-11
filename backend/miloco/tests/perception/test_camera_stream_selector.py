from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from miloco.config.settings import ExternalCameraStreamSettings
from miloco.perception.collect.camera_adapter import (
    CameraDeviceAdapter,
    _CameraDeviceState,
)
from miloco.perception.collect.camera_stream import MiotCameraVideoStreamSource
from miloco.perception.collect.camera_stream_selector import (
    ConfiguredCameraVideoStreamSource,
    create_camera_video_stream_source,
)
from miloco.perception.types import PerceptionDevice


async def _callback(*args) -> None:
    pass


class _ExternalSource:
    def __init__(self, url: str) -> None:
        self.url = url
        self.starts = []
        self.stops = []

    async def start(self, camera_id, channel, callback):
        self.starts.append((camera_id, channel, callback))
        return 73

    async def stop(self, camera_id, channel, registration_id):
        self.stops.append((camera_id, channel, registration_id))


def _entry(channel: int = 0) -> ExternalCameraStreamSettings:
    return ExternalCameraStreamSettings(
        physical_did="dual",
        channel=channel,
        url="rtsp://user:password@camera.local/live?token=secret",
    )


async def test_channel_override_never_starts_miot_video(monkeypatch):
    created = []

    def make_source(url: str):
        source = _ExternalSource(url)
        created.append(source)
        return source

    monkeypatch.setattr(
        "miloco.perception.collect.camera_stream_selector.RtspCameraVideoStreamSource",
        make_source,
    )
    proxy = MagicMock()
    proxy.start_camera_decode_video_stream = AsyncMock(return_value=12)
    proxy.stop_camera_decode_video_stream = AsyncMock()
    source = ConfiguredCameraVideoStreamSource(proxy, [_entry(0)])

    external_id = await source.start("dual", 0, _callback)
    miot_id = await source.start("dual", 1, _callback)
    await source.stop("dual", 0, external_id)
    await source.stop("dual", 1, miot_id)

    assert external_id == 73
    proxy.start_camera_decode_video_stream.assert_awaited_once()
    assert proxy.start_camera_decode_video_stream.await_args.args[:2] == ("dual", 1)
    assert created[0].stops == [("dual", 0, 73)]
    proxy.stop_camera_decode_video_stream.assert_awaited_once_with("dual", 1, 12)


async def test_factory_without_overrides_preserves_plain_miot_source(monkeypatch):
    proxy = MagicMock()
    proxy.start_camera_decode_video_stream = AsyncMock(return_value=19)
    monkeypatch.setattr(
        "miloco.perception.collect.camera_stream_selector.get_settings",
        lambda: MagicMock(camera=MagicMock(external_streams=[])),
    )
    source = create_camera_video_stream_source(proxy)
    assert isinstance(source, MiotCameraVideoStreamSource)

    registration_id = await source.start("cam", 0, _callback)

    assert registration_id == 19
    proxy.start_camera_decode_video_stream.assert_awaited_once()


async def test_adapter_keeps_miot_audio_with_external_video(monkeypatch):
    external = _ExternalSource("rtsp://camera.local/live")
    proxy = MagicMock()
    proxy.start_camera_decode_video_stream = AsyncMock(return_value=1)
    proxy.start_camera_decode_audio_stream = AsyncMock(return_value=22)
    proxy.stop_camera_decode_audio_stream = AsyncMock()
    monkeypatch.setattr(
        "miloco.perception.collect.camera_stream_selector.RtspCameraVideoStreamSource",
        lambda url: external,
    )
    source = ConfiguredCameraVideoStreamSource(proxy, [_entry()])
    adapter = CameraDeviceAdapter(proxy, video_stream_source=source)
    device = PerceptionDevice(
        did="dual",
        name="dual",
        device_type="camera",
        room_id="r",
        room_name="r",
        online=True,
    )

    await adapter.connect_device("dual", source=device)
    assert "dual" in adapter._devices
    await adapter.disconnect_device("dual")

    proxy.start_camera_decode_video_stream.assert_not_awaited()
    proxy.start_camera_decode_audio_stream.assert_awaited_once()
    assert external.stops == [("dual", 0, 73)]
    proxy.stop_camera_decode_audio_stream.assert_awaited_once_with("dual", 0, 22)


async def test_unavailable_external_stream_never_falls_back_to_miot(monkeypatch):
    external = _ExternalSource("rtsp://offline.local/live")
    proxy = MagicMock()
    proxy.start_camera_decode_video_stream = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "miloco.perception.collect.camera_stream_selector.RtspCameraVideoStreamSource",
        lambda url: external,
    )
    source = ConfiguredCameraVideoStreamSource(proxy, [_entry()])

    registration_id = await source.start("dual", 0, _callback)

    assert registration_id == 73
    proxy.start_camera_decode_video_stream.assert_not_awaited()


async def test_external_silence_does_not_trigger_miot_reconnect(monkeypatch):
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: 200_000
    )
    source = MagicMock()
    source.uses_external_stream.side_effect = lambda did, channel: channel == 0
    proxy = MagicMock()
    proxy.reconnect_camera = AsyncMock()
    adapter = CameraDeviceAdapter(proxy, video_stream_source=source)
    state = _CameraDeviceState(did="dual")
    state.connected_at_ms = 1
    adapter._devices["dual"] = state

    await adapter._check_stalled_cameras()

    proxy.reconnect_camera.assert_not_awaited()
