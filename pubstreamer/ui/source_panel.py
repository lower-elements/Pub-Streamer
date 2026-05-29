"""Sources tab — add mic/app sources, adjust volume, open VST chains."""

import ctypes
import datetime
import math
import os
import threading
import numpy as np
import wx


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)

from ..config import Config
from ..audio.mixer import AudioSource, Mixer
from ..audio.capture_device import DeviceCapture, list_wasapi_devices, list_loopback_devices
from ..audio.capture_process import (WatchedAppCapture, list_audio_sessions)
from ..audio.capture_file import FileCapture
from ..audio.capture_url import UrlStreamCapture
from ..audio.capture_tts import ChatTtsCapture
from ..audio.capture_sound_events import (SoundEventCapture, EVENTS as SOUND_EVENTS,
                                          list_packs, available_events)
from ..audio.capture_mastodon import MastodonRepliesCapture
from ..tts import engine_names, engine_key, engine_class, engine_display_name, make_engine
from .vst_panel import VstPanelDialog
from ..i18n import _


_EVENT_OBJECT_STATECHANGE = 0x800A
_OBJID_CLIENT = -4


def _make_sapi_fallback():
    """Return a SAPI engine fallback if SAPI is available, else None."""
    from ..tts import make_engine
    eng = make_engine("sapi")
    return eng if eng.is_available() else None


def _play_audio_blocking(audio: "np.ndarray", sample_rate: int, channels: int):
    """Play a float32 (channels, frames) array through the default output and block until done."""
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        import pyaudio
    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(format=pyaudio.paFloat32, channels=channels,
                         rate=sample_rate, output=True)
        clipped = np.clip(audio, -1.0, 1.0)
        stream.write(clipped.T.flatten().astype("float32").tobytes())
        stream.stop_stream()
        stream.close()
    finally:
        pa.terminate()


class _ListCtrlAccessible(wx.Accessible):
    """Exposes each item in a CheckListBox as ROLE_SYSTEM_CHECKBUTTON to MSAA."""

    def GetRole(self, childId):
        if childId == 0:
            return super().GetRole(childId)
        return (wx.ACC_OK, wx.ROLE_SYSTEM_CHECKBUTTON)

    def GetState(self, childId):
        if childId == 0:
            return super().GetState(childId)
        states = wx.ACC_STATE_SYSTEM_SELECTABLE | wx.ACC_STATE_SYSTEM_FOCUSABLE
        win = self.Window
        if win.IsChecked(childId - 1):
            states |= wx.ACC_STATE_SYSTEM_CHECKED
        if win.IsSelected(childId - 1):
            states |= wx.ACC_STATE_SYSTEM_SELECTED
            if win.HasFocus():
                states |= wx.ACC_STATE_SYSTEM_FOCUSED
        return (wx.ACC_OK, states)


class AccessibleCheckListBox(wx.CheckListBox):
    """wx.CheckListBox with MSAA accessibility so NVDA announces checked state."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.SetAccessible(_ListCtrlAccessible(self))
        self.Bind(wx.EVT_CHECKLISTBOX, self._notify_state_change)

    def _notify_state_change(self, event):
        event.Skip()
        ctypes.windll.user32.NotifyWinEvent(
            _EVENT_OBJECT_STATECHANGE,
            self.Handle,
            _OBJID_CLIENT,
            event.Selection + 1,
        )


def _offthread_sessions(timeout: float = 10.0) -> list[dict]:
    """Run list_audio_sessions() on a worker thread to avoid deadlocking the wx STA thread."""
    result: list[dict] = []
    done = threading.Event()

    def _worker():
        try:
            result.extend(list_audio_sessions())
        except Exception as e:
            _log(f"[session-worker] exception: {e}")
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True, name="session-enum").start()
    fired = done.wait(timeout=timeout)
    _log(f"[offthread_sessions] {'OK' if fired else 'TIMED OUT'} -> {len(result)} sessions")
    return result


class SourcePanel(wx.Panel):
    def __init__(self, parent, mixer: Mixer, config: Config, sse=None, mastodon=None):
        super().__init__(parent)
        self._mixer    = mixer
        self._config   = config
        self._sse      = sse       # AudioPubChatClient | None
        self._mastodon = mastodon  # MastodonClient | None
        self._sources: list[AudioSource] = []   # parallel to listbox items
        self._build()
        self._restore_all()

    def _build(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ── source list ──────────────────────────────────────────────────────
        sizer.Add(wx.StaticText(self, label=_("Audio sources:")), 0, wx.LEFT | wx.TOP, 8)

        self._lb = wx.ListBox(self, style=wx.LB_SINGLE, name="Audio sources")
        self._lb.Bind(wx.EVT_LISTBOX, self._on_select)
        sizer.Add(self._lb, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── source buttons ───────────────────────────────────────────────────
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self._btn_add    = wx.Button(self, label=_("&Add Source"))
        self._btn_edit   = wx.Button(self, label=_("&Edit…"))
        self._btn_remove = wx.Button(self, label=_("&Remove"))
        self._btn_vst    = wx.Button(self, label=_("Source &VST Chain…"))
        for btn in (self._btn_add, self._btn_edit, self._btn_remove, self._btn_vst):
            btn_row.Add(btn, 0, wx.RIGHT, 4)
        sizer.Add(btn_row, 0, wx.LEFT | wx.BOTTOM, 8)

        self._btn_add.Bind(wx.EVT_BUTTON,    self._add_source)
        self._btn_edit.Bind(wx.EVT_BUTTON,   self._edit)
        self._btn_remove.Bind(wx.EVT_BUTTON, self._remove)
        self._btn_vst.Bind(wx.EVT_BUTTON,    self._open_vst)
        self._btn_edit.Disable()
        self._btn_remove.Disable()

        # ── per-source controls ──────────────────────────────────────────────
        ctrl_box = wx.StaticBox(self, label=_("Selected source"))
        ctrl_sizer = wx.StaticBoxSizer(ctrl_box, wx.VERTICAL)

        vol_row = wx.BoxSizer(wx.HORIZONTAL)
        _vol_lbl = wx.StaticText(self, label=_("Volume:"))   # must precede slider in z-order
        self._vol_slider = wx.Slider(self, value=100, minValue=0, maxValue=100,
                                     name="Volume", style=wx.SL_HORIZONTAL)
        self._vol_slider.Bind(wx.EVT_SLIDER, self._on_volume)
        self._vol_slider.Bind(wx.EVT_SCROLL_CHANGED, self._on_volume_commit)
        self._vol_val_lbl = wx.StaticText(self, label="100%", size=(40, -1))
        vol_row.Add(_vol_lbl,           0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        vol_row.Add(self._vol_slider,   1, wx.EXPAND)
        vol_row.Add(self._vol_val_lbl,  0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        ctrl_sizer.Add(vol_row, 0, wx.EXPAND | wx.ALL, 4)

        check_row = wx.BoxSizer(wx.HORIZONTAL)

        # Creation order = z-order = tab order AND MSAA label-association order.
        # Must be: mute → "Fade:" label → fade slider → monitor.
        self._mute_cb = wx.CheckBox(self, label=_("&Mute this source"))
        self._mute_cb.Bind(wx.EVT_CHECKBOX, self._on_mute)

        _fade_lbl = wx.StaticText(self, label=_("Fade:"))   # precedes slider in z-order → MSAA label
        self._fade_slider = wx.Slider(self, value=0, minValue=0, maxValue=100,
                                      name="Fade duration", style=wx.SL_HORIZONTAL,
                                      size=(110, -1))
        self._fade_slider.Bind(wx.EVT_SLIDER,         self._on_fade)
        self._fade_slider.Bind(wx.EVT_SCROLL_CHANGED, self._on_fade_commit)
        self._fade_val_lbl = wx.StaticText(self, label="0.0 s", size=(42, -1))

        self._monitor_cb = wx.CheckBox(self, label=_("M&onitor this source"))
        self._monitor_cb.Bind(wx.EVT_CHECKBOX, self._on_monitor)

        check_row.Add(self._mute_cb,      0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        check_row.Add(_fade_lbl,          0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        check_row.Add(self._fade_slider,  0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        check_row.Add(self._fade_val_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        check_row.Add(self._monitor_cb,   0, wx.ALIGN_CENTER_VERTICAL)
        ctrl_sizer.Add(check_row, 0, wx.LEFT | wx.BOTTOM, 4)

        self._mute_to_stream_cb = wx.CheckBox(self, label=_("&Mute to stream (heard locally only)"))
        self._mute_to_stream_cb.Bind(wx.EVT_CHECKBOX, self._on_mute_to_stream)
        ctrl_sizer.Add(self._mute_to_stream_cb, 0, wx.LEFT | wx.BOTTOM, 4)

        sizer.Add(ctrl_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── monitor output device ─────────────────────────────────────────────
        mon_dev_row = wx.BoxSizer(wx.HORIZONTAL)
        mon_dev_row.Add(wx.StaticText(self, label=_("Monitor output:")),
                        0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._mon_dev_ch = wx.Choice(self, name="Monitor output device")
        self._mon_dev_ch.Bind(wx.EVT_CHOICE, self._on_mon_device)
        mon_dev_row.Add(self._mon_dev_ch, 1)
        sizer.Add(mon_dev_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._mon_dev_items: list[int | None] = []  # parallel to Choice items; None = system default
        self._populate_mon_devices()

        # ── monitor boost ─────────────────────────────────────────────────────
        mon_boost_row = wx.BoxSizer(wx.HORIZONTAL)
        _mon_boost_lbl = wx.StaticText(self, label=_("Monitor boost:"))   # must precede slider in z-order
        saved_db = int(self._config.monitor_gain_db)
        self._mon_boost_sl = wx.Slider(self, value=saved_db, minValue=0, maxValue=40,
                                       name="Monitor boost (dB)", style=wx.SL_HORIZONTAL)
        self._mon_boost_sl.Bind(wx.EVT_SLIDER,         self._on_mon_boost)
        self._mon_boost_sl.Bind(wx.EVT_SCROLL_CHANGED, self._on_mon_boost_commit)
        self._mon_boost_val_lbl = wx.StaticText(self, label=f"+{saved_db} dB", size=(48, -1))
        mon_boost_row.Add(_mon_boost_lbl,          0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        mon_boost_row.Add(self._mon_boost_sl,      1, wx.EXPAND)
        mon_boost_row.Add(self._mon_boost_val_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        sizer.Add(mon_boost_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._mixer.monitor_gain = 10 ** (saved_db / 20.0)

        # ── source status (error / peak) ─────────────────────────────────────
        self._status_label = wx.StaticText(self, label="", style=wx.ST_NO_AUTORESIZE)
        self._status_label.SetForegroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        sizer.Add(self._status_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── master VST ───────────────────────────────────────────────────────
        self._btn_master_vst = wx.Button(self, label=_("Edit &Master VST chain…"))
        self._btn_master_vst.Bind(wx.EVT_BUTTON, self._open_master_vst)
        sizer.Add(self._btn_master_vst, 0, wx.LEFT | wx.BOTTOM, 8)

        self.SetSizer(sizer)

        self._status_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_status_tick, self._status_timer)
        self._status_timer.Start(2000)

    # ── adding sources ───────────────────────────────────────────────────────

    def _add_source(self, _event=None):
        dlg = _AddSourceDialog(self, self._mixer.sample_rate,
                               self._mixer.channels, self._mixer.chunk_frames,
                               config=self._config, mastodon=self._mastodon)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return

        source_type = dlg.source_type

        if source_type == "mic":
            dev = dlg.selected_device()
            dlg.Destroy()
            if dev is None:
                return
            cap = DeviceCapture(device_index=dev["index"],
                                sample_rate=self._mixer.sample_rate,
                                channels=self._mixer.channels,
                                chunk_frames=self._mixer.chunk_frames)
            cap.start()
            _log(f"Add source: mic '{dev['name']}' device_index={dev['index']}")
            self._register(AudioSource(cap, name=dev["name"]))

        elif source_type == "loopback":
            dev = dlg.selected_loopback_device()
            dlg.Destroy()
            if dev is None:
                return
            cap = DeviceCapture(device_index=dev["index"],
                                sample_rate=dev["sample_rate"],
                                channels=dev["channels"],
                                chunk_frames=self._mixer.chunk_frames)
            cap._is_loopback = True
            try:
                cap.start()
            except Exception as e:
                wx.MessageBox(f"Could not open loopback device:\n{e}",
                              "Loopback Error", wx.OK | wx.ICON_WARNING, self)
                return
            _log(f"Add source: loopback '{dev['name']}' device_index={dev['index']}")
            self._register(AudioSource(cap, name=dev["name"]))

        elif source_type == "app":
            exe_names = dlg.checked_exe_names()
            dlg.Destroy()
            if not exe_names:
                wx.MessageBox(_("No applications were selected."), _("Add App"),
                              wx.OK | wx.ICON_INFORMATION, self)
                return
            for exe_name in exe_names:
                cap = WatchedAppCapture(exe_name=exe_name,
                                        sample_rate=self._mixer.sample_rate,
                                        channels=self._mixer.channels,
                                        chunk_frames=self._mixer.chunk_frames)
                cap.start()
                _log(f"Add source: watch '{exe_name}'")
                self._register(AudioSource(cap, name=exe_name))

        elif source_type == "file":
            path = dlg.selected_file_path()
            dlg.Destroy()
            if not path:
                return
            cap = FileCapture(path=path,
                              sample_rate=self._mixer.sample_rate,
                              channels=self._mixer.channels,
                              chunk_frames=self._mixer.chunk_frames)
            cap.start()
            name = os.path.basename(path)
            _log(f"Add source: file '{path}'")
            self._register(AudioSource(cap, name=name))

        elif source_type == "url":
            url = dlg.selected_audio_url()
            dlg.Destroy()
            if not url:
                return
            cap = UrlStreamCapture(url=url,
                                   sample_rate=self._mixer.sample_rate,
                                   channels=self._mixer.channels,
                                   chunk_frames=self._mixer.chunk_frames)
            cap.start()
            _log(f"Add source: url '{url}'")
            self._register(AudioSource(cap, name=url))

        elif source_type == "tts":
            tts_cfg = dlg.tts_config
            dlg.Destroy()
            eng_key = tts_cfg.get("engine", "sapi")
            engine  = make_engine(eng_key, tts_cfg.get("engine_config", {}))
            if not engine.is_available():
                wx.MessageBox(
                    f"The '{engine.name}' engine requires packages that are not installed.\n"
                    "Please install the required dependencies and try again.",
                    "Engine Unavailable", wx.OK | wx.ICON_WARNING, self)
                return
            fallback = _make_sapi_fallback() if eng_key != "sapi" else None
            cap = ChatTtsCapture(engine=engine,
                                 sample_rate=self._mixer.sample_rate,
                                 channels=self._mixer.channels,
                                 chunk_frames=self._mixer.chunk_frames,
                                 template=tts_cfg.get("tts_template", ""),
                                 fallback_engine=fallback)
            cap.start()
            if self._sse is not None:
                self._sse.add_chat_subscriber(cap._speak_ref)
            name = f"Chat TTS ({engine.name})"
            _log(f"Add source: Chat TTS engine={eng_key}")
            self._register(AudioSource(cap, name=name))

        elif source_type == "sounds":
            sc = dlg.sounds_config
            dlg.Destroy()
            cap = SoundEventCapture(
                pack=sc["pack"],
                enabled_events=set(sc["enabled_events"]),
                sample_rate=self._mixer.sample_rate,
                channels=self._mixer.channels,
                chunk_frames=self._mixer.chunk_frames,
                icecast_host=self._config.icecast_host,
                icecast_port=self._config.icecast_port,
                mountpoint=self._config.mountpoint,
            )
            cap.start()
            if self._sse is not None:
                self._sse.add_chat_subscriber(cap._chat_ref)
            mrc = self._find_capture(MastodonRepliesCapture)
            if mrc is not None:
                self._wire_mastodon_sound(cap, mrc)
            name = f"Sound Events ({sc['pack']})"
            _log(f"Add source: Sound Events pack={sc['pack']} events={sc['enabled_events']}")
            self._register(AudioSource(cap, name=name))

        elif source_type == "mastodon_replies":
            mr_cfg = dlg.mastodon_tts_config
            dlg.Destroy()
            eng_key_str = mr_cfg.get("engine", "sapi")
            engine      = make_engine(eng_key_str, mr_cfg.get("engine_config", {}))
            if not engine.is_available():
                wx.MessageBox(
                    f"The '{engine.name}' engine requires packages that are not installed.",
                    "Engine Unavailable", wx.OK | wx.ICON_WARNING, self)
                return
            fallback = _make_sapi_fallback() if eng_key_str != "sapi" else None
            cap = MastodonRepliesCapture(
                mastodon_client=self._mastodon,
                engine=engine,
                sample_rate=self._mixer.sample_rate,
                channels=self._mixer.channels,
                chunk_frames=self._mixer.chunk_frames,
                fallback_engine=fallback,
            )
            cap.start()
            sec = self._find_capture(SoundEventCapture)
            if sec is not None:
                self._wire_mastodon_sound(sec, cap)
            name = f"Mastodon Replies ({engine.name})"
            _log(f"Add source: Mastodon Replies engine={eng_key_str}")
            self._register(AudioSource(cap, name=name))

        else:
            dlg.Destroy()

    def _register(self, src: AudioSource, save: bool = True):
        self._sources.append(src)
        self._mixer.add_source(src)
        self._lb.Append(src.name)
        self._lb.SetSelection(self._lb.GetCount() - 1)
        self._refresh_controls()
        if save:
            self.save_all()

    # ── helpers ──────────────────────────────────────────────────────────────

    def fire_sound_event(self, event_key: str):
        """Trigger a sound event on every active SoundEventCapture. Thread-safe."""
        for src in self._sources:
            if isinstance(src.capture, SoundEventCapture):
                src.capture.trigger(event_key)

    def _find_capture(self, cap_type):
        for src in self._sources:
            if isinstance(src.capture, cap_type):
                return src.capture
        return None

    def _wire_mastodon_sound(self, sec: "SoundEventCapture",
                              mrc: "MastodonRepliesCapture"):
        """Connect a SoundEventCapture to a MastodonRepliesCapture if the event is enabled."""
        if "mastodon_reply" in sec.enabled_events:
            mrc.on_reply = sec._mastodon_reply_ref

    def _unwire_capture(self, cap):
        """Remove all external subscriptions for a capture before stopping/replacing it."""
        if isinstance(cap, ChatTtsCapture) and self._sse is not None:
            self._sse.remove_chat_subscriber(cap._speak_ref)
        if isinstance(cap, SoundEventCapture):
            if self._sse is not None:
                self._sse.remove_chat_subscriber(cap._chat_ref)
            mrc = self._find_capture(MastodonRepliesCapture)
            if mrc is not None and mrc.on_reply is cap._mastodon_reply_ref:
                mrc.on_reply = None
        if isinstance(cap, MastodonRepliesCapture):
            cap.on_reply = None

    def _wire_capture(self, cap):
        """Subscribe a newly-created capture to the appropriate external sources."""
        if isinstance(cap, ChatTtsCapture) and self._sse is not None:
            self._sse.add_chat_subscriber(cap._speak_ref)
        if isinstance(cap, SoundEventCapture):
            if self._sse is not None:
                self._sse.add_chat_subscriber(cap._chat_ref)
            mrc = self._find_capture(MastodonRepliesCapture)
            if mrc is not None:
                self._wire_mastodon_sound(cap, mrc)
        if isinstance(cap, MastodonRepliesCapture):
            sec = self._find_capture(SoundEventCapture)
            if sec is not None:
                self._wire_mastodon_sound(sec, cap)

    def _build_capture_from_dlg(self, dlg) -> "tuple":
        """
        Create a (capture, display_name) pair from a completed dialog without
        starting or wiring the capture.  Raises ValueError if the dialog
        selection is invalid.
        """
        source_type = dlg.source_type

        if source_type == "mic":
            dev = dlg.selected_device()
            if dev is None:
                raise ValueError("No device selected.")
            cap = DeviceCapture(device_index=dev["index"],
                                sample_rate=self._mixer.sample_rate,
                                channels=self._mixer.channels,
                                chunk_frames=self._mixer.chunk_frames)
            return cap, dev["name"]

        if source_type == "loopback":
            dev = dlg.selected_loopback_device()
            if dev is None:
                raise ValueError("No loopback device selected.")
            cap = DeviceCapture(device_index=dev["index"],
                                sample_rate=dev["sample_rate"],
                                channels=dev["channels"],
                                chunk_frames=self._mixer.chunk_frames)
            cap._is_loopback = True
            return cap, dev["name"]

        if source_type == "app":
            names = dlg.checked_exe_names()
            if not names:
                raise ValueError("No application selected.")
            exe_name = names[0]
            cap = WatchedAppCapture(exe_name=exe_name,
                                    sample_rate=self._mixer.sample_rate,
                                    channels=self._mixer.channels,
                                    chunk_frames=self._mixer.chunk_frames)
            return cap, exe_name

        if source_type == "file":
            path = dlg.selected_file_path()
            if not path:
                raise ValueError("No file selected.")
            cap = FileCapture(path=path,
                              sample_rate=self._mixer.sample_rate,
                              channels=self._mixer.channels,
                              chunk_frames=self._mixer.chunk_frames)
            return cap, os.path.basename(path)

        if source_type == "url":
            url = dlg.selected_audio_url()
            if not url:
                raise ValueError("No audio URL provided.")
            cap = UrlStreamCapture(url=url,
                                   sample_rate=self._mixer.sample_rate,
                                   channels=self._mixer.channels,
                                   chunk_frames=self._mixer.chunk_frames)
            return cap, url

        if source_type == "tts":
            tts_cfg  = dlg.tts_config
            eng_key  = tts_cfg.get("engine", "sapi")
            engine   = make_engine(eng_key, tts_cfg.get("engine_config", {}))
            if not engine.is_available():
                raise ValueError(
                    f"The '{engine.name}' engine requires packages that are not installed.")
            fallback = _make_sapi_fallback() if eng_key != "sapi" else None
            cap = ChatTtsCapture(engine=engine,
                                 sample_rate=self._mixer.sample_rate,
                                 channels=self._mixer.channels,
                                 chunk_frames=self._mixer.chunk_frames,
                                 template=tts_cfg.get("tts_template", ""),
                                 fallback_engine=fallback)
            return cap, f"Chat TTS ({engine.name})"

        if source_type == "sounds":
            sc  = dlg.sounds_config
            cap = SoundEventCapture(
                pack=sc["pack"],
                enabled_events=set(sc["enabled_events"]),
                sample_rate=self._mixer.sample_rate,
                channels=self._mixer.channels,
                chunk_frames=self._mixer.chunk_frames,
                icecast_host=self._config.icecast_host,
                icecast_port=self._config.icecast_port,
                mountpoint=self._config.mountpoint,
            )
            return cap, f"Sound Events ({sc['pack']})"

        if source_type == "mastodon_replies":
            mr_cfg      = dlg.mastodon_tts_config
            eng_key_str = mr_cfg.get("engine", "sapi")
            engine      = make_engine(eng_key_str, mr_cfg.get("engine_config", {}))
            if not engine.is_available():
                raise ValueError(
                    f"The '{engine.name}' engine requires packages that are not installed.")
            fallback = _make_sapi_fallback() if eng_key_str != "sapi" else None
            cap = MastodonRepliesCapture(
                mastodon_client=self._mastodon,
                engine=engine,
                sample_rate=self._mixer.sample_rate,
                channels=self._mixer.channels,
                chunk_frames=self._mixer.chunk_frames,
                fallback_engine=fallback,
            )
            return cap, f"Mastodon Replies ({engine.name})"

        raise ValueError(f"Unknown source type: {source_type!r}")

    # ── editing ──────────────────────────────────────────────────────────────

    def _source_entry(self, src: "AudioSource") -> dict:
        """Serialize one source to an entry dict (same format as save_all)."""
        entry: dict = {
            "name":           src.name,
            "gain":           src.gain,
            "muted":          src.muted,
            "mute_to_stream": src.mute_to_stream,
            "monitored":      src.monitored,
            "fade_duration":  src.fade_duration,
            "vst":            src.vst.to_dict(),
        }
        cap = src.capture
        if isinstance(cap, DeviceCapture):
            is_lb = getattr(cap, "_is_loopback", False)
            entry["type"]         = "loopback" if is_lb else "mic"
            entry["device_index"] = cap.device_index
            if is_lb:
                entry["sample_rate"] = cap.sample_rate
                entry["channels"]    = cap.channels
        elif isinstance(cap, WatchedAppCapture):
            entry["type"]     = "app_watch"
            entry["exe_name"] = cap.exe_name
        elif isinstance(cap, FileCapture):
            entry["type"] = "file"
            entry["path"] = cap.path
        elif isinstance(cap, UrlStreamCapture):
            entry["type"] = "url"
            entry["url"] = cap.url
        elif isinstance(cap, ChatTtsCapture):
            entry["type"]          = "tts"
            entry["engine"]        = engine_key(cap._engine.name)
            entry["engine_config"] = cap._engine.get_config()
            entry["tts_template"]  = cap.template
        elif isinstance(cap, SoundEventCapture):
            entry["type"]           = "sounds"
            entry["pack"]           = cap.pack
            entry["enabled_events"] = list(cap.enabled_events)
        elif isinstance(cap, MastodonRepliesCapture):
            entry["type"]          = "mastodon_replies"
            entry["engine"]        = engine_key(cap._engine.name)
            entry["engine_config"] = cap._engine.get_config()
        else:
            entry["type"] = "app_include"
        return entry

    def _edit(self, _event=None):
        idx = self._lb.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        src   = self._sources[idx]
        entry = self._source_entry(src)

        dlg = _AddSourceDialog(self, self._mixer.sample_rate,
                               self._mixer.channels, self._mixer.chunk_frames,
                               config=self._config, mastodon=self._mastodon,
                               edit_mode=True)
        dlg.load_initial_state(entry)

        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return

        try:
            new_cap, new_name = self._build_capture_from_dlg(dlg)
        except ValueError as exc:
            dlg.Destroy()
            wx.MessageBox(str(exc), "Edit Source", wx.OK | wx.ICON_ERROR, self)
            return
        dlg.Destroy()

        old_cap = src.capture
        self._unwire_capture(old_cap)
        self._mixer.remove_source(src)

        src.capture = new_cap
        src.name    = new_name

        self._mixer.add_source(src)
        self._wire_capture(new_cap)

        try:
            new_cap.start()
        except Exception as e:
            self._mixer.remove_source(src)
            src.capture = old_cap
            src.name    = entry["name"]
            self._mixer.add_source(src)
            self._wire_capture(old_cap)
            old_cap.start()
            wx.MessageBox(f"Could not start source:\n{e}",
                          "Edit Source", wx.OK | wx.ICON_WARNING, self)
            return

        self._lb.SetString(idx, new_name)
        self.save_all()
        _log(f"Edit source: '{entry['name']}' -> '{new_name}'")

        threading.Thread(target=lambda c=old_cap: c.stop(),
                         daemon=True, name="src-edit-stop").start()

    # ── removing ─────────────────────────────────────────────────────────────

    def _remove(self, _event=None):
        idx = self._lb.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        src = self._sources.pop(idx)
        _log(f"Remove source: '{src.name}'")
        self._mixer.remove_source(src)
        if isinstance(src.capture, ChatTtsCapture) and self._sse is not None:
            self._sse.remove_chat_subscriber(src.capture._speak_ref)
        if isinstance(src.capture, SoundEventCapture) and self._sse is not None:
            self._sse.remove_chat_subscriber(src.capture._chat_ref)
        if isinstance(src.capture, SoundEventCapture):
            mrc = self._find_capture(MastodonRepliesCapture)
            if mrc is not None and mrc.on_reply is src.capture._mastodon_reply_ref:
                mrc.on_reply = None
        if isinstance(src.capture, MastodonRepliesCapture):
            src.capture.on_reply = None
        self._lb.Delete(idx)
        self.save_all()
        threading.Thread(target=self._stop_capture_bg, args=(src,),
                         daemon=True, name="src-stop").start()

    def _stop_capture_bg(self, src: "AudioSource"):
        try:
            src.capture.stop()
        except Exception as e:
            _log(f"[stop] '{src.name}' error: {e}")

    # ── controls ─────────────────────────────────────────────────────────────

    def _on_select(self, _event=None):
        self._refresh_controls()
        self._refresh_status()

    _peak_log_counter: int = 0

    def _on_status_tick(self, _event=None):
        self._refresh_status()
        self._check_new_errors()
        self._peak_log_counter += 1
        if self._peak_log_counter >= 5:   # every 10 s (timer fires every 2 s)
            self._peak_log_counter = 0
            self._log_all_peaks()

    def _log_all_peaks(self):
        for src in self._sources:
            p = src.peak
            db = f"{20 * math.log10(p):+.1f} dBFS" if p > 0 else "-inf dBFS (silence)"
            err = getattr(src.capture, "error", None)
            state = f"ERROR: {err}" if err else db
            _log(f"[peak] '{src.name}': {state}")

    def _refresh_status(self):
        src = self._selected()
        if not src:
            self._status_label.SetLabel("")
            return
        err = getattr(src.capture, "error", None)
        if err:
            self._status_label.SetForegroundColour(wx.Colour(180, 30, 30))
            self._status_label.SetLabel(f"Error: {err}")
            _log(f"[status] '{src.name}' error: {err}")
        else:
            peak = src.peak
            if peak > 0:
                db = 20 * math.log10(peak)
                label = f"Peak: {db:+.1f} dBFS"
            else:
                label = "Peak: silence"
            self._status_label.SetForegroundColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            self._status_label.SetLabel(label)

    def _check_new_errors(self):
        """Log any newly-failed PLC sources (all sources, not just selected)."""
        for src in self._sources:
            err = getattr(src.capture, "error", None)
            if err and not getattr(src, "_logged_error", False):
                src._logged_error = True
                _log(f"[ERROR] source '{src.name}' failed: {err}")

    def _refresh_controls(self):
        src = self._selected()
        has_sel = src is not None
        self._btn_edit.Enable(has_sel)
        self._btn_remove.Enable(has_sel)
        if src:
            vol = int(src.gain * 100)
            self._vol_slider.SetValue(vol)
            self._vol_val_lbl.SetLabel(f"{vol}%")
            self._mute_cb.SetValue(src.muted)
            fade_int = min(100, max(0, round(src.fade_duration * 10)))
            self._fade_slider.SetValue(fade_int)
            self._fade_val_lbl.SetLabel(f"{src.fade_duration:.1f} s")
            self._monitor_cb.SetValue(src.monitored)
            self._mute_to_stream_cb.SetValue(src.mute_to_stream)

    def _selected(self) -> AudioSource | None:
        idx = self._lb.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(self._sources):
            return None
        return self._sources[idx]

    def _on_volume(self, _event=None):
        val = self._vol_slider.GetValue()
        self._vol_val_lbl.SetLabel(f"{val}%")
        src = self._selected()
        if src:
            src.gain = val / 100.0

    def _on_volume_commit(self, _event=None):
        src = self._selected()
        if src:
            _log(f"Volume: '{src.name}' -> {int(src.gain * 100)}%")
        self.save_all()

    def _on_mute(self, _event=None):
        src = self._selected()
        if src:
            src.muted = self._mute_cb.GetValue()
            _log(f"Mute: '{src.name}' -> {src.muted} (fade={src.fade_duration:.1f}s)")
            self.save_all()

    def _on_fade(self, _event=None):
        val = self._fade_slider.GetValue() / 10.0
        self._fade_val_lbl.SetLabel(f"{val:.1f} s")
        src = self._selected()
        if src:
            src.fade_duration = val

    def _on_fade_commit(self, _event=None):
        self.save_all()

    def _on_monitor(self, _event=None):
        src = self._selected()
        if not src:
            return
        src.monitored = self._monitor_cb.GetValue()
        _log(f"Monitor: '{src.name}' -> {src.monitored}")
        if src.monitored:
            if not self._mixer.monitor_active:
                # Read device index here on the main thread — wx widgets are not
                # thread-safe and GetSelection() must never be called from a
                # background thread.
                sel = self._mon_dev_ch.GetSelection()
                dev_idx = self._mon_dev_items[sel] if sel != wx.NOT_FOUND else None
                threading.Thread(target=self._start_monitor_bg, args=(src, dev_idx),
                                 daemon=True, name="monitor-start").start()
        else:
            if not any(s.monitored for s in self._sources):
                self._mixer.stop_monitor()
        self.save_all()

    def _on_mute_to_stream(self, _event=None):
        src = self._selected()
        if src:
            src.mute_to_stream = self._mute_to_stream_cb.GetValue()
            _log(f"Mute-to-stream: '{src.name}' -> {src.mute_to_stream}")
            self.save_all()

    # ── VST dialogs ──────────────────────────────────────────────────────────

    def _open_vst(self, _event=None):
        src = self._selected()
        if not src:
            wx.MessageBox(_("Select a source first."), _("VST"), wx.OK | wx.ICON_INFORMATION, self)
            return
        _log(f"VST dialog opened: '{src.name}'")
        VstPanelDialog(self, src.vst, title=f"VST — {src.name}").ShowModal()
        _log(f"VST dialog closed: '{src.name}' plugins={src.vst.plugin_names()}")
        self.save_all()

    def _open_master_vst(self, _event=None):
        _log("Master VST dialog opened")
        VstPanelDialog(self, self._mixer.master_vst, title="Master VST Chain").ShowModal()
        _log(f"Master VST dialog closed plugins={self._mixer.master_vst.plugin_names()}")
        self.save_all()

    # ── persistence ──────────────────────────────────────────────────────────

    def save_all(self):
        """Serialize all sources and master VST chain to config and flush to disk."""
        data = []
        for src in self._sources:
            entry: dict = {
                "name":            src.name,
                "gain":            src.gain,
                "muted":           src.muted,
                "mute_to_stream":  src.mute_to_stream,
                "monitored":       src.monitored,
                "fade_duration":   src.fade_duration,
                "vst":             src.vst.to_dict(),
            }
            cap = src.capture
            if isinstance(cap, DeviceCapture):
                is_lb = getattr(cap, "_is_loopback", False)
                entry["type"]         = "loopback" if is_lb else "mic"
                entry["device_index"] = cap.device_index
                if is_lb:
                    entry["sample_rate"] = cap.sample_rate
                    entry["channels"]    = cap.channels
            elif isinstance(cap, WatchedAppCapture):
                entry["type"]     = "app_watch"
                entry["exe_name"] = cap.exe_name
            elif isinstance(cap, FileCapture):
                entry["type"] = "file"
                entry["path"] = cap.path
            elif isinstance(cap, UrlStreamCapture):
                entry["type"] = "url"
                entry["url"] = cap.url
            elif isinstance(cap, ChatTtsCapture):
                entry["type"]          = "tts"
                entry["engine"]        = engine_key(cap._engine.name)
                entry["engine_config"] = cap._engine.get_config()
                entry["tts_template"]  = cap.template
            elif isinstance(cap, SoundEventCapture):
                entry["type"]           = "sounds"
                entry["pack"]           = cap.pack
                entry["enabled_events"] = list(cap.enabled_events)
            elif isinstance(cap, MastodonRepliesCapture):
                entry["type"]          = "mastodon_replies"
                entry["engine"]        = engine_key(cap._engine.name)
                entry["engine_config"] = cap._engine.get_config()
            else:
                entry["type"]         = "app_include"
                entry["process_name"] = getattr(cap, "_proc_name",
                                                 src.name.rsplit(" [", 1)[0])
                entry["process_pid"]  = cap.pid
            data.append(entry)
        self._config.sources          = data
        self._config.master_vst_chain = self._mixer.master_vst.to_dict()
        self._config.save()

    def _restore_all(self):
        """Re-create sources from saved config on startup."""
        _log(f"Restore: loading {len(self._config.sources)} saved source(s)")
        for entry in self._config.sources:
            try:
                t = entry.get("type")
                if t == "mic":
                    self._restore_mic(entry)
                elif t == "loopback":
                    self._restore_loopback(entry)
                elif t == "app_watch":
                    self._restore_app_watch(entry)
                elif t == "file":
                    self._restore_file(entry)
                elif t == "url":
                    self._restore_url(entry)
                elif t == "tts":
                    self._restore_tts(entry)
                elif t == "sounds":
                    self._restore_sounds(entry)
                elif t == "mastodon_replies":
                    self._restore_mastodon_replies(entry)
            except Exception:
                pass
        try:
            master_data = self._config.master_vst_chain
            if master_data:
                self._mixer.master_vst.from_dict(master_data)
        except Exception:
            pass
        _log(f"Restore: done - {len(self._sources)} source(s) active")
        if any(s.monitored for s in self._sources):
            _log("Restore: starting monitor in background")
            sel = self._mon_dev_ch.GetSelection()
            dev_idx = self._mon_dev_items[sel] if sel != wx.NOT_FOUND else None
            threading.Thread(target=self._start_monitor_bg, args=(None, dev_idx),
                             daemon=True, name="monitor-start").start()

    def _populate_mon_devices(self):
        """Fill the monitor output Choice with available output devices."""
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        items  = [_("System default")]
        indices: list[int | None] = [None]
        try:
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d["maxOutputChannels"] > 0:
                    items.append(d["name"])
                    indices.append(i)
        finally:
            pa.terminate()
        self._mon_dev_ch.Set(items)
        self._mon_dev_items = indices
        saved = self._config.monitor_device_index
        sel = 0
        if saved is not None:
            try:
                sel = indices.index(saved)
            except ValueError:
                pass
        self._mon_dev_ch.SetSelection(sel)

    def _on_mon_device(self, _event=None):
        sel = self._mon_dev_ch.GetSelection()
        if sel == wx.NOT_FOUND:
            return
        idx = self._mon_dev_items[sel]
        self._config.monitor_device_index = idx
        self._config.save()
        if self._mixer.monitor_active:
            # Pass idx now (main thread) so _restart_monitor_bg never touches widgets.
            threading.Thread(target=self._restart_monitor_bg, args=(idx,),
                             daemon=True, name="monitor-restart").start()

    def _on_mon_boost(self, _event=None):
        db = self._mon_boost_sl.GetValue()
        self._mon_boost_val_lbl.SetLabel(f"+{db} dB")
        self._mixer.monitor_gain = 10 ** (db / 20.0)

    def _on_mon_boost_commit(self, _event=None):
        db = self._mon_boost_sl.GetValue()
        _log(f"Monitor boost: +{db} dB")
        self._config.monitor_gain_db = float(db)
        self._config.save()

    def _restart_monitor_bg(self, dev_idx: "int | None" = None):
        self._mixer.stop_monitor()
        self._start_monitor_bg(dev_idx=dev_idx)

    def _start_monitor_bg(self, src: "AudioSource | None" = None,
                          dev_idx: "int | None" = None):
        try:
            self._mixer.start_monitor(device_index=dev_idx)
        except Exception as e:
            if src is not None:
                wx.CallAfter(self._on_monitor_failed, src, str(e))
            else:
                wx.CallAfter(self._clear_monitor_flags)

    def _on_monitor_failed(self, src: "AudioSource", err: str):
        wx.MessageBox(f"Could not open monitor output:\n{err}",
                      "Monitor Error", wx.OK | wx.ICON_WARNING, self)
        src.monitored = False
        self._monitor_cb.SetValue(False)
        self.save_all()

    def _clear_monitor_flags(self):
        for s in self._sources:
            s.monitored = False

    def _restore_mic(self, entry: dict):
        saved_idx = entry.get("device_index")
        if saved_idx is None:
            return
        _log(f"Restore: mic '{entry.get('name')}' device_index={saved_idx}")
        cap = DeviceCapture(device_index=saved_idx,
                            sample_rate=self._mixer.sample_rate,
                            channels=self._mixer.channels,
                            chunk_frames=self._mixer.chunk_frames)
        cap.start()
        src = AudioSource(cap, name=entry.get("name", f"Device {saved_idx}"))
        src.gain            = float(entry.get("gain", 1.0))
        src.muted           = bool(entry.get("muted", False))
        src.mute_to_stream  = bool(entry.get("mute_to_stream", False))
        src.monitored       = bool(entry.get("monitored", False))
        src.fade_duration   = float(entry.get("fade_duration", 0.0))
        src._gain_factor    = 0.0 if src.muted else 1.0
        src.vst.from_dict(entry.get("vst", []))
        self._register(src, save=False)

    def _restore_loopback(self, entry: dict):
        saved_idx = entry.get("device_index")
        if saved_idx is None:
            return
        sr  = int(entry.get("sample_rate", self._mixer.sample_rate))
        ch  = int(entry.get("channels",    self._mixer.channels))
        _log(f"Restore: loopback '{entry.get('name')}' device_index={saved_idx}")
        cap = DeviceCapture(device_index=saved_idx, sample_rate=sr,
                            channels=ch, chunk_frames=self._mixer.chunk_frames)
        cap._is_loopback = True
        try:
            cap.start()
        except Exception as e:
            _log(f"Restore: loopback '{entry.get('name')}' start failed: {e}")
            return
        src = AudioSource(cap, name=entry.get("name", f"Loopback {saved_idx}"))
        src.gain            = float(entry.get("gain", 1.0))
        src.muted           = bool(entry.get("muted", False))
        src.mute_to_stream  = bool(entry.get("mute_to_stream", False))
        src.monitored       = bool(entry.get("monitored", False))
        src.fade_duration   = float(entry.get("fade_duration", 0.0))
        src._gain_factor    = 0.0 if src.muted else 1.0
        src.vst.from_dict(entry.get("vst", []))
        self._register(src, save=False)

    def _restore_app_watch(self, entry: dict):
        exe_name = entry.get("exe_name", "")
        if not exe_name:
            return
        _log(f"Restore: app_watch '{exe_name}'")
        cap = WatchedAppCapture(exe_name=exe_name,
                                sample_rate=self._mixer.sample_rate,
                                channels=self._mixer.channels,
                                chunk_frames=self._mixer.chunk_frames)
        cap.start()
        src = AudioSource(cap, name=exe_name)
        src.gain            = float(entry.get("gain", 1.0))
        src.muted           = bool(entry.get("muted", False))
        src.mute_to_stream  = bool(entry.get("mute_to_stream", False))
        src.monitored       = bool(entry.get("monitored", False))
        src.fade_duration   = float(entry.get("fade_duration", 0.0))
        src._gain_factor    = 0.0 if src.muted else 1.0
        src.vst.from_dict(entry.get("vst", []))
        self._register(src, save=False)

    def _restore_file(self, entry: dict):
        path = entry.get("path", "")
        if not path or not os.path.exists(path):
            _log(f"Restore: file '{path}' not found, skipping")
            return
        _log(f"Restore: file '{path}'")
        cap = FileCapture(path=path,
                          sample_rate=self._mixer.sample_rate,
                          channels=self._mixer.channels,
                          chunk_frames=self._mixer.chunk_frames)
        cap.start()
        src = AudioSource(cap, name=entry.get("name", os.path.basename(path)))
        src.gain            = float(entry.get("gain", 1.0))
        src.muted           = bool(entry.get("muted", False))
        src.mute_to_stream  = bool(entry.get("mute_to_stream", False))
        src.monitored       = bool(entry.get("monitored", False))
        src.fade_duration   = float(entry.get("fade_duration", 0.0))
        src._gain_factor    = 0.0 if src.muted else 1.0
        src.vst.from_dict(entry.get("vst", []))
        self._register(src, save=False)

    def _restore_url(self, entry: dict):
        url = entry.get("url", "").strip()
        if not url:
            return
        _log(f"Restore: url '{url}'")
        cap = UrlStreamCapture(url=url,
                               sample_rate=self._mixer.sample_rate,
                               channels=self._mixer.channels,
                               chunk_frames=self._mixer.chunk_frames)
        cap.start()
        src = AudioSource(cap, name=entry.get("name", url))
        src.gain            = float(entry.get("gain", 1.0))
        src.muted           = bool(entry.get("muted", False))
        src.mute_to_stream  = bool(entry.get("mute_to_stream", False))
        src.monitored       = bool(entry.get("monitored", False))
        src.fade_duration   = float(entry.get("fade_duration", 0.0))
        src._gain_factor    = 0.0 if src.muted else 1.0
        src.vst.from_dict(entry.get("vst", []))
        self._register(src, save=False)

    def _restore_tts(self, entry: dict):
        eng_key = entry.get("engine", "sapi")
        engine  = make_engine(eng_key, entry.get("engine_config", {}))
        if not engine.is_available():
            _log(f"Restore: TTS engine '{eng_key}' unavailable, skipping")
            return
        _log(f"Restore: Chat TTS engine={eng_key}")
        fallback = _make_sapi_fallback() if eng_key != "sapi" else None
        cap = ChatTtsCapture(engine=engine,
                             sample_rate=self._mixer.sample_rate,
                             channels=self._mixer.channels,
                             chunk_frames=self._mixer.chunk_frames,
                             template=entry.get("tts_template", ""),
                             fallback_engine=fallback)
        cap.start()
        if self._sse is not None:
            self._sse.add_chat_subscriber(cap._speak_ref)
        src = AudioSource(cap, name=entry.get("name", f"Chat TTS ({engine.name})"))
        src.gain            = float(entry.get("gain",            1.0))
        src.muted           = bool(entry.get("muted",            False))
        src.mute_to_stream  = bool(entry.get("mute_to_stream",   False))
        src.monitored       = bool(entry.get("monitored",        False))
        src.fade_duration   = float(entry.get("fade_duration",   0.0))
        src._gain_factor    = 0.0 if src.muted else 1.0
        src.vst.from_dict(entry.get("vst", []))
        self._register(src, save=False)

    def _restore_sounds(self, entry: dict):
        pack = entry.get("pack", "default")
        _log(f"Restore: Sound Events pack={pack}")
        cap = SoundEventCapture(
            pack=pack,
            enabled_events=set(entry.get("enabled_events", [])),
            sample_rate=self._mixer.sample_rate,
            channels=self._mixer.channels,
            chunk_frames=self._mixer.chunk_frames,
            icecast_host=self._config.icecast_host,
            icecast_port=self._config.icecast_port,
            mountpoint=self._config.mountpoint,
        )
        cap.start()
        if self._sse is not None:
            self._sse.add_chat_subscriber(cap._chat_ref)
        mrc = self._find_capture(MastodonRepliesCapture)
        if mrc is not None:
            self._wire_mastodon_sound(cap, mrc)
        src = AudioSource(cap, name=entry.get("name", f"Sound Events ({pack})"))
        src.gain            = float(entry.get("gain",           1.0))
        src.muted           = bool(entry.get("muted",           False))
        src.mute_to_stream  = bool(entry.get("mute_to_stream",  False))
        src.monitored       = bool(entry.get("monitored",       False))
        src.fade_duration   = float(entry.get("fade_duration",  0.0))
        src._gain_factor    = 0.0 if src.muted else 1.0
        src.vst.from_dict(entry.get("vst", []))
        self._register(src, save=False)

    def _restore_mastodon_replies(self, entry: dict):
        eng_key_str = entry.get("engine", "sapi")
        engine      = make_engine(eng_key_str, entry.get("engine_config", {}))
        if not engine.is_available():
            _log(f"Restore: Mastodon Replies engine '{eng_key_str}' unavailable, skipping")
            return
        fallback = _make_sapi_fallback() if eng_key_str != "sapi" else None
        _log(f"Restore: Mastodon Replies engine={eng_key_str}")
        cap = MastodonRepliesCapture(
            mastodon_client=self._mastodon,
            engine=engine,
            sample_rate=self._mixer.sample_rate,
            channels=self._mixer.channels,
            chunk_frames=self._mixer.chunk_frames,
            fallback_engine=fallback,
        )
        cap.start()
        sec = self._find_capture(SoundEventCapture)
        if sec is not None:
            self._wire_mastodon_sound(sec, cap)
        src = AudioSource(cap, name=entry.get("name", f"Mastodon Replies ({engine.name})"))
        src.gain            = float(entry.get("gain",           1.0))
        src.muted           = bool(entry.get("muted",           False))
        src.mute_to_stream  = bool(entry.get("mute_to_stream",  False))
        src.monitored       = bool(entry.get("monitored",       False))
        src.fade_duration   = float(entry.get("fade_duration",  0.0))
        src._gain_factor    = 0.0 if src.muted else 1.0
        src.vst.from_dict(entry.get("vst", []))
        self._register(src, save=False)

    # ── speech control ───────────────────────────────────────────────────────

    def flush_all_speech(self):
        """Silence all TTS and sound-event sources immediately, clearing queues."""
        for src in self._sources:
            if hasattr(src.capture, "flush"):
                try:
                    src.capture.flush()
                except Exception:
                    pass

    # ── shutdown ─────────────────────────────────────────────────────────────

    def stop_all(self):
        self._status_timer.Stop()
        self.save_all()
        for src in self._sources:
            try:
                src.capture.stop()
            except Exception:
                pass


# ── _AddSourceDialog ──────────────────────────────────────────────────────────

# Canonical source type labels (msgids). Translated at runtime inside _build().
_SOURCE_TYPE_IDS = ["Microphone", "Application", "Device Loopback", "Audio File",
                    "Audio URL", "Chat TTS", "Sound Events", "Mastodon Replies"]
_TYPE_MIC      = 0
_TYPE_APP      = 1
_TYPE_LOOPBACK = 2
_TYPE_FILE     = 3
_TYPE_URL      = 4
_TYPE_TTS      = 5
_TYPE_SOUNDS   = 6
_TYPE_MASTODON = 7


def _source_type_labels() -> list[str]:
    return [_(_id) for _id in _SOURCE_TYPE_IDS]


class _AddSourceDialog(wx.Dialog):
    """
    Unified 'Add Source' dialog.

    A combo-box at the top selects the source type; the content area below
    swaps to show the relevant controls via wx.Simplebook.
    """

    def __init__(self, parent, sample_rate: int, channels: int, chunk_frames: int,
                 config=None, mastodon=None, edit_mode: bool = False):
        title = _("Edit Source") if edit_mode else _("Add Source")
        super().__init__(parent, title=title, size=(480, 540),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._edit_mode = edit_mode
        self._sample_rate  = sample_rate
        self._channels     = channels
        self._chunk_frames = chunk_frames
        self._config       = config
        self._mastodon     = mastodon
        self._schema_widgets: dict = {}

        # Fetch data up-front so the dialog doesn't stall on show.
        self._devices       = list_wasapi_devices()
        self._loopback_devs = list_loopback_devices()

        # Enumerate current audio sessions for the app page list.
        _log("[AddSourceDialog] fetching audio sessions")
        self._procs = _offthread_sessions()
        _log(f"[AddSourceDialog] {len(self._procs)} sessions found")

        self._build()
        self.Centre()
        wx.CallAfter(self._type_combo.SetFocus)

    def _build(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── type selector ─────────────────────────────────────────────────────
        type_row = wx.BoxSizer(wx.HORIZONTAL)
        type_row.Add(wx.StaticText(self, label=_("Source type:")),
                     0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        _st_labels = _source_type_labels()
        self._type_combo = wx.ComboBox(self, value=_st_labels[0],
                                       choices=_st_labels,
                                       style=wx.CB_READONLY, name="Source type")
        self._type_combo.Bind(wx.EVT_COMBOBOX, self._on_type)
        type_row.Add(self._type_combo, 1)
        outer.Add(type_row, 0, wx.EXPAND | wx.ALL, 10)

        # ── swappable content area ────────────────────────────────────────────
        self._book = wx.Simplebook(self)
        self._book.AddPage(self._build_mic_page(),      "Microphone")
        self._book.AddPage(self._build_app_page(),      "Application")
        self._book.AddPage(self._build_loopback_page(), "Device Loopback")
        self._book.AddPage(self._build_file_page(),     "Audio File")
        self._book.AddPage(self._build_url_page(),      "Audio URL")
        self._book.AddPage(self._build_tts_page(),      "Chat TTS")
        self._book.AddPage(self._build_sounds_page(),    "Sound Events")
        self._book.AddPage(self._build_mastodon_page(),  "Mastodon Replies")
        # Disable every page except the first so their controls are excluded from
        # the tab order.  wx.Simplebook hides them visually but on Windows hidden
        # pages remain focusable via keyboard without this.
        for i in range(1, self._book.GetPageCount()):
            self._book.GetPage(i).Disable()
        outer.Add(self._book, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # ── standard buttons ──────────────────────────────────────────────────
        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(self, wx.ID_OK, label=_("&Save") if self._edit_mode else _("&Add"))
        ok_btn.SetDefault()
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(wx.Button(self, wx.ID_CANCEL))
        btn_sizer.Realize()
        outer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)

        self.SetSizer(outer)

    # ── page builders ─────────────────────────────────────────────────────────

    def _build_mic_page(self) -> wx.Panel:
        page = wx.Panel(self._book)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(page, label=_("Select a microphone / input device:")),
                  0, wx.BOTTOM, 4)
        choices = [f"{d['name']}  (ch: {d['channels']})" for d in self._devices]
        self._mic_lb = wx.ListBox(page, choices=choices, style=wx.LB_SINGLE,
                                  name="Microphone")
        if choices:
            self._mic_lb.SetSelection(0)
        sizer.Add(self._mic_lb, 1, wx.EXPAND)

        page.SetSizer(sizer)
        return page

    def _build_app_page(self) -> wx.Panel:
        page = wx.Panel(self._book)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(page, label=_("Select applications to watch and capture:")),
                  0, wx.BOTTOM, 4)

        hint = wx.StaticText(
            page,
            label=_("Checked apps are watched by name. Capture starts automatically "
                    "when the app plays audio, even if launched after this is added. "
                    "Use Browse to add an app not currently running."))
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        hint.Wrap(440)
        sizer.Add(hint, 0, wx.BOTTOM, 6)

        # Show exe names (no PID) — WatchedAppCapture matches by name, not PID
        seen: set[str] = set()
        choices: list[str] = []
        for p in self._procs:
            name_lower = p["name"].lower()
            if name_lower not in seen:
                seen.add(name_lower)
                choices.append(p["name"])
        if not choices:
            choices = ["(no active audio sessions found)"]
        self._app_clb = AccessibleCheckListBox(page, choices=choices,
                                               style=wx.LB_SORT, name="Processes")
        sizer.Add(self._app_clb, 1, wx.EXPAND)

        self._btn_browse_exe = wx.Button(page, label=_("&Browse for .exe…"))
        self._btn_browse_exe.Bind(wx.EVT_BUTTON, self._on_browse_exe)
        sizer.Add(self._btn_browse_exe, 0, wx.TOP, 6)

        page.SetSizer(sizer)
        return page

    def _build_loopback_page(self) -> wx.Panel:
        page = wx.Panel(self._book)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(page,
                  label=_("Select a render-device loopback (captures all audio from that output):")),
                  0, wx.BOTTOM, 4)
        choices = [f"{d['name']}  (ch: {d['channels']}  {d['sample_rate']} Hz)"
                   for d in self._loopback_devs]
        self._lb_loopback = wx.ListBox(page, choices=choices, style=wx.LB_SINGLE,
                                       name="Loopback device")
        if choices:
            self._lb_loopback.SetSelection(0)
        sizer.Add(self._lb_loopback, 1, wx.EXPAND)

        note = wx.StaticText(page,
               label=_("Note: some devices may not support loopback and will fail on Add."))
        note.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        sizer.Add(note, 0, wx.TOP, 6)

        page.SetSizer(sizer)
        return page

    def _build_file_page(self) -> wx.Panel:
        page = wx.Panel(self._book)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(page, label=_("Select an audio file to play on loop:")),
                  0, wx.BOTTOM, 4)

        path_row = wx.BoxSizer(wx.HORIZONTAL)
        self._file_path_txt = wx.TextCtrl(page, value="", style=wx.TE_READONLY,
                                           name="File path")
        self._btn_browse_file = wx.Button(page, label=_("&Browse…"))
        self._btn_browse_file.Bind(wx.EVT_BUTTON, self._on_browse_file)
        path_row.Add(self._file_path_txt, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        path_row.Add(self._btn_browse_file, 0)
        sizer.Add(path_row, 0, wx.EXPAND | wx.BOTTOM, 6)

        note = wx.StaticText(
            page,
            label=_("Supported formats: WAV, MP3, FLAC, OGG, AIFF, and others. "
                    "The file will be resampled to the mixer rate automatically."))
        note.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        note.Wrap(440)
        sizer.Add(note, 0)

        page.SetSizer(sizer)
        return page

    def _build_url_page(self) -> wx.Panel:
        page = wx.Panel(self._book)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(page, label=_("Enter an audio stream URL:")),
                  0, wx.BOTTOM, 4)

        self._url_txt = wx.TextCtrl(page, value="http://", name="Audio URL")
        sizer.Add(self._url_txt, 0, wx.EXPAND | wx.BOTTOM, 6)

        note = wx.StaticText(
            page,
            label=_("Use a direct stream URL such as Icecast or another HTTP audio "
                    "stream that ffmpeg can decode. The stream is decoded live and "
                    "mixed like any other source."))
        note.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        note.Wrap(440)
        sizer.Add(note, 0)

        page.SetSizer(sizer)
        return page

    def _build_tts_page(self) -> wx.Panel:
        page  = wx.Panel(self._book)
        sizer = wx.BoxSizer(wx.VERTICAL)

        hint = wx.StaticText(
            page,
            label=_("Receives Audio Pub chat messages and synthesises them with the "
                    "chosen TTS engine.  The audio output can be monitored, routed "
                    "through VST effects, and sent to the stream."))
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        hint.Wrap(440)
        sizer.Add(hint, 0, wx.BOTTOM, 8)

        engine_row = wx.BoxSizer(wx.HORIZONTAL)
        engine_row.Add(wx.StaticText(page, label=_("Engine:")),
                       0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        _enames = engine_names()
        self._tts_engine_cb = wx.ComboBox(page, choices=_enames,
                                           value=_enames[0],
                                           style=wx.CB_READONLY, name="TTS engine")
        self._tts_engine_cb.Bind(wx.EVT_COMBOBOX, self._on_tts_engine)
        engine_row.Add(self._tts_engine_cb, 1)
        sizer.Add(engine_row, 0, wx.EXPAND | wx.BOTTOM, 8)

        self._tts_book = wx.Simplebook(page)
        for _ename in engine_names():
            _cls = engine_class(_ename)
            self._tts_book.AddPage(
                self._build_schema_panel(self._tts_book, _cls.CONFIG_SCHEMA, _ename),
                _ename)
        for i in range(1, self._tts_book.GetPageCount()):
            self._tts_book.GetPage(i).Disable()
        sizer.Add(self._tts_book, 1, wx.EXPAND)

        tmpl_row = wx.BoxSizer(wx.HORIZONTAL)
        tmpl_row.Add(wx.StaticText(page, label=_("Message template:")),
                     0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._tts_template = wx.TextCtrl(
            page,
            value="{username} says: {message}",
            name="Message template",
        )
        tmpl_row.Add(self._tts_template, 1)
        sizer.Add(tmpl_row, 0, wx.EXPAND | wx.TOP, 8)

        self._btn_tts_preview = wx.Button(page, label=_("&Preview Voice"))
        self._btn_tts_preview.Bind(wx.EVT_BUTTON, self._on_tts_preview)
        sizer.Add(self._btn_tts_preview, 0, wx.TOP, 8)

        page.SetSizer(sizer)
        return page

    def _fmt_slider_val(self, val: int, field: dict) -> str:
        fmt   = field.get("fmt", "")
        scale = field.get("scale")
        if fmt == "pct_signed":
            return f"+{val}%" if val >= 0 else f"{val}%"
        if fmt == "hz_signed":
            return f"+{val} Hz" if val >= 0 else f"{val} Hz"
        if fmt == "scale_x" and scale:
            return f"{val / scale:.2f}×"
        if scale:
            return f"{val / scale:.2f}"
        return str(val)

    def _schema_browse_file(self, ctrl: "wx.TextCtrl", wildcard: str):
        dlg = wx.FileDialog(self, wildcard=wildcard,
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _build_schema_panel(self, parent: "wx.Simplebook",
                            schema: list, prefix: str) -> "wx.Panel":
        """Build a settings panel driven by a CONFIG_SCHEMA list.

        Widget refs are stored in self._schema_widgets keyed by:
          '{prefix}.{key}'        primary widget
          '{prefix}.{key}__lbl'  slider value label
          '{prefix}.{key}__ids'  voice ID list ([] = integer-index mode)
          '{prefix}.{key}__btn'  voice fetch button
        """
        p  = wx.Panel(parent)
        vs = wx.BoxSizer(wx.VERTICAL)

        # Strip "mr:" namespace prefix when looking up the engine class.
        _lookup = prefix[3:] if prefix.startswith("mr:") else prefix

        for f in schema:
            ft  = f.get("type", "")
            key = f.get("key", "")
            lbl = f.get("label", "")
            wk  = f"{prefix}.{key}"

            if ft == "text":
                row = wx.BoxSizer(wx.HORIZONTAL)
                row.Add(wx.StaticText(p, label=_(lbl)),
                        0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
                style = wx.TE_PASSWORD if f.get("password") else 0
                ctrl  = wx.TextCtrl(p, style=style)
                row.Add(ctrl, 1)
                self._schema_widgets[wk] = ctrl
                vs.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)

            elif ft == "file":
                vs.Add(wx.StaticText(p, label=_(lbl)), 0, wx.BOTTOM, 2)
                row      = wx.BoxSizer(wx.HORIZONTAL)
                ctrl     = wx.TextCtrl(p, style=wx.TE_READONLY)
                wildcard = f.get("wildcard", "All files (*.*)|*.*")
                btn      = wx.Button(p, label=_("&Browse…"))
                btn.Bind(wx.EVT_BUTTON,
                         lambda e, c=ctrl, w=wildcard: self._schema_browse_file(c, w))
                row.Add(ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
                row.Add(btn,  0)
                self._schema_widgets[wk] = ctrl
                vs.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)

            elif ft == "choice":
                raw     = f.get("choices", [])
                labels  = [c[1] if isinstance(c, tuple) else str(c) for c in raw]
                row     = wx.BoxSizer(wx.HORIZONTAL)
                row.Add(wx.StaticText(p, label=_(lbl)),
                        0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
                ch = wx.Choice(p, choices=labels)
                if labels:
                    ch.SetSelection(0)
                row.Add(ch, 1)
                self._schema_widgets[wk] = ch
                vs.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)

            elif ft == "checkbox":
                cb = wx.CheckBox(p, label=_(lbl))
                self._schema_widgets[wk] = cb
                vs.Add(cb, 0, wx.BOTTOM, 4)

            elif ft == "slider":
                mn   = f.get("min", 0)
                mx   = f.get("max", 100)
                dflt = f.get("default", mn)
                row  = wx.BoxSizer(wx.HORIZONTAL)
                row.Add(wx.StaticText(p, label=_(lbl)),
                        0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
                sl      = wx.Slider(p, value=dflt, minValue=mn, maxValue=mx)
                val_lbl = wx.StaticText(p, label=self._fmt_slider_val(dflt, f),
                                        size=(42, -1))
                sl.Bind(wx.EVT_SLIDER,
                        lambda e, s=sl, l=val_lbl, fld=f:
                            l.SetLabel(self._fmt_slider_val(s.GetValue(), fld)))
                row.Add(sl,      1)
                row.Add(val_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
                self._schema_widgets[wk]           = sl
                self._schema_widgets[wk + "__lbl"] = val_lbl
                vs.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)

            elif ft == "voice_list":
                fetch_method   = f.get("fetch")
                static_choices = f.get("choices")

                if fetch_method:
                    fetch_btn = wx.Button(p, label=_("&Get Available Voices"))
                    fetch_btn.Bind(
                        wx.EVT_BUTTON,
                        lambda e, pfx=prefix, k=key, fn=fetch_method, b=fetch_btn:
                            self._schema_fetch_voices(pfx, k, fn, b))
                    self._schema_widgets[wk + "__btn"] = fetch_btn
                    vs.Add(fetch_btn, 0, wx.BOTTOM, 4)

                vs.Add(wx.StaticText(p, label=_(lbl)), 0, wx.BOTTOM, 2)
                lb   = wx.ListBox(p, choices=[], style=wx.LB_SINGLE)
                ids: list[str] = []

                if static_choices is not None:
                    if static_choices and isinstance(static_choices[0], tuple):
                        ids = [c[0] for c in static_choices]
                        lb.Set([c[1] for c in static_choices])
                    else:
                        ids = [str(c) for c in static_choices]
                        lb.Set([str(c) for c in static_choices])
                    if static_choices:
                        lb.SetSelection(0)
                elif not fetch_method:
                    # Index mode: populate from the engine's list_voices()
                    try:
                        voice_strs = engine_class(_lookup)().list_voices()
                    except Exception:
                        voice_strs = []
                    if not voice_strs:
                        voice_strs = [_("(no voices found)")]
                    lb.Set(voice_strs)
                    lb.SetSelection(0)
                    # ids stays [] → integer-index storage mode

                self._schema_widgets[wk]           = lb
                self._schema_widgets[wk + "__ids"] = ids
                vs.Add(lb, 1, wx.EXPAND | wx.BOTTOM, 4)

            elif ft == "note":
                txt = wx.StaticText(p, label=_(f.get("text", "")))
                txt.SetForegroundColour(
                    wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
                txt.Wrap(440)
                vs.Add(txt, 0, wx.BOTTOM, 4)

        p.SetSizer(vs)
        return p

    def _schema_fetch_voices(self, prefix: str, key: str,
                             method_name: str, btn: "wx.Button"):
        _lookup = prefix[3:] if prefix.startswith("mr:") else prefix
        cls     = engine_class(_lookup)
        wk      = f"{prefix}.{key}"
        cfg     = self._schema_get_config(prefix, cls.CONFIG_SCHEMA)

        btn.Disable()
        btn.SetLabel(_("Fetching…"))

        def _worker():
            pairs, err = [], None
            try:
                pairs = getattr(cls, method_name)(cfg)
            except Exception as exc:
                err = str(exc)
            wx.CallAfter(_done, pairs, err)

        def _done(pairs, err):
            if self:
                if err:
                    wx.MessageBox(f"Failed to fetch voices:\n{err}",
                                  _lookup, wx.OK | wx.ICON_ERROR, self)
                lb = self._schema_widgets.get(wk)
                if lb is not None:
                    self._schema_widgets[wk + "__ids"] = [vid for vid, _ in pairs]
                    lb.Set([lbl for _, lbl in pairs])
                    if pairs:
                        lb.SetSelection(0)
                btn.SetLabel(_("&Get Available Voices"))
                btn.Enable()

        threading.Thread(target=_worker, daemon=True,
                         name=f"{_lookup}-voices").start()

    def _schema_get_config(self, prefix: str, schema: list) -> dict:
        """Read current widget values for a given engine prefix into a config dict."""
        cfg = {}
        for f in schema:
            ft  = f.get("type", "")
            key = f.get("key", "")
            if not key:
                continue
            wk = f"{prefix}.{key}"
            w  = self._schema_widgets.get(wk)
            if w is None:
                continue

            if ft in ("text", "file"):
                cfg[key] = w.GetValue()
            elif ft == "choice":
                sel     = w.GetSelection()
                choices = f.get("choices", [])
                if sel >= 0 and sel < len(choices):
                    c = choices[sel]
                    cfg[key] = c[0] if isinstance(c, tuple) else c
                elif choices:
                    c = choices[0]
                    cfg[key] = c[0] if isinstance(c, tuple) else c
                else:
                    cfg[key] = ""
            elif ft == "checkbox":
                cfg[key] = w.GetValue()
            elif ft == "slider":
                val   = w.GetValue()
                scale = f.get("scale")
                cfg[key] = val / scale if scale else val
            elif ft == "voice_list":
                ids = self._schema_widgets.get(wk + "__ids", [])
                sel = w.GetSelection()
                if ids:
                    cfg[key] = (ids[sel]
                                if sel != wx.NOT_FOUND and sel < len(ids)
                                else ids[0])
                else:
                    cfg[key] = max(0, sel) if sel != wx.NOT_FOUND else 0
        return cfg

    def _schema_set_config(self, prefix: str, schema: list, cfg: dict):
        """Set widget values from a config dict for a given engine prefix."""
        for f in schema:
            ft  = f.get("type", "")
            key = f.get("key", "")
            if not key or key not in cfg:
                continue
            wk  = f"{prefix}.{key}"
            w   = self._schema_widgets.get(wk)
            if w is None:
                continue
            val = cfg[key]

            if ft in ("text", "file"):
                w.SetValue(str(val) if val is not None else "")
            elif ft == "choice":
                choices = f.get("choices", [])
                ids     = [c[0] if isinstance(c, tuple) else c for c in choices]
                try:
                    w.SetSelection(ids.index(val))
                except ValueError:
                    if choices:
                        w.SetSelection(0)
            elif ft == "checkbox":
                w.SetValue(bool(val))
            elif ft == "slider":
                scale   = f.get("scale")
                int_val = int(float(val) * scale) if scale else int(val)
                int_val = max(w.GetMin(), min(w.GetMax(), int_val))
                w.SetValue(int_val)
                lbl = self._schema_widgets.get(wk + "__lbl")
                if lbl:
                    lbl.SetLabel(self._fmt_slider_val(int_val, f))
            elif ft == "voice_list":
                ids = self._schema_widgets.get(wk + "__ids", [])
                if ids:
                    try:
                        w.SetSelection(ids.index(str(val)))
                    except ValueError:
                        pass   # voice not yet in list (fetch pending)
                else:
                    idx = int(val) if val is not None else 0
                    if 0 <= idx < w.GetCount():
                        w.SetSelection(idx)

    def _build_mastodon_page(self) -> wx.Panel:
        page  = wx.Panel(self._book)
        sizer = wx.BoxSizer(wx.VERTICAL)

        hint = wx.StaticText(
            page,
            label=_("Reads Mastodon replies to your stream post aloud using TTS. "
                    "The source stays idle until a post has been made from the Mastodon tab."))
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        hint.Wrap(440)
        sizer.Add(hint, 0, wx.BOTTOM, 8)

        engine_row = wx.BoxSizer(wx.HORIZONTAL)
        engine_row.Add(wx.StaticText(page, label=_("TTS engine:")),
                       0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        _enames = engine_names()
        self._mr_engine_cb = wx.ComboBox(page, choices=_enames, value=_enames[0],
                                          style=wx.CB_READONLY, name="Mastodon TTS engine")
        self._mr_engine_cb.Bind(wx.EVT_COMBOBOX, self._on_mr_engine)
        engine_row.Add(self._mr_engine_cb, 1)
        sizer.Add(engine_row, 0, wx.EXPAND | wx.BOTTOM, 8)

        self._mr_book = wx.Simplebook(page)
        for _ename in engine_names():
            _cls = engine_class(_ename)
            self._mr_book.AddPage(
                self._build_schema_panel(self._mr_book, _cls.CONFIG_SCHEMA, "mr:" + _ename),
                _ename)
        for i in range(1, self._mr_book.GetPageCount()):
            self._mr_book.GetPage(i).Disable()
        sizer.Add(self._mr_book, 1, wx.EXPAND)

        page.SetSizer(sizer)
        return page

    def _on_mr_engine(self, _event=None):
        old = self._mr_book.GetSelection()
        new = self._mr_engine_cb.GetSelection()
        if old not in (wx.NOT_FOUND, new):
            self._mr_book.GetPage(old).Disable()
        self._mr_book.SetSelection(new)
        self._mr_book.GetPage(new).Enable()
        self.Layout()
        self._mr_engine_cb.SetFocus()

    def _build_sounds_page(self) -> wx.Panel:
        page  = wx.Panel(self._book)
        sizer = wx.BoxSizer(wx.VERTICAL)

        hint = wx.StaticText(
            page,
            label=_("Plays audio cues when chat messages arrive or listener count changes. "
                    "Sound packs are sub-folders inside the 'sounds' directory."))
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        hint.Wrap(440)
        sizer.Add(hint, 0, wx.BOTTOM, 8)

        pack_row = wx.BoxSizer(wx.HORIZONTAL)
        pack_row.Add(wx.StaticText(page, label=_("Sound pack:")),
                     0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        packs = list_packs()
        self._sounds_pack_cb = wx.ComboBox(page, choices=packs, value=packs[0],
                                            style=wx.CB_READONLY, name="Sound pack")
        self._sounds_pack_cb.Bind(wx.EVT_COMBOBOX, self._on_sounds_pack)
        pack_row.Add(self._sounds_pack_cb, 1)
        sizer.Add(pack_row, 0, wx.EXPAND | wx.BOTTOM, 8)

        sizer.Add(wx.StaticText(page, label=_("Play sound for:")), 0, wx.BOTTOM, 4)
        self._sounds_clb = AccessibleCheckListBox(
            page,
            choices=[_(label) for _key, label, _fname in SOUND_EVENTS],
            style=wx.LB_SINGLE,
            name="Sound events",
        )
        self._sounds_clb.Bind(wx.EVT_CHECKLISTBOX, self._on_sounds_check)
        sizer.Add(self._sounds_clb, 1, wx.EXPAND)

        # Initialise availability and check all available events by default.
        self._sounds_refresh_availability(packs[0], check_available=True)

        page.SetSizer(sizer)
        return page

    def _sounds_refresh_availability(self, pack: str, check_available: bool = False):
        avail = available_events(pack)
        self._sounds_avail = avail   # remember for check guard
        for i, (key, label, _fname) in enumerate(SOUND_EVENTS):
            if key in avail:
                self._sounds_clb.SetString(i, _(label))
                if check_available:
                    self._sounds_clb.Check(i, True)
            else:
                self._sounds_clb.SetString(i, _(label) + _(" (no file)"))
                self._sounds_clb.Check(i, False)

    def _on_sounds_pack(self, _event=None):
        pack = self._sounds_pack_cb.GetValue()
        self._sounds_refresh_availability(pack)

    def _on_sounds_check(self, event):
        idx = event.GetInt()
        key = SOUND_EVENTS[idx][0]
        if key not in self._sounds_avail:
            self._sounds_clb.Check(idx, False)   # reject — no file

    def _on_tts_engine(self, _event=None):
        old = self._tts_book.GetSelection()
        new = self._tts_engine_cb.GetSelection()
        if old != wx.NOT_FOUND and old != new:
            self._tts_book.GetPage(old).Disable()
        self._tts_book.SetSelection(new)
        self._tts_book.GetPage(new).Enable()
        self._tts_engine_cb.SetFocus()

    def _on_tts_preview(self, _event=None):
        name = self._tts_engine_cb.GetValue()
        key  = engine_key(name)
        cfg  = self._tts_page_config()
        eng  = make_engine(key, cfg)
        if not eng.is_available():
            wx.MessageBox(
                f"'{eng.name}' is not available.\n"
                "Please install the required packages first.",
                "Engine Unavailable", wx.OK | wx.ICON_WARNING, self)
            return
        self._btn_tts_preview.Disable()
        self._btn_tts_preview.SetLabel(_("Previewing…"))

        def _worker():
            try:
                audio = eng.synthesize(
                    _("Hello, this is a preview of the selected voice."),
                    sample_rate=48000, channels=2,
                )
                if audio is not None:
                    _play_audio_blocking(audio, sample_rate=48000, channels=2)
            except Exception as e:
                print(f"[TTS preview] {e}", flush=True)
            finally:
                wx.CallAfter(_restore)

        def _restore():
            if self:
                self._btn_tts_preview.SetLabel(_("&Preview Voice"))
                self._btn_tts_preview.Enable()
                self._btn_tts_preview.SetFocus()

        threading.Thread(target=_worker, daemon=True, name="tts-preview").start()

    def _tts_page_config(self) -> dict:
        """Read current engine-specific widget values into a config dict."""
        name = self._tts_engine_cb.GetValue()
        return self._schema_get_config(name, engine_class(name).CONFIG_SCHEMA)

    # ── event handlers ────────────────────────────────────────────────────────

    # ── initial state loading (edit mode) ────────────────────────────────────

    def load_initial_state(self, entry: dict):
        """Pre-populate the dialog from a serialized source entry dict."""
        _TYPE_MAP = {
            "mic":             _TYPE_MIC,
            "loopback":        _TYPE_LOOPBACK,
            "app_watch":       _TYPE_APP,
            "app_include":     _TYPE_APP,
            "file":            _TYPE_FILE,
            "url":             _TYPE_URL,
            "tts":             _TYPE_TTS,
            "sounds":          _TYPE_SOUNDS,
            "mastodon_replies":_TYPE_MASTODON,
        }
        type_str = entry.get("type", "mic")
        type_idx = _TYPE_MAP.get(type_str, _TYPE_MIC)
        self._type_combo.SetSelection(type_idx)
        self._on_type()

        if type_str == "mic":
            self._preload_mic(entry.get("device_index"))
        elif type_str == "loopback":
            self._preload_loopback(entry.get("device_index"))
        elif type_str in ("app_watch", "app_include"):
            self._preload_app(entry.get("exe_name", ""))
        elif type_str == "file":
            self._file_path_txt.SetValue(entry.get("path", ""))
        elif type_str == "url":
            self._url_txt.SetValue(entry.get("url", ""))
        elif type_str == "tts":
            self._preload_tts(entry.get("engine", "sapi"),
                              entry.get("engine_config", {}),
                              entry.get("tts_template", ""))
        elif type_str == "sounds":
            self._preload_sounds(entry.get("pack", "default"),
                                 entry.get("enabled_events", []))
        elif type_str == "mastodon_replies":
            self._preload_mastodon(entry.get("engine", "sapi"),
                                   entry.get("engine_config", {}))

    def _preload_mic(self, device_index):
        for i, dev in enumerate(self._devices):
            if dev.get("index") == device_index:
                self._mic_lb.SetSelection(i)
                return

    def _preload_loopback(self, device_index):
        for i, dev in enumerate(self._loopback_devs):
            if dev.get("index") == device_index:
                self._lb_loopback.SetSelection(i)
                return

    def _preload_app(self, exe_name: str):
        if not exe_name:
            return
        for i in range(self._app_clb.GetCount()):
            if self._app_clb.GetString(i).lower() == exe_name.lower():
                self._app_clb.Check(i)
                return
        # App not currently running — add it to the list
        idx = self._app_clb.Append(exe_name)
        self._app_clb.Check(idx)

    def _preload_tts(self, eng_key_str: str, cfg: dict, template: str):
        name = engine_display_name(eng_key_str)
        if self._tts_engine_cb.SetStringSelection(name):
            self._on_tts_engine()
        self._schema_set_config(name, engine_class(name).CONFIG_SCHEMA, cfg)
        self._tts_template.SetValue(template or ChatTtsCapture._DEFAULT_TEMPLATE)

    def _preload_sounds(self, pack: str, enabled_events: list):
        packs = list_packs()
        if pack in packs:
            self._sounds_pack_cb.SetValue(pack)
        self._sounds_refresh_availability(pack, check_available=False)
        avail = available_events(pack)
        for i, (key, _label, _fname) in enumerate(SOUND_EVENTS):
            self._sounds_clb.Check(i, key in enabled_events and key in avail)

    def _preload_mastodon(self, eng_key_str: str, cfg: dict):
        name = engine_display_name(eng_key_str)
        if self._mr_engine_cb.SetStringSelection(name):
            self._on_mr_engine()
        self._schema_set_config("mr:" + name, engine_class(name).CONFIG_SCHEMA, cfg)

    # ── event handlers ────────────────────────────────────────────────────────

    def _on_type(self, _event=None):
        old = self._book.GetSelection()
        new = self._type_combo.GetSelection()
        if old != wx.NOT_FOUND and old != new:
            self._book.GetPage(old).Disable()
        self._book.SetSelection(new)
        self._book.GetPage(new).Enable()
        self.Layout()
        self._type_combo.SetFocus()

    def _on_browse_exe(self, _event=None):
        dlg = wx.FileDialog(self, "Select application executable",
                            wildcard="Executables (*.exe)|*.exe",
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            exe_name = os.path.basename(dlg.GetPath())
            items = [self._app_clb.GetString(i).lower()
                     for i in range(self._app_clb.GetCount())]
            if exe_name.lower() not in items:
                idx = self._app_clb.Append(exe_name)
                self._app_clb.Check(idx)
            else:
                for i, s in enumerate(items):
                    if s == exe_name.lower():
                        self._app_clb.Check(i)
                        break
        dlg.Destroy()

    def _on_browse_file(self, _event=None):
        wildcard = (
            "Audio files (*.wav;*.mp3;*.flac;*.ogg;*.aiff;*.aif)|"
            "*.wav;*.mp3;*.flac;*.ogg;*.aiff;*.aif|"
            "All files (*.*)|*.*"
        )
        dlg = wx.FileDialog(self, "Select audio file",
                            wildcard=wildcard,
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            self._file_path_txt.SetValue(dlg.GetPath())
        dlg.Destroy()

    # ── result accessors ──────────────────────────────────────────────────────

    @property
    def source_type(self) -> str:
        """Returns 'mic', 'app', 'loopback', 'file', 'tts', 'sounds', or 'mastodon_replies'."""
        idx = self._type_combo.GetSelection()
        if idx == _TYPE_MIC:
            return "mic"
        if idx == _TYPE_LOOPBACK:
            return "loopback"
        if idx == _TYPE_FILE:
            return "file"
        if idx == _TYPE_URL:
            return "url"
        if idx == _TYPE_TTS:
            return "tts"
        if idx == _TYPE_SOUNDS:
            return "sounds"
        if idx == _TYPE_MASTODON:
            return "mastodon_replies"
        return "app"

    @property
    def tts_config(self) -> dict:
        """Returns {'engine': key, 'engine_config': {...}, 'tts_template': ...} for TTS sources."""
        name = self._tts_engine_cb.GetValue()
        key  = engine_key(name)
        cfg  = self._tts_page_config()
        return {"engine": key, "engine_config": cfg,
                "tts_template": self._tts_template.GetValue()}

    @property
    def sounds_config(self) -> dict:
        """Returns {'pack': ..., 'enabled_events': [...]} for Sound Events sources."""
        pack = self._sounds_pack_cb.GetValue()
        enabled = [SOUND_EVENTS[i][0]
                   for i in range(self._sounds_clb.GetCount())
                   if self._sounds_clb.IsChecked(i)]
        return {"pack": pack, "enabled_events": enabled}

    @property
    def mastodon_tts_config(self) -> dict:
        """Returns {'engine': key, 'engine_config': {...}} for Mastodon Replies sources."""
        name = self._mr_engine_cb.GetValue()
        key  = engine_key(name)
        cfg  = self._schema_get_config("mr:" + name, engine_class(name).CONFIG_SCHEMA)
        return {"engine": key, "engine_config": cfg}

    def selected_device(self) -> dict | None:
        """Returns the selected WASAPI device dict, or None if nothing selected."""
        idx = self._mic_lb.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(self._devices):
            return None
        return self._devices[idx]

    def selected_loopback_device(self) -> dict | None:
        """Returns the selected loopback device dict, or None if nothing selected."""
        idx = self._lb_loopback.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(self._loopback_devs):
            return None
        return self._loopback_devs[idx]

    def checked_exe_names(self) -> list[str]:
        """Returns list of checked exe names (app page only)."""
        return [self._app_clb.GetString(i)
                for i in range(self._app_clb.GetCount())
                if self._app_clb.IsChecked(i)]

    def selected_file_path(self) -> str:
        """Returns the selected file path (file page only), or empty string."""
        return self._file_path_txt.GetValue().strip()

    def selected_audio_url(self) -> str:
        """Returns the entered audio stream URL, or empty string."""
        return self._url_txt.GetValue().strip()


# ── _AddMicDialog — ASIO support (commented out; needs sub-device/channel enumeration) ──
#
# TODO: Before re-enabling, wire init() + getChannels() + getChannelInfo() through the
#       UI so the user can pick individual input channels, not just a driver name.
#       The IASIO vtable implementation lives in capture_asio.py.
#
# from ..audio.capture_asio import AsioCapture, list_asio_drivers
#
# class _AddMicDialog(wx.Dialog):
#     def __init__(self, parent, sample_rate, channels, chunk_frames):
#         ...  # radio buttons: WASAPI | ASIO
#     def _select_api(self, api): ...   # 0=WASAPI, 1=ASIO
#     def _on_ok(self, _event=None): ...
#     def get_result(self): ...         # returns (capture_obj, name)
