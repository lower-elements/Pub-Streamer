/*
 * hook_engine.c — Inline hook implementation.
 *
 * References (public documentation only):
 *   - MSDN: VirtualProtect, FlushInstructionCache
 *   - Intel x86/x64 manual: JMP encoding (opcode E9 / FF 25)
 *   - Richter, "Windows via C/C++", 5th ed., ch. 22 (DLL injection & API hooking)
 */

#include "hook_engine.h"
#include <string.h>

/* Write a JMP from `from` to `to` into the byte buffer `buf`.
 * buf must be at least PS_HOOK_PATCH_SIZE bytes.              */
static void write_jmp(uint8_t *buf, void *from, void *to)
{
#ifdef _WIN64
    /* FF 25 00 00 00 00      JMP QWORD PTR [RIP+0]
     * <8 bytes of absolute address>                */
    buf[0] = 0xFF;
    buf[1] = 0x25;
    buf[2] = 0x00;
    buf[3] = 0x00;
    buf[4] = 0x00;
    buf[5] = 0x00;
    memcpy(buf + 6, &to, 8);
    (void)from;
#else
    /* E9 <rel32>   JMP rel32
     * rel32 = destination - (source + 5)           */
    intptr_t rel = (intptr_t)to - ((intptr_t)from + 5);
    buf[0] = 0xE9;
    memcpy(buf + 1, &rel, 4);
#endif
}

bool ps_hook_install(PsHook *h, void *target, void *detour)
{
    if (!target || !detour) return false;
    h->target   = target;
    h->detour   = detour;
    h->installed = false;

    /* Save the bytes we are about to overwrite. */
    memcpy(h->saved, target, PS_HOOK_PATCH_SIZE);

    /* Build the trampoline in executable memory allocated from the heap.
     * We write it into h->trampoline[] which lives in our DLL's .data
     * section — we must mark it PAGE_EXECUTE_READWRITE.               */
    DWORD old;
    if (!VirtualProtect(h->trampoline, PS_TRAMPOLINE_SIZE,
                        PAGE_EXECUTE_READWRITE, &old))
        return false;

    /* First part: the original bytes we saved. */
    memcpy(h->trampoline, h->saved, PS_HOOK_PATCH_SIZE);

    /* Second part: JMP back to target+PS_HOOK_PATCH_SIZE. */
    uint8_t *jmp_back_src = h->trampoline + PS_HOOK_PATCH_SIZE;
    void    *jmp_back_dst = (uint8_t *)target + PS_HOOK_PATCH_SIZE;
    write_jmp(jmp_back_src, jmp_back_src, jmp_back_dst);

    FlushInstructionCache(GetCurrentProcess(),
                          h->trampoline, PS_TRAMPOLINE_SIZE);

    /* Patch the target function. */
    if (!VirtualProtect(target, PS_HOOK_PATCH_SIZE,
                        PAGE_EXECUTE_READWRITE, &old))
        return false;

    write_jmp((uint8_t *)target, target, detour);
    FlushInstructionCache(GetCurrentProcess(), target, PS_HOOK_PATCH_SIZE);

    VirtualProtect(target, PS_HOOK_PATCH_SIZE, old, &old);

    h->installed = true;
    return true;
}

void ps_hook_remove(PsHook *h)
{
    if (!h->installed) return;
    DWORD old;
    if (VirtualProtect(h->target, PS_HOOK_PATCH_SIZE,
                       PAGE_EXECUTE_READWRITE, &old)) {
        memcpy(h->target, h->saved, PS_HOOK_PATCH_SIZE);
        FlushInstructionCache(GetCurrentProcess(),
                              h->target, PS_HOOK_PATCH_SIZE);
        VirtualProtect(h->target, PS_HOOK_PATCH_SIZE, old, &old);
    }
    h->installed = false;
}
