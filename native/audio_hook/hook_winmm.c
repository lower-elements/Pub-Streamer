/*
 * hook_winmm.c — Intercept waveOutOpen and waveOutWrite (WinMM API).
 *
 * waveOutOpen   : captures the WAVEFORMATEX so we know the sample format.
 * waveOutWrite  : copies PCM data from WAVEHDR into the shared memory ring.
 *
 * References:
 *   - MSDN: waveOutOpen, waveOutWrite, WAVEHDR, WAVEFORMATEX, WAVEFORMATEXTENSIBLE
 *   - MSDN: mmreg.h constants (WAVE_FORMAT_PCM, WAVE_FORMAT_IEEE_FLOAT,
 *            WAVE_FORMAT_EXTENSIBLE, KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)
 */

#include "hook_engine.h"
#include "shmem.h"
#include <mmsystem.h>
#include <mmreg.h>      /* WAVEFORMATEXTENSIBLE, KSDATAFORMAT_SUBTYPE_* */
#include <string.h>
#include <math.h>

/* ── shared memory state (owned by dllmain.c, extern here) ────────────────── */
extern HANDLE        g_shmem_handle;
extern PsAudioShmem *g_shmem;
extern float        *g_ring;           /* points just past PsAudioShmem in mapping */

/* Set to 1 by hook_wasapi.c when the WASAPI path is active. */
extern volatile LONG g_wasapi_active;

/* Set to 1 here when waveOutOpen fires; checked by hook_wasapi.c to avoid
 * suppressing WinMM capture with the WASAPI hook in WinMM-based processes. */
volatile LONG g_winmm_active = 0;

/* ── captured format ─────────────────────────────────────────────────────── */
static WAVEFORMATEXTENSIBLE g_fmt;
static bool                 g_fmt_valid = false;

/* True if the captured format uses 32-bit IEEE float samples. */
static bool g_is_float = false;

/* ── hooks ───────────────────────────────────────────────────────────────── */
static PsHook g_hook_open;
static PsHook g_hook_write;

typedef MMRESULT (WINAPI *pfn_waveOutOpen)(LPHWAVEOUT, UINT_PTR,
    LPCWAVEFORMATEX, DWORD_PTR, DWORD_PTR, DWORD);
typedef MMRESULT (WINAPI *pfn_waveOutWrite)(HWAVEOUT, LPWAVEHDR, UINT);

/* ── helpers ─────────────────────────────────────────────────────────────── */

/* KSDATAFORMAT_SUBTYPE_IEEE_FLOAT {00000003-0000-0010-8000-00AA00389B71}
 * Byte representation (little-endian GUID as stored by Windows):          */
static const BYTE k_subtype_float[16] = {
    0x03,0x00,0x00,0x00, 0x00,0x00, 0x10,0x00,
    0x80,0x00, 0x00,0xAA,0x00,0x38,0x9B,0x71
};

static void record_format(LPCWAVEFORMATEX wfx)
{
    if (!wfx || g_fmt_valid) return;

    memcpy(&g_fmt, wfx,
           (wfx->cbSize > 0)
               ? sizeof(WAVEFORMATEX) + wfx->cbSize
               : sizeof(WAVEFORMATEX));

    if (wfx->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) {
        g_is_float = true;
    } else if (wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE && wfx->cbSize >= 22) {
        const WAVEFORMATEXTENSIBLE *wfxe = (const WAVEFORMATEXTENSIBLE *)wfx;
        g_is_float = (memcmp(&wfxe->SubFormat, k_subtype_float, 16) == 0);
    } else {
        g_is_float = false; /* PCM integer */
    }

    if (!g_shmem) return;
    g_shmem->channels        = wfx->nChannels > 2 ? 2 : wfx->nChannels;
    g_shmem->sample_rate     = wfx->nSamplesPerSec;
    g_shmem->format_tag      = wfx->wFormatTag;
    g_shmem->bits_per_sample = wfx->wBitsPerSample;
    InterlockedExchange(&g_winmm_active, 1);
    g_fmt_valid = true;
}

/* Convert one interleaved PCM buffer to float32 and write to ring. */
static void push_audio(const void *data, DWORD byte_count)
{
    if (!g_shmem || !g_ring || !data || !byte_count) return;

    /* Late injection: if we missed waveOutOpen, assume 16-bit stereo 44100 Hz.
     * This is the most common format for Windows 9x-era WinMM games.
     * record_format() will override these values if waveOutOpen fires later. */
    if (!g_fmt_valid) {
        g_fmt.Format.nChannels      = 2;
        g_fmt.Format.wBitsPerSample = 16;
        g_fmt.Format.nSamplesPerSec = 44100;
        g_fmt.Format.wFormatTag     = WAVE_FORMAT_PCM;
        g_is_float                  = false;
        g_shmem->channels           = 2;
        g_shmem->sample_rate        = 44100;
        g_shmem->format_tag         = WAVE_FORMAT_PCM;
        g_shmem->bits_per_sample    = 16;
        InterlockedExchange(&g_winmm_active, 1);
        g_fmt_valid                 = true;
    }

    UINT ch   = g_shmem->channels;
    UINT bits = g_fmt.Format.wBitsPerSample;
    DWORD frame_bytes = (bits / 8) * ch;
    if (frame_bytes == 0) return;

    DWORD n_frames = byte_count / frame_bytes;
    if (n_frames == 0) return;

    const BYTE *src = (const BYTE *)data;

    for (DWORD f = 0; f < n_frames; f++) {
        UINT ring_idx = (g_shmem->write_pos + f) % PS_RING_FRAMES;
        float *dst = g_ring + ring_idx * ch;

        for (UINT c = 0; c < ch; c++) {
            float s;
            if (g_is_float && bits == 32) {
                float v; memcpy(&v, src, 4); s = v;
            } else if (bits == 16) {
                INT16 v; memcpy(&v, src, 2);
                s = v / 32768.0f;
            } else if (bits == 8) {
                UINT8 v = *src;
                s = (v / 128.0f) - 1.0f;
            } else if (bits == 32) {
                INT32 v; memcpy(&v, src, 4);
                s = v / 2147483648.0f;
            } else {
                s = 0.0f;
            }
            dst[c] = s;
            src += bits / 8;
        }
    }

    /* Commit: advance write_pos atomically so Python sees a consistent value. */
    InterlockedAdd((volatile LONG *)&g_shmem->write_pos, (LONG)n_frames);
}

/* ── detours ─────────────────────────────────────────────────────────────── */

static MMRESULT WINAPI detour_waveOutOpen(
    LPHWAVEOUT      phwo,
    UINT_PTR        uDeviceID,
    LPCWAVEFORMATEX pwfx,
    DWORD_PTR       dwCallback,
    DWORD_PTR       dwInstance,
    DWORD           fdwOpen)
{
    record_format(pwfx);
    return ps_hook_original(pfn_waveOutOpen, &g_hook_open)(
        phwo, uDeviceID, pwfx, dwCallback, dwInstance, fdwOpen);
}

static MMRESULT WINAPI detour_waveOutWrite(
    HWAVEOUT  hwo,
    LPWAVEHDR pwh,
    UINT      cbwh)
{
    /* Always capture waveOutWrite — the WASAPI hook defers to WinMM when
     * g_winmm_active is set, so there is no double-write. */
    if (pwh && pwh->lpData && pwh->dwBufferLength > 0)
        push_audio(pwh->lpData, pwh->dwBufferLength);
    return ps_hook_original(pfn_waveOutWrite, &g_hook_write)(hwo, pwh, cbwh);
}

/* ── public interface ────────────────────────────────────────────────────── */

void hook_winmm_install(void)
{
    HMODULE hMM = GetModuleHandleA("winmm.dll");
    if (!hMM) return;  /* not loaded — WASAPI hook covers this app */

    void *fn_open  = GetProcAddress(hMM, "waveOutOpen");
    void *fn_write = GetProcAddress(hMM, "waveOutWrite");

    if (fn_open)  ps_hook_install(&g_hook_open,  fn_open,  detour_waveOutOpen);
    if (fn_write) ps_hook_install(&g_hook_write, fn_write, detour_waveOutWrite);
}

void hook_winmm_remove(void)
{
    ps_hook_remove(&g_hook_write);
    ps_hook_remove(&g_hook_open);
}
