#!/usr/bin/env python3
"""What the save really says about sandwiches, field by field.

    py -3.13 taz_sandwich.py check          one look at all ten levels
    py -3.13 taz_sandwich.py watch          the same, live, as you play

READ-ONLY. Nothing is written.

Close the AP client first -- only one thing at a time on PINE.

WHY
---
Two places in each level's save block claim to know how many sandwiches were
collected:

    +0x1E4   a count.  The client SPOOFS this: a hub only builds a bonus game
             portal if it reads AT LEAST 100 while loading (slti at
             0x0021C9EC), so the client writes 101. Not 100 -- a RUNNING level
             whose count is exactly 100 destroys every sandwich object in
             itself (bnel at 0x0024A6DC). 101 clears the portal gate and never
             matches the destroyer, and the game cannot produce it, so a 101
             in this field is always ours and never yours.

    +0x004   480 bytes, one dword per sandwich. The client never writes a byte
             of it.

They do not simply agree, and the reason matters: Starting Sandwiches is given
by writing the COUNT and nothing else, because there is no way to hand somebody
a sandwich they never touched. So the model is

    count  ==  bitmap + starting        unless we spoofed it

and the client works out your real progress the same way. Every level here is
labelled with which of those it is, so the model can be checked rather than
believed. Pass --start to match your yaml.

If anything comes back UNEXPLAINED, the model is wrong and the client should
not be trusting it -- send it to me. If the bitmap reads as "malformed" then it
is not one dword per sandwich after all, and the client falls back to the
count on its own.
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


def raw_bitmap(G, lid, f):
    """The 120 dwords, unjudged, so a malformed one can be described."""
    base = G.T.level_block(lid, f) + G.T.L_SANDWICH_BITS
    try:
        data = G.mem.read_bytes(base, 480)
    except Exception as e:
        return None, f"unreadable ({e!r})"
    if len(data) < 480:
        return None, "short read"
    words = struct.unpack("<120I", data[:480])
    odd = [w for w in words if w not in (0, 1)]
    if odd:
        return words, (f"malformed -- {len(odd)} dword(s) are not 0 or 1, "
                       f"e.g. 0x{odd[0]:08X}")
    return words, None


def report(G, g, START):
    f = g.save_file
    D = sys.modules["tazworld.logic"]
    # 0/1/2 internally, 1/2/3 in the game's own menu. Printing only the
    # index reads as the wrong file.
    print(f"    save file {f} (slot {f + 1} in the menu), "
          f"Taz is in level {g.level_id()}")
    print(f"    starting sandwiches: {START} -- the count carries this, the "
          "bitmap does not")
    print()
    print(f"      {'level':<24}{'count':>7}{'bitmap':>8}{'+start':>8}   ")
    print(f"      {'-' * 24}{'-' * 7}{'-' * 8}{'-' * 8}")
    disagree = []
    for lid in D.LEVEL_ORDER:
        name = D.LEVEL_NAME[lid]
        cnt = g._u32(G.T.level_block(lid, f) + G.T.L_SANDWICHES)
        words, why = raw_bitmap(G, lid, f)
        if why:
            print(f"      {name:<24}{cnt:>7}{'--':>8}{'--':>8}   {why}")
            continue
        n = sum(words)
        true = min(n + START, 100)
        if true == cnt:
            mark = "   honest"
        elif cnt > 100:
            mark = "   <== spoofed, bonus granted"
        elif cnt == 100:
            # The client must never write this one. A running level whose
            # count reads exactly 100 destroys every sandwich object in itself
            # (0x0024A6D8), which is why the spoof is 101 -- the portal gate
            # only wants 100 OR MORE. An exact 100 nobody earned is a bug.
            mark = "   <== EXACTLY 100 AND NOT EARNED -- this despawns them"
            disagree.append((name, cnt, true))
        elif cnt == 99:
            mark = "   <== capped at 99, bonus not granted"
        else:
            mark = "   <== UNEXPLAINED"
            disagree.append((name, cnt, true))
        print(f"      {name:<24}{cnt:>7}{n:>8}{true:>8}{mark}")
    print()
    if disagree:
        print(f"    {len(disagree)} level(s) the model does not explain:")
        for n, c, t in disagree:
            print(f"      {n}: count {c}, but bitmap+start says {t}")
        print("    That means bitmap + starting is NOT the whole story, and")
        print("    the client should not be trusting it. Send me this.")
    else:
        print("    Every level is explained by  count = bitmap + starting,")
        print("    spoofed to 100, or capped at 99. That is the model the")
        print("    client uses, so it is reading your real progress.")
    return disagree


def cmd_check(G, g, args):
    report(G, g, args.start)
    return 0


def cmd_watch(G, g, args):
    """The same, repeatedly, so a walk between a hub and a level shows the
    spoof going on and coming back off."""
    last = None
    print("    Walk between a hub and a level. Ctrl-C to stop.")
    print()
    try:
        while True:
            f = g.save_file
            D = sys.modules["tazworld.logic"]
            row = []
            for lid in D.LEVEL_ORDER:
                cnt = g._u32(G.T.level_block(lid, f) + G.T.L_SANDWICHES)
                words, why = raw_bitmap(G, lid, f)
                row.append((lid, cnt, None if why else sum(words)))
            key = tuple((c, b) for _, c, b in row)
            if key != last:
                last = key
                bad = [(D.LEVEL_NAME[l], c, b) for l, c, b in row
                       if b is not None and b != c]
                print(f"    [level {g.level_id()}, state {g.game_state()}] "
                      + (", ".join(f"{n} count={c} bitmap={b}"
                                   for n, c, b in bad) or "all agree"))
            time.sleep(0.25)
    except KeyboardInterrupt:
        print()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("check")
    c.add_argument("--start", type=int, default=25,
                   help="your Starting Sandwiches setting")
    c.set_defaults(fn=cmd_check)
    sub.add_parser("watch").set_defaults(fn=cmd_watch)
    args = ap.parse_args()

    G = load_world()
    if G.mem is None:
        raise SystemExit("    pcsx2_mem could not be imported -- pine.py has "
                         "to be in worlds/tazwanted/pcsx2_interface/.")
    g = G.Game()
    if not g.connect():
        raise SystemExit("    could not reach PCSX2. Is it running with the "
                         "game booted, PINE on, and the AP client closed?")
    g.refresh_save_file()
    return args.fn(G, g, args)


if __name__ == "__main__":
    raise SystemExit(main())
