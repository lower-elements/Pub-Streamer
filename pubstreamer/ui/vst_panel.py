"""VST chain editor — modal dialog, opened per-source or for the master chain."""

import datetime
import wx
import wx.lib.scrolledpanel as scrolled

from ..audio.vst_chain import VstChain


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)


class VstPanelDialog(wx.Dialog):
    def __init__(self, parent, chain: VstChain, title: str = "VST Chain"):
        super().__init__(parent, title=title, size=(480, 400),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._chain = chain
        self._build()
        self._refresh()
        self.Centre()

    def _build(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ── plugin list ──────────────────────────────────────────────────────
        sizer.Add(wx.StaticText(self, label="Loaded plugins:"), 0, wx.LEFT | wx.TOP, 8)
        self._lb = wx.ListBox(self, style=wx.LB_SINGLE, name="Loaded plugins")
        self._lb.Bind(wx.EVT_LISTBOX, self._on_select)
        sizer.Add(self._lb, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        add_btn  = wx.Button(self, label="&Add VST…")
        rem_btn  = wx.Button(self, label="&Remove")
        prop_btn = wx.Button(self, label="&Properties…")
        add_btn.Bind(wx.EVT_BUTTON,  self._add)
        rem_btn.Bind(wx.EVT_BUTTON,  self._remove)
        prop_btn.Bind(wx.EVT_BUTTON, self._open_properties)
        for b in (add_btn, rem_btn, prop_btn):
            btn_row.Add(b, 0, wx.RIGHT, 4)
        sizer.Add(btn_row, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 8)

        # ── parameters (read-only summary) ───────────────────────────────────
        sizer.Add(wx.StaticText(self, label="Parameters (selected plugin):"), 0, wx.LEFT, 8)
        self._params = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
                                   name="Plugin parameters")
        sizer.Add(self._params, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── bypass + close ───────────────────────────────────────────────────
        bottom = wx.BoxSizer(wx.HORIZONTAL)
        self._bypass_cb = wx.CheckBox(self, label="&Bypass chain")
        self._bypass_cb.SetValue(not self._chain.enabled)

        def _on_bypass(e):
            self._chain.enabled = not self._bypass_cb.GetValue()
            _log(f"VST bypass: '{self.Title}' enabled={self._chain.enabled}")

        self._bypass_cb.Bind(wx.EVT_CHECKBOX, _on_bypass)
        bottom.Add(self._bypass_cb, 1, wx.ALIGN_CENTER_VERTICAL)
        close_btn = wx.Button(self, wx.ID_CLOSE, label="&Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda _: self.EndModal(wx.ID_CLOSE))
        bottom.Add(close_btn, 0)
        sizer.Add(bottom, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(sizer)
        # Re-read parameters whenever this dialog regains focus (e.g. after Properties closes)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)

    def _refresh(self):
        self._lb.Clear()
        for name in self._chain.plugin_names():
            self._lb.Append(name)

    def _on_activate(self, event):
        if event.GetActive():
            self._on_select()
        event.Skip()

    def _on_select(self, _event=None):
        idx = self._lb.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        params = self._chain.plugin_parameters(idx)
        lines = "\n".join(f"{k}: {v}" for k, v in params.items())
        self._params.SetValue(lines or "(no parameters exposed)")

    def _add(self, _event=None):
        dlg = wx.FileDialog(
            self, "Select VST plugin",
            wildcard="VST plugins (*.vst3;*.dll)|*.vst3;*.dll|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            err = self._chain.add_plugin(path)
            if err:
                _log(f"VST load failed: '{path}' — {err}")
                wx.MessageBox(f"Failed to load plugin:\n{err}", "Plugin error",
                              wx.OK | wx.ICON_ERROR, self)
            else:
                _log(f"VST loaded: '{path}' in '{self.Title}'")
                self._refresh()
        dlg.Destroy()

    def _remove(self, _event=None):
        idx = self._lb.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        names = self._chain.plugin_names()
        _log(f"VST remove: '{names[idx]}' from '{self.Title}'")
        self._chain.remove_plugin(idx)
        self._params.SetValue("")
        self._refresh()

    def _open_properties(self, _event=None):
        idx = self._lb.GetSelection()
        if idx == wx.NOT_FOUND:
            wx.MessageBox("Select a plugin first.", "Properties",
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        VstPropertiesDialog(self, self._chain, idx).Show()


# ── VstPropertiesDialog ───────────────────────────────────────────────────────

class VstPropertiesDialog(wx.Dialog):
    """
    Per-plugin properties: preset picker, parameter sliders/inputs, native UI button.
    Opened modeless so the native editor frame can coexist with it.
    """

    def __init__(self, parent, chain: VstChain, slot_index: int):
        names = chain.plugin_names()
        title = f"Properties — {names[slot_index]}" if slot_index < len(names) else "VST Properties"
        super().__init__(parent, title=title, size=(520, 580),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._chain       = chain
        self._idx         = slot_index
        self._kind        = chain.slot_kind(slot_index)
        self._editor_frame = None
        self._param_rows  = []   # (param_key, control, [val_label])
        self._build()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()

    def _build(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── presets (VST2 only, only if the plugin actually has programs) ────
        programs = self._chain.get_programs(self._idx)
        if programs:
            preset_box = wx.StaticBox(self, label="Preset")
            ps = wx.StaticBoxSizer(preset_box, wx.HORIZONTAL)
            self._preset_cb = wx.ComboBox(self, choices=programs,
                                           style=wx.CB_READONLY, name="Preset")
            cur = self._chain.get_program(self._idx)
            if 0 <= cur < len(programs):
                self._preset_cb.SetSelection(cur)
            load_btn = wx.Button(self, label="&Load Preset")
            load_btn.Bind(wx.EVT_BUTTON, self._load_preset)
            ps.Add(self._preset_cb, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
            ps.Add(load_btn, 0, wx.ALIGN_CENTER_VERTICAL)
            outer.Add(ps, 0, wx.EXPAND | wx.ALL, 8)
        else:
            self._preset_cb = None

        # ── parameter controls ───────────────────────────────────────────────
        outer.Add(wx.StaticText(self, label="Parameters:"), 0, wx.LEFT, 8)

        self._scroll = scrolled.ScrolledPanel(self, style=wx.VSCROLL)
        scroll = self._scroll
        scroll.SetupScrolling(scroll_x=False)
        grid = wx.FlexGridSizer(cols=3, vgap=4, hgap=8)
        grid.AddGrowableCol(1, 1)

        params = self._chain.plugin_parameters(self._idx)
        self._param_rows = []

        if self._kind == "vst2":
            for i, (name, value) in enumerate(params.items()):
                lbl = wx.StaticText(scroll, label=name + ":", style=wx.ST_ELLIPSIZE_END)
                lbl.SetMinSize((150, -1))
                slider = wx.Slider(scroll, value=int(value * 1000),
                                   minValue=0, maxValue=1000,
                                   name=name, style=wx.SL_HORIZONTAL)
                val_lbl = wx.StaticText(scroll, label=f"{value:.4f}", size=(60, -1))

                def _on_slide(evt, pi=i, vl=val_lbl):
                    v = evt.GetEventObject().GetValue() / 1000.0
                    vl.SetLabel(f"{v:.4f}")
                    self._chain.set_parameter(self._idx, pi, v)

                slider.Bind(wx.EVT_SLIDER, _on_slide)
                grid.Add(lbl,     0, wx.ALIGN_CENTER_VERTICAL)
                grid.Add(slider,  1, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
                grid.Add(val_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
                self._param_rows.append((i, slider, val_lbl))
        else:
            for name, value in params.items():
                lbl = wx.StaticText(scroll, label=name + ":", style=wx.ST_ELLIPSIZE_END)
                lbl.SetMinSize((150, -1))
                tc = wx.TextCtrl(scroll, value=str(value), name=name,
                                 style=wx.TE_PROCESS_ENTER)

                def _on_enter(evt, pn=name, t=tc):
                    self._apply_vst3(pn, t)

                tc.Bind(wx.EVT_TEXT_ENTER, _on_enter)

                set_btn = wx.Button(scroll, label="Set", size=(44, -1))
                set_btn.Bind(wx.EVT_BUTTON, lambda e, pn=name, t=tc: self._apply_vst3(pn, t))

                grid.Add(lbl,     0, wx.ALIGN_CENTER_VERTICAL)
                grid.Add(tc,      1, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
                grid.Add(set_btn, 0, wx.ALIGN_CENTER_VERTICAL)
                self._param_rows.append((name, tc))

        scroll.SetSizer(grid)
        outer.Add(scroll, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── bottom row ───────────────────────────────────────────────────────
        bottom = wx.BoxSizer(wx.HORIZONTAL)
        if self._chain.has_editor(self._idx):
            ui_btn = wx.Button(self, label="Open &Plugin UI")
            ui_btn.Bind(wx.EVT_BUTTON, self._open_editor)
            bottom.Add(ui_btn, 0, wx.RIGHT, 8)
        bottom.AddStretchSpacer(1)
        close_btn = wx.Button(self, wx.ID_CLOSE, label="&Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda _: self.Close())
        bottom.Add(close_btn, 0)
        outer.Add(bottom, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(outer)

    # ── preset ───────────────────────────────────────────────────────────────

    def _load_preset(self, _event=None):
        if not self._preset_cb:
            return
        sel = self._preset_cb.GetSelection()
        if sel == wx.NOT_FOUND:
            return
        self._chain.set_program(self._idx, sel)
        # Defer the refresh: let wx finish processing the button event first,
        # then read back the new parameter values from the plugin.
        wx.CallAfter(self._refresh_param_values)

    def _refresh_param_values(self):
        params = self._chain.plugin_parameters(self._idx)
        vals   = list(params.values())
        # Freeze prevents individual redraws mid-loop; Thaw flushes everything at once
        self._scroll.Freeze()
        try:
            if self._kind == "vst2":
                for i, slider, val_lbl in self._param_rows:
                    if i < len(vals):
                        v = float(vals[i])
                        slider.SetValue(int(v * 1000))
                        val_lbl.SetLabel(f"{v:.4f}")
            else:
                for j, (name, tc) in enumerate(self._param_rows):
                    if j < len(vals):
                        tc.SetValue(str(vals[j]))
        finally:
            self._scroll.Thaw()

    # ── VST3 param write ──────────────────────────────────────────────────────

    def _apply_vst3(self, name: str, tc):
        try:
            self._chain.set_parameter(self._idx, name, float(tc.GetValue()))
        except ValueError:
            pass

    # ── native editor window ──────────────────────────────────────────────────

    def _open_editor(self, _event=None):
        if self._editor_frame and self._editor_frame.IsShown():
            self._editor_frame.Raise()
            return

        frame = wx.Frame(self, title="Plugin UI",
                         style=wx.CAPTION | wx.CLOSE_BOX |
                               wx.FRAME_TOOL_WINDOW | wx.CLIP_CHILDREN)
        hwnd   = frame.GetHandle()
        result = self._chain.open_editor(self._idx, hwnd)

        if result is None:
            frame.Destroy()
            wx.MessageBox("This plugin does not provide a native editor.",
                          "Plugin UI", wx.OK | wx.ICON_INFORMATION, self)
            return

        w, h = result
        frame.SetClientSize((w, h))
        frame.Bind(wx.EVT_CLOSE, self._on_editor_close)
        frame.Show()
        self._editor_frame = frame

    def _on_editor_close(self, _event):
        self._chain.close_editor(self._idx)
        if self._editor_frame:
            self._editor_frame.Destroy()
            self._editor_frame = None

    def _on_close(self, event):
        if self._editor_frame:
            self._chain.close_editor(self._idx)
            self._editor_frame.Destroy()
            self._editor_frame = None
        event.Skip()
