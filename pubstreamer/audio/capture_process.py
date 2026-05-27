"""
Per-process WASAPI loopback capture using the Windows 10 2004+
AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS API.

This avoids any external virtual-cable dependency by calling
ActivateAudioInterfaceAsync with a process-specific activation struct,
matching the technique used in the OBS win-capture-audio plugin.
"""

import ctypes
import ctypes.wintypes as wintypes
import math
import os
import queue
import struct
import sys
import threading
import time
import numpy as np

from comtypes import GUID, IUnknown, HRESULT, COMMETHOD, CoUninitialize
import comtypes

def _CoInitializeMTA():
    """Initialize COM as MTA (required for WASAPI process loopback threads)."""
    COINIT_MULTITHREADED = 0x0
    hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    # S_OK (0) or S_FALSE (1) = already initialized as MTA — both fine.
    # RPC_E_CHANGED_MODE (-2147417850 / 0x80010106) = already STA — leave it.
    if hr < 0 and hr != -2147417850:
        raise OSError(f"CoInitializeEx(MTA) failed: 0x{hr & 0xFFFFFFFF:08X}")

# ── Windows constants ──────────────────────────────────────────────────────────

AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_SHAREMODE_SHARED = 0

WAVE_FORMAT_PCM         = 0x0001
WAVE_FORMAT_IEEE_FLOAT  = 0x0003
WAVE_FORMAT_EXTENSIBLE  = 0xFFFE

AUDCLNT_BUFFERFLAGS_SILENT = 0x00000002

# SubFormat GUIDs for WAVE_FORMAT_EXTENSIBLE
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = GUID("{00000003-0000-0010-8000-00AA00389B71}")
KSDATAFORMAT_SUBTYPE_PCM        = GUID("{00000001-0000-0010-8000-00AA00389B71}")

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1

# Device string that tells Windows to create a process-loopback endpoint
# Defined in audioclientactivationparams.h as VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "{2eef81be-33fa-4800-9670-1cd474972c3f}"

AUDCLNT_E_BUFFER_OPERATION_PENDING = -0x7FF8FA36  # 0x88890011 -> signed
AUDCLNT_S_BUFFER_EMPTY = 0x8890001  # not HRESULT failure

# ── COM structs ────────────────────────────────────────────────────────────────

class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag",      wintypes.WORD),
        ("nChannels",       wintypes.WORD),
        ("nSamplesPerSec",  wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign",     wintypes.WORD),
        ("wBitsPerSample",  wintypes.WORD),
        ("cbSize",          wintypes.WORD),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    class _Samples(ctypes.Union):
        _fields_ = [
            ("wValidBitsPerSample", wintypes.WORD),
            ("wSamplesPerBlock",    wintypes.WORD),
            ("wReserved",           wintypes.WORD),
        ]
    _fields_ = [
        ("Format",        WAVEFORMATEX),
        ("Samples",       _Samples),
        ("dwChannelMask", wintypes.DWORD),
        ("SubFormat",     GUID),
    ]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId",    wintypes.DWORD),
        ("ProcessLoopbackMode", wintypes.DWORD),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType",      wintypes.DWORD),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class PROPVARIANT(ctypes.Structure):
    class _UNION(ctypes.Union):
        class _BLOB(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", ctypes.c_void_p)]
        _fields_ = [
            ("llVal",   ctypes.c_longlong),
            ("blob",    _BLOB),
        ]
    _fields_ = [
        ("vt",       ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("_union",   _UNION),
    ]
    VT_BLOB = 0x0041

    @classmethod
    def from_blob(cls, data: bytes) -> "PROPVARIANT":
        pv = cls()
        pv.vt = cls.VT_BLOB
        buf = ctypes.create_string_buffer(data)
        pv._union.blob.cbSize = len(data)
        pv._union.blob.pBlobData = ctypes.cast(buf, ctypes.c_void_p)
        pv._buf_ref = buf  # keep alive
        return pv


# ── COM interfaces ─────────────────────────────────────────────────────────────

IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
IID_IActivateAudioInterfaceCompletionHandler = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")


class IAudioCaptureClient(IUnknown):
    _iid_ = IID_IAudioCaptureClient
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], ctypes.POINTER(ctypes.c_char_p), "ppData"),
                  (["out"], ctypes.POINTER(wintypes.UINT), "pNumFramesAvailable"),
                  (["out"], ctypes.POINTER(wintypes.DWORD), "pdwFlags"),
                  (["out"], ctypes.POINTER(ctypes.c_uint64), "pu64DevicePosition"),
                  (["out"], ctypes.POINTER(ctypes.c_uint64), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer",
                  (["in"], wintypes.UINT, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize",
                  (["out"], ctypes.POINTER(wintypes.UINT), "pNumFramesInNextPacket")),
    ]


class IAudioClient(IUnknown):
    _iid_ = IID_IAudioClient
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], wintypes.DWORD, "ShareMode"),
                  (["in"], wintypes.DWORD, "StreamFlags"),
                  (["in"], ctypes.c_longlong, "hnsBufferDuration"),
                  (["in"], ctypes.c_longlong, "hnsPeriodicity"),
                  (["in"], ctypes.POINTER(WAVEFORMATEX), "pFormat"),
                  (["in"], ctypes.POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize",
                  (["out"], ctypes.POINTER(wintypes.UINT), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency",
                  (["out"], ctypes.POINTER(ctypes.c_longlong), "phnsLatency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding",
                  (["out"], ctypes.POINTER(wintypes.UINT), "pNumPaddingFrames")),
        COMMETHOD([], HRESULT, "IsFormatSupported",
                  (["in"], wintypes.DWORD, "ShareMode"),
                  (["in"], ctypes.POINTER(WAVEFORMATEX), "pFormat"),
                  (["out"], ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)), "ppClosestMatch")),
        COMMETHOD([], HRESULT, "GetMixFormat",
                  (["out"], ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["out"], ctypes.POINTER(ctypes.c_longlong), "phnsDefaultDevicePeriod"),
                  (["out"], ctypes.POINTER(ctypes.c_longlong), "phnsMinimumDevicePeriod")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle",
                  (["in"], wintypes.HANDLE, "eventHandle")),
        COMMETHOD([], HRESULT, "GetService",
                  (["in"], ctypes.POINTER(GUID), "riid"),
                  (["out"], ctypes.POINTER(ctypes.c_void_p), "ppv")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = IID_IActivateAudioInterfaceCompletionHandler
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], ctypes.c_void_p, "activateOperation")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], ctypes.POINTER(HRESULT), "activateResult"),
                  (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "activatedInterface")),
    ]


# ── ActivateAudioInterfaceAsync ────────────────────────────────────────────────

_mmdevapi = ctypes.windll.mmdevapi

def _activate_audio_interface_async(device_id: str, iid: GUID, activation_params_pv,
                                     completion_handler) -> IActivateAudioInterfaceAsyncOperation:
    fn = _mmdevapi.ActivateAudioInterfaceAsync
    fn.restype = ctypes.c_long
    fn.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(PROPVARIANT),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    async_op_ptr = ctypes.c_void_p()
    hr = fn(
        device_id,
        ctypes.byref(iid),
        ctypes.byref(activation_params_pv),
        ctypes.cast(completion_handler, ctypes.c_void_p),
        ctypes.byref(async_op_ptr),
    )
    if hr < 0:
        raise OSError(f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08X}")
    op = IActivateAudioInterfaceAsyncOperation()
    op.this = async_op_ptr
    return op


# ── ProcessLoopbackCapture ─────────────────────────────────────────────────────


class ProcessLoopbackCapture:
    """
    Captures per-process audio via the WASAPI process loopback API
    (ActivateAudioInterfaceAsync with AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS).

    Requires Windows 10 2004+ (build 19041+). Works for any target bitness
    and any process integrity level -- no DLL injection required.

    Usage::

        cap = ProcessLoopbackCapture(pid=1234)
        cap.start()
        while running:
            frame = cap.read()   # numpy float32 (channels, chunk_frames) or zeros
        cap.stop()
    """

    def __init__(self, pid: int, sample_rate: int = 48000,
                 channels: int = 2, chunk_frames: int = 1024,
                 include_child_processes: bool = True):
        self.pid               = pid
        self.sample_rate       = sample_rate
        self.channels          = channels
        self.chunk_frames      = chunk_frames
        self._include_children = include_child_processes
        self._queue: queue.Queue            = queue.Queue(maxsize=4)
        self._stop_event                    = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None              = None

    @property
    def mode(self) -> str:
        return "include"

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"plc-{self.pid}")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread = None

    def read(self) -> np.ndarray:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return np.zeros((self.channels, self.chunk_frames), dtype=np.float32)

    def _run(self):
        _CoInitializeMTA()
        try:
            self._capture_loop()
        except Exception as e:
            self.error = str(e)
            print(f"[PLC pid={self.pid}] error: {e}", flush=True)
        finally:
            CoUninitialize()

    def _capture_loop(self):
        loopback_mode = (PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
                         if self._include_children else
                         PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE)
        act_params = AUDIOCLIENT_ACTIVATION_PARAMS()
        act_params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        act_params.ProcessLoopbackParams.TargetProcessId     = self.pid
        act_params.ProcessLoopbackParams.ProcessLoopbackMode = loopback_mode
        pv = PROPVARIANT.from_blob(bytes(act_params))

        # Minimal COM completion handler via a manual vtable.
        # COM layout: object = [vtable_ptr] -> vtable = [QI, AddRef, Release, ActivateCompleted]
        done   = threading.Event()
        result = []  # filled by callback: [activation_hr, audio_client_raw_ptr]

        _QI_T = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p)
        _UL_T = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        _AC_T = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)

        class _Vtbl(ctypes.Structure):
            _fields_ = [("qi", _QI_T), ("ar", _UL_T), ("rel", _UL_T), ("ac", _AC_T)]

        class _Obj(ctypes.Structure):
            _fields_ = [("vtbl", ctypes.POINTER(_Vtbl))]

        def _ac_cb(this, op_ptr):
            try:
                hr_out = ctypes.c_long(0)
                iunk   = ctypes.c_void_p(0)
                vtbl   = ctypes.c_void_p.from_address(op_ptr).value
                fn_ptr = ctypes.c_void_p.from_address(
                    vtbl + 3 * _COM_PTR_SZ).value  # GetActivateResult slot 3
                fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_long),
                                        ctypes.POINTER(ctypes.c_void_p))(fn_ptr)
                fn(op_ptr, ctypes.byref(hr_out), ctypes.byref(iunk))
                result[:] = [hr_out.value, iunk.value if iunk.value else 0]
            except Exception as ex:
                result[:] = [-1, 0]
                print(f"[PLC pid={self.pid}] ActivateCompleted error: {ex}", flush=True)
            finally:
                done.set()
            return 0

        # IUnknown / IActivateAudioInterfaceCompletionHandler / IAgileObject
        # Windows calls QI for IAgileObject to verify the handler is apartment-agile.
        # Without it, ActivateAudioInterfaceAsync returns E_ILLEGAL_METHOD_CALL.
        _IID_IUnknown_b = b'\x00\x00\x00\x00\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46'
        _IID_IAgile_b   = b'\x94\x2B\xEA\x94\xCC\xE9\xE0\x49\xC0\xFF\xEE\x64\xCA\x8F\x5B\x90'
        _IID_IActC_b    = b'\xAB\x49\xD9\x41\x62\x98\x4A\x44\x80\xF6\xC2\x61\x33\x4D\xA5\xEB'
        _HANDLER_IIDS   = frozenset([_IID_IUnknown_b, _IID_IAgile_b, _IID_IActC_b])

        def _qi(this, riid_ptr, ppv_ptr):
            riid = ctypes.string_at(riid_ptr, 16)
            if riid in _HANDLER_IIDS:
                if ppv_ptr:
                    ctypes.c_void_p.from_address(ppv_ptr).value = this
                return 0  # S_OK
            if ppv_ptr:
                ctypes.c_void_p.from_address(ppv_ptr).value = 0
            return 0x80004002  # E_NOINTERFACE

        qi_fn  = _QI_T(_qi)
        ar_fn  = _UL_T(lambda t: 1)
        rel_fn = _UL_T(lambda t: 1)
        ac_fn  = _AC_T(_ac_cb)
        vtbl   = _Vtbl(qi_fn, ar_fn, rel_fn, ac_fn)
        hobj   = _Obj()
        hobj.vtbl   = ctypes.pointer(vtbl)
        handler_ptr = ctypes.c_void_p(ctypes.addressof(hobj))

        iid = IID_IAudioClient
        _activate_audio_interface_async(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, iid, pv, handler_ptr)

        if not done.wait(timeout=5.0):
            raise OSError(f"ActivateAudioInterfaceAsync timed out for pid={self.pid}")
        if not result or result[0] < 0 or not result[1]:
            hr = result[0] if result else -1
            raise OSError(
                f"Process loopback activation failed: 0x{hr & 0xFFFFFFFF:08X}")

        ac_ptr = result[1]  # raw IAudioClient*

        # GetMixFormat [slot 8]
        fmt_ptr = ctypes.c_void_p(0)
        hr = _vtfn(ac_ptr, 8, ctypes.c_long,
                   ctypes.POINTER(ctypes.c_void_p))(ac_ptr, ctypes.byref(fmt_ptr))
        if hr < 0 or not fmt_ptr.value:
            raise OSError(f"GetMixFormat failed: 0x{hr & 0xFFFFFFFF:08X}")

        fmt_raw = fmt_ptr.value
        wfx     = WAVEFORMATEX.from_address(fmt_raw)
        fmt_ch    = wfx.nChannels
        fmt_sr    = wfx.nSamplesPerSec
        fmt_bits  = wfx.wBitsPerSample
        fmt_align = wfx.nBlockAlign
        fmt_float = wfx.wFormatTag == WAVE_FORMAT_IEEE_FLOAT
        if wfx.wFormatTag == WAVE_FORMAT_EXTENSIBLE:
            # GUID.__eq__ is unreliable on ctypes struct fields; compare raw bytes.
            # KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = {00000003-0000-0010-8000-00AA00389B71}
            # SubFormat sits at offset 24 in WAVEFORMATEXTENSIBLE.
            _IEEE_FLOAT_SF = b'\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71'
            fmt_float = ctypes.string_at(fmt_raw + 24, 16) == _IEEE_FLOAT_SF
        print(f"[PLC pid={self.pid}] format ch={fmt_ch} sr={fmt_sr} "
              f"bits={fmt_bits} float={fmt_float}", flush=True)

        # Initialize [slot 3]
        hr = _vtfn(ac_ptr, 3, ctypes.c_long,
                   wintypes.DWORD, wintypes.DWORD,
                   ctypes.c_longlong, ctypes.c_longlong,
                   ctypes.c_void_p, ctypes.c_void_p)(
            ac_ptr, AUDCLNT_SHAREMODE_SHARED, 0,
            2000000, 0, fmt_raw, None)
        _free = ctypes.windll.ole32.CoTaskMemFree
        _free.argtypes = [ctypes.c_void_p]
        _free.restype  = None
        _free(fmt_raw)
        if hr < 0:
            raise OSError(f"IAudioClient::Initialize failed: 0x{hr & 0xFFFFFFFF:08X}")

        # GetService -> IAudioCaptureClient [slot 14]
        cap_ptr = ctypes.c_void_p(0)
        iid_cap = IID_IAudioCaptureClient
        hr = _vtfn(ac_ptr, 14, ctypes.c_long,
                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
            ac_ptr, ctypes.byref(iid_cap), ctypes.byref(cap_ptr))
        if hr < 0 or not cap_ptr.value:
            raise OSError(
                f"GetService(IAudioCaptureClient) failed: 0x{hr & 0xFFFFFFFF:08X}")

        # Start [slot 10]
        _vtfn(ac_ptr, 10, ctypes.c_long)(ac_ptr)
        print(f"[PLC pid={self.pid}] capture started", flush=True)

        cp         = cap_ptr.value
        _accum     = np.zeros((self.channels, 0), dtype=np.float32)
        _stat_pkts = 0
        _stat_peak = 0.0
        _stat_next = time.monotonic() + 10.0

        while not self._stop_event.is_set():
            # GetNextPacketSize [slot 5]
            pkt_sz = wintypes.UINT(0)
            hr = _vtfn(cp, 5, ctypes.c_long,
                       ctypes.POINTER(wintypes.UINT))(cp, ctypes.byref(pkt_sz))
            if hr < 0:
                break
            if pkt_sz.value == 0:
                self._stop_event.wait(0.005)
                continue

            # GetBuffer [slot 3]
            data_ptr   = ctypes.c_char_p(None)
            num_frames = wintypes.UINT(0)
            flags      = wintypes.DWORD(0)
            hr = _vtfn(cp, 3, ctypes.c_long,
                       ctypes.POINTER(ctypes.c_char_p),
                       ctypes.POINTER(wintypes.UINT),
                       ctypes.POINTER(wintypes.DWORD),
                       ctypes.c_void_p, ctypes.c_void_p)(
                cp, ctypes.byref(data_ptr), ctypes.byref(num_frames),
                ctypes.byref(flags), None, None)
            if hr < 0:
                break

            nf = num_frames.value
            if nf > 0 and not (flags.value & AUDCLNT_BUFFERFLAGS_SILENT):
                raw = ctypes.string_at(data_ptr, nf * fmt_align)
                _stat_pkts += 1
                if fmt_float and fmt_bits == 32:
                    samples = np.frombuffer(raw, dtype=np.float32)
                elif fmt_bits == 16:
                    samples = (np.frombuffer(raw, dtype=np.int16)
                               .astype(np.float32) / 32768.0)
                elif fmt_bits == 32:
                    samples = (np.frombuffer(raw, dtype=np.int32)
                               .astype(np.float32) / 2147483648.0)
                else:
                    samples = np.frombuffer(raw, dtype=np.float32)

                incoming = samples.reshape(nf, fmt_ch).T.copy()
                p = float(np.max(np.abs(incoming)))
                if p > _stat_peak:
                    _stat_peak = p
                if fmt_ch == 1 and self.channels == 2:
                    incoming = np.vstack([incoming, incoming])
                elif fmt_ch > self.channels:
                    incoming = incoming[:self.channels, :]

                _accum = np.concatenate((_accum, incoming), axis=1)
                while _accum.shape[1] >= self.chunk_frames:
                    chunk  = _accum[:, :self.chunk_frames]
                    _accum = _accum[:, self.chunk_frames:]
                    try:
                        self._queue.put_nowait(chunk)
                    except queue.Full:
                        try: self._queue.get_nowait()
                        except queue.Empty: pass
                        try: self._queue.put_nowait(chunk)
                        except queue.Full: pass

            # ReleaseBuffer [slot 4]
            _vtfn(cp, 4, ctypes.c_long, wintypes.UINT)(cp, nf)

            if time.monotonic() >= _stat_next:
                db = (f"{20*math.log10(_stat_peak):+.1f}"
                      if _stat_peak > 0 else "-inf")
                print(f"[PLC pid={self.pid}] 10s: pkts={_stat_pkts} peak={db}dBFS",
                      flush=True)
                _stat_pkts = 0; _stat_peak = 0.0
                _stat_next = time.monotonic() + 10.0

        # Stop [slot 11]
        _vtfn(ac_ptr, 11, ctypes.c_long)(ac_ptr)


# ── Process / session enumeration helpers (for UI) ────────────────────────────

import ctypes.wintypes as wt

TH32CS_SNAPPROCESS = 0x00000002

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize",              wt.DWORD),
        ("cntUsage",            wt.DWORD),
        ("th32ProcessID",       wt.DWORD),
        ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID",        wt.DWORD),
        ("cntThreads",          wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase",      ctypes.c_long),
        ("dwFlags",             wt.DWORD),
        ("szExeFile",           ctypes.c_char * 260),
    ]


def list_processes() -> list[dict]:
    """Return [{pid, name}] for all running processes."""
    snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    results = []
    if ctypes.windll.kernel32.Process32First(snapshot, ctypes.byref(entry)):
        while True:
            results.append({
                "pid": entry.th32ProcessID,
                "name": entry.szExeFile.decode("utf-8", errors="replace"),
            })
            if not ctypes.windll.kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    ctypes.windll.kernel32.CloseHandle(snapshot)
    return results


_COM_PTR_SZ = ctypes.sizeof(ctypes.c_void_p)

_user32 = ctypes.windll.user32
_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)

_WIN_DIR_CACHE: str = ""


def _win_dir() -> str:
    global _WIN_DIR_CACHE
    if not _WIN_DIR_CACHE:
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.kernel32.GetWindowsDirectoryW(buf, 260)
        _WIN_DIR_CACHE = buf.value.lower()
    return _WIN_DIR_CACHE


def _get_exe_path(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value.lower() if ok else ""
    finally:
        k32.CloseHandle(h)


def is_user_app(pid: int) -> bool:
    """Return True only for genuine user-facing apps — not Windows system processes.

    Two-stage filter:
    1. Reject if the exe lives under C:\\Windows\\ (TTS engines, audiodg, COM surrogates).
    2. Reject if the process has no visible top-level window (background services).
    """
    path = _get_exe_path(pid)
    if not path:
        return False
    win = _win_dir()
    if path.startswith(win + "\\"):
        return False  # system process

    found = [False]
    target = pid

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return 1
        out = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(out))
        if out.value == target:
            found[0] = True
            return 0
        return 1

    cb = _WNDENUMPROC(_cb)
    _user32.EnumWindows(cb, 0)
    return found[0]


_CLSID_MMDeviceEnumerator  = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnumerator   = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_IID_IAudioSessionManager2 = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
_IID_IAudioSessionControl2 = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")


def _vtfn(obj_addr: int, slot: int, restype, *argtypes):
    """Return a ctypes callable for vtable[slot] on the COM object at obj_addr."""
    vtable  = ctypes.c_void_p.from_address(obj_addr).value
    fn_addr = ctypes.c_void_p.from_address(vtable + slot * _COM_PTR_SZ).value
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn_addr)


def _get_default_session_manager2() -> int:
    """Return raw IAudioSessionManager2* for the default render endpoint, or 0."""
    enumerator = ctypes.c_void_p()
    hr = ctypes.windll.ole32.CoCreateInstance(
        ctypes.byref(_CLSID_MMDeviceEnumerator), None, 1,
        ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(enumerator))
    if hr < 0 or not enumerator.value:
        return 0
    device = ctypes.c_void_p()
    # IMMDeviceEnumerator::GetDefaultAudioEndpoint [slot 4]
    hr = _vtfn(enumerator.value, 4, HRESULT,
               wintypes.DWORD, wintypes.DWORD,
               ctypes.POINTER(ctypes.c_void_p))(
        enumerator.value, 0, 0, ctypes.byref(device))  # eRender=0, eConsole=0
    if hr < 0 or not device.value:
        return 0
    mgr = ctypes.c_void_p()
    # IMMDevice::Activate [slot 3]
    hr = _vtfn(device.value, 3, HRESULT,
               ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p,
               ctypes.POINTER(ctypes.c_void_p))(
        device.value, ctypes.byref(_IID_IAudioSessionManager2), 1, None,
        ctypes.byref(mgr))
    if hr < 0 or not mgr.value:
        return 0
    return mgr.value


def list_audio_sessions(proc_map: "dict[int, str] | None" = None,
                        deduplicate: bool = True) -> list[dict]:
    """
    Return [{pid, name}] for processes that currently have active WASAPI audio
    sessions on ANY active render endpoint.  Deduplicated by executable name,
    keeping the lowest PID (parent) per name so that INCLUDE_TARGET_PROCESS_TREE
    captures all children.  Returns [] on any failure.

    Enumerates all active render endpoints (not just the default) so that apps
    rendering to non-default devices (e.g. a specific GoXLR output or VAC channel)
    are included.  The process loopback API captures per-process audio regardless
    of which device the process renders to, so we only need the PID.

    proc_map: optional pre-fetched {pid: name} dict.  If None, list_processes()
    is called internally.
    """
    DEVICE_STATE_ACTIVE = 1
    eRender = 0

    def _sessions_on_device(device_addr: int, all_procs: dict) -> list[dict]:
        """Return raw (possibly duplicate-name) sessions from one IMMDevice."""
        mgr = ctypes.c_void_p()
        hr = _vtfn(device_addr, 3, HRESULT,
                   ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p,
                   ctypes.POINTER(ctypes.c_void_p))(
            device_addr, ctypes.byref(_IID_IAudioSessionManager2), 1, None,
            ctypes.byref(mgr))
        if hr < 0 or not mgr.value:
            return []

        sess_enum = ctypes.c_void_p()
        hr = _vtfn(mgr.value, 5, HRESULT, ctypes.POINTER(ctypes.c_void_p))(
            mgr, ctypes.byref(sess_enum))
        if hr < 0 or not sess_enum.value:
            return []

        count = ctypes.c_int(0)
        _vtfn(sess_enum.value, 3, HRESULT, ctypes.POINTER(ctypes.c_int))(
            sess_enum, ctypes.byref(count))

        results: list[dict] = []
        for i in range(count.value):
            session = ctypes.c_void_p()
            hr = _vtfn(sess_enum.value, 4, HRESULT,
                       ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                sess_enum, i, ctypes.byref(session))
            if hr < 0 or not session.value:
                continue
            ctrl2 = ctypes.c_void_p()
            hr = _vtfn(session.value, 0, HRESULT,
                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                session, ctypes.byref(_IID_IAudioSessionControl2),
                ctypes.byref(ctrl2))
            if hr < 0 or not ctrl2.value:
                continue
            pid = wintypes.DWORD(0)
            _vtfn(ctrl2.value, 14, HRESULT, ctypes.POINTER(wintypes.DWORD))(
                ctrl2, ctypes.byref(pid))
            if pid.value and pid.value in all_procs:
                results.append({"pid": pid.value, "name": all_procs[pid.value]})
        return results

    _CoInitializeMTA()
    try:
        enumerator = ctypes.c_void_p()
        hr = ctypes.windll.ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MMDeviceEnumerator), None, 1,
            ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(enumerator))
        if hr < 0 or not enumerator.value:
            return []

        # EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)  [slot 3]
        # Returns all active render endpoints, not just the default.
        collection = ctypes.c_void_p()
        hr = _vtfn(enumerator.value, 3, HRESULT,
                   wintypes.DWORD, wintypes.DWORD,
                   ctypes.POINTER(ctypes.c_void_p))(
            enumerator, eRender, DEVICE_STATE_ACTIVE, ctypes.byref(collection))
        if hr < 0 or not collection.value:
            return []

        # IMMDeviceCollection::GetCount [slot 3]
        ep_count = wintypes.UINT(0)
        _vtfn(collection.value, 3, HRESULT, ctypes.POINTER(wintypes.UINT))(
            collection, ctypes.byref(ep_count))

        all_procs = (proc_map if proc_map is not None
                     else {p["pid"]: p["name"] for p in list_processes()})
        raw: list[dict] = []

        for i in range(ep_count.value):
            # IMMDeviceCollection::Item(i)  [slot 4]
            device = ctypes.c_void_p()
            hr = _vtfn(collection.value, 4, HRESULT,
                       wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(
                collection, i, ctypes.byref(device))
            if hr < 0 or not device.value:
                continue
            raw.extend(_sessions_on_device(device.value, all_procs))

        if not deduplicate:
            return sorted(raw, key=lambda x: x["name"].lower())

        # Deduplicate by name, keep lowest PID (parent process) — for UI display.
        by_name: dict[str, dict] = {}
        for p in sorted(raw, key=lambda x: x["pid"]):
            key = p["name"].lower()
            if key not in by_name:
                by_name[key] = p
        return sorted(by_name.values(), key=lambda x: x["name"].lower())

    except Exception as e:
        print(f"[list_audio_sessions] {e}")
        return []
    finally:
        CoUninitialize()


# ── Elevation helpers ─────────────────────────────────────────────────────────

def _needs_elevated_plc(pid: int) -> bool:
    """
    Return True if PLC from the current (non-elevated) caller would capture the
    wrong audio for this pid.

    This covers both UAC-elevated processes (TokenElevation=1) AND processes
    with uiAccess=true in their manifest.  Both cases prevent non-elevated PLC
    from correctly targeting the process audio; both are detected by whether
    OpenProcess with full VM/thread access rights is denied.
    """
    PROCESS_ALL_NEEDED = (
        0x0002 |  # PROCESS_CREATE_THREAD
        0x0008 |  # PROCESS_VM_OPERATION
        0x0010 |  # PROCESS_VM_READ
        0x0020 |  # PROCESS_VM_WRITE
        0x0400    # PROCESS_QUERY_INFORMATION
    )
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_ALL_NEEDED, False, pid)
    if h:
        k32.CloseHandle(h)
        return False  # we have full access -- regular PLC will work
    return True  # access denied -- need elevated PLC bridge


def _caller_is_elevated() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


# ── ElevatedPLCCapture ────────────────────────────────────────────────────────

class ElevatedPLCCapture:
    """
    Captures audio from an elevated process by spawning plc_bridge.py elevated
    (UAC prompt) and receiving PCM over a named pipe.

    Presents the same start()/stop()/read()/error interface as
    ProcessLoopbackCapture.
    """

    def __init__(self, pid: int, sample_rate: int = 48000,
                 channels: int = 2, chunk_frames: int = 1024):
        self.pid          = pid
        self.sample_rate  = sample_rate
        self.channels     = channels
        self.chunk_frames = chunk_frames
        self.error: "str | None" = None

        self._queue       = queue.Queue(maxsize=4)
        self._stop        = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._pipe_handle = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"elev-plc-{self.pid}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        h, self._pipe_handle = self._pipe_handle, None
        if h:
            ctypes.windll.kernel32.CloseHandle(h)

    def read(self) -> np.ndarray:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return np.zeros((self.channels, self.chunk_frames), dtype=np.float32)

    # ── internal ──────────────────────────────────────────────────────────────

    def _run(self):
        try:
            self._setup_and_read()
        except Exception as e:
            self.error = str(e)
            print(f"[ElevatedPLC pid={self.pid}] {e}", flush=True)

    def _setup_and_read(self):
        k32        = ctypes.windll.kernel32
        pipe_name  = f"\\\\.\\pipe\\pubstreamer-plc-{self.pid}"
        bridge_py  = os.path.join(os.path.dirname(__file__), "plc_bridge.py")

        # Create named pipe server (inbound: we read, bridge writes).
        PIPE_ACCESS_INBOUND = 0x00000001
        PIPE_TYPE_BYTE      = 0x00000000
        PIPE_WAIT           = 0x00000000
        INVALID_HANDLE      = wintypes.HANDLE(-1).value

        h = k32.CreateNamedPipeW(
            pipe_name,
            PIPE_ACCESS_INBOUND,
            PIPE_TYPE_BYTE | PIPE_WAIT,
            1,      # max instances
            0,      # out buffer (unused for inbound)
            65536,  # in buffer
            5000,   # default timeout ms
            None)   # default security

        if h == INVALID_HANDLE:
            raise OSError(
                f"CreateNamedPipe failed: {k32.GetLastError()}")

        self._pipe_handle = h

        # Launch plc_bridge.py elevated via ShellExecuteW(runas).
        # lpFile = python interpreter; lpParameters = script + arguments.
        params = (f'"{bridge_py}" {self.pid} "{pipe_name}" '
                  f'{self.sample_rate} {self.channels} {self.chunk_frames}')

        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 0)
        if int(ret) <= 32:
            raise OSError(
                f"ShellExecuteW(runas) failed: code {int(ret)}")

        # Wait for the bridge to connect (blocks until bridge connects or stop).
        print(f"[ElevatedPLC pid={self.pid}] waiting for bridge connection ...",
              flush=True)
        if not k32.ConnectNamedPipe(h, None):
            err = k32.GetLastError()
            if err != 535:  # ERROR_PIPE_CONNECTED -- already connected, ok
                raise OSError(f"ConnectNamedPipe failed: {err}")

        print(f"[ElevatedPLC pid={self.pid}] bridge connected, reading ...",
              flush=True)

        chunk_bytes = self.channels * self.chunk_frames * 4  # float32

        while not self._stop.is_set():
            buf     = ctypes.create_string_buffer(chunk_bytes)
            n_read  = wintypes.DWORD(0)
            ok      = k32.ReadFile(h, buf, chunk_bytes, ctypes.byref(n_read), None)
            if not ok or n_read.value == 0:
                break
            if n_read.value == chunk_bytes:
                arr = np.frombuffer(buf.raw, dtype=np.float32).reshape(
                    self.channels, self.chunk_frames).copy()
                try:
                    self._queue.put_nowait(arr)
                except queue.Full:
                    try: self._queue.get_nowait()
                    except queue.Empty: pass
                    try: self._queue.put_nowait(arr)
                    except queue.Full: pass


# ── WatchedAppCapture ────────────────────────────────────────────────────────

class WatchedAppCapture:
    """Loopback capture that watches for an app by exe name.

    Polls every 2 seconds. When the target exe appears, picks the backend:

      WOW64 (32-bit) + injectable (OpenProcess ALL_ACCESS succeeds):
        LegacyCapture.  Injects audio_hook32.dll to hook WinMM/DirectSound at
        source — needed because 32-bit apps often route audio through compat
        layers that don't appear under their PID in WASAPI sessions.  No UAC.

      Everything else (64-bit, or WOW64 elevated/uiAccess like NVDA):
        ProcessLoopbackCapture.  No injection, no UAC, works at any privilege
        level.  ActivateAudioInterfaceAsync only needs the target PID.
        Note: do NOT inject into 64-bit processes — it can crash them.

    If the app exits, watching resumes automatically.
    """

    def __init__(self, exe_name: str, sample_rate: int = 48000,
                 channels: int = 2, chunk_frames: int = 1024):
        self.exe_name     = exe_name
        self.sample_rate  = sample_rate
        self.channels     = channels
        self.chunk_frames = chunk_frames
        # error is always None; the watcher never gives up permanently
        self.error: "str | None" = None

        self._stop_event = threading.Event()
        self._lock       = threading.Lock()
        self._cap: "ProcessLoopbackCapture | object | None" = None
        self._thread: "threading.Thread | None" = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"watch-{self.exe_name}")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        with self._lock:
            cap, self._cap = self._cap, None
        if cap is not None:
            cap.stop()
        self._thread = None

    def read(self) -> np.ndarray:
        with self._lock:
            cap = self._cap
        if cap is None:
            return np.zeros((self.channels, self.chunk_frames), dtype=np.float32)
        return cap.read()

    def _run(self):
        print(f"[WatchedAppCapture] watching for '{self.exe_name}'", flush=True)
        own_pid = os.getpid()
        while not self._stop_event.wait(2.0):
            with self._lock:
                cap = self._cap
            # If the current capture died (app exited or error), drop it and resume watching
            if cap is not None and cap.error:
                print(f"[WatchedAppCapture '{self.exe_name}'] capture lost ({cap.error}) -- re-watching",
                      flush=True)
                cap.stop()
                with self._lock:
                    if self._cap is cap:
                        self._cap = None
                cap = None
            if cap is not None:
                continue  # actively capturing, nothing to do

            # Find the process first so we can check its bitness.
            try:
                procs = list_processes()
            except Exception:
                continue
            target_proc = next(
                (p for p in procs
                 if p["name"].lower() == self.exe_name.lower()
                 and p["pid"] != own_pid),
                None,
            )
            if target_proc is None:
                continue  # not running yet

            pid = target_proc["pid"]

            # Decide capture backend.
            #
            # Injectable (OpenProcess ALL_ACCESS succeeds):
            #   LegacyCapture.  Injects audio_hook32.dll (WOW64) or
            #   audio_hook64.dll (64-bit) to hook WinMM/DirectSound/WASAPI at
            #   source.  GoXLR intercepts the WASAPI render path before the
            #   loopback buffer, so PLC returns mic/monitor mix on this system.
            #   DLL injection is the only way to get actual app audio.
            #
            # Non-injectable (elevated / uiAccess like NVDA):
            #   ProcessLoopbackCapture.  No injection possible.
            _k32 = ctypes.windll.kernel32
            _PROCESS_ALL_ACCESS = 0x1FFFFF
            use_legacy = False
            _wow = wintypes.BOOL(0)
            _hq = _k32.OpenProcess(0x1000, False, pid)
            if _hq:
                _k32.IsWow64Process(_hq, ctypes.byref(_wow))
                _k32.CloseHandle(_hq)

            hi = _k32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
            if hi:
                _k32.CloseHandle(hi)
                use_legacy = True

            if use_legacy:
                bits = "WOW64" if _wow.value else "64-bit"
                print(f"[WatchedAppCapture '{self.exe_name}'] pid={pid}, "
                      f"{bits}+injectable, starting LegacyCapture", flush=True)
                from .capture_legacy import LegacyCapture
                new_cap = LegacyCapture(
                    pid=pid,
                    sample_rate=self.sample_rate,
                    channels=self.channels,
                    chunk_frames=self.chunk_frames,
                )
            else:
                print(f"[WatchedAppCapture '{self.exe_name}'] pid={pid}, "
                      f"non-injectable, starting ProcessLoopbackCapture", flush=True)
                new_cap = ProcessLoopbackCapture(
                    pid=pid,
                    sample_rate=self.sample_rate,
                    channels=self.channels,
                    chunk_frames=self.chunk_frames,
                    include_child_processes=True,
                )

            new_cap.start()
            with self._lock:
                self._cap = new_cap

        print(f"[WatchedAppCapture] '{self.exe_name}' stopped", flush=True)
