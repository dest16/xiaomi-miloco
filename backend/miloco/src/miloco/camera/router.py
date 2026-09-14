"""Manage standalone RTSP cameras independently from the MIoT catalog."""

from fastapi import APIRouter, Depends

from miloco.config import get_settings
from miloco.config.settings import RtspCameraSettings
from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.middleware.exceptions import (
    ResourceNotFoundException,
    ValidationException,
)
from miloco.perception.collect.camera_stream_selector import (
    create_camera_encoded_video_stream_source,
    create_camera_video_stream_source,
)
from miloco.perception.collect.rtsp_camera_stream import redact_rtsp_url
from miloco.schema.common_schema import NormalResponse
from miloco.utils.agent_config import update_shared_config

router = APIRouter(prefix="/cameras", tags=["Cameras"])
manager = get_manager()


def _payload(camera: RtspCameraSettings) -> dict:
    return {
        "id": camera.id,
        "name": camera.name,
        "room_name": camera.room_name,
        "url": redact_rtsp_url(camera.url.get_secret_value()),
        "enabled": camera.enabled,
        "source_type": "rtsp",
    }


def _persist(cameras: list[RtspCameraSettings]) -> None:
    update_shared_config(
        camera={
            "rtsp_cameras": [
                {
                    "id": camera.id,
                    "name": camera.name,
                    "room_name": camera.room_name,
                    "url": camera.url.get_secret_value(),
                    "enabled": camera.enabled,
                }
                for camera in cameras
            ]
        }
    )


async def _reload(camera_id: str | None = None) -> None:
    # URL changes need a fresh decoder. Other source changes are picked up by
    # the next sync, but using one path keeps CRUD behavior immediate.
    adapter = manager.miot_service._camera_adapter()
    if adapter is not None and camera_id is not None:
        await adapter.disconnect_device(camera_id)
    # Rebuild both consumers independently so adding the first RTSP camera and
    # changing a URL take effect immediately without restarting the backend.
    manager.miot_service._video_stream_source = create_camera_video_stream_source(
        manager.miot_proxy
    )
    manager.miot_service._encoded_video_stream_source = (
        create_camera_encoded_video_stream_source()
    )
    if adapter is not None:
        adapter._video_stream_source = create_camera_video_stream_source(
            manager.miot_proxy
        )
    await manager.miot_service._sync_camera_adapter()


@router.get("", response_model=NormalResponse)
async def list_rtsp_cameras(current_user: str = Depends(verify_token)):
    cameras = get_settings().camera.rtsp_cameras
    return NormalResponse(code=0, message="ok", data=[_payload(c) for c in cameras])


@router.post("", response_model=NormalResponse)
async def create_rtsp_camera(
    request: RtspCameraSettings,
    current_user: str = Depends(verify_token),
):
    cameras = list(get_settings().camera.rtsp_cameras)
    if any(camera.id == request.id for camera in cameras):
        raise ValidationException(f"Camera id already exists: {request.id}")
    cameras.append(request)
    _persist(cameras)
    await _reload()
    return NormalResponse(code=0, message="RTSP camera created", data=_payload(request))


@router.put("/{camera_id}", response_model=NormalResponse)
async def update_rtsp_camera(
    camera_id: str,
    request: RtspCameraSettings,
    current_user: str = Depends(verify_token),
):
    if request.id != camera_id:
        raise ValidationException("Camera id in path and body must match")
    cameras = list(get_settings().camera.rtsp_cameras)
    index = next((i for i, camera in enumerate(cameras) if camera.id == camera_id), None)
    if index is None:
        raise ResourceNotFoundException(f"RTSP camera not found: {camera_id}")
    cameras[index] = request
    _persist(cameras)
    await _reload(camera_id)
    return NormalResponse(code=0, message="RTSP camera updated", data=_payload(request))


@router.delete("/{camera_id}", response_model=NormalResponse)
async def delete_rtsp_camera(
    camera_id: str,
    current_user: str = Depends(verify_token),
):
    cameras = list(get_settings().camera.rtsp_cameras)
    remaining = [camera for camera in cameras if camera.id != camera_id]
    if len(remaining) == len(cameras):
        raise ResourceNotFoundException(f"RTSP camera not found: {camera_id}")
    _persist(remaining)
    await _reload(camera_id)
    return NormalResponse(code=0, message="RTSP camera deleted", data=None)
