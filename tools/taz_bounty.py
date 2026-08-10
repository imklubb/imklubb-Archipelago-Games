#!/usr/bin/env python3
"""Taz's bounty: which number it is, and the banner that raises it.

    py -3.13 taz_bounty.py check          verify the addresses vs the dump
    py -3.13 taz_bounty.py status         every bounty number, right now
    py -3.13 taz_bounty.py try            award one, the way the item does
    py -3.13 taz_bounty.py try --amount 25000
    py -3.13 taz_bounty.py watch          record the game awarding one itself

`try` is the one that answers it. It runs the SHIPPED Game.grant_effect, so
what you see is what the Raised Bounty item does -- the game's own animated
banner, the slow-motion, and both numbers moving together.

`watch` records the game doing it: smash a poster or a Golden Sam Statue while
it runs and every bounty field that moves is printed. Nothing is written.

Close the AP client first -- PINE takes one connection at a time.


WHICH NUMBER IS THE BOUNTY
--------------------------
Three, and they are not the same thing. All read out of ee_dump.bin.

  A. THE RUNNING TOTAL -- the "$" the stats screens show.
     0x0040403C + file*0x42B4  (i.e. record + 0x42A0)
     Read by playerstats.cpp (0x002B503C, formats '$%d'), the save-slot
     browser (0x001154BC) and the milestone check (0x002B85F4, ten thresholds
     from 0x004B1020). Written only by the banner's driver walking it toward
     its target, plus playerstats 0x002B5048 and the new-game reset.

  B. THE LEVEL'S BOUNTY -- what this level has cost Sam.
     level_block(lid, file) + 0x218   (D.L_TOTAL_BOUNTY)
     Two writers in the whole image: 0x00201E8C, inside the award function,
     and 0x0029B48C, the new-game reset. One reader: the keeper AI at
     0x00171344, which is gated on it being positive.

  C. record + 0x214 -- bounty LOST this level, decremented on penalties only
     (0x00201ED8), read by the end-of-level stats at 0x00270BDC.

0x00507210, which the client used to call CURRENT_BOUNTY, is none of these.
It has no direct load or store anywhere; it is produced once at 0x00281F04 as
the base of a two-entry array of 0x35C-byte per-player records and stored as a
pointer into 0x003FF07C. Not a bounty.


WHAT THE ITEM USED TO DO
------------------------
It wrote `LIVE_BLOCK_BASE + (lid-3)*0x238 + file*0x1000`, with LIVE_BLOCK_BASE
= 0x00408BC4, described as a "live per-level block". Work that address back
through the save geometry:

    level_block(3, file 2) + 0x218
      = 0x00400444 + 2*0x42B4 + 0 + 0x218
      = 0x00408BC4

to the byte. It was save slot 2's per-level bounty all along -- and the dump it
was found in was taken on slot 2 (0x003FF2F0 reads 2), so the numbers looked
current because they were. The file stride is 0x42B4, not 0x1000, so the item
wrote the right field for no save file at all: on file 0 it raised slot 2's
copy, and on files 1 and 2 it landed on an unaligned offset belonging to
nothing.


HOW IT AWARDS ONE NOW
---------------------
0x00201DD0(a0 = string index, a1 = dollars) is not a display function, it is
the crediting one:

    00201E8C  sw   $v1, ($a3)        level bounty += a1
    00201E90  lb   $v0, 0x2f0($t0)   the save slot, as a SIGNED byte
    00201E9C  lw   $v0, 0x503c($v1)  the running total
    00201EA8  sw   $v0, -0x5c58($a0) 0x003CA3A8 = total + a1, the target

and the per-frame driver at 0x00202140 walks the real total up to that target
while running the slow-motion. Every award the game makes goes through it and
none of them adds anything itself, so calling it IS the fix -- the money and
the animation are the same act.

a0 = 156 is what the game's own Wanted Poster award passes (0x002B587C). That
index is special-cased at 0x00201FCC: it ignores the string and prints the
level's poster count as "%d / %d". It is also the only zero-length entry in the
whole 1621-entry table, which is why notify.py borrows it for its own text.

Each level has its own award unit at 0x0046B520 + lid*4, and everything the
game gives is that times something -- a poster x1, a statue x2 (the sll at
0x0024C8F4), the destruction bonus x0.5/x0.75/x1.
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

SAVE_RECORD = 0x003FFD9C
FILE_STRIDE = 0x42B4
LEVEL_STRIDE = 0x238
L_TOTAL_BOUNTY = 0x218
L_BOUNTY_DEDUCT = 0x214
RUNNING_TOTAL = 0x42A0          # from the record base

BOUNTY_UNITS = 0x0046B520
CURRENT_FILE = 0x003FF2F0
LEVEL_BYTE = 0x0046DD5C
WIDGET = 0x003CA37C
BANNER_STATE = 0x003CA3B4
BANNER_TARGET = 0x003CA3A8

LEVEL_NAMES = {
    3: "Yosemite Zoo", 4: "Ice Burg", 5: "Zooney Tunes", 6: "Looney Lagoon",
    9: "Looningdale's", 10: "Samsonian Museum", 11: "Bank of Samerica",
    14: "Taz: Haunted", 15: "Cartoon Strip-Mine", 16: "Granny Canyon",
    18: "Tazland A-maze-ment Park",
}

EXPECT = [
    (0x00201E8C, 0xACE30000, "sw v1, (a3)          level bounty += a1"),
    (0x00201E90, 0x810202F0, "lb v0, 0x2f0(t0)     the save slot, SIGNED"),
    (0x00201E9C, 0x8C62503C, "lw v0, 0x503c(v1)    the running total"),
    (0x00201EA8, 0xAC82A3A8, "sw v0, -0x5c58(a0)   0x003CA3A8 = total + a1"),
    (0x00201E6C, 0x10C00099, "beqz a2, 0x2020d4    a loss with nothing to lose"),
    (0x00201FCC, 0x1682001E, "bne s4, v0, 0x202048 the a0 == 156 branch"),
    (0x0020214C, 0x8C62A37C, "lw v0, -0x5c84(v1)   the driver's own null gate"),
    (0x00202170, 0x104001B1, "beqz v0, 0x202838    ... and what it does with it"),
    (0x0024C8F4, 0x00052840, "sll a1, a1, 1        a statue is worth double"),
    (0x0029B48C, 0xAC600000, "sw zero, (v1)        the new-game reset"),
]


def load_game():
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


def addrs(slot, lid):
    rec = SAVE_RECORD + slot * FILE_STRIDE
    return {
        "running total": rec + RUNNING_TOTAL,
        "level bounty": rec + lid * LEVEL_STRIDE + L_TOTAL_BOUNTY,
        "lost this level": rec + lid * LEVEL_STRIDE + L_BOUNTY_DEDUCT,
    }


# ------------------------------------------------------------------- check

def cmd_check(_args):
    if not os.path.exists(DUMP):
        print(f"    no {os.path.basename(DUMP)} -- take one with taz_ramdump.py")
        return 2
    d = open(DUMP, "rb").read()
    if len(d) < 0x02000000:
        print(f"    {os.path.basename(DUMP)} is {len(d)} bytes, expected 32MB")
        return 2

    def w(a):
        return struct.unpack_from("<I", d, a)[0]

    bad = 0
    print(f"    {os.path.basename(DUMP)}: offset == EE address\n")
    for a, want, what in EXPECT:
        got = w(a)
        ok = got == want
        bad += not ok
        print(f"    {'ok  ' if ok else 'BAD '} 0x{a:08X}  {got:08X}"
              f"{'' if ok else f' (expected {want:08X})'}  {what}")

    slot = w(CURRENT_FILE) & 0xFF
    lid = w(LEVEL_BYTE) & 0xFF
    print(f"\n    the dump was taken on save file {slot}, in level {lid} "
          f"({LEVEL_NAMES.get(lid, '?')})")
    for name, a in addrs(slot, lid).items():
        print(f"      0x{a:08X}  {name:<16} = {w(a):,}")

    # All three, because the active one being empty looks like a bug until
    # you can see it is simply a fresh save.
    print("\n    every file's running total, so the active one has context:")
    for f in range(3):
        t = w(SAVE_RECORD + f * FILE_STRIDE + RUNNING_TOTAL)
        print(f"      file {f}{'  <- active' if f == slot else '           '}"
              f"  ${t:,}")

    print("\n    and the proof the old 'live block' was slot 2:")
    old = 0x00408BC4
    real = SAVE_RECORD + 2 * FILE_STRIDE + 3 * LEVEL_STRIDE + L_TOTAL_BOUNTY
    print(f"      LIVE_BLOCK_BASE          0x{old:08X}")
    print(f"      level_block(3, file 2)   0x{real:08X}  "
          f"{'identical' if old == real else 'DIFFERENT'}")

    print("\n    per-level award unit (0x0046B520 + lid*4):")
    for lid_ in sorted(LEVEL_NAMES):
        u = w(BOUNTY_UNITS + lid_ * 4)
        if u:
            print(f"      {lid_:>2}  {LEVEL_NAMES[lid_]:<26} ${u:,}")

    print()
    if bad:
        print(f"    {bad} of {len(EXPECT)} did not match -- different build. "
              f"Do not write anything.")
        return 1
    print(f"    all {len(EXPECT)} instructions match.")
    return 0


# ------------------------------------------------------------------ status

def read_all(G, mem):
    slot = mem.read_u8(CURRENT_FILE)
    lid = mem.read_u8(LEVEL_BYTE)
    out = {"slot": slot if slot < 0x80 else slot - 0x100, "lid": lid}
    if out["slot"] < 0 or not 3 <= lid <= 29:
        return out
    for name, a in addrs(out["slot"], lid).items():
        out[name] = mem.read_u32(a)
    out["banner state"] = mem.read_u32(BANNER_STATE)
    out["banner target"] = mem.read_u32(BANNER_TARGET)
    out["widget"] = mem.read_u32(WIDGET)
    out["unit"] = mem.read_u32(BOUNTY_UNITS + lid * 4)
    return out


def show(s):
    if s["slot"] < 0:
        print("    no save file loaded.")
        return
    print(f"    save file {s['slot']}, level {s['lid']} "
          f"({LEVEL_NAMES.get(s['lid'], '?')}), unit ${s.get('unit', 0):,}")
    for k in ("running total", "level bounty", "lost this level"):
        if k in s:
            print(f"      {k:<16} = ${s[k]:,}")
    print(f"      banner          state {s.get('banner state')}  "
          f"target ${s.get('banner target', 0):,}  "
          f"widget 0x{s.get('widget', 0):08X}")


def cmd_status(_args):
    G = load_game()
    game, mem = hooked(G)
    if not game:
        return 2
    show(read_all(G, mem))
    print(f"\n    bounty_ready() says "
          f"{'YES' if game.bounty_ready() else 'no, not right now'}")
    G.mem.un_hook()
    return 0


# ------------------------------------------------------------------- watch

def cmd_watch(args):
    G = load_game()
    game, mem = hooked(G)
    if not game:
        return 2
    print("    Watching. Smash a Wanted Poster or a Golden Sam Statue and let\n"
          "    the banner finish. Nothing is written. Ctrl-C to stop.\n")
    t0, prev = time.time(), None
    try:
        while time.time() - t0 < args.secs:
            try:
                now = read_all(G, mem)
            except Exception:
                time.sleep(0.1)
                continue
            if prev is None:
                show(now)
                print()
            elif now != prev:
                bits = "  ".join(
                    f"{k}: {prev.get(k)} -> {v}"
                    for k, v in now.items() if prev.get(k) != v)
                print(f"    {time.time() - t0:7.2f}s  {bits}")
            prev = now
            time.sleep(0.03)
    except KeyboardInterrupt:
        print("\n    stopped.")
    G.mem.un_hook()
    return 0


# --------------------------------------------------------------------- try

def cmd_try(args):
    G = load_game()
    game, mem = hooked(G)
    if not game:
        return 2

    if args.amount is not None:
        game.BOUNTY_STEP = int(args.amount)
    print(f"    awarding ${game.BOUNTY_STEP:,}\n    before:")
    show(read_all(G, mem))

    if not game.bounty_ready():
        print("\n    bounty_ready() says no. Waiting for a good moment "
              "(up to 20s)...")
    end = time.time() + 20.0
    got = "defer"
    while time.time() < end:
        game.refresh_save_file()
        got = game.grant_effect("bounty")
        if got != "defer":
            break
        time.sleep(0.1)
    if got == "defer":
        print("\n    never became safe. `status` will say which test is "
              "refusing.")
        G.mem.un_hook()
        return 1

    print("\n    awarded. The banner should be running -- slow motion, and "
          "the $ counting up.\n")
    t0, prev = time.time(), None
    while time.time() - t0 < 12.0:
        try:
            now = read_all(G, mem)
        except Exception:
            break
        if prev is not None and now != prev:
            bits = "  ".join(f"{k}: {prev.get(k)} -> {v}"
                             for k, v in now.items() if prev.get(k) != v)
            print(f"    {time.time() - t0:7.2f}s  {bits}")
        if prev is not None and not now.get("banner state") \
                and prev.get("banner state"):
            break
        prev = now
        time.sleep(0.03)

    print("\n    after:")
    show(read_all(G, mem))
    G.mem.un_hook()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="verify the addresses against ee_dump.bin")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("status", help="every bounty number, right now")
    s.set_defaults(fn=cmd_status)

    w = sub.add_parser("watch", help="record the game awarding one itself")
    w.add_argument("--secs", type=float, default=300.0)
    w.set_defaults(fn=cmd_watch)

    t = sub.add_parser("try", help="award one the way the item does")
    t.add_argument("--amount", type=int, default=None)
    t.set_defaults(fn=cmd_try)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
