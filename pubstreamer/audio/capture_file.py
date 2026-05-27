"""
FileCapture — looping audio file source.

Reads any pedalboard.io-supported format (WAV, MP3, FLAC, OGG, AIFF…) into
memory on start(), resamples to the mixer sample rate, then serves
chunk_frames-sized slices in a round-robin loop.
"""

import threading
import numpy as np


class FileCapture:
    """
    Audio source that plays an audio file on a continuous loop.
    Presents the same read() interface as other capture classes.
    """

    def __init__(self, path: str, sample_rate: int = 48000,
                 channels: int = 2, chunk_frames: int = 1024):
        self.path         = path
        self.sample_rate  = sample_rate
        self.channels     = channels
        self.chunk_frames = chunk_frames
        self.error: str | None = None

        self._data: np.ndarray | None = None  # (channels, total_frames) float32
        self._pos  = 0
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._load, daemon=True, name="file-load").start()

    def _load(self):
        try:
            from pedalboard.io import AudioFile
            with AudioFile(self.path).resampled_to(self.sample_rate) as f:
                data = f.read(f.frames)   # → (channels, frames) float32
            # Channel normalisation
            if data.shape[0] == 1 and self.channels == 2:
                data = np.vstack([data, data])
            elif data.shape[0] > self.channels:
                data = data[:self.channels, :]
            self._data = data.astype(np.float32)
        except Exception as e:
            self.error = str(e)

    def stop(self):
        self._data = None

    def read(self) -> np.ndarray:
        data = self._data
        if data is None or data.shape[1] == 0:
            return np.zeros((self.channels, self.chunk_frames), dtype=np.float32)
        total = data.shape[1]
        with self._lock:
            start = self._pos
            end   = start + self.chunk_frames
            if end <= total:
                chunk     = data[:, start:end].copy()
                self._pos = 0 if end == total else end
            else:
                # Wrap: stitch tail of file with head
                tail      = data[:, start:]
                need      = self.chunk_frames - tail.shape[1]
                head      = data[:, :need]
                chunk     = np.concatenate([tail, head], axis=1)
                self._pos = need
        return chunk
