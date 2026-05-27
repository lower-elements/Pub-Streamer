/*
 * shmem.h — Shared memory IPC protocol between the hook DLL and the Python host.
 *
 * The hook DLL creates a named file mapping when it is loaded.  The Python host
 * opens the same mapping by name, reads audio frames from the ring buffer, and
 * tracks its own read position.
 *
 * Layout
 * ------
 *   PsAudioShmem sits at offset 0 of the mapping.
 *   The ring buffer follows immediately: PS_RING_FRAMES * channels float32 values,
 *   stored interleaved (L0 R0 L1 R1 …).
 *   write_pos is an absolute frame counter; wrap at PS_RING_FRAMES for the index.
 *
 * Synchronisation
 * ---------------
 *   The DLL increments write_pos with InterlockedAdd after committing each batch.
 *   Python polls write_pos (with a short sleep between polls).  No named event is
 *   needed; polling is cheap enough for a 21 ms chunk period.
 *
 * All integers are little-endian (native on x86/x64 Windows).
 */

#pragma once
#include <stdint.h>

#define PS_SHMEM_NAME    "Local\\pubstreamer-audio-%u"   /* %u = target PID */
#define PS_SHMEM_MAGIC   0x50534155u   /* 'PSAU' */
#define PS_SHMEM_VERSION 1u
#define PS_MAX_CHANNELS  2u
#define PS_RING_FRAMES   32768u        /* ~682 ms at 48 kHz; power of two for cheap modulo */

/* Total mapping size: header + ring buffer */
#define PS_SHMEM_SIZE \
    (sizeof(PsAudioShmem) + PS_RING_FRAMES * PS_MAX_CHANNELS * sizeof(float))

#pragma pack(push, 1)
typedef struct {
    uint32_t magic;        /* PS_SHMEM_MAGIC                                    */
    uint32_t version;      /* PS_SHMEM_VERSION                                  */
    uint32_t channels;     /* 1 or 2, set once on first captured buffer         */
    uint32_t sample_rate;  /* e.g. 48000, set once on first captured buffer     */
    uint32_t write_pos;        /* absolute frame counter; updated after each write  */
    uint32_t wasapi_step;      /* WASAPI probe progress (0=not started, 8=active)   */
    uint32_t format_tag;       /* WAVE_FORMAT_PCM=1, IEEE_FLOAT=3, EXTENSIBLE=0xFFFE*/
    uint32_t bits_per_sample;  /* wBitsPerSample from WAVEFORMATEX                  */
    /* float ring[PS_RING_FRAMES * channels] follows in the mapping             */
} PsAudioShmem;
#pragma pack(pop)
