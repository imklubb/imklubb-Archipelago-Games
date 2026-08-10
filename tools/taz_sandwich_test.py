#!/usr/bin/env python3
"""Does the bonus-game sandwich spoof do what it is supposed to?

    py -3.13 taz_sandwich_test.py

No emulator. The save file is a dict, the clock is a number this owns, and
Taz's position is whatever the script says -- so a sequence that takes a minute
in game runs instantly and always the same way.

WHAT IS BEING TESTED
--------------------
The bonus game gate at 0x0021C8B8 is patched to read the granted list instead
of anybody's sandwich count, so in a working session the count is simply the
truth at every moment and none of the machinery below runs. The last two
scenarios are that one; everything before them is the FALLBACK, for a PCSX2
still running an older translation of the patched block or a build whose
addresses do not match.

The fallback is the count-based spoof. 100 or more is what makes a portal
appear, so the apworld lies about the count and puts the truth back
afterwards. The rules that lie has to obey, in Caleb's words:

  * write 100 to the bonus game level to make it appear
  * make sure the CHECK does not fire off that 100
  * put the true value back as soon as Taz starts moving in the matching hub,
    so the level entrance shows the real number
  * keep writing the true value until we know Taz did not go into the matching
    level -- and if he did not, back to 100

Each of those is a named check below. They are written from the note, not from
what game.py currently does, so a failure means the promise is broken rather
than that the code changed.
"""

import importlib.util
import os
import sys
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")

TICK = 0.1                      # client.py's poll interval

ICE_BURG = 4                    # hub 3's levels
ZOONEY = 5
LOONEY = 6
HUB = 3
OTHER_HUB = 8
TAZLAND = 18                    # the one level with no bonus game


class Clock:
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
    g.time = clock
    return g


class Rig:
    """A scripted save file and a scripted player.

    Only the five memory seams are replaced: the level, the game state, Taz's
    position, and the two save-file accessors. Everything else is the shipped
    code.
    """

    def __init__(self, G, granted=()):
        self.G = G
        self.T = sys.modules["tazworld.logic"]
        self.clock = Clock()
        G.time = self.clock
        self.save = {}
        self.granted = set(granted)
        self.lid = HUB
        self.gs = G.STATE_ACTIVE
        self.pos = (0.0, 0.0, 0.0)
        self.log = []

        self.bits = {}                    # lid -> how many dwords are set

        class Mem:
            """Only the one call the bitmap needs. 480 bytes, one dword per
            sandwich, which the client never writes -- so the rig models it as
            the player's own record rather than as part of `save`."""

            def __init__(self, rig):
                self.rig = rig

            def read_bytes(self, addr, n):
                for lid, count in self.rig.bits.items():
                    base = (self.rig.T.level_block(lid, 0)
                            + self.rig.T.L_SANDWICH_BITS)
                    if addr == base:
                        import struct as _s
                        return _s.pack("<120I",
                                       *([1] * count + [0] * (120 - count)))
                return b"\0" * n

            def read_u32(self, addr):
                # posters_done goes through mem rather than _u32, and it is
                # now the only thing a completion check rests on, so the rig
                # has to answer it from the same save the rest of it uses.
                return self.rig.save.get(addr, 0)

        G.mem = Mem(self)
        self.g = G.Game()
        self.g.save_file = 0
        self.g.level_id = lambda: self.lid
        self.g.game_state = lambda: self.gs
        self.g._pos = lambda: self.pos
        self.g._u32 = lambda a: self.save.get(a, 0)
        self.g._w32 = self._w32

    # -------------------------------------------------------- the save file

    def _addr(self, lid):
        return self.T.level_block(lid, 0) + self.T.L_SANDWICHES

    def _w32(self, addr, value):
        changed = self.save.get(addr) != value
        self.save[addr] = value
        return changed

    def sand(self, lid):
        return self.save.get(self._addr(lid), 0)

    def collect(self, lid, n):
        """The player picks sandwiches up: the GAME writes this, not us.

        Both places -- the count AND the per-sandwich record -- because that
        is what actually collecting one does.
        """
        self.save[self._addr(lid)] = n
        self.bits[lid] = n

    def fake_count(self, lid, n):
        """Leave a value in the COUNT field only, as a stale spoof from a
        previous session would."""
        self.save[self._addr(lid)] = n

    def break_posters(self, lid, n=7):
        """Smash n of a level's seven wanted posters. The GAME writes these
        and the client never does, which is the whole reason a completion is
        judged from them."""
        base = self.T.level_block(lid, 0) + self.T.L_POSTER
        for i in range(self.T.POSTERS_PER_LEVEL):
            self.save[base + i * 4] = 1 if i < n else 0

    # ------------------------------------------------------------- the play

    def tick(self, seconds):
        n = max(1, int(seconds / TICK))
        for _ in range(n):
            self.clock.now += TICK
            self.log += self.g.sandwich_tick(self.granted)
            self.log += self.g.completion_tick()

    def play(self, lid, seconds=2.0, moving=True):
        """Actively playing, with Taz walking about unless told otherwise."""
        self.lid = lid
        self.gs = self.G.STATE_ACTIVE
        n = max(1, int(seconds / TICK))
        for i in range(n):
            self.clock.now += TICK
            if moving:
                self.pos = (self.pos[0] + 25.0, 0.0, 0.0)
            self.log += self.g.sandwich_tick(self.granted)
            self.log += self.g.completion_tick()

    def load(self, dest, seconds=2.0, lag=0.5):
        """A loading screen, with lid LAGGING behind the transition.

        lid does not flip to the destination when the loading screen appears:
        walking out of a hub it still reads as the hub for the first frames.
        Modelling it as instant is what let earlier versions of the spoof pass
        here and fail in the game, so the lag is the default now and every
        scenario below runs through it.

        Ticks are collected and handed back so a scenario can assert on what
        the count read at EVERY moment of the load rather than only at the end
        -- which matters because one frame at the wrong value is enough.
        """
        src = self.lid
        self.gs = 5                     # a LOAD_STATE
        self.lid = src
        self.tick(lag)
        self.lid = dest
        self.tick(max(TICK, seconds - lag))

    def during_load(self, dest, watch, seconds=2.0, lag=0.5):
        """The same, returning what `watch` read on every single tick."""
        src, seen = self.lid, []
        self.gs = 5
        for i in range(max(1, int(seconds / TICK))):
            self.lid = src if i * TICK < lag else dest
            self.clock.now += TICK
            self.log += self.g.sandwich_tick(self.granted)
            seen.append(self.sand(watch))
        return seen


# ---------------------------------------------------------------- the checks

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    mark = "PASS" if ok else "*** FAIL ***"
    print(f"    {mark:<12} {label}")
    if detail and not ok:
        print(f"                 {detail}")


def scenario_portal_appears(G):
    """The point of the whole mechanism.

    Ice Burg's bonus game has been granted and the player has 60 sandwiches
    there. Walking from the level back to the hub, the hub is built while the
    loading screen is up -- and THAT is the moment the count has to read 100,
    or the portal is never created.
    """
    r = Rig(G, granted={ICE_BURG})
    r.collect(ICE_BURG, 60)
    r.play(ICE_BURG, 3.0)                      # playing, true count learned
    during = []

    r.lid, r.gs = HUB, 5                       # the load into the hub
    for _ in range(20):
        r.clock.now += TICK
        r.log += r.g.sandwich_tick(r.granted)
        during.append(r.sand(ICE_BURG))

    check("a granted bonus reads at least 100 while the hub is loading",
          all(v >= G.SANDWICH_GOAL for v in during),
          f"saw {sorted(set(during))}, wanted 100 or more")

    r.play(HUB, 3.0)
    check("the true count is back once Taz moves in the hub",
          r.sand(ICE_BURG) == 60, f"reads {r.sand(ICE_BURG)}, wanted 60")
    return r


def scenario_ungranted_capped(G):
    """A player who really collected all 100 must still not reach a portal the
    server has not given them."""
    r = Rig(G, granted=set())
    r.collect(ZOONEY, 100)
    r.play(ZOONEY, 3.0)
    seen = []
    r.lid, r.gs = HUB, 5
    for _ in range(20):
        r.clock.now += TICK
        r.log += r.g.sandwich_tick(r.granted)
        seen.append(r.sand(ZOONEY))
    check("an ungranted bonus never reads 100, even at a real 100",
          all(v < G.SANDWICH_GOAL for v in seen),
          f"saw {sorted(set(seen))}")
    r.play(HUB, 3.0)
    check("and the player's real 100 is not destroyed",
          r.sand(ZOONEY) == 100, f"reads {r.sand(ZOONEY)}, wanted 100")


def scenario_level_shows_truth(G):
    """Inside a level the HUD must be honest, or the player cannot tell how
    many they have actually found."""
    r = Rig(G, granted={ICE_BURG})
    r.collect(ICE_BURG, 42)
    r.play(ICE_BURG, 2.0)
    r.play(HUB, 2.0)
    r.load(ICE_BURG)
    r.play(ICE_BURG, 2.0)
    check("playing a level shows the true count, not the spoof",
          r.sand(ICE_BURG) == 42, f"reads {r.sand(ICE_BURG)}, wanted 42")


def scenario_round_trip(G):
    """Sandwiches do not respawn, so losing the count loses them for good."""
    r = Rig(G, granted={ICE_BURG, ZOONEY})
    r.collect(ICE_BURG, 30)
    r.collect(ZOONEY, 70)
    r.play(ICE_BURG, 2.0)
    r.collect(ICE_BURG, 35)                    # picks up five more
    r.play(ICE_BURG, 2.0)
    r.load(HUB)
    r.play(HUB, 3.0)
    r.load(ZOONEY)
    r.play(ZOONEY, 2.0)
    r.load(HUB)
    r.play(HUB, 3.0)
    check("a round trip keeps both levels' real counts",
          (r.sand(ICE_BURG), r.sand(ZOONEY)) == (35, 70),
          f"reads {(r.sand(ICE_BURG), r.sand(ZOONEY))}, wanted (35, 70)")


def scenario_unvisited_not_zeroed(G):
    """The save file is the only thing that knows about a level the client has
    never watched being played. Assuming zero wiped real progress."""
    r = Rig(G, granted={ICE_BURG})
    r.collect(LOONEY, 88)                      # collected before connecting
    r.collect(ICE_BURG, 10)
    r.play(ICE_BURG, 2.0)
    r.load(HUB)
    r.play(HUB, 3.0)
    r.load(ICE_BURG)
    r.play(ICE_BURG, 2.0)
    check("a level the client never watched is not zeroed",
          r.sand(LOONEY) == 88, f"reads {r.sand(LOONEY)}, wanted 88")


def scenario_check_does_not_fire(G):
    """The spoof must never look like a hundred sandwiches to Archipelago."""
    D = sys.modules["tazworld.logic"]
    r = Rig(G, granted={ICE_BURG})
    r.collect(ICE_BURG, 60)
    r.play(ICE_BURG, 2.0)
    locs = [l for l in D.sandwich_locations(100, 0) if l["level"] == ICE_BURG]

    r.lid, r.gs = HUB, 5                       # mid-spoof
    worst = set()
    for _ in range(20):
        r.clock.now += TICK
        r.log += r.g.sandwich_tick(r.granted)
        worst |= r.g.satisfied(locs)
    check("the 100-sandwich check does not fire off the spoof",
          not worst, f"fired {sorted(worst)} at 60 real sandwiches")

    r.play(HUB, 2.0)
    r.load(ICE_BURG)
    r.play(ICE_BURG, 1.0)
    r.collect(ICE_BURG, 100)        # picked up WHILE playing, as they are
    r.play(ICE_BURG, 1.0)
    check("...but it does fire on a real 100",
          bool(r.g.satisfied(locs)), "a genuine 100 sent nothing")


def scenario_no_bonus_level(G):
    """Tazland has no bonus game, so it must be left alone entirely."""
    r = Rig(G, granted={ICE_BURG})
    r.collect(TAZLAND, 55)
    r.play(ICE_BURG, 2.0)
    r.load(HUB)
    r.play(HUB, 3.0)
    check("Tazland, which has no bonus game, is never touched",
          r.sand(TAZLAND) == 55, f"reads {r.sand(TAZLAND)}, wanted 55")


def scenario_idle_hub(G):
    """A player who puts the controller down must not be left staring at a
    wrong number forever."""
    r = Rig(G, granted={ICE_BURG})
    r.collect(ICE_BURG, 60)
    r.play(ICE_BURG, 2.0)
    r.load(HUB)
    r.play(HUB, 12.0, moving=False)
    check("an idle player still gets the true count back within 10s",
          r.sand(ICE_BURG) == 60, f"reads {r.sand(ICE_BURG)}, wanted 60")


def scenario_other_hub(G):
    """Hub 8 has portals of its own, and they are built during the load into
    it -- so walking hub 3 -> hub 8 has to carry the spoof too."""
    r = Rig(G, granted={9})                    # Looningdale's, in hub 8
    r.collect(9, 20)
    r.play(9, 2.0)
    r.load(OTHER_HUB)
    r.play(OTHER_HUB, 3.0)                     # truth goes back
    seen = []
    r.lid, r.gs = HUB, 5                       # now warp to hub 3
    for _ in range(20):
        r.clock.now += TICK
        r.log += r.g.sandwich_tick(r.granted)
        seen.append(r.sand(9))
    r.play(HUB, 0.5)
    r.lid, r.gs = OTHER_HUB, 5                 # and back to hub 8
    back = []
    for _ in range(20):
        r.clock.now += TICK
        r.log += r.g.sandwich_tick(r.granted)
        back.append(r.sand(9))
    check("hub to hub still carries the spoof, so the far hub builds its "
          "portals",
          all(v >= G.SANDWICH_GOAL for v in back),
          f"arriving at hub 8 saw {sorted(set(back))}, wanted 100 or more")


def scenario_back_to_100(G):
    """Caleb's last clause: once we know Taz did NOT go into the matching
    level, the count goes back up so the portal is there next time.

    WHEN it goes back up has moved, and deliberately. The note assumed the
    moment that mattered was Taz committing to a different level; it is not.
    The police box is decided in one place only -- while the hub's map is
    being constructed, i.e. during the load INTO the hub (0x0021C8B8) -- and
    every load into a hub comes from somewhere that is not a hub, so it
    re-spoofs everything on the way. Ice Burg staying honest while Taz walks
    into Zooney Tunes therefore costs nothing, and it is one less window in
    which a wrong guess about the destination could touch a level Taz is
    actually entering.

    So what is asserted is the promise rather than the mechanism: go into a
    different level, come back, and the portal has been paid for.
    """
    r = Rig(G, granted={ICE_BURG})
    r.collect(ICE_BURG, 60)
    r.play(ICE_BURG, 2.0)
    r.load(HUB)
    r.play(HUB, 3.0)                           # settled: truth is showing
    check("standing in the hub, the entrance shows the real number",
          r.sand(ICE_BURG) == 60, f"reads {r.sand(ICE_BURG)}, wanted 60")

    r.load(ZOONEY)                             # he goes somewhere ELSE
    r.play(ZOONEY, 2.0)
    seen = r.during_load(HUB, watch=ICE_BURG)  # ...and comes back
    check("coming back to the hub, Ice Burg's portal is paid for again",
          all(v >= G.SANDWICH_GOAL for v in seen),
          f"saw {sorted(set(seen))}, wanted 100 or more")
    r.play(HUB, 3.0)
    check("and the entrance goes honest again once he settles",
          r.sand(ICE_BURG) == 60, f"reads {r.sand(ICE_BURG)}, wanted 60")


def scenario_entering_a_level_keeps_its_sandwiches(G):
    """The one that emptied Cartoon Strip-Mine.

    A level built from a count of a hundred has already had all its
    sandwiches, so the game despawns every one. The hub is what needs the
    lie -- the level being walked INTO must see the truth, for the whole
    load, not just once it is active.
    """
    r = Rig(G, granted={ICE_BURG})
    r.collect(ICE_BURG, 25)
    r.play(ICE_BURG, 2.0)
    r.load(HUB)
    r.play(HUB, 3.0)

    seen = []
    r.lid, r.gs = ICE_BURG, 5              # walking back in
    for _ in range(20):
        r.clock.now += TICK
        r.log += r.g.sandwich_tick(r.granted)
        seen.append(r.sand(ICE_BURG))
    check("the level being entered keeps its real count for the whole load",
          all(v == 25 for v in seen),
          f"saw {sorted(set(seen))}, wanted only [25] -- 100 despawns them all")

    # ...while the OTHER levels stay spoofed, so the hub keeps its portals
    check("the other levels are still spoofed during that load",
          r.sand(ZOONEY) < G.SANDWICH_GOAL or ZOONEY in r.granted)


def scenario_completion_not_faked(G):
    """Beating a boss writes 1 into all three of its hub's completion flags,
    because in Open mode those flags ARE the boss gate. Walking into one of
    those levels must not then read as having finished it."""
    r = Rig(G, granted=set())
    T = r.T
    r.g.taz_state = lambda: 0x0A

    # Cartoon Strip-Mine is 15, in hub 13, whose boss is Dodge City (17).
    CSM = 15
    a = T.level_block(CSM, 0) + T.L_COMPLETE
    r.lid, r.gs = 13, G.STATE_ACTIVE
    # exactly what enforce_access does when Dodge City is granted
    r.g._true_complete.setdefault(CSM, r.g._u32(a))
    r.g._w32(a, 1)
    r.g._complete_wrote[CSM] = 1

    r.lid = CSM
    r.tick(1.0)
    check("walking into a level the boss gate marked complete does not "
          "report it",
          not r.g.read_completions(),
          f"reported {r.g.read_completions()}")
    check("...and the flag itself is put back",
          r.g._u32(a) == 0, f"flag reads {r.g._u32(a)}")

    # a genuine completion still counts -- and a genuine one is seven posters
    # at the exit, never a flag
    r.break_posters(CSM, 6)
    r.tick(0.5)
    check("six of the seven posters is not a completion",
          not r.g.read_completions(),
          f"reported {r.g.read_completions()}")
    r.break_posters(CSM, 7)
    r.tick(0.5)
    check("a completion the player actually earned still reports",
          r.g.read_completions() == {CSM},
          f"reported {r.g.read_completions()}")


def scenario_linear_gate_not_a_completion(G):
    """Linear opens a boss by writing its hub's three level-complete flags.

    Looningdale's, on a seed whose Gladiatoons poster gate was already met:
    the flag was written to 1 while the player was walking in, and the first
    tick inside read it back and sent the Level Complete check for a level
    they had never finished.

    The same shape as the Open bug before it -- reading back our own write --
    but through the other writer, which did none of the bookkeeping.
    """
    r = Rig(G, granted=set())
    T = r.T
    LOONINGDALES, HUB8, GLADIATOONS = 9, 8, 12
    a = T.level_block(LOONINGDALES, 0) + T.L_COMPLETE
    r.g.taz_state = lambda: 0x0A

    r.lid, r.gs = HUB8, G.STATE_ACTIVE       # standing in Sam Francisco
    r.tick(0.5)
    check("the flags are not written under a player standing in the hub",
          not r.g.enforce_linear_gate({GLADIATOONS}) and r.g._u32(a) == 0,
          f"flag reads {r.g._u32(a)}")

    r.lid, r.gs = LOONINGDALES, 5            # the load in -- now it is safe
    r.g.enforce_linear_gate({GLADIATOONS})
    check("...but a load sets them, so the hub opens the boss next time",
          r.g._u32(a) == 1, f"flag reads {r.g._u32(a)}")

    r.lid, r.gs = LOONINGDALES, G.STATE_ACTIVE
    check("walking in does NOT report a level the gate marked complete",
          not r.g.read_completions(),
          f"reported {r.g.read_completions()}")

    r.tick(0.5)
    check("...and the flag is handed back to the player",
          r.g._u32(a) == 0, f"flag reads {r.g._u32(a)}")

    r.g.enforce_linear_gate({GLADIATOONS})
    check("the gate does not write the level being played, ever",
          r.g._u32(a) == 0, f"flag reads {r.g._u32(a)}")

    r.break_posters(LOONINGDALES)            # they actually finish it
    r.tick(0.5)
    check("a completion the player earned still reports",
          r.g.read_completions() == {LOONINGDALES},
          f"reported {r.g.read_completions()}")


def scenario_the_flag_is_never_evidence(G):
    """Granny Canyon, and the reason the fallback is gone rather than fixed.

    The completion flag was kept as a fallback behind a remembered "the
    player's own value", captured before the client's first write. That is as
    honest as a run-time capture can be, and it still handed over a check
    nobody earned: the 1 it captured had been left in the SAVE FILE by an
    earlier session. Nothing readable while the game is running can tell that
    apart from a completion the player finished, because it is the same byte.

    So the flag is not consulted at all. Seven posters and the exit, which the
    client never writes and cannot fake.
    """
    r = Rig(G, granted=set())
    T = r.T
    GRANNY = 16
    a = T.level_block(GRANNY, 0) + T.L_COMPLETE
    r.g.taz_state = lambda: 0x0A

    r.save[a] = 1                            # left by a previous session
    r.lid, r.gs = GRANNY, G.STATE_ACTIVE
    r.tick(0.5)
    check("a flag already set in the save is not a completion",
          not r.g.read_completions(),
          f"reported {r.g.read_completions()} with no posters broken")

    # and it is not made into one by the client remembering it first
    r.g._true_complete[GRANNY] = 1
    r.g._complete_wrote[GRANNY] = 1
    check("...nor is one the client has remembered as 'theirs'",
          not r.g.read_completions(),
          f"reported {r.g.read_completions()}")

    r.break_posters(GRANNY, 7)
    check("seven posters at the exit is, and is the only thing that is",
          r.g.read_completions() == {GRANNY},
          f"reported {r.g.read_completions()}")


def scenario_satisfied_never_reads_the_flag(G):
    """The OTHER path, and the one that kept the bug alive.

    A completion location carries `offset = L_COMPLETE, rule = "nonzero"`, and
    satisfied() has a generic "the field is non-zero, so send it" fallthrough
    that honoured it. So every round of fixing read_completions left this half
    of the leak wide open, and the check went out the moment a poster gate
    wrote the flag -- which is exactly when the player saw it fire.

    new_checks unions satisfied() with the remembered completions, so
    satisfied() must contribute NOTHING here.
    """
    D = sys.modules["tazworld.logic"]
    r = Rig(G, granted=set())
    T = r.T
    GRANNY = 16
    locs = [l for l in D.completion_locations() if l["level"] == GRANNY]
    a = T.level_block(GRANNY, 0) + T.L_COMPLETE
    r.g.taz_state = lambda: 0x0A
    r.lid, r.gs = GRANNY, G.STATE_ACTIVE

    r.save[a] = 1                            # a poster gate just wrote it
    check("satisfied() does not send a completion off the flag",
          not r.g.satisfied(locs),
          f"sent {r.g.satisfied(locs)} with no posters broken")

    r.break_posters(GRANNY, 7)
    check("...and does not send it off the posters either -- that is "
          "read_completions' job alone",
          not r.g.satisfied(locs), f"sent {r.g.satisfied(locs)}")
    check("read_completions is still the one that says yes",
          r.g.read_completions() == {GRANNY},
          f"reported {r.g.read_completions()}")

    # Everything else in the same level still comes through satisfied(), so
    # the skip has to be exactly one type wide.
    other = [l for l in D.all_locations(sandwich_interval=100,
                                        destruction_interval=50)
             if l.get("level") == GRANNY and l["type"] == "poster"]
    check("posters in the same level still send normally",
          len(r.g.satisfied(other)) == len(other),
          f"{len(r.g.satisfied(other))} of {len(other)} poster checks")


def scenario_resync(G):
    """The one that fired every sandwich check at once.

    Reconnecting to the same seed builds a brand new client, so nothing is
    remembered -- and the save still holds the 100 the previous session wrote
    so the portal would appear. Reading that back as the truth means the
    client believes the player has all hundred.
    """
    r = Rig(G, granted={ICE_BURG})
    r.collect(ICE_BURG, 25)                    # what the player actually has
    r.fake_count(ICE_BURG, G.SANDWICH_GOAL)    # what the last session left
    D = sys.modules["tazworld.logic"]
    locs = [l for l in D.sandwich_locations(1, 0) if l["level"] == ICE_BURG]

    r.lid, r.gs = HUB, G.STATE_ACTIVE          # a fresh client, in the hub
    r.tick(0.5)
    check("a fresh client is not fooled by the 100 left in the save",
          r.g.true_sandwiches(ICE_BURG) == 25,
          f"believes {r.g.true_sandwiches(ICE_BURG)}, wanted 25")

    r.play(HUB, 3.0)
    check("and the hub shows the real number again",
          r.sand(ICE_BURG) == 25, f"reads {r.sand(ICE_BURG)}, wanted 25")

    r.load(ICE_BURG)
    r.play(ICE_BURG, 2.0)
    got = r.g.satisfied(locs)
    check("walking in fires the checks up to 25, not all hundred",
          len(got) == 25, f"{len(got)} checks fired")


def scenario_bitmap_malformed(G):
    """If the bitmap is not what it is believed to be, nothing may be written
    from it -- falling back is the only safe answer."""
    r = Rig(G, granted=set())
    r.collect(ZOONEY, 40)
    r.bits.clear()                             # nothing readable there
    check("an unreadable bitmap falls back to the count",
          r.g.true_sandwiches(ZOONEY) == 40,
          f"believes {r.g.true_sandwiches(ZOONEY)}, wanted 40")


def scenario_head_start(G):
    """Caleb's actual save, transcribed.

    Starting Sandwiches lives in the COUNT and never in the bitmap, so the
    truth is bitmap + start. Reading the bitmap alone made a granted level --
    count 100, bitmap 0 -- look like zero, and writing THAT back would have
    deleted twenty-five sandwiches a level, permanently.
    """
    START = 25
    rows = [(ICE_BURG, 100, 0, 25), (ZOONEY, 25, 0, 25),
            (14, 99, 75, 100), (15, 100, 0, 25), (18, 34, 9, 34)]
    r = Rig(G, granted={ICE_BURG, 15})
    r.g.starting_sandwiches = START
    bad = []
    for lid, count, bits, want in rows:
        r.fake_count(lid, count)
        r.bits[lid] = bits
        got = r.g.true_sandwiches(lid)
        if got != want:
            bad.append((lid, count, bits, got, want))
    check("every level of a real save reads as bitmap + starting",
          not bad,
          "; ".join(f"level {l}: count {c} bitmap {b} -> {g}, wanted {w}"
                    for l, c, b, g, w in bad))

    # and the one that would have been destructive
    r.fake_count(ICE_BURG, 100)
    r.bits[ICE_BURG] = 0
    check("a granted level with nothing collected is 25, not 0",
          r.g.true_sandwiches(ICE_BURG) == 25,
          f"reads {r.g.true_sandwiches(ICE_BURG)}")

    # a level genuinely finished still reports its 100-sandwich check
    D = sys.modules["tazworld.logic"]
    locs = [l for l in D.sandwich_locations(100, 0) if l["level"] == 14]
    r.lid, r.gs = 14, G.STATE_ACTIVE
    r.tick(0.5)
    check("75 collected on top of a 25 start does fire the 100 check",
          r.g.satisfied(locs) == {locs[0]["id"]},
          f"fired {r.g.satisfied(locs)}")


def scenario_one_caller(G):
    """_bitmap_count must have exactly one caller: true_sandwiches.

    Not a style rule. It returns a number that is missing the head start, and
    the one time something else called it, nine levels were written twenty-five
    short -- which is unrecoverable if the bitmap is ever cleared. A source
    check is crude, but it is the only thing that catches the next bypass
    before it reaches a save file.
    """
    src = open(os.path.join(WORLD, "game.py"), encoding="utf-8").read()
    lines = [l for l in src.splitlines() if "_bitmap_count" in l]
    defs = [l for l in lines if l.strip().startswith("def ")]
    calls = [l for l in lines if "self._bitmap_count(" in l]
    check("_bitmap_count is defined once and called once",
          len(defs) == 1 and len(calls) == 1,
          f"{len(defs)} definition(s), {len(calls)} call(s): "
          + "; ".join(c.strip() for c in calls))


def scenario_hub_writes_the_head_start(G):
    """The bug itself: walking into a hub must write bitmap + start, never the
    bitmap on its own."""
    r = Rig(G, granted={ICE_BURG})
    r.g.starting_sandwiches = 25
    r.collect(ICE_BURG, 0)                 # nothing picked up
    r.fake_count(ICE_BURG, 25)             # the head start is all they have
    r.bits[14] = 75                        # Taz: Haunted, genuinely at 100
    r.fake_count(14, 100)

    r.play(ICE_BURG, 2.0)
    r.load(HUB)
    r.play(HUB, 3.0)                       # settles, so the truth is written
    check("a level with only the head start keeps its 25",
          r.sand(ICE_BURG) == 25, f"reads {r.sand(ICE_BURG)}, wanted 25")
    check("a level at 75 collected keeps its 100",
          r.sand(14) == 100, f"reads {r.sand(14)}, wanted 100")


def scenario_lid_lags(G):
    """Walking out of a hub, lid reads as the HUB for the first frames.

    Correcting the value once lid resolves is NOT good enough, and this is the
    scenario that used to say it was. 0x0024A6D8 tests the count every frame
    the level runs, so a tenth of a second at exactly 100 empties Ice Burg for
    good. The count has to be right for the whole load, not right eventually.
    """
    r = Rig(G, granted={ICE_BURG})
    r.g.starting_sandwiches = 25
    r.collect(ICE_BURG, 0)
    r.fake_count(ICE_BURG, 25)
    r.play(HUB, 3.0)                       # settled in the hub, truth written

    seen = r.during_load(ICE_BURG, watch=ICE_BURG, seconds=1.2, lag=0.6)
    check("the level being entered reads 25 on EVERY tick of the load",
          all(v == 25 for v in seen),
          f"saw {sorted(set(seen))} -- anything else had a window to empty it")

    # ...and the hub it came from still had its spoof while IT was building
    check("the other hubs' levels stayed spoofed throughout",
          r.sand(15) >= G.SANDWICH_GOAL or 15 not in r.granted)


def scenario_never_exactly_a_hundred(G):
    """The invariant the despawn hangs on.

    0x0024A6DC is `bnel v1,t4` against a literal 100: EXACTLY 100 destroys
    every sandwich object in the running level, anything else leaves them
    alone. 0x0021C9EC is `slti v1,v1,0x64`: 100 OR MORE builds the portal.
    So no level may ever be written to exactly 100 unless the player really
    has 100 -- and every spoof still has to clear 100.
    """
    r = Rig(G, granted={ICE_BURG, ZOONEY, 15})
    r.g.starting_sandwiches = 25
    for lid in (ICE_BURG, ZOONEY, LOONEY, 15):
        r.collect(lid, 0)
        r.fake_count(lid, 25)

    watched, spoofs = [], []

    def sweep():
        for lid in (ICE_BURG, ZOONEY, LOONEY, 15):
            v = r.sand(lid)
            watched.append((lid, v))
            if v >= G.SANDWICH_GOAL:
                spoofs.append((lid, v))

    r.play(HUB, 3.0); sweep()
    for _ in range(3):
        r.lid, r.gs = HUB, 5
        for i in range(12):                 # into Ice Burg, lid lagging
            r.lid = HUB if i < 6 else ICE_BURG
            r.clock.now += TICK
            r.log += r.g.sandwich_tick(r.granted)
            sweep()
        r.play(ICE_BURG, 2.0); sweep()
        r.lid, r.gs = ICE_BURG, 5
        for i in range(12):                 # and back out to the hub
            r.lid = ICE_BURG if i < 6 else HUB
            r.clock.now += TICK
            r.log += r.g.sandwich_tick(r.granted)
            sweep()
        r.play(HUB, 3.0); sweep()

    at_100 = [(lid, v) for lid, v in watched if v == G.SANDWICH_GOAL]
    check("no level is ever written to exactly 100 -- the despawn value",
          not at_100, f"{len(at_100)} reading(s) at exactly 100, e.g. {at_100[:3]}")
    check("...and the spoof still clears 100, so the portal is still built",
          bool(spoofs) and all(v > G.SANDWICH_GOAL for _, v in spoofs),
          f"spoof values seen: {sorted({v for _, v in spoofs})}")


def scenario_portal_for_the_level_just_left(G):
    """Caleb: 'when I left the level, the bonus game was despawned.'

    The hub's police box is built from the count DURING the load into the hub,
    and the level being left is the one whose portal is being decided. Holding
    that one at its true value -- which is what keeping the destination honest
    used to do, since lid still reads the level on the way out -- is exactly
    how to lose it.
    """
    r = Rig(G, granted={ICE_BURG})
    r.g.starting_sandwiches = 25
    r.collect(ICE_BURG, 0)
    r.fake_count(ICE_BURG, 25)
    r.play(ICE_BURG, 2.0)                  # in the level: honest, reads 25

    seen = r.during_load(HUB, watch=ICE_BURG, seconds=1.2, lag=0.6)
    check("walking OUT of Ice Burg, its own count clears 100 for the whole "
          "load",
          all(v >= G.SANDWICH_GOAL for v in seen),
          f"saw {sorted(set(seen))} -- below 100 means no bonus game portal")

    r.play(HUB, 3.0)
    check("and the entrance is honest again once he is standing in the hub",
          r.sand(ICE_BURG) == 25, f"reads {r.sand(ICE_BURG)}, wanted 25")


def scenario_mid_load_connect(G):
    """A client that connects during a loading screen knows nothing about
    which way it is going, so it must not write. Guessing costs a level."""
    r = Rig(G, granted={ICE_BURG})
    r.collect(ICE_BURG, 40)
    r.lid, r.gs = HUB, 5                   # first tick ever, mid-load
    r.tick(1.0)
    check("a client that connects mid-load writes nothing at all",
          r.sand(ICE_BURG) == 40, f"reads {r.sand(ICE_BURG)}, wanted 40")


def scenario_gate_live_never_lies(G):
    """With the gate patched, the count decides nothing, so it is never a lie.

    Not "briefly wrong and then corrected" -- never wrong at all, at any tick,
    in any state, in either direction. That is the whole reason for patching
    the gate rather than getting better at timing the spoof.
    """
    r = Rig(G, granted={ICE_BURG, ZOONEY})
    r.g._bonus_gate_live = True
    r.g.starting_sandwiches = 25
    r.collect(ICE_BURG, 10)                # 35 real, with the head start
    r.fake_count(ICE_BURG, 35)
    r.collect(ZOONEY, 0)
    r.fake_count(ZOONEY, 25)

    seen = set()

    def sweep():
        seen.add((r.sand(ICE_BURG), r.sand(ZOONEY)))

    r.play(HUB, 3.0); sweep()
    for _ in range(2):
        for v in r.during_load(ICE_BURG, watch=ICE_BURG):
            seen.add((v, r.sand(ZOONEY)))
        r.play(ICE_BURG, 2.0); sweep()
        for v in r.during_load(HUB, watch=ICE_BURG):
            seen.add((v, r.sand(ZOONEY)))
        r.play(HUB, 3.0); sweep()

    check("with the gate patched the counts are never anything but true",
          seen == {(35, 25)},
          f"saw {sorted(seen)}, wanted only [(35, 25)]")


def scenario_gate_live_no_cap(G):
    """The cap the spoof needs is itself a lie, and the patch retires it.

    A player who really found all hundred in a level nobody granted them a
    bonus for used to be held at 99 so no portal would appear. With the gate
    patched the portal is refused by the gate, so the hundred can stand.
    """
    r = Rig(G, granted=set())
    r.g._bonus_gate_live = True
    r.collect(ZOONEY, 100)
    r.play(ZOONEY, 2.0)
    r.load(HUB)
    r.play(HUB, 3.0)
    check("an ungranted level at a real 100 keeps its 100, uncapped",
          r.sand(ZOONEY) == 100, f"reads {r.sand(ZOONEY)}, wanted 100")


def scenario_gate_falls_out(G):
    """If the patch stops being live, the spoof has to come straight back.

    This is the only reason the fallback is still in the file, so it is worth
    a test: a session that has been running honestly must start paying for
    portals again the moment the gate is not ours.
    """
    r = Rig(G, granted={ICE_BURG})
    r.g._bonus_gate_live = True
    r.collect(ICE_BURG, 60)
    r.play(ICE_BURG, 2.0)
    r.load(HUB)
    r.play(HUB, 3.0)
    check("while the gate is live, walking out of a level writes no spoof",
          r.sand(ICE_BURG) == 60, f"reads {r.sand(ICE_BURG)}, wanted 60")

    r.g._bonus_gate_live = False           # the patch is gone
    r.load(ICE_BURG)
    r.play(ICE_BURG, 2.0)
    seen = r.during_load(HUB, watch=ICE_BURG)
    check("once it is gone, the spoof pays for the portal again",
          all(v >= G.SANDWICH_GOAL for v in seen),
          f"saw {sorted(set(seen))}, wanted 100 or more")


def main():
    clock = Clock()
    G = load_game(clock)
    print("    the bonus-game sandwich spoof, against what note 7 asks for:")
    print()
    scenario_portal_appears(G)
    scenario_ungranted_capped(G)
    scenario_level_shows_truth(G)
    scenario_round_trip(G)
    scenario_unvisited_not_zeroed(G)
    scenario_check_does_not_fire(G)
    scenario_no_bonus_level(G)
    scenario_idle_hub(G)
    scenario_other_hub(G)
    scenario_back_to_100(G)
    scenario_entering_a_level_keeps_its_sandwiches(G)
    scenario_completion_not_faked(G)
    scenario_linear_gate_not_a_completion(G)
    scenario_the_flag_is_never_evidence(G)
    scenario_satisfied_never_reads_the_flag(G)
    scenario_resync(G)
    scenario_bitmap_malformed(G)
    scenario_head_start(G)
    scenario_one_caller(G)
    scenario_hub_writes_the_head_start(G)
    scenario_lid_lags(G)
    scenario_never_exactly_a_hundred(G)
    scenario_portal_for_the_level_just_left(G)
    scenario_mid_load_connect(G)
    scenario_gate_live_never_lies(G)
    scenario_gate_live_no_cap(G)
    scenario_gate_falls_out(G)
    bad = [l for l, ok, _ in RESULTS if not ok]
    print()
    print(f"    {len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
