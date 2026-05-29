"""
UrlStreamCapture - streams remote audio URLs into the mixer via ffmpeg.

Uses ffmpeg as a decoder so Icecast/HTTP radio streams and other URLs ffmpeg
understands can be treated like regular mixer sources. Audio is converted to
float32 PCM at the mixer's sample rate/channel count and queued in chunk-sized
buffers for the mixer thread.
"""

import queue
import subprocess
import threading
import time
import numpy as np


class UrlStreamCapture:
    """Audio source that decodes a remote URL continuously via ffmpeg."""

    def __init__(self, url: str, sample_rate: int = 48000,
                 channels: int = 2, chunk_frames: int = 1024):
        self.url = url
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_frames = chunk_frames
        self.error: str | None = None

        # Treat remote streams more like internet radio than local capture:
        # build a few seconds of PCM so bitrate changes and network bursts do
        # not turn into audible mute/unmute behavior.
        chunk_seconds = chunk_frames / float(sample_rate) if sample_rate > 0 else 0.02
        self._queue_capacity_chunks = max(128, int(round(6.0 / chunk_seconds)))
        self._prebuffer_chunks = max(32, int(round(3.0 / chunk_seconds)))
        self._queue: queue.Queue = queue.Queue(maxsize=self._queue_capacity_chunks)
        self._primed = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

    def start(self):
        self.error = None
        self._primed = False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="url-stream")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._kill_proc()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self._drain_queue()

    def read(self) -> np.ndarray:
        if not self._primed:
            if self._queue.qsize() < self._prebuffer_chunks:
                return np.zeros((self.channels, self.chunk_frames), dtype=np.float32)
            self._primed = True
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            # Force the stream to rebuild a small buffer before resuming.
            self._primed = False
            return np.zeros((self.channels, self.chunk_frames), dtype=np.float32)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._proc = subprocess.Popen(
                    self._build_cmd(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    bufsize=0,
                )
            except FileNotFoundError:
                self.error = "ffmpeg not found in PATH."
                return
            except Exception as e:
                self.error = str(e)
                return

            try:
                self._pump_stdout()
            finally:
                self._kill_proc()

            if not self._stop_event.is_set():
                if self.error is None:
                    self.error = "Stream disconnected. Reconnecting..."
                time.sleep(2.0)

    def _build_cmd(self) -> list[str]:
        return [
            "ffmpeg",
            "-loglevel", "warning",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", self.url,
            "-vn",
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "pipe:1",
        ]

    def _pump_stdout(self):
        if not self._proc or self._proc.stdout is None:
            return
        self.error = None
        self._primed = False
        bytes_per_chunk = self.chunk_frames * self.channels * 4
        buf = bytearray()

        while not self._stop_event.is_set() and self._proc.poll() is None:
            need = bytes_per_chunk - len(buf)
            data = self._proc.stdout.read(need)
            if not data:
                break
            buf.extend(data)
            if len(buf) < bytes_per_chunk:
                continue

            chunk = np.frombuffer(buf[:bytes_per_chunk], dtype=np.float32).copy()
            del buf[:bytes_per_chunk]
            chunk = chunk.reshape(self.chunk_frames, self.channels).T
            self._queue_chunk(chunk)

    def _queue_chunk(self, chunk: np.ndarray):
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                pass

    def _drain_queue(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _kill_proc(self):
        if self._proc is None:
            return
        try:
            if self._proc.stdout:
                self._proc.stdout.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            pass
        self._proc = None
