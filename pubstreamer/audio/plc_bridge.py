"""
plc_bridge.py -- Elevated PLC bridge subprocess.

Launched by ElevatedPLCCapture (via ShellExecuteW runas) when the target
process is elevated and the caller is not.  Runs ProcessLoopbackCapture on
the target PID and writes raw float32 PCM to a named pipe that the
non-elevated parent reads.

Usage (spawned automatically -- not meant to be run by hand):
    python plc_bridge.py <pid> <pipe_name> <sample_rate> <channels> <chunk_frames>
"""

import sys, os, ctypes, ctypes.wintypes as wt, time

def main():
    if len(sys.argv) < 6:
        sys.exit(1)

    pid        = int(sys.argv[1])
    pipe_name  = sys.argv[2]
    sample_rate  = int(sys.argv[3])
    channels     = int(sys.argv[4])
    chunk_frames = int(sys.argv[5])

    # Add the Pub-Streamer root to sys.path (two levels up from this file).
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    from pubstreamer.audio.capture_process import ProcessLoopbackCapture

    k32 = ctypes.windll.kernel32
    GENERIC_WRITE      = 0x40000000
    OPEN_EXISTING      = 3
    INVALID_HANDLE     = wt.HANDLE(-1).value

    # Connect to the named pipe created by the non-elevated parent.
    h = INVALID_HANDLE
    for _ in range(30):
        h = k32.CreateFileW(pipe_name, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        if h != INVALID_HANDLE:
            break
        k32.WaitNamedPipeW(pipe_name, 2000)

    if h == INVALID_HANDLE:
        sys.exit(2)

    cap = ProcessLoopbackCapture(
        pid=pid, sample_rate=sample_rate,
        channels=channels, chunk_frames=chunk_frames,
        include_child_processes=True)
    cap.start()
    time.sleep(0.5)

    if cap.error:
        k32.CloseHandle(h)
        sys.exit(3)

    chunk_bytes = channels * chunk_frames * 4  # float32

    while True:
        chunk = cap.read()   # (channels, chunk_frames) float32
        data  = chunk.tobytes()
        written = wt.DWORD(0)
        ok = k32.WriteFile(h, data, len(data), ctypes.byref(written), None)
        if not ok or written.value != len(data):
            break

    k32.CloseHandle(h)
    cap.stop()


if __name__ == "__main__":
    main()
