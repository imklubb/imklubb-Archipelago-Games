#!/usr/bin/env python3
"""The game's slow-motion, and the text it puts on screen during it.

    py -3.13 taz_slowmo.py check                 verify the addresses vs the dump
    py -3.13 taz_slowmo.py watch                 RECORD a real slowdown
    py -3.13 taz_slowmo.py watch --secs 180
    py -3.13 taz_slowmo.py watch --raw           every sample, not just changes

`watch` is the one that matters. Trigger a Golden Sam Statue, a destruction
bonus, or the West boss while it runs, and the recording says exactly what
moves and in what order. Nothing is written; it is safe on a real seed.

Close the AP client first -- PINE takes one connection at a time.


WHAT THIS WATCHES, AND WHY THOSE ADDRESSES
------------------------------------------
Read out of ee_dump.bin, instruction by instruction. Every claim below has
the address it came from so it can be checked without trusting this file.

The engine has a time scale. `SetTimeScale(f12)` at 0x002C8DD0 stores the
request at 0x004752B8 (0x002C8DE0) and tail-calls 0x002C9198, which copies
it to 0x004125CC (0x002C91B8). That copy is the live one: the frame-time
routine at 0x002859A8 loads it (0x00285A20) and multiplies it into the
delta (0x00285A28), which lands in 0x00412664 -- the dt that 473 readers
use. So `float at 0x004125CC < 1.0` IS the slowdown, whoever asked for it.

Only two things ask for less than 1.0:

  * The bounty/cash banner, 0x00201DD0. It writes 0.25 into its own factor
    at 0x00201F58, decays that toward a 0.1 floor, then ramps back and
    calls SetTimeScale(1.0) at 0x002027A0 while clearing its state word at
    0x002027A4. Its per-frame driver 0x00202140 hands the factor to
    SetTimeScale at 0x00202830.

    This is the one the player sees, because the banner is the text. It is
    raised for the Golden Sam Statue (string 421, from 0x0024C8F0) and for
    the destruction bonus (string 425, from 0x0021072C, 0x002106D4 and
    0x00210664 -- the 50%, 75% and 100% tiers).

  * The West boss, WestBoss.cpp: SetTimeScale(0.5) at 0x00190240 and
    0x001904C8, restored at 0x0019007C, 0x00190A68 and 0x00190D54.

POPUP_STATE (0x003CA3B4) is the banner's own state word, 0 when idle and
1..5 while it runs. There is a getter for it in the game at 0x00202100.

Recorded, with X being mashed to skip: a Golden Sam Statue runs 5.09s and a
smashed Wanted Poster 2.91s, both 0.245 decaying to a 0.10 floor, holding,
then ramping back, through states 2, 3, 4, 5, 0. Note the poster -- the
dump only turned up the statue and destruction-bonus call sites, so the
list of triggers was incomplete from the start and gating on the time
scale rather than on the triggers is what made that not matter.

POPUP_STATE was expected to outlast the scale, since the banner ramps its
slowdown back out. At 30ms sampling it does not: scale and state reach 1.0
and 0 on the same sample. Both are still watched -- one read each, and the
state is the one that actually means "the banner is up".

Two things this is NOT:

  * GAME_STATE does not move. 0x003FF040 is written in exactly one place,
    0x00284E3C inside SetGameState, and none of that function's 66 callers
    is in the banner or boss code. It stays 1 (Active) throughout.

  * The 100-sandwich line (string 422) is not a slowdown at all. It goes
    to raise_subtitle at 0x0024C7E4 with a1=2 and f12=5.0, so it lands on
    subtitle list A and notify.idle() already refuses while it is up. It
    is watched here anyway, to prove that.
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

TIME_SCALE = 0x004125CC          # live engine time scale, float
TIME_SCALE_REQ = 0x004752B8      # what SetTimeScale was last asked for
POPUP_STATE = 0x003CA3B4         # bounty banner state, 0 = idle
POPUP_FACTOR = 0x003CA3B0        # the banner's own slow-mo factor
POPUP_EXPIRY = 0x003CA3A4        # compared against game time 0x003FF054
FRAME_DT = 0x00412664            # the scaled delta the game actually uses
GAME_TIME = 0x003FF054
GAME_STATE = 0x003FF040

LIST_A = 0x00508FE0
LIST_COUNT = 0x30
SLOT_A = 0x004746A0
SLOT_OPEN = 0x194


# The instructions the addresses above were read from. `check` asserts each
# one still decodes to the same word, so a wrong build is caught before it
# is ever written to.
EXPECT = [
    (0x002C8DE0, 0xE44C52B8, "swc1 $f12, 0x52b8($v0)   SetTimeScale stores"),
    (0x002C91A0, 0xC44052B8, "lwc1 $f0, 0x52b8($v0)    ... and reloads it"),
    (0x002C91B8, 0xE48035CC, "swc1 $f0, 0x35cc($a0)    -> 0x004125CC"),
    (0x00285A20, 0xC44C35CC, "lwc1 $f12, 0x35cc($v0)   frame delta reads it"),
    (0x00285A28, 0x460C0302, "mul.s $f12, $f0, $f12    ... and scales by it"),
    (0x00201F58, 0xE440A3B0, "swc1 $f0, -0x5c50($v0)   banner factor = 0.25"),
    (0x00202034, 0xAEA2A3B4, "sw $v0, -0x5c4c($s5)     banner state = 1"),
    (0x0020279C, 0xE440A3B0, "swc1 $f0, -0x5c50($v0)   factor = 1.0"),
    (0x002027A0, 0x0C0B2374, "jal 0x2c8dd0             SetTimeScale(1.0)"),
    (0x002027A4, 0xAEA0A3B4, "sw $zero, -0x5c4c($s5)   banner state = 0"),
    (0x00202830, 0x0C0B2374, "jal 0x2c8dd0             driver feeds the factor"),
    (0x00202108, 0x8C62A3B4, "lw $v0, -0x5c4c($v1)     the game's own getter"),
    (0x00190240, 0x0C0B2374, "jal 0x2c8dd0             WestBoss, with 0.5"),
    (0x00190238, 0x3C013F00, "lui $at, 0x3f00          ... that 0.5"),
    (0x0024C8F0, 0x0C080774, "jal 0x201dd0             statue, string 421"),
    (0x0021072C, 0x0C080774, "jal 0x201dd0             destruction, string 425"),
    (0x0024C7E4, 0x0C0B15BA, "jal 0x2c56e8             sandwiches -> subtitle"),
]


# ------------------------------------------------------------------- setup

def load_mem():
    """pcsx2_mem out of the world, so this uses the shipped one."""
    import types
    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    sys.modules["tazworld"] = pkg
    for name in ("pcsx2_mem",):
        path = os.path.join(WORLD, name + ".py")
        spec = importlib.util.spec_from_file_location("tazworld." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tazworld." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
    return sys.modules["tazworld.pcsx2_mem"]


# ------------------------------------------------------------------- check

def cmd_check(_args):
    """Every address above, against the dump. No emulator needed."""
    if not os.path.exists(DUMP):
        print(f"  no {os.path.basename(DUMP)} -- take one with taz_ramdump.py")
        return 2
    with open(DUMP, "rb") as fh:
        d = fh.read()
    if len(d) < 0x02000000:
        print(f"  {os.path.basename(DUMP)} is {len(d)} bytes, expected 32MB")
        return 2

    bad = 0
    print(f"  {os.path.basename(DUMP)}: {len(d)} bytes, offset == EE address\n")
    for addr, want, what in EXPECT:
        got = struct.unpack_from("<I", d, addr)[0]
        ok = got == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'BAD '} 0x{addr:08X}  {got:08X}"
              f"{'' if ok else f' (expected {want:08X})'}  {what}")

    print()
    for addr, name in ((TIME_SCALE, "time scale"),
                       (TIME_SCALE_REQ, "requested"),
                       (POPUP_FACTOR, "banner factor")):
        print(f"  0x{addr:08X}  {name:<14} = "
              f"{struct.unpack_from('<f', d, addr)[0]}")
    for addr, name in ((POPUP_STATE, "banner state"),
                       (GAME_STATE, "game state"),
                       (LIST_A + LIST_COUNT, "list A count")):
        print(f"  0x{addr:08X}  {name:<14} = "
              f"{struct.unpack_from('<I', d, addr)[0]}")

    print()
    if bad:
        print(f"  {bad} of {len(EXPECT)} did not match. These addresses are "
              f"for a different build -- do not write anything.")
        return 1
    print(f"  all {len(EXPECT)} instructions match. The dump was taken during "
          f"normal play,\n  so a time scale of 1.0 and a banner state of 0 "
          f"are the right idle values.")
    return 0


# ------------------------------------------------------------------- watch

FIELDS = [
    ("scale",   TIME_SCALE,          "f"),
    ("request", TIME_SCALE_REQ,      "f"),
    ("factor",  POPUP_FACTOR,        "f"),
    ("state",   POPUP_STATE,         "u"),
    ("expiry",  POPUP_EXPIRY,        "f"),
    ("dt",      FRAME_DT,            "f"),
    ("gstate",  GAME_STATE,          "u"),
    ("listA",   LIST_A + LIST_COUNT, "u"),
    ("slotA",   SLOT_A + SLOT_OPEN,  "u"),
]


def sample(mem):
    out = {}
    for name, addr, kind in FIELDS:
        try:
            out[name] = (mem.read_float(addr) if kind == "f"
                         else mem.read_u32(addr))
        except Exception:
            out[name] = None
    return out


def fmt(name, v):
    if v is None:
        return f"{name}=??"
    if isinstance(v, float):
        return f"{name}={v:.5f}"
    return f"{name}={v}"


def cmd_watch(args):
    mem = load_mem()
    if not mem.hook():
        print("  could not reach PCSX2. Is it running with the game booted, "
              "PINE on (Settings -> Advanced, slot 28011),\n  and the AP "
              "client CLOSED?")
        return 2
    print(f"  connected: {mem.game_id()}\n")
    print("  Watching. Trigger one of these and let it finish:\n"
          "    * a Golden Sam Statue        (banner, string 421)\n"
          "    * a destruction bonus        (banner, string 425)\n"
          "    * the West boss              (SetTimeScale 0.5)\n"
          "    * the 100th sandwich         (subtitle, string 422 -- should\n"
          "                                  move listA, NOT scale)\n")
    print("  Ctrl-C to stop.\n")

    t0 = time.time()
    prev = None
    slow_since = None
    events = []
    try:
        while time.time() - t0 < args.secs:
            now = sample(mem)
            if args.raw or prev is None or now != prev:
                t = time.time() - t0
                if prev is None:
                    line = "  ".join(fmt(k, now[k]) for k, _, _ in FIELDS)
                else:
                    line = "  ".join(fmt(k, now[k]) for k, _, _ in FIELDS
                                     if now[k] != prev[k]) or "(no change)"
                print(f"  {t:7.2f}s  {line}")

            scale = now.get("scale")
            slow = (scale is not None and scale < 0.999) or bool(now.get("state"))
            if slow and slow_since is None:
                slow_since = time.time()
                print(f"  {time.time() - t0:7.2f}s  >>> SLOWDOWN BEGINS")
            elif not slow and slow_since is not None:
                dur = time.time() - slow_since
                events.append(dur)
                print(f"  {time.time() - t0:7.2f}s  <<< slowdown ends "
                      f"({dur:.2f}s)")
                slow_since = None

            prev = now
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  stopped.")

    print()
    if events:
        print(f"  {len(events)} slowdown(s), "
              f"{min(events):.2f}s to {max(events):.2f}s long.")
        print("  That is how long the notifier would hold its queue.")
    else:
        print("  No slowdown seen. Either none was triggered, or the signal "
              "is not\n  what this file says it is -- say which, because "
              "they need different fixes.")
    mem.un_hook()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="verify the addresses against ee_dump.bin")
    c.set_defaults(fn=cmd_check)

    w = sub.add_parser("watch", help="record a real slowdown")
    w.add_argument("--secs", type=float, default=300.0)
    w.add_argument("--interval", type=float, default=0.03)
    w.add_argument("--raw", action="store_true",
                   help="print every sample, not just changes")
    w.set_defaults(fn=cmd_watch)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
