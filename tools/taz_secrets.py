#!/usr/bin/env python3
"""The OTHER way into a bonus game, and why the police box is not it.

    py -3.13 taz_secrets.py check      verify all 27 words vs the dump
    py -3.13 taz_secrets.py status     what the menu would allow, right now
    py -3.13 taz_secrets.py lock       lock every Secrets entry
    py -3.13 taz_secrets.py lock --granted 4,9    ...except these levels
    py -3.13 taz_secrets.py unlock     put the shipping immediates back
    py -3.13 taz_secrets.py gate       diagnose the POLICE BOX gate instead

Close the AP client first -- PINE takes one connection at a time.


TWO ROUTES, AND ONLY ONE OF THEM WAS EVER GATED
-----------------------------------------------
A bonus game is a level id in 21..29. There are four ways to reach one, and
the world only ever knew about the first:

  a. a police box in a level      mapfile.cpp 0x0028A4B0 -> phonebox.cpp
                                  GATED by taz_bonus.py's patch
  b. the Secrets page             secretsmenu.cpp 0x00150478   UNGATED
  c. the two-player menu          twoplayermenu.cpp 0x00123444 ungated
  d. Tournament                   Tournament.cpp 0x001C1F60    ungated

All four end at the same event: 0x002E6C00("scenechange", "_<levelname>"),
consumed by 0x002743A8, which strips the leading underscore and resolves the
name to a level.

**The police box really is gated.** `D.callers(0x0021C8B8)` returns exactly
one site, `0x0028A4B0` in mapfile.cpp, and the gate's answer decides whether
the box object is constructed at all:

    0x0028A4B0  0C08722E  jal 0x21c8b8
    0x0028A4B8  1040FF68  beqz $v0, 0x28a25c   -> build nothing

taz_bonus.py replaces the seven words at 0x0021C8F4 so that gate answers
from our table at 0x01F00A00 instead of from the sandwich count. Verified
here: the client's BONUS_TABLE_ORDER is [10,16,9,6,11,14,4,15,5], and the
game's own nine switch bodies read exactly those levels in exactly that
order. The orders agree, so the table is not mis-indexed.

**The Secrets page is not gated by anything.** It is a front-end page built
by frontendmenu.cpp (0x001069D4 -> 0x0014F178), and its enable test is the
raw sandwich count, three times per level -- once per SAVE SLOT:

    0x001504B0  8C821A98  lw   $v0, 0x1a98($a0)   ; Safari count, slot 0
    0x001504B4  28420064  slti $v0, $v0, 0x64     ; < 100 ?
    0x001504B8  1040000E  beqz $v0, 0x1504f4      ; >= 100 -> ENABLE

then again for slot 1 and slot 2, then the "All Bonus Games Unlocked" cheat
bit (0x003FF2EC & 4) as a last chance. Twenty-seven comparisons, all
`slti rX, rY, 100`, all verified word for word by `check`.

That it ORs across all three save slots is the part that matters: capping
the count in the AP save file cannot help, because another file's progress
still opens the entry.

WHAT THIS CHANGES, AND WHY IT IS SAFE
-------------------------------------
Only the 16-bit immediate, at 27 addresses. `slti` is signed and the count
field only ever holds 0..101, so:

    0x0064  the shipping test        enabled once the player has 100
    0x7FFF  count < 32767 is ALWAYS true  -> never enabled
    0x8000  count < -32768 is NEVER true  -> always enabled

No branch target moves, no delay slot changes meaning, and nothing is
relocated -- a `beqz` delay slot always executes and a `beql` delay slot
always executes when taken, in every one of the three variants. `unlock`
writes 0x0064 back.

NOT DONE HERE
-------------
The two-player and Tournament routes (c and d). Both are ungated with
respect to the AP item; neither has been costed. Say so rather than
pretending the coverage is complete.
"""

import argparse
import importlib.util
import os
import struct
import sys

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")
DUMP = os.path.join(HERE, "ee_dump.bin")

NEVER = 0x7FFF          # count < 32767 -- always true, so never enabled
ALWAYS = 0x8000         # count < -32768 -- never true, so always enabled
SHIPPED = 0x0064        # the game's own 100

CHEAT_WORD = 0x003FF2EC
CHEAT_ALL_BONUS = 0x4
CURRENT_FILE = 0x003FF2F0

# Where the police-box gate and its table live, for `gate`.
BONUS_PATCH_AT = 0x0021C8F4
BONUS_TABLE = 0x01F00A00
BONUS_ORIGINAL = [0x3C02004A, 0x00031880, 0x244216E0, 0x00621821,
                  0x8C640000, 0x00800008, 0x00000000]

# save block = 0x003FFD9C + slot*0x42B4 + lid*0x238, count at +0x1E4;
# the menu reads it as 0x003FF000 + slot*0x42B4 + 0xF80 + lid*0x238.
SAVE_BASE = 0x003FFD9C
FILE_STRIDE = 0x42B4
LEVEL_STRIDE = 0x238
L_SANDWICHES = 0x1E4

# (parent level, the scene name, the three slti addresses).
# Every one asserted by `check` before anything is written.
SITES = [
    (5,  "_rcsafari",    (0x001504B4, 0x001504C4, 0x001504D8)),
    (4,  "_rcicedome",   (0x0015051C, 0x0015052C, 0x00150540)),
    (6,  "_deaqua",      (0x00150584, 0x00150594, 0x001505A8)),
    (9,  "_vrdeptstr",   (0x001505EC, 0x001505FC, 0x00150610)),
    (10, "_vrmuseum",    (0x00150654, 0x00150664, 0x00150678)),
    (11, "_deconstruct", (0x001506BC, 0x001506CC, 0x001506E0)),
    (16, "_vrgrandc",    (0x00150724, 0x00150734, 0x00150748)),
    (15, "_rcgoldmine",  (0x0015078C, 0x0015079C, 0x001507B0)),
    (14, "_detasmania",  (0x001507F4, 0x00150804, 0x00150818)),
]

LEVEL_NAME = {4: "Ice Burg", 5: "Zooney Tunes", 6: "Looney Lagoon",
              9: "Looningdale's", 10: "Samsonian Museum",
              11: "Bank of Samerica", 14: "Taz: Haunted",
              15: "Cartoon Strip-Mine", 16: "Granny Canyon"}

# Anchors that are not immediates, so `check` also proves it is this build.
ANCHORS = [
    (0x00150478, None, "secretsmenu input handler (function start)"),
    (0x001504B0, 0x8C821A98, "lw $v0, 0x1a98($a0)   Safari count, slot 0"),
    (0x001504E8, 0x30420004, "andi $v0,$v0,4        the cheat bit"),
    (0x0028A4B0, 0x0C08722E, "jal 0x21c8b8          the police-box gate"),
    (0x0028A4B8, 0x1040FF68, "beqz $v0             gate 0 -> no box built"),
]


def is_slti_100(w):
    return (w >> 26) == 0x0A and (w & 0xFFFF) == SHIPPED


def is_slti_any(w):
    return (w >> 26) == 0x0A


def retarget(w, imm):
    return (w & 0xFFFF0000) | (imm & 0xFFFF)


def load_mem():
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


def count_addr(lid, slot):
    return SAVE_BASE + slot * FILE_STRIDE + lid * LEVEL_STRIDE + L_SANDWICHES


# ------------------------------------------------------------------- check

def cmd_check(_args):
    if not os.path.exists(DUMP):
        print(f"  no {os.path.basename(DUMP)} -- take one with taz_ramdump.py")
        return 2
    with open(DUMP, "rb") as fh:
        d = fh.read()
    if len(d) < 0x02000000:
        print(f"  {os.path.basename(DUMP)} is {len(d)} bytes, expected 32MB")
        return 2

    def w(a):
        return struct.unpack_from("<I", d, a)[0]

    bad = 0
    print(f"\n  {os.path.basename(DUMP)}: offset == EE address")
    # The source path is the one claim that NAMES the module, so assert it
    # too -- everything else here is just addresses that could be anything.
    path = d[0x00492848:d.index(0, 0x00492848)].decode("latin-1")
    want_path = "C:/Taz/Source/secretsmenu.cpp"
    if path != want_path:
        bad += 1
    print(f"  {'ok  ' if path == want_path else 'BAD '} 0x00492848  {path!r}"
          + ("" if path == want_path else f"   expected {want_path!r}") + "\n")

    for addr, want, what in ANCHORS:
        got = w(addr)
        ok = want is None or got == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'BAD '} 0x{addr:08X}  {got:08X}  {what}"
              + ("" if ok else f"   expected {want:08X}"))

    print("\n  the 27 comparisons, three per bonus game (slot 0 / 1 / 2):\n")
    for lid, name, addrs in SITES:
        words = [w(a) for a in addrs]
        state = ("shipping" if all(is_slti_100(x) for x in words) else
                 "LOCKED" if all((x & 0xFFFF) == NEVER for x in words) else
                 "unlocked" if all((x & 0xFFFF) == ALWAYS for x in words) else
                 "MIXED")
        ok = all(is_slti_any(x) for x in words)
        bad += not ok
        print(f"  {'ok  ' if ok else 'BAD '} {LEVEL_NAME[lid]:<20} "
              f"{name:<13} " + " ".join(f"{x:08X}" for x in words)
              + f"   {state}")
        if not ok:
            print("        not an slti -- these addresses are for another "
                  "build, do NOT write")

    print()
    if bad:
        print(f"  {bad} did not match. Write nothing.")
        return 1
    print(f"  all {len(ANCHORS) + len(SITES)} match.")

    # ...and the gate the police box uses, for contrast.
    gate = [w(BONUS_PATCH_AT + i * 4) for i in range(7)]
    print("\n  for comparison, the police-box gate at 0x0021C8F4:")
    print("    " + " ".join(f"{x:08X}" for x in gate))
    print("    " + ("the shipping jump-table dispatch (patch NOT installed)"
                    if gate == BONUS_ORIGINAL else
                    "not the shipping words -- a patch is in place"))
    return 0


# ------------------------------------------------------------------ status

def cmd_status(_args):
    mem = load_mem()
    if not mem.hook():
        print("  could not reach PCSX2. Game booted, PINE on (slot 28011), "
              "AP client CLOSED?")
        return 2
    print(f"  connected: {mem.game_id()}\n")

    cheat = mem.read_u32(CHEAT_WORD)
    slot = mem.read_u8(CURRENT_FILE)
    slot = slot - 256 if slot > 127 else slot
    print(f"  cheat word 0x{CHEAT_WORD:08X} = 0x{cheat:08X}   "
          f"'All Bonus Games Unlocked' bit is "
          f"{'ON -- it bypasses everything' if cheat & CHEAT_ALL_BONUS else 'off'}")
    print(f"  current save file = {slot}"
          f"{' (none loaded)' if slot < 0 else ''}\n")

    print(f"  {'level':<20} {'menu':<10} {'slot0':>6} {'slot1':>6} "
          f"{'slot2':>6}   would the Secrets page offer it?")
    for lid, name, addrs in SITES:
        words = [mem.read_u32(a) for a in addrs]
        imm = [x & 0xFFFF for x in words]
        state = ("shipping" if all(i == SHIPPED for i in imm) else
                 "LOCKED" if all(i == NEVER for i in imm) else
                 "unlocked" if all(i == ALWAYS for i in imm) else "MIXED")
        counts = [mem.read_u32(count_addr(lid, s)) for s in range(3)]
        offered = any(c >= i for c, i in zip(counts, imm)) or \
            bool(cheat & CHEAT_ALL_BONUS)
        print(f"  {LEVEL_NAME[lid]:<20} {state:<10} "
              + " ".join(f"{c:>6}" for c in counts)
              + f"   {'YES' if offered else 'no'}")
    print("\n  Any slot at or above the compared number opens the entry -- "
          "that is\n  what makes capping the AP file's count insufficient.")
    mem.un_hook()
    return 0


# -------------------------------------------------------------- lock/unlock

def _write(mem, imm_for):
    changed = 0
    for lid, name, addrs in SITES:
        imm = imm_for(lid)
        for a in addrs:
            w = mem.read_u32(a)
            if not is_slti_any(w):
                raise RuntimeError(
                    f"0x{a:08X} holds {w:08X}, which is not an slti -- "
                    f"refusing to write")
            want = retarget(w, imm)
            if w != want:
                mem.write_u32(a, want)
                if mem.read_u32(a) != want:
                    raise RuntimeError(f"0x{a:08X} did not stay written")
                changed += 1
    return changed


def cmd_lock(args):
    granted = set()
    if args.granted:
        granted = {int(x) for x in args.granted.replace(",", " ").split()}
    mem = load_mem()
    if not mem.hook():
        print("  could not reach PCSX2 (AP client CLOSED?)")
        return 2
    print(f"  connected: {mem.game_id()}")
    try:
        n = _write(mem, lambda lid: SHIPPED if lid in granted else NEVER)
    except RuntimeError as exc:
        print(f"  {exc}")
        mem.un_hook()
        return 1
    print(f"  {n} word(s) changed.")
    if granted:
        print(f"  Left at the shipping test (needs 100 sandwiches): "
              + ", ".join(LEVEL_NAME[l] for l in sorted(granted)
                          if l in LEVEL_NAME))
    print("  The Secrets page will now grey out every other bonus game.")
    print("  The police box is unaffected -- that is taz_bonus.py's job.")
    mem.un_hook()
    return 0


def cmd_unlock(_args):
    mem = load_mem()
    if not mem.hook():
        print("  could not reach PCSX2 (AP client CLOSED?)")
        return 2
    n = _write(mem, lambda lid: SHIPPED)
    print(f"  {n} word(s) put back to the shipping 100.")
    mem.un_hook()
    return 0


# -------------------------------------------------------------------- gate

def cmd_gate(_args):
    """Why did a police box appear? Read everything that decides it."""
    mem = load_mem()
    if not mem.hook():
        print("  could not reach PCSX2 (AP client CLOSED?)")
        return 2
    print(f"  connected: {mem.game_id()}\n")

    words = [mem.read_u32(BONUS_PATCH_AT + i * 4) for i in range(7)]
    installed = words != BONUS_ORIGINAL
    print(f"  gate words at 0x{BONUS_PATCH_AT:08X}:")
    print("    " + " ".join(f"{w:08X}" for w in words))
    if not installed:
        print("    ** THE SHIPPING WORDS. The patch is NOT installed, so the")
        print("       gate is answering from the sandwich count -- which is")
        print("       exactly the bug. Ask why bonus_gate_tick did not run.")
    else:
        print("    a patch is in place")

    table = mem.read_bytes(BONUS_TABLE, 9)
    order = [10, 16, 9, 6, 11, 14, 4, 15, 5]   # bonus id 21..29 -> parent
    print(f"\n  table at 0x{BONUS_TABLE:08X}: {table.hex(' ')}")
    print(f"  {'bonus id':<9} {'level':<20} table  the gate would")
    for i, lid in enumerate(order):
        v = table[i]
        print(f"  {21 + i:<9} {LEVEL_NAME.get(lid, lid):<20} {v:>5}  "
              f"{'BUILD the box' if (v and installed) else 'build nothing' if installed else 'use the count'}")

    cheat = mem.read_u32(CHEAT_WORD)
    print(f"\n  cheat word = 0x{cheat:08X}  'All Bonus Games Unlocked' "
          f"{'ON -- returns 1 BEFORE the patch is reached' if cheat & CHEAT_ALL_BONUS else 'off'}")
    if cheat & CHEAT_ALL_BONUS:
        print("    ** that bit alone explains a box appearing, whatever the "
              "table says\n       (0x0021C8E0 bnez -> return 1)")

    print("\n  Remember: the gate only decides CONSTRUCTION, at map load.")
    print("  A box already built stays until that map is loaded again, so a")
    print("  correct table can still coexist with a box on screen.")
    mem.un_hook()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    lk = sub.add_parser("lock")
    lk.add_argument("--granted", default="",
                    help="level ids to leave at the shipping test, e.g. 4,9")
    lk.set_defaults(fn=cmd_lock)
    sub.add_parser("unlock").set_defaults(fn=cmd_unlock)
    sub.add_parser("gate").set_defaults(fn=cmd_gate)
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
