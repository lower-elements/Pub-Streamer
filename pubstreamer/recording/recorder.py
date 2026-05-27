"""
Recorder: taps the Mixer's output and writes it to disk via ffmpeg.

Mixed mode  — one file, identical to what ffmpeg sends to Icecast.
Stems mode  — one file per source (post-gain, post-VST, post-fade),
              inside a timestamped subdirectory.

All I/O runs on daemon writer threads; start() and stop() are blocking
but intended to be called from background threads only.
"""

import os
import queue
import re
import subprocess
import threading
from datetime import datetime
from typing import Callable

import numpy as np


_CODEC_MAP: dict[str, tuple[str, bool]] = {
    # format: (ffmpeg_codec, needs_bitrate)
    "mp3":  ("libmp3lame", True),
    "wav":  ("pcm_s16le",  False),
    "ogg":  ("libvorbis",  True),
    "flac": ("flac",       False),
    "aac":  ("aac",        True),
    "opus": ("libopus",    True),
}

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip("_ ")
    return cleaned or "source"


class Recorder:
    """
    Attach to a Mixer to record its output.

    Config attributes (set by RecordingPanel before calling start):
        output_dir, stems, record_with_stream, fmt, sample_rate, bitrate
    """

    def __init__(self, mixer):
        self._mixer = mixer

        # Config — set by RecordingPanel
        self.output_dir:         str  = ""
        self.stems:              bool = False
        self.record_with_stream: bool = False
        self.fmt:                str  = "wav"
        self.sample_rate:        int  = 48000
        self.bitrate:            int  = 128

        self._running = False
        self._procs:         list[subprocess.Popen] = []
        self._threads:       list[threading.Thread] = []
        self._rec_queue:     "queue.Queue | None"   = None
        self._stems_queues:  "dict | None"          = None

        self.on_state_change: "Callable[[str], None] | None" = None

    @property
    def is_running(self) -> bool:
        return self._running

    def remaining_seconds(self) -> float:
        """Rough estimate of buffered audio not yet written to disk, in seconds."""
        chunks = 0
        if self._rec_queue is not None:
            chunks = self._rec_queue.qsize()
        elif self._stems_queues is not None:
            chunks = max((q.qsize() for q in self._stems_queues.values()), default=0)
        return chunks * self._mixer.chunk_frames / self._mixer.sample_rate

    # ── public API (call from background threads) ────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        if not self.output_dir:
            raise RuntimeError("Output directory is not set")

        codec, needs_bitrate = _CODEC_MAP.get(self.fmt, ("pcm_s16le", False))
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        if self.stems:
            self._start_stems(ts, codec, needs_bitrate)
        else:
            self._start_mixed(ts, codec, needs_bitrate)

        self._running = True
        if self.on_state_change:
            self.on_state_change("recording")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._mixer.detach_recorder()

        # Writer threads notice _running is False after their next get() timeout
        # and exit cleanly, closing stdin so ffmpeg flushes and finishes.
        for t in self._threads:
            t.join(timeout=15)

        self._threads.clear()
        self._procs.clear()
        self._rec_queue    = None
        self._stems_queues = None

        if self.on_state_change:
            self.on_state_change("stopped")

    # ── internal ─────────────────────────────────────────────────────────────

    def _start_mixed(self, ts: str, codec: str, needs_bitrate: bool) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        ext   = self.fmt if self.fmt != "opus" else "opus"
        path  = os.path.join(self.output_dir, f"{ts}.{ext}")
        q     = queue.Queue(maxsize=64)
        proc  = self._launch_ffmpeg(path, codec, needs_bitrate)
        self._rec_queue = q
        self._procs.append(proc)
        t = threading.Thread(target=self._writer, args=(q, proc),
                             daemon=True, name="rec-mixed")
        self._threads.append(t)
        t.start()
        self._mixer.attach_recorder(q, None)

    def _start_stems(self, ts: str, codec: str, needs_bitrate: bool) -> None:
        session_dir = os.path.join(self.output_dir, ts)
        os.makedirs(session_dir, exist_ok=True)
        ext     = self.fmt if self.fmt != "opus" else "opus"
        sources = self._mixer.get_sources()

        stems_queues: dict = {}
        used_names:   set  = set()
        for src in sources:
            safe = _safe_filename(src.name)
            if safe in used_names:
                i = 2
                while f"{safe}_{i}" in used_names:
                    i += 1
                safe = f"{safe}_{i}"
            used_names.add(safe)

            path = os.path.join(session_dir, f"{safe}.{ext}")
            q    = queue.Queue(maxsize=64)
            proc = self._launch_ffmpeg(path, codec, needs_bitrate)
            stems_queues[src] = q
            self._procs.append(proc)
            t = threading.Thread(target=self._writer, args=(q, proc),
                                 daemon=True, name=f"rec-{safe}")
            self._threads.append(t)
            t.start()

        self._stems_queues = stems_queues
        self._mixer.attach_recorder(None, stems_queues)

    def _launch_ffmpeg(self, output_path: str, codec: str,
                       needs_bitrate: bool) -> subprocess.Popen:
        cmd = [
            "ffmpeg", "-y",
            "-f",  "f32le",
            "-ar", str(self._mixer.sample_rate),
            "-ac", str(self._mixer.channels),
            "-i",  "pipe:0",
            "-ar", str(self.sample_rate),
            "-c:a", codec,
        ]
        if needs_bitrate:
            cmd += ["-b:a", f"{self.bitrate}k"]
        cmd.append(output_path)
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )

    def _writer(self, q: queue.Queue, proc: subprocess.Popen) -> None:
        while True:
            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                if not self._running:
                    break
                continue
            try:
                # Mixer produces (channels, frames) float32; ffmpeg f32le wants interleaved.
                proc.stdin.write(chunk.T.flatten().astype(np.float32).tobytes())
            except (BrokenPipeError, OSError):
                break
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()
