"""
ASIO capture via direct COM vtable calls.

ASIO drivers self-register in HKLM\\SOFTWARE\\ASIO with their COM CLSID.
We instantiate them via CoCreateInstance (per the ASIO convention, the IID
equals the CLSID) and call the IASIO vtable directly using ctypes.

This works with any installed ASIO driver — GoXLR, ReaRoute, ASIO4ALL, etc.
It does NOT require PortAudio or the Steinberg SDK.
"""

import ctypes
import ctypes.wintypes as wt
import winreg
import queue
import numpy as np

from comtypes import GUID, CoInitialize, CoUninitialize

_ole32 = ctypes.windll.ole32
_ole32.CoCreateInstance.restype = ctypes.c_long

_CLSCTX_INPROC_SERVER = 1

# ── ASIO sample type constants (from Steinberg asio.h) ────────────────────────
_ST_INT16_MSB   = 0
_ST_INT24_MSB   = 1
_ST_INT32_MSB   = 2
_ST_FLOAT32_MSB = 3
_ST_INT32_LSB   = 16
_ST_INT24_LSB   = 17
_ST_INT16_LSB   = 18
_ST_FLOAT32_LSB = 19
_ST_FLOAT64_LSB = 20

# ── ASIO structs ──────────────────────────────────────────────────────────────

class _ChannelInfo(ctypes.Structure):
    _fields_ = [
        ("channel",      ctypes.c_long),
        ("isInput",      ctypes.c_long),
        ("isActive",     ctypes.c_long),
        ("channelGroup", ctypes.c_long),
        ("type",         ctypes.c_long),
        ("name",         ctypes.c_char * 32),
    ]

class _BufferInfo(ctypes.Structure):
    _fields_ = [
        ("isInput",    ctypes.c_long),
        ("channelNum", ctypes.c_long),
        ("buffers",    ctypes.c_void_p * 2),
    ]

# Callback function types (called from the ASIO driver thread)
_BufSwitchT    = ctypes.CFUNCTYPE(None, ctypes.c_long, ctypes.c_long)
_RateChangedT  = ctypes.CFUNCTYPE(None, ctypes.c_double)
_MsgT          = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_long, ctypes.c_long,
                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_double))
_BufSwitchTiT  = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_long, ctypes.c_long)

class _Callbacks(ctypes.Structure):
    _fields_ = [
        ("bufferSwitch",         _BufSwitchT),
        ("sampleRateDidChange",  _RateChangedT),
        ("asioMessage",          _MsgT),
        ("bufferSwitchTimeInfo", _BufSwitchTiT),
    ]


# ── IASIO vtable wrapper ──────────────────────────────────────────────────────

class _IASIO:
    """
    Thin accessor for the IASIO COM vtable.

    IASIO vtable layout (IUnknown at 0-2, then IASIO methods):
      0  QueryInterface
      1  AddRef
      2  Release
      3  init(void* sysRef) -> ASIOBool
      4  getDriverName(char* name)
      5  getDriverVersion() -> long
      6  getErrorMessage(char* string)
      7  start() -> ASIOError
      8  stop() -> ASIOError
      9  getChannels(long* in, long* out) -> ASIOError
      10 getLatencies(long* in, long* out) -> ASIOError
      11 getBufferSize(long* min, long* max, long* pref, long* gran) -> ASIOError
      12 canSampleRate(double) -> ASIOError
      13 getSampleRate(double*) -> ASIOError
      14 setSampleRate(double) -> ASIOError
      15 getClockSources(...)
      16 setClockSource(long) -> ASIOError
      17 getSamplePosition(...)
      18 getChannelInfo(ASIOChannelInfo*) -> ASIOError
      19 createBuffers(ASIOBufferInfo*, long, long, ASIOCallbacks*) -> ASIOError
      20 disposeBuffers() -> ASIOError
      21 controlPanel() -> ASIOError
      22 future(long, void*) -> ASIOError
      23 outputReady() -> ASIOError
    """

    def __init__(self, raw_ptr: int):
        self._p = raw_ptr
        vtbl_addr = ctypes.cast(raw_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
        self._vtbl = ctypes.cast(vtbl_addr, ctypes.POINTER(ctypes.c_void_p * 24)).contents

    def _call(self, slot: int, restype, *argtypes):
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return proto(self._vtbl[slot])

    def init(self, hwnd=None) -> bool:
        fn = self._call(3, ctypes.c_long, ctypes.c_void_p)
        return bool(fn(self._p, hwnd))

    def get_driver_name(self) -> str:
        buf = ctypes.create_string_buffer(64)
        fn = self._call(4, None, ctypes.c_char_p)
        fn(self._p, buf)
        return buf.value.decode("utf-8", errors="replace")

    def get_channels(self) -> tuple[int, int]:
        n_in, n_out = ctypes.c_long(0), ctypes.c_long(0)
        fn = self._call(9, ctypes.c_long,
                        ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_long))
        fn(self._p, ctypes.byref(n_in), ctypes.byref(n_out))
        return n_in.value, n_out.value

    def get_buffer_size(self) -> tuple[int, int, int, int]:
        mn, mx, pref, gran = (ctypes.c_long(0),) * 4
        fn = self._call(11, ctypes.c_long,
                        ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_long),
                        ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_long))
        fn(self._p, ctypes.byref(mn), ctypes.byref(mx),
           ctypes.byref(pref), ctypes.byref(gran))
        return mn.value, mx.value, pref.value, gran.value

    def get_sample_rate(self) -> float:
        rate = ctypes.c_double(0.0)
        fn = self._call(13, ctypes.c_long, ctypes.POINTER(ctypes.c_double))
        fn(self._p, ctypes.byref(rate))
        return rate.value

    def set_sample_rate(self, rate: float) -> int:
        fn = self._call(14, ctypes.c_long, ctypes.c_double)
        return fn(self._p, ctypes.c_double(rate))

    def get_channel_info(self, channel: int, is_input: bool) -> _ChannelInfo:
        info = _ChannelInfo()
        info.channel = channel
        info.isInput = 1 if is_input else 0
        fn = self._call(18, ctypes.c_long, ctypes.POINTER(_ChannelInfo))
        fn(self._p, ctypes.byref(info))
        return info

    def create_buffers(self, buf_arr, num_channels: int,
                       buf_size: int, callbacks: _Callbacks) -> int:
        fn = self._call(19, ctypes.c_long,
                        ctypes.POINTER(_BufferInfo), ctypes.c_long, ctypes.c_long,
                        ctypes.POINTER(_Callbacks))
        return fn(self._p, buf_arr, num_channels, buf_size, ctypes.byref(callbacks))

    def dispose_buffers(self):
        fn = self._call(20, ctypes.c_long)
        fn(self._p)

    def start(self) -> int:
        fn = self._call(7, ctypes.c_long)
        return fn(self._p)

    def stop(self) -> int:
        fn = self._call(8, ctypes.c_long)
        return fn(self._p)

    def release(self):
        fn = self._call(2, ctypes.c_ulong)
        fn(self._p)


# ── Registry enumeration ──────────────────────────────────────────────────────

def list_asio_drivers() -> list[dict]:
    """
    Return [{name, clsid}] for all ASIO drivers registered on this machine.
    Returns an empty list if the ASIO registry key doesn't exist.
    """
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ASIO")
    except FileNotFoundError:
        return []
    drivers = []
    i = 0
    while True:
        try:
            name = winreg.EnumKey(key, i)
            sub = winreg.OpenKey(key, name)
            try:
                clsid = winreg.QueryValueEx(sub, "CLSID")[0]
                drivers.append({"name": name, "clsid": clsid})
            except FileNotFoundError:
                pass
            i += 1
        except OSError:
            break
    return drivers


# ── AsioCapture ───────────────────────────────────────────────────────────────

def _samples_to_float32(raw: bytes, sample_type: int, n: int) -> np.ndarray:
    if sample_type in (_ST_FLOAT32_LSB, _ST_FLOAT32_MSB):
        return np.frombuffer(raw[:n * 4], dtype=np.float32).copy()
    if sample_type in (_ST_INT32_LSB, _ST_INT32_MSB):
        return np.frombuffer(raw[:n * 4], dtype=np.int32).astype(np.float32) / 2_147_483_648.0
    if sample_type in (_ST_INT16_LSB, _ST_INT16_MSB):
        return np.frombuffer(raw[:n * 2], dtype=np.int16).astype(np.float32) / 32_768.0
    if sample_type in (_ST_INT24_LSB, _ST_INT24_MSB):
        # 24-bit packed: expand to int32 by shifting
        b = np.frombuffer(raw[:n * 3], dtype=np.uint8).reshape(n, 3)
        as32 = (b[:, 0].astype(np.int32)
                | (b[:, 1].astype(np.int32) << 8)
                | (b[:, 2].astype(np.int32) << 16))
        # sign-extend from 24 bits
        as32 = np.where(as32 >= 0x800000, as32 - 0x1000000, as32)
        return as32.astype(np.float32) / 8_388_608.0
    # fallback: treat as int32
    return np.frombuffer(raw[:n * 4], dtype=np.int32).astype(np.float32) / 2_147_483_648.0


class AsioCapture:
    """
    Captures audio from an ASIO driver (identified by its registry CLSID)
    and delivers stereo float32 frames via read().

    The driver is initialised on start() and released on stop().
    Audio buffers arrive on the ASIO driver thread via bufferSwitch and are
    converted to float32 before being pushed onto a queue.
    """

    def __init__(self, clsid: str, chunk_frames: int = 1024,
                 sample_rate: int = 48000, num_input_channels: int = 2):
        self._clsid       = clsid
        self._chunk_frames = chunk_frames
        self._target_rate  = float(sample_rate)
        self._num_in       = num_input_channels
        self._queue: queue.Queue = queue.Queue(maxsize=16)

        self._iasio: _IASIO | None = None
        self._buf_arr = None
        self._callbacks: _Callbacks | None = None
        self._cb_refs   = None          # keep callback objects alive
        self._sample_type: int = _ST_INT32_LSB
        self._buf_size: int = 1024
        self._actual_channels: int = 0

    # ── public interface ──────────────────────────────────────────────────────

    def start(self):
        CoInitialize()
        clsid_guid = GUID(self._clsid)
        raw = ctypes.c_void_p()
        hr = _ole32.CoCreateInstance(
            ctypes.byref(clsid_guid),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(clsid_guid),   # IID == CLSID per ASIO convention
            ctypes.byref(raw),
        )
        if hr < 0 or not raw.value:
            raise OSError(f"CoCreateInstance failed: 0x{hr & 0xFFFFFFFF:08X}")

        self._iasio = _IASIO(raw.value)

        if not self._iasio.init(None):
            raise OSError("ASIO driver init() returned false")

        # Try requested sample rate; fall back to driver default
        self._iasio.set_sample_rate(self._target_rate)
        actual_rate = self._iasio.get_sample_rate()
        if actual_rate != self._target_rate:
            self._iasio.set_sample_rate(actual_rate)

        # Buffer size: use driver's preferred size
        _, _, pref, _ = self._iasio.get_buffer_size()
        self._buf_size = pref

        # How many input channels does the driver actually have?
        n_in, _ = self._iasio.get_channels()
        self._actual_channels = min(self._num_in, n_in)
        if self._actual_channels == 0:
            raise OSError("ASIO driver reports no input channels")

        # Get sample type from channel 0
        ch_info = self._iasio.get_channel_info(0, is_input=True)
        self._sample_type = ch_info.type

        # Build buffer info array
        ArrType = _BufferInfo * self._actual_channels
        buf_list = []
        for i in range(self._actual_channels):
            bi = _BufferInfo()
            bi.isInput    = 1
            bi.channelNum = i
            buf_list.append(bi)
        self._buf_arr = ArrType(*buf_list)

        # Build callbacks — keep all closures alive via self._cb_refs
        capture_self = self   # avoid late-binding in closures

        @_BufSwitchT
        def buf_switch(idx, direct):
            capture_self._on_buffer(idx)

        @_RateChangedT
        def rate_changed(rate):
            pass

        @_MsgT
        def asio_msg(sel, val, msg, opt):
            return 0

        @_BufSwitchTiT
        def buf_switch_ti(params, idx, direct):
            capture_self._on_buffer(idx)
            return params

        self._cb_refs = (buf_switch, rate_changed, asio_msg, buf_switch_ti)
        self._callbacks = _Callbacks(buf_switch, rate_changed, asio_msg, buf_switch_ti)

        hr = self._iasio.create_buffers(
            self._buf_arr, self._actual_channels, self._buf_size, self._callbacks
        )
        if hr not in (0, 0x3f4847a0):   # ASE_OK or ASE_SUCCESS
            raise OSError(f"ASIO createBuffers failed: {hr}")

        self._iasio.start()

    def stop(self):
        if self._iasio:
            try:
                self._iasio.stop()
                self._iasio.dispose_buffers()
                self._iasio.release()
            except Exception:
                pass
            self._iasio = None
        self._callbacks = None
        self._cb_refs   = None
        CoUninitialize()

    def read(self) -> np.ndarray:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return np.zeros((2, self._chunk_frames), dtype=np.float32)

    # ── buffer switch callback (ASIO driver thread) ───────────────────────────

    def _on_buffer(self, idx: int):
        n = self._buf_size
        # Determine bytes per sample for this type
        if self._sample_type in (_ST_INT16_LSB, _ST_INT16_MSB):
            bps = 2
        elif self._sample_type in (_ST_INT24_LSB, _ST_INT24_MSB):
            bps = 3
        else:
            bps = 4

        channels_f32: list[np.ndarray] = []
        for i in range(self._actual_channels):
            ptr = self._buf_arr[i].buffers[idx]
            if not ptr:
                channels_f32.append(np.zeros(n, dtype=np.float32))
                continue
            raw = ctypes.string_at(ptr, n * bps)
            channels_f32.append(_samples_to_float32(raw, self._sample_type, n))

        # Mix to stereo
        if len(channels_f32) == 1:
            stereo = np.vstack([channels_f32[0], channels_f32[0]])
        else:
            stereo = np.vstack([channels_f32[0], channels_f32[1]])

        # Chunk into mixer-sized pieces and enqueue
        for start in range(0, n, self._chunk_frames):
            chunk = stereo[:, start:start + self._chunk_frames]
            if chunk.shape[1] < self._chunk_frames:
                chunk = np.pad(chunk, ((0, 0), (0, self._chunk_frames - chunk.shape[1])))
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                pass
