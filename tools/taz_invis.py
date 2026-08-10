#!/usr/bin/env python3
"""Invisibility: the flag, and the look, which are two different things.

    py -3.13 taz_invis.py check              verify the addresses vs the dump
    py -3.13 taz_invis.py status             what Taz is drawn with right now
    py -3.13 taz_invis.py watch              RECORD a real vanilla pickup
    py -3.13 taz_invis.py try                grant it the way the client does
    py -3.13 taz_invis.py try --secs 8

`try` is the one that answers the question. It runs the SHIPPED
Game.grant_effect / hold_traps / end_powerup, so what you see is what the
client does -- not a re-typed copy of it. Taz should go translucent within a
frame, without spinning, and go solid again when it ends.

`watch` records the game doing it itself. Pick up a real invisibility while it
runs and every field that moves is printed, which is the check on everything
below. Nothing is written in `watch` or `status`.

Close the AP client first -- PINE takes one connection at a time.


WHY THE FLAG WAS NOT ENOUGH
---------------------------
Read out of ee_dump.bin instruction by instruction; every claim carries the
address it came from.

The pickup handler is 0x0024BA88 and dispatches on the pickup type through
the table at 0x004A6F30; type 7 is invisibility, at 0x0024C000. Its writes,
in order:

    0x0024C0CC  sw   v0, 0x194(v1)     costume +0x194 = 1      the flag
    0x0024C0DC  sw   zero, 0x160(v0)   costume +0x160 = 0.0    the timer
    0x0024C0E4  sw   zero, 0x164(v1)   costume +0x164 = 0.0    blink phase
    0x0024C0F0  swc1 f0, 0x168(v0)     costume +0x168 = 0.75   blink period
    0x0024C0EC  jal  0x23f3c8          <-- THE VISUAL
    0x0024C108  sw   v0, 0x16c(a0)     costume +0x16C = 2

and nothing else. Note there is no store to +0x170 anywhere on this path.

0x0023F3C8(obj, desc) is two calls to the setter at 0x0030DFB8, four
instructions long, which writes obj[0x140 + slot*4] = mode and
obj[0x150 + slot*4] = param. So the visual is four words, and they are on the
TAZ object rather than the costume:

    Taz+0x140 = 3      Taz+0x150 = 0
    Taz+0x144 = 4      Taz+0x154 = 0x003AAE80

0x003AAE80 is {3.5, 0x80, 0x80, 0x80, 0x80} -- RGBA 128 across, half alpha.
The renderer's mode-4 handler at 0x003059A0 loads it at 0x003059AC and reads
R, G, B and A out of it into the GIF packet every draw. No dirty flag, no
compare against a previous value: write the words and the next frame is
translucent.

Which is why spinning looked like the fix. STATE_SPINUP dispatches to
0x0024FEF8, which resets the material and then re-reads the flag at
0x00250048 and re-applies at 0x00250058. Four other paths do the same --
0x00250640, the state machine's electrocution case, SetModel at 0x0023F560,
and 0x0017F178 -- so any of them would have worked. Spinning is just the one
a player does by accident.

The opaque values are what the game's own restore (0x0023F1A8) leaves:
Taz+0x140 = 2, +0x150 = 0x003AAE50, +0x144 = 4, +0x154 = 0x003AAE60.

THE TIMER, AND THE BLINK
------------------------
+0x160 counts UP for invisibility, not down. The tick at 0x001C6790 adds the
frame delta (0x001C67C0), starts blinking past 20.0 (0x001C67D0) and ends the
whole effect past 25.0 (0x001C68C0) through 0x001C68E8, which clears the flag
and restores the material. The blink is that same 0x0023F3C8 / 0x0023F1A8 pair
being toggled: +0x168 is the half-period, 0.75 at grant, shrinking x0.75 per
toggle to a 0.25 floor (0x001C6870), so the flashing speeds up as it runs out.

So the game runs the whole effect itself. The client's old 18.98 was not
out-writing a countdown -- it parked the timer just under the blink threshold,
where the tick does nothing, which is exactly why it never blinked and never
expired. Re-asserting the material every tick would have cancelled the blink
even if the timer had been left alone.

The length is therefore set by where the timer STARTS, since it always ends at
25.0: game.py plants INVIS_START = 25.0 - INVIS_SECONDS and then leaves the
effect entirely alone. At INVIS_SECONDS = 15.0 that is 10 seconds solid and
the game's own 5-second blink-out. The five seconds are not adjustable without
patching the two thresholds, which are constants in the game's code.
"""

import argparse
import importlib.util
import os
import struct
import sys
import time

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")
DUMP = os.path.join(HERE, "ee_dump.bin")

MAT_OPAQUE = (2, 0x003AAE50, 4, 0x003AAE60)      # what 0x0023F1A8 leaves

EXPECT = [
    (0x0024C0CC, 0xAC620194, "sw v0, 0x194(v1)      the flag"),
    (0x0024C0DC, 0xAC400160, "sw zero, 0x160(v0)    the timer"),
    (0x0024C0E4, 0xAC600164, "sw zero, 0x164(v1)    blink phase"),
    (0x0024C0EC, 0x0C08FCF2, "jal 0x23f3c8          THE VISUAL"),
    (0x0024C0F0, 0xE4400168, "swc1 f0, 0x168(v0)    blink period, 0.75"),
    (0x0024C108, 0xAC82016C, "sw v0, 0x16c(a0)      = 2"),
    (0x0023F3E8, 0x0C0C37EE, "jal 0x30dfb8          slot 0, mode 3"),
    (0x0023F400, 0x8C4335D0, "lw v1, 0x35d0(v0)     reads MAT_FLAGS"),
    (0x0023F404, 0x30630002, "andi v1, v1, 2        ... bit 1"),
    (0x0023F428, 0x0C0C37EE, "jal 0x30dfb8          slot 1, mode 4"),
    (0x0030DFC4, 0xAC460140, "sw a2, 0x140(v0)      mode[slot]"),
    (0x0030DFCC, 0xAC870150, "sw a3, 0x150(a0)      param[slot]"),
    (0x003059AC, 0x8C500150, "lw s0, 0x150(v0)      renderer reads param"),
    (0x00305A18, 0x8E020004, "lw v0, 4(s0)          ... R out of it"),
    (0x00305A20, 0x8E030008, "lw v1, 8(s0)          ... G"),
    (0x00305A28, 0x8E04000C, "lw a0, 0xc(s0)        ... B"),
    (0x00305A30, 0x8E020010, "lw v0, 0x10(s0)       ... and A, every draw"),
    (0x00250048, 0x8C420194, "lw v0, 0x194(v0)      spin re-reads the flag"),
    (0x00250058, 0x0C08FCF2, "jal 0x23f3c8          ... and re-applies"),
    (0x001C67C0, 0x46020000, "add.s f0, f0, f2      +0x160 counts UP"),
    (0x001C6920, 0xAC600194, "sw zero, 0x194(v1)    the game's own clear"),
]


def load_game():
    """The shipped game.py, under a synthetic package. Same trick as
    taz_despawn.py, and for the same reason: this exercises the real code."""
    import types
    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    sys.modules["tazworld"] = pkg
    for name in ("_imports", "logic", "game"):
        path = os.path.join(WORLD, name + ".py")
        spec = importlib.util.spec_from_file_location("tazworld." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tazworld." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
    return sys.modules["tazworld.game"]


def hooked(G):
    mem = G.mem
    if mem is None:
        print("    pcsx2_mem did not import, so there is nothing to hook.")
        return None, None
    game = G.Game()
    try:
        if not game.connect():
            raise RuntimeError("connect() said no")
    except Exception as e:
        print(f"    could not reach PCSX2: {e}")
        print("    Is it running with the game booted, PINE on (Settings -> "
              "Advanced, slot 28011),\n    and the AP client CLOSED?")
        return None, None
    return game, mem


def taz_ptr(G, mem):
    p = mem.read_u32(G.T.TAZ_PTR)
    return p if mem.valid_ptr(p) else None


def read_state(G, mem, taz):
    """The four material words and the costume fields that go with them."""
    T = G.T
    out = {}
    for name, off in (("mode0", T.O_MAT_MODE), ("param0", T.O_MAT_PARAM),
                      ("mode1", T.O_MAT_MODE + 4),
                      ("param1", T.O_MAT_PARAM + 4)):
        out[name] = mem.read_u32(taz + off)
    c = mem.read_u32(taz + T.O_COSTUME_PTR)
    out["costume"] = c
    if mem.valid_ptr(c):
        out["flag"] = mem.read_u32(c + T.C_INVISIBLE)
        out["timer"] = round(mem.read_float(c + T.C_POWER_TIME), 4)
        out["phase"] = round(mem.read_float(c + T.C_BLINK_PHASE), 4)
        out["blink"] = round(mem.read_float(c + T.C_BLINK_HALF), 4)
        out["active"] = mem.read_u32(c + T.C_ACTIVE_ID)
        out["sub"] = mem.read_u32(c + T.C_ACTIVE_SUB)
    st = mem.read_u32(taz + T.O_STATE_PTR)
    out["state"] = mem.read_u8(st + T.S_STATE) if mem.valid_ptr(st) else None
    return out


def show(G, s, prefix="    "):
    mat = (s["mode0"], s["param0"], s["mode1"], s["param1"])
    if mat == (3, 0, 4, G.T.MAT_INVISIBLE):
        what = "TRANSLUCENT"
    elif mat == MAT_OPAQUE:
        what = "opaque"
    else:
        what = "something else"
    print(f"{prefix}material  {mat[0]}, 0x{mat[1]:08X}, {mat[2]}, "
          f"0x{mat[3]:08X}   -> {what}")
    if "flag" in s:
        print(f"{prefix}costume   flag={s['flag']}  timer={s['timer']}  "
              f"phase={s['phase']}  blink={s['blink']}  "
              f"active={s['active'] if s['active'] != 0xFFFFFFFF else -1}  "
              f"sub=0x{s['sub']:08X}")
    print(f"{prefix}state     0x{s['state']:02X}" if s["state"] is not None
          else f"{prefix}state     unavailable")


# ------------------------------------------------------------------- check

def cmd_check(_args):
    if not os.path.exists(DUMP):
        print(f"    no {os.path.basename(DUMP)} -- take one with taz_ramdump.py")
        return 2
    d = open(DUMP, "rb").read()
    if len(d) < 0x02000000:
        print(f"    {os.path.basename(DUMP)} is {len(d)} bytes, expected 32MB")
        return 2

    bad = 0
    print(f"    {os.path.basename(DUMP)}: offset == EE address\n")
    for addr, want, what in EXPECT:
        got = struct.unpack_from("<I", d, addr)[0]
        ok = got == want
        bad += not ok
        print(f"    {'ok  ' if ok else 'BAD '} 0x{addr:08X}  {got:08X}"
              f"{'' if ok else f' (expected {want:08X})'}  {what}")

    print()
    inv = struct.unpack_from("<f", d, 0x003AAE80)[0]
    rgba = [struct.unpack_from("<I", d, 0x003AAE80 + 4 * i)[0]
            for i in range(1, 5)]
    print(f"    0x003AAE80  {{{inv}, {', '.join(str(v) for v in rgba)}}}"
          f"   half alpha -- the invisibility material")
    nor = struct.unpack_from("<f", d, 0x003AAE60)[0]
    rgba = [struct.unpack_from("<I", d, 0x003AAE60 + 4 * i)[0]
            for i in range(1, 5)]
    print(f"    0x003AAE60  {{{nor}, {', '.join(str(v) for v in rgba)}}}"
          f"   opaque -- the default")
    print(f"    0x004125D0  {struct.unpack_from('<I', d, 0x004125D0)[0]}"
          f"              MAT_FLAGS; bit 1 would skip slot 1")

    print()
    if bad:
        print(f"    {bad} of {len(EXPECT)} did not match -- these addresses "
              f"are for a different build.\n    Do not write anything.")
        return 1
    print(f"    all {len(EXPECT)} instructions match.")
    return 0


# ------------------------------------------------------------------ status

def cmd_status(_args):
    G = load_game()
    game, mem = hooked(G)
    if not game:
        return 2
    taz = taz_ptr(G, mem)
    if not taz:
        print("    no Taz object -- in a menu or loading?")
        return 1
    print(f"    Taz @ 0x{taz:08X}\n")
    show(G, read_state(G, mem, taz))
    return 0


# ------------------------------------------------------------------- watch

def cmd_watch(args):
    G = load_game()
    game, mem = hooked(G)
    if not game:
        return 2
    print("    Watching. Pick up a REAL invisibility and let it run out.\n"
          "    Every field that moves is printed. Nothing is written.\n"
          "    Ctrl-C to stop.\n")
    t0 = time.time()
    prev = None
    try:
        while time.time() - t0 < args.secs:
            taz = taz_ptr(G, mem)
            if not taz:
                time.sleep(0.1)
                continue
            try:
                now = read_state(G, mem, taz)
            except Exception:
                time.sleep(0.05)
                continue
            if prev is None:
                print(f"    {time.time() - t0:7.2f}s  baseline")
                show(G, now, "              ")
            elif now != prev:
                moved = {k: (prev.get(k), v) for k, v in now.items()
                         if prev.get(k) != v}
                bits = "  ".join(
                    f"{k}: {a if not isinstance(a, int) else hex(a)}"
                    f" -> {b if not isinstance(b, int) else hex(b)}"
                    for k, (a, b) in moved.items())
                print(f"    {time.time() - t0:7.2f}s  {bits}")
            prev = now
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n    stopped.")
    G.mem.un_hook()
    return 0


# --------------------------------------------------------------------- try

def cmd_try(args):
    """Grant it through the shipped code, hold, then end -- and watch."""
    G = load_game()
    game, mem = hooked(G)
    if not game:
        return 2
    taz = taz_ptr(G, mem)
    if not taz:
        print("    no Taz object -- load a level first.")
        return 1

    print(f"    Taz @ 0x{taz:08X}\n    before:")
    show(G, read_state(G, mem, taz))

    # grant_effect defers until Taz is at a safe moment, exactly as the client
    # does, so this may take a couple of tries. Do not fight it.
    end = None
    for _ in range(60):
        end = game.grant_effect("invisibility")
        if end != "defer":
            break
        time.sleep(0.1)
    if end == "defer":
        print("\n    Taz never reached a safe state -- stop spinning and "
              "try again.")
        return 1

    print("\n    granted:")
    show(G, read_state(G, mem, taz))
    secs = args.secs if args.secs is not None else G.T.INVIS_SECONDS
    print(f"\n    >>> Taz should be translucent NOW, without spinning.")
    print(f"    >>> The GAME runs it from here: {G.T.INVIS_SECONDS - 5.0:.0f}s "
          f"solid, then a {G.T.INVIS_BLINK_FOR:.0f}s blink-out, then it ends "
          f"itself.\n")

    # Every toggle of the material is one blink, so printing the changes makes
    # the blink visible here as well as on screen -- and shows it speeding up.
    active = {"invisibility": time.time() + secs + 4.0}
    t0, prev, blinks = time.time(), None, 0
    while active:
        for name in game.hold_traps(active):
            active.pop(name, None)
        try:
            s = read_state(G, mem, taz)
        except Exception:
            time.sleep(0.05)
            continue
        mat = (s["mode0"], s["param0"], s["mode1"], s["param1"])
        if prev is not None and mat != prev:
            blinks += 1
            on = mat == (3, 0, G.T.MAT_INVISIBLE) or mat[0] == 3
            print(f"    {time.time() - t0:6.2f}s  material "
                  f"{'ON ' if on else 'off'}   timer={s.get('timer')}  "
                  f"blink={s.get('blink')}")
        if prev is not None and s.get("flag") == 0 and prev is not None:
            print(f"    {time.time() - t0:6.2f}s  the game ended it "
                  f"({blinks} material changes)")
            break
        prev = mat
        time.sleep(0.03)

    game.end_powerup("invisibility")
    print("\n    ended:")
    show(G, read_state(G, mem, taz))
    mat = tuple(read_state(G, mem, taz)[k]
                for k in ("mode0", "param0", "mode1", "param1"))
    if mat == MAT_OPAQUE:
        print("\n    Back to the opaque material. Nothing left behind.")
    else:
        print("\n    NOT back to opaque. Either the game re-applied the "
              "material itself\n    while this ran -- which the restore is "
              "meant to leave alone -- or the\n    teardown is wrong. Say "
              "which it looked like.")
    G.mem.un_hook()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="verify the addresses against ee_dump.bin")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("status", help="what Taz is drawn with right now")
    s.set_defaults(fn=cmd_status)

    w = sub.add_parser("watch", help="record a real vanilla pickup")
    w.add_argument("--secs", type=float, default=300.0)
    w.add_argument("--interval", type=float, default=0.05)
    w.set_defaults(fn=cmd_watch)

    t = sub.add_parser("try", help="grant it the way the client does")
    t.add_argument("--secs", type=float, default=None,
                   help="how long to watch; defaults to INVIS_SECONDS")
    t.set_defaults(fn=cmd_try)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
