/*
 * pub_streamer_hook.dll
 *
 * Injected into a target audio-rendering process.  Patches the IAudioRenderClient
 * vtable to intercept GetBuffer/ReleaseBuffer, captures the PCM data, and streams
 * it to a Python-side named-pipe server.
 *
 * Pipe name  : \\.\pipe\PubStreamerAudio_<target-pid>
 * Protocol   : On connect, send 20-byte header (5 x uint32 LE):
 *                channels, sample_rate, bits_per_sample, block_align, is_float
 *              Then for each rendered packet:
 *                uint32  num_frames
 *                BYTE[]  num_frames * block_align bytes of audio
 *
 * The DLL keeps a persistent watchdog thread running in the target process.
 * When the pipe is closed (app restarts, pipe breaks), the watchdog reconnects
 * automatically.  Vtable patching happens only once per process lifetime.
 *
 * Build (x64 MSVC, from a vcvars64 shell):
 *   cl /LD /O2 /EHsc hook_audio.cpp /Fe:pub_streamer_hook.dll ole32.lib uuid.lib
 */

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "uuid.lib")

// -- Globals ------------------------------------------------------------------
static HANDLE           g_pipe        = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION g_cs;
static DWORD            g_target_pid  = 0;

static uint32_t g_channels    = 2;
static uint32_t g_sample_rate = 48000;
static uint32_t g_bits        = 32;
static uint32_t g_block_align = 8;
static uint32_t g_is_float    = 1;

// Set to 1 after vtable has been patched; never patch again.
static volatile LONG g_patched = 0;

// -- Original vtable function pointers ----------------------------------------
typedef HRESULT (STDMETHODCALLTYPE *PFN_GetBuffer)
    (IAudioRenderClient*, UINT32, BYTE**);
typedef HRESULT (STDMETHODCALLTYPE *PFN_ReleaseBuffer)
    (IAudioRenderClient*, UINT32, DWORD);

static PFN_GetBuffer     g_orig_GetBuffer     = nullptr;
static PFN_ReleaseBuffer g_orig_ReleaseBuffer = nullptr;

static thread_local BYTE* t_pData = nullptr;

// -- Vtable patcher -----------------------------------------------------------
static void patch_slot(void** vtbl, int slot, void* new_fn, void** out_orig)
{
    void** target = &vtbl[slot];
    DWORD  old_prot;
    if (!VirtualProtect(target, sizeof(void*), PAGE_EXECUTE_READWRITE, &old_prot))
        return;
    if (out_orig)
        *out_orig = *target;
    *target = new_fn;
    VirtualProtect(target, sizeof(void*), old_prot, &old_prot);
}

// -- Hooked GetBuffer ---------------------------------------------------------
static HRESULT STDMETHODCALLTYPE hook_GetBuffer(
    IAudioRenderClient* self, UINT32 numFrames, BYTE** ppData)
{
    HRESULT hr = g_orig_GetBuffer(self, numFrames, ppData);
    t_pData = (SUCCEEDED(hr) && ppData) ? *ppData : nullptr;
    return hr;
}

// -- Hooked ReleaseBuffer -----------------------------------------------------
static HRESULT STDMETHODCALLTYPE hook_ReleaseBuffer(
    IAudioRenderClient* self, UINT32 numFramesWritten, DWORD dwFlags)
{
    BYTE* data = t_pData;
    t_pData = nullptr;

    if (data && numFramesWritten > 0 &&
        g_pipe != INVALID_HANDLE_VALUE &&
        !(dwFlags & AUDCLNT_BUFFERFLAGS_SILENT))
    {
        uint32_t n  = numFramesWritten;
        uint32_t cb = n * g_block_align;
        DWORD    written;
        EnterCriticalSection(&g_cs);
        bool ok = (WriteFile(g_pipe, &n,   sizeof(n), &written, nullptr) &&
                   WriteFile(g_pipe, data, cb,         &written, nullptr));
        if (!ok) {
            CloseHandle(g_pipe);
            g_pipe = INVALID_HANDLE_VALUE;
            // Watchdog thread will detect INVALID_HANDLE_VALUE and reconnect.
        }
        LeaveCriticalSection(&g_cs);
    }

    return g_orig_ReleaseBuffer(self, numFramesWritten, dwFlags);
}

// -- Vtable patch (called once) -----------------------------------------------
// Initialises COM, creates a temporary IAudioClient on the default render
// endpoint to obtain the IAudioRenderClient vtable, patches it, then
// updates the global format fields.  Safe to call from a non-UI thread.
static void do_vtable_patch()
{
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);

    IMMDeviceEnumerator* enumerator = nullptr;
    CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                     __uuidof(IMMDeviceEnumerator), (void**)&enumerator);
    if (!enumerator) goto done;

    IMMDevice* device = nullptr;
    enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &device);
    enumerator->Release();
    if (!device) goto done;

    {
        IAudioClient* client = nullptr;
        device->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&client);
        device->Release();
        if (!client) goto done;

        WAVEFORMATEX* fmt = nullptr;
        if (SUCCEEDED(client->GetMixFormat(&fmt)) && fmt) {
            g_channels    = fmt->nChannels;
            g_sample_rate = fmt->nSamplesPerSec;
            g_bits        = fmt->wBitsPerSample;
            g_block_align = fmt->nBlockAlign;
            g_is_float    = (fmt->wFormatTag == 3 || fmt->wFormatTag == 0xFFFE) ? 1 : 0;

            HRESULT hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED, 0,
                                            2000000, 0, fmt, nullptr);
            CoTaskMemFree(fmt);
            fmt = nullptr;

            if (SUCCEEDED(hr)) {
                IAudioRenderClient* rc = nullptr;
                client->GetService(__uuidof(IAudioRenderClient), (void**)&rc);
                if (rc) {
                    void** vtbl = *reinterpret_cast<void***>(rc);
                    patch_slot(vtbl, 3, (void*)hook_GetBuffer,
                               (void**)&g_orig_GetBuffer);
                    patch_slot(vtbl, 4, (void*)hook_ReleaseBuffer,
                               (void**)&g_orig_ReleaseBuffer);
                    rc->Release();
                }
            }
        } else {
            if (fmt) CoTaskMemFree(fmt);
        }

        client->Release();
    }

done:
    CoUninitialize();
}

// -- Watchdog thread ----------------------------------------------------------
// Runs forever.  Reconnects the pipe whenever g_pipe == INVALID_HANDLE_VALUE.
// Patches the vtable the first time it connects (g_patched guards against repeat).
static DWORD WINAPI watchdog_thread(LPVOID)
{
    wchar_t pipe_name[64];
    swprintf_s(pipe_name, 64, L"\\\\.\\pipe\\PubStreamerAudio_%lu", g_target_pid);

    for (;;) {
        // If pipe is alive, sleep and loop.
        if (g_pipe != INVALID_HANDLE_VALUE) {
            Sleep(200);
            continue;
        }

        // Try to open the named pipe server created by Python.
        HANDLE h = CreateFileW(pipe_name, GENERIC_WRITE, 0, nullptr,
                               OPEN_EXISTING, 0, nullptr);
        if (h == INVALID_HANDLE_VALUE) {
            DWORD err = GetLastError();
            if (err == ERROR_PIPE_BUSY)
                WaitNamedPipeW(pipe_name, 200);
            else
                Sleep(200);
            continue;
        }

        // Connected.  Patch vtable on first connection only.
        if (InterlockedCompareExchange(&g_patched, 1, 0) == 0)
            do_vtable_patch();

        // Send format header so Python knows the stream format.
        uint32_t header[5] = {
            g_channels, g_sample_rate, g_bits, g_block_align, g_is_float
        };
        DWORD written;
        if (!WriteFile(h, header, sizeof(header), &written, nullptr)) {
            CloseHandle(h);
            continue;
        }

        // Make the pipe live.
        EnterCriticalSection(&g_cs);
        g_pipe = h;
        LeaveCriticalSection(&g_cs);
    }
}

// -- DllMain ------------------------------------------------------------------
BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID)
{
    switch (reason) {
    case DLL_PROCESS_ATTACH:
        DisableThreadLibraryCalls(hModule);
        InitializeCriticalSection(&g_cs);
        g_target_pid = GetCurrentProcessId();
        // Launch the persistent watchdog.  If the DLL is already loaded
        // (ref-count increment), this branch is NOT entered — but the
        // existing watchdog thread continues running and will reconnect.
        {
            HANDLE t = CreateThread(nullptr, 0, watchdog_thread, nullptr, 0, nullptr);
            if (t) CloseHandle(t);
        }
        break;

    case DLL_PROCESS_DETACH:
        if (g_pipe != INVALID_HANDLE_VALUE) {
            CloseHandle(g_pipe);
            g_pipe = INVALID_HANDLE_VALUE;
        }
        DeleteCriticalSection(&g_cs);
        break;
    }
    return TRUE;
}
