"""Camera video stream lifecycle abstractions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from miloco.miot.client import MiotProxy


DecodedVideoCallback = Callable[
    [str, "NDArray[np.uint8]", int, int, int, int], Awaitable[None]
]
EncodedVideoCallback = Callable[
    [str, bytes, int, int, int, str, bool], Awaitable[None]
]


class CameraVideoStreamSource(Protocol):
    """Own the lifecycle of decoded video subscriptions for a camera."""

    async def start(
        self,
        camera_id: str,
        channel: int,
        callback: DecodedVideoCallback,
    ) -> int:
        """Start a stream and return its registration id."""
        ...

    async def stop(
        self,
        camera_id: str,
        channel: int,
        registration_id: int,
    ) -> None:
        """Stop a previously registered stream."""
        ...


class CameraEncodedVideoStreamSource(Protocol):
    """Own an encoded Annex-B packet subscription for browser preview."""

    async def start(
        self,
        camera_id: str,
        channel: int,
        callback: EncodedVideoCallback,
    ) -> int:
        ...

    async def stop(
        self,
        camera_id: str,
        channel: int,
        registration_id: int,
    ) -> None:
        ...


class MiotCameraVideoStreamSource:
    """Provide decoded video frames through the existing MIoT proxy."""

    def __init__(self, miot_proxy: MiotProxy) -> None:
        self._miot_proxy = miot_proxy

    async def start(
        self,
        camera_id: str,
        channel: int,
        callback: DecodedVideoCallback,
    ) -> int:
        # MiotProxy still declares the legacy four-argument PyAV callback,
        # while the manager actually dispatches a six-argument BGR ndarray.
        miot_callback = cast(Any, callback)
        return await self._miot_proxy.start_camera_decode_video_stream(
            camera_id,
            channel,
            miot_callback,
        )

    async def stop(
        self,
        camera_id: str,
        channel: int,
        registration_id: int,
    ) -> None:
        await self._miot_proxy.stop_camera_decode_video_stream(
            camera_id,
            channel,
            registration_id,
        )


def uses_external_video_stream(
    source: CameraVideoStreamSource,
    camera_id: str,
    channel: int,
) -> bool:
    """Return whether silence/reconnect is owned by an external source."""
    predicate = getattr(source, "uses_external_stream", None)
    return bool(predicate and predicate(camera_id, channel))
