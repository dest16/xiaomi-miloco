"""Select MIoT or external RTSP video per physical camera channel."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from miloco.config import get_settings
from miloco.perception.collect.camera_stream import (
    CameraEncodedVideoStreamSource,
    CameraVideoStreamSource,
    DecodedVideoCallback,
    EncodedVideoCallback,
    MiotCameraVideoStreamSource,
)
from miloco.perception.collect.rtsp_camera_stream import RtspCameraVideoStreamSource
from miloco.perception.collect.rtsp_encoded_stream import RtspEncodedVideoStreamSource

if TYPE_CHECKING:
    from miloco.config.settings import ExternalCameraStreamSettings, RtspCameraSettings
    from miloco.miot.client import MiotProxy

logger = logging.getLogger(__name__)


def _perception_video_fps() -> int:
    try:
        input_cfg = get_settings().perception.engine.get("input", {})
        base_fps = max(1, int(input_cfg.get("fps", 3)))
        omni_fps = max(1, int(input_cfg.get("omni_fps", 1)))
    except (AttributeError, TypeError, ValueError):
        base_fps, omni_fps = 3, 1
    return omni_fps if omni_fps >= base_fps else omni_fps * -(-base_fps // omni_fps)


def _decoded_rtsp_source(url: str) -> RtspCameraVideoStreamSource:
    decode = getattr(get_settings().camera, "rtsp_decode", None)
    if decode is None:
        return RtspCameraVideoStreamSource(url)
    return RtspCameraVideoStreamSource(
        url,
        decoder_backend=decode.backend,
        ffmpeg_path=decode.ffmpeg_path,
        vaapi_device=decode.vaapi_device,
        output_fps=_perception_video_fps(),
    )


class ConfiguredCameraVideoStreamSource:
    """Route each exact physical DID and channel to its configured source."""

    def __init__(
        self,
        miot_proxy: MiotProxy,
        external_streams: Sequence[ExternalCameraStreamSettings],
        rtsp_cameras: Sequence[RtspCameraSettings] = (),
    ) -> None:
        self._miot = MiotCameraVideoStreamSource(miot_proxy)
        self._external: dict[tuple[str, int], RtspCameraVideoStreamSource] = {}
        self._external_urls: dict[tuple[str, int], str] = {}
        for item in external_streams:
            key = (item.physical_did, item.channel)
            if key in self._external:
                raise ValueError("Duplicate external camera stream identity")
            url = item.url.get_secret_value()
            self._external[key] = _decoded_rtsp_source(url)
            self._external_urls[key] = url
        for item in rtsp_cameras:
            key = (item.id, 0)
            if key in self._external:
                raise ValueError("Duplicate external camera stream identity")
            url = item.url.get_secret_value()
            self._external[key] = _decoded_rtsp_source(url)
            self._external_urls[key] = url
        self._active: dict[tuple[str, int, int], CameraVideoStreamSource] = {}
        self._lock = asyncio.Lock()

    def uses_external_stream(self, camera_id: str, channel: int) -> bool:
        key = (camera_id, channel)
        if key in self._external:
            return True
        return channel == 0 and any(
            item.id == camera_id for item in get_settings().camera.rtsp_cameras
        )

    def _refresh_standalone_source(
        self, camera_id: str, channel: int
    ) -> None:
        if channel != 0:
            return
        configured = next(
            (
                item
                for item in get_settings().camera.rtsp_cameras
                if item.id == camera_id
            ),
            None,
        )
        if configured is None:
            return
        key = (camera_id, channel)
        url = configured.url.get_secret_value()
        if self._external_urls.get(key) == url:
            return
        if any(active[:2] == key for active in self._active):
            raise RuntimeError(
                "RTSP URL changed while the camera is active; disable it before editing"
            )
        self._external[key] = _decoded_rtsp_source(url)
        self._external_urls[key] = url

    async def start(
        self,
        camera_id: str,
        channel: int,
        callback: DecodedVideoCallback,
    ) -> int:
        async with self._lock:
            self._refresh_standalone_source(camera_id, channel)
            source: CameraVideoStreamSource = self._external.get(
                (camera_id, channel), self._miot
            )
            registration_id = await source.start(camera_id, channel, callback)
            if registration_id >= 0:
                self._active[(camera_id, channel, registration_id)] = source
            if source is not self._miot:
                logger.info(
                    "camera=%s channel=%d video_source=external_rtsp miot_video_bypassed=true",
                    camera_id,
                    channel,
                )
            return registration_id

    async def stop(
        self,
        camera_id: str,
        channel: int,
        registration_id: int,
    ) -> None:
        async with self._lock:
            source = self._active.pop((camera_id, channel, registration_id), None)
            if source is not None:
                await source.stop(camera_id, channel, registration_id)


def create_camera_video_stream_source(
    miot_proxy: MiotProxy,
) -> CameraVideoStreamSource:
    """Keep the original MIoT implementation unless overrides are configured."""
    camera = get_settings().camera
    external_streams = camera.external_streams
    rtsp_cameras = getattr(camera, "rtsp_cameras", [])
    if not external_streams and not rtsp_cameras:
        return MiotCameraVideoStreamSource(miot_proxy)
    return ConfiguredCameraVideoStreamSource(
        miot_proxy,
        external_streams,
        rtsp_cameras,
    )


class ConfiguredCameraEncodedVideoStreamSource:
    """Route configured external cameras to compressed RTSP packet readers."""

    def __init__(
        self,
        external_streams: Sequence[ExternalCameraStreamSettings],
        rtsp_cameras: Sequence[RtspCameraSettings] = (),
    ) -> None:
        self._sources: dict[tuple[str, int], RtspEncodedVideoStreamSource] = {}
        self._urls: dict[tuple[str, int], str] = {}
        for item in external_streams:
            self._add((item.physical_did, item.channel), item.url.get_secret_value())
        for item in rtsp_cameras:
            self._add((item.id, 0), item.url.get_secret_value())
        self._active: dict[tuple[str, int, int], CameraEncodedVideoStreamSource] = {}
        self._lock = asyncio.Lock()

    def _add(self, key: tuple[str, int], url: str) -> None:
        if key in self._sources:
            raise ValueError("Duplicate external camera stream identity")
        self._sources[key] = RtspEncodedVideoStreamSource(url)
        self._urls[key] = url

    def supports(self, camera_id: str, channel: int) -> bool:
        return (camera_id, channel) in self._sources or (
            channel == 0
            and any(item.id == camera_id for item in get_settings().camera.rtsp_cameras)
        )

    def _refresh(self, camera_id: str, channel: int) -> None:
        if channel != 0:
            return
        configured = next(
            (item for item in get_settings().camera.rtsp_cameras if item.id == camera_id),
            None,
        )
        if configured is None:
            return
        key = (camera_id, channel)
        url = configured.url.get_secret_value()
        if self._urls.get(key) == url:
            return
        if any(active[:2] == key for active in self._active):
            raise RuntimeError("RTSP URL changed while the camera is active")
        self._sources[key] = RtspEncodedVideoStreamSource(url)
        self._urls[key] = url

    async def start(
        self, camera_id: str, channel: int, callback: EncodedVideoCallback
    ) -> int:
        async with self._lock:
            self._refresh(camera_id, channel)
            source = self._sources.get((camera_id, channel))
            if source is None:
                return -1
            registration_id = await source.start(camera_id, channel, callback)
            if registration_id >= 0:
                self._active[(camera_id, channel, registration_id)] = source
            return registration_id

    async def stop(
        self, camera_id: str, channel: int, registration_id: int
    ) -> None:
        async with self._lock:
            source = self._active.pop((camera_id, channel, registration_id), None)
            if source is not None:
                await source.stop(camera_id, channel, registration_id)


def create_camera_encoded_video_stream_source() -> ConfiguredCameraEncodedVideoStreamSource:
    camera = get_settings().camera
    return ConfiguredCameraEncodedVideoStreamSource(
        getattr(camera, "external_streams", []),
        getattr(camera, "rtsp_cameras", []),
    )
