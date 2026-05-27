"""Mastodon tab — OAuth login, post composition, auto-post on stream start."""

import threading
import wx

from ..config import Config
from ..mastodon.client import MastodonClient
from ..mastodon.oauth import run_oauth_flow
from ..i18n import _


class MastodonPanel(wx.Panel):
    def __init__(self, parent, config: Config, client: MastodonClient):
        super().__init__(parent)
        self._cfg    = config
        self._client = client
        self._build()
        self._load()

    # ── build ────────────────────────────────────────────────────────────────

    def _build(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── credentials ──────────────────────────────────────────────────────
        cred_box  = wx.StaticBox(self, label=_("Mastodon account"))
        cred_form = wx.StaticBoxSizer(cred_box, wx.VERTICAL)

        # Instance row
        inst_row = wx.BoxSizer(wx.HORIZONTAL)
        inst_row.Add(wx.StaticText(self, label=_("Instance (e.g. mastodon.social):")),
                     0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._instance = wx.TextCtrl(self, name="Mastodon instance")
        self._instance.Bind(wx.EVT_TEXT, self._on_instance_change)
        inst_row.Add(self._instance, 1, wx.ALIGN_CENTER_VERTICAL)
        cred_form.Add(inst_row, 0, wx.EXPAND | wx.ALL, 6)

        # Connect button + account status
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self._connect_btn = wx.Button(self, label=_("&Connect to Mastodon"))
        self._connect_btn.Bind(wx.EVT_BUTTON, self._on_connect_toggle)
        btn_row.Add(self._connect_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self._account_lbl = wx.StaticText(self, label=_("Not connected"),
                                           style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_END)
        btn_row.Add(self._account_lbl, 1, wx.ALIGN_CENTER_VERTICAL)
        cred_form.Add(btn_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        outer.Add(cred_form, 0, wx.EXPAND | wx.ALL, 8)

        # ── post composition ─────────────────────────────────────────────────
        post_box   = wx.StaticBox(self, label=_("Stream announcement post"))
        post_sizer = wx.StaticBoxSizer(post_box, wx.VERTICAL)

        post_hint = wx.StaticText(
            self,
            label=_("Use {url}, {title}, or {description} to insert stream info."))
        post_hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        post_hint.Wrap(560)
        post_sizer.Add(post_hint, 0, wx.ALL, 6)

        self._post_text = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 100),
                                      name="Post text")
        self._post_text.Bind(wx.EVT_TEXT, self._on_post_text_change)
        post_sizer.Add(self._post_text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        outer.Add(post_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── auto-post + post button ───────────────────────────────────────────
        ctrl_row = wx.BoxSizer(wx.HORIZONTAL)
        self._auto_post_cb = wx.CheckBox(self, label=_("&Auto-post when stream goes live"))
        self._auto_post_cb.Bind(wx.EVT_CHECKBOX, self._on_auto_post_change)
        ctrl_row.Add(self._auto_post_cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)

        self._post_btn = wx.Button(self, label=_("&Post Now"))
        self._post_btn.Bind(wx.EVT_BUTTON, self._on_post_now)
        self._post_btn.Disable()   # enabled only when stream is live
        ctrl_row.Add(self._post_btn, 0, wx.ALIGN_CENTER_VERTICAL)

        outer.Add(ctrl_row, 0, wx.LEFT | wx.BOTTOM, 8)

        # ── status ───────────────────────────────────────────────────────────
        self._status_lbl = wx.StaticText(self, label="",
                                          style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_END)
        outer.Add(self._status_lbl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(outer)

    # ── load ─────────────────────────────────────────────────────────────────

    def _load(self):
        self._instance.SetValue(self._cfg.mastodon_instance)
        self._client.instance_url = self._cfg.mastodon_instance
        self._client.access_token = self._cfg.mastodon_token
        self._post_text.SetValue(self._cfg.mastodon_post_text)
        self._auto_post_cb.SetValue(self._cfg.mastodon_auto_post)
        if self._cfg.mastodon_token:
            self._show_connected(self._cfg.mastodon_account_name)
        else:
            self._show_disconnected()

    # ── account state helpers ─────────────────────────────────────────────────

    def _show_connected(self, acct: str):
        self._connect_btn.SetLabel(_("&Disconnect"))
        label = _("Connected as @{acct}").format(acct=acct) if acct else _("Connected")
        self._account_lbl.SetLabel(label)
        self._account_lbl.SetForegroundColour(wx.Colour(30, 140, 30))

    def _show_disconnected(self):
        self._connect_btn.SetLabel(_("&Connect to Mastodon"))
        self._account_lbl.SetLabel(_("Not connected"))
        self._account_lbl.SetForegroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

    # ── credential handlers ───────────────────────────────────────────────────

    def _on_instance_change(self, _event=None):
        self._cfg.mastodon_instance = self._instance.GetValue().strip()
        self._client.instance_url   = self._cfg.mastodon_instance
        self._cfg.save()

    def _on_connect_toggle(self, _event=None):
        if self._cfg.mastodon_token:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        instance = self._instance.GetValue().strip()
        if not instance:
            wx.MessageBox(_("Enter your Mastodon instance first (e.g. mastodon.social)."),
                          "Mastodon", wx.OK | wx.ICON_WARNING, self)
            return
        self._connect_btn.Disable()
        self._account_lbl.SetLabel(_("Opening browser…"))
        self._account_lbl.SetForegroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))

        def _success(token: str, acct: str):
            self._cfg.mastodon_token        = token
            self._cfg.mastodon_account_name = acct
            self._client.access_token       = token
            self._cfg.save()
            wx.CallAfter(self._connect_btn.Enable)
            wx.CallAfter(self._show_connected, acct)

        def _error(msg: str):
            wx.CallAfter(self._connect_btn.Enable)
            wx.CallAfter(self._show_disconnected)
            wx.CallAfter(wx.MessageBox,
                         _("Could not connect to Mastodon:\n{msg}").format(msg=msg),
                         "Mastodon", wx.OK | wx.ICON_ERROR)

        run_oauth_flow(instance, on_success=_success, on_error=_error)

    def _disconnect(self):
        self._cfg.mastodon_token        = ""
        self._cfg.mastodon_account_name = ""
        self._client.access_token       = ""
        self._cfg.save()
        self._show_disconnected()

    # ── post-text / auto-post ─────────────────────────────────────────────────

    def _on_post_text_change(self, _event=None):
        self._cfg.mastodon_post_text = self._post_text.GetValue()
        self._cfg.save()

    def _on_auto_post_change(self, _event=None):
        self._cfg.mastodon_auto_post = self._auto_post_cb.GetValue()
        self._cfg.save()

    # ── stream live / stopped notifications ──────────────────────────────────

    def on_stream_live(self, stream_url: str):
        """Called (from any thread) when Audio Pub confirms the stream is live."""
        self._client.stream_url         = stream_url
        self._client.stream_title       = self._cfg.ap_stream_title
        self._client.stream_description = self._cfg.ap_stream_description
        wx.CallAfter(self._post_btn.Enable)
        if self._auto_post_cb.GetValue():
            wx.CallAfter(self._do_post)

    def on_stream_stopped(self):
        """Called when the stream stops."""
        self._client.clear()
        wx.CallAfter(self._post_btn.Disable)
        wx.CallAfter(self._status_lbl.SetLabel, "")

    # ── posting ──────────────────────────────────────────────────────────────

    def _on_post_now(self, _event=None):
        self._do_post()

    def _do_post(self):
        if not self._client.instance_url or not self._client.access_token:
            wx.MessageBox(_("Connect your Mastodon account first."),
                          "Mastodon", wx.OK | wx.ICON_WARNING, self)
            return
        text = self._post_text.GetValue().strip()
        if not text:
            wx.MessageBox(_("The post text is empty."), "Mastodon",
                          wx.OK | wx.ICON_WARNING, self)
            return
        self._post_btn.Disable()
        self._post_btn.SetLabel(_("Posting…"))
        self._status_lbl.SetLabel(_("Posting…"))

        def _worker():
            try:
                _sid, url = self._client.post(text)
                wx.CallAfter(_done, url, None)
            except Exception as e:
                wx.CallAfter(_done, None, str(e))

        def _done(post_url, err):
            if not self:
                return
            self._post_btn.SetLabel(_("&Post Now"))
            if self._client.stream_url:
                self._post_btn.Enable()
            if err:
                self._status_lbl.SetForegroundColour(wx.Colour(180, 30, 30))
                self._status_lbl.SetLabel(_("Post failed: {err}").format(err=err))
            else:
                self._status_lbl.SetForegroundColour(
                    wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
                self._status_lbl.SetLabel(_("Posted: {post_url}").format(post_url=post_url))

        threading.Thread(target=_worker, daemon=True, name="masto-post").start()
