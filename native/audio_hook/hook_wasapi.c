/*
 * hook_wasapi.c — Capture the WASAPI render output to get the complete,
 *                 correctly-timed audio mix from dsound.dll (or any other
 *                 renderer in the process).
 *
 * Why this approach beats DirectSound-Unlock capture:
 *   - dsound.dll mixes ALL secondary buffers (SFX + BGM) and writes the
 *     final mix via IAudioRenderClient::ReleaseBuffer.
 *   - SFX stored in static DirectSound buffers are loaded once with
 *     Lock/Unlock and then just Play()-ed; no further Unlock fires after
 *     injection, so the Unlock hook misses them entirely.
 *   - Capturing at ReleaseBuffer gives correct playback timing with no
 *     reversed-latency or phase artefacts.
 *
 * IAudioRenderClient vtable (audioclient.h / MSDN):
 *   0  QueryInterface   3  GetBuffer(UINT32, BYTE**)
 *   1  AddRef           4  ReleaseBuffer(UINT32, DWORD)
 *   2  Release
 *
 * IMMDeviceEnumerator vtable (mmdeviceapi.h):
 *   0-2  IUnknown   3  EnumAudioEndpoints   4  GetDefaultAudioEndpoint
 *
 * IMMDevice vtable (mmdeviceapi.h):
 *   0-2  IUnknown   3  Activate
 *
 * IAudioClient vtable (audioclient.h):
 *   0-2  IUnknown   3  Initialize   8  GetMixFormat   14  GetService
 *
 * All IAudioRenderClient instances in a process share one vtable pointer
 * (same audioses.dll class).  Patching slot 3 and 4 on the probe's
 * instance therefore patches dsound.dll's instance too, even if it was
 * created before injection.
 *
 * Probe progress is written to g_shmem->_pad[0] so Python can see where
 * it stopped if the probe fails:
 *   1  Thread started       5  DevActivate done
 *   2  CoInitialize done    6  GetMixFormat done
 *   3  CoCreateInstance ok  7  Initialize done
 *   4  GetDefaultEndpoint   8  GetService done → vtable patched
 *
 * References: MSDN "IAudioRenderClient", "IAudioClient",
 *             "IMMDeviceEnumerator", "IMMDevice".
 */

#include "hook_engine.h"
#include "shmem.h"
#include <mmsystem.h>   /* WAVEFORMATEX — must precede other mm headers */
#include <mmreg.h>      /* WAVEFORMATEXTENSIBLE, WAVE_FORMAT_EXTENSIBLE */
#include <objbase.h>    /* CoCreateInstance, CoInitializeEx, CoUninitialize,
                           CoTaskMemFree */
#include <string.h>

extern PsAudioShmem *g_shmem;
extern float        *g_ring;

/* Set to 1 by hook_winmm.c when waveOutOpen fires.  We skip WASAPI capture
 * for WinMM-based processes to avoid overwriting the WinMM hook's audio. */
extern volatile LONG g_winmm_active;

/* Set to 1 once our WASAPI hook is wired up.  The WinMM and DirectSound
 * Unlock hooks check this flag and skip writing so only one path fills
 * the ring buffer.                                                       */
volatile LONG g_wasapi_active = 0;

/* ── device mix format (filled by probe thread) ─────────────────────── */
static UINT g_wch    = 2;
static UINT g_wbits  = 32;
static bool g_wfloat = true;
static UINT g_wrate  = 48000;

/* Buffer pointer returned by GetBuffer; used inside the matching
 * ReleaseBuffer call from the same thread.  Thread-local so concurrent
 * GetBuffer calls from different threads each carry their own pointer.  */
static __declspec(thread) BYTE *g_arc_buf = NULL;

/* ── GUID literals ──────────────────────────────────────────────────── */
static const GUID k_CLSID_MMDeviceEnumerator = {
    0xbcde0395,0xe52f,0x467c,{0x8e,0x3d,0xc4,0x57,0x92,0x91,0x69,0x2e}};
static const GUID k_IID_IMMDeviceEnumerator = {
    0xa95664d2,0x9614,0x4f35,{0xa7,0x46,0xde,0x8d,0xb6,0x36,0x17,0xe6}};
static const GUID k_IID_IAudioClient = {
    0x1cb9ad4c,0xdbfa,0x4c32,{0xb1,0x78,0xc2,0xf5,0x68,0xa7,0x03,0xb2}};
static const GUID k_IID_IAudioRenderClient = {
    0xf294acfc,0x3146,0x4483,{0xa7,0xbf,0xad,0xdc,0xa7,0xc2,0x60,0xe2}};

/* KSDATAFORMAT_SUBTYPE_IEEE_FLOAT {00000003-0000-0010-8000-00AA00389B71} */
static const BYTE k_float_sub[16] = {
    0x03,0x00,0x00,0x00, 0x00,0x00, 0x10,0x00,
    0x80,0x00, 0x00,0xAA,0x00,0x38,0x9B,0x71
};

/* ── vtable function pointer types ─────────────────────────────────── */
typedef ULONG   (STDMETHODCALLTYPE *pfn_Rel)(void *);
typedef HRESULT (STDMETHODCALLTYPE *pfn_GetDefaultEndpoint)(
    void *, int, int, void **);
typedef HRESULT (STDMETHODCALLTYPE *pfn_DevActivate)(
    void *, const GUID *, DWORD, void *, void **);
typedef HRESULT (STDMETHODCALLTYPE *pfn_AC_Initialize)(
    void *, int, DWORD, LONGLONG, LONGLONG, const WAVEFORMATEX *, const GUID *);
typedef HRESULT (STDMETHODCALLTYPE *pfn_AC_GetMixFormat)(void *, WAVEFORMATEX **);
typedef HRESULT (STDMETHODCALLTYPE *pfn_AC_GetService)(void *, const GUID *, void **);
typedef HRESULT (STDMETHODCALLTYPE *pfn_arc_GetBuffer)(void *, UINT32, BYTE **);
typedef HRESULT (STDMETHODCALLTYPE *pfn_arc_ReleaseBuffer)(void *, UINT32, DWORD);

static pfn_arc_GetBuffer     g_orig_GetBuffer     = NULL;
static pfn_arc_ReleaseBuffer g_orig_ReleaseBuffer = NULL;

/* ── progress helper (writes to shmem wasapi_step, readable from Python) ─ */
#define WPROG(n) do { if (g_shmem) g_shmem->wasapi_step = (uint32_t)(n); } while(0)

/* ── detour: GetBuffer ──────────────────────────────────────────────── */

static HRESULT STDMETHODCALLTYPE detour_GetBuffer(
    void *self, UINT32 nFrames, BYTE **ppData)
{
    HRESULT hr = g_orig_GetBuffer(self, nFrames, ppData);
    g_arc_buf  = (SUCCEEDED(hr) && ppData) ? *ppData : NULL;
    return hr;
}

/* ── ring push ──────────────────────────────────────────────────────── */

static void arc_push(const BYTE *data, UINT32 n)
{
    /* When WinMM is active, let the WinMM hook handle capture instead. */
    if (g_winmm_active) return;
    if (!g_shmem || !g_ring || !data || !n) return;

    /* Always stamp the WASAPI format so Python sees the correct rate
     * even if the DSound probe wrote sr=44100 before WASAPI took over. */
    UINT dst_ch = (g_wch > 2) ? 2 : g_wch;
    g_shmem->channels        = dst_ch;
    g_shmem->sample_rate     = g_wrate;
    g_shmem->format_tag      = g_wfloat ? WAVE_FORMAT_IEEE_FLOAT : WAVE_FORMAT_PCM;
    g_shmem->bits_per_sample = g_wbits;

    UINT src_ch  = g_wch;
    UINT bits    = g_wbits;
    bool isfloat = g_wfloat;
    UINT fbytes  = (bits / 8) * src_ch;

    for (UINT32 f = 0; f < n; f++) {
        const BYTE *src = data + f * fbytes;
        UINT  idx = (g_shmem->write_pos + f) % PS_RING_FRAMES;
        float *dst = g_ring + idx * dst_ch;

        float s[8] = {0};
        UINT  lim  = (src_ch < 8) ? src_ch : 8;
        for (UINT c = 0; c < lim; c++) {
            float v = 0.0f;
            if (isfloat && bits == 32) {
                memcpy(&v, src + c * 4, 4);
            } else if (bits == 16) {
                INT16 i16; memcpy(&i16, src + c * 2, 2); v = i16 / 32768.0f;
            } else if (bits == 32) {
                INT32 i32; memcpy(&i32, src + c * 4, 4); v = i32 / 2147483648.0f;
            }
            /* Sanitize: zero NaN/Inf, clamp overdriven values. */
            if (v != v || v > 3.4e38f || v < -3.4e38f) v = 0.0f;
            if (v >  1.0f) v =  1.0f;
            if (v < -1.0f) v = -1.0f;
            s[c] = v;
        }

        if (dst_ch == 1) {
            float sum = 0;
            for (UINT c = 0; c < lim; c++) sum += s[c];
            dst[0] = lim ? (sum / (float)lim) : 0.0f;
        } else {
            dst[0] = s[0];
            dst[1] = (src_ch >= 2) ? s[1] : s[0];
        }
    }
    InterlockedAdd((volatile LONG *)&g_shmem->write_pos, (LONG)n);
}

/* ── detour: ReleaseBuffer ──────────────────────────────────────────── */

#define AUDCLNT_BUFFERFLAGS_SILENT 0x00000002

static HRESULT STDMETHODCALLTYPE detour_ReleaseBuffer(
    void *self, UINT32 nFrames, DWORD dwFlags)
{
    if (!(dwFlags & AUDCLNT_BUFFERFLAGS_SILENT) && g_arc_buf && nFrames > 0)
        arc_push(g_arc_buf, nFrames);
    g_arc_buf = NULL;
    return g_orig_ReleaseBuffer(self, nFrames, dwFlags);
}

/* ── vtable patch ───────────────────────────────────────────────────── */

static void patch_arc_vtable(void *arc)
{
    void **vt = *(void ***)arc;
    DWORD old;
    if (!VirtualProtect(&vt[3], sizeof(void *) * 2, PAGE_EXECUTE_READWRITE, &old))
        return;
    if (!g_orig_GetBuffer)     g_orig_GetBuffer     = (pfn_arc_GetBuffer)vt[3];
    if (!g_orig_ReleaseBuffer) g_orig_ReleaseBuffer = (pfn_arc_ReleaseBuffer)vt[4];
    vt[3] = detour_GetBuffer;
    vt[4] = detour_ReleaseBuffer;
    VirtualProtect(&vt[3], sizeof(void *) * 2, old, &old);
}

/* ── probe thread ───────────────────────────────────────────────────── */

static DWORD WINAPI probe_wasapi_vtable(LPVOID unused)
{
    (void)unused;
    WPROG(1);

    /* Give dsound.dll more time to initialise its render session so that
     * when we patch the shared vtable the session is already running.
     * DSound probe fires at 300ms; we wait an additional 500ms.        */
    Sleep(800);

    /* Use STA (apartment-threaded) to match DirectSound / the game.    */
    HRESULT hr_co = CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);
    if (FAILED(hr_co) && hr_co != RPC_E_CHANGED_MODE) { WPROG(0x81); return 0; }
    WPROG(2);

    /* ── Get default render endpoint ──────────────────────────────── */
    void *pEnum = NULL;
    HRESULT hr = CoCreateInstance(
        &k_CLSID_MMDeviceEnumerator, NULL, 0x17 /* CLSCTX_ALL */,
        &k_IID_IMMDeviceEnumerator, &pEnum);
    if (FAILED(hr) || !pEnum) { WPROG(0x83); goto done_co; }
    WPROG(3);

    void **ev = *(void ***)pEnum;

    void *pDevice = NULL;
    /* slot 4 = GetDefaultAudioEndpoint(EDataFlow, ERole, IMMDevice**) */
    hr = ((pfn_GetDefaultEndpoint)ev[4])(pEnum, 0 /*eRender*/, 0 /*eConsole*/, &pDevice);
    ((pfn_Rel)ev[2])(pEnum);
    if (FAILED(hr) || !pDevice) { WPROG(0x84); goto done_co; }
    WPROG(4);

    /* ── Activate IAudioClient ────────────────────────────────────── */
    void **dv = *(void ***)pDevice;
    void *pAC = NULL;
    /* slot 3 = Activate(REFIID, DWORD, PROPVARIANT*, void**) */
    hr = ((pfn_DevActivate)dv[3])(pDevice, &k_IID_IAudioClient,
                                   0x17 /* CLSCTX_ALL */, NULL, &pAC);
    ((pfn_Rel)dv[2])(pDevice);
    if (FAILED(hr) || !pAC) { WPROG(0x85); goto done_co; }
    WPROG(5);

    void **av = *(void ***)pAC;

    /* ── GetMixFormat ─────────────────────────────────────────────── */
    WAVEFORMATEX *pwfx = NULL;
    /* slot 8 = GetMixFormat(WAVEFORMATEX**) */
    hr = ((pfn_AC_GetMixFormat)av[8])(pAC, &pwfx);
    if (FAILED(hr) || !pwfx) { WPROG(0x86); ((pfn_Rel)av[2])(pAC); goto done_co; }

    g_wch   = pwfx->nChannels;
    g_wbits = pwfx->wBitsPerSample;
    g_wrate = pwfx->nSamplesPerSec;
    if (pwfx->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) {
        g_wfloat = true;
    } else if (pwfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE && pwfx->cbSize >= 22) {
        const WAVEFORMATEXTENSIBLE *e = (const WAVEFORMATEXTENSIBLE *)pwfx;
        g_wfloat = (memcmp(&e->SubFormat, k_float_sub, 16) == 0);
    } else {
        g_wfloat = false;
    }
    WPROG(6);

    /* ── Initialize (required before GetService) ──────────────────── */
    /* AUDCLNT_SHAREMODE_SHARED=0, StreamFlags=0, hnsBufferDuration=0,
     * hnsPeriodicity=0.  Using pwfx (still valid, freed after).       */
    hr = ((pfn_AC_Initialize)av[3])(pAC, 0, 0, 0, 0, pwfx, NULL);
    CoTaskMemFree(pwfx);
    if (FAILED(hr)) { WPROG(0x87); ((pfn_Rel)av[2])(pAC); goto done_co; }
    WPROG(7);

    /* ── GetService → IAudioRenderClient ─────────────────────────── */
    void *pARC = NULL;
    /* slot 14 = GetService(REFIID, void**) */
    hr = ((pfn_AC_GetService)av[14])(pAC, &k_IID_IAudioRenderClient, &pARC);
    if (FAILED(hr) || !pARC) { WPROG(0x88); ((pfn_Rel)av[2])(pAC); goto done_co; }

    patch_arc_vtable(pARC);
    ((pfn_Rel)(*(void ***)pARC)[2])(pARC);
    ((pfn_Rel)av[2])(pAC);

    WPROG(8);
    /* Signal DSound / WinMM hooks to stand down. */
    InterlockedExchange(&g_wasapi_active, 1);

done_co:
    if (SUCCEEDED(hr_co)) CoUninitialize();
    return 0;
}

/* ── public interface ────────────────────────────────────────────────── */

void hook_wasapi_probe_async(void)
{
    HANDLE ht = CreateThread(NULL, 0, probe_wasapi_vtable, NULL, 0, NULL);
    if (ht) CloseHandle(ht);
}
