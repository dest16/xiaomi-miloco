"""RTSP-backed camera video stream source."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, Future, TimeoutError
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import av

from miloco.perception.collect.camera_stream import DecodedVideoCallback

logger = logging.getLogger(__name__)

_DEFAULT_RTSP_OPTIONS = {"rtsp_transport": "tcp"}
_DEFAULT_RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)
_DEFAULT_OPEN_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 5.0


def redact_rtsp_url(url: str) -> str:
    """Return an RTSP endpoint safe for logs.

    User information is replaced and the query/fragment are omitted because
    they commonly carry access tokens.
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "<unknown-host>"
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        userinfo = ""
        if parsed.username is not None or parsed.password is not None:
            userinfo = "***:***@"
        return urlunsplit(
            (parsed.scheme or "rtsp", f"{userinfo}{host}", parsed.path, "", "")
        )
    except ValueError:
        return "rtsp://<redacted>"


class RtspCameraVideoStreamSource:
    """Decode one RTSP URL and emit frames through the common camera callback.

    A source instance owns at most one reader thread and one RTSP connection.
    Blocking PyAV operations never run on the asyncio event loop.
    """

    def __init__(
        self,
        url: str,
        *,
        reconnect_backoff_seconds: Sequence[float] = (
            _DEFAULT_RECONNECT_BACKOFF_SECONDS
        ),
        open_timeout_seconds: float = _DEFAULT_OPEN_TIMEOUT_SECONDS,
        read_timeout_seconds: float = _DEFAULT_READ_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
        decoder_backend: str = "pyav",
        ffmpeg_path: str = "ffmpeg",
        vaapi_device: str = "/dev/dri/renderD128",
        output_fps: int = 3,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
            raise ValueError("A valid RTSP URL with a host is required")
        if not reconnect_backoff_seconds or any(
            delay < 0 for delay in reconnect_backoff_seconds
        ):
            raise ValueError("Reconnect backoff must contain non-negative delays")

        self._url = url
        self._safe_url = redact_rtsp_url(url)
        self._reconnect_backoff_seconds = tuple(reconnect_backoff_seconds)
        self._open_timeout = open_timeout_seconds
        self._read_timeout = read_timeout_seconds
        if decoder_backend not in {"auto", "pyav", "ffmpeg-vaapi"}:
            raise ValueError("Unsupported RTSP decoder backend")
        if output_fps <= 0:
            raise ValueError("output_fps must be positive")
        self._opener = opener or av.open
        # An injected opener is a PyAV test seam and must never spawn FFmpeg.
        self._decoder_backend = "pyav" if opener is not None else decoder_backend
        self._ffmpeg_path = ffmpeg_path
        self._vaapi_device = vaapi_device
        self._output_fps = output_fps
        self._vaapi_disabled = False
        self._vaapi_ever_emitted = False

        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._container: Any | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callback: DecodedVideoCallback | None = None
        self._camera_id: str | None = None
        self._channel = 0
        self._registration_id = -1
        self._next_registration_id = 0
        self._last_timestamp_ms = -1
        self._pending_callbacks: set[Future[None]] = set()

    @property
    def running(self) -> bool:
        """Whether the reader thread is alive."""
        with self._state_lock:
            return self._thread is not None and self._thread.is_alive()

    def uses_external_stream(self, camera_id: str, channel: int) -> bool:
        """RTSP owns reconnect; MIoT's silence watchdog must ignore it."""
        return True

    async def start(
        self,
        camera_id: str,
        channel: int,
        callback: DecodedVideoCallback,
    ) -> int:
        """Start the sole reader, returning the existing id on duplicate start."""
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                if camera_id != self._camera_id or channel != self._channel:
                    raise RuntimeError(
                        "RTSP source is already assigned to another camera"
                    )
                return self._registration_id

            self._stop_event.clear()
            self._loop = loop
            self._callback = callback
            self._camera_id = camera_id
            self._channel = channel
            self._registration_id = self._next_registration_id
            self._next_registration_id += 1
            self._last_timestamp_ms = -1
            registration_id = self._registration_id
            thread = threading.Thread(
                target=self._reader_main,
                name=f"rtsp-camera-{camera_id}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return registration_id

    async def stop(
        self,
        camera_id: str,
        channel: int,
        registration_id: int,
    ) -> None:
        """Stop the matching reader and wait until all owned work is released."""
        with self._state_lock:
            if (
                self._thread is None
                or camera_id != self._camera_id
                or channel != self._channel
                or registration_id != self._registration_id
            ):
                return
            self._stop_event.set()
            thread = self._thread
            process = self._process

        if process is not None and process.poll() is None:
            process.terminate()

        # PyAV/FFmpeg containers are not safe to close from a thread other than
        # the one currently executing decode(). Doing so can race native
        # demux/decoder state and terminate the whole process. The read timeout
        # bounds this wait; _reader_main closes the container in its own thread.
        if thread is not threading.current_thread():
            await asyncio.to_thread(thread.join)

        with self._state_lock:
            pending = tuple(self._pending_callbacks)
            self._thread = None
            self._container = None
            self._process = None
            self._loop = None
            self._callback = None
            self._camera_id = None
            self._registration_id = -1

        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in pending),
                return_exceptions=True,
            )
        logger.debug("RTSP reader stopped for %s", self._safe_url)

    def _reader_main(self) -> None:
        backoff_index = 0
        connected_once = False
        while not self._stop_event.is_set():
            container: Any | None = None
            try:
                if self._decoder_backend != "pyav" and not self._vaapi_disabled:
                    self._read_ffmpeg_vaapi_once()
                    connected_once = connected_once or self._vaapi_ever_emitted
                    if not self._stop_event.is_set():
                        logger.warning("RTSP stream ended for %s", self._safe_url)
                    continue
                container = self._opener(
                    self._url,
                    mode="r",
                    options=dict(_DEFAULT_RTSP_OPTIONS),
                    timeout=(self._open_timeout, self._read_timeout),
                )
                with self._state_lock:
                    self._container = container
                if self._stop_event.is_set():
                    break

                video_stream = container.streams.video[0]
                connected_once = True
                backoff_index = 0
                logger.info("RTSP reader connected to %s", self._safe_url)
                for frame in container.decode(video_stream):
                    if self._stop_event.is_set():
                        break
                    self._deliver_frame(frame)
                if not self._stop_event.is_set():
                    logger.warning("RTSP stream ended for %s", self._safe_url)
            except Exception as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                if (
                    self._decoder_backend == "auto"
                    and not self._vaapi_ever_emitted
                    and not self._vaapi_disabled
                ):
                    self._vaapi_disabled = True
                    logger.warning(
                        "FFmpeg VAAPI unavailable for %s (%s); falling back to PyAV",
                        self._safe_url,
                        type(exc).__name__,
                    )
                    backoff_index = 0
                    continue
                if connected_once:
                    logger.warning(
                        "RTSP stream disconnected for %s (%s)",
                        self._safe_url,
                        type(exc).__name__,
                    )
                elif backoff_index == 0:
                    logger.warning(
                        "Initial RTSP connection failed for %s (%s)",
                        self._safe_url,
                        type(exc).__name__,
                    )
                else:
                    logger.info(
                        "RTSP connection still unavailable for %s (%s)",
                        self._safe_url,
                        type(exc).__name__,
                    )
            finally:
                if container is not None:
                    with self._state_lock:
                        if self._container is container:
                            self._container = None
                    self._close_container(container)

            if self._stop_event.is_set():
                break
            delay = self._reconnect_backoff_seconds[
                min(backoff_index, len(self._reconnect_backoff_seconds) - 1)
            ]
            logger.info(
                "Reconnecting RTSP reader for %s in %.1fs", self._safe_url, delay
            )
            backoff_index += 1
            if self._stop_event.wait(delay):
                break

    def _read_ffmpeg_vaapi_once(self) -> None:
        """Decode in FFmpeg/VAAPI, sample on-GPU, and pipe BGR rawvideo in NUT."""
        command = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-hwaccel",
            "vaapi",
            "-hwaccel_device",
            self._vaapi_device,
            "-hwaccel_output_format",
            "vaapi",
            "-i",
            self._url,
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"fps={self._output_fps},hwdownload,format=nv12,format=bgr24",
            "-c:v",
            "rawvideo",
            "-threads:v",
            "1",
            "-pix_fmt",
            "bgr24",
            "-f",
            "nut",
            "-flush_packets",
            "1",
            "pipe:1",
        ]
        process = subprocess.Popen(  # noqa: S603 - argv is never passed through a shell
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        with self._state_lock:
            self._process = process
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_tail: deque[str] = deque(maxlen=8)

        def drain_stderr() -> None:
            for raw_line in iter(process.stderr.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    stderr_tail.append(line.replace(self._url, self._safe_url))

        stderr_thread = threading.Thread(
            target=drain_stderr,
            name=f"rtsp-vaapi-stderr-{self._camera_id}",
            daemon=True,
        )
        stderr_thread.start()
        container: Any | None = None
        emitted = False
        try:
            container = av.open(process.stdout, mode="r", format="nut")
            with self._state_lock:
                self._container = container
            video_stream = container.streams.video[0]
            logger.info(
                "RTSP perception decoder=ffmpeg-vaapi device=%s fps=%d source=%s",
                self._vaapi_device,
                self._output_fps,
                self._safe_url,
            )
            for frame in container.decode(video_stream):
                if self._stop_event.is_set():
                    break
                emitted = True
                self._vaapi_ever_emitted = True
                self._deliver_frame(frame)
            if not self._stop_event.is_set() and process.wait(timeout=1) != 0:
                detail = "; ".join(stderr_tail) or "ffmpeg exited without diagnostics"
                raise RuntimeError(detail)
        finally:
            if container is not None:
                with self._state_lock:
                    if self._container is container:
                        self._container = None
                self._close_container(container)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            with self._state_lock:
                if self._process is process:
                    self._process = None
            if not emitted and process.returncode not in {None, 0}:
                detail = "; ".join(stderr_tail) or "ffmpeg exited before first frame"
                raise RuntimeError(detail)

    def _deliver_frame(self, frame: Any) -> None:
        recv_unix_ms = time.time_ns() // 1_000_000
        bgr = frame.to_ndarray(format="bgr24")
        decoded_unix_ms = time.time_ns() // 1_000_000
        timestamp_ms = self._next_timestamp(frame)

        with self._state_lock:
            loop = self._loop
            callback = self._callback
            camera_id = self._camera_id
            channel = self._channel
        if (
            self._stop_event.is_set()
            or loop is None
            or loop.is_closed()
            or callback is None
            or camera_id is None
        ):
            return

        async def invoke_callback() -> None:
            await callback(
                camera_id,
                bgr,
                timestamp_ms,
                channel,
                recv_unix_ms,
                decoded_unix_ms,
            )

        callback_coro = invoke_callback()
        try:
            future = asyncio.run_coroutine_threadsafe(callback_coro, loop)
        except RuntimeError:
            callback_coro.close()
            return
        with self._state_lock:
            self._pending_callbacks.add(future)
        future.add_done_callback(self._callback_done)

        # Keep callback work bounded to one frame. The short timeout lets stop()
        # interrupt a slow consumer without waiting for it to finish.
        while not self._stop_event.is_set():
            try:
                future.result(timeout=0.1)
                return
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001
                return
        future.cancel()

    def _next_timestamp(self, frame: Any) -> int:
        pts = getattr(frame, "pts", None)
        time_base = getattr(frame, "time_base", None)
        if pts is not None and time_base is not None:
            try:
                candidate = int(pts * time_base * 1000)
            except (TypeError, ValueError, OverflowError):
                candidate = time.monotonic_ns() // 1_000_000
        else:
            candidate = time.monotonic_ns() // 1_000_000

        with self._state_lock:
            if candidate <= self._last_timestamp_ms:
                candidate = self._last_timestamp_ms + 1
            self._last_timestamp_ms = candidate
        return candidate

    def _callback_done(self, future: Future[None]) -> None:
        with self._state_lock:
            self._pending_callbacks.discard(future)
        try:
            error = future.exception()
        except CancelledError:
            return
        if error is not None:
            logger.warning(
                "RTSP frame callback failed for %s (%s)",
                self._safe_url,
                type(error).__name__,
            )

    @staticmethod
    def _close_container(container: Any) -> None:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring an error while closing an RTSP container")
