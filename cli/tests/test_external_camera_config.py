import json
import os
import sys
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

if os.name == "nt":
    sys.modules.setdefault("fcntl", MagicMock())

import miloco_cli.config as config_module
from miloco_cli.config import load_config, set_value
from miloco_cli.main import cli

URL = "rtsp://user:password@camera.local:8554/live?token=top-secret"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "miloco" / "config.json"
    monkeypatch.setenv("MILOCO_HOME", str(path.parent))
    if os.name == "nt":

        def windows_atomic_write(target, data):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        monkeypatch.setattr(config_module, "atomic_write", windows_atomic_write)
    return path


def _raw_config() -> str:
    return json.dumps(
        [{"physical_did": "dual", "channel": 0, "url": URL}],
        separators=(",", ":"),
    )


def test_cli_config_persists_external_stream_array(isolated_config):
    value = set_value("camera.external_streams", _raw_config())
    assert value[0]["physical_did"] == "dual"
    assert load_config()["camera"]["external_streams"][0]["url"] == URL


def test_cli_config_persists_rtsp_decode_settings(isolated_config):
    assert set_value("camera.rtsp_decode.backend", "FFMPEG-VAAPI") == "ffmpeg-vaapi"
    assert set_value("camera.rtsp_decode.ffmpeg_path", "/usr/bin/ffmpeg") == "/usr/bin/ffmpeg"
    assert set_value("camera.rtsp_decode.vaapi_device", "/dev/dri/renderD129") == "/dev/dri/renderD129"

    decode = load_config()["camera"]["rtsp_decode"]
    assert decode == {
        "backend": "ffmpeg-vaapi",
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "vaapi_device": "/dev/dri/renderD129",
    }


@pytest.mark.parametrize("value", ["", "vaapi", "ffmpeg", "gpu"])
def test_cli_config_rejects_invalid_rtsp_decode_backend(value):
    with pytest.raises(ValueError, match="auto / pyav / ffmpeg-vaapi"):
        set_value("camera.rtsp_decode.backend", value)


@pytest.mark.parametrize(
    "value",
    [
        "{}",
        '[{"physical_did":"","channel":0,"url":"rtsp://camera/live"}]',
        '[{"physical_did":"cam","channel":-1,"url":"rtsp://camera/live"}]',
        '[{"physical_did":"cam","channel":0,"url":"http://camera/live"}]',
    ],
)
def test_cli_config_rejects_invalid_external_streams(value):
    with pytest.raises(ValueError):
        set_value("camera.external_streams", value)


def test_config_show_get_and_set_redact_rtsp_secrets(runner, isolated_config):
    result = runner.invoke(
        cli,
        ["config", "set", "camera.external_streams", _raw_config(), "--no-restart"],
    )
    assert result.exit_code == 0
    assert "password" not in result.output
    assert "top-secret" not in result.output

    for args in (
        ["config", "show"],
        ["config", "get", "camera.external_streams"],
        ["config", "get", "camera.external_streams", "--value-only"],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0
        assert "password" not in result.output
        assert "top-secret" not in result.output
        assert "rtsp://***:***@camera.local:8554/live" in result.output


def test_config_show_unmasked_is_explicit_escape_hatch(runner, isolated_config):
    set_value("camera.external_streams", _raw_config())
    result = runner.invoke(cli, ["config", "show", "--unmasked"])
    assert result.exit_code == 0
    assert URL in result.output
