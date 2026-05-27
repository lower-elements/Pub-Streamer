"""
VST effect chain per source group.

VST3 plugins (.vst3) are hosted via Spotify's pedalboard library.
VST2 plugins (.dll)  are hosted via a minimal ctypes wrapper that calls
VSTPluginMain and processReplacing directly — pedalboard 0.9+ dropped VST2.

Both plugin types are held in a single ordered list so the signal chain
respects the order the user added them.
"""

import ctypes
import os
import numpy as np

try:
    from pedalboard import Pedalboard, load_plugin
    _PEDALBOARD_AVAILABLE = True
except ImportError:
    _PEDALBOARD_AVAILABLE = False


# ── VST2 ctypes host ──────────────────────────────────────────────────────────

_VST_MAGIC = 0x56737450   # 'VstP'

# Opcode constants (from the Steinberg VST2 SDK)
_effOpen                   = 0
_effClose                  = 1
_effSetProgram             = 2
_effGetProgram             = 3
_effGetProgramName         = 5     # current program name
_effGetProgramNameIndexed  = 29    # VST 2.1+ — name by index
_effGetParamName           = 8
_effEditGetRect            = 13    # ptr → ERect**
_effEditOpen               = 14    # ptr = parent HWND
_effEditClose              = 15
_effSetSampleRate          = 10
_effSetBlockSize           = 11
_effMainsChanged           = 12
_effGetEffectName          = 45
_amVersion                 = 1     # audioMasterVersion
_effFlagsHasEditor         = 1     # bit in AEffect.flags


class _AEffect(ctypes.Structure):
    pass


# VstTimeInfo — returned to plugins for audioMasterGetTime queries.
class _VstTimeInfo(ctypes.Structure):
    _fields_ = [
        ("samplePos",            ctypes.c_double),
        ("sampleRate",           ctypes.c_double),
        ("nanoSeconds",          ctypes.c_double),
        ("ppqPos",               ctypes.c_double),
        ("tempo",                ctypes.c_double),
        ("barStartPos",          ctypes.c_double),
        ("cycleStartPos",        ctypes.c_double),
        ("cycleEndPos",          ctypes.c_double),
        ("timeSigNumerator",     ctypes.c_int32),
        ("timeSigDenominator",   ctypes.c_int32),
        ("smpteOffset",          ctypes.c_int32),
        ("smpteFrameRate",       ctypes.c_int32),
        ("samplesToNextClock",   ctypes.c_int32),
        ("flags",                ctypes.c_int32),
    ]

_kVstTransportPlaying = 1 << 1
_kVstTempoValid       = 1 << 10
_kVstTimeSigValid     = 1 << 13

_time_info = _VstTimeInfo()
_time_info.sampleRate         = 48000.0
_time_info.tempo              = 120.0
_time_info.timeSigNumerator   = 4
_time_info.timeSigDenominator = 4
_time_info.flags              = _kVstTransportPlaying | _kVstTempoValid | _kVstTimeSigValid
_time_info_addr = ctypes.addressof(_time_info)

_audioMasterGetTime = 7

# Return type must be c_int64 (VstIntPtr = pointer-sized on x64).
# Using c_long (32-bit on Windows) leaves RAX's upper 32 bits as garbage;
# plugins reading the return of audioMasterGetTime as a VstTimeInfo* then
# crash on whatever address those garbage bits form.
_AudioMasterProc = ctypes.WINFUNCTYPE(
    ctypes.c_int64,
    ctypes.POINTER(_AEffect), ctypes.c_int32, ctypes.c_int32,
    ctypes.c_int64, ctypes.c_void_p, ctypes.c_float,
)
_DispatcherProc = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    ctypes.POINTER(_AEffect), ctypes.c_int32, ctypes.c_int32,
    ctypes.c_long, ctypes.c_void_p, ctypes.c_float,
)
_SetParamProc = ctypes.WINFUNCTYPE(None,
    ctypes.POINTER(_AEffect), ctypes.c_int32, ctypes.c_float)
_GetParamProc = ctypes.WINFUNCTYPE(ctypes.c_float,
    ctypes.POINTER(_AEffect), ctypes.c_int32)
_ProcessProc  = ctypes.WINFUNCTYPE(None,
    ctypes.POINTER(_AEffect),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
    ctypes.c_int32)

_AEffect._fields_ = [
    ("magic",                ctypes.c_int32),
    ("dispatcher",           _DispatcherProc),
    ("_process_deprecated",  _ProcessProc),
    ("setParameter",         _SetParamProc),
    ("getParameter",         _GetParamProc),
    ("numPrograms",          ctypes.c_int32),
    ("numParams",            ctypes.c_int32),
    ("numInputs",            ctypes.c_int32),
    ("numOutputs",           ctypes.c_int32),
    ("flags",                ctypes.c_int32),
    ("_resvd1",              ctypes.c_void_p),
    ("_resvd2",              ctypes.c_void_p),
    ("initialDelay",         ctypes.c_int32),
    ("_realQualities",       ctypes.c_int32),
    ("_offQualities",        ctypes.c_int32),
    ("_ioRatio",             ctypes.c_float),
    ("object",               ctypes.c_void_p),
    ("user",                 ctypes.c_void_p),
    ("uniqueID",             ctypes.c_int32),
    ("version",              ctypes.c_int32),
    ("processReplacing",     _ProcessProc),
    ("_processDoubleReplacing", ctypes.c_void_p),
    ("_future",              ctypes.c_char * 56),
]

# ERect layout from the VST2 SDK (top/left/bottom/right, NOT Windows RECT order)
class _ERect(ctypes.Structure):
    _fields_ = [
        ("top",    ctypes.c_short),
        ("left",   ctypes.c_short),
        ("bottom", ctypes.c_short),
        ("right",  ctypes.c_short),
    ]


@_AudioMasterProc
def _audio_master(effect, opcode, index, value, ptr, opt):
    if opcode == _amVersion:
        return 2400   # VST 2.4
    if opcode == _audioMasterGetTime:
        return _time_info_addr
    return 0


class Vst2Plugin:
    """Minimal VST2 plugin host using ctypes."""

    def __init__(self, path: str, sample_rate: int = 48000, block_size: int = 1024):
        self._lib = ctypes.WinDLL(path)
        self._aeffect = None

        # Locate VSTPluginMain (some older plugins export 'main' instead)
        for entry in ("VSTPluginMain", "main"):
            try:
                vst_main = getattr(self._lib, entry)
                break
            except AttributeError:
                continue
        else:
            raise RuntimeError("Neither VSTPluginMain nor main exported by DLL")

        vst_main.restype  = ctypes.POINTER(_AEffect)
        vst_main.argtypes = [_AudioMasterProc]
        aeffect = vst_main(_audio_master)

        if not aeffect or aeffect.contents.magic != _VST_MAGIC:
            raise RuntimeError("DLL did not return a valid AEffect (not a VST2 plugin?)")

        self._aeffect = aeffect
        self._dispatch(_effOpen, 0, 0, None, 0.0)
        self._dispatch(_effSetSampleRate, 0, 0, None, float(sample_rate))
        self._dispatch(_effSetBlockSize, 0, block_size, None, 0.0)
        self._dispatch(_effMainsChanged, 0, 1, None, 0.0)

        # Cache processReplacing by reading the pointer directly from the struct
        # in memory.  This is more reliable than accessing it as a struct field
        # attribute on every process() call.
        ae_addr   = ctypes.cast(aeffect, ctypes.c_void_p).value
        pr_offset = _AEffect.processReplacing.offset   # verified 120
        pr_val    = ctypes.c_void_p.from_address(ae_addr + pr_offset).value
        self._process_fn: _ProcessProc | None = _ProcessProc(pr_val) if pr_val else None
        self._diag_done = False
        _time_info.sampleRate = float(sample_rate)

    def _dispatch(self, opcode, index=0, value=0, ptr=None, opt=0.0):
        ae = self._aeffect
        return ae.contents.dispatcher(ae, opcode, index, value, ptr, opt)

    @property
    def name(self) -> str:
        buf = ctypes.create_string_buffer(64)
        self._dispatch(_effGetEffectName, 0, 0, buf, 0.0)
        n = buf.value.decode("utf-8", errors="replace").strip("\x00")
        return n or os.path.basename(getattr(self, "_path", "VST2 Plugin"))

    @property
    def parameters(self) -> dict:
        ae = self._aeffect
        n = min(ae.contents.numParams, 128)
        result = {}
        for i in range(n):
            buf = ctypes.create_string_buffer(32)
            self._dispatch(_effGetParamName, i, 0, buf, 0.0)
            pname = buf.value.decode("utf-8", errors="replace").strip("\x00") or f"Param {i}"
            val = ae.contents.getParameter(ae, i)
            result[pname] = round(float(val), 4)
        return result

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process float32 (channels, frames) through processReplacing."""
        if self._process_fn is None:
            if not self._diag_done:
                self._diag_done = True
                print("[VST2] processReplacing is NULL — plugin skipped")
            return audio

        ae    = self._aeffect
        n     = audio.shape[1]
        n_in  = ae.contents.numInputs
        n_out = ae.contents.numOutputs

        if n_out == 0:
            return audio

        PtrF    = ctypes.POINTER(ctypes.c_float)
        PtrPtrF = ctypes.POINTER(PtrF)

        # Build contiguous float32 buffers
        in_bufs = [
            np.ascontiguousarray(
                audio[i] if i < audio.shape[0] else np.zeros(n, dtype=np.float32),
                dtype=np.float32,
            )
            for i in range(n_in)
        ]
        out_bufs = [np.zeros(n, dtype=np.float32) for _ in range(n_out)]

        # Store raw pointer values in numpy uint64 arrays.  This gives a
        # contiguous 8-bytes-per-slot layout that the DLL reads as float**.
        # Using numpy here (rather than ctypes array + cast) avoids any
        # ambiguity about how ctypes converts array-of-pointer to float**.
        in_ptrs  = np.array([b.ctypes.data for b in in_bufs],  dtype=np.uint64)
        out_ptrs = np.array([b.ctypes.data for b in out_bufs], dtype=np.uint64)
        in_pp    = in_ptrs.ctypes.data_as(PtrPtrF)
        out_pp   = out_ptrs.ctypes.data_as(PtrPtrF)

        if not self._diag_done:
            ae_addr   = ctypes.cast(ae, ctypes.c_void_p).value
            pr_val    = ctypes.c_void_p.from_address(ae_addr + _AEffect.processReplacing.offset).value
            obj_val   = ae.contents.object  or 0
            user_val  = ae.contents.user    or 0
            print(f"[VST2] ae={ae_addr:#018x}  processReplacing={pr_val:#018x}")
            print(f"[VST2] ae.object={obj_val:#018x}  ae.user={user_val:#018x}")
            print(f"[VST2] numIn={n_in} numOut={n_out}")
            print(f"[VST2] in_pp ={in_ptrs.ctypes.data:#018x}  "
                  f"values={[f'{v:#018x}' for v in in_ptrs]}")
            print(f"[VST2] out_pp={out_ptrs.ctypes.data:#018x}  "
                  f"values={[f'{v:#018x}' for v in out_ptrs]}")

        try:
            self._process_fn(ae, in_pp, out_pp, ctypes.c_int32(n))
        except Exception as e:
            if not self._diag_done:
                self._diag_done = True
                print(f"[VST2] processReplacing raised {type(e).__name__}: {e}")
            return audio

        if not self._diag_done:
            self._diag_done = True
            diff = float(np.max(np.abs(
                out_bufs[0].astype(np.float64) - in_bufs[0].astype(np.float64)
            )))
            print(f"[VST2] processReplacing OK  max|out-in|={diff:.6f}")

        if n_out == 1:
            return np.vstack([out_bufs[0], out_bufs[0]])
        return np.vstack(out_bufs[:2])

    # ── presets ───────────────────────────────────────────────────────────────

    @property
    def num_programs(self) -> int:
        return self._aeffect.contents.numPrograms if self._aeffect else 0

    def program_names(self) -> list[str]:
        n = min(self.num_programs, 512)
        if n == 0:
            return []
        names = []
        cur = self._dispatch(_effGetProgram)
        for i in range(n):
            buf = ctypes.create_string_buffer(64)
            self._dispatch(_effGetProgramNameIndexed, i, -1, buf, 0.0)
            name = buf.value.decode("utf-8", errors="replace").strip("\x00")
            if not name:
                # Older plugins that only support effGetProgramName
                self._dispatch(_effSetProgram, 0, i, None, 0.0)
                buf2 = ctypes.create_string_buffer(64)
                self._dispatch(_effGetProgramName, 0, 0, buf2, 0.0)
                name = buf2.value.decode("utf-8", errors="replace").strip("\x00")
            names.append(name or f"Program {i}")
        # Restore original program if we changed it
        if self._dispatch(_effGetProgram) != cur:
            self._dispatch(_effSetProgram, 0, cur, None, 0.0)
        return names

    def get_program(self) -> int:
        return self._dispatch(_effGetProgram)

    def set_program(self, index: int):
        self._dispatch(_effSetProgram, 0, index, None, 0.0)

    # ── parameter write ───────────────────────────────────────────────────────

    def set_parameter(self, index: int, value: float):
        ae = self._aeffect
        ae.contents.setParameter(ae, index, ctypes.c_float(value))

    # ── native editor ─────────────────────────────────────────────────────────

    @property
    def has_editor(self) -> bool:
        return bool(self._aeffect and self._aeffect.contents.flags & _effFlagsHasEditor)

    def open_editor(self, hwnd: int) -> tuple[int, int]:
        """Open the plugin's native editor inside the given parent HWND.
        Returns the (width, height) the plugin wants for its window."""
        rect_ptr = ctypes.c_void_p(0)
        self._dispatch(_effEditGetRect, 0, 0, ctypes.byref(rect_ptr), 0.0)
        w, h = 400, 300
        if rect_ptr.value:
            r = ctypes.cast(rect_ptr.value, ctypes.POINTER(_ERect)).contents
            cw = r.right - r.left
            ch = r.bottom - r.top
            if cw > 0:
                w = cw
            if ch > 0:
                h = ch
        self._dispatch(_effEditOpen, 0, 0, ctypes.c_void_p(hwnd), 0.0)
        return w, h

    def close_editor(self):
        self._dispatch(_effEditClose, 0, 0, None, 0.0)

    def close(self):
        if self._aeffect:
            self._dispatch(_effMainsChanged, 0, 0, None, 0.0)
            self._dispatch(_effClose, 0, 0, None, 0.0)
            self._aeffect = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ── VstChain ──────────────────────────────────────────────────────────────────

class VstChain:
    """
    Ordered effect chain that accepts both VST2 (.dll) and VST3 (.vst3) plugins.

    Plugins are stored as (kind, plugin_obj, path) tuples and applied in order
    on each process() call so the signal chain always matches the list order.
    """

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self.enabled     = True
        # Each slot: ("vst2", Vst2Plugin, path) | ("vst3", pedalboard_plugin, path)
        self._slots: list[tuple[str, object, str]] = []
        # One single-element Pedalboard per VST3 plugin (maintains plugin state)
        self._boards: dict[int, object] = {}   # id(plugin) -> Pedalboard

    # ── mutation ──────────────────────────────────────────────────────────────

    def add_plugin(self, path: str) -> str | None:
        """Load a VST2 or VST3 plugin. Returns an error string on failure."""
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".dll":
                plugin = Vst2Plugin(path, sample_rate=self.sample_rate,
                                    block_size=1024)
                plugin._path = path
                self._slots.append(("vst2", plugin, path))
            else:
                if not _PEDALBOARD_AVAILABLE:
                    return "pedalboard not installed (required for VST3)"
                plugin = load_plugin(path)
                self._boards[id(plugin)] = Pedalboard([plugin])
                self._slots.append(("vst3", plugin, path))
            return None
        except Exception as e:
            return str(e)

    def remove_plugin(self, index: int):
        if 0 <= index < len(self._slots):
            kind, plugin, _ = self._slots.pop(index)
            if kind == "vst3":
                self._boards.pop(id(plugin), None)
            elif kind == "vst2":
                try:
                    plugin.close()
                except Exception:
                    pass

    def plugin_names(self) -> list[str]:
        names = []
        for kind, plugin, path in self._slots:
            tag = "VST2" if kind == "vst2" else "VST3"
            base = os.path.basename(path)
            names.append(f"[{tag}] {base}")
        return names

    def plugin_parameters(self, index: int) -> dict:
        if index >= len(self._slots):
            return {}
        kind, plugin, _ = self._slots[index]
        try:
            if kind == "vst2":
                return plugin.parameters
            else:
                return {name: getattr(plugin, name)
                        for name in dir(plugin) if not name.startswith("_")}
        except Exception:
            return {}

    # ── preset / parameter / editor access ───────────────────────────────────

    def slot_kind(self, index: int) -> str:
        if 0 <= index < len(self._slots):
            return self._slots[index][0]
        return ""

    def has_editor(self, index: int) -> bool:
        if 0 <= index < len(self._slots):
            kind, plugin, _ = self._slots[index]
            return kind == "vst2" and plugin.has_editor
        return False

    def get_programs(self, index: int) -> list[str]:
        if 0 <= index < len(self._slots):
            kind, plugin, _ = self._slots[index]
            if kind == "vst2":
                try:
                    return plugin.program_names()
                except Exception:
                    pass
        return []

    def get_program(self, index: int) -> int:
        if 0 <= index < len(self._slots):
            kind, plugin, _ = self._slots[index]
            if kind == "vst2":
                try:
                    return plugin.get_program()
                except Exception:
                    pass
        return 0

    def set_program(self, index: int, program: int):
        if 0 <= index < len(self._slots):
            kind, plugin, _ = self._slots[index]
            if kind == "vst2":
                try:
                    plugin.set_program(program)
                except Exception:
                    pass

    def set_parameter(self, slot_index: int, param_key, value: float):
        """Set a parameter. param_key is an int index for VST2, a str name for VST3."""
        if 0 <= slot_index < len(self._slots):
            kind, plugin, _ = self._slots[slot_index]
            try:
                if kind == "vst2":
                    plugin.set_parameter(int(param_key), value)
                else:
                    setattr(plugin, str(param_key), value)
            except Exception:
                pass

    def open_editor(self, index: int, hwnd: int) -> tuple[int, int] | None:
        """Open native editor. Returns (width, height) or None if unavailable."""
        if 0 <= index < len(self._slots):
            kind, plugin, _ = self._slots[index]
            if kind == "vst2" and plugin.has_editor:
                try:
                    return plugin.open_editor(hwnd)
                except Exception:
                    pass
        return None

    def close_editor(self, index: int):
        if 0 <= index < len(self._slots):
            kind, plugin, _ = self._slots[index]
            if kind == "vst2":
                try:
                    plugin.close_editor()
                except Exception:
                    pass

    # ── serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> list[dict]:
        """Return a JSON-safe list describing every loaded plugin and its state."""
        result = []
        for i, (kind, plugin, path) in enumerate(self._slots):
            entry: dict = {"path": path, "kind": kind}
            try:
                if kind == "vst2":
                    entry["program"] = plugin.get_program()
                    ae = plugin._aeffect
                    n = min(ae.contents.numParams, 128)
                    # Store params as indexed list so names don't need to be unique
                    entry["params"] = [
                        round(float(ae.contents.getParameter(ae, j)), 6)
                        for j in range(n)
                    ]
                else:
                    entry["params"] = self.plugin_parameters(i)
            except Exception:
                pass
            result.append(entry)
        return result

    def from_dict(self, data: list[dict]) -> list[str]:
        """Load plugins from serialised data. Returns a list of error strings."""
        errors: list[str] = []
        for entry in data:
            path = entry.get("path", "")
            err = self.add_plugin(path)
            if err:
                errors.append(f"{os.path.basename(path)}: {err}")
                continue
            idx = len(self._slots) - 1
            kind = self._slots[idx][0]
            try:
                if kind == "vst2" and "program" in entry:
                    self.set_program(idx, int(entry["program"]))
                params = entry.get("params", [])
                if kind == "vst2" and isinstance(params, list):
                    for j, v in enumerate(params):
                        self.set_parameter(idx, j, float(v))
                elif kind == "vst3" and isinstance(params, dict):
                    for name, v in params.items():
                        self.set_parameter(idx, name, float(v))
            except Exception:
                pass
        return errors

    # ── processing ────────────────────────────────────────────────────────────

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Apply all plugins in order to float32 (channels, frames) audio."""
        if not self.enabled or not self._slots:
            return audio
        for kind, plugin, path in self._slots:
            try:
                if kind == "vst2":
                    audio = plugin.process(audio)
                else:
                    board = self._boards.get(id(plugin))
                    if board is not None:
                        audio = board(audio, self.sample_rate)
            except Exception as e:
                import os as _os
                print(f"[VstChain] {_os.path.basename(path)}: {type(e).__name__}: {e}")
        return audio
