import pytest
from miloco.config.settings import CameraSettings, MilocoSettings
from pydantic import ValidationError


def test_external_camera_stream_is_keyed_by_physical_did_and_channel():
    settings = MilocoSettings(
        camera={
            "external_streams": [
                {
                    "physical_did": " dual ",
                    "channel": 1,
                    "url": "rtsp://user:password@camera.local/live?token=secret",
                }
            ]
        }
    )
    item = settings.camera.external_streams[0]
    assert item.physical_did == "dual"
    assert item.channel == 1
    assert item.url.get_secret_value().startswith("rtsp://user:password@")
    assert "password" not in repr(item)


@pytest.mark.parametrize(
    "item",
    [
        {"physical_did": "", "channel": 0, "url": "rtsp://camera/live"},
        {"physical_did": "cam", "channel": -1, "url": "rtsp://camera/live"},
        {"physical_did": "cam", "channel": 0, "url": "http://camera/live"},
        {"physical_did": "cam", "channel": 0, "url": "rtsp:///live"},
    ],
)
def test_invalid_external_camera_stream_is_rejected_without_secret(item):
    with pytest.raises(ValidationError) as error:
        CameraSettings(external_streams=[item])
    assert "password" not in str(error.value)


def test_duplicate_external_camera_identity_is_rejected():
    item = {"physical_did": "cam", "channel": 0, "url": "rtsp://camera/live"}
    with pytest.raises(ValidationError, match="duplicate"):
        CameraSettings(external_streams=[item, item])


def test_standalone_rtsp_camera_has_own_identity_and_metadata():
    settings = CameraSettings(
        rtsp_cameras=[
            {
                "id": " living-room ",
                "name": " 客厅摄像头 ",
                "room_name": " 客厅 ",
                "url": "rtsp://go2rtc.local:8554/living-room",
            }
        ]
    )
    camera = settings.rtsp_cameras[0]
    assert camera.id == "living-room"
    assert camera.name == "客厅摄像头"
    assert camera.room_name == "客厅"
    assert camera.enabled is True


def test_standalone_rtsp_camera_rejects_reserved_or_duplicate_ids():
    item = {
        "id": "camera:ch1",
        "name": "camera",
        "url": "rtsp://go2rtc.local/camera",
    }
    with pytest.raises(ValidationError, match="reserved"):
        CameraSettings(rtsp_cameras=[item])

    item["id"] = "camera"
    with pytest.raises(ValidationError, match="duplicate"):
        CameraSettings(rtsp_cameras=[item, item])
