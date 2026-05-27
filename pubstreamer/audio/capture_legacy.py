"""
capture_legacy.py — Audio capture for processes that use pre-WASAPI audio APIs
(WinMM waveOut, legacy DirectSound).

How it works
------------
A small hook DLL (audio_hook32.dll / audio_hook64.dll) is injected into the
target process via CreateRemoteThread + LoadLibraryA.  The DLL patches
waveOutWrite and IDirectSoundBuffer::Unlock so that every audio frame the
application renders is also written into a named shared-memory ring buffer.
This module opens that ring buffer, reads frames from it, and presents the
same read() interface as ProcessLoopbackCapture.

Build
-----
Compile the DLLs and injector32.exe from native/ using CMake (see
native/audio_hook/CMakeLists.txt).  Place the outputs in native/dist/:
    audio_hook32.dll   — injected into 32-bit (WOW64) target processes
    audio_hook64.dll   — injected into 64-bit target processes
    injector32.exe     — 32-bit helper spawned by Python for WOW64 targets

Shared memory protocol (matches shmem.h)
-----------------------------------------
    Offset  Size  Field
         0     4  magic            (0x50534155 = 'PSAU')
         4     4  version          (1)
         8     4  channels         (1 or 2; filled by DLL on first audio)
        12     4  sample_rate      (e.g. 48000)
        16     4  write_pos        (absolute frame counter, atomic)
        20     4  wasapi_step      (WASAPI probe progress 0-8)
        24     4  format_tag       (WAVE_FORMAT_PCM=1, IEEE_FLOAT=3, EXTENSIBLE=0xFFFE)
        28     4  bits_per_sample  (wBitsPerSample from WAVEFORMATEX)
        32     *  ring             (write_pos % PS_RING_FRAMES) * channels float32
"""

import ctypes
import ctypes.wintypes as wt
import math
import mmap
import os
import queue
import struct
import subprocess
import threading
import time

import numpy as np

# ── constants matching shmem.h ──────────────────────────────────────────────
_SHMEM_NAME_FMT  = "Local\\pubstreamer-audio-{pid}"
_SHMEM_MAGIC     = 0x50534155
_SHMEM_VER       = 1
_RING_FRAMES     = 32768
_MAX_CHANNELS    = 2
_HEADER_FMT      = "<IIIIIIII"   # magic ver ch sr write_pos wasapi_step format_tag bits
_HEADER_SIZE     = struct.calcsize(_HEADER_FMT)
_SHMEM_SIZE      = _HEADER_SIZE + _RING_FRAMES * _MAX_CHANNELS * 4  # 4 = sizeof float

# ── native binary locations ────────────────────────────────────────────────
_HERE    = os.path.dirname(os.path.abspath(__file__))
_DIST    = os.path.normpath(os.path.join(_HERE, "..", "..", "native", "dist"))
_DLL32   = os.path.join(_DIST, "audio_hook32.dll")
_DLL64   = os.path.join(_DIST, "audio_hook64.dll")
_INJ32   = os.path.join(_DIST, "injector32.exe")

# ── Win32 helpers ──────────────────────────────────────────────────────────
_k32 = ctypes.windll.kernel32

# Functions that return or receive pointer-sized values must have correct
# restype/argtypes.  Without them ctypes defaults to c_int (32-bit), which
# silently truncates 64-bit addresses — causing wrong remote-thread start
# addresses and OverflowError when passing the truncated value onward.
_k32.OpenProcess.restype        = ctypes.c_void_p
_k32.OpenProcess.argtypes       = [wt.DWORD, wt.BOOL, wt.DWORD]

_k32.VirtualAllocEx.restype     = ctypes.c_void_p
_k32.VirtualAllocEx.argtypes    = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_size_t, wt.DWORD, wt.DWORD]

_k32.VirtualFreeEx.restype      = wt.BOOL
_k32.VirtualFreeEx.argtypes     = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_size_t, wt.DWORD]

_k32.WriteProcessMemory.restype  = wt.BOOL
_k32.WriteProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.POINTER(ctypes.c_size_t)]

_k32.GetModuleHandleA.restype   = ctypes.c_void_p
_k32.GetModuleHandleA.argtypes  = [ctypes.c_char_p]

_k32.GetProcAddress.restype     = ctypes.c_void_p
_k32.GetProcAddress.argtypes    = [ctypes.c_void_p, ctypes.c_char_p]

_k32.CreateRemoteThread.restype  = ctypes.c_void_p
_k32.CreateRemoteThread.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t, ctypes.c_void_p,
                                     ctypes.c_void_p, wt.DWORD,
                                     ctypes.c_void_p]

_k32.WaitForSingleObject.restype  = wt.DWORD
_k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]

_k32.GetExitCodeThread.restype   = wt.BOOL
_k32.GetExitCodeThread.argtypes  = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]

_k32.CloseHandle.restype         = wt.BOOL
_k32.CloseHandle.argtypes        = [ctypes.c_void_p]

PROCESS_ALL_ACCESS              = 0x1F0FFF
PROCESS_QUERY_INFORMATION       = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT               = 0x1000
MEM_RESERVE              = 0x2000
MEM_RELEASE              = 0x8000
PAGE_READWRITE           = 0x04
FILE_MAP_READ            = 0x0004


def _is_wow64(pid: int) -> bool:
    """Return True if the process is a 32-bit WOW64 process."""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    result = wt.BOOL(False)
    ctypes.windll.kernel32.IsWow64Process(h, ctypes.byref(result))
    _k32.CloseHandle(h)
    return bool(result.value)


def _inject_dll(pid: int, dll_path: str, wow64: bool) -> bool:
    """
    Inject dll_path into the process identified by pid.
    For WOW64 (32-bit) targets, spawn injector32.exe (non-elevated first,
    then elevated via ShellExecuteW runas if access is denied).
    For 64-bit targets, inject directly from Python via ctypes.
    Returns True on success.
    """
    if wow64:
        if not os.path.exists(_INJ32):
            raise FileNotFoundError(
                f"injector32.exe not found at {_INJ32}. "
                "Build native/ with CMake (x86 configuration).")

        # First try without elevation.
        result = subprocess.run(
            [_INJ32, str(pid), dll_path],
            capture_output=True, timeout=15)
        if result.returncode == 0:
            return True

        # If OpenProcess was denied (exit code 2), try elevated injection.
        # The hook DLL creates shared memory with a NULL DACL so we can read
        # it even though the DLL runs in the elevated target process.
        if result.returncode == 2:
            print(f"[inject] non-elevated injection denied for pid={pid}; "
                  f"trying elevated (UAC prompt expected)", flush=True)
            params = f'"{_INJ32}" {pid} "{dll_path}"'
            SEE_MASK_NOCLOSEPROCESS = 0x40
            import ctypes.wintypes as _wt

            class SHELLEXECUTEINFOW(ctypes.Structure):
                _fields_ = [
                    ("cbSize",       ctypes.c_ulong),
                    ("fMask",        ctypes.c_ulong),
                    ("hwnd",         _wt.HWND),
                    ("lpVerb",       ctypes.c_wchar_p),
                    ("lpFile",       ctypes.c_wchar_p),
                    ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory",  ctypes.c_wchar_p),
                    ("nShow",        ctypes.c_int),
                    ("hInstApp",     _wt.HINSTANCE),
                    ("lpIDList",     ctypes.c_void_p),
                    ("lpClass",      ctypes.c_wchar_p),
                    ("hkeyClass",    _wt.HKEY),
                    ("dwHotKey",     ctypes.c_ulong),
                    ("hIcon",        _wt.HANDLE),
                    ("hProcess",     _wt.HANDLE),
                ]

            sei = SHELLEXECUTEINFOW()
            sei.cbSize       = ctypes.sizeof(sei)
            sei.fMask        = SEE_MASK_NOCLOSEPROCESS
            sei.lpVerb       = "runas"
            sei.lpFile       = _INJ32
            sei.lpParameters = f'{pid} "{dll_path}"'
            sei.nShow        = 0  # SW_HIDE

            if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
                return False

            # Wait for the elevated injector to finish (up to 15 s).
            if sei.hProcess:
                ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 15000)
                ec = _wt.DWORD(0)
                ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(ec))
                ctypes.windll.kernel32.CloseHandle(sei.hProcess)
                return ec.value == 0
            # ShellExecuteExW succeeded but no process handle returned (can
            # happen when the DLL is already loaded — Windows returns the
            # cached module without spawning a visible injector process).
            # Best effort: give the injector a moment then assume success.
            time.sleep(2.0)
            return True
        return False

    # 64-bit injection directly from Python
    if not os.path.exists(dll_path):
        raise FileNotFoundError(f"Hook DLL not found: {dll_path}")

    path_bytes = dll_path.encode("utf-8") + b"\x00"
    hProc = _k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not hProc:
        return False
    try:
        remote = _k32.VirtualAllocEx(
            hProc, None, len(path_bytes),
            MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not remote:
            return False
        written = ctypes.c_size_t(0)
        _k32.WriteProcessMemory(
            hProc, remote, path_bytes, len(path_bytes),
            ctypes.byref(written))
        load_lib = _k32.GetProcAddress(
            _k32.GetModuleHandleA(b"kernel32.dll"), b"LoadLibraryA")
        hThread = _k32.CreateRemoteThread(
            hProc, None, 0, load_lib, remote, 0, None)
        if not hThread:
            _k32.VirtualFreeEx(hProc, remote, 0, MEM_RELEASE)
            return False
        _k32.WaitForSingleObject(hThread, 10000)
        ec = wt.DWORD(0)
        _k32.GetExitCodeThread(hThread, ctypes.byref(ec))
        _k32.CloseHandle(hThread)
        _k32.VirtualFreeEx(hProc, remote, 0, MEM_RELEASE)
        return bool(ec.value)
    finally:
        _k32.CloseHandle(hProc)


# ── LegacyCapture ──────────────────────────────────────────────────────────

class LegacyCapture:
    """
    Captures audio from a process that uses WinMM or legacy DirectSound by
    injecting a hook DLL and reading from the resulting shared memory ring.

    Presents the same interface as ProcessLoopbackCapture: start(), stop(), read().
    """

    def __init__(self, pid: int, sample_rate: int = 48000,
                 channels: int = 2, chunk_frames: int = 1024):
        self.pid          = pid
        self.sample_rate  = sample_rate
        self.channels     = channels
        self.chunk_frames = chunk_frames
        self.error: str | None = None

        self._stop_event  = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=4)
        self._thread: threading.Thread | None = None
        self._shmem_mm: mmap.mmap | None = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"legacy-{self.pid}")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread = None
        self._close_shmem()

    def read(self) -> np.ndarray:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return np.zeros((self.channels, self.chunk_frames), dtype=np.float32)

    # ── internal ────────────────────────────────────────────────────────────

    def _run(self):
        try:
            self._inject_and_capture()
        except Exception as e:
            self.error = str(e)
            print(f"[LegacyCapture pid={self.pid}] error: {e}", flush=True)

    def _inject_and_capture(self):
        wow64  = _is_wow64(self.pid)
        dll    = _DLL32 if wow64 else _DLL64
        suffix = "32" if wow64 else "64"
        print(f"[LegacyCapture pid={self.pid}] "
              f"{'WOW64' if wow64 else '64-bit'}, injecting {dll}", flush=True)

        if not _inject_dll(self.pid, dll, wow64):
            raise RuntimeError(
                f"Injection failed for pid={self.pid}. "
                "Check that audio_hook{suffix}.dll and (for 32-bit) injector32.exe "
                "are present in native/dist/ and that the process is still running.")

        # Wait for the DLL to create the shared memory (up to 3 s).
        shmem_name = _SHMEM_NAME_FMT.format(pid=self.pid)
        mm = None
        for _ in range(30):
            try:
                mm = _open_shmem(shmem_name)
                break
            except OSError:
                time.sleep(0.1)
        if mm is None:
            raise RuntimeError(
                f"Shared memory '{shmem_name}' never appeared. "
                "DLL may have failed to initialise.")

        self._shmem_mm = mm
        print(f"[LegacyCapture pid={self.pid}] shared memory open, reading …",
              flush=True)

        self._read_loop(mm)

    def _read_loop(self, mm: mmap.mmap):
        """Poll the ring buffer and emit chunk_frames-sized numpy arrays."""
        accum    = np.zeros((0,), dtype=np.float32)
        last_pos = 0
        src_rate = 0   # filled from shmem on first valid header
        interval = self.chunk_frames / self.sample_rate * 0.5
        _exit_check = 0   # counter for periodic process-alive checks

        # Request 1 ms system timer resolution so time.sleep() wakes closer
        # to the requested interval rather than the default 15.6 ms quantum.
        _winmm = ctypes.windll.winmm
        _winmm.timeBeginPeriod(1)
        try:
            while not self._stop_event.is_set():
                # Every ~100 iterations (~2 s at 48 kHz/1024 chunk), check
                # whether the target process is still alive.  If it has exited
                # set error so WatchedAppCapture can re-watch and re-inject.
                _exit_check += 1
                if _exit_check >= 100:
                    _exit_check = 0
                    hProc = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                             False, self.pid)
                    if not hProc:
                        self.error = f"process pid={self.pid} exited"
                        break
                    _k32.CloseHandle(hProc)
                mm.seek(0)
                hdr = mm.read(_HEADER_SIZE)
                magic, ver, ch, sr, write_pos = struct.unpack_from(_HEADER_FMT, hdr)[:5]

                if magic != _SHMEM_MAGIC or ver != _SHMEM_VER:
                    time.sleep(0.01)
                    continue

                # Wait until the DLL fills in format fields.
                if ch == 0 or sr == 0:
                    time.sleep(0.01)
                    continue

                # Latch the source sample rate and adjust the poll interval to match.
                if src_rate != sr:
                    src_rate = sr
                    interval = self.chunk_frames / src_rate * 0.5

                if last_pos == 0:
                    last_pos = write_pos  # sync start position

                available = (write_pos - last_pos) & 0xFFFFFFFF
                if available == 0:
                    time.sleep(interval)
                    continue

                # Cap to ring size to avoid reading stale data on a slow poll.
                if available > _RING_FRAMES:
                    last_pos = (write_pos - _RING_FRAMES) & 0xFFFFFFFF
                    available = _RING_FRAMES

                # Read frames from ring (handle wrap-around).
                frames = self._read_ring(mm, last_pos, available, ch)
                last_pos = (last_pos + available) & 0xFFFFFFFF

                # Mix down / upmix channels to match self.channels.
                frames = _remix(frames, ch, self.channels)

                # Resample to the target rate if the game runs at a different rate.
                if src_rate != self.sample_rate:
                    frames = _resample(frames, src_rate, self.sample_rate)

                # Interleave channels so the accumulator layout matches
                # reshape(chunk_frames, channels) used when cutting chunks.
                # frames shape is (dst_ch, n); .T gives (n, dst_ch); flatten
                # gives [ch0_f0, ch1_f0, ch0_f1, ch1_f1, ...].
                accum = np.concatenate([accum, frames.T.flatten()])

                chunk_samples = self.chunk_frames * self.channels
                while len(accum) >= chunk_samples:
                    # Interleaved accum → (chunk_frames, channels) → (channels, chunk_frames)
                    chunk = accum[:chunk_samples].reshape(
                        self.chunk_frames, self.channels).T.copy()
                    accum = accum[chunk_samples:]
                    try:
                        self._queue.put_nowait(chunk)
                    except queue.Full:
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._queue.put_nowait(chunk)
                        except queue.Full:
                            pass
                # No sleep here — immediately re-poll for more data.
        finally:
            _winmm.timeEndPeriod(1)

    @staticmethod
    def _read_ring(mm: mmap.mmap, start: int, n: int, ch: int) -> np.ndarray:
        """Read n frames from the ring, returning shape (ch, n) float32.

        Uses one or two bulk mmap reads instead of per-frame seeks to
        avoid Python-loop overhead that causes jitter at 48 kHz.
        """
        ring_offset = _HEADER_SIZE
        start_idx   = start % _RING_FRAMES
        to_end      = _RING_FRAMES - start_idx

        if n <= to_end:
            mm.seek(ring_offset + start_idx * ch * 4)
            raw = mm.read(n * ch * 4)
        else:
            mm.seek(ring_offset + start_idx * ch * 4)
            raw  = mm.read(to_end * ch * 4)
            mm.seek(ring_offset)
            raw += mm.read((n - to_end) * ch * 4)

        arr = np.frombuffer(raw, dtype=np.float32).copy()
        if len(arr) < n * ch:
            arr = np.concatenate(
                [arr, np.zeros(n * ch - len(arr), dtype=np.float32)])
        # Sanitize: replace NaN/Inf with 0 then clamp to [-1, 1].
        # Rare garbage values from threading races in GetBuffer must not reach
        # the mixer.
        np.nan_to_num(arr, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        np.clip(arr, -1.0, 1.0, out=arr)
        # arr is interleaved (n, ch); transpose to planar (ch, n)
        return arr.reshape(n, ch).T.copy()

    def _close_shmem(self):
        if self._shmem_mm:
            try:
                self._shmem_mm.close()
            except Exception:
                pass
            self._shmem_mm = None


def _open_shmem(name: str) -> mmap.mmap:
    """Open a named file mapping created by the hook DLL.

    mmap.mmap(-1, size, tagname=name) is the correct Windows path: Python
    calls CreateFileMappingW internally, which returns the existing mapping
    when the name already exists, then MapViewOfFile with FILE_MAP_READ.
    Passing a raw Win32 HANDLE as the fileno argument is wrong because
    Python treats that integer as a C runtime file descriptor.
    """
    return mmap.mmap(-1, _SHMEM_SIZE, tagname=name, access=mmap.ACCESS_READ)


def _remix(frames: np.ndarray, src_ch: int, dst_ch: int) -> np.ndarray:
    """Remix (src_ch, n) → (dst_ch, n)."""
    if src_ch == dst_ch:
        return frames
    if dst_ch == 1 and src_ch == 2:
        return ((frames[0] + frames[1]) * 0.5).reshape(1, -1)
    if dst_ch == 2 and src_ch == 1:
        return np.vstack([frames[0], frames[0]])
    # Truncate or zero-pad for unusual channel counts.
    if dst_ch < src_ch:
        return frames[:dst_ch]
    pad = np.zeros((dst_ch - src_ch, frames.shape[1]), dtype=np.float32)
    return np.vstack([frames, pad])


def _resample(frames: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resample (ch, n_src) → (ch, n_dst)."""
    if src_rate == dst_rate:
        return frames
    ch, n = frames.shape
    n_out = max(1, int(round(n * dst_rate / src_rate)))
    x_in  = np.arange(n, dtype=np.float64)
    x_out = np.linspace(0.0, n - 1, n_out)
    out   = np.empty((ch, n_out), dtype=np.float32)
    for c in range(ch):
        out[c] = np.interp(x_out, x_in, frames[c])
    return out
