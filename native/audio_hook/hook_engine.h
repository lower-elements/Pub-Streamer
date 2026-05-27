/*
 * hook_engine.h — Minimal inline function hook for x86 and x64.
 *
 * Technique: overwrite the first bytes of the target function with an
 * unconditional JMP to the detour, and provide a trampoline that executes
 * those saved bytes then jumps back into the original function.
 *
 * x86  : 5-byte relative JMP  (E9 rel32)
 * x64  : 14-byte absolute JMP (FF 25 00 00 00 00 <abs64>)
 *
 * This technique is described in Richter's "Windows via C/C++" and predates
 * all modern hooking libraries.  It is the canonical approach documented on
 * MSDN under "Detours" (Microsoft Research, 1999).
 */

#pragma once
#include <windows.h>
#include <stdbool.h>
#include <stdint.h>

#ifdef _WIN64
#define PS_HOOK_PATCH_SIZE  14u
#else
#define PS_HOOK_PATCH_SIZE   5u
#endif

/* Enough trampoline space: saved bytes + one full patch to jump back. */
#define PS_TRAMPOLINE_SIZE  (PS_HOOK_PATCH_SIZE + PS_HOOK_PATCH_SIZE)

typedef struct {
    void    *target;                         /* address of the original function  */
    void    *detour;                         /* address of our replacement        */
    uint8_t  saved[PS_HOOK_PATCH_SIZE];      /* original bytes we overwrote       */
    uint8_t  trampoline[PS_TRAMPOLINE_SIZE]; /* saved bytes + jump back           */
    bool     installed;
} PsHook;

/*
 * ps_hook_install — Patch target, fill trampoline, mark h->installed = true.
 * Returns false on VirtualProtect failure.
 */
bool ps_hook_install(PsHook *h, void *target, void *detour);

/*
 * ps_hook_remove — Restore the original bytes at target.
 */
void ps_hook_remove(PsHook *h);

/*
 * ps_hook_call_original — Cast trampoline to a function pointer of type T and
 * call it.  Convenience macro so callers do not need to cast manually.
 *
 * Usage:  MMRESULT r = ps_hook_call_original(MMRESULT(WINAPI*)(HWAVEOUT,...), &hook, hwo, pwh, cb);
 */
#define ps_hook_original(fn_type, hook_ptr)  ((fn_type)(void *)(hook_ptr)->trampoline)
