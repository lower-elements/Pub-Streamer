/*
 * hook_dsound.c — Intercept DirectSoundCreate/DirectSoundCreate8 and
 *                 IDirectSoundBuffer::Unlock to capture PCM audio.
 *
 * Approach (COM vtable patching — documented in the DirectX SDK and MSDN):
 *   1. Hook the exported DirectSoundCreate[8] to intercept the IDirectSound[8]*.
 *   2. Patch slot 3 (CreateSoundBuffer) in the returned object's vtable.
 *   3. In our CreateSoundBuffer detour, patch slot 19 (Unlock) in every
 *      secondary IDirectSoundBuffer returned to the caller.
 *   4. In our Unlock detour, the arguments include the data pointer and byte
 *      count — we convert and push to the ring buffer.
 *
 * IDirectSoundBuffer vtable layout (from dsound.h / MSDN):
 *   0  QueryInterface   3  GetCaps        6  GetVolume    9  GetStatus
 *   1  AddRef           4  GetCurrentPos  7  GetPan      10  Initialize
 *   2  Release          5  GetFormat      8  GetFreq     11  Lock
 *                                                        12  Play
 *                                                        13  SetCurrentPos
 *                                                        14  SetFormat
 *                                                        15  SetVolume
 *                                                        16  SetPan
 *                                                        17  SetFrequency
 *                                                        18  Stop
 *                                                        19  Unlock
 *                                                        20  Restore
 *
 * IDirectSound[8] vtable (from dsound.h):
 *   0  QueryInterface   3  CreateSoundBuffer    6  SetCooperativeLevel
 *   1  AddRef           4  GetCaps              7  Compact
 *   2  Release          5  DuplicateSoundBuffer 8  GetSpeakerConfig
 *
 * References: MSDN "IDirectSoundBuffer", "IDirectSound8",
 *             Raymond Chen "The Old New Thing" COM vtable layout articles.
 */

#include "hook_engine.h"
#include "shmem.h"
#include <mmsystem.h>   /* must precede dsound.h — defines WAVEFORMATEX */
#include <objbase.h>    /* CoInitializeEx, CoUninitialize */
#include <dsound.h>
#include <mmreg.h>
#include <string.h>

extern PsAudioShmem *g_shmem;
extern float        *g_ring;

/* Set to 1 by hook_wasapi.c when the WASAPI path is active; we skip
 * writing here to avoid double-filling the ring buffer.               */
extern volatile LONG g_wasapi_active;

/* ── format state (same recording helper used by hook_winmm.c) ────────────── */
static bool         g_ds_fmt_valid = false;
static UINT         g_ds_channels   = 2;
static UINT         g_ds_bits       = 16;
static bool         g_ds_is_float   = false;

static const BYTE k_subtype_float[16] = {
    0x03,0x00,0x00,0x00, 0x00,0x00, 0x10,0x00,
    0x80,0x00, 0x00,0xAA,0x00,0x38,0x9B,0x71
};

static void ds_record_format(LPCWAVEFORMATEX wfx)
{
    if (!wfx || g_ds_fmt_valid) return;
    g_ds_channels = wfx->nChannels > 2 ? 2 : wfx->nChannels;
    g_ds_bits     = wfx->wBitsPerSample;
    if (wfx->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) {
        g_ds_is_float = true;
    } else if (wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE && wfx->cbSize >= 22) {
        const WAVEFORMATEXTENSIBLE *e = (const WAVEFORMATEXTENSIBLE *)wfx;
        g_ds_is_float = (memcmp(&e->SubFormat, k_subtype_float, 16) == 0);
    }
    if (g_shmem && !g_shmem->channels) {
        g_shmem->channels    = g_ds_channels;
        g_shmem->sample_rate = wfx->nSamplesPerSec;
    }
    g_ds_fmt_valid = true;
}

/* ── ring push (same logic as hook_winmm.c, duplicated to keep files independent) */
static void ds_push_audio(const void *data, DWORD bytes)
{
    if (!g_shmem || !g_ring || !data || !bytes) return;

    /* Late injection: missed CreateSoundBuffer. Assume 16-bit stereo 44100 Hz. */
    if (!g_ds_fmt_valid) {
        g_ds_channels  = 2;
        g_ds_bits      = 16;
        g_ds_is_float  = false;
        if (!g_shmem->channels) {
            g_shmem->channels    = 2;
            g_shmem->sample_rate = 44100;
        }
        g_ds_fmt_valid = true;
    }
    UINT ch         = g_ds_channels;
    UINT bits       = g_ds_bits;
    DWORD frame_sz  = (bits / 8) * ch;
    if (!frame_sz) return;
    DWORD n_frames  = bytes / frame_sz;
    const BYTE *src = (const BYTE *)data;

    for (DWORD f = 0; f < n_frames; f++) {
        UINT  idx = (g_shmem->write_pos + f) % PS_RING_FRAMES;
        float *d  = g_ring + idx * ch;
        for (UINT c = 0; c < ch; c++) {
            float s;
            if (g_ds_is_float && bits == 32) {
                float v; memcpy(&v, src, 4); s = v;
            } else if (bits == 16) {
                INT16 v; memcpy(&v, src, 2); s = v / 32768.0f;
            } else if (bits == 8) {
                s = ((float)*src / 128.0f) - 1.0f;
            } else if (bits == 32) {
                INT32 v; memcpy(&v, src, 4); s = v / 2147483648.0f;
            } else { s = 0.0f; }
            d[c] = s;
            src += bits / 8;
        }
    }
    InterlockedAdd((volatile LONG *)&g_shmem->write_pos, (LONG)n_frames);
}

/* ── IDirectSoundBuffer vtable patch ──────────────────────────────────────── */

typedef HRESULT (WINAPI *pfn_dsb_unlock)(
    IDirectSoundBuffer *, LPVOID, DWORD, LPVOID, DWORD);
typedef HRESULT (WINAPI *pfn_dsb_unlock8)(
    IDirectSoundBuffer8 *, LPVOID, DWORD, LPVOID, DWORD);

static pfn_dsb_unlock g_original_unlock = NULL;

static HRESULT WINAPI detour_dsb_unlock(
    IDirectSoundBuffer *self,
    LPVOID  pv1, DWORD cb1,
    LPVOID  pv2, DWORD cb2)
{
    /* Defer to WASAPI hook when it is active (captures full mix). */
    if (!g_wasapi_active) {
        if (pv1 && cb1) ds_push_audio(pv1, cb1);
        if (pv2 && cb2) ds_push_audio(pv2, cb2);
    }
    return g_original_unlock(self, pv1, cb1, pv2, cb2);
}

/* Patch the Unlock slot (19) in the vtable of a buffer object.
 * The vtable pointer is the first word of the COM object.       */
static void patch_buffer_unlock(IDirectSoundBuffer *buf)
{
    if (!buf) return;
    void **vtbl = *(void ***)buf;

    DWORD old;
    if (!VirtualProtect(&vtbl[19], sizeof(void *), PAGE_EXECUTE_READWRITE, &old))
        return;

    if (!g_original_unlock)
        g_original_unlock = (pfn_dsb_unlock)vtbl[19];

    vtbl[19] = detour_dsb_unlock;
    VirtualProtect(&vtbl[19], sizeof(void *), old, &old);
}

/* ── IDirectSound CreateSoundBuffer patch ────────────────────────────────── */

typedef HRESULT (WINAPI *pfn_ds_create_sb)(
    IDirectSound *, LPCDSBUFFERDESC, IDirectSoundBuffer **, IUnknown *);

static pfn_ds_create_sb g_original_create_sb = NULL;

static HRESULT WINAPI detour_ds_create_sb(
    IDirectSound      *self,
    LPCDSBUFFERDESC    pDesc,
    IDirectSoundBuffer **ppBuf,
    IUnknown           *pUnkOuter)
{
    HRESULT hr = g_original_create_sb(self, pDesc, ppBuf, pUnkOuter);
    if (SUCCEEDED(hr) && ppBuf && *ppBuf) {
        /* Capture format from primary or first secondary buffer we see. */
        if (pDesc && pDesc->lpwfxFormat)
            ds_record_format(pDesc->lpwfxFormat);
        /* Only intercept secondary buffers (primary has no audio data). */
        if (pDesc && !(pDesc->dwFlags & DSBCAPS_PRIMARYBUFFER))
            patch_buffer_unlock(*ppBuf);
    }
    return hr;
}

static void patch_ds_create_sb(IDirectSound *ds)
{
    if (!ds) return;
    void **vtbl = *(void ***)ds;
    DWORD old;
    /* Slot 3 = CreateSoundBuffer (IDirectSound vtable, from dsound.h). */
    if (!VirtualProtect(&vtbl[3], sizeof(void *), PAGE_EXECUTE_READWRITE, &old))
        return;
    if (!g_original_create_sb)
        g_original_create_sb = (pfn_ds_create_sb)vtbl[3];
    vtbl[3] = detour_ds_create_sb;
    VirtualProtect(&vtbl[3], sizeof(void *), old, &old);
}

/* ── DirectSoundCreate / DirectSoundCreate8 hooks ────────────────────────── */

static PsHook g_hook_dsc;
static PsHook g_hook_dsc8;

typedef HRESULT (WINAPI *pfn_DirectSoundCreate)(
    LPCGUID, IDirectSound **, IUnknown *);
typedef HRESULT (WINAPI *pfn_DirectSoundCreate8)(
    LPCGUID, IDirectSound8 **, IUnknown *);

static HRESULT WINAPI detour_DirectSoundCreate(
    LPCGUID pguid, IDirectSound **ppDS, IUnknown *pUnk)
{
    HRESULT hr = ps_hook_original(pfn_DirectSoundCreate, &g_hook_dsc)(
        pguid, ppDS, pUnk);
    if (SUCCEEDED(hr) && ppDS && *ppDS)
        patch_ds_create_sb(*ppDS);
    return hr;
}

static HRESULT WINAPI detour_DirectSoundCreate8(
    LPCGUID pguid, IDirectSound8 **ppDS8, IUnknown *pUnk)
{
    HRESULT hr = ps_hook_original(pfn_DirectSoundCreate8, &g_hook_dsc8)(
        pguid, ppDS8, pUnk);
    if (SUCCEEDED(hr) && ppDS8 && *ppDS8)
        patch_ds_create_sb((IDirectSound *)*ppDS8); /* vtable layout identical for slots 0-5 */
    return hr;
}

/* ── public interface ────────────────────────────────────────────────────── */

void hook_dsound_install(void)
{
    HMODULE hDS = GetModuleHandleA("dsound.dll");
    if (!hDS) return;  /* not loaded — WASAPI hook covers this app */

    void *fn_dsc  = GetProcAddress(hDS, "DirectSoundCreate");
    void *fn_dsc8 = GetProcAddress(hDS, "DirectSoundCreate8");

    if (fn_dsc)  ps_hook_install(&g_hook_dsc,  fn_dsc,  detour_DirectSoundCreate);
    if (fn_dsc8) ps_hook_install(&g_hook_dsc8, fn_dsc8, detour_DirectSoundCreate8);
}

void hook_dsound_remove(void)
{
    ps_hook_remove(&g_hook_dsc8);
    ps_hook_remove(&g_hook_dsc);
}

/* ── Background vtable probe (late-injection support) ────────────────────────
 * All IDirectSoundBuffer objects from dsound.dll share a single vtable.
 * Creating any probe buffer and calling patch_buffer_unlock() on it patches
 * slot 19 (Unlock) globally — including in buffers the game created before
 * we were injected.
 *
 * Must run in a separate thread to avoid the DllMain loader-lock restriction
 * on calling CoInitializeEx / DirectSoundCreate.                             */

static DWORD WINAPI probe_dsound_vtable(LPVOID unused)
{
    (void)unused;
    Sleep(300); /* yield past DllMain and loader unlock */

    HMODULE hDS = GetModuleHandleA("dsound.dll");
    if (!hDS) return 0;

    typedef HRESULT (WINAPI *pfn_DSC)(LPCGUID, IDirectSound **, IUnknown *);
    pfn_DSC fn = (pfn_DSC)GetProcAddress(hDS, "DirectSoundCreate");
    if (!fn) return 0;

    /* COM must be initialised on this thread before calling DirectSoundCreate. */
    HRESULT hr_co = CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);
    if (FAILED(hr_co) && hr_co != RPC_E_CHANGED_MODE) return 0;

    IDirectSound *pDS = NULL;
    HRESULT hr = fn(NULL, &pDS, NULL); /* passes through our installed hook */
    if (FAILED(hr) || !pDS) {
        if (SUCCEEDED(hr_co)) CoUninitialize();
        return 0;
    }

    /* SetCooperativeLevel is required before CreateSoundBuffer.
     * DSSCL_NORMAL (1) is the least-invasive level. */
    HWND hwnd = GetForegroundWindow();
    if (!hwnd) hwnd = GetDesktopWindow();
    void **ds_vt = *(void ***)pDS;
    typedef HRESULT (WINAPI *pfn_SCL)(IDirectSound *, HWND, DWORD);
    ((pfn_SCL)ds_vt[6])(pDS, hwnd, 1 /* DSSCL_NORMAL */);

    /* Create a minimal secondary buffer via the ORIGINAL CreateSoundBuffer
     * (bypassing our detour) to avoid ds_record_format() overwriting the
     * game's real audio format with the probe's dummy format.               */
    if (g_original_create_sb) {
        WAVEFORMATEX wfx = {0};
        wfx.wFormatTag      = WAVE_FORMAT_PCM;
        wfx.nChannels       = 1;
        wfx.nSamplesPerSec  = 22050;
        wfx.wBitsPerSample  = 16;
        wfx.nBlockAlign     = 2;
        wfx.nAvgBytesPerSec = 44100;

        DSBUFFERDESC desc = {0};
        desc.dwSize        = sizeof(DSBUFFERDESC);
        desc.dwFlags       = 0;
        desc.dwBufferBytes = 4096;
        desc.lpwfxFormat   = &wfx;

        IDirectSoundBuffer *pBuf = NULL;
        hr = g_original_create_sb(pDS, &desc, &pBuf, NULL);
        if (SUCCEEDED(hr) && pBuf) {
            patch_buffer_unlock(pBuf); /* patches the SHARED vtable */
            typedef ULONG (WINAPI *pfn_Rel)(IDirectSoundBuffer *);
            void **bvt = *(void ***)pBuf;
            ((pfn_Rel)bvt[2])(pBuf);  /* Release probe buffer */
        }
    }

    typedef ULONG (WINAPI *pfn_RelDS)(IDirectSound *);
    ((pfn_RelDS)ds_vt[2])(pDS); /* Release probe IDirectSound */

    if (SUCCEEDED(hr_co)) CoUninitialize();
    return 0;
}

void hook_dsound_probe_async(void)
{
    HANDLE ht = CreateThread(NULL, 0, probe_dsound_vtable, NULL, 0, NULL);
    if (ht) CloseHandle(ht);
}
