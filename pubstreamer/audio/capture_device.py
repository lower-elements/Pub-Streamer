"""
Microphone / WASAPI device capture via pyaudiowpatch.
ASIO capture is handled separately in capture_asio.py.
"""

import queue
import threading
import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    import pyaudio

_FORMAT = pyaudio.paFloat32
_RATE = 48000
_CHANNELS = 2


# ── WASAPI device enumeration ──────────────────────────────────────────────────

def list_wasapi_devices() -> list[dict]:
    """Return [{index, name, channels}] for WASAPI input devices (non-loopback)."""
    pa = pyaudio.PyAudio()
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0 and not info.get("isLoopbackDevice"):
            devices.append({
                "index": i,
                "name": info["name"],
                "channels": int(info["maxInputChannels"]),
            })
    pa.terminate()
    return devices

list_input_devices = list_wasapi_devices  # backwards compat alias


def list_loopback_devices() -> list[dict]:
    """Return [{index, name, channels, sample_rate}] for WASAPI loopback devices."""
    pa = pyaudio.PyAudio()
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("isLoopbackDevice"):
            devices.append({
                "index": i,
                "name": info["name"],
                "channels": min(int(info["maxInputChannels"]), 2),
                "sample_rate": int(info["defaultSampleRate"]),
            })
    pa.terminate()
    return devices


# ── WASAPI capture ─────────────────────────────────────────────────────────────

class DeviceCapture:
    """Captures audio from a WASAPI input device (microphone or loopback)."""

    def __init__(self, device_index: int, sample_rate: int = _RATE,
                 channels: int = _CHANNELS, chunk_frames: int = 1024):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_frames = chunk_frames
        self._queue: queue.Queue = queue.Queue(maxsize=4)
        self._pa = None
        self._stream = None

    def start(self):
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=_FORMAT,
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=self.chunk_frames,
            stream_callback=self._callback,
        )
        self._stream.start_stream()

    def stop(self):
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa:
            pa = self._pa
            self._pa = None
            # Pa_Terminate() can stall on Windows when PortAudio's internal threads
            # don't exit cleanly.  Run it on a daemon thread so a stuck call never
            # blocks the caller; the OS cleans up the handle if the thread outlives
            # the process.
            t = threading.Thread(target=pa.terminate, daemon=True, name="pa-terminate")
            t.start()
            t.join(timeout=1.0)

    def read(self) -> np.ndarray:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return np.zeros((self.channels, self.chunk_frames), dtype=np.float32)

    def _callback(self, in_data, frame_count, time_info, status):
        samples = np.frombuffer(in_data, dtype=np.float32)
        if len(samples) == frame_count:
            samples = np.stack([samples, samples])
        else:
            samples = samples.reshape(frame_count, self.channels).T.copy()
        try:
            self._queue.put_nowait(samples)
        except queue.Full:
            # Drop oldest stale frame; keep this fresh one.
            try: self._queue.get_nowait()
            except queue.Empty: pass
            try: self._queue.put_nowait(samples)
            except queue.Full: pass
        return (None, pyaudio.paContinue)
