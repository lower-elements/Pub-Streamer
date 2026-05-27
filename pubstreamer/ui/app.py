"""Root wx.Frame — three-tab Notebook layout with native Win32 controls."""

import wx

from ..i18n import _


class _StreamCloseDialog(wx.Dialog):
    def __init__(self, parent, stream_title: str = ""):
        super().__init__(parent, title=_("Stream In Progress"),
                         style=wx.DEFAULT_DIALOG_STYLE)
        self.ID_AUTO   = wx.NewIdRef()
        self.ID_MANUAL = wx.NewIdRef()
        self.ID_END    = wx.NewIdRef()

        msg = _("A stream is in progress.")
        if stream_title:
            msg += f'\n"{stream_title}"'
        msg += _("\nHow would you like to handle it?")

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label=msg), 0, wx.ALL, 12)

        btn_sizer = wx.BoxSizer(wx.VERTICAL)
        defs = [
            (self.ID_AUTO,   _("&Auto Resume on Next Launch"),
             _("Pub-Streamer reconnects automatically on next launch.")),
            (self.ID_MANUAL, _("&Manual Resume on Next Launch"),
             _("On next launch, click Reconnect when you're ready.")),
            (self.ID_END,    _("&End Stream Entirely"),
             _("Ends the stream on Audio Pub and disconnects now.")),
            (wx.ID_CANCEL,   _("&Keep Streaming"), ""),
        ]
        for id_, label, tip in defs:
            btn = wx.Button(self, id_, label)
            if tip:
                btn.SetToolTip(tip)
            btn_sizer.Add(btn, 0, wx.EXPAND | wx.BOTTOM, 4)
            btn.Bind(wx.EVT_BUTTON, lambda e, r=id_: self.EndModal(r))

        outer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(outer)
        self.Centre()

from ..config import Config
from ..audio.mixer import Mixer
from ..chat.sse_client import AudioPubChatClient
from .source_panel import SourcePanel
from .stream_panel import StreamPanel
from .chat_panel import ChatPanel
from .mastodon_panel import MastodonPanel
from ..mastodon.client import MastodonClient
from ..audiopub.client import AudioPubClient
from ..recording.recorder import Recorder
from .recording_panel import RecordingPanel

# Language menu option tags: (display_label, language_code)
_LANG_OPTIONS = [
    ("System Default", ""),
    ("English",        "en"),
    ("日本語 (Japanese)", "ja"),
]


class PubStreamerFrame(wx.Frame):
    def __init__(self, parent, config: Config):
        super().__init__(parent, title="Pub-Streamer", size=(720, 540),
                         style=wx.DEFAULT_FRAME_STYLE)
        self._cfg = config
        self._mixer = Mixer(
            sample_rate=config.sample_rate,
            channels=config.channels,
            chunk_frames=config.chunk_frames,
        )
        self._sse      = AudioPubChatClient()
        self._mastodon = MastodonClient()
        self._audiopub  = AudioPubClient()
        self._audiopub.setup(config.base_url)
        self._recorder  = Recorder(self._mixer)
        self._build_menu()
        self._build_body()
        self._mixer.start()
        if config.ap_token:
            self._audiopub.restore(
                config.ap_token, config.ap_user_id,
                config.ap_display_name, config.ap_stream_key,
            )
            self._stream_panel.on_account_restored(
                config.ap_display_name, config.ap_stream_key, config.ap_user_id
            )
        self._statusbar = self.CreateStatusBar()
        self._statusbar.SetStatusText(_("Ready"))

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Centre()

    # ── menu ────────────────────────────────────────────────────────────────

    def _build_menu(self):
        bar = wx.MenuBar()

        file_menu = wx.Menu()
        file_menu.Append(wx.ID_SAVE, _("&Save config\tCtrl+S"))
        file_menu.AppendSeparator()
        file_menu.Append(wx.ID_EXIT, _("E&xit"))
        bar.Append(file_menu, _("&File"))

        help_menu = wx.Menu()
        help_menu.Append(wx.ID_ABOUT, _("&About"))
        help_menu.AppendSeparator()

        # Language submenu
        lang_menu = wx.Menu()
        self._lang_items: list[tuple[wx.MenuItem, str]] = []
        current_lang = self._cfg.language
        for label, code in _LANG_OPTIONS:
            item = lang_menu.AppendRadioItem(wx.ID_ANY, label)
            if code == current_lang:
                item.Check(True)
            self._lang_items.append((item, code))
            self.Bind(wx.EVT_MENU, lambda e, c=code: self._on_language(c), item)
        help_menu.AppendSubMenu(lang_menu, _("&Language"))

        bar.Append(help_menu, _("&Help"))

        self.SetMenuBar(bar)
        self.Bind(wx.EVT_MENU, lambda _: self._cfg.save(), id=wx.ID_SAVE)
        self.Bind(wx.EVT_MENU, lambda _: self.Close(), id=wx.ID_EXIT)
        self.Bind(wx.EVT_MENU, self._on_about, id=wx.ID_ABOUT)

    # ── body ─────────────────────────────────────────────────────────────────

    def _build_body(self):
        self._notebook    = wx.Notebook(self, name="Main navigation")
        self._sources     = SourcePanel(self._notebook, self._mixer, self._cfg,
                                        sse=self._sse, mastodon=self._mastodon)
        self._notebook.AddPage(self._sources, _("Sources"))
        self._mastodon_panel = MastodonPanel(self._notebook, self._cfg, self._mastodon)
        self._stream_panel = StreamPanel(
            self._notebook, self._cfg, self._mixer, self._sse,
            self._audiopub,
            on_stream_state=self._on_stream_state,
            mastodon_panel=self._mastodon_panel,
            recorder=self._recorder,
        )
        self._recording_panel = RecordingPanel(self._notebook, self._cfg, self._recorder)
        # Chain recorder state so sound events fire alongside the UI callback.
        _rec_ui_cb = self._recorder.on_state_change
        def _rec_state(state: str):
            if _rec_ui_cb:
                _rec_ui_cb(state)
            self._sources.fire_sound_event(
                "record_start" if state == "recording" else "record_end"
            )
        self._recorder.on_state_change = _rec_state
        self._notebook.AddPage(self._stream_panel, _("Stream"))
        self._chat = ChatPanel(self._notebook, self._sse, self._cfg, self._audiopub)
        self._notebook.AddPage(self._chat, _("Chat"))
        self._notebook.AddPage(self._mastodon_panel, _("Mastodon"))
        self._notebook.AddPage(self._recording_panel, _("Recording"))

        root_sizer = wx.BoxSizer(wx.VERTICAL)
        root_sizer.Add(self._notebook, 1, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(root_sizer)

    # ── callbacks ────────────────────────────────────────────────────────────

    def _on_stream_state(self, state: str):
        labels = {
            "connecting": _("Connecting to Icecast…"),
            "streaming":  _("Streaming"),
            "stopped":    _("Stream stopped"),
            "error":      _("Error: ffmpeg not found — add it to PATH"),
        }
        wx.CallAfter(self._statusbar.SetStatusText, labels.get(state, state))
        if state == "streaming":
            self._sources.fire_sound_event("stream_start")
        elif state in ("stopped", "error"):
            self._sources.fire_sound_event("stream_end")

    def _on_about(self, _event=None):
        wx.MessageBox(
            _("Pub-Streamer\n\n"
              "Lightweight all-in-one Audio Pub streaming client.\n"
              "Captures mic and per-app audio, applies VST effects,\n"
              "encodes via ffmpeg, and streams to Audio Pub.\n"
              "Chat TTS is a source — add it from the Sources tab.\n\n"
              "Built for low-spec hardware."),
            _("About Pub-Streamer"),
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    def _on_language(self, code: str):
        if code == self._cfg.language:
            return
        self._cfg.language = code
        self._cfg.save()
        wx.MessageBox(
            _("Language will change on next launch."),
            _("Language"),
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    def _on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE and not event.HasAnyModifier():
            self._sources.flush_all_speech()
        else:
            event.Skip()

    def _on_close(self, event):
        if self._stream_panel.is_streaming:
            dlg    = _StreamCloseDialog(self, self._cfg.ap_stream_title)
            result = dlg.ShowModal()
            id_auto, id_manual, id_end = dlg.ID_AUTO, dlg.ID_MANUAL, dlg.ID_END
            dlg.Destroy()
            if result == id_auto:
                self._stream_panel.prepare_resume("auto")
                self._stream_panel.disconnect_encoder()
            elif result == id_manual:
                self._stream_panel.prepare_resume("manual")
                self._stream_panel.disconnect_encoder()
            elif result == id_end:
                self._stream_panel.stop_stream()
            else:
                event.Veto()
                return
        if self._recorder.is_running:
            secs = self._recorder.remaining_seconds()
            if secs >= 1:
                est = f"about {round(secs)} second(s) of buffered audio remain"
            else:
                est = "less than a second of buffered audio remains"
            dlg = wx.MessageDialog(
                self,
                _("A local recording is still saving.\n")
                + f"{est.capitalize()}, plus a moment for the encoder to finish.\n\n"
                + _("Exit now and lose the end of the recording?"),
                _("Recording in progress"),
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            )
            dlg.SetYesNoLabels(_("Exit Now"), _("Keep Open"))
            result = dlg.ShowModal()
            dlg.Destroy()
            if result != wx.ID_YES:
                event.Veto()
                return
        try:
            self._sources.save_all()
        except Exception:
            pass
        try:
            self._cfg.save()
        except Exception:
            pass
        self.Destroy()
