from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


INSTALL_PY = Path(__file__).parents[1] / "install.py"
sys.modules.setdefault("questionary", SimpleNamespace())
if "rich" not in sys.modules:
    rich = ModuleType("rich")
    rich_console = ModuleType("rich.console")
    rich_progress = ModuleType("rich.progress")

    class RichStub:
        pass

    rich_console.Console = RichStub
    for name in (
        "BarColumn",
        "DownloadColumn",
        "Progress",
        "SpinnerColumn",
        "TextColumn",
        "TimeRemainingColumn",
        "TransferSpeedColumn",
    ):
        setattr(rich_progress, name, RichStub)
    sys.modules["rich"] = rich
    sys.modules["rich.console"] = rich_console
    sys.modules["rich.progress"] = rich_progress
SPEC = importlib.util.spec_from_file_location("miloco_install", INSTALL_PY)
assert SPEC and SPEC.loader
install = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = install
SPEC.loader.exec_module(install)


class StubI18n:
    def t(self, key: str, *args: str) -> str:
        return f"{key}: {'; '.join(args)}"


class StubUI:
    def __init__(self) -> None:
        self.i18n = StubI18n()

    def fail(self, message: str):
        raise RuntimeError(message)


def make_installer(tmp_path: Path, *, local_dist: bool = True):
    instance = install.Installer(
        plat=install.Platform(
            os="linux",
            arch="x86_64",
            is_interactive=False,
            lang="en",
        ),
        ui=StubUI(),
        downloader=object(),
        local_dist=local_dist,
        miloco_home=tmp_path / "home",
        agent_platform="openclaw",
    )
    instance.dist_dir = tmp_path / "dist"
    return instance


def populate_complete_dist(dist: Path) -> None:
    dist.mkdir()
    for name in (
        "miloco_miot-0.1-manylinux_2_28_x86_64.whl",
        "miloco-0.1-py3-none-any.whl",
        "miloco_cli-0.1-py3-none-any.whl",
        "miloco-models-0.1.tar.gz",
        "miloco-openclaw-plugin-0.1.tgz",
    ):
        (dist / name).touch()


def test_local_dist_is_used_without_release_fallback(tmp_path: Path) -> None:
    instance = make_installer(tmp_path)
    populate_complete_dist(instance.dist_dir)
    instance._fetch_release_bundle = lambda: pytest.fail(
        "local dist must not fetch a release"
    )

    instance._validate_local_dist()

    assert instance._get_src_dir() == instance.dist_dir


def test_local_dist_fails_fast_when_an_artifact_is_missing(tmp_path: Path) -> None:
    instance = make_installer(tmp_path)
    populate_complete_dist(instance.dist_dir)
    (instance.dist_dir / "miloco-models-0.1.tar.gz").unlink()
    instance._fetch_release_bundle = lambda: pytest.fail(
        "incomplete local dist must not fetch a release"
    )

    with pytest.raises(RuntimeError, match="local_dist_incomplete.*miloco-models"):
        instance._validate_local_dist()


def test_local_dist_reuses_complete_initialization_flow(tmp_path: Path) -> None:
    instance = make_installer(tmp_path)
    populate_complete_dist(instance.dist_dir)
    calls: list[str] = []
    instance._print_welcome = lambda: calls.append("welcome")
    instance._run_dev_build = lambda: pytest.fail("local dist must not build")
    instance._print_summary = lambda: calls.append("summary")
    for step in (
        "choose_platform",
        "check_deps",
        "install",
        "download",
        "init_service",
        "account",
        "configure",
        "plugin",
    ):
        setattr(instance, f"_step_{step}", lambda step=step: calls.append(step))

    instance.run()

    assert calls == [
        "welcome",
        "choose_platform",
        "check_deps",
        "install",
        "download",
        "init_service",
        "account",
        "configure",
        "plugin",
        "summary",
    ]


def test_release_source_path_is_unchanged(tmp_path: Path) -> None:
    instance = make_installer(tmp_path, local_dist=False)
    release_dir = tmp_path / "release"
    instance._fetch_release_bundle = lambda: release_dir

    assert instance._get_src_dir() == release_dir
