"""Select MIoT or external RTSP video per physical camera channel."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from miloco.config import get_settings
from miloco.perception.collect.camera_stream import (
    CameraVideoStreamSource,
    DecodedVideoCallback,
    MiotCameraVideoStreamSource,
)
from miloco.perception.collect.rtsp_camera_stream import RtspCameraVideoStreamSource

if TYPE_CHECKING:
    from miloco.config.settings import ExternalCameraStreamSettings
    from miloco.miot.client import MiotProxy

logger = logging.getLogger(__name__)


class ConfiguredCameraVideoStreamSource:
    """Route each exact physical DID and channel to its configured source."""

    def __init__(
        self,
        miot_proxy: MiotProxy,
        external_streams: Sequence[ExternalCameraStreamSettings],
    ) -> None:
        self._miot = MiotCameraVideoStreamSource(miot_proxy)
        self._external: dict[tuple[str, int], RtspCameraVideoStreamSource] = {}
        for item in external_streams:
            key = (item.physical_did, item.channel)
            if key in self._external:
                raise ValueError("Duplicate external camera stream identity")
            self._external[key] = RtspCameraVideoStreamSource(
                item.url.get_secret_value()
            )
        self._active: dict[tuple[str, int, int], CameraVideoStreamSource] = {}
        self._lock = asyncio.Lock()

    def uses_external_stream(self, camera_id: str, channel: int) -> bool:
        return (camera_id, channel) in self._external

    async def start(
        self,
        camera_id: str,
        channel: int,
        callback: DecodedVideoCallback,
    ) -> int:
        async with self._lock:
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
    external_streams = get_settings().camera.external_streams
    if not external_streams:
        return MiotCameraVideoStreamSource(miot_proxy)
    return ConfiguredCameraVideoStreamSource(miot_proxy, external_streams)
