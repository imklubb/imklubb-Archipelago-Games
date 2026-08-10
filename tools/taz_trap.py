#!/usr/bin/env python3
"""taz_trap.py -- fire ONE trap at Taz and watch exactly what happens.

No Archipelago, no server, no seed, no queue. It drives the SHIPPED
Game.grant_effect the way client.py's loop does -- one call every 0.1s,
holding and ending the effect the same way -- and prints Taz's state on
every tick so the whole sequence is visible.

WHY IT EXISTS
-------------
Four traps used to be held back while Taz was spinning, on the theory that
starting one mid-spin breaks his model. That is true of effects which write
S_STATE directly -- burp, chilli pepper, hiccup -- and NOT of these four,
which install a handler and ask through S_REQUEST exactly as the game's own
0x002C44D8 does. The engine performs the transition itself, from whatever Taz
happens to be doing. Spin into a stick of dynamite in vanilla and he stops
and takes it.

Deferring them was inventing a restriction the game does not have, and the
attempted cure was worse: cancelling the spin to hurry the trap along fought
the player's own held button, cancel-SPINUP-cancel-SPINUP ten times a second
until they let go of circle. This tool recorded that, which is how it was
found.

    py -3.13 taz_trap.py try dynamite         lands, whatever Taz is doing
    py -3.13 taz_trap.py try dynamite --old   the old defer-until-idle rule

`--old` changes nothing on disk. It moves the trap into the direct-write
family for that one run so the two can be compared back to back.

VERBS
-----
    py -3.13 taz_trap.py check              offline. Prove the tool works.
    py -3.13 taz_trap.py list               the traps and how each is gated
    py -3.13 taz_trap.py try <trap> [opts]  live. Fire one.

      --old          defer until Taz is idle, the way it used to
      --wait N       seconds to keep trying before giving up (default 20)
      --countdown N  seconds before firing, to get Taz spinning (default 5)

Close the AP client first -- PINE takes one connection at a time.
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

# The names grant_effect answers to, from client.py's EFFECT_ITEMS. Read from
# that file rather than copied, so this cannot drift from what the client
# actually sends -- two hand-written copies of one fact is how the costume
# name table ended up with "Skateboarder" in one place and "Skater" in the
# other.
def effect_names():
    import re
    src = open(os.path.join(WORLD, "client.py"), encoding="utf-8").read()
    block = src[src.index("EFFECT_ITEMS = {"):]
    block = block[:block.index("}")]
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block))


STATE_NAME = {
    0x0C: "SPINUP", 0x0D: "SPIN", 0x0E: "SPINDOWN",
    0x1D: "ELECTROCUTED", 0x2E: "MOVESQUASHED", 0x3A: "BUBBLEGUM",
    0x3B: "CHILLIPEPPER", 0x4F: "BADFOOD (dynamite)",
    0x54: "CAGED", 0x55: "CAGEDMOVE", 0x59: "CAUGHT",
}


def load_game():
    """The real game.py, under a synthetic package, exactly as the tests do."""
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


# ---------------------------------------------------------------- offline

class FakeMem:
    """Enough of pcsx2_mem to run grant_effect with nothing plugged in."""

    EE_MIN, EE_MAX = 0x00100000, 0x02000000
    TAZ, STATE, COSTUME, BONUS = 0x00500000, 0x00510000, 0x00520000, 0x00530000

    def __init__(self):
        self.w = {}
        self.writes = []

    def valid_ptr(self, p):
        return p is not None and self.EE_MIN <= p < self.EE_MAX

    def read_u32(self, a):
        return self.w.get(a, 0)

    def write_u32(self, a, v):
        self.writes.append((a, v))
        self.w[a] = v & 0xFFFFFFFF

    def read_u8(self, a):
        v = self.w.get(a, 0)
        return (v if isinstance(v, int) else 0) & 0xFF

    def write_u8(self, a, v):
        self.writes.append((a, v))
        self.w[a] = v & 0xFF

    def read_bytes(self, a, n):
        v = self.w.get(a, b"\0" * n)
        return v if isinstance(v, bytes) else struct.pack("<I", v)[:n]

    def write_bytes(self, a, d):
        self.writes.append((a, d))
        self.w[a] = d

    def read_float(self, a):
        return struct.unpack("<f", self.read_bytes(a, 4))[0]

    def write_float(self, a, v):
        self.write_bytes(a, struct.pack("<f", v))

    def deref(self, addr, *offs):
        p = self.read_u32(addr)
        for o in offs[:-1]:
            p = self.read_u32(p + o)
        return (p + offs[-1]) if offs else p


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, _):
        pass


def cmd_check(args):
    """Offline. Prove this tool drives the real thing, before it costs time
    at an emulator -- the rule this project keeps relearning."""
    mem, clock = FakeMem(), Clock()
    G = load_game()
    G.mem = mem
    G.time = clock
    g = G.Game()
    T = G.T
    ok = [0, 0]

    def check(label, good, detail=""):
        ok[1] += 1
        ok[0] += 1 if good else 0
        print(f"  {'ok  ' if good else 'FAIL'}  {label}"
              + (f"   {detail}" if detail and not good else ""))

    print("\n  driving the shipped grant_effect over a fake EE\n")

    names = effect_names()
    traps = {v for k, v in names.items() if k.endswith("Trap")}
    check("client.py's trap list was read", len(traps) == 6, sorted(traps))

    # The two families, derived from grant_effect's own dispatch rather than
    # from the comment beside the sets.
    import re
    src = open(os.path.join(WORLD, "game.py"), encoding="utf-8").read()
    body = src[src.index("    def grant_effect(self, name):"):
               src.index("    def _squash_bit(self")]
    routed = set(re.findall(
        r'if name == "(\w+)":\s*\n\s*return self\.(?:_install_state|_squash)',
        body))
    check("REQUEST_PATH is exactly what goes through the request field",
          g.REQUEST_PATH == routed,
          f"{sorted(g.REQUEST_PATH)} vs {sorted(routed)}")
    check("DEFER_UNTIL_SAFE is exactly what writes S_STATE directly",
          g.DEFER_UNTIL_SAFE == {n for n, sp in g.POWERUPS.items()
                                 if sp.get("state") is not None},
          sorted(g.DEFER_UNTIL_SAFE))
    check("a spin does not block a request-path trap",
          not (g.SPIN_STATES & g.NOT_PLAYABLE))
    check("but a death or a capture does",
          {0x2C, 0x3D, 0x59, 0x54} <= g.NOT_PLAYABLE)

    def at(state):
        mem.w.clear()
        mem.writes.clear()
        mem.write_u32(T.TAZ_PTR, mem.TAZ)
        mem.write_u32(mem.TAZ + T.O_STATE_PTR, mem.STATE)
        mem.write_u32(mem.TAZ + T.O_COSTUME_PTR, mem.COSTUME)
        mem.write_u32(mem.STATE + T.S_STATE, state)
        mem.write_u32(mem.STATE + G.S_REQUEST, state)
        g._safe_since = None
        g._playable_since = None
        for _ in range(6):
            clock.now += 0.1
            g.safe_to_interrupt()
            g.playable()

    req = mem.STATE + G.S_REQUEST
    at(0x0D)
    got = g.grant_effect("dynamite")
    check("dynamite lands on a spinning Taz", got != "defer", repr(got))
    check("...by asking, not by writing the state",
          mem.read_u32(req) == T.EAT_BAD_FOOD_STATE
          and mem.read_u32(mem.STATE + T.S_STATE) == 0x0D)
    check("...and nothing cancels the spin",
          mem.read_u32(req) != G.IDLE_STATE)

    at(0x0D)
    check("burp still waits -- it writes S_STATE directly",
          g.grant_effect("burp") == "defer")

    at(0x59)
    check("nothing lands on a captured Taz",
          g.grant_effect("dynamite") == "defer")

    print(f"\n  {ok[0]}/{ok[1]} passed")
    if ok[0] != ok[1]:
        print("  Do NOT run this live until that is fixed.\n")
        return 1
    print("  The tool drives the real code path. Safe to run live.\n")
    return 0


def cmd_list(args):
    G = load_game()
    g = G.Game
    names = effect_names()
    print("\n  item                    effect          enters state via   "
          "may land mid-spin?")
    print("  " + "-" * 74)
    for item, eff in sorted(names.items()):
        if not item.endswith("Trap"):
            continue
        if eff in g.REQUEST_PATH:
            how, spin = "S_REQUEST (asks)", "YES"
        elif eff in g.DEFER_UNTIL_SAFE:
            how, spin = "S_STATE (writes)", "no"
        else:
            how, spin = "nothing", "n/a"
        print(f"  {item:<23} {eff:<15} {how:<18} {spin}")
    print("\n  Asking through S_REQUEST is the game's own mechanism, so the")
    print("  engine performs the transition from whatever Taz is doing --")
    print("  spinning included, exactly as vanilla does. Writing S_STATE")
    print("  skips the exit of whatever he was in, and mid-spin that breaks")
    print("  his model.\n")
    return 0


# ---------------------------------------------------------------- live

def cmd_try(args):
    G = load_game()
    if G.mem is None:
        print("    pcsx2_mem did not import; pcsx2_interface/pine.py is "
              "missing from the world.")
        return 1
    mem = G.mem
    T = G.T
    g = G.Game()
    try:
        if not g.connect():
            print("    could not reach PCSX2 on PINE. Is the game running, "
                  "and is PINE enabled in Settings -> Advanced?")
            print("    Close the AP client first -- one connection at a time.")
            return 1
    except Exception as e:
        print(f"    hooking PCSX2 failed: {type(e).__name__}: {e}")
        return 1

    names = effect_names()
    valid = {v for k, v in names.items() if k.endswith("Trap")}
    if args.trap not in valid:
        print(f"    '{args.trap}' is not a trap. Try: {', '.join(sorted(valid))}")
        return 2

    # --old reproduces the previous rule for comparison, by moving this trap
    # into the direct-write family for one run. Nothing on disk changes.
    if args.old:
        g.REQUEST_PATH = set(g.REQUEST_PATH) - {args.trap}
        g.DEFER_UNTIL_SAFE = set(g.DEFER_UNTIL_SAFE) | {args.trap}

    def state():
        try:
            a = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, T.S_STATE)
            return None if a is None else mem.read_u8(a)
        except Exception:
            return None

    def request():
        try:
            a = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, G.S_REQUEST)
            return None if a is None else mem.read_u32(a)
        except Exception:
            return None

    def show(st):
        if st is None:
            return "??"
        return f"0x{st:02X} {STATE_NAME.get(st, '')}".strip()

    print(f"\n  {args.trap}"
          + ("   (--old: defer until Taz is idle)" if args.old
             else "   (asks through S_REQUEST -- should land mid-spin)"))
    print(f"  Spin, and keep spinning. Firing in {args.countdown}s.\n")
    for n in range(args.countdown, 0, -1):
        print(f"    {n}...  Taz is {show(state())}")
        time.sleep(1.0)

    t0 = time.time()
    last = None
    until = "defer"
    print()
    while time.time() - t0 < args.wait:
        st, rq = state(), request()
        until = g.grant_effect(args.trap)
        now_rq = request()
        line = (f"  t+{time.time() - t0:5.2f}  state {show(st):<20} "
                f"-> {until if until == 'defer' else 'APPLIED'}")
        if now_rq != rq and until != "defer":
            line += f"   request <- 0x{now_rq:02X}"
        if line[:40] != (last or "")[:40] or until != "defer":
            print(line)
        last = line
        if until != "defer":
            break
        time.sleep(0.1)

    if until == "defer":
        print(f"\n  Gave up after {args.wait}s -- it never applied.")
        if args.old:
            print("  That is the old behaviour: the trap waits out the spin.")
        return 1

    print(f"\n  Applied after {time.time() - t0:.2f}s. Taz is now "
          f"{show(state())}")

    # Hold and end it the way client.py's loop does, so the whole life of the
    # trap is exercised and nothing is left pinned on.
    if until:
        while time.time() < until:
            st = state()
            print(f"  holding... {show(st)}", end="\r")
            time.sleep(0.2)
        print()
        if args.trap in g.POWERUPS:
            g.end_powerup(args.trap)
        elif args.trap == "squash":
            g.end_squash()
        print(f"  ended. Taz is {show(state())}")
    print()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("check", help="offline").set_defaults(fn=cmd_check)
    sub.add_parser("list", help="the traps and their gating").set_defaults(
        fn=cmd_list)
    t = sub.add_parser("try", help="live: fire one trap")
    t.add_argument("trap")
    t.add_argument("--old", action="store_true",
                   help="defer until idle, the way it used to")
    t.add_argument("--wait", type=float, default=20.0)
    t.add_argument("--countdown", type=int, default=5)
    t.set_defaults(fn=cmd_try)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
