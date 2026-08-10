#!/usr/bin/env python3
"""Which states should the No Spinning trap cancel?

    py -3.13 taz_nospin_test.py

No emulator: game.py's `mem` is a module attribute, so it is replaced with a
dict and hold_traps' real writes are visible.

THE QUESTION
------------
Spin runs 0x0C SPINUP -> 0x0D SPIN -> 0x0E SPINDOWN. The trap used to cancel
all three. Cancelling at SPINUP is the clean place -- nothing has happened yet,
so it reads as "he did not spin" rather than as a spin cut short.

The worry was that the client polls every 0.1s and SPINUP lasts only about a
fifth of a second, so SPINUP alone might be missed depending on where the poll
lands. Measured, it is not: 0.21s and 0.23s each guarantee two samples. That is
a measurement rather than an opinion, and printing it is the point of this file.

SPINDOWN is the other end: that is the game ENDING a spin, so cancelling it
fights the recovery rather than the action. An older note in game.py says
0x0C/0x0D/0x0E are the cage-escape chain as well -- the cage recording below
does not show any of them, so that is neither confirmed nor ruled out here.
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

TICK = 0.1
TAZ_OBJ, STATE_OBJ = 0x01200000, 0x01300000

SPINUP, SPIN, SPINDOWN, IDLE, MOVE = 0x0C, 0x0D, 0x0E, 0x0A, 0x00

POLICIES = {
    "SPINUP only":            {SPINUP},
    "SPINUP + SPIN":          {SPINUP, SPIN},
    "all three (the old set)": {SPINUP, SPIN, SPINDOWN},
}

# Transcribed from Caleb's two Taz: Haunted recordings. Both are him spinning
# to trigger the transformation, which is the ordinary way a spin happens.
SPINS = {
    "recording 1": [(MOVE, 0.0), (SPINUP, 1.80), (SPIN, 2.01), (0x5D, 3.61)],
    "recording 2": [(IDLE, 0.0), (MOVE, 1.43), (SPINUP, 1.74), (SPIN, 1.97),
                    (0x5D, 3.33)],
}

# The sequences the trap must NOT touch. The cage is the one that matters:
# being unable to leave it is a soft lock, not an inconvenience.
LEAVE_ALONE = {
    "the cage catchers": [(IDLE, 0.0), (0x22, 1.0), (0x54, 2.0), (0x55, 3.5),
                          (MOVE, 8.0)],
    "riding the coaster": [(IDLE, 0.0), (0x4D, 1.0), (0x15, 40.0),
                           (0x04, 40.7), (MOVE, 43.2)],
    "the ball": [(IDLE, 0.0), (0x5E, 1.0), (0x52, 1.2), (MOVE, 20.0)],
}


class FakeMem:
    EE_MIN, EE_MAX = 0x00100000, 0x02000000

    def __init__(self):
        self.w = {}
        self.writes = []

    def valid_ptr(self, p):
        return p is not None and self.EE_MIN <= p < self.EE_MAX

    def read_u32(self, a):
        return self.w.get(a, 0)

    def write_u32(self, a, v):
        self.w[a] = v & 0xFFFFFFFF
        self.writes.append((a, v))

    def read_u8(self, a):
        return self.w.get(a, 0) & 0xFF

    def write_u8(self, a, v):
        self.w[a] = v & 0xFF

    def write_bytes(self, a, d):
        self.w[a] = d

    def read_bytes(self, a, n):
        v = self.w.get(a, b"\0" * n)
        return v if isinstance(v, bytes) else struct.pack("<I", v)[:n]

    def follow(self, addr, *offs):
        p = self.read_u32(addr)
        for o in offs:
            p = self.read_u32(p + o)
        return p

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


def load_game(mem, clock):
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
    g = sys.modules["tazworld.game"]
    g.mem = mem
    g.time = clock
    return g


def state_at(trace, t):
    cur = None
    for sid, start in trace:
        if t >= start:
            cur = sid
        else:
            break
    return cur


def run(G, mem, clock, trace, policy, phase):
    """Poll hold_traps with a No Spinning trap active, and report when it
    cancelled and what state Taz was in at the time."""
    T = G.T
    mem.w.clear()
    mem.write_u32(T.TAZ_PTR, TAZ_OBJ)
    mem.write_u32(TAZ_OBJ + T.O_STATE_PTR, STATE_OBJ)
    g = G.Game()
    g.NO_SPIN_STATES = policy
    end = trace[-1][1]
    cancels, t = [], phase
    while t <= end:
        clock.now = 1000.0 + t
        st = state_at(trace, t)
        mem.write_u32(STATE_OBJ + T.S_STATE, st if st is not None else 0)
        mem.writes.clear()
        g.hold_traps({"no_spin": clock.now + 60.0})
        for a, v in mem.writes:
            if a == STATE_OBJ + G.S_REQUEST:
                cancels.append((round(t, 2), st))
        t += TICK
    return cancels


def main():
    mem, clock = FakeMem(), Clock()
    G = load_game(mem, clock)
    phases = [round(i * 0.01, 3) for i in range(10)]

    print("    does the trap stop the spin? -- every phase offset, both "
          "recordings\n")
    print(f"      {'policy':<26}{'caught':<10}{'cancelled at'}")
    print(f"      {'-' * 26}{'-' * 10}{'-' * 28}")
    for name, policy in POLICIES.items():
        caught, where = 0, set()
        for trace in SPINS.values():
            for ph in phases:
                c = run(G, mem, clock, trace, policy, ph)
                if c:
                    caught += 1
                    where.add(c[0][1])
        total = len(SPINS) * len(phases)
        at = ", ".join(f"0x{v:02X}" for v in sorted(where)) or "-"
        print(f"      {name:<26}{caught}/{total:<8}{at}")

    print()
    print("    and what each one would ALSO cancel:\n")
    bad = 0
    for name, policy in POLICIES.items():
        hits = []
        for what, trace in LEAVE_ALONE.items():
            for ph in phases:
                if run(G, mem, clock, trace, policy, ph):
                    hits.append(what)
                    break
        print(f"      {name:<26}"
              + (", ".join(sorted(set(hits))) if hits else "nothing"))

    print()
    print("    how much room SPINUP alone actually has:\n")
    for name, trace in SPINS.items():
        ids = [sid for sid, _ in trace]
        i = ids.index(SPINUP)
        span = trace[i + 1][1] - trace[i][1]
        print(f"      {name:<16}SPINUP lasts {span:.2f}s = "
              f"{span / TICK:.1f} polls, so {int(span / TICK)} always land "
              f"inside it")
    print("      -> enough on this data, with one poll to spare. A spin that")
    print("         started in under 0.2s would slip past SPINUP alone, which")
    print("         is what SPIN is kept for.")

    print()
    shipped = G.Game.NO_SPIN_STATES
    label = next((n for n, p in POLICIES.items() if p == shipped), str(shipped))
    print(f"    shipping: {label}  "
          + ", ".join(f"0x{v:02X}" for v in sorted(shipped)))

    # The two things that have to hold whichever set is chosen.
    caught = sum(1 for trace in SPINS.values() for ph in phases
                 if run(G, mem, clock, trace, shipped, ph))
    ok = caught == len(SPINS) * len(phases)
    bad += 0 if ok else 1
    print(f"    {'PASS' if ok else '*** FAIL ***':<12} it stops every "
          f"recorded spin, at every phase ({caught}/{len(SPINS) * len(phases)})")

    touched = [w for w, trace in LEAVE_ALONE.items() for ph in phases
               if run(G, mem, clock, trace, shipped, ph)]
    ok = not touched
    bad += 0 if ok else 1
    print(f"    {'PASS' if ok else '*** FAIL ***':<12} it touches nothing "
          f"else{'' if ok else ' -- ' + ', '.join(sorted(set(touched)))}")
    print()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
