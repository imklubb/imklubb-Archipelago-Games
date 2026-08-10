#!/usr/bin/env python3
"""Does a boss door say the right thing, on the line the game actually shows?

    py -3.13 taz_door_test.py

No emulator. The string table is a dict of fake memory, so a walk between hubs
and levels runs instantly and always the same way.

WHAT IS BEING TESTED
--------------------
A boss door does not have one line of advice. It has five, and the selector at
0x00266F00 picks between them every time the player walks into the trigger:

    all three of the hub's levels complete       -> line 5
    else the level the player was in immediately
      before this hub, if it is complete         -> that level's line
    else                                         -> line 1

So writing ONE of the five puts the message where the player usually will not
see it. That is exactly what used to happen -- line 1 of Elephant Pong's door
was written for all three bosses -- and it is why Linear never showed a poster
count while Open mostly worked: an Open player has finished the hub's levels,
so the door was already showing line 5, which the separate BOSS_HINT path did
rewrite.

The other half of the bug was the guard. The old code compared the message it
wanted to the message it wanted last time, so once the game rebuilt the panel
nothing put it back. Every check below runs the tick twice with the table
disturbed in between.

The strings are packed with zero slack -- Dodge City's shortest line is 29
characters -- so the fix moves the table POINTER at a scratch buffer rather
than writing into the table. Nothing in the game reads the length field.
"""

import importlib.util
import os
import struct
import sys
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")

HUB_OF_BOSS = {7: 3, 12: 8, 17: 13}


def load_game():
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


class Mem:
    """Sparse memory, seeded with the string table as the dump holds it."""

    def __init__(self, G):
        self.b = bytearray()
        self.m = {}
        for door in G.BOSS_DOOR.values():
            for idx, ptr, length in door["lines"]:
                e = G.STR_TABLE + idx * G.STR_STRIDE
                self.write_u32(e, ptr)
                self.write_u32(e + 4, length)
                # The shipping text, so a restore can be seen to work.
                self.write_bytes(ptr, ("x" * length).encode("utf-16-le")
                                 + b"\0\0")

    def read_bytes(self, a, n):
        return bytes(self.m.get(a + i, 0) for i in range(n))

    def write_bytes(self, a, d):
        for i, byte in enumerate(d):
            self.m[a + i] = byte

    def read_u32(self, a):
        return struct.unpack("<I", self.read_bytes(a, 4))[0]

    def write_u32(self, a, v):
        self.write_bytes(a, struct.pack("<I", int(v) & 0xFFFFFFFF))


RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"    {'PASS' if ok else '*** FAIL ***':<12} {label}")
    if detail and not ok:
        print(f"                 {detail}")


def shown(G, g, boss_id):
    """What every one of this door's five lines reads, as a set.

    A set, deliberately: the game chooses one of the five and which one is not
    knowable from here, so the only safe assertion is that they all agree.
    """
    out = set()
    for idx, _, _ in G.BOSS_DOOR[boss_id]["lines"]:
        ptr = g._u32(G.STR_TABLE + idx * G.STR_STRIDE)
        n = g._u32(G.STR_TABLE + idx * G.STR_STRIDE + 4)
        raw = G.mem.read_bytes(ptr, n * 2)
        out.add(raw.decode("utf-16-le"))
    return out


def pointers(G, g, boss_id):
    return [g._u32(G.STR_TABLE + idx * G.STR_STRIDE)
            for idx, _, _ in G.BOSS_DOOR[boss_id]["lines"]]


def originals(G, boss_id):
    return [ptr for _, ptr, _ in G.BOSS_DOOR[boss_id]["lines"]]


def main():
    G = load_game()
    G.mem = Mem(G)
    g = G.Game()
    print("    the boss door panel, against the line the game picks:")
    print()

    MSG = "You can't face the boss until you've collected 21 more wanted posters."

    # ---- all five, not one ------------------------------------------------
    g.boss_door_text(7, MSG)
    check("every one of a door's five lines carries the message",
          shown(G, g, 7) == {MSG},
          f"reads {sorted(shown(G, g, 7))}")

    check("...and the message is longer than the shortest slot, which is the "
          "point",
          len(MSG) > min(n for _, _, n in G.BOSS_DOOR[17]["lines"]),
          f"{len(MSG)} chars vs a 29 character slot")

    # ---- the other doors are not touched ---------------------------------
    check("the doors in other hubs are left alone",
          pointers(G, g, 12) == originals(G, 12)
          and pointers(G, g, 17) == originals(G, 17))

    # ---- it repairs itself -----------------------------------------------
    #
    # The panel is rebuilt from the table every time the player walks into the
    # trigger, and anything at all may have scribbled on it in between. The
    # old guard remembered what it last wanted and so never looked again.
    for idx, ptr, length in G.BOSS_DOOR[7]["lines"]:
        G.mem.write_u32(G.STR_TABLE + idx * G.STR_STRIDE, ptr)
    g.boss_door_text(7, MSG)
    check("a table put back by the game is re-asserted on the next tick",
          shown(G, g, 7) == {MSG},
          f"reads {sorted(shown(G, g, 7))}")

    # ---- and it puts back exactly what was there --------------------------
    g.boss_door_text(7, None)
    check("leaving the hub restores the shipping pointers exactly",
          pointers(G, g, 7) == originals(G, 7),
          f"{[hex(p) for p in pointers(G, g, 7)]} vs "
          f"{[hex(p) for p in originals(G, 7)]}")
    check("...and the lengths with them",
          [g._u32(G.STR_TABLE + i * G.STR_STRIDE + 4)
           for i, _, _ in G.BOSS_DOOR[7]["lines"]]
          == [n for _, _, n in G.BOSS_DOOR[7]["lines"]])

    # ---- the packed table is never written ------------------------------
    #
    # The one mistake here that cannot be undone. Every slot is packed against
    # the next with a single NUL, so a long write eats its neighbour and the
    # original is gone until the game is rebooted.
    before = {a: v for a, v in G.mem.m.items()
              if any(p <= a < p + 200
                     for d in G.BOSS_DOOR.values() for _, p, _ in d["lines"])}
    for boss_id in G.BOSS_DOOR:
        g.boss_door_text(boss_id, MSG)
        g.boss_door_text(boss_id, None)
    after = {a: v for a, v in G.mem.m.items() if a in before}
    check("nothing is ever written into the packed string table itself",
          before == after,
          f"{sum(1 for a in before if before[a] != after.get(a))} bytes "
          f"changed inside the shipping strings")

    # ---- each door gets its own buffer ------------------------------------
    g.boss_door_text(7, "seven")
    g.boss_door_text(12, "twelve")
    g.boss_door_text(17, "seventeen")
    check("three doors overridden at once do not share a buffer",
          (shown(G, g, 7), shown(G, g, 12), shown(G, g, 17))
          == ({"seven"}, {"twelve"}, {"seventeen"}),
          f"{shown(G, g, 7)}, {shown(G, g, 12)}, {shown(G, g, 17)}")
    for boss_id in G.BOSS_DOOR:
        g.boss_door_text(boss_id, None)

    # ---- an entry somebody else owns is refused ---------------------------
    idx = G.BOSS_DOOR[12]["lines"][0][0]
    G.mem.write_u32(G.STR_TABLE + idx * G.STR_STRIDE, 0x00700000)
    try:
        g.boss_door_text(12, MSG)
        refused = ""
    except RuntimeError as exc:
        refused = str(exc)
    check("a table entry pointing somewhere unexpected is refused, not lost",
          "neither the shipping" in refused, refused or "overwrote it anyway")
    G.mem.write_u32(G.STR_TABLE + idx * G.STR_STRIDE,
                    G.BOSS_DOOR[12]["lines"][0][1])

    # ---- too long is an error, not a silent truncation --------------------
    try:
        g.boss_door_text(7, "x" * (G.DOOR_TEXT_CAP + 1))
        refused = ""
    except ValueError as exc:
        refused = str(exc)
    check("a message past the buffer is refused rather than truncated",
          "capacity" in refused, refused or "wrote it anyway")

    # ---- the buffers stay inside notify's scratch -------------------------
    try:
        spec = importlib.util.spec_from_file_location(
            "tazworld.notify", os.path.join(WORLD, "notify.py"))
        sys.modules.setdefault("tazworld.pcsx2_mem",
                               types.ModuleType("tazworld.pcsx2_mem"))
        N = importlib.util.module_from_spec(spec)
        sys.modules["tazworld.notify"] = N
        spec.loader.exec_module(N)
    except Exception as exc:
        check("the door buffers do not collide with notify's scratch", False,
              f"could not load notify.py to ask: {exc!r}")
    else:
        used = set(range(N.CTRL, N.CODE + 4 * len(N._code_words())))
        used |= set(range(N.TEXT_BUF, N.TEXT_BUF + N.TEXT_CAP * 2))
        ours = set(range(G.DOOR_TEXT_BUF,
                         G.DOOR_TEXT_BUF
                         + len(G.BOSS_DOOR) * G.DOOR_TEXT_SLOT))
        check("the door buffers do not collide with notify's scratch",
              not (ours & used) and ours <= set(range(N.SCRATCH_LO,
                                                      N.SCRATCH_HI)),
              f"0x{G.DOOR_TEXT_BUF:08X} for "
              f"{len(G.BOSS_DOOR) * G.DOOR_TEXT_SLOT} bytes")

    # ---- the mapping itself ----------------------------------------------
    check("each door's five lines are consecutive table indices",
          all([i for i, _, _ in d["lines"]]
              == list(range(d["lines"][0][0], d["lines"][0][0] + 5))
              for d in G.BOSS_DOOR.values()))
    check("each door stands in the hub its levels belong to",
          {b: d["hub"] for b, d in G.BOSS_DOOR.items()} == HUB_OF_BOSS)

    print()
    bad = RESULTS.count(False)
    print(f"    {len(RESULTS) - bad}/{len(RESULTS)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
