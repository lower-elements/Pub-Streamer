"""
ChatTtsCapture — a mixer source that synthesises queued text to PCM audio.

speak(text) is called from the SSE subscriber thread when a chat message
arrives.  A background render thread converts text to audio via the
configured TtsEngine and appends it to a shared buffer.  The mixer calls
read() at the usual chunk rate; silence is returned between utterances.
"""

import queue
import threading
import numpy as np


class ChatTtsCapture:
    """
    Audio capture that converts chat messages to speech.

    Presents the same read() interface as other capture classes so it can
    be dropped straight into AudioSource / Mixer without changes.
    """

    _DEFAULT_TEMPLATE = "{username} says: {message}"

    def __init__(self, engine, sample_rate: int = 48000,
                 channels: int = 2, chunk_frames: int = 1024,
                 template: str = "", fallback_engine=None):
        self._engine       = engine
        self._fallback     = fallback_engine   # used when primary engine returns None
        self._sample_rate  = sample_rate
        self._channels     = channels
        self._chunk_frames = chunk_frames
        self.template      = template or self._DEFAULT_TEMPLATE

        self._text_queue: queue.Queue = queue.Queue()
        self._buf      = np.zeros((channels, 0), dtype=np.float32)
        self._buf_lock = threading.Lock()
        self._flush_gen = 0   # incremented on each flush; render loop discards stale audio

        self._running = False
        self._thread: threading.Thread | None = None
        self.error: str | None = None

        # Stable bound-method reference for SSE subscriber registration.
        # The SSE client calls fn(username, content); this formats the text.
        self._speak_ref = self._on_chat_message

    # ── public API ───────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._render_loop, daemon=True, name="tts-render"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        self._text_queue.put(None)   # sentinel to unblock the render loop
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def speak(self, text: str):
        """Enqueue a raw text string for synthesis.  Thread-safe."""
        self._text_queue.put(text)

    def flush(self):
        """Drain queued text and discard buffered audio.  Thread-safe."""
        self._flush_gen += 1
        while True:
            try:
                self._text_queue.get_nowait()
            except queue.Empty:
                break
        with self._buf_lock:
            self._buf = np.zeros((self._channels, 0), dtype=np.float32)

    def _on_chat_message(self, username: str, content: str):
        """SSE subscriber callback — formats and enqueues a chat message."""
        try:
            text = self.template.format(username=username, message=content)
        except (KeyError, ValueError):
            text = f"{username} says: {content}"
        self._text_queue.put(text)

    def read(self) -> np.ndarray:
        """Return the next chunk of synthesised audio, or silence if none ready."""
        with self._buf_lock:
            n = self._buf.shape[1]
            if n >= self._chunk_frames:
                chunk = self._buf[:, :self._chunk_frames].copy()
                self._buf = self._buf[:, self._chunk_frames:]
                return chunk
            if n > 0:
                # Partial buffer — pad the tail with silence.
                chunk = np.zeros((self._channels, self._chunk_frames), dtype=np.float32)
                chunk[:, :n] = self._buf
                self._buf = np.zeros((self._channels, 0), dtype=np.float32)
                return chunk
        return np.zeros((self._channels, self._chunk_frames), dtype=np.float32)

    # ── background render loop ───────────────────────────────────────────────

    def _render_loop(self):
        while self._running:
            try:
                text = self._text_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if text is None:
                break
            gen = self._flush_gen
            try:
                audio = self._engine.synthesize(text, self._sample_rate, self._channels)
            except Exception as e:
                self.error = str(e)
                audio = None
            if self._flush_gen != gen:
                continue
            if (audio is None or audio.shape[1] == 0) and self._fallback is not None:
                try:
                    audio = self._fallback.synthesize(text, self._sample_rate, self._channels)
                except Exception as e:
                    self.error = str(e)
                    audio = None
                if self._flush_gen != gen:
                    continue
            if audio is not None and audio.shape[1] > 0:
                with self._buf_lock:
                    self._buf = np.concatenate([self._buf, audio], axis=1)
