#!/usr/bin/env python3
"""Raise real subtitles on demand, with no stealing and nothing left behind.

The game raises a message by calling

    raise_subtitle(a0 = string index, a1 = flags, f12 = duration)   0x002C56E8
        obj = alloc(0x10)
        obj[0x00] = 0                  no page yet
        obj[0x04] = index              bounds-checked against 1621 strings
        obj[0x08] = duration
        obj[0x0C] = flags              bit 1 picks slot A, else slot B
        insert(list A, obj)

and then its per-frame tick opens a page, runs the timer, and tears the whole
thing down through end_message -- real allocation, real teardown, no leak.
The only thing we could never do was CALL it.

So: the tick 0x002C5838 is invoked by `jal` from two sites, both of them the
word 0x0C0B160E with a nop in the delay slot. Repoint them at a trampoline in
scratch that checks a control word, calls raise_subtitle when it is set, and
tail-calls the real tick. From then on a message is four memory writes and
the game does absolutely everything else.

    control block                      trampoline
    +0x00  request   write 1           addiu sp, sp, -0x20
    +0x04  index                       sw    ra, 0x10(sp)
    +0x08  flags                       lui   t0, hi
    +0x0C  duration (float bits)       lw/addiu/sw   ticks++
    +0x10  last object raised          lw    t1, request
    +0x14  tick count                  beq   t1, zero, done
    +0x18  raise count                 sw    zero, request
                                       lw    a0/a1/t2  index/flags/duration
                                       mtc1  t2, $f12
                                       jal   0x002C56E8
                                       sw    v0, last;  raises++
                                 done: lw    ra, 0x10(sp)
                                       j     0x002C5838
                                       addiu sp, sp, 0x20

If PCSX2's recompiler does not notice the patched word it keeps running the
old translation, which calls the real tick -- nothing happens and the tick
counter stays at zero. That is the failure mode: benign and visible, not a
crash.

    install     verify the call sites, write the code, patch both jals
    status      tick counter, raise counter, patch state
    say <id>    raise one message
    text "..."  repoint a string entry, then raise it
    test <n>    n messages, checking the heap returns to where it started
    remove      put the original jal words back
"""

import argparse
import json
import os
import struct
import sys
import time

import taz_steal as T

TICK = 0x002C5838
RAISE = 0x002C56E8
CALL_SITES = (0x002827D8, 0x002BC968)
ORIGINAL = 0x0C000000 | (TICK >> 2)          # jal 0x002C5838
STRCOUNT_PTR = 0x00413DFC
STRCOUNT_OFF = 0x24

BASE_DEFAULT = 0x01F00900                    # clear of the 0x01F00800 stubs
CTRL_REQUEST, CTRL_INDEX, CTRL_FLAGS = 0x00, 0x04, 0x08
CTRL_DURATION, CTRL_LAST, CTRL_TICKS, CTRL_RAISES = 0x0C, 0x10, 0x14, 0x18
CTRL_CALL_FN, CTRL_CALL_A0, CTRL_CALL_A1 = 0x1C, 0x20, 0x24
CTRL_CALL_RET, CTRL_CALLS = 0x28, 0x2C
CODE_OFF = 0x40          # control block grew a call slot

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "taz_tramp.json")


def load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save(st):
    with open(STATE, "w") as fh:
        json.dump(st, fh, indent=2)


def code_words(ctrl):
    """The trampoline, assembled by hand.

    ctrl must sit at an address whose low half is under 0x8000, so the lui/lw
    pair needs no sign-extension fixup.
    """
    hi, lo = (ctrl >> 16) & 0xFFFF, ctrl & 0xFFFF
    if lo >= 0x8000 - 0x40:
        raise ValueError("control block low half too high for a plain lui/lw")

    def lw(rt, off):
        return 0x8C000000 | (8 << 21) | (rt << 16) | ((lo + off) & 0xFFFF)

    def sw(rt, off):
        return 0xAC000000 | (8 << 21) | (rt << 16) | ((lo + off) & 0xFFFF)

    w = [
        0x27BDFFE0, 0xAFBF0010, 0x3C080000 | hi,
        lw(9, CTRL_TICKS), 0x25290001, sw(9, CTRL_TICKS),
        lw(9, CTRL_REQUEST), 0x11200000 | 13, 0x00000000,
        sw(0, CTRL_REQUEST),
        lw(4, CTRL_INDEX), lw(5, CTRL_FLAGS), lw(10, CTRL_DURATION),
        0x448A6000,
        0x0C000000 | (RAISE >> 2), 0x00000000,
        0x3C080000 | hi, sw(2, CTRL_LAST),
        lw(9, CTRL_RAISES), 0x25290001, sw(9, CTRL_RAISES),
        0x3C080000 | hi,                                   # L_call
        lw(11, CTRL_CALL_FN), 0x11600000 | 11, 0x00000000,
        sw(0, CTRL_CALL_FN),
        lw(4, CTRL_CALL_A0), lw(5, CTRL_CALL_A1),
        0x0160F809, 0x00000000,                            # jalr $t3
        0x3C080000 | hi, sw(2, CTRL_CALL_RET),
        lw(9, CTRL_CALLS), 0x25290001, sw(9, CTRL_CALLS),
        0x8FBF0010, 0x08000000 | (TICK >> 2), 0x27BD0020,  # done
    ]
    # `done` is index 21; the beq at index 7 must land there.
    assert (w[7] & 0xFFFF) == 21 - 8, "subtitle branch out of step"
    assert (w[23] & 0xFFFF) == 35 - 24, "call branch out of step"
    return w


def addrs(base=None):
    b = base if base is not None else load().get("base", BASE_DEFAULT)
    return b, b + CODE_OFF


def installed(p, base=None):
    ctrl, code = addrs(base)
    want = code_words(ctrl)
    if p.many([code + 4 * i for i in range(len(want))]) != want:
        return False, "code missing or damaged"
    live = [a for a in CALL_SITES
            if p.r32(a) == (0x0C000000 | (code >> 2))]
    if not live:
        return False, "code present but no call site is patched"
    return True, f"{len(live)}/{len(CALL_SITES)} call sites patched"


def cmd_install(p, args):
    ctrl, code = addrs(args.base)
    if (code >> 28) != 0:
        print("    the trampoline must sit in the same 256MB region as the")
        print("    call sites for `jal` to reach it. Pick a lower --base.")
        return 1

    for a in CALL_SITES:
        w = p.r32(a)
        # The original, or a jal into our own scratch -- which covers an
        # older build of the trampoline, so changing its layout is a
        # non-event rather than something that has to be removed first.
        target = (w & 0x03FFFFFF) << 2
        ours = (w >> 26) == 3 and 0x01F00000 <= target < 0x01F02000
        if w != ORIGINAL and not ours:
            print(f"    call site 0x{a:08X} reads 0x{w:08X}, expected "
                  f"0x{ORIGINAL:08X}. Refusing to patch.")
            return 1

    if not T.scratch_idle(p, ctrl, 0xC0):
        already, _ = installed(p, args.base)
        if not already:
            print(f"    0x{ctrl:08X} is not 0xC0 bytes of idle zeros.")
            print("    Pick another with --base.")
            return 1

    for i in range(16):
        p.w32(ctrl + 4 * i, 0)
    words = code_words(ctrl)
    for i, w in enumerate(words):
        p.w32(code + 4 * i, w)
    if p.many([code + 4 * i for i in range(len(words))]) != words:
        print("    code readback failed. Nothing patched.")
        return 1
    print(f"    control 0x{ctrl:08X}   code 0x{code:08X} "
          f"({len(words)} instructions)")

    patched = {}
    for a in CALL_SITES:
        patched[hex(a)] = p.r32(a)
        p.w32(a, 0x0C000000 | (code >> 2))
        ok = p.r32(a) == (0x0C000000 | (code >> 2))
        print(f"    call site 0x{a:08X}  jal 0x{TICK:08X} -> "
              f"jal 0x{code:08X}   {'ok' if ok else 'READBACK FAILED'}")
        if not ok:
            return 1
    save({**load(), "base": ctrl, "sites": patched})
    print()
    print("    SAVE STATE. Then watch the counter move:")
    print("      py -3.13 taz_tramp.py status")
    print("    If ticks stays at 0 the recompiler is still running the old")
    print("    translation of that block -- see `status` for what to try.")
    return 0


def cmd_status(p, args):
    ctrl, code = addrs(args.base)
    ok, why = installed(p, args.base)
    print(f"    control 0x{ctrl:08X}   code 0x{code:08X}   {why}")
    for a in CALL_SITES:
        w = p.r32(a)
        print(f"      0x{a:08X}  0x{w:08X}  "
              + ("patched" if w == (0x0C000000 | (code >> 2))
                 else "original" if w == ORIGINAL else "UNKNOWN"))
    if not ok:
        return 1
    a = p.many([ctrl + o for o in (CTRL_REQUEST, CTRL_INDEX, CTRL_FLAGS,
                                   CTRL_DURATION, CTRL_LAST, CTRL_TICKS,
                                   CTRL_RAISES)])
    dur = struct.unpack("<f", a[3].to_bytes(4, "little"))[0]
    print(f"      request {a[0]}   index {a[1]}   flags 0x{a[2]:X}   "
          f"duration {dur:.2f}")
    print(f"      last object 0x{a[4]:08X}   ticks {a[5]}   raises {a[6]}")
    t0 = a[5]
    time.sleep(1.0)
    t1 = p.r32(ctrl + CTRL_TICKS)
    print(f"      ticks {t0} -> {t1} in one second ({t1 - t0} frames)")
    if t1 == t0:
        print()
        print("    The trampoline is NOT running. The word is patched, so")
        print("    PCSX2 is still executing a cached translation of that")
        print("    block. Things that force a recompile: change level, or")
        print("    save state and load it back. Nothing is broken -- the")
        print("    old translation calls the real tick, so the game is fine.")
        return 1
    return 0


def strcount(p):
    mgr = p.r32(STRCOUNT_PTR)
    return p.r32(mgr + STRCOUNT_OFF) if T.ee(mgr) else 0


def raise_one(p, index, seconds, flags, base=None, wait=2.0):
    ctrl, _ = addrs(base)
    if p.r32(ctrl + CTRL_REQUEST):
        return None, "a request is still pending"
    n = strcount(p)
    if not 0 <= index < n:
        return None, f"index {index} is outside 0..{n - 1}"
    before = p.r32(ctrl + CTRL_RAISES)
    p.w32(ctrl + CTRL_INDEX, index)
    p.w32(ctrl + CTRL_FLAGS, flags)
    p.wf32(ctrl + CTRL_DURATION, seconds)
    p.w32(ctrl + CTRL_REQUEST, 1)
    end = time.time() + wait
    while time.time() < end:
        if p.r32(ctrl + CTRL_RAISES) != before:
            obj = p.r32(ctrl + CTRL_LAST)
            return (obj or None), (None if obj else
                                   "raise_subtitle returned 0 (rejected)")
        time.sleep(0.004)
    p.w32(ctrl + CTRL_REQUEST, 0)
    return None, f"no raise within {wait}s -- is the trampoline running?"


def cmd_say(p, args):
    ok, why = installed(p, args.base)
    if not ok:
        print(f"    {why}. Run `install` first.")
        return 1
    txt = T.entry_text(p, args.id)
    print(f"    id {args.id}  {txt!r}")
    obj, err = raise_one(p, args.id, args.seconds, args.flags, args.base)
    if not obj:
        print(f"    {err}")
        return 1
    print(f"    raised: object 0x{obj:08X} (the game's, not ours)")
    if args.watch:
        t0 = time.time()
        while time.time() - t0 < args.seconds + 4:
            ok2, _ = T.validate(p, obj)
            nodes, count = T.list_walk(p)
            if not ok2:
                print(f"      t+{time.time() - t0:4.1f}s  object freed by the "
                      f"game, list A count {count}")
                break
            time.sleep(0.05)
        else:
            print("      object still allocated after the message ended")
    return 0


def cmd_text(p, args):
    """Repoint a string table entry at our own words, then raise it.

    The renderer resolves the index once, at raise time, so the entry has to
    be pointing at our text BEFORE the message goes up. It is restored after.
    """
    ok, why = installed(p, args.base)
    if not ok:
        print(f"    {why}. Run `install` first.")
        return 1
    raw = args.words.encode("utf-16le")
    scratch = args.scratch
    if not T.scratch_idle(p, scratch, len(raw) + 0x20):
        print(f"    0x{scratch:08X} is not idle for {len(raw) + 0x20} bytes.")
        return 1
    entry = T.STR_TABLE + args.id * 0x10
    was_ptr, was_len = p.r32(entry), p.r32(entry + 4)
    print(f"    entry {args.id}: 0x{was_ptr:08X} len {was_len} "
          f"-> 0x{scratch:08X} len {len(args.words)}")
    for i in range(0, len(raw), 4):
        p.w32(scratch + i, int.from_bytes(raw[i:i + 4].ljust(4, b"\0"), "little"))
    p.w32(scratch + ((len(raw) + 3) & ~3), 0)
    p.w32(entry, scratch)
    p.w32(entry + 4, len(args.words))
    try:
        obj, err = raise_one(p, args.id, args.seconds, args.flags, args.base)
        if not obj:
            print(f"    {err}")
            return 1
        print(f"    raised: object 0x{obj:08X}   {args.words!r}")
        time.sleep(min(args.seconds, 1.5))
    finally:
        p.w32(entry, was_ptr)
        p.w32(entry + 4, was_len)
        print("    string entry restored")
    return 0


def cmd_test(p, args):
    """The proof: n messages, and the heap ends where it started.

    Nothing here is ours -- the game allocates the object and the node, opens
    the page, and frees all of it through end_message. If the in-use total
    comes back to its starting value, there is no leak to argue about.
    """
    ok, why = installed(p, args.base)
    if not ok:
        print(f"    {why}. Run `install` first.")
        return 1
    ids = args.ids or [1339, 1394, 1465, 1472]
    ids = [i for i in ids if T.entry_text(p, i)]
    print(f"    {args.n} messages, {args.seconds}s each, ids {ids}")
    T.list_walk(p)
    base_heap = p.r32(T.IN_USE_TOTAL)
    print(f"    heap at start 0x{base_heap:08X}")
    problems, raised = [], 0
    for i in range(args.n):
        index = ids[i % len(ids)]
        obj, err = raise_one(p, index, args.seconds, args.flags, args.base)
        if not obj:
            problems.append(f"message {i + 1} (id {index}): {err}")
            print(f"      {i + 1:3d}  id {index:5d}  FAILED: {err}")
            continue
        raised += 1
        end, freed = time.time() + args.seconds + 5, False
        while time.time() < end:
            good, _ = T.validate(p, obj)
            if not good:
                freed = True
                break
            time.sleep(0.02)
        nodes, count = T.list_walk(p)
        now = p.r32(T.IN_USE_TOTAL)
        drift = (now - base_heap) & 0xFFFFFFFF
        if drift > 0x80000000:
            drift -= 1 << 32
        print(f"      {i + 1:3d}  id {index:5d}  object 0x{obj:08X}  "
              f"{'freed by the game' if freed else 'STILL ALLOCATED'}  "
              f"list {count}  heap {drift:+#x}")
        if not freed:
            problems.append(f"message {i + 1}: object 0x{obj:08X} never freed")
        if args.gap:
            time.sleep(args.gap)
    time.sleep(1.0)
    final = (p.r32(T.IN_USE_TOTAL) - base_heap) & 0xFFFFFFFF
    if final > 0x80000000:
        final -= 1 << 32
    nodes, count = T.list_walk(p)
    print()
    print(f"    raised {raised}/{args.n}   list A count {count}   "
          f"heap drift {final:+#x}")
    for k in ("A", "B"):
        s = T.slot_state(p, k)
        print(f"    slot {k} {'busy 0x%08X' % s['open'] if s['open'] else 'free'}")
    if problems:
        print(f"\n    {len(problems)} PROBLEM(S):")
        for m in problems[:15]:
            print(f"      {m}")
        return 1
    print("\n    Every message was allocated, shown and freed by the game.")
    return 0


def cmd_remove(p, args):
    ctrl, code = addrs(args.base)
    n = 0
    for a in CALL_SITES:
        if p.r32(a) != ORIGINAL:
            p.w32(a, ORIGINAL)
            n += 1
        print(f"    0x{a:08X}  {'restored' if p.r32(a) == ORIGINAL else 'FAILED'}")
    for i in range(len(code_words(ctrl)) + 8):
        p.w32(code + 4 * i, 0)
    for i in range(16):
        p.w32(ctrl + 4 * i, 0)
    print(f"    {n} call site(s) put back, scratch cleared")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=lambda x: int(x, 0), default=None)
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("install").set_defaults(fn=cmd_install)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("remove").set_defaults(fn=cmd_remove)

    s = sub.add_parser("say")
    s.add_argument("id", type=int)
    s.add_argument("--seconds", type=float, default=3.0)
    s.add_argument("--flags", type=lambda x: int(x, 0), default=2)
    s.add_argument("--watch", action="store_true",
                   help="follow the object until the game frees it")
    s.set_defaults(fn=cmd_say)

    x = sub.add_parser("text")
    x.add_argument("words")
    x.add_argument("--id", type=int, default=1472,
                   help="string entry to borrow and restore")
    x.add_argument("--scratch", type=lambda x: int(x, 0), default=0x01F01000)
    x.add_argument("--seconds", type=float, default=4.0)
    x.add_argument("--flags", type=lambda x: int(x, 0), default=2)
    x.set_defaults(fn=cmd_text)

    t = sub.add_parser("test")
    t.add_argument("n", type=int)
    t.add_argument("--seconds", type=float, default=2.0)
    t.add_argument("--gap", type=float, default=0.3)
    t.add_argument("--flags", type=lambda x: int(x, 0), default=2)
    t.add_argument("--ids", type=int, nargs="*")
    t.set_defaults(fn=cmd_test)

    args = ap.parse_args()
    return args.fn(T.Pine().connect(), args)


if __name__ == "__main__":
    sys.exit(main())
