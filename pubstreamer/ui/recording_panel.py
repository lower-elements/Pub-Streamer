"""Recording tab: configure and control local recording independent of streaming."""

import threading
import wx

from ..config import Config
from ..recording.recorder import Recorder
from ..i18n import _

_LOSSLESS = {"wav", "flac"}
_FORMATS  = ["WAV", "MP3", "FLAC", "OGG", "AAC", "Opus"]
_FMT_KEYS = ["wav",  "mp3", "flac", "ogg", "aac", "opus"]
_RATES    = ["44100", "48000", "96000", "192000"]


class RecordingPanel(wx.Panel):
    def __init__(self, parent, config: Config, recorder: Recorder):
        super().__init__(parent)
        self._cfg = config
        self._rec = recorder

        self._rec.on_state_change = lambda state: wx.CallAfter(self._on_state, state)

        self._build()
        self._load()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── Settings ─────────────────────────────────────────────────────────
        settings_box  = wx.StaticBox(self, label=_("Recording settings"))
        settings_sizer = wx.StaticBoxSizer(settings_box, wx.VERTICAL)

        # Output directory
        dir_row = wx.BoxSizer(wx.HORIZONTAL)
        dir_row.Add(wx.StaticText(self, label=_("Output directory:")),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._dir_ctrl = wx.TextCtrl(self, name="Output directory")
        self._dir_ctrl.Bind(wx.EVT_TEXT, self._on_dir_changed)
        dir_row.Add(self._dir_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._browse_btn = wx.Button(self, label=_("Browse…"), name="Browse output directory")
        self._browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        dir_row.Add(self._browse_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        settings_sizer.Add(dir_row, 0, wx.EXPAND | wx.ALL, 6)

        # Format / sample rate / bitrate
        fmt_row = wx.BoxSizer(wx.HORIZONTAL)

        fmt_row.Add(wx.StaticText(self, label=_("Format:")),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._fmt_cb = wx.ComboBox(self, choices=_FORMATS, style=wx.CB_READONLY,
                                   name="Recording format")
        self._fmt_cb.Bind(wx.EVT_COMBOBOX, self._on_live_sync)
        fmt_row.Add(self._fmt_cb, 0, wx.RIGHT, 12)

        fmt_row.Add(wx.StaticText(self, label=_("Sample rate:")),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._rate_cb = wx.ComboBox(self, choices=_RATES, style=wx.CB_READONLY,
                                    name="Recording sample rate")
        self._rate_cb.Bind(wx.EVT_COMBOBOX, self._on_live_sync)
        fmt_row.Add(self._rate_cb, 0, wx.RIGHT, 12)

        self._kbps_lbl = wx.StaticText(self, label=_("Bitrate (kbps):"))
        fmt_row.Add(self._kbps_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._kbps = wx.SpinCtrl(self, min=32, max=512, name="Recording bitrate")
        fmt_row.Add(self._kbps, 0)

        settings_sizer.Add(fmt_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Checkboxes
        self._stems_cb = wx.CheckBox(
            self, label=_("&Split into stems"))
        self._stems_cb.Bind(wx.EVT_CHECKBOX, self._on_live_sync)
        settings_sizer.Add(self._stems_cb, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self._with_stream_cb = wx.CheckBox(
            self, label=_("&Record with stream"))
        self._with_stream_cb.Bind(wx.EVT_CHECKBOX, self._on_live_sync)
        settings_sizer.Add(self._with_stream_cb, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        outer.Add(settings_sizer, 0, wx.EXPAND | wx.ALL, 8)

        # ── Control ──────────────────────────────────────────────────────────
        ctrl_box   = wx.StaticBox(self, label=_("Manual recording"))
        ctrl_sizer = wx.StaticBoxSizer(ctrl_box, wx.HORIZONTAL)

        self._start_btn = wx.Button(self, label=_("&Start Recording"))
        self._start_btn.Bind(wx.EVT_BUTTON, self._on_start)
        ctrl_sizer.Add(self._start_btn, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)

        self._stop_btn = wx.Button(self, label=_("S&top Recording"))
        self._stop_btn.Bind(wx.EVT_BUTTON, self._on_stop)
        self._stop_btn.Disable()
        ctrl_sizer.Add(self._stop_btn, 0, wx.TOP | wx.BOTTOM | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 6)

        self._status_lbl = wx.StaticText(self, label=_("Idle"))
        ctrl_sizer.Add(self._status_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)

        outer.Add(ctrl_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(outer)

    # ── load / save config ───────────────────────────────────────────────────

    def _load(self):
        self._dir_ctrl.SetValue(self._cfg.rec_output_dir)

        fmt_key = self._cfg.rec_format
        idx = _FMT_KEYS.index(fmt_key) if fmt_key in _FMT_KEYS else 0
        self._fmt_cb.SetSelection(idx)

        rate_str = str(self._cfg.rec_sample_rate)
        if rate_str in _RATES:
            self._rate_cb.SetSelection(_RATES.index(rate_str))
        else:
            self._rate_cb.SetSelection(1)  # 48000

        self._kbps.SetValue(self._cfg.rec_bitrate)
        self._stems_cb.SetValue(self._cfg.rec_stems)
        self._with_stream_cb.SetValue(self._cfg.rec_with_stream)
        self._sync_bitrate_state()
        self._push_to_recorder()

    def _save_config(self):
        self._cfg.rec_output_dir  = self._dir_ctrl.GetValue().strip()
        self._cfg.rec_stems       = self._stems_cb.GetValue()
        self._cfg.rec_with_stream = self._with_stream_cb.GetValue()
        idx = self._fmt_cb.GetSelection()
        self._cfg.rec_format      = _FMT_KEYS[idx] if 0 <= idx < len(_FMT_KEYS) else "wav"
        rate_str = self._rate_cb.GetValue()
        try:
            self._cfg.rec_sample_rate = int(rate_str)
        except ValueError:
            pass
        self._cfg.rec_bitrate = self._kbps.GetValue()
        self._cfg.save()
        self._push_to_recorder()

    def _push_to_recorder(self):
        self._rec.output_dir         = self._cfg.rec_output_dir
        self._rec.stems              = self._cfg.rec_stems
        self._rec.record_with_stream = self._cfg.rec_with_stream
        self._rec.fmt                = self._cfg.rec_format
        self._rec.sample_rate        = self._cfg.rec_sample_rate
        self._rec.bitrate            = self._cfg.rec_bitrate

    def _sync_bitrate_state(self):
        idx = self._fmt_cb.GetSelection()
        key = _FMT_KEYS[idx] if 0 <= idx < len(_FMT_KEYS) else "wav"
        lossless = key in _LOSSLESS
        self._kbps.Enable(not lossless)
        self._kbps_lbl.Enable(not lossless)

    # ── event handlers ────────────────────────────────────────────────────────

    def _on_dir_changed(self, _event=None):
        self._rec.output_dir      = self._dir_ctrl.GetValue().strip()
        self._cfg.rec_output_dir  = self._rec.output_dir
        self._cfg.save()

    def _on_browse(self, _event=None):
        dlg = wx.DirDialog(self, _("Choose recording output folder"),
                           self._dir_ctrl.GetValue() or "",
                           style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            self._dir_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _on_live_sync(self, _event=None):
        """Immediately push control state to the recorder (no disk I/O for most fields)."""
        self._sync_bitrate_state()
        self._rec.record_with_stream = self._with_stream_cb.GetValue()
        self._rec.stems              = self._stems_cb.GetValue()
        idx = self._fmt_cb.GetSelection()
        self._rec.fmt                = _FMT_KEYS[idx] if 0 <= idx < len(_FMT_KEYS) else "wav"
        rate_str = self._rate_cb.GetValue()
        try:
            self._rec.sample_rate = int(rate_str)
        except ValueError:
            pass
        self._rec.bitrate = self._kbps.GetValue()
        # Persist flags that affect auto-start so they survive an app restart.
        self._cfg.rec_with_stream = self._rec.record_with_stream
        self._cfg.rec_stems       = self._rec.stems
        self._cfg.rec_format      = self._rec.fmt
        self._cfg.rec_sample_rate = self._rec.sample_rate
        self._cfg.rec_bitrate     = self._rec.bitrate
        self._cfg.save()

    def _on_start(self, _event=None):
        self._save_config()
        if not self._rec.output_dir:
            wx.MessageBox(_("Set an output directory before recording."),
                          _("Recording"), wx.OK | wx.ICON_WARNING, self)
            return
        self._start_btn.Disable()
        self._status_lbl.SetLabel(_("Starting…"))

        def _worker():
            try:
                self._rec.start()
            except Exception as exc:
                wx.CallAfter(self._status_lbl.SetLabel, _("Error"))
                wx.CallAfter(self._start_btn.Enable)
                wx.CallAfter(wx.MessageBox,
                             f"Recording failed to start:\n{exc}",
                             _("Recording"), wx.OK | wx.ICON_ERROR)

        threading.Thread(target=_worker, daemon=True, name="rec-start-ui").start()

    def _on_stop(self, _event=None):
        self._stop_btn.Disable()
        self._status_lbl.SetLabel(_("Stopping…"))
        threading.Thread(target=self._rec.stop, daemon=True, name="rec-stop-ui").start()

    def _on_state(self, state: str):
        recording = (state == "recording")
        self._start_btn.Enable(not recording)
        self._stop_btn.Enable(recording)
        self._status_lbl.SetLabel(_("Recording…") if recording else _("Idle"))

        self._dir_ctrl.SetEditable(not recording)
        self._browse_btn.Enable(not recording)
        self._fmt_cb.Enable(not recording)
        self._rate_cb.Enable(not recording)
        self._kbps.Enable(not recording and not (
            _FMT_KEYS[self._fmt_cb.GetSelection()] in _LOSSLESS
            if 0 <= self._fmt_cb.GetSelection() < len(_FMT_KEYS) else False
        ))
        self._stems_cb.Enable(not recording)
