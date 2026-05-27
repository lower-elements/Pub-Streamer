"""Stream tab — Audio Pub account login, stream metadata, server settings, start/stop."""

import threading
from datetime import datetime, timedelta
import wx

from ..config import Config
from ..audio.mixer import Mixer
from ..chat.sse_client import AudioPubChatClient
from ..streaming.ffmpeg_stream import IcecastStream
from ..audiopub.client import AudioPubClient
from ..i18n import _


class _ScheduleDialog(wx.Dialog):
    def __init__(self, parent, hour12: int = 12, minute: int = 0, pm: bool = False):
        super().__init__(parent, title=_("Schedule Stream Start"),
                         style=wx.DEFAULT_DIALOG_STYLE)
        grid = wx.FlexGridSizer(rows=3, cols=2, vgap=8, hgap=8)

        grid.Add(wx.StaticText(self, label=_("Hour (1–12):")), 0, wx.ALIGN_CENTER_VERTICAL)
        self._hour = wx.SpinCtrl(self, value=str(hour12), min=1, max=12)
        grid.Add(self._hour, 0)

        grid.Add(wx.StaticText(self, label=_("Minute:")), 0, wx.ALIGN_CENTER_VERTICAL)
        self._minute = wx.SpinCtrl(self, value=str(minute), min=0, max=59)
        grid.Add(self._minute, 0)

        grid.Add(wx.StaticText(self, label=_("AM / PM:")), 0, wx.ALIGN_CENTER_VERTICAL)
        self._ampm = wx.Choice(self, choices=["AM", "PM"])
        self._ampm.SetSelection(1 if pm else 0)
        grid.Add(self._ampm, 0)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(grid, 0, wx.ALL, 12)
        outer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL),
                  0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(outer)
        self.Centre()

    def scheduled_datetime(self) -> datetime:
        h = self._hour.GetValue() % 12  # 12 AM → 0, 12 PM → 12
        if self._ampm.GetSelection() == 1:
            h += 12
        target = datetime.now().replace(hour=h, minute=self._minute.GetValue(),
                                        second=0, microsecond=0)
        if target <= datetime.now():
            target += timedelta(days=1)
        return target


class StreamPanel(wx.Panel):
    def __init__(self, parent, config: Config, mixer: Mixer,
                 sse: AudioPubChatClient, audiopub: AudioPubClient,
                 on_stream_state=None, mastodon_panel=None, recorder=None):
        super().__init__(parent)
        self._cfg             = config
        self._sse             = sse
        self._audiopub        = audiopub
        self._on_stream_state = on_stream_state
        self._mastodon_panel  = mastodon_panel
        self._recorder        = recorder

        self._streaming        = False
        self._reconnect_mode   = False
        self._pending_stream_id: str = ""
        self._stop_mode        = ""   # "disconnect" | "stop" | ""
        self._cur_listeners    = 0
        self._peak_listeners   = 0

        self._stream = IcecastStream(
            mixer_fn=mixer.read_for_stream,
            sample_rate=mixer.sample_rate,
            channels=mixer.channels,
            chunk_frames=mixer.chunk_frames,
        )
        self._stream.on_state_change = self._on_ffmpeg_state

        # Wire audiopub callbacks (called from background thread).
        self._audiopub.on_login_ok   = self._on_ap_login_ok
        self._audiopub.on_login_fail = self._on_ap_login_fail

        self._build()
        self._load()
        self._sse.on_listeners = self._on_sse_listeners

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── Audio Pub account ────────────────────────────────────────────────
        acct_box  = wx.StaticBox(self, label=_("Audio Pub account"))
        acct_form = wx.StaticBoxSizer(acct_box, wx.VERTICAL)

        # Instance URL row — always visible
        url_row = wx.BoxSizer(wx.HORIZONTAL)
        url_row.Add(wx.StaticText(self, label=_("Instance URL:")),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._ap_url = wx.TextCtrl(self, value=self._cfg.base_url, name="Audio Pub instance URL")
        url_row.Add(self._ap_url, 1, wx.ALIGN_CENTER_VERTICAL)
        acct_form.Add(url_row, 0, wx.EXPAND | wx.ALL, 6)

        # Login row (email + password + button)
        self._login_row = wx.BoxSizer(wx.HORIZONTAL)
        self._login_row.Add(wx.StaticText(self, label=_("Email:")),
                            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._ap_email = wx.TextCtrl(self, name="Audio Pub email")
        self._login_row.Add(self._ap_email, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self._login_row.Add(wx.StaticText(self, label=_("Password:")),
                            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._ap_password = wx.TextCtrl(self, style=wx.TE_PASSWORD, name="Audio Pub password")
        self._login_row.Add(self._ap_password, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self._login_btn = wx.Button(self, label=_("&Log In"))
        self._login_btn.Bind(wx.EVT_BUTTON, self._on_login)
        self._login_row.Add(self._login_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        acct_form.Add(self._login_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Connected row (status label + logout button)
        self._connected_row = wx.BoxSizer(wx.HORIZONTAL)
        self._acct_lbl = wx.StaticText(self, label="",
                                        style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_END)
        self._acct_lbl.SetForegroundColour(wx.Colour(30, 140, 30))
        self._connected_row.Add(self._acct_lbl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self._logout_btn = wx.Button(self, label=_("Log &Out"))
        self._logout_btn.Bind(wx.EVT_BUTTON, self._on_logout)
        self._connected_row.Add(self._logout_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        acct_form.Add(self._connected_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        outer.Add(acct_form, 0, wx.EXPAND | wx.ALL, 8)

        # ── Stream metadata ──────────────────────────────────────────────────
        meta_box  = wx.StaticBox(self, label=_("Stream details  (required when logged in)"))
        meta_form = wx.StaticBoxSizer(meta_box, wx.VERTICAL)
        meta_grid = wx.FlexGridSizer(rows=2, cols=2, vgap=6, hgap=8)
        meta_grid.AddGrowableCol(1, 1)

        meta_grid.Add(wx.StaticText(self, label=_("Title:")), 0, wx.ALIGN_CENTER_VERTICAL)
        self._stream_title = wx.TextCtrl(self, value=self._cfg.ap_stream_title,
                                          name="Stream title")
        meta_grid.Add(self._stream_title, 1, wx.EXPAND)

        meta_grid.Add(wx.StaticText(self, label=_("Description:")), 0, wx.ALIGN_CENTER_VERTICAL)
        self._stream_desc = wx.TextCtrl(self, value=self._cfg.ap_stream_description,
                                         style=wx.TE_MULTILINE, size=(-1, 60),
                                         name="Stream description")
        meta_grid.Add(self._stream_desc, 1, wx.EXPAND)

        meta_form.Add(meta_grid, 0, wx.EXPAND | wx.ALL, 6)
        self._stream_archive = wx.CheckBox(self, label=_("&Archive recording after stream ends"))
        self._stream_archive.SetValue(self._cfg.ap_stream_archive)
        meta_form.Add(self._stream_archive, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        outer.Add(meta_form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Server settings ──────────────────────────────────────────────────
        settings_box = wx.StaticBox(self, label=_("Server settings"))
        form = wx.StaticBoxSizer(settings_box, wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=5, cols=2, vgap=6, hgap=8)
        grid.AddGrowableCol(1, 1)

        def field(label, value, password=False, readonly=False):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            style = 0
            if password:
                style |= wx.TE_PASSWORD
            if readonly:
                style |= wx.TE_READONLY
            ctrl = wx.TextCtrl(self, value=value, style=style, name=label.rstrip(":"))
            grid.Add(ctrl, 1, wx.EXPAND)
            return ctrl

        self._ice_host = field(_("Icecast host:"),   self._cfg.icecast_host)
        self._ice_port = field(_("Icecast port:"),   str(self._cfg.icecast_port))
        self._username = field(_("Username:"),       self._cfg.source_user)
        self._password = field(_("Stream key:"),     self._cfg.source_password, password=True)
        self._mountpt  = field(_("Mount point:"),    self._cfg.mountpoint)

        form.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        outer.Add(form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Format row ───────────────────────────────────────────────────────
        fmt_sizer = wx.BoxSizer(wx.HORIZONTAL)
        fmt_sizer.Add(wx.StaticText(self, label=_("Bitrate:")),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._bitrate = wx.ComboBox(self, value=str(self._cfg.bitrate),
                                    choices=["64", "96", "128", "192"],
                                    style=wx.CB_READONLY, name="Bitrate")
        fmt_sizer.Add(self._bitrate, 0, wx.RIGHT, 12)
        fmt_sizer.Add(wx.StaticText(self, label=_("Format:")),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._fmt = wx.ComboBox(self, value=self._cfg.format,
                                choices=["mp3", "aac"],
                                style=wx.CB_READONLY, name="Format")
        fmt_sizer.Add(self._fmt, 0)
        outer.Add(fmt_sizer, 0, wx.LEFT | wx.BOTTOM, 8)

        # ── Start / stop ─────────────────────────────────────────────────────
        ctrl_row = wx.BoxSizer(wx.HORIZONTAL)
        self._start_btn = wx.Button(self, label=_("&Start Stream"))
        self._start_btn.Bind(wx.EVT_BUTTON, self._toggle)
        ctrl_row.Add(self._start_btn, 0, wx.RIGHT, 8)
        self._fresh_btn = wx.Button(self, label=_("Start &Fresh"))
        self._fresh_btn.Bind(wx.EVT_BUTTON, self._on_start_fresh)
        self._fresh_btn.Hide()
        ctrl_row.Add(self._fresh_btn, 0, wx.RIGHT, 8)
        self._status_text = wx.StaticText(self, label=_("Idle"))
        ctrl_row.Add(self._status_text, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(ctrl_row, 0, wx.LEFT | wx.BOTTOM, 8)

        # ── Schedule ─────────────────────────────────────────────────────────
        sched_row = wx.BoxSizer(wx.HORIZONTAL)
        self._sched_btn = wx.Button(self, label=_("Set Sc&heduled Time…"))
        self._sched_btn.Bind(wx.EVT_BUTTON, self._on_set_schedule)
        sched_row.Add(self._sched_btn, 0, wx.RIGHT, 8)
        self._sched_cb = wx.CheckBox(self, label=_("Automatically start at scheduled time"))
        self._sched_cb.Enable(False)
        sched_row.Add(self._sched_cb, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(sched_row, 0, wx.LEFT | wx.BOTTOM, 8)

        self._sched_dt: "datetime | None" = None
        self._sched_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_sched_tick, self._sched_timer)
        self._sched_timer.Start(5000)

        # ── Stream info (last in tab order; screen readers tab here to check state)
        outer.Add(wx.StaticText(self, label=_("Stream info:")),
                  0, wx.LEFT | wx.TOP, 8)
        self._info = wx.TextCtrl(
            self, style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_NO_VSCROLL,
            size=(-1, 80), name="Stream info")
        self._info.SetValue(_("Not streaming"))
        outer.Add(self._info, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(outer)

    # ── load ──────────────────────────────────────────────────────────────────

    def _load(self):
        if self._audiopub.is_logged_in:
            self._show_connected(self._audiopub.display_name)
        else:
            self._show_disconnected()
        mode = self._cfg.stream_resume_mode
        if mode == "auto" and self._cfg.stream_resume_id:
            wx.CallAfter(self._auto_reconnect)
        elif mode == "manual" and self._cfg.stream_resume_id:
            wx.CallAfter(self._enter_reconnect_mode,
                         self._cfg.stream_resume_id,
                         self._cfg.stream_resume_title)

    # ── account state helpers ──────────────────────────────────────────────────

    def _show_connected(self, display_name: str):
        name = display_name or self._cfg.ap_display_name or "your account"
        self._acct_lbl.SetLabel(_("Connected as {name}").format(name=name))
        for w in (self._ap_email, self._ap_password, self._login_btn):
            w.Hide()
        for w in (self._acct_lbl, self._logout_btn):
            w.Show()
        # Auto-fill stream key and mount point from account, make them read-only.
        if self._audiopub.stream_key:
            self._password.SetValue(self._audiopub.stream_key)
            self._password.SetEditable(False)
        if self._audiopub.user_id:
            self._mountpt.SetValue(self._audiopub.user_id)
            self._mountpt.SetEditable(False)
        self.Layout()

    def _show_disconnected(self):
        for w in (self._acct_lbl, self._logout_btn):
            w.Hide()
        for w in (self._ap_email, self._ap_password, self._login_btn):
            w.Show()
        self._password.SetEditable(True)
        self._mountpt.SetEditable(True)
        self.Layout()

    # ── account callbacks (called from background thread) ─────────────────────

    def _on_ap_login_ok(self, user_id: str, display_name: str, stream_key: str):
        self._cfg.ap_token        = self._audiopub.get_token()
        self._cfg.ap_user_id      = user_id
        self._cfg.ap_display_name = display_name
        self._cfg.ap_stream_key   = stream_key
        self._cfg.save()
        wx.CallAfter(self._login_btn.Enable)
        wx.CallAfter(self._login_btn.SetLabel, _("&Log In"))
        wx.CallAfter(self._show_connected, display_name)

    def _on_ap_login_fail(self, message: str):
        wx.CallAfter(self._login_btn.Enable)
        wx.CallAfter(self._login_btn.SetLabel, _("&Log In"))
        wx.CallAfter(wx.MessageBox,
                     _("Login failed:\n{message}").format(message=message),
                     _("Audio Pub"), wx.OK | wx.ICON_ERROR)

    # ── login / logout handlers ───────────────────────────────────────────────

    def _on_login(self, _event=None):
        email    = self._ap_email.GetValue().strip()
        password = self._ap_password.GetValue()
        if not email or not password:
            wx.MessageBox(_("Enter your email and password."),
                          _("Audio Pub"), wx.OK | wx.ICON_WARNING, self)
            return
        self._audiopub.setup(self._ap_url.GetValue().strip() or self._cfg.base_url)
        self._login_btn.Disable()
        self._login_btn.SetLabel(_("Logging in…"))
        self._audiopub.login_async(email, password)

    def _on_logout(self, _event=None):
        self._audiopub.logout()
        self._cfg.ap_token        = ""
        self._cfg.ap_user_id      = ""
        self._cfg.ap_display_name = ""
        self._cfg.ap_stream_key   = ""
        self._cfg.save()
        self._password.SetValue("")
        self._mountpt.SetValue("")
        self._show_disconnected()

    # ── stream control ────────────────────────────────────────────────────────

    def _toggle(self, _event=None):
        if self._reconnect_mode:
            self._do_reconnect()
        elif self._stream.is_running:
            self._ask_stop()
        else:
            self._start()

    def _start(self):
        self._save_config()
        self._dim_meta_fields(True)
        if self._audiopub.is_logged_in:
            self._start_with_account()
        else:
            self._start_manual()

    def _start_manual(self):
        if not self._cfg.mountpoint:
            self._dim_meta_fields(False)
            wx.MessageBox(
                _("Enter your Audio Pub user ID (mount point) before starting, "
                  "or log in to have it filled automatically."),
                _("Mount point required"), wx.OK | wx.ICON_WARNING, self)
            return
        self._do_start_stream()

    def _start_with_account(self):
        title = self._stream_title.GetValue().strip()
        if not title:
            self._dim_meta_fields(False)
            wx.MessageBox(_("Enter a stream title before starting."),
                          _("Title required"), wx.OK | wx.ICON_WARNING, self)
            return
        self._cfg.ap_stream_title       = title
        self._cfg.ap_stream_description = self._stream_desc.GetValue().strip()
        self._cfg.ap_stream_archive     = self._stream_archive.GetValue()
        self._cfg.save()

        self._start_btn.Disable()
        wx.CallAfter(self._status_text.SetLabel, _("Creating stream…"))

        def _worker():
            try:
                self._audiopub.create_stream(
                    title       = self._cfg.ap_stream_title,
                    description = self._cfg.ap_stream_description,
                    should_archive = self._cfg.ap_stream_archive,
                )
                wx.CallAfter(self._on_stream_created)
            except Exception as exc:
                wx.CallAfter(self._on_stream_create_failed, str(exc))

        threading.Thread(target=_worker, daemon=True, name="ap-create").start()

    def _on_stream_created(self):
        self._start_btn.Enable()
        self._do_start_stream()

    def _on_stream_create_failed(self, msg: str):
        self._start_btn.Enable()
        self._dim_meta_fields(False)
        wx.CallAfter(self._status_text.SetLabel, _("Error"))
        wx.MessageBox(_("Could not create stream on Audio Pub:\n{msg}").format(msg=msg),
                      _("Audio Pub"), wx.OK | wx.ICON_ERROR, self)

    def _do_start_stream(self):
        """Connect ffmpeg to Icecast and start the SSE client."""
        self._stream.start(
            host       = self._cfg.icecast_host,
            port       = self._cfg.icecast_port,
            username   = self._cfg.source_user,
            password   = self._cfg.source_password,
            mountpoint = self._cfg.mountpoint,
            bitrate    = self._cfg.bitrate,
            fmt        = self._cfg.format,
        )
        if self._mastodon_panel is not None:
            self._sse.on_stream_id_ready = self._mastodon_panel.on_stream_live
        self._sse.start(self._cfg.base_url, self._cfg.mountpoint)
        if self._recorder is not None and self._recorder.record_with_stream:
            def _start_rec():
                try:
                    self._recorder.start()
                except Exception as exc:
                    wx.CallAfter(wx.MessageBox,
                                 _("Recording failed to start:\n{exc}").format(exc=exc),
                                 _("Recording"), wx.OK | wx.ICON_WARNING)
            threading.Thread(target=_start_rec, daemon=True, name="rec-auto-start").start()
        wx.CallAfter(self._start_btn.SetLabel, _("&Stop Stream"))

    def _ask_stop(self):
        dlg = wx.MessageDialog(
            self,
            _("How do you want to stop the stream?"),
            _("Stop Stream"),
            wx.YES_NO | wx.CANCEL | wx.CANCEL_DEFAULT | wx.ICON_QUESTION,
        )
        dlg.SetYesNoCancelLabels(
            _("Disconnect &Encoder Only"), _("Stop &Entirely"), _("&Cancel")
        )
        result = dlg.ShowModal()
        dlg.Destroy()
        if result == wx.ID_YES:
            self._disconnect_encoder()
        elif result == wx.ID_NO:
            self._stop_entirely()

    def _disconnect_encoder(self):
        """Stop ffmpeg and SSE but leave the Audio Pub stream record open."""
        self._stop_mode = "disconnect"
        stream_id = self._audiopub.current_stream_id or ""
        title     = self._cfg.ap_stream_title
        self._stream.stop()
        self._sse.stop()
        self._sse.on_stream_id_ready = None
        if self._mastodon_panel is not None:
            self._mastodon_panel.on_stream_stopped()
        if self._recorder is not None and self._recorder.is_running:
            threading.Thread(target=self._recorder.stop,
                             daemon=True, name="rec-auto-stop").start()
        wx.CallAfter(self._enter_reconnect_mode, stream_id, title)

    def _stop_entirely(self):
        """Stop ffmpeg, SSE, and end the Audio Pub stream record."""
        self._stop_mode = "stop"
        self._stream.stop()
        self._sse.stop()
        self._sse.on_stream_id_ready = None
        if self._mastodon_panel is not None:
            self._mastodon_panel.on_stream_stopped()
        if self._recorder is not None and self._recorder.is_running:
            threading.Thread(target=self._recorder.stop,
                             daemon=True, name="rec-auto-stop").start()
        # Clear any stored resume mode.
        self._cfg.stream_resume_mode  = ""
        self._cfg.stream_resume_id    = ""
        self._cfg.stream_resume_title = ""
        self._cfg.save()
        # End the stream record on Audio Pub if we created one.
        if self._audiopub.current_stream_id:
            sid = self._audiopub.current_stream_id
            threading.Thread(
                target=self._audiopub.end_stream,
                args=(sid,),
                daemon=True,
                name="ap-end",
            ).start()

    def _on_ffmpeg_state(self, state: str):
        self._streaming = (state == "streaming")
        if not self._streaming:
            self._cur_listeners = 0
        if state in ("stopped", "error"):
            # _disconnect_encoder queues _enter_reconnect_mode after us, which
            # re-dims. For all other stop paths undim here.
            if self._stop_mode != "disconnect":
                wx.CallAfter(self._dim_meta_fields, False)
            label = (_("Disconnected") if self._stop_mode == "disconnect"
                     else _("Error — ffmpeg not found in PATH") if state == "error"
                     else _("Stopped"))
            self._stop_mode = ""
            wx.CallAfter(self._status_text.SetLabel, label)
            wx.CallAfter(self._start_btn.SetLabel, _("&Start Stream"))
        else:
            labels = {"connecting": _("Connecting…"), "streaming": _("Streaming")}
            wx.CallAfter(self._status_text.SetLabel, labels.get(state, state))
        wx.CallAfter(self._refresh_info)
        if self._on_stream_state:
            self._on_stream_state(state)

    def _on_sse_listeners(self, count: int):
        """Called from SSE background thread when listener count changes."""
        if count > self._peak_listeners:
            self._peak_listeners = count
        self._cur_listeners = count
        wx.CallAfter(self._refresh_info)
        if self._streaming and self._on_stream_state:
            n   = count
            pk  = self._peak_listeners
            self._on_stream_state(
                _("Streaming — {n} listener(s) (peak: {pk})").format(n=n, pk=pk)
            )

    def _refresh_info(self):
        """Rebuild the stream info text field from current state."""
        lines: list[str] = []
        if self._streaming:
            n  = self._cur_listeners
            pk = self._peak_listeners
            lines.append(_("State: Streaming"))
            lines.append(_("Listeners: {n}").format(n=n))
            lines.append(_("Peak: {pk}").format(pk=pk))
            if self._audiopub.current_stream_id:
                lines.append(_("Stream ID: {sid}").format(
                    sid=self._audiopub.current_stream_id))
        elif self._stream.is_running:
            lines.append(_("State: Connecting…"))
        else:
            lines.append(_("State: Not streaming"))
            if self._peak_listeners > 0:
                lines.append(_("Peak this session: {n} listener(s)").format(
                    n=self._peak_listeners))
        self._info.SetValue("\n".join(lines))

    # ── schedule ─────────────────────────────────────────────────────────────

    def _on_set_schedule(self, _event=None):
        if self._sched_dt:
            h24 = self._sched_dt.hour
            h12 = h24 % 12 or 12
            pm  = h24 >= 12
            m   = self._sched_dt.minute
        else:
            now = datetime.now()
            h24 = now.hour
            h12 = h24 % 12 or 12
            pm  = h24 >= 12
            m   = now.minute

        dlg = _ScheduleDialog(self, hour12=h12, minute=m, pm=pm)
        if dlg.ShowModal() == wx.ID_OK:
            self._sched_dt = dlg.scheduled_datetime()
            h24  = self._sched_dt.hour
            h12  = h24 % 12 or 12
            pm   = h24 >= 12
            m    = self._sched_dt.minute
            ampm = "PM" if pm else "AM"
            self._sched_cb.SetLabel(
                f"Automatically start at {h12}:{m:02d} {ampm}"
            )
            self._sched_cb.Enable(True)
        dlg.Destroy()
        self._sched_btn.SetFocus()

    def _on_sched_tick(self, _event=None):
        if not self._sched_cb.IsChecked():
            return
        if self._stream.is_running:
            return
        if self._sched_dt and datetime.now() >= self._sched_dt:
            self._sched_cb.SetValue(False)
            self._sched_dt = None
            self._start()

    # ── config save ───────────────────────────────────────────────────────────

    def _save_config(self):
        self._cfg.base_url        = self._ap_url.GetValue().strip() or self._cfg.base_url
        self._cfg.icecast_host    = self._ice_host.GetValue()
        self._cfg.source_user     = self._username.GetValue()
        if self._password.IsEditable():
            self._cfg.source_password = self._password.GetValue()
        if self._mountpt.IsEditable():
            self._cfg.mountpoint = self._mountpt.GetValue()
        try:
            self._cfg.icecast_port = int(self._ice_port.GetValue())
        except ValueError:
            pass
        try:
            self._cfg.bitrate = int(self._bitrate.GetValue())
        except ValueError:
            pass
        self._cfg.format = self._fmt.GetValue()
        self._audiopub.setup(self._cfg.base_url)
        self._cfg.save()

    def stop_stream(self):
        if self._stream.is_running:
            self._stop_entirely()

    # ── reconnect / resume ────────────────────────────────────────────────────

    @property
    def is_streaming(self) -> bool:
        return self._stream.is_running

    def _dim_meta_fields(self, dim: bool):
        colour = (wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE)
                  if dim else wx.NullColour)
        for ctrl in (self._stream_title, self._stream_desc):
            ctrl.SetEditable(not dim)
            ctrl.SetBackgroundColour(colour)
            ctrl.Refresh()

    def _enter_reconnect_mode(self, stream_id: str = "", title: str = ""):
        """Switch to reconnect state (dimmed fields, Reconnect button)."""
        self._reconnect_mode    = True
        self._pending_stream_id = stream_id
        if title:
            self._stream_title.SetValue(title)
        self._dim_meta_fields(True)
        self._start_btn.SetLabel(_("&Reconnect"))
        self._fresh_btn.Show()
        self._status_text.SetLabel(_("Disconnected — reconnect or start fresh"))
        self.Layout()

    def _auto_reconnect(self):
        """Called on UI thread at launch when auto resume mode is set."""
        self._audiopub.current_stream_id = self._cfg.stream_resume_id
        self._cfg.stream_resume_mode  = ""
        self._cfg.stream_resume_id    = ""
        self._cfg.stream_resume_title = ""
        self._cfg.save()
        self._do_start_stream()

    def _do_reconnect(self):
        """Reconnect encoder to an existing AP stream (Reconnect button handler)."""
        if self._pending_stream_id:
            self._audiopub.current_stream_id = self._pending_stream_id
        self._pending_stream_id = ""
        # Clear launch-time resume config.
        self._cfg.stream_resume_mode  = ""
        self._cfg.stream_resume_id    = ""
        self._cfg.stream_resume_title = ""
        self._cfg.save()
        self._reconnect_mode = False
        self._fresh_btn.Hide()
        self.Layout()
        self._do_start_stream()

    def _on_start_fresh(self, _event=None):
        """Abandon the existing stream and return to idle, ready for a new one."""
        sid = self._audiopub.current_stream_id or self._pending_stream_id
        if sid:
            threading.Thread(
                target=self._audiopub.end_stream, args=(sid,),
                daemon=True, name="ap-end-fresh",
            ).start()
        self._audiopub.current_stream_id = None
        self._cfg.stream_resume_mode  = ""
        self._cfg.stream_resume_id    = ""
        self._cfg.stream_resume_title = ""
        self._cfg.save()
        self._reconnect_mode    = False
        self._pending_stream_id = ""
        self._fresh_btn.Hide()
        self._start_btn.SetLabel(_("&Start Stream"))
        self._dim_meta_fields(False)
        self._stream_title.SetValue("")
        self._stream_desc.SetValue("")
        self._status_text.SetLabel(_("Idle"))
        self.Layout()

    # ── called by app.py before close ─────────────────────────────────────────

    def prepare_resume(self, mode: str):
        """Save stream state to config so the next launch can resume."""
        self._cfg.stream_resume_mode  = mode
        self._cfg.stream_resume_id    = self._audiopub.current_stream_id or ""
        self._cfg.stream_resume_title = self._cfg.ap_stream_title
        self._cfg.save()

    def disconnect_encoder(self):
        """Disconnect ffmpeg/SSE without ending the AP stream (called on close)."""
        if self._stream.is_running:
            self._stream.stop()
            self._sse.stop()
            self._sse.on_stream_id_ready = None
            if self._recorder is not None and self._recorder.is_running:
                threading.Thread(target=self._recorder.stop,
                                 daemon=True, name="rec-auto-stop").start()

    # ── called by app on startup if token was saved ────────────────────────────

    def on_account_restored(self, display_name: str, stream_key: str, user_id: str):
        """Called from any thread after a saved session is verified."""
        wx.CallAfter(self._show_connected, display_name)
