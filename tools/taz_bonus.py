#!/usr/bin/env python3
"""The bonus game portals, decided by us instead of by a sandwich count.

    py -3.13 taz_bonus.py check                 offline, no emulator needed
    py -3.13 taz_bonus.py status                what the gate holds right now
    py -3.13 taz_bonus.py install --grant 4,5   patch it, granting those levels
    py -3.13 taz_bonus.py grant 4,5,15          change the granted list only
    py -3.13 taz_bonus.py watch                 the table and the counts, live
    py -3.13 taz_bonus.py remove                put the shipping code back

Everything except `check` needs PCSX2 running with the game booted and PINE
on. Close the AP client first -- only one thing at a time on PINE.

WHY
---
A hub decides whether to build a bonus game portal in exactly one place, and
the sandwich count is only what that place happens to READ:

    0x0028A4B0   mapfile.cpp, SPECIALTYPE_POLICEBOX. The only caller. It hands
                 over the target level's name and builds nothing if the answer
                 is zero.
    0x0021C8B8   the gate. Name -> level id; anything that is not one of the
                 nine bonus levels passes straight through; the nine that are
                 go through a jump table into a switch that loads that level's
                 count and ends at `slti v1,v1,0x64` -- 100 or more, open.

This replaces the seven words of that switch dispatch with a read of a
nine-byte table in scratch RAM that the client owns. The count stops mattering,
which means it never has to be a lie -- no spoof, no window in which a level
believes it has already had all hundred sandwiches, nothing to get wrong.

The failure mode is the one notify.py documents: if PCSX2 is still running an
older translation of that block, it keeps the shipping behaviour. So the
sandwich spoof stays underneath as a fallback, and `status` will tell you which
of the two is actually doing the work.

WHAT TO CHECK IN GAME
---------------------
Install with a grant list, walk into a hub, and look at the level entrances.
The portals should follow the list exactly -- including a level you have every
sandwich in but no grant for, which must NOT have one. Then walk into a level
and count the sandwiches: with the gate patched they are never touched.
"""

import argparse
import importlib.util
import os
import struct
import sys
import time
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")
DUMP = os.path.join(HERE, "ee_dump.bin")


def load_world():
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


RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"    {'PASS' if ok else '*** FAIL ***':<12} {label}")
    if detail and not ok:
        print(f"                 {detail}")


def branch_target(at, word):
    off = word & 0xFFFF
    off = off - 0x10000 if off & 0x8000 else off
    return at + 4 + off * 4


# ------------------------------------------------------------ offline check

def cmd_check(G, g, args):
    """Everything that can be settled without the emulator.

    The point of assembling the patch instead of writing seven hex literals is
    that a wrong branch becomes a failure here rather than a jump into the
    middle of something while Caleb is playing.
    """
    D = sys.modules["tazworld.logic"]
    at = G.BONUS_PATCH_AT
    words = G.BONUS_PATCH

    print("    the seven words that replace the jump table dispatch:")
    names = ["lui   v0, hi", "addu  v0, v0, v1", "lbu   v0, lo(v0)",
             "beq   v0, zero, <return 0>", "nop", "b     <return 1>", "nop"]
    for i, (w, n) in enumerate(zip(words, names)):
        print(f"      0x{at + i * 4:08X}: {w:08X}   {n}")
    print()

    check("the two branches land on the game's own return paths",
          branch_target(at + 12, words[3]) == G.BONUS_RET_ZERO
          and branch_target(at + 20, words[5]) == G.BONUS_RET_ONE,
          f"beq -> 0x{branch_target(at + 12, words[3]):08X}, "
          f"b -> 0x{branch_target(at + 20, words[5]):08X}")

    lo = words[2] & 0xFFFF
    check("the load reads our table and nothing else",
          (words[0] & 0xFFFF) == (G.BONUS_TABLE >> 16)
          and lo == (G.BONUS_TABLE & 0xFFFF) and not (lo & 0x8000),
          f"reads 0x{((words[0] & 0xFFFF) << 16) + lo:08X}, "
          f"wanted 0x{G.BONUS_TABLE:08X}")

    check("the patch is exactly as long as what it replaces",
          len(words) == len(G.BONUS_ORIGINAL))

    check("every bonus level maps to a real level with a bonus game",
          (sorted(G.BONUS_LEVEL.values())
           == sorted(l for l in D.LEVEL_ORDER if l not in D.NO_BONUS)),
          f"maps to {sorted(G.BONUS_LEVEL.values())}")

    check("the table has one byte per bonus level, in id order",
          (len(G.BONUS_TABLE_ORDER) == len(G.BONUS_LEVEL)
           and G.BONUS_TABLE_ORDER[0] == G.BONUS_LEVEL[min(G.BONUS_LEVEL)]),
          f"{G.BONUS_TABLE_ORDER}")

    # notify.py owns the scratch page. Landing on its control block or its
    # text buffer would be a silent corruption of something that works.
    try:
        # notify.py imports pcsx2_mem at module level, and pine is not here
        # when this runs offline. Only its layout constants are wanted, so a
        # stub is enough and it is registered under the package name the
        # relative import will look for.
        sys.modules.setdefault("tazworld.pcsx2_mem",
                               types.ModuleType("tazworld.pcsx2_mem"))
        spec = importlib.util.spec_from_file_location(
            "tazworld.notify", os.path.join(WORLD, "notify.py"))
        N = importlib.util.module_from_spec(spec)
        sys.modules["tazworld.notify"] = N
        spec.loader.exec_module(N)
    except Exception as exc:
        check("the table does not collide with notify's scratch", False,
              f"could not load notify.py to ask: {exc!r}")
    else:
        used = set(range(N.CTRL, N.CODE + 4 * len(N._code_words())))
        used |= set(range(N.TEXT_BUF, N.TEXT_BUF + N.TEXT_CAP * 2))
        ours = set(range(G.BONUS_TABLE, G.BONUS_TABLE + len(G.BONUS_LEVEL)))
        check("the table does not collide with notify's scratch",
              not (ours & used) and N.SCRATCH_LO <= G.BONUS_TABLE < N.SCRATCH_HI,
              f"0x{G.BONUS_TABLE:08X} overlaps notify")

    if os.path.exists(DUMP):
        raw = open(DUMP, "rb").read()

        def w(a):
            return struct.unpack_from("<I", raw, a)[0]

        live = [w(at + i * 4) for i in range(len(G.BONUS_ORIGINAL))]
        check("the shipping words match the RAM dump",
              live == G.BONUS_ORIGINAL,
              "dump has " + " ".join(f"{x:08X}" for x in live))
        check("the return paths in the dump are still the epilogue",
              (w(G.BONUS_RET_ZERO) == 0x0000102D
               and w(G.BONUS_RET_ONE) == 0x24020001),
              f"0x{G.BONUS_RET_ZERO:08X}={w(G.BONUS_RET_ZERO):08X}, "
              f"0x{G.BONUS_RET_ONE:08X}={w(G.BONUS_RET_ONE):08X}")
        # And the mapping, re-derived rather than trusted: each switch body
        # loads one level's count, and the displacement says which.
        tab = [w(0x004A16E0 + i * 4) for i in range(len(G.BONUS_LEVEL))]
        by_addr = {G.T.level_block(l, 0) + G.T.L_SANDWICHES: l
                   for l in range(3, 21)}
        derived, bad = {}, []
        for i, t in enumerate(tab):
            for a in range(t, t + 0x60, 4):
                x = w(a)
                if (x >> 26) == 0x23 and ((x >> 16) & 31) == 3:
                    derived[G.BONUS_FIRST_ID + i] = by_addr.get(
                        0x003FF000 + (x & 0xFFFF))
                    break
        for bid, lid in G.BONUS_LEVEL.items():
            if derived.get(bid) != lid:
                bad.append(f"{bid} -> {derived.get(bid)}, code says {lid}")
        check("the id -> level mapping matches the game's own jump table",
              not bad, "; ".join(bad))
    else:
        print(f"    (no ee_dump.bin here, so the checks against the game's "
              f"own code were skipped)")

    print()
    bad = RESULTS.count(False)
    print(f"    {len(RESULTS) - bad}/{len(RESULTS)} passed")
    return 1 if bad else 0


# --------------------------------------------------------------- live verbs

def parse_grant(text, D):
    if not text:
        return set()
    out = set()
    for part in str(text).replace(",", " ").split():
        lid = int(part)
        if lid not in D.LEVEL_ORDER or lid in D.NO_BONUS:
            raise SystemExit(f"    {lid} is not a level with a bonus game. "
                             f"Pick from "
                             f"{[l for l in D.LEVEL_ORDER if l not in D.NO_BONUS]}")
        out.add(lid)
    return out


def show(G, g):
    D = sys.modules["tazworld.logic"]
    patched = g.bonus_gate_installed()
    shipped = g.bonus_gate_original()
    where = ("ours -- the granted list decides" if patched else
             "the shipping code -- the sandwich count decides" if shipped else
             "SOMETHING ELSE, and nothing here will touch it")
    print(f"    gate at 0x{G.BONUS_PATCH_AT:08X}: {where}")
    got = G._read_words(G.BONUS_PATCH_AT, len(G.BONUS_PATCH))
    print("      " + " ".join(f"{x:08X}" for x in got))
    print()
    try:
        table = G.mem.read_bytes(G.BONUS_TABLE, len(G.BONUS_LEVEL))
    except Exception:
        table = b""
    f = g.save_file
    print(f"      {'level':<24}{'granted':>9}{'count':>7}   portal appears?")
    print(f"      {'-' * 24}{'-' * 9}{'-' * 7}")
    for i, lid in enumerate(G.BONUS_TABLE_ORDER):
        byte = table[i] if i < len(table) else None
        cnt = g._u32(G.T.level_block(lid, f) + G.T.L_SANDWICHES)
        if patched:
            verdict = "yes" if byte else "no"
        else:
            verdict = "yes" if cnt >= D.SANDWICH_GOAL else "no"
        print(f"      {D.LEVEL_NAME[lid]:<24}"
              f"{('--' if byte is None else byte):>9}{cnt:>7}   {verdict}")
    print()
    return patched


def cmd_status(G, g, args):
    show(G, g)
    return 0


def cmd_install(G, g, args):
    D = sys.modules["tazworld.logic"]
    grant = parse_grant(args.grant, D)
    try:
        g.bonus_gate_install(grant)
    except Exception as exc:
        print(f"    not installed: {exc}")
        return 1
    print("    installed.")
    print()
    show(G, g)
    print("    Now walk into a hub and look at the level entrances. If the")
    print("    portals do not follow the granted column, PCSX2 is still")
    print("    running its old translation of that block -- say so, because")
    print("    that is the one thing this cannot check from outside.")
    return 0


def cmd_grant(G, g, args):
    D = sys.modules["tazworld.logic"]
    grant = parse_grant(args.levels, D)
    if not g.bonus_gate_installed():
        print("    the gate is not patched, so the table is not being read.")
        print("    Run `install` first.")
        return 1
    g.bonus_gate_write_table(grant)
    show(G, g)
    print("    Leave the hub and come back -- the portals are decided while")
    print("    the hub's map is built, so a change only shows on the next load.")
    return 0


def cmd_remove(G, g, args):
    if g.bonus_gate_remove():
        print("    the shipping gate is back. The sandwich count decides again.")
        return 0
    print("    could not restore it -- read what is there with `status`.")
    return 1


def cmd_watch(G, g, args):
    print("    Walk between hubs and levels. Ctrl-C to stop.")
    print()
    last = None
    try:
        while True:
            key = (g.bonus_gate_installed(), g.level_id(), g.game_state())
            if key != last:
                last = key
                print(f"    [level {key[1]}, state {key[2]}, "
                      f"gate {'ours' if key[0] else 'shipping'}]")
                show(G, g)
            time.sleep(0.25)
    except KeyboardInterrupt:
        print()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check, live=False)
    sub.add_parser("status").set_defaults(fn=cmd_status, live=True)
    i = sub.add_parser("install")
    i.add_argument("--grant", default="",
                   help="level ids to grant, e.g. --grant 4,5,15")
    i.set_defaults(fn=cmd_install, live=True)
    gr = sub.add_parser("grant")
    gr.add_argument("levels", help="level ids, e.g. 4,5,15")
    gr.set_defaults(fn=cmd_grant, live=True)
    sub.add_parser("remove").set_defaults(fn=cmd_remove, live=True)
    sub.add_parser("watch").set_defaults(fn=cmd_watch, live=True)
    args = ap.parse_args()

    G = load_world()
    g = None
    if args.live:
        if G.mem is None:
            raise SystemExit("    pcsx2_mem could not be imported -- pine.py "
                             "has to be in worlds/tazwanted/pcsx2_interface/.")
        g = G.Game()
        if not g.connect():
            raise SystemExit("    could not reach PCSX2. Is it running with "
                             "the game booted, PINE on, and the AP client "
                             "closed?")
        g.refresh_save_file()
    return args.fn(G, g, args)


if __name__ == "__main__":
    raise SystemExit(main())
