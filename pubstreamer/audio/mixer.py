"""
Audio mixer: pulls frames from all sources, applies per-source VST chains,
applies gain, sums into a single output buffer, then applies a master VST chain.

A single background mix thread (start/stop) produces chunks at the target
sample rate and distributes them to:
  - _stream_queue  → consumed by IcecastStream via read_for_stream()
  - _mon_queue     → consumed by the pyaudio monitor callback
This ensures each source's capture queue is only read by one thread.
"""

import queue
import threading
import time
import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    import pyaudio

from .vst_chain import VstChain

_FORMAT = pyaudio.paFloat32


class AudioSource:
    """Wraps any capture object alongside its gain, mute, monitor, and VST chain."""

    def __init__(self, capture, name: str, gain: float = 1.0):
        self.capture          = capture
        self.name             = name
        self.gain             = gain
        self.vst              = VstChain()
        self.muted            = False
        self.mute_to_stream   = False   # heard locally but excluded from the stream
        self.monitored        = False
        self.fade_duration: float = 0.0  # seconds; 0 = instant mute/unmute
        self.peak: float = 0.0  # rolling peak updated by mix thread

        # Fade state: 0.0 = silent, 1.0 = full volume; advances each chunk.
        self._gain_factor: float = 1.0

    def advance_gain(self, chunk_frames: int, sample_rate: int) -> "float | np.ndarray":
        """
        Return the gain envelope for this chunk and advance the fade state.
        Returns a plain float when gain is flat (no ramp needed), or a
        float32 ndarray of shape (chunk_frames,) when actively fading.
        Called once per chunk from the mix thread.
        """
        target = 0.0 if self.muted else 1.0

        if self.fade_duration <= 0.0 or sample_rate <= 0:
            self._gain_factor = target
            return self._gain_factor * self.gain

        if self._gain_factor == target:
            return self._gain_factor * self.gain

        step = chunk_frames / (self.fade_duration * sample_rate)
        diff = target - self._gain_factor
        if abs(diff) <= step:
            ramp = np.linspace(self._gain_factor, target, chunk_frames, dtype=np.float32)
            self._gain_factor = target
        else:
            end = self._gain_factor + (1.0 if diff > 0 else -1.0) * step
            ramp = np.linspace(self._gain_factor, end, chunk_frames, dtype=np.float32)
            self._gain_factor = end
        return (ramp * self.gain).astype(np.float32)


class Mixer:
    """
    Thread-safe mixer driven by a single background mix thread.

    Call start() once after construction. The mix thread calls _compute_mix()
    at chunk_frames/sample_rate intervals and distributes chunks to
    _stream_queue and _mon_queue. Nothing else should call _compute_mix().
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 2,
                 chunk_frames: int = 1024):
        self.sample_rate  = sample_rate
        self.channels     = channels
        self.chunk_frames = chunk_frames
        self.master_vst   = VstChain(sample_rate)

        self._sources: list[AudioSource] = []
        self._lock = threading.Lock()

        self._stream_queue: queue.Queue = queue.Queue(maxsize=8)

        self._rec_lock = threading.Lock()
        self._rec_queue: "queue.Queue | None" = None
        self._stems_queues: "dict | None" = None   # AudioSource → Queue

        self._running        = False
        self._mix_thread: threading.Thread | None = None
        # When True the monitor callback drives mixing from the hardware clock;
        # the software mix loop idles.  Set by start_monitor / stop_monitor.
        self._mon_drives_mix = False

        self._mon_pa:     object | None = None
        self._mon_stream: object | None = None

        # Extra gain applied only to the monitor output (not the stream).
        # Lets users compensate for process-loopback signals being quieter
        # than what they hear through hardware amp/DSP (e.g. GoXLR).
        self.monitor_gain: float = 1.0

    # ── source management ───────────────────────────────────────────────────

    def add_source(self, source: AudioSource):
        with self._lock:
            self._sources.append(source)

    def remove_source(self, source: AudioSource):
        with self._lock:
            if source in self._sources:
                self._sources.remove(source)

    def get_sources(self) -> list[AudioSource]:
        with self._lock:
            return list(self._sources)

    # ── recording taps ──────────────────────────────────────────────────────

    def attach_recorder(self, rec_queue, stems_queues=None):
        """Register queues that _compute_mix feeds alongside the stream queue."""
        with self._rec_lock:
            self._rec_queue    = rec_queue
            self._stems_queues = stems_queues

    def detach_recorder(self):
        with self._rec_lock:
            self._rec_queue    = None
            self._stems_queues = None

    # ── mix thread ──────────────────────────────────────────────────────────

    def start(self):
        """Start the background mix thread. Safe to call multiple times."""
        if self._running:
            return
        self._running = True
        self._mix_thread = threading.Thread(
            target=self._mix_loop, daemon=True, name="mixer"
        )
        self._mix_thread.start()

    def stop(self):
        """Stop the mix thread and close the monitor stream."""
        self._running = False
        self.stop_monitor()
        if self._mix_thread:
            self._mix_thread.join(timeout=2)
            self._mix_thread = None

    def _mix_loop(self):
        # Raise Windows timer resolution to 1 ms so time.sleep() is accurate enough
        # to keep the stream queue full without jitter-induced dropouts.
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        try:
            self._mix_loop_inner()
        finally:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass

    def _mix_loop_inner(self):
        interval = self.chunk_frames / self.sample_rate   # ~21.3 ms at 48 kHz
        while self._running:
            if self._mon_drives_mix:
                # Monitor callback is driving the mix from the hardware clock.
                # Nothing to do here; sleep and check again.
                time.sleep(interval)
                continue
            t0 = time.perf_counter()
            out, _ = self._compute_mix()
            try:
                self._stream_queue.put_nowait(out)
            except queue.Full:
                pass
            elapsed = time.perf_counter() - t0
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)

    def _compute_mix(self) -> "tuple[np.ndarray, np.ndarray | None]":
        """Returns (stream_mix, monitor_mix). monitor_mix is None when not monitoring."""
        out = np.zeros((self.channels, self.chunk_frames), dtype=np.float32)
        mon = np.zeros((self.channels, self.chunk_frames), dtype=np.float32) if self._mon_drives_mix else None
        silence = np.zeros((self.channels, self.chunk_frames), dtype=np.float32)

        with self._rec_lock:
            rec_q    = self._rec_queue
            stems_q  = self._stems_queues

        with self._lock:
            sources = list(self._sources)

        for src in sources:
            frame = src.capture.read()
            if frame is None:
                if stems_q is not None and src in stems_q:
                    try: stems_q[src].put_nowait(silence)
                    except queue.Full: pass
                continue
            if frame.shape != (self.channels, self.chunk_frames):
                frame = np.zeros((self.channels, self.chunk_frames), dtype=np.float32)
            gain_env = src.advance_gain(self.chunk_frames, self.sample_rate)
            if isinstance(gain_env, float) and gain_env == 0.0:
                if stems_q is not None and src in stems_q:
                    try: stems_q[src].put_nowait(silence)
                    except queue.Full: pass
                continue  # fully muted, no contribution
            p = float(np.max(np.abs(frame)))
            src.peak = p if p > src.peak else src.peak * 0.97
            frame = src.vst.process(frame)
            scaled = frame * gain_env  # gain_env broadcasts: float or (chunk_frames,)
            if not src.mute_to_stream:
                out += scaled
            if mon is not None and src.monitored:
                mon += scaled
            if stems_q is not None and src in stems_q:
                try: stems_q[src].put_nowait(scaled.copy())
                except queue.Full: pass

        out = self.master_vst.process(out)
        np.clip(out, -1.0, 1.0, out=out)
        if mon is not None:
            np.clip(mon, -1.0, 1.0, mon)

        # Feed recording queue (mixed, after master VST — matches stream exactly).
        if rec_q is not None:
            try: rec_q.put_nowait(out.copy())
            except queue.Full: pass

        return out, mon

    def read_for_stream(self) -> np.ndarray:
        """Block until the next mixed chunk is ready (called by IcecastStream)."""
        try:
            return self._stream_queue.get(timeout=1.0)
        except queue.Empty:
            return np.zeros((self.channels, self.chunk_frames), dtype=np.float32)

    # ── monitor output ──────────────────────────────────────────────────────

    @property
    def monitor_active(self) -> bool:
        return self._mon_stream is not None and self._mon_stream.is_active()

    def start_monitor(self, device_index: int | None = None):
        """Open a pyaudio output stream for monitor playback."""
        self.stop_monitor()
        self._mon_pa = pyaudio.PyAudio()
        self._mon_stream = self._mon_pa.open(
            format=_FORMAT,
            channels=self.channels,
            rate=self.sample_rate,
            output=True,
            output_device_index=device_index,
            frames_per_buffer=self.chunk_frames,
            stream_callback=self._mon_callback,
        )
        # Signal the software mix loop to idle; the callback owns mixing now.
        self._mon_drives_mix = True
        self._mon_stream.start_stream()

    def stop_monitor(self):
        self._mon_drives_mix = False
        if self._mon_stream:
            try:
                self._mon_stream.stop_stream()
                self._mon_stream.close()
            except Exception:
                pass
            self._mon_stream = None
        if self._mon_pa:
            pa = self._mon_pa
            self._mon_pa = None
            # pa.terminate() can block — run it on a daemon thread and don't join.
            threading.Thread(target=pa.terminate, daemon=True,
                             name="pa-terminate-mon").start()

    def _mon_callback(self, in_data, frame_count, time_info, status):
        # Compute the mix here, driven by the hardware output clock.
        # This eliminates the software-timer drift that caused monitor latency buildup.
        out, mon = self._compute_mix()
        # Also feed the stream queue so streaming stays in sync when monitoring.
        try:
            self._stream_queue.put_nowait(out)
        except queue.Full:
            try: self._stream_queue.get_nowait()
            except queue.Empty: pass
            try: self._stream_queue.put_nowait(out)
            except queue.Full: pass
        if mon is None:
            mon = np.zeros((self.channels, self.chunk_frames), dtype=np.float32)
        if self.monitor_gain != 1.0:
            mon = mon * self.monitor_gain
            np.clip(mon, -1.0, 1.0, out=mon)
        return (mon.T.flatten().tobytes(), pyaudio.paContinue)


def _drain(q: queue.Queue):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break
