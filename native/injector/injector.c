/*
 * injector.c — Minimal DLL injector.
 *
 * Usage: injector.exe <pid> <absolute_dll_path>
 *
 * Compiles as a 32-bit executable so it can inject a 32-bit DLL into a
 * WOW64 (32-bit) process.  The Python host spawns this binary when it
 * detects that the target process is 32-bit; for 64-bit targets it
 * performs injection directly via ctypes.
 *
 * Technique: CreateRemoteThread(LoadLibraryA) — documented on MSDN under
 * CreateRemoteThread and described in Richter "Windows via C/C++" ch. 22.
 *
 * Exit codes:
 *   0  success
 *   1  bad arguments
 *   2  OpenProcess failed
 *   3  VirtualAllocEx failed
 *   4  WriteProcessMemory failed
 *   5  CreateRemoteThread failed
 *   6  thread wait / GetExitCodeThread failure
 *   7  LoadLibrary returned NULL inside target (DLL init failed)
 */

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char *argv[])
{
    if (argc < 3) {
        fprintf(stderr, "usage: injector.exe <pid> <dll_path>\n");
        return 1;
    }

    DWORD       pid      = (DWORD)atoi(argv[1]);
    const char *dll_path = argv[2];
    SIZE_T      path_len = strlen(dll_path) + 1;

    HANDLE hProc = OpenProcess(
        PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION |
        PROCESS_VM_WRITE | PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
        FALSE, pid);
    if (!hProc) {
        fprintf(stderr, "OpenProcess(%u) failed: %lu\n", pid, GetLastError());
        return 2;
    }

    /* Allocate memory in the target for the DLL path string. */
    LPVOID remote_str = VirtualAllocEx(
        hProc, NULL, path_len, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remote_str) {
        fprintf(stderr, "VirtualAllocEx failed: %lu\n", GetLastError());
        CloseHandle(hProc);
        return 3;
    }

    if (!WriteProcessMemory(hProc, remote_str, dll_path, path_len, NULL)) {
        fprintf(stderr, "WriteProcessMemory failed: %lu\n", GetLastError());
        VirtualFreeEx(hProc, remote_str, 0, MEM_RELEASE);
        CloseHandle(hProc);
        return 4;
    }

    /* LoadLibraryA is at the same address in all 32-bit processes on the
     * same OS session because kernel32.dll is loaded at a fixed base by ASLR
     * at boot time and shared across all processes.                          */
    LPTHREAD_START_ROUTINE load_lib =
        (LPTHREAD_START_ROUTINE)GetProcAddress(
            GetModuleHandleA("kernel32.dll"), "LoadLibraryA");

    HANDLE hThread = CreateRemoteThread(
        hProc, NULL, 0, load_lib, remote_str, 0, NULL);
    if (!hThread) {
        fprintf(stderr, "CreateRemoteThread failed: %lu\n", GetLastError());
        VirtualFreeEx(hProc, remote_str, 0, MEM_RELEASE);
        CloseHandle(hProc);
        return 5;
    }

    WaitForSingleObject(hThread, 10000);

    DWORD exit_code = 0;
    if (!GetExitCodeThread(hThread, &exit_code)) {
        CloseHandle(hThread);
        VirtualFreeEx(hProc, remote_str, 0, MEM_RELEASE);
        CloseHandle(hProc);
        return 6;
    }

    CloseHandle(hThread);
    VirtualFreeEx(hProc, remote_str, 0, MEM_RELEASE);
    CloseHandle(hProc);

    /* exit_code is the return value of LoadLibraryA, which is the HMODULE.
     * A NULL return means the DLL failed to load.                          */
    if (!exit_code) {
        fprintf(stderr, "LoadLibraryA returned NULL in target process\n");
        return 7;
    }

    return 0;
}
