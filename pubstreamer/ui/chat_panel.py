"""Chat tab — message log and outbound message input."""

import threading
import time
import wx

from ..chat.sse_client import AudioPubChatClient
from ..config import Config
from ..i18n import _

_MAX_LINES = 200


class ChatPanel(wx.Panel):
    def __init__(self, parent, sse: AudioPubChatClient, config: Config,
                 audiopub=None):
        super().__init__(parent)
        self._sse      = sse
        self._cfg      = config
        self._audiopub = audiopub
        self._entries: list[str] = []
        self._build()
        self._wire_sse()

    def _build(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(self, label=_("Chat messages:")), 0, wx.LEFT | wx.TOP, 8)
        self._lb = wx.ListBox(self, style=wx.LB_SINGLE, name="Chat messages")
        sizer.Add(self._lb, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # ── Send row ─────────────────────────────────────────────────────────
        send_row = wx.BoxSizer(wx.HORIZONTAL)
        self._msg_ctrl = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER,
                                     name="Chat message to send")
        self._msg_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_send)
        send_row.Add(self._msg_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._send_btn = wx.Button(self, label=_("&Send"))
        self._send_btn.Bind(wx.EVT_BUTTON, self._on_send)
        send_row.Add(self._send_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(send_row, 0, wx.EXPAND | wx.ALL, 8)

        self._send_status = wx.StaticText(self, label="")
        sizer.Add(self._send_status, 0, wx.LEFT | wx.BOTTOM, 8)

        self.SetSizer(sizer)

    # ── SSE wiring ────────────────────────────────────────────────────────────

    def _wire_sse(self):
        self._sse.on_chat        = self._on_chat
        self._sse.on_chat_delete = self._on_chat_delete
        # on_listeners is handled by StreamPanel

    # ── SSE callbacks (background thread → wx.CallAfter) ─────────────────────

    def _on_chat(self, username: str, content: str):
        entry = f"[{username}]: {content}"
        queued_at = time.perf_counter()
        wx.CallAfter(self._append, entry, queued_at)

    def _on_chat_delete(self, chat_id: str):
        pass

    # ── Send ─────────────────────────────────────────────────────────────────

    def _on_send(self, _event=None):
        content = self._msg_ctrl.GetValue().strip()
        if not content:
            return

        username = "You"
        if self._audiopub is not None and self._audiopub.is_logged_in:
            username = getattr(self._audiopub, "display_name", None) or "You"

        stream_id = self._audiopub.current_stream_id if self._audiopub else ""

        self._msg_ctrl.SetValue("")

        if not stream_id:
            # No active stream — dispatch locally so TTS/sound sources can be tested.
            self._sse.dispatch_local(username, content)
            self._set_status(_("(local — not sent to stream)"))
            return

        # Active stream: dispatch locally immediately so TTS reads outgoing messages,
        # then post to Audio Pub in the background.
        self._sse.dispatch_local(username, content)
        self._send_btn.Disable()
        self._set_status("")

        def _worker():
            try:
                self._audiopub.send_chat(stream_id, content)
            except Exception as exc:
                wx.CallAfter(self._set_status, f"Error: {exc}")
            finally:
                wx.CallAfter(self._send_btn.Enable)

        threading.Thread(target=_worker, daemon=True, name="chat-send").start()

    def _set_status(self, msg: str):
        self._send_status.SetLabel(msg)

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _append(self, entry: str, queued_at: float | None = None):
        if queued_at is not None:
            lag = time.perf_counter() - queued_at
            if lag > 0.5:
                print(f"[ChatPanel] UI append lag {lag:.3f}s", flush=True)
        self._entries.append(entry)
        self._lb.Append(entry)
        while self._lb.GetCount() > _MAX_LINES:
            self._lb.Delete(0)
            self._entries.pop(0)
        self._lb.SetSelection(self._lb.GetCount() - 1)
