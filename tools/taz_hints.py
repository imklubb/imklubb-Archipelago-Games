#!/usr/bin/env python3
"""The two hint systems in Taz: Wanted, and how to switch each one off.

    py -3.13 taz_hints.py check      offline, against ee_dump.bin
    py -3.13 taz_hints.py status     what the game holds right now
    py -3.13 taz_hints.py off        silence the Standard-only prompt book
    py -3.13 taz_hints.py on         hand it back
    py -3.13 taz_hints.py flythrough silence the intro camera, this level only

Everything but `check` needs PCSX2 running with the game booted and PINE on.
Close the AP client first -- only one thing at a time on PINE.

THEY ARE TWO DIFFERENT SYSTEMS
------------------------------
1. THE PROMPT BOOK (prompt.cpp). The popups in Zooney Tunes. Built by
   0x002B8E58, which jumps through a table at 0x004B1280 indexed by level id.
   Only TWO levels in the game land on the case that reads the difficulty:

       lid 5  safari   Zooney Tunes      built only when difficulty == 0
       lid 9  deptstr  Looningdale's     built only when difficulty == 0
       lid 3, 14, 15                     always built
       everything else                   never built

   Which is why Zooney Tunes is where you notice it, and why changing
   difficulty makes it stop. `off` forces a1 to zero in the delay slot at
   0x002B8EBC, which is exactly what Advanced and Expert leave there. The
   always-built levels enter one instruction later and never see it.

2. THE INTRO CAMERA. Bugs' voice lines over the flythrough at level start.
   NOT difficulty gated -- there is no difficulty test anywhere on its path,
   so this is not the one that goes away when you change difficulty. Its lines
   come from a table at 0x00474170 (stride 0x24 by level id; safari's row is
   string indices 50..57) and the whole sequence is skipped when the marker
   count at 0x0046DD88 is zero, which is the path every hub and boss already
   takes. `flythrough` writes that zero. It is rebuilt on the next level load,
   so it is a per-visit switch rather than a setting.
"""

import argparse
import importlib.util
import os
import struct
import sys
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")
DUMP = os.path.join(HERE, "ee_dump.bin")

PROMPT_TABLE = 0x004B1280        # index = lid - 3
CASE_DIFFICULTY = 0x002B8EB0
CASE_ALWAYS = 0x002B8EC0
CASE_NEVER = 0x002B8EC4
LEVEL_NAMES = {3: "zoohub", 4: "icedome", 5: "safari", 6: "aqua",
               7: "zooboss", 8: "cityhub", 9: "deptstr", 10: "museum",
               11: "contruct", 12: "cityboss", 13: "westhub", 14: "ghost",
               15: "goldmine"}


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


def cmd_check(G, g, args):
    if not os.path.exists(DUMP):
        print("    no ee_dump.bin here, so there is nothing to check against.")
        return 1
    raw = open(DUMP, "rb").read()

    def w(a):
        return struct.unpack_from("<I", raw, a)[0]

    check("the instruction we replace is the one we think it is",
          w(G.PROMPT_GATE_AT) == G.PROMPT_GATE_ORIGINAL,
          f"0x{G.PROMPT_GATE_AT:08X} holds {w(G.PROMPT_GATE_AT):08X}, "
          f"wanted {G.PROMPT_GATE_ORIGINAL:08X} (sltiu a1, v1, 1)")

    # The word before it is the branch whose delay slot we are in; if that
    # ever stops being a branch, the patch is being written somewhere else.
    check("...and it really is a delay slot, so the write cannot be skipped",
          (w(G.PROMPT_GATE_AT - 4) >> 26) in (0x04, 0x05, 0x02),
          f"0x{G.PROMPT_GATE_AT - 4:08X} holds {w(G.PROMPT_GATE_AT - 4):08X}")

    check("the replacement sets a1 to zero and touches nothing else",
          G.PROMPT_GATE_PATCH == 0x0000282D
          and w(0x002B8E64) == G.PROMPT_GATE_PATCH,
          "daddu a1, zero, zero -- the same word the function already uses "
          "at 0x002B8E64")

    rows = {lid: w(PROMPT_TABLE + (lid - 3) * 4) for lid in LEVEL_NAMES}
    gated = sorted(l for l, t in rows.items() if t == CASE_DIFFICULTY)
    check("exactly two levels read the difficulty: safari and deptstr",
          gated == [5, 9],
          f"difficulty-gated levels are {gated}")
    check("...and no level the patch must not touch shares the instruction",
          all(rows[l] in (CASE_ALWAYS, CASE_NEVER)
              for l in rows if l not in (5, 9)),
          f"{ {LEVEL_NAMES[l]: hex(rows[l]) for l in rows} }")

    # The intro camera, so the two systems stay told apart.
    safari = [w(0x00474170 + 5 * 0x24 + i * 4) for i in range(8)]
    check("the intro camera's safari lines are still 50..57",
          safari == list(range(50, 58)), f"{safari}")

    print()
    print("    the prompt book, level by level:")
    for lid in sorted(LEVEL_NAMES):
        t = rows[lid]
        kind = {CASE_DIFFICULTY: "Standard only", CASE_ALWAYS: "always",
                CASE_NEVER: "never"}.get(t, f"0x{t:08X}?")
        print(f"      {lid:>3}  {LEVEL_NAMES[lid]:<9} {kind}")

    print()
    bad = RESULTS.count(False)
    print(f"    {len(RESULTS) - bad}/{len(RESULTS)} passed")
    return 1 if bad else 0


def show(G, g):
    live = g._u32(G.PROMPT_GATE_AT)
    state = ("silenced (Standard reads like Advanced)"
             if live == G.PROMPT_GATE_PATCH else
             "shipping (Standard shows them)"
             if live == G.PROMPT_GATE_ORIGINAL else
             "SOMETHING ELSE -- nothing here will touch it")
    print(f"    prompt book at 0x{G.PROMPT_GATE_AT:08X}: {live:08X}  {state}")
    try:
        print(f"    intro camera markers in this level: "
              f"{g._u32(G.INTROCAM_COUNT)}   (level {g.level_id()})")
    except Exception:
        pass


def cmd_status(G, g, args):
    show(G, g)
    return 0


def cmd_off(G, g, args):
    for line in g.prompt_gate_set(True):
        print(f"    {line}")
    show(G, g)
    print("    Re-enter Zooney Tunes -- the book is built when the level "
          "loads.")
    return 0


def cmd_on(G, g, args):
    for line in g.prompt_gate_set(False):
        print(f"    {line}")
    show(G, g)
    return 0


def cmd_flythrough(G, g, args):
    before = g._u32(G.INTROCAM_COUNT)
    g._w32(G.INTROCAM_COUNT, 0)
    print(f"    intro camera markers {before} -> {g._u32(G.INTROCAM_COUNT)}")
    print("    This visit only. The count is rebuilt on the next level load,")
    print("    so it has to be re-written each time to stay off.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check, live=False)
    sub.add_parser("status").set_defaults(fn=cmd_status, live=True)
    sub.add_parser("off").set_defaults(fn=cmd_off, live=True)
    sub.add_parser("on").set_defaults(fn=cmd_on, live=True)
    sub.add_parser("flythrough").set_defaults(fn=cmd_flythrough, live=True)
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
