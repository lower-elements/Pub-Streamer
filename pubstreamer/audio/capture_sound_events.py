"""Sound-event source — plays audio cues on chat, listener join/leave."""

import pathlib
import threading

import numpy as np

_SOUNDS_DIR = pathlib.Path(__file__).parent.parent.parent / "sounds"

# All recognised events, in display order: (key, label, filename)
EVENTS: list[tuple[str, str, str]] = [
    ("chat",           "Chat message",   "chat.wav"),
    ("join",           "Listener joins", "join.wav"),
    ("leave",          "Listener leaves","leave.wav"),
    ("mastodon_reply", "Mastodon reply", "mastodon_reply.wav"),
    ("stream_start",   "Stream starts",  "stream_start.wav"),
    ("stream_end",     "Stream ends",    "stream_end.wav"),
    ("record_start",   "Recording starts","record_start.wav"),
    ("record_end",     "Recording ends", "record_end.wav"),
]

# Fallback filenames tried in order when the primary file is missing.
_FALLBACKS: dict[str, list[str]] = {
    "mastodon_reply": ["mastodon_reply.wav", "chat.wav"],
}


def list_packs() -> list[str]:
    """Return ['default', …other packs in alphabetical order]."""
    if not _SOUNDS_DIR.is_dir():
        return ["default"]
    others = sorted(p.name for p in _SOUNDS_DIR.iterdir()
                    if p.is_dir() and p.name != "default")
    return ["default"] + others


def sound_path(pack: str, filename: str) -> pathlib.Path:
    return _SOUNDS_DIR / pack / filename


def available_events(pack: str) -> set[str]:
    """Return the set of event keys whose sound file (or any fallback) exists in *pack*."""
    result = set()
    for key, _label, fname in EVENTS:
        candidates = _FALLBACKS.get(key, [fname])
        if any(sound_path(pack, f).is_file() for f in candidates):
            result.add(key)
    return result


class SoundEventCapture:
    """
    Capture object that outputs audio cues for SSE chat and Icecast
    listener-count changes.  Plugs into the mixer the same way as
    any other capture via read().
    """

    def __init__(self, pack: str = "default",
                 enabled_events: "set[str] | None" = None,
                 sample_rate: int = 48000,
                 channels: int = 2,
                 chunk_frames: int = 1024,
                 icecast_host: str = "",
                 icecast_port: int = 8000,
                 mountpoint: str = "",
                 poll_interval: float = 5.0):
        self.pack           = pack
        self.enabled_events = set(enabled_events or [])
        self._sr            = sample_rate
        self._ch            = channels
        self._cf            = chunk_frames
        self._host          = icecast_host
        self._port          = icecast_port
        self._mount         = mountpoint.strip("/")
        self._poll_iv       = poll_interval

        # Each entry is [audio_array, current_pos]; all play simultaneously.
        self._playing: list                    = []
        self._play_lock                        = threading.Lock()
        self._stop                             = threading.Event()
        self.error: "str | None"               = None

        # Stable bound-method references for subscriber registration.
        self._chat_ref           = self._on_chat
        self._mastodon_reply_ref = self._on_mastodon_reply

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        need_poll = bool({"join", "leave"} & self.enabled_events and self._host)
        if need_poll:
            threading.Thread(target=self._poll_loop, daemon=True,
                             name="sounds-poll").start()

    def stop(self):
        self._stop.set()

    def trigger(self, event_key: str):
        """Fire a sound event by key if it is enabled. Thread-safe."""
        if event_key in self.enabled_events:
            self._trigger(event_key)

    def flush(self):
        """Stop all currently playing sounds immediately."""
        with self._play_lock:
            self._playing.clear()

    # ── read() ───────────────────────────────────────────────────────────────

    def read(self) -> "np.ndarray | None":
        out = np.zeros((self._ch, self._cf), dtype=np.float32)
        with self._play_lock:
            if not self._playing:
                return out
            still = []
            for item in self._playing:
                audio, pos = item
                remaining = audio.shape[1] - pos
                if remaining <= 0:
                    continue
                end = min(pos + self._cf, audio.shape[1])
                out[:, :end - pos] += audio[:, pos:end]
                item[1] = end
                if end < audio.shape[1]:
                    still.append(item)
            self._playing = still
        return out

    # ── sound triggering ─────────────────────────────────────────────────────

    def _trigger(self, event_key: str):
        fname = next((f for k, _l, f in EVENTS if k == event_key), None)
        if fname is None:
            return
        candidates = _FALLBACKS.get(event_key, [fname])
        path = next((sound_path(self.pack, f) for f in candidates
                     if sound_path(self.pack, f).is_file()), None)
        if path is None:
            return
        try:
            from ..tts.base import decode_audio_bytes
            audio = decode_audio_bytes(path.read_bytes(), self._sr, self._ch)
            if audio is not None and audio.shape[1] > 0:
                with self._play_lock:
                    self._playing.append([audio, 0])
        except Exception as e:
            self.error = str(e)
            print(f"[SoundEvents] trigger '{event_key}' error: {e}", flush=True)

    def _on_chat(self, username: str, content: str):
        if "chat" in self.enabled_events:
            self._trigger("chat")

    def _on_mastodon_reply(self):
        if "mastodon_reply" in self.enabled_events:
            self._trigger("mastodon_reply")

    # ── Icecast listener polling ─────────────────────────────────────────────

    def _poll_loop(self):
        prev: "int | None" = None
        while not self._stop.wait(timeout=self._poll_iv):
            try:
                count = self._fetch_listeners()
                if count is not None and prev is not None:
                    if count > prev and "join" in self.enabled_events:
                        self._trigger("join")
                    elif count < prev and "leave" in self.enabled_events:
                        self._trigger("leave")
                if count is not None:
                    prev = count
            except Exception as e:
                self.error = str(e)

    def _fetch_listeners(self) -> "int | None":
        import httpx
        url = f"http://{self._host}:{self._port}/status-json.xsl"
        r = httpx.get(url, timeout=5.0)
        r.raise_for_status()
        sources = r.json().get("icestats", {}).get("source")
        if sources is None:
            return None
        if isinstance(sources, dict):
            sources = [sources]
        if not self._mount:
            # No mountpoint configured — use first source
            return int(sources[0].get("listeners", 0)) if sources else None
        for s in sources:
            # Match via listenurl ("http://host:port/mount") or explicit mount key
            listenurl = s.get("listenurl", "")
            if listenurl.rsplit("/", 1)[-1] == self._mount:
                return int(s.get("listeners", 0))
            if s.get("mount", "").strip("/") == self._mount:
                return int(s.get("listeners", 0))
        return None
