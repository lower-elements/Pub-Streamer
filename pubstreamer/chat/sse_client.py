"""
SSE client for Audio Pub's /live/[id]/events endpoint.
Emits chat, state, and listener-count callbacks from a daemon thread.
"""

import json
import threading
import time
from typing import Callable

import httpx


def discover_stream_id(base_url: str, user_id: str, timeout: float = 8.0) -> "str | None":
    """
    Query Audio Pub's SvelteKit data endpoint to find the active stream ID
    for a given user (whose UUID is the Icecast mount point).

    Returns the stream UUID string, or None if not found / user has no active stream.
    """
    url = f"{base_url.rstrip('/')}/user/{user_id}/__data.json"
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        nodes = r.json().get("nodes", [])
        # nodes[1].data is a devalue-encoded array; data[0] is the page-load object
        # with {"stream": <index>, ...}. data[1] is the stream object template with
        # {"id": <index>, "state": <index>, ...}. data[2] is the stream's UUID string.
        # We also need to check state — only connect to active/disconnected streams.
        for node in nodes:
            if node.get("type") != "data":
                continue
            data = node.get("data", [])
            if not isinstance(data, list) or len(data) < 2:
                continue
            root = data[0]
            if not isinstance(root, dict) or "stream" not in root:
                continue
            stream_idx = root["stream"]
            if stream_idx is None or not isinstance(stream_idx, int):
                continue
            stream_tmpl = data[stream_idx]
            if not isinstance(stream_tmpl, dict):
                continue
            id_idx = stream_tmpl.get("id")
            state_idx = stream_tmpl.get("state")
            if not isinstance(id_idx, int) or not isinstance(state_idx, int):
                continue
            stream_id = data[id_idx] if id_idx < len(data) else None
            state = data[state_idx] if state_idx < len(data) else None
            if isinstance(stream_id, str) and state in ("active", "disconnected", "pending"):
                return stream_id
    except Exception as e:
        print(f"[SSEClient] stream discovery error: {e}")
    return None


class AudioPubChatClient:
    """
    Connects to an Audio Pub SSE stream and dispatches events to callbacks.

    Callbacks are invoked from the background thread — UI handlers must
    schedule tkinter updates via root.after() rather than touching widgets
    directly.
    """

    def __init__(self):
        self.on_chat: Callable[[str, str], None] | None = None       # (username, content)
        self.on_chat_delete: Callable[[str], None] | None = None     # (chat_id)
        self.on_state: Callable[[str], None] | None = None           # ("active"|"disconnected"|...)
        self.on_listeners: Callable[[int], None] | None = None       # (count)
        # Fired once when the stream UUID is first discovered; arg is the full listener URL.
        self.on_stream_id_ready: Callable[[str], None] | None = None  # (stream_url)

        # Additional subscribers for chat events (e.g. TTS sources).
        # Each receives (username, content) on the SSE thread.
        self._chat_subscribers: list[Callable[[str, str], None]] = []

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._base_url = ""
        self._user_id = ""
        self._stream_id = ""

    def add_chat_subscriber(self, fn: Callable[[str, str], None]):
        """Register an additional chat callback (thread-safe to call from UI thread)."""
        if fn not in self._chat_subscribers:
            self._chat_subscribers.append(fn)

    def remove_chat_subscriber(self, fn: Callable[[str, str], None]):
        try:
            self._chat_subscribers.remove(fn)
        except ValueError:
            pass

    def start(self, base_url: str, user_id: str):
        """Start the SSE client. user_id is the Audio Pub user UUID (= Icecast mount point)."""
        self._base_url = base_url.rstrip("/")
        self._user_id = user_id
        self._stream_id = ""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sse-chat")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        # Discover stream ID from Audio Pub before connecting.
        retry_delay = 1
        while not self._stop_event.is_set() and not self._stream_id:
            self._stream_id = discover_stream_id(self._base_url, self._user_id) or ""
            if not self._stream_id:
                print(f"[SSEClient] no active stream found for user {self._user_id}, retrying in {retry_delay}s")
                self._stop_event.wait(timeout=retry_delay)
                retry_delay = min(retry_delay * 2, 30)

        if self._stop_event.is_set():
            return

        print(f"[SSEClient] discovered stream {self._stream_id}")
        if self.on_stream_id_ready:
            stream_url = f"{self._base_url}/live/{self._stream_id}"
            self.on_stream_id_ready(stream_url)
        url = f"{self._base_url}/live/{self._stream_id}/events"
        retry_delay = 1
        while not self._stop_event.is_set():
            try:
                with httpx.stream("GET", url,
                                  headers={"Accept": "text/event-stream"},
                                  timeout=None) as response:
                    retry_delay = 1
                    self._consume(response)
            except httpx.RemoteProtocolError:
                pass
            except Exception as e:
                if not self._stop_event.is_set():
                    print(f"[SSEClient] connection error: {e}")
            if not self._stop_event.is_set():
                self._stop_event.wait(timeout=retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    def _consume(self, response):
        event_type = "message"
        data_lines: list[str] = []

        for line in response.iter_lines():
            if self._stop_event.is_set():
                break
            line = line.rstrip("\r")
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "":
                # Dispatch accumulated event
                if data_lines:
                    payload = "".join(data_lines)
                    self._dispatch(event_type, payload)
                event_type = "message"
                data_lines = []

    def _dispatch(self, event_type: str, payload: str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return

        if event_type == "chat":
            user = data.get("user", {})
            username = (user.get("displayName") or user.get("name", "?")) if isinstance(user, dict) else str(user)
            content  = data.get("content", "")
            if self.on_chat:
                self.on_chat(username, content)
            for fn in list(self._chat_subscribers):
                try:
                    fn(username, content)
                except Exception:
                    pass

        elif event_type == "chat_delete" and self.on_chat_delete:
            self.on_chat_delete(str(data.get("chatId", "")))

        elif event_type == "state" and self.on_state:
            self.on_state(data.get("state", ""))

        elif event_type == "listeners" and self.on_listeners:
            self.on_listeners(int(data.get("activeListeners", 0)))
