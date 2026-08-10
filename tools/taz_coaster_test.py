#!/usr/bin/env python3
"""Replay the recorded coaster rides through death_tick at the client's own
poll rate, at every phase offset, and check the verdict.

The point of the phase sweep: a coaster death's MOVE frame lasts 0.02s and the
client polls every 0.1s, so whether that frame is ever sampled depends on where
the poll happens to land. A rule that only works at some phases is a rule that
works on Caleb's machine on Tuesday.

No emulator. Time is a number this owns.
"""

import importlib.util
import os
import sys
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")
TICK = 0.1                      # client.py's TICK


class Clock:
    """A clock the test drives, substituted for the `time` module in game.py."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, _):
        pass


def load_game(clock):
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
    g.time = clock                       # every time.time() in game.py is ours
    # And no emulator. pcsx2_mem imports fine on a machine that has pine
    # beside it, so leaving this alone would have death_tick making real PINE
    # calls from a unit test -- on Caleb's machine, but not in a sandbox,
    # which is the worst way for a test to differ.
    g.mem = None
    return g


# ------------------------------------------------------- the recorded rides
#
# Straight from taz_coaster.py's output, transcribed. Each entry is
# (state id, the timestamp it BEGAN at); the last is the end of the trace.
# 0x4D ONMINECART  0x00 MOVE  0x08 JUMP  0x0A IDLE
# 0x15 PROJECTILE  0x04 GETUPFROMSLIDE

MINE = 15

TRACES = {
    "death A -- crash, Cartoon Strip-Mine": (
        [(0x0A, 64.0), (0x00, 64.9), (0x08, 66.06), (0x4D, 66.51),
         (0x00, 86.21), (0x08, 86.23), (0x00, 86.58), (None, 88.87)],
        True,
    ),
    "death B -- fall, Cartoon Strip-Mine": (
        [(0x0A, 0.47), (0x00, 1.52), (0x08, 2.32), (0x4D, 2.38),
         (0x00, 58.75), (0x08, 58.79), (0x00, 59.12), (0x0A, 67.06),
         (None, 70.0)],
        True,
    ),
    "survived -- ride completed": (
        [(0x0A, 86.0), (0x00, 86.58), (0x08, 88.87), (0x4D, 89.28),
         (0x15, 193.94), (0x04, 194.63), (0x00, 197.11), (None, 224.43)],
        False,
    ),
}


def state_at(trace, t):
    """What the state field reads at time t, or None past the end."""
    cur = None
    for sid, start in trace:
        if t >= start:
            cur = sid
        else:
            break
    return cur


class Fake:
    """Game with the four memory reads replaced by the trace."""

    def __init__(self, G, clock, trace, lid=MINE):
        self.G, self.clock, self.trace, self.lid = G, clock, trace, lid
        self.g = G.Game()
        self.g.level_id = lambda: self.lid
        self.g.game_state = lambda: G.STATE_ACTIVE
        self.g._state = lambda: state_at(self.trace, self.clock.now - 1000.0)
        self.g.taz_state = self.g._state


def replay(G, trace, phase, lid=MINE):
    """Poll death_tick every TICK from `phase`, and collect what it reported."""
    clock = Clock()
    G.time = clock
    f = Fake(G, clock, trace, lid)
    end = trace[-1][1]
    said, t = [], phase
    while t <= end:
        clock.now = 1000.0 + t
        kind = f.g.death_tick()
        if kind:
            said.append((round(t, 2), kind))
        t += TICK
    return said


def naive_replay(G, trace, phase):
    """What the obvious rule would have done: report on cart -> MOVE.

    Kept so the reason the real rule is inverted is a measurement rather than
    an assertion in a comment.
    """
    clock = Clock()
    prev, said, t = None, [], phase
    end = trace[-1][1]
    while t <= end:
        st = state_at(trace, t)
        if prev == 0x4D and st == 0x00:
            said.append((round(t, 2), "void_out"))
        prev = st
        t += TICK
    return said


# Transcribed from Caleb's vanilla recording in Taz: Haunted -- transform,
# die, transform again, break the Lab Poster, take the teleporter out.
HAUNTED = 14

HAUNTED_TRACES = {
    "ball death -- the game puts him back to MOVE": (
        [(0x0A, 0.0), (0x0C, 1.80), (0x0D, 2.01), (0x5D, 3.61), (0x51, 3.78),
         (0x5E, 12.11), (0x52, 12.13), (0x00, 21.49), (0x5D, 29.82),
         (None, 30.0)],
        True,
    ),
    "the Lab Poster as the ball -- he stays a ball": (
        [(0x5E, 0.0), (0x52, 0.02), (0x31, 51.0), (0x00, 52.7), (None, 60.0)],
        False,
    ),
}


def haunted(G, trace, phase, moved_at=None):
    """Replay one of those at the client's poll rate.

    `moved_at` fakes the state object being rebuilt at that moment, which is
    what a half-built 0x00 looks like from outside.
    """
    clock = Clock()
    G.time = clock
    f = Fake(G, clock, trace, lid=HAUNTED)
    f.g.force_spin = lambda: True
    obj = [0x0083E3D0]
    said, t = [], phase
    end = trace[-1][1]

    class M:
        """Only the one call death_tick makes on the state object."""

        def follow(self, *a):
            return obj[0]

    g = sys.modules["tazworld.game"]
    g.mem = M()
    while t <= end:
        clock.now = 1000.0 + t
        if moved_at is not None and abs(t - moved_at) < 0.05:
            obj[0] += 0x40
        k = f.g.death_tick()
        if k:
            said.append((round(t, 2), k))
        t += TICK
    g.mem = None
    return said


# The other Taz: Haunted recording: one of the two catchers that cages rather
# than nets.  0x22 PLAYANIMATION -> 0x54 CAGED -> 0x55 CAGEDMOVE
CAGE_TRACE = [(0x00, 0.0), (0x22, 38.40), (0x54, 39.40), (0x55, 41.25),
              (0x00, 46.0), (None, 50.0)]

# An ordinary keeper: the net first, the cage after. That is ONE capture.
#
# Starts on IDLE rather than MOVE because death_tick will not trust a reading
# until it has seen Taz demonstrably alive, and 0x00 is both MOVE and what a
# half-built state object reads -- so it does not count. Sitting in pure MOVE
# from the level load never satisfies that, which is unreachable in play but
# very easy to write into a test.
NET_THEN_CAGE = [(0x0A, 0.0), (0x00, 2.0), (0x59, 10.0), (0x54, 12.0),
                 (0x55, 13.5), (0x00, 18.0), (None, 22.0)]


def main():
    clock = Clock()
    G = load_game(clock)
    print(f"    COASTER_STATE    0x{G.COASTER_STATE:02X}")
    print("    COASTER_DISMOUNT " + ", ".join(
        f"0x{v:02X}" for v in sorted(G.COASTER_DISMOUNT)))
    print(f"    COASTER_VERDICT  {G.COASTER_VERDICT}s"
          f"    COASTER_GRACE {G.COASTER_GRACE}s")
    print()

    phases = [round(i * 0.01, 3) for i in range(10)]
    bad = 0

    for name, (trace, want_death) in TRACES.items():
        hits, naive_hits = [], []
        for ph in phases:
            got = replay(G, trace, ph)
            hits.append(len(got) == 1 and got[0][1] == "void_out")
            naive_hits.append(len(naive_replay(G, trace, ph)) == 1)
        ok = all(h == want_death for h in hits)
        bad += 0 if ok else 1
        n_ok = sum(1 for h in hits if h == want_death)
        n_naive = sum(1 for h in naive_hits if h == want_death)
        print(f"    {name}")
        print(f"      want {'a death' if want_death else 'NO death':<9}"
              f"   this rule {n_ok}/{len(phases)} phases"
              f"   {'PASS' if ok else '*** FAIL ***'}")
        print(f"      the obvious 'cart -> MOVE' rule instead: "
              f"{n_naive}/{len(phases)} phases")
        got = replay(G, trace, 0.0)
        if got:
            first = trace[[s for s, _ in trace].index(0x4D) + 1][1]
            print(f"      reported {got} "
                  f"({got[0][0] - first:.2f}s after the cart ended)")
        print()

    # A ride that is merely long must not time out into a death, and re-boarding
    # must not either.
    long_ride = [(0x0A, 0.0), (0x4D, 1.0), (0x15, 400.0), (0x04, 400.7),
                 (0x00, 403.2), (None, 410.0)]
    got = replay(G, long_ride, 0.0)
    ok = not got
    bad += 0 if ok else 1
    print(f"    a 400s ride, survived: {'PASS' if ok else f'*** FAIL {got}'}")

    reboard = [(0x0A, 0.0), (0x4D, 1.0), (0x00, 30.0), (0x4D, 30.2),
               (0x15, 60.0), (0x04, 60.7), (0x00, 63.2), (None, 70.0)]
    got = replay(G, reboard, 0.0)
    ok = not got
    bad += 0 if ok else 1
    print(f"    hop off and straight back on: {'PASS' if ok else f'*** FAIL {got}'}")

    # Leaving the level mid-ride is not a death of this kind.
    clock2 = Clock()
    G.time = clock2
    f = Fake(G, clock2, [(0x0A, 0.0), (0x4D, 1.0), (0x00, 20.0), (None, 30.0)])
    said, t = [], 0.0
    while t <= 30.0:
        clock2.now = 1000.0 + t
        if 19.9 <= t <= 25.0:
            f.g.game_state = lambda: 5        # a load
        else:
            f.g.game_state = lambda: G.STATE_ACTIVE
        k = f.g.death_tick()
        if k:
            said.append((round(t, 2), k))
        t += TICK
    ok = not said
    bad += 0 if ok else 1
    print(f"    load starts as the cart ends: {'PASS' if ok else f'*** FAIL {said}'}")

    # ---- note 2: effects are held for the whole ride and the dismount
    clock3 = Clock()
    G.time = clock3
    f = Fake(G, clock3, TRACES["survived -- ride completed"][0])
    checks = [
        (100.0, True,  "mid-ride"),
        (193.9, True,  "the last moment on the cart"),
        (194.5, True,  "mid-flight, thrown clear"),
        (196.0, True,  "getting up"),
        (197.2, True,  "walking again, still inside the grace"),
        (198.0, False, "grace expired"),
    ]
    print()
    for t, want, why in checks:
        clock3.now = 1000.0 + t
        got = f.g.on_coaster()
        ok = got == want
        bad += 0 if ok else 1
        print(f"    on_coaster at {t:6.1f}s ({why:<30}) = {str(got):<5} "
              f"{'PASS' if ok else '*** FAIL ***'}")

    # The case on_coaster cannot cover by itself: a whole ride with nothing in
    # the queue, so grant_effect is never called and never arms the grace --
    # then an item arrives while Taz is still in mid-air. Only death_tick
    # running every poll keeps the deadline alive, so this passes only if it
    # really does.
    #
    # Asserted on on_coaster rather than on grant_effect: "dynamite" is in
    # DEFER_UNTIL_SAFE, and safe_to_interrupt's debounce returns False on its
    # first call, so grant_effect says "defer" on the very first tick whatever
    # the coaster does. That made an earlier version of this test pass against
    # a build with the fix removed.
    print()
    clock4 = Clock()
    G.time = clock4
    f4 = Fake(G, clock4, TRACES["survived -- ride completed"][0])
    t = 86.0
    while t < 194.4:                      # poll death_tick, never grant_effect
        clock4.now = 1000.0 + t
        f4.g.death_tick()
        t += TICK
    clock4.now = 1000.0 + 194.5           # an item lands mid-flight
    ok = f4.g.on_coaster()
    bad += 0 if ok else 1
    print(f"    ride with an empty queue, item lands mid-flight: "
          f"{'PASS' if ok else '*** FAIL -- it would have fired ***'}")

    print()
    print("    Taz: Haunted, the ball:")
    for name, (trace, want) in HAUNTED_TRACES.items():
        hits = [bool(haunted(G, trace, ph)) for ph in phases]
        ok = all(h == want for h in hits)
        bad += 0 if ok else 1
        print(f"      {'PASS' if ok else '*** FAIL ***':<12} {name}")
        print(f"                   want {'a death' if want else 'NO death'}, "
              f"got it on {sum(hits)}/{len(phases)} phases")

    # the object being rebuilt under us is not a death, whatever it reads
    trace = HAUNTED_TRACES["ball death -- the game puts him back to MOVE"][0]
    got = haunted(G, trace, 0.0, moved_at=21.6)
    ok = not got
    bad += 0 if ok else 1
    print(f"      {'PASS' if ok else '*** FAIL ***':<12} a state object "
          f"rebuilt mid-transition is not a death")

    # a single-sample 0x00 blip must not fire
    blip = [(0x5E, 0.0), (0x52, 0.02), (0x00, 30.0), (0x52, 30.12),
            (0x31, 51.0), (0x00, 52.7), (None, 60.0)]
    got = haunted(G, blip, 0.0)
    ok = not got
    bad += 0 if ok else 1
    print(f"      {'PASS' if ok else '*** FAIL ***':<12} a 0.1s blip to MOVE "
          f"and straight back is not a death")

    print()
    print("    Taz: Haunted, the cage catchers:")
    for name, trace, want in (
            ("a cage on its own is a capture", CAGE_TRACE, ["captures"]),
            ("a net then a cage is ONE capture", NET_THEN_CAGE, ["captures"])):
        kinds = [k for _, k in haunted(G, trace, 0.0)]
        ok = kinds == want
        bad += 0 if ok else 1
        print(f"      {'PASS' if ok else '*** FAIL ***':<12} {name}")
        if not ok:
            print(f"                   reported {kinds}, wanted {want}")

    print()
    print("    ALL PASS" if not bad else f"    {bad} FAILURE(S)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
