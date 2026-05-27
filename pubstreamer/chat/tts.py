"""
SAPI 5 TTS wrapper — queue-driven, non-blocking.

All speech happens on a dedicated thread so the UI and SSE client are
never blocked waiting for an utterance to finish.
"""

import queue
import threading


_SAPI_AVAILABLE = False
try:
    import win32com.client
    _SAPI_AVAILABLE = True
except ImportError:
    pass


class Tts:
    """
    Thread-safe SAPI 5 text-to-speech with a bounded message queue.

    If pywin32 is not installed, speak() calls are silently no-ops so the
    rest of the application still runs on non-Windows machines.
    """

    def __init__(self, max_queue: int = 5):
        self._max_queue = max_queue
        self._enabled = True
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stop_event = threading.Event()
        self._voice = None
        self._voices_cache: list[str] = []
        self._thread = threading.Thread(target=self._run, daemon=True, name="sapi-tts")
        self._thread.start()

    # ── voice configuration ─────────────────────────────────────────────────

    def list_voices(self) -> list[str]:
        """Return cached voice list (populated by the TTS thread; no COM on caller)."""
        return list(self._voices_cache)

    def set_voice(self, index: int):
        self._queue.put(("_set_voice", index))

    def set_rate(self, rate: int):
        """rate: -10 (slowest) to 10 (fastest), 0 is normal."""
        self._queue.put(("_set_rate", rate))

    def set_volume(self, volume: int):
        """volume: 0–100."""
        self._queue.put(("_set_volume", volume))

    # ── speech control ──────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, v: bool):
        self._enabled = v

    def speak(self, text: str):
        if not self._enabled or not _SAPI_AVAILABLE:
            return
        try:
            self._queue.put_nowait(("speak", text))
        except queue.Full:
            # Drop oldest and re-queue
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(("speak", text))
            except queue.Full:
                pass

    def stop_current(self):
        """Interrupt whatever is being spoken right now."""
        self._queue.put(("stop", None))

    def shutdown(self):
        self._stop_event.set()
        self._queue.put(("_quit", None))
        self._thread.join(timeout=3)

    # ── background thread ───────────────────────────────────────────────────

    def _run(self):
        if not _SAPI_AVAILABLE:
            return
        import pythoncom
        pythoncom.CoInitialize()
        try:
            self._voice = win32com.client.Dispatch("SAPI.SpVoice")
            vs = self._voice.GetVoices()
            self._voices_cache = [vs.Item(i).GetDescription() for i in range(vs.Count)]
        except Exception as e:
            print(f"[TTS] SAPI init failed: {e}")
            pythoncom.CoUninitialize()
            return

        try:
            while not self._stop_event.is_set():
                try:
                    cmd, arg = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if cmd == "_quit":
                    break
                elif cmd == "speak":
                    try:
                        self._voice.Speak(arg, 1)
                        self._voice.WaitUntilDone(-1)
                    except Exception as e:
                        print(f"[TTS] speak error: {e}")
                elif cmd == "stop":
                    try:
                        self._voice.Speak("", 3)  # purge queue
                    except Exception:
                        pass
                elif cmd == "_set_voice":
                    try:
                        voices = self._voice.GetVoices()
                        if arg < voices.Count:
                            self._voice.Voice = voices.Item(arg)
                    except Exception:
                        pass
                elif cmd == "_set_rate":
                    try:
                        self._voice.Rate = max(-10, min(10, int(arg)))
                    except Exception:
                        pass
                elif cmd == "_set_volume":
                    try:
                        self._voice.Volume = max(0, min(100, int(arg)))
                    except Exception:
                        pass
        finally:
            pythoncom.CoUninitialize()
