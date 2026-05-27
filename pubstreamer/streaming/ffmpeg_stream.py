"""
FFmpeg-backed Icecast stream output.

Pipes float32 PCM from the mixer into ffmpeg, which encodes and pushes to
the Icecast mountpoint. Auto-reconnects on disconnect.
"""

import subprocess
import threading
import time
import numpy as np
from typing import Callable


class IcecastStream:
    """
    Manages an ffmpeg subprocess that receives raw PCM via stdin and pushes
    encoded audio to an Icecast server.

    *mixer_fn* is a callable that returns one chunk as float32 numpy
    (channels, frames). Called on every iteration of the writer thread.
    """

    def __init__(self, mixer_fn: Callable[[], np.ndarray],
                 sample_rate: int = 48000, channels: int = 2,
                 chunk_frames: int = 1024):
        self._mixer_fn = mixer_fn
        self._sample_rate = sample_rate
        self._channels = channels
        self._chunk_frames = chunk_frames

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

        # Connection settings (set via start())
        self._host = ""
        self._port = 8000
        self._username = "source"
        self._password = ""
        self._mountpoint = ""
        self._bitrate = 96
        self._format = "mp3"

        self.on_state_change: Callable[[str], None] | None = None  # "connecting"|"streaming"|"stopped"|"error"

    # ── public API ──────────────────────────────────────────────────────────

    def start(self, host: str, port: int, password: str, mountpoint: str,
              bitrate: int = 96, fmt: str = "mp3", username: str = "source"):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._mountpoint = mountpoint.lstrip("/")
        self._bitrate = bitrate
        self._format = fmt
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ffmpeg-stream")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._kill_proc()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._notify("stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── internals ───────────────────────────────────────────────────────────

    def _build_cmd(self) -> list[str]:
        icecast_url = (
            f"icecast://{self._username}:{self._password}@"
            f"{self._host}:{self._port}/{self._mountpoint}"
        )
        if self._format == "aac":
            codec, fmt_flag, content_type = "aac", "adts", "audio/aac"
        else:
            codec, fmt_flag, content_type = "libmp3lame", "mp3", "audio/mpeg"

        return [
            "ffmpeg", "-loglevel", "warning",
            "-f", "f32le",
            "-ar", str(self._sample_rate),
            "-ac", str(self._channels),
            "-i", "pipe:0",
            "-c:a", codec,
            "-b:a", f"{self._bitrate}k",
            "-f", fmt_flag,
            "-content_type", content_type,
            icecast_url,
        ]

    def _kill_proc(self):
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                pass
            self._proc = None

    def _notify(self, state: str):
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception:
                pass

    def _run(self):
        retry_delay = 2
        while not self._stop_event.is_set():
            self._notify("connecting")
            cmd = self._build_cmd()
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                self._notify("streaming")
                retry_delay = 2
                self._write_loop()
            except FileNotFoundError:
                self._notify("error")
                print("[IcecastStream] ffmpeg not found - ensure it is in PATH")
                break
            except Exception as e:
                print(f"[IcecastStream] error: {e}")
            finally:
                self._kill_proc()

            if not self._stop_event.is_set():
                self._notify("connecting")
                self._stop_event.wait(timeout=retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    def _write_loop(self):
        bytes_per_sample = 4  # float32
        chunk_bytes = self._chunk_frames * self._channels * bytes_per_sample
        while not self._stop_event.is_set() and self._proc and self._proc.poll() is None:
            frame = self._mixer_fn()
            # ffmpeg expects interleaved: (frames, channels) → flatten
            data = frame.T.astype(np.float32).tobytes()
            try:
                self._proc.stdin.write(data)
            except BrokenPipeError:
                break
            except Exception:
                break
