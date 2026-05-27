/*
 * dllmain.c — DLL entry point.
 *
 * DLL_PROCESS_ATTACH : create shared memory, install hooks, set magic.
 * DLL_PROCESS_DETACH : remove hooks, unmap and close shared memory.
 */

#include "shmem.h"
#include "hook_engine.h"
#include <windows.h>
#include <stdio.h>

/* Shared memory globals — referenced by hook_winmm.c and hook_dsound.c. */
HANDLE        g_shmem_handle = NULL;
PsAudioShmem *g_shmem        = NULL;
float        *g_ring          = NULL;  /* interleaved float32 ring buffer */

/* Forward declarations (defined in hook_winmm.c / hook_dsound.c / hook_wasapi.c). */
void hook_winmm_install(void);
void hook_winmm_remove(void);
void hook_dsound_install(void);
void hook_dsound_remove(void);
void hook_dsound_probe_async(void);
void hook_wasapi_probe_async(void);

static void shmem_create(DWORD pid)
{
    char name[64];
    _snprintf_s(name, sizeof(name), _TRUNCATE, PS_SHMEM_NAME, (unsigned)pid);

    /* Use a NULL DACL so non-elevated (or lower-integrity) processes can read.
     * Without this, an elevated-process-hosted DLL creates a mapping that only
     * elevated callers can open, preventing LegacyCapture from reading it when
     * Pub-Streamer runs non-elevated (e.g. NVDA with uiAccess=true). */
    SECURITY_DESCRIPTOR sd;
    SECURITY_ATTRIBUTES sa;
    InitializeSecurityDescriptor(&sd, SECURITY_DESCRIPTOR_REVISION);
    SetSecurityDescriptorDacl(&sd, TRUE, NULL, FALSE);
    sa.nLength              = sizeof(sa);
    sa.lpSecurityDescriptor = &sd;
    sa.bInheritHandle       = FALSE;

    g_shmem_handle = CreateFileMappingA(
        INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE,
        0, (DWORD)PS_SHMEM_SIZE, name);
    if (!g_shmem_handle) return;

    g_shmem = (PsAudioShmem *)MapViewOfFile(
        g_shmem_handle, FILE_MAP_ALL_ACCESS, 0, 0, PS_SHMEM_SIZE);
    if (!g_shmem) return;

    /* Ring buffer starts immediately after the header. */
    g_ring = (float *)(g_shmem + 1);

    /* Zero the mapping and write the header fields that are known now.
     * channels/sample_rate are filled in by the first hook that fires. */
    memset(g_shmem, 0, PS_SHMEM_SIZE);
    g_shmem->magic          = PS_SHMEM_MAGIC;
    g_shmem->version        = PS_SHMEM_VERSION;
    /* Sentinels — confirm the new fields are at the right offsets.
     * Cleared to 0 by the first hook that writes real values. */
    g_shmem->wasapi_step     = 0xDEAD0001u;
    g_shmem->format_tag      = 0xDEAD0002u;
    g_shmem->bits_per_sample = 0xDEAD0003u;
}

static void shmem_destroy(void)
{
    if (g_shmem)        { UnmapViewOfFile(g_shmem); g_shmem = NULL; }
    if (g_shmem_handle) { CloseHandle(g_shmem_handle); g_shmem_handle = NULL; }
    g_ring = NULL;
}

BOOL WINAPI DllMain(HINSTANCE hInst, DWORD reason, LPVOID reserved)
{
    (void)hInst;
    (void)reserved;

    switch (reason) {
    case DLL_PROCESS_ATTACH:
        DisableThreadLibraryCalls(hInst);
        shmem_create(GetCurrentProcessId());
        hook_winmm_install();
        hook_dsound_install();
        hook_dsound_probe_async();   /* fallback: patch existing DSound vtable */
        hook_wasapi_probe_async();   /* primary: captures full dsound.dll mix  */
        break;

    case DLL_PROCESS_DETACH:
        hook_winmm_remove();
        hook_dsound_remove();
        shmem_destroy();
        break;
    }
    return TRUE;
}
