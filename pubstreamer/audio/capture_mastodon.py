"""Mastodon Replies TTS source — polls a Mastodon thread for new replies."""

import html
import queue
import re
import threading

import numpy as np


def _strip_html(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", raw)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


class MastodonRepliesCapture:
    """
    Polls GET /api/v1/statuses/{id}/context for new descendants and
    speaks them via the configured TTS engine.  Sits idle while
    MastodonClient.current_status_id is None.
    """

    def __init__(self, mastodon_client, engine,
                 sample_rate: int = 48000,
                 channels: int = 2,
                 chunk_frames: int = 1024,
                 poll_interval: float = 15.0,
                 fallback_engine=None):
        self._client      = mastodon_client
        self._engine      = engine
        self._fallback    = fallback_engine
        self._sr          = sample_rate
        self._ch          = channels
        self._cf          = chunk_frames
        self._poll_iv     = poll_interval

        self._seen_ids:    set[str]                  = set()
        self._tracked_sid: str | None                = None
        self._queue:       "queue.Queue[np.ndarray]" = queue.Queue(maxsize=32)
        self._play_buf:    "np.ndarray | None"       = None
        self._play_pos:    int                       = 0
        self._stop                                   = threading.Event()
        self.error:        "str | None"              = None

        # Called (no args) whenever a new reply is detected, before TTS speaks.
        self.on_reply = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="masto-replies").start()

    def stop(self):
        self._stop.set()

    def flush(self):
        """Drop all queued audio and stop current playback immediately."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._play_buf = None

    # ── read() ───────────────────────────────────────────────────────────────

    def read(self) -> "np.ndarray | None":
        silence = np.zeros((self._ch, self._cf), dtype=np.float32)
        if self._play_buf is None:
            try:
                self._play_buf = self._queue.get_nowait()
                self._play_pos = 0
            except queue.Empty:
                return silence
        remaining = self._play_buf.shape[1] - self._play_pos
        if remaining <= 0:
            self._play_buf = None
            return silence
        if remaining >= self._cf:
            chunk = self._play_buf[:, self._play_pos:self._play_pos + self._cf].copy()
            self._play_pos += self._cf
        else:
            chunk = silence.copy()
            chunk[:, :remaining] = self._play_buf[:, self._play_pos:]
            self._play_buf = None
        return chunk

    # ── polling ──────────────────────────────────────────────────────────────

    def _poll_loop(self):
        while not self._stop.wait(timeout=self._poll_iv):
            sid = self._client.current_status_id
            if not sid or not self._client.instance_url:
                continue
            if sid != self._tracked_sid:
                self._seen_ids.clear()
                self._tracked_sid = sid
            try:
                self._fetch_and_speak(sid)
            except Exception as e:
                self.error = str(e)
                print(f"[MastodonReplies] poll error: {e}", flush=True)

    def _fetch_and_speak(self, status_id: str):
        import httpx
        instance = self._client.instance_url.strip("/")
        resp = httpx.get(
            f"https://{instance}/api/v1/statuses/{status_id}/context",
            timeout=10.0,
        )
        resp.raise_for_status()
        for reply in resp.json().get("descendants", []):
            rid = reply.get("id")
            if not rid or rid in self._seen_ids:
                continue
            self._seen_ids.add(rid)
            content = _strip_html(reply.get("content", ""))
            if not content:
                continue
            if self.on_reply is not None:
                try:
                    self.on_reply()
                except Exception:
                    pass
            name = reply.get("account", {}).get("display_name", "")
            text = f"{name}: {content}" if name else content
            self._speak(text)

    def _speak(self, text: str):
        audio = None
        try:
            audio = self._engine.synthesize(text, self._sr, self._ch)
        except Exception as e:
            self.error = str(e)
        if (audio is None or audio.shape[1] == 0) and self._fallback:
            try:
                audio = self._fallback.synthesize(text, self._sr, self._ch)
            except Exception as e:
                self.error = str(e)
        if audio is not None and audio.shape[1] > 0:
            try:
                self._queue.put_nowait(audio)
            except queue.Full:
                pass
