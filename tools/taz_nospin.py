#!/usr/bin/env python3
"""The No Spinning trap, live, so you can feel it and see what it did.

    py -3.13 taz_nospin.py watch                 what a spin looks like, no trap
    py -3.13 taz_nospin.py run --seconds 30      arm the trap and try to spin
    py -3.13 taz_nospin.py run --states 0x0C     SPINUP only
    py -3.13 taz_nospin.py run --states 0x0C,0x0D,0x0E    the old set

Close the AP client first -- only one thing at a time on PINE.

WHAT IT DOES
------------
It drives the world's own Game.hold_traps at the client's own poll rate, so
this is the shipped trap rather than a copy of it. `watch` writes nothing at
all; `run` writes only what the trap itself writes, which is the state request
at +0x10C.

WHAT TO LOOK FOR
----------------
Spin runs 0x0C SPINUP -> 0x0D SPIN -> 0x0E SPINDOWN, and the trap cancels by
asking for IDLE instead. Offline the three candidate sets were indistinguishable
-- every one of them cancels at SPINUP, because SPINUP always comes first -- so
what is left to learn is how it FEELS, and whether anything else in the level
uses the same states.

The two worth trying deliberately:

  * mash the spin button. SPINUP lasts about a fifth of a second against a
    0.1s poll, which is two samples with one to spare. If a spin ever slips
    through, this prints it.
  * get caged by one of Taz: Haunted's two cage catchers while the trap is
    running, and try to get out. An old note says 0x0C/0x0D/0x0E are the
    cage-escape chain as well; the cage recording did not show them, so it is
    an open question and this is how it gets closed.
"""

import argparse
import importlib.util
import os
import sys
import time
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")

TICK = 0.1                       # client.py's TICK

SPIN_CHAIN = {0x0C: "SPINUP", 0x0D: "SPIN", 0x0E: "SPINDOWN"}
IDLE_STATE = 0x0A


def load_world():
    """The world's own game.py, with its real memory interface.

    Hooked through Game.connect(), not mem.connect(): pcsx2_mem's entry point
    is hook(), and getting that wrong is a traceback rather than a message.
    """
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


def name_of(G, v):
    if v is None:
        return "-"
    return SPIN_CHAIN.get(v) or G.STATE_NAMES.get(v, f"0x{v:02X}") \
        if hasattr(G, "STATE_NAMES") else SPIN_CHAIN.get(v, f"0x{v:02X}")


def cmd_watch(G, g, args):
    """No trap. Just what the spin chain looks like on this machine."""
    print("    READ-ONLY. Spin a few times, and mash it once or twice.")
    print("    Ctrl-C to stop.")
    print()
    last, t0, runs = None, time.time(), []
    cur = None
    try:
        while True:
            st = g.taz_state()
            if st != last:
                if st in SPIN_CHAIN:
                    print(f"    [{time.time() - t0:7.2f}] "
                          f"0x{st:02X} {SPIN_CHAIN[st]}")
                    if st == 0x0C:
                        cur = time.time()
                elif last in SPIN_CHAIN and cur:
                    runs.append(time.time() - cur)
                    cur = None
                last = st
            time.sleep(TICK)
    except KeyboardInterrupt:
        print()
    if runs:
        print(f"    {len(runs)} spin(s), each lasting "
              + ", ".join(f"{r:.2f}s" for r in runs))
    return 0


def cmd_run(G, g, args):
    """Arm the trap and report what it actually cancelled."""
    states = args.states or sorted(G.Game.NO_SPIN_STATES)
    g.NO_SPIN_STATES = set(states)
    print("    cancelling: "
          + ", ".join(f"0x{v:02X} {SPIN_CHAIN.get(v, '?')}" for v in states))
    print(f"    for {args.seconds:.0f}s, polled every {TICK}s -- the rate the "
          "client uses.")
    print()
    print("    Try to spin. Mash it. If you can, get caged and try to escape.")
    print()

    until = time.time() + args.seconds
    active = {"no_spin": until}
    t0, last = time.time(), None
    cancels, got_through, chain = [], [], []
    try:
        while time.time() < until:
            st = g.taz_state()
            if st != last:
                if st in SPIN_CHAIN:
                    chain.append((round(time.time() - t0, 2), st))
                    if st == 0x0D and not any(
                            abs(c - (time.time() - t0)) < 1.0
                            for c in cancels):
                        got_through.append(round(time.time() - t0, 2))
                        print(f"    [{time.time() - t0:7.2f}] "
                              f">> A SPIN GOT THROUGH -- reached 0x0D SPIN")
                last = st
            # The shipped trap, doing its own reading and writing.
            g.hold_traps(active)
            ra = G.mem.deref(G.T.TAZ_PTR, G.T.O_STATE_PTR, G.S_REQUEST)
            try:
                if ra is not None and G.mem.read_u8(ra) == IDLE_STATE:
                    now = round(time.time() - t0, 2)
                    if not cancels or now - cancels[-1] > 0.3:
                        cancels.append(now)
                        print(f"    [{now:7.2f}] cancelled, Taz was in "
                              f"0x{(st or 0):02X} "
                              f"{SPIN_CHAIN.get(st, '?')}")
            except Exception:
                pass
            time.sleep(TICK)
    except KeyboardInterrupt:
        print()

    print()
    print(f"    {len(cancels)} cancel(s), {len(got_through)} spin(s) that got "
          "through")
    seen = sorted({v for _, v in chain})
    print("    states of the spin chain seen: "
          + (", ".join(f"0x{v:02X} {SPIN_CHAIN[v]}" for v in seen) or "none"))
    if got_through:
        print()
        print("    A spin reaching 0x0D means the cancel was missed. If that")
        print("    only happens with --states 0x0C, SPIN is earning its keep")
        print("    as the backstop; if it happens with both, the trap needs")
        print("    to act on the request BEFORE the state, not after it.")
    if not cancels:
        print()
        print("    Nothing cancelled at all. Either no spin was attempted, or")
        print("    the trap is not reaching the request field -- `watch` will")
        print("    tell you which, since it reads the same states.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("watch").set_defaults(fn=cmd_watch)
    r = sub.add_parser("run")
    r.add_argument("--seconds", type=float, default=30.0)
    r.add_argument("--states",
                   type=lambda s: [int(x, 0) for x in s.split(",")],
                   help="comma separated, e.g. 0x0C or 0x0C,0x0D")
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args()

    G = load_world()
    if G.mem is None:
        raise SystemExit("    pcsx2_mem could not be imported -- pine.py has "
                         "to be in worlds/tazwanted/pcsx2_interface/.")
    g = G.Game()
    if not g.connect():
        raise SystemExit("    could not reach PCSX2. Is it running with the "
                         "game booted, PINE on, and the AP client closed?")
    print(f"    connected. Taz is in level {g.level_id()}.")
    print()
    return args.fn(G, g, args)


if __name__ == "__main__":
    raise SystemExit(main())