#!/usr/bin/env python3
"""The catcher checks, tested without playing the game.

    py -3.13 taz_catcher_test.py sim         the offline test suite
    py -3.13 taz_catcher_test.py live        watch a real level, read-only
    py -3.13 taz_catcher_test.py despawn     list the keepers you can see
    py -3.13 taz_catcher_test.py despawn -w 2   and send catcher 2 away

For the enemy lists themselves use taz_enemylist.py, which walks them
properly and can assert the whole structure against a dump with no emulator
running. The `roster` verb that used to live here is superseded by it.

`sim` drives worlds/tazwanted/game.py's CatcherJudge -- the SHIPPED code, not
a copy of it -- through scripted timelines. Each case is a way a check has
actually gone wrong or plausibly could. It needs nothing but Python: no
PCSX2, no emulator, no save file.

It ALSO runs the shipped `catchers()` against ee_dump.bin, which is the part
this suite spent nine sessions without. Every judge case feeds hand-built
keeper dicts, so `catchers()` could read the enemy list completely wrong and
the suite stayed green -- and it did, and it was. If you add a case, ask
which of those two halves it actually exercises.

`live` connects to PINE and prints what the judge concludes each tick, with
its reasoning, while you play. It never writes -- including no despawns -- so
it is safe to run over a real seed.

`despawn` is the one verb that writes, and it exists because the despawn is
the only part of this that a simulation cannot settle: writing ANIM_DESPAWN
(0xE) to a keeper's animation field is what the game does to send one away,
but whether it accepts the write from outside is a question only the running
game can answer. Run it, watch, and say what happened.

Close the AP client before any of the live verbs -- PINE takes one connection
at a time.
"""

import argparse
import importlib.util
import json
import os
import struct
import sys
import time

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")


# ---------------------------------------------------------------- loading

def load_game():
    """Import game.py without dragging in the rest of the world.

    It is a package module ("from . import logic as D"), so it is loaded under
    a synthetic package whose only other member is logic.py. That keeps this
    honest -- the test exercises the file that ships -- without needing
    Archipelago on the path.
    """
    import types
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


def load_posts(G):
    path = os.path.join(WORLD, "data", "taz_catchers.json")
    raw = json.load(open(path, encoding="utf-8"))
    return {int(k): [c["pos"] for c in v.get("catchers", [])]
            for k, v in raw.items()}


def load_radii(G):
    """The per-level radius recorded beside the posts. start_catchers used to
    drop this on the floor, so nothing in the client ever read it."""
    path = os.path.join(WORLD, "data", "taz_catchers.json")
    raw = json.load(open(path, encoding="utf-8"))
    return {int(k): v["radius"] for k, v in raw.items() if v.get("radius")}


# ---------------------------------------------------------------- the rig

MINE = 15            # Cartoon Strip-Mine, where the wrong check was sent
DRESSED = 0x9        # its costume, the Adventurer
NONE = 0xFF
# Cartoon Strip-Mine has four keepers, so 0..3 are the only real indices.
# Asking for a fifth is how a test starts testing its own typo.


class Rig:
    """A scripted level. Time is a number this owns, not the wall clock.

    Every poll is passed an explicit `now`, so a case that spans twenty
    seconds runs instantly and always the same way. Nothing here sleeps.

    A keeper here behaves the way a live capture says a real one does, which
    is not what the disassembly suggested:

        streams in ------> dormant group          )  totals preserved
        Taz approaches --> active group           )  by every move
        the takedown ----> state 6, hit latch set
        ~2.9s later -----> state 14, E_ALIVE clears
        ~0.75s later ----> FREED. Out of both lists. The total DROPS.

    The last step is the correction. `beat()` walks the whole sequence; the
    individual steps are there for cases that need to stop part way.

    Getting this wrong is how the old suite passed for nine sessions while
    the client saw one keeper in six: its `leave()` synthesised a departure
    the game does not produce, so the fake and the judge agreed about a world
    neither had checked. If a step here stops matching a capture, this class
    is what to fix first.
    """

    def __init__(self, G, level=MINE, costume=DRESSED, total=20):
        self.G = G
        self.judge = G.CatcherJudge(posts=load_posts(G),
                                    level_radius=load_radii(G))
        self.level = level
        self.posts = self.judge.posts[level]
        self.keepers = {}        # ptr -> dict
        self.costume = costume
        self.state = 0
        self.total = total
        self.complete = True     # what catchers() reports as walk_ok
        self.t = 0.0
        self.fired = []
        self.lost = []           # the judge's own report of what got away
        self.blind = []          # ...and of what the player needs telling

    # -- the world -------------------------------------------------
    def spawn(self, idx, ptr=None, offset=(0.0, 0.0, 0.0), alive=True):
        """A keeper at post `idx`. `alive=False` is one already beaten before
        the client was looking, or switched off by the level script."""
        ptr = ptr if ptr is not None else 0x01000000 + idx * 0x100
        p = self.posts[idx]
        self.keepers[ptr] = {
            "ptr": ptr,
            "pos": tuple(p[i] + offset[i] for i in range(3)),
            # The leash centre the game keeps on the object at E_SUB+0x30.
            # Write-once in the constructor, so it does NOT follow `move`.
            "home": tuple(p),
            "name": "enemy keeper%02d" % (idx + 1),
            "active": True, "anim": 2, "defeated": False, "alive": alive}
        return ptr

    def move(self, ptr, pos):
        self.keepers[ptr]["pos"] = tuple(pos)

    # -- the three steps of a kill
    def hit(self, ptr):
        """The takedown lands: state 6, hit latch set.

        Reached ONLY from a hit -- all five guard blocks that lead to state 6
        require the latch -- so this is the thing that separates a kill from
        a distance cull further down the line.
        """
        k = self.keepers[ptr]
        k["anim"] = self.G.STATE_DEFEATED
        k["defeated"] = True

    def downed(self, ptr):
        """The 6 -> 14 handoff: E_ALIVE clears one instruction after the
        state is set (0x00163E84 then 0x00163E8C)."""
        k = self.keepers[ptr]
        k["anim"] = self.G.STATE_DESPAWN
        k["alive"] = False

    def freed(self, ptr):
        """The object is destroyed. Out of both lists, total drops."""
        self.keepers.pop(ptr, None)
        self.total -= 1

    def beat(self, ptr):
        """A whole kill at the recorded pace, polling throughout."""
        self.hit(ptr)
        self.tick(2.9)
        self.downed(ptr)
        self.tick(0.7)
        self.freed(ptr)
        self.tick(0.2)

    def getup(self, ptr):
        """The OTHER exit from state 6: back to suspicious, latch cleared,
        E_ALIVE untouched. A ranged hit, a chili pepper, a burp."""
        k = self.keepers[ptr]
        k["defeated"] = False
        k["anim"] = 3

    # -- the things that are not kills
    def cull(self, ptr):
        """Taz walked out of range. The idle handler sends the keeper to
        state 14 as well (0x00163634) and it is REPARENTED to the dormant
        group -- alive, total unchanged. This is why state 14 corroborates
        nothing and state 6 does."""
        k = self.keepers[ptr]
        k["anim"] = self.G.STATE_DESPAWN
        k["active"] = False

    def wake(self, ptr):
        k = self.keepers[ptr]
        k["active"] = True
        k["anim"] = 2

    def deactivate(self, ptr):
        """The TRIGGER script's DEACTIVATE, at 0x00274E44. Clears E_ALIVE and
        touches NOTHING else -- two instructions straight to the epilogue."""
        self.keepers[ptr]["alive"] = False

    def unload(self):
        """Leaving the level. EVERY enemy goes at once."""
        self.keepers.clear()
        self.total = 0

    def torn(self, on=True):
        """A list walk that came up short of its own count field."""
        self.complete = not on

    def unread(self, ptr, field="alive"):
        """A field that could not be read this tick. catchers() reports None
        rather than a plausible zero, and the judge must conclude nothing."""
        self.keepers[ptr][field] = None

    def strip(self):
        """The Costume Strip Trap, exactly as grant_effect does it."""
        self.judge.note_costume_strip(self.t)
        self.costume = NONE

    # -- time ------------------------------------------------------
    def tick(self, seconds=0.1):
        """Advance and poll, the way the client's 0.1s loop does."""
        steps = max(1, int(round(seconds / 0.1)))
        for _ in range(steps):
            self.t += 0.1
            got = self.judge.poll(self.level, list(self.keepers.values()),
                                  self.costume, self.state, self.total,
                                  complete=self.complete, now=self.t)
            self.fired.extend(got)
            # `lost` and `blind` are per-poll, so the client reports each one
            # once. A test spanning several polls has to accumulate them the
            # same way `fired` is accumulated, or it reads whatever the LAST
            # poll happened to say -- which is nothing.
            self.lost.extend(self.judge.lost)
            self.blind.extend(self.judge.blind)
        return self.fired

    def enter(self, level):
        self.level = level
        self.posts = self.judge.posts.get(level, [])


# ---------------------------------------------------------------- cases

CASES = []


def case(name, why):
    def wrap(fn):
        CASES.append((name, why, fn))
        return fn
    return wrap


# -- a kill, whole and in pieces --------------------------------------

@case("a whole takedown is credited",
      "state 6, the bit clears, the object is freed -- the recorded shape")
def t_real(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(2.0)
    r.beat(p)
    return r.fired, [2]


@case("...exactly once, though two paths could book it",
      "the bit clearing and the free are the same kill seen twice")
def t_real_once(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(2.0)
    r.beat(p)
    r.tick(5.0)
    return (r.fired, r.blind), ([2], [])


@case("the bit clearing credits it before the object is freed",
      "path 1, the early signal -- no need to wait out the shrink")
def t_path1(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.hit(p)
    r.tick(2.9)
    before = list(r.fired)
    r.downed(p)
    r.tick(0.1)
    return (before, r.fired), ([], [2])


@case("the free credits it even if the bit was never seen clear",
      "path 2 -- the client stalled through the whole 0.75s window")
def t_path2(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.hit(p)
    r.tick(0.5)
    r.freed(p)              # never observed in state 14, never saw alive=0
    r.tick(0.2)
    return r.fired, [2]


@case("a kill seen ONLY as a disappearance credits nothing",
      "no state 6 and no cleared bit is not corroboration, it is a guess")
def t_gone_uncorroborated(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.freed(p)
    r.tick(1.0)
    return r.fired, []


@case("all four in a level, beaten one after another",
      "the whole level, which is what the backstop used to be needed for")
def t_all_four(G):
    r = Rig(G)
    ps = [r.spawn(i) for i in range(4)]
    r.tick(1.0)
    for p in ps:
        r.beat(p)
    return sorted(r.fired), [0, 1, 2, 3]


# -- the things that are not kills ------------------------------------

@case("a knockdown is not a takedown",
      "state 6 with the other exit: it gets back up and is never freed")
def t_knockdown(G):
    r = Rig(G)
    p = r.spawn(1)
    r.tick(1.0)
    r.hit(p)
    r.tick(2.0)
    r.getup(p)
    r.tick(5.0)
    return r.fired, []


@case("the hit latch alone credits nothing, however long it is held",
      "E_DEFEATED is a hit/stun latch; four setters, ten clearers")
def t_latch_is_not_proof(G):
    r = Rig(G)
    p = r.spawn(0)
    r.keepers[p]["defeated"] = True
    r.tick(30.0)
    return r.fired, []


@case("a keeper knocked down, back up, then lost to an unload is refused",
      "corroboration has to expire when the game says it got up, or a "
      "knockdown plus a level exit books a check for a keeper still standing")
def t_knockdown_then_unload(G):
    r = Rig(G)
    p = r.spawn(1)
    r.tick(1.0)
    r.hit(p)
    r.tick(1.0)
    r.getup(p)
    r.tick(1.0)
    r.unload()
    r.tick(1.0)
    return r.fired, []


@case("...and one knocked down, back up, then genuinely beaten IS credited",
      "expiring the corroboration must not make a real second kill invisible")
def t_knockdown_then_kill(G):
    r = Rig(G)
    p = r.spawn(1)
    r.tick(1.0)
    r.hit(p)
    r.tick(1.0)
    r.getup(p)
    r.tick(1.0)
    r.beat(p)
    return r.fired, [1]


@case("a distance cull is not a takedown",
      "it reparents to dormant -- state 14 and all -- and stays alive")
def t_cull(G):
    r = Rig(G)
    p = r.spawn(3)
    r.tick(1.0)
    r.cull(p)
    r.tick(10.0)
    r.wake(p)
    r.tick(1.0)
    return r.fired, []


@case("a keeper culled and THEN killed is still credited",
      "the commonest real sequence: walk away, come back, beat it")
def t_cull_then_kill(G):
    r = Rig(G)
    p = r.spawn(3)
    r.tick(1.0)
    r.cull(p)
    r.tick(5.0)
    r.wake(p)
    r.tick(1.0)
    r.beat(p)
    return r.fired, [3]


@case("a culled keeper lost to a level unload credits nothing",
      "state 14 from the cull would corroborate if 14 were trusted -- "
      "it is not, and everything going at once is caught as well")
def t_cull_then_unload(G):
    r = Rig(G)
    a, b = r.spawn(0), r.spawn(1)
    r.tick(1.0)
    r.cull(a)
    r.cull(b)
    r.tick(2.0)
    r.unload()
    r.tick(2.0)
    return r.fired, []


@case("a level unload after a real kill still credits only the kill",
      "the free comes 3.7s after the takedown; the exit takes longer")
def t_kill_then_unload(G):
    r = Rig(G)
    a, b = r.spawn(0), r.spawn(1)
    r.tick(1.0)
    r.beat(a)
    r.unload()
    r.tick(2.0)
    return r.fired, [0]


@case("a torn read is not four keepers leaving at once",
      "a walk short of its own count is a bad sample, not a departure")
def t_torn(G):
    r = Rig(G)
    ps = [r.spawn(i) for i in range(4)]
    r.tick(1.0)
    for p in ps:
        r.hit(p)
    r.tick(0.5)
    r.torn(True)
    for p in list(ps):
        r.keepers.pop(p)      # the walk came up short; they never left
    r.tick(2.0)
    return r.fired, []


@case("...and the real departure after it is still credited",
      "skipping a bad sample must not mean skipping the good one")
def t_torn_then_real(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.hit(p)
    r.tick(0.5)
    r.torn(True)
    r.keepers.pop(p)
    r.tick(1.0)
    r.torn(False)
    r.tick(0.2)
    return r.fired, [2]


@case("our own despawn is not a takedown",
      "DESPAWN_RECIPE writes the vanish directly; no state 6, bit untouched")
def t_our_despawn(G):
    r = Rig(G)
    p = r.spawn(0)
    r.tick(1.0)
    r.judge.note_despawn(p)
    r.cull(p)
    r.tick(1.0)
    r.freed(p)
    r.tick(2.0)
    return r.fired, []


# -- what the judge deliberately refuses ------------------------------

@case("a keeper already beaten when first seen is refused",
      "no transition was observed, so there is nothing to credit")
def t_already_beaten(G):
    r = Rig(G)
    r.spawn(2, alive=False)
    r.tick(5.0)
    return r.fired, []


@case("...and the player is told why",
      "a silent refusal is the failure mode this whole class is against")
def t_already_beaten_is_loud(G):
    r = Rig(G)
    r.spawn(2, alive=False)
    r.tick(1.0)
    return any("already beaten" in b for b in r.blind), True


@case("an unreadable defeat bit concludes nothing",
      "a torn read reported as clear would credit a check nobody earned")
def t_unreadable(G):
    r = Rig(G)
    p = r.spawn(1)
    r.tick(1.0)
    r.unread(p)
    r.tick(5.0)
    return r.fired, []


@case("...and the real takedown after it is still credited",
      "concluding nothing must not mean forgetting the keeper")
def t_unreadable_then_beaten(G):
    r = Rig(G)
    p = r.spawn(1)
    r.tick(1.0)
    r.unread(p)
    r.tick(2.0)
    r.keepers[p]["alive"] = True
    r.tick(0.2)
    r.beat(p)
    return r.fired, [1]


@case("a bit cleared with no state 6 ever seen still credits",
      "a stalled client must not cost a check")
def t_deactivate_credits(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.deactivate(p)
    r.tick(0.2)
    return r.fired, [2]


@case("...but says so, because that is the shape of a script DEACTIVATE",
      "0x00274E44 clears E_ALIVE and touches nothing else")
def t_deactivate_is_loud(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.deactivate(p)
    r.tick(0.2)
    return any("without the keeper ever being seen defeated" in b
               for b in r.blind), True


@case("a normal takedown says nothing of the sort",
      "the warning above has to be rare or it is noise, and noise is ignored")
def t_normal_is_quiet(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.beat(p)
    return r.blind, []


# -- identity ---------------------------------------------------------

@case("a takedown away from the post is still the right catcher",
      "keepers chase Taz, so the kill happens wherever he led it")
def t_chased(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    # Dragged most of the way to catcher 0 before it goes down. The leash
    # centre does not follow, which is exactly why identity reads it.
    r.move(p, r.posts[0])
    r.tick(1.0)
    r.beat(p)
    return r.fired, [2]


@case("identity is taken from the leash centre, not the sighting",
      "a client attaching mid-chase must still get the right catcher")
def t_identity_midchase(G):
    r = Rig(G)
    p = r.spawn(3)
    r.move(p, r.posts[1])        # already chasing when we first look
    r.tick(1.0)
    r.beat(p)
    return r.fired, [3]


@case("a keeper with no readable position is reported, not dropped",
      "it used to reach debug logging only, which looked like nothing")
def t_no_post(G):
    r = Rig(G)
    p = r.spawn(2)
    r.keepers[p]["home"] = None
    r.keepers[p]["pos"] = None
    r.tick(1.0)
    r.beat(p)
    return (r.fired, len(r.lost)), ([], 1)


@case("two keepers beaten give two different checks",
      "the commonest thing a player does, and it must not collapse to one")
def t_two(G):
    r = Rig(G)
    a, b = r.spawn(0), r.spawn(3)
    r.tick(1.0)
    r.beat(a)
    r.beat(b)
    return sorted(r.fired), [0, 3]


@case("three keepers beaten together, Bank of Samerica style",
      "his capture: task force 04, 05 and 06, close enough to co-activate")
def t_three_together(G):
    r = Rig(G)
    ps = [r.spawn(i) for i in (0, 1, 2)]
    r.tick(1.0)
    for p in ps:
        r.hit(p)
    r.tick(2.9)
    for p in ps:
        r.downed(p)
    r.tick(0.7)
    for p in ps:
        r.freed(p)
    r.tick(0.5)
    return sorted(r.fired), [0, 1, 2]


@case("beating the same catcher's keeper twice sends one check",
      "the dedup, and it must stay")
def t_twice(G):
    r = Rig(G)
    p = r.spawn(1)
    r.tick(1.0)
    r.beat(p)
    q = r.spawn(1, ptr=0x03000000)
    r.tick(1.0)
    r.beat(q)
    return r.fired, [1]


@case("...and says so, because it also means a wrong post match",
      "a real catcher dying into somebody else's ticked box is silent")
def t_twice_is_loud(G):
    r = Rig(G)
    p = r.spawn(1)
    r.tick(1.0)
    r.beat(p)
    q = r.spawn(1, ptr=0x03000000)
    r.tick(1.0)
    r.beat(q)
    return any("already checked" in b for b in r.blind), True


@case("a keeper that is not a catcher is ignored",
      "Yosemite Zoo has ONE post and keepers 5000 units from it")
def t_not_a_catcher(G):
    r = Rig(G)
    real = r.spawn(0)
    other = r.spawn(1, ptr=0x05000000)
    r.keepers[other]["home"] = tuple(c + 50000.0 for c in r.posts[1])
    r.tick(1.0)
    return (sorted(r.judge.post_of.values()),
            other in r.judge.not_a_catcher), ([0], True)


@case("...and beating one credits nothing",
      "without the cutoff it filed under the nearest post and sent a check")
def t_not_a_catcher_beaten(G):
    r = Rig(G)
    other = r.spawn(1, ptr=0x05000000)
    r.keepers[other]["home"] = tuple(c + 50000.0 for c in r.posts[1])
    r.tick(1.0)
    r.beat(other)
    return r.fired, []


@case("...and says nothing about it, however many there are",
      "one line per keeper per poll is what flooded the client")
def t_not_a_catcher_is_quiet(G):
    r = Rig(G)
    r.spawn(0)
    for n in range(4):
        q = r.spawn(1, ptr=0x05000000 + n * 0x100)
        r.keepers[q]["home"] = tuple(c + 50000.0 + n for c in r.posts[1])
    r.tick(30.0)
    return r.blind, []


@case("a level where NOTHING matches is reported, once",
      "seeing keepers and matching none of them means the JSON is wrong")
def t_nothing_matches(G):
    r = Rig(G)
    for n in range(3):
        q = r.spawn(0, ptr=0x06000000 + n * 0x100)
        r.keepers[q]["home"] = tuple(c + 50000.0 + n for c in r.posts[0])
    r.tick(30.0)
    return (len(r.blind), "wrong for this level" in (r.blind or [""])[0]), \
        (1, True)


@case("...but not while the level is still settling",
      "five Tazland keepers were judged against Yosemite Zoo's single post "
      "because LEVEL_ID and the enemy lists disagree across a transition")
def t_nothing_matches_waits(G):
    r = Rig(G)
    for n in range(3):
        q = r.spawn(0, ptr=0x06000000 + n * 0x100)
        r.keepers[q]["home"] = tuple(c + 50000.0 + n for c in r.posts[0])
    r.tick(G.SETTLE_SECS - 0.5)
    early = list(r.blind)
    r.tick(1.0)
    return (early, len(r.blind)), ([], 1)


@case("a keeper just inside the level radius is still a catcher",
      "the recorded posts are 0-102 units out; the radius is 1220-1500")
def t_edge_of_radius(G):
    r = Rig(G)
    rad = load_radii(G)[MINE]
    p = r.spawn(2)
    r.keepers[p]["home"] = (r.posts[2][0] + rad * 0.9,
                            r.posts[2][1], r.posts[2][2])
    r.tick(1.0)
    r.beat(p)
    return r.fired, [2]


# -- levels and the client's last word --------------------------------

@case("a credit survives leaving and re-entering the level",
      "_reset_level deliberately does not clear `credited`")
def t_level_change(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.beat(p)
    r.unload()
    r.enter(5)
    r.tick(1.0)
    r.enter(MINE)
    q = r.spawn(2)
    r.tick(1.0)
    r.beat(q)
    return r.fired, [2]


@case("a refused check can be earned again",
      "catcher_refused stops an out-of-logic check; uncredit lets it retry")
def t_uncredit(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.beat(p)
    r.judge.uncredit(MINE, 2)     # the client refused it
    q = r.spawn(2, ptr=0x04000000)
    r.tick(1.0)
    r.beat(q)
    return r.fired, [2, 2]


@case("a keeper's pointer being reused does not inherit its state",
      "the allocator reuses addresses; _forget is why that is safe")
def t_ptr_reuse(G):
    r = Rig(G)
    p = r.spawn(0)
    r.tick(1.0)
    r.hit(p)
    r.tick(0.5)
    r.freed(p)
    r.tick(0.5)               # credited via path 2
    q = r.spawn(1, ptr=p)     # same address, a different catcher
    r.tick(1.0)
    return (r.fired, r.judge.post_of.get(q)), ([0], 1)


@case("...and the keeper at the reused pointer can still be credited",
      "checking the index alone would miss credited_ptrs blocking the kill")
def t_ptr_reuse_credits(G):
    r = Rig(G)
    p = r.spawn(0)
    r.tick(1.0)
    r.beat(p)
    q = r.spawn(1, ptr=p)     # same address, catcher 1 this time
    r.tick(1.0)
    r.beat(q)
    return sorted(r.fired), [0, 1]


# -- the retired conditions must genuinely be retired ------------------

@case("the costume is not consulted",
      "condition 4 is gone; a takedown with no costume loss still sends")
def t_costume_ignored(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.beat(p)
    r.tick(10.0)               # costume never comes off
    return r.fired, [2]


@case("a costume loss on its own credits nothing",
      "an enemy hit strips a costume too; that ambiguity is why it is gone")
def t_costume_alone(G):
    r = Rig(G)
    r.spawn(0)
    r.spawn(1)
    r.tick(1.0)
    r.costume = NONE
    r.tick(10.0)
    return r.fired, []


@case("the Costume Strip Trap credits nothing",
      "it used to look exactly like the confirming half of a takedown")
def t_strip(G):
    r = Rig(G)
    r.spawn(0)
    r.tick(1.0)
    r.strip()
    r.tick(10.0)
    return r.fired, []


@case("a count drop on its own credits nothing",
      "a count cannot tell a kill from a streamer, which is why it went")
def t_count_alone(G):
    r = Rig(G)
    r.spawn(0)
    r.tick(1.0)
    r.total -= 3
    r.tick(10.0)
    return r.fired, []


@case("a poll gap cannot lose a takedown",
      "the free is terminal, so not looking in time is no longer fatal")
def t_gap(G):
    r = Rig(G)
    p = r.spawn(2)
    r.tick(1.0)
    r.hit(p)
    r.tick(0.2)
    r.downed(p)
    r.freed(p)
    r.t += 600.0               # ten minutes with the client asleep
    r.tick(0.1)
    return r.fired, [2]


@case("being caught is not a takedown",
      "it costs the costume, which used to be the confirming half")
def t_caught(G):
    r = Rig(G)
    r.spawn(0)
    r.tick(1.0)
    r.state = G.CAUGHT_STATE
    r.costume = NONE
    r.tick(5.0)
    return r.fired, []


@case("dying is not a takedown",
      "0x002806F4 calls RemoveCostume, and that used to be indistinguishable")
def t_dying(G):
    r = Rig(G)
    r.spawn(0)
    r.tick(1.0)
    r.state = sorted(G.DEATH_STATES)[0]
    r.costume = NONE
    r.tick(5.0)
    return r.fired, []


# -- despawning still works -------------------------------------------

@case("a banked catcher is offered for despawn",
      "despawn_targets reads homes and despawned, which the judge still keeps")
def t_despawn_targets(G):
    r = Rig(G)
    p = r.spawn(1)
    r.tick(1.0)
    r.judge.credited.setdefault(MINE, set()).add(1)
    got = r.judge.despawn_targets(MINE, {(MINE, 1)},
                                  list(r.keepers.values()))
    return got, [(p, 1)]


@case("a keeper chasing Taz is left alone",
      "one halfway across the level must not vanish in front of him")
def t_despawn_far(G):
    r = Rig(G)
    p = r.spawn(1)
    r.tick(1.0)
    r.move(p, tuple(c + 9000.0 for c in r.posts[1]))
    r.tick(0.2)
    got = r.judge.despawn_targets(MINE, {(MINE, 1)},
                                  list(r.keepers.values()))
    return got, []


@case("an unbanked catcher is never despawned",
      "it would delete a check the player has not earned yet")
def t_despawn_unbanked(G):
    r = Rig(G)
    r.spawn(1)
    r.tick(1.0)
    got = r.judge.despawn_targets(MINE, set(), list(r.keepers.values()))
    return got, []

def net_cases(G):
    """The client's refusal, tested against a stub rather than a live client.

    Only three fields are read -- mode, costumes, levels -- so a stub with
    those three is the whole of the input. The method under test is the real
    one, taken off the class.
    """
    path = os.path.join(WORLD, "client.py")
    src = open(path, encoding="utf-8").read()
    # client.py imports Archipelago machinery at module level, so the one
    # method is compiled on its own rather than importing the module.
    start = src.index("    def catcher_refused(self, lid):")
    end = src.index("    def new_checks(self):")
    ns = {"D": sys.modules["tazworld.logic"], "G": G}
    exec("class _Stub:\n" + src[start:end], ns)
    refused = ns["_Stub"].catcher_refused

    class S:
        pass

    out = []

    def check(label, mode, costumes, levels, lid, want_refused):
        s = S()
        s.mode, s.costumes, s.levels = mode, set(costumes), set(levels)
        got = refused(s, lid)
        ok = bool(got) == want_refused
        out.append((label, ok, got))

    dressed = G.COSTUME_BY_NAME["Adventurer"]
    ninja = G.COSTUME_BY_NAME["Ninja"]
    reindeer = G.COSTUME_BY_NAME["Christmas Reindeer"]

    check("open, no costume, no unlock -> refused",
          "open", [], [], MINE, True)
    check("open, costume but no unlock -> refused",
          "open", [dressed], [], MINE, True)
    check("open, unlock but no costume -> refused",
          "open", [], [MINE], MINE, True)
    check("open, both -> sent",
          "open", [dressed], [MINE], MINE, False)
    check("open, the wrong costume -> refused",
          "open", [ninja], [MINE], MINE, True)
    # Linear has no level unlock items at all. Testing for one there would
    # refuse every catcher in the seed, which is the note's own warning.
    check("linear, costume, no unlock item -> sent",
          "linear", [dressed], [], MINE, False)
    check("linear, no costume -> refused",
          "linear", [], [], MINE, True)
    # Yosemite Zoo is a hub with a keeper in it. Hubs are never unlock items,
    # so the unlock test must not apply to it.
    check("open, hub keeper with the Reindeer -> sent",
          "open", [reindeer], [], 3, False)
    check("open, hub keeper without it -> refused",
          "open", [], [], 3, True)

    # THE FLOOD. `catcher_blind` is what the client turns into red lines for
    # the player. It was only ever cleared in __init__ and start_catchers, so
    # everything appended to it stayed for the whole session -- and client.py
    # reports the FIRST entry each time, once a minute. One keeper standing
    # 5299 units from Yosemite Zoo's single post therefore produced the same
    # sentence every sixty seconds for the rest of the run, in every level,
    # long after leaving the one it was about.
    #
    # Checked in the source because the alternative is a live Game with a
    # hooked memory, and this is the property that actually matters: the list
    # is emptied once per tick.
    gsrc = open(os.path.join(WORLD, "game.py"), encoding="utf-8").read()
    start = gsrc.index("    def catcher_tick(self")
    end = gsrc.index("\n    def ", start + 10)
    body = gsrc[start:end]
    # Judging during a LOAD is what produced "In 3: 5 keepers ... none of
    # them within 800 units" while the player was walking into Tazland: the
    # incoming level's enemies exist before LEVEL_ID stops reading the old
    # one. Source-checked for the same reason as the clear below.
    out.append(("catcher_tick refuses to judge during a load",
                "if self.game_state() != STATE_ACTIVE:" in body, None))

    cleared = body.find("self.catcher_blind = []")
    appended = body.find("self.catcher_blind.append")
    out.append(("catcher_tick empties catcher_blind, so a stale warning "
                "cannot repeat forever", cleared >= 0, None))
    # find() rather than index(): a missing one has to REPORT, not raise. The
    # first version of this check crashed the suite instead of failing it,
    # which is a worse test than none -- a traceback reads as a broken test
    # rather than as broken code.
    out.append(("...before anything appends to it",
                0 <= cleared < appended,
                f"clear at {cleared}, first append at {appended}"))
    return out


def hooked_game(G):
    """A connected Game with the catcher posts loaded, or None.

    Hooking goes through Game.connect() rather than the memory module, which
    is what the client itself does: pcsx2_mem's entry point is hook(), not
    connect(), and calling the wrong one is how this first went wrong.
    """
    mem = sys.modules["tazworld.game"].mem
    if mem is None:
        print("    pcsx2_mem did not import, so there is nothing to hook. It "
              "needs pcsx2_interface/pine.py, which lives inside the world.")
        return None
    game = G.Game()
    try:
        ok = game.connect()
    except Exception as e:
        # pine swallows socket errors and simply reports not-connected, but
        # anything else it raises should read as a message, not a traceback.
        print(f"    hooking PCSX2 failed: {type(e).__name__}: {e}")
        return None
    if not ok:
        print("    could not reach PCSX2 on PINE. Is the game running, and is "
              "PINE enabled in Settings -> Advanced? Close the AP client "
              "first as well -- only one thing at a time on that socket.")
        return None
    path = os.path.join(WORLD, "data", "taz_catchers.json")
    game.start_catchers(json.load(open(path, encoding="utf-8")))
    return game


# --------------------------------------------------- catchers() vs the dump

class DumpMem:
    """ee_dump.bin behind the pcsx2_mem API, where file offset == address.

    Enough of the interface for TazPS2: read_u32, read_bytes, read_floats,
    valid_ptr. Reads outside the image raise, exactly as a bad live read does.
    """

    EE_MIN, EE_MAX = 0x00100000, 0x02000000

    def __init__(self, path):
        self.blob = open(path, "rb").read()

    def read_u32(self, a):
        if not 0 <= a <= len(self.blob) - 4:
            raise ValueError("0x%08X outside the dump" % a)
        return struct.unpack_from("<I", self.blob, a)[0]

    def read_bytes(self, a, n):
        if not 0 <= a <= len(self.blob) - n:
            raise ValueError("0x%08X outside the dump" % a)
        return self.blob[a:a + n]

    def read_floats(self, a, n):
        return struct.unpack("<" + "f" * n, self.read_bytes(a, 4 * n))

    def valid_ptr(self, p):
        return p is not None and self.EE_MIN <= p < self.EE_MAX


def dump_cases(G):
    """THE TEST THIS SUITE DID NOT HAVE, and the reason the bee hive catcher
    survived nine sessions.

    Every case above feeds the judge hand-built keeper dicts. Not one of them
    ever ran `catchers()` against memory -- so `catchers()` could read the
    enemy list completely wrong, return one keeper out of six, and the suite
    stayed green the whole time. It did, and it was: 0x0046C680 is a linked
    list head, not an array, and indexing it returned only the first and last
    enemy in the level.

    This runs the SHIPPED catchers() against a real 32MB image and asserts it
    finds every keeper. The dump is Zooney Tunes taken with Taz far from all
    of them, so all eleven enemies are in the dormant group -- which is what
    also makes it a fair test of walking BOTH groups. Revert either half of
    the fix and this goes red immediately.

    Skipped, LOUDLY, if there is no dump: a quiet skip is how a test that
    proves nothing looks exactly like a test that passed.
    """
    path = os.path.join(HERE, "ee_dump.bin")
    if not os.path.exists(path):
        return [("catchers() vs a real dump -- NO ee_dump.bin, NOT RUN. "
                 "py -3.13 taz_ramdump.py --out ee_dump.bin", False, None)]

    mem = DumpMem(path)
    out = []

    def check(label, ok, got=None):
        out.append((label, bool(ok), got))

    lvl = mem.read_bytes(G.LEVEL_MANAGER, 8).split(b"\0")[0]
    check("the dump is Zooney Tunes (the manager names the level 'safari')",
          lvl == b"safari", lvl)

    keepers = G.TazPS2(mem).catchers()
    names = sorted(k["name"] for k in keepers)

    # SIX. The old index read returned one. This is the whole bug.
    check("catchers() finds all six keepers", len(keepers) == 6,
          f"{len(keepers)}: {names}")
    check("and they are keeper01 through keeper06",
          names == ["enemy keeper%02d" % i for i in range(1, 7)], names)

    # The specific one, by name, because a count can be right by accident.
    check("keeper05 -- the bee hive one -- is among them",
          any(k["name"] == "enemy keeper05" for k in keepers), names)

    # `all()` over an empty list is True, so each of these also says "and
    # there were some". Without that they pass loudest exactly when
    # catchers() is returning nothing at all -- which is the failure being
    # guarded against. Caught by breaking the fix on purpose and watching
    # three of these stay green over an empty list.
    check("every keeper has a readable leash centre",
          keepers and all(k["home"] is not None for k in keepers),
          [k["name"] for k in keepers if k["home"] is None] or "no keepers")

    check("every keeper reads its permanent defeat bit",
          keepers and all(k["alive"] is not None for k in keepers),
          [k["name"] for k in keepers if k["alive"] is None] or "no keepers")

    # None has been beaten in this capture, so all six must read alive. A
    # check for "not None" alone would pass on a read stuck at zero -- which
    # would credit all six the moment the judge trusted it.
    check("and none of them reads as beaten in this capture",
          keepers and all(k["alive"] is True for k in keepers),
          [k["name"] for k in keepers if k["alive"] is not True]
          or "no keepers")

    # Identity: six keepers must land on six DIFFERENT posts, or the judge
    # credits one catcher repeatedly and the dedup silently eats the rest.
    judge = G.CatcherJudge(posts=load_posts(G), level_radius=load_radii(G))
    idx = [judge.match_post(5, k["home"]) for k in keepers]
    check("the six match six distinct posts",
          len(set(idx)) == 6 and None not in idx, sorted(map(str, idx)))

    # The regression guard. Indexing the head as an array can never see more
    # than two nodes, because index >= 2 is head+0x178 onwards and reads zero.
    # If this ever yields more than two the structure has changed, and the
    # walk needs re-deriving rather than trusting.
    n = mem.read_u32(G.ENEMY_DORMANT + G.L_COUNT)
    old = [p for p in (mem.read_u32(G.ENEMY_DORMANT + G.L_NEXT + i * 4)
                       for i in range(min(n, 40))) if mem.valid_ptr(p)]
    check("indexing the list head as an array still yields at most two "
          "(the bug this replaced)", len(old) <= 2 < len(keepers),
          f"{len(old)} readable of {n}")

    return out


# --------------------------------------- the judge vs a RECORDED real kill

CAPTURE = os.path.join(HERE, "data", "taz_catcher_capture.txt")


def capture_cases(G):
    """Replay a real `taz_enemylist.py watch` recording through the judge.

    Everything else in this file is a timeline someone wrote down from what
    they believed the game does. This one IS what the game did: two catchers
    beaten in Zooney Tunes, every field change at 10Hz, keeper01 and then
    keeper05, the bee hive one.

    It is here because the belief and the game have now disagreed twice in
    this project, and both times the belief was the thing shipped. A hand-
    written rig can be made to agree with a wrong judge -- the old suite's
    `leave()` did exactly that for nine sessions. A recording cannot.

    The enemy homes come from ee_dump.bin, so this also pins identity: it is
    the leash centres that decide these are catchers 1 and 5.
    """
    out = []

    def check(label, ok, got=None):
        out.append((label, bool(ok), got))

    if not os.path.exists(CAPTURE):
        return [("the recorded capture is missing -- data/"
                 "taz_catcher_capture.txt", False, None)]
    text = open(CAPTURE, encoding="utf-8").read()

    # The capture is evidence before it is a fixture. Assert the things it
    # was kept FOR are still in it, so an edited file cannot quietly turn
    # into a weaker test.
    check("the recording caught the bug live -- keeper05 invisible at 2 of 3",
          "is INVISIBLE to the old index read (2 of 3)" in text)
    check("it caught E_ALIVE clearing, twice",
          text.count("**E_ALIVE 1 -> 0**") == 2,
          text.count("**E_ALIVE 1 -> 0**"))
    check("it caught two objects being FREED, not reparented",
          text.count("TOTAL DROPPED") == 2, text.count("TOTAL DROPPED"))
    check("and distance culls that were NOT",
          text.count("total unchanged -- a list-to-list move") >= 8)

    dump = os.path.join(HERE, "ee_dump.bin")
    if not os.path.exists(dump):
        check("replaying it needs ee_dump.bin for the leash centres -- "
              "NOT RUN", False)
        return out
    mem = DumpMem(dump)
    homes = {}
    head = G.ENEMY_DORMANT
    cur = mem.read_u32(head + G.L_NEXT)
    while cur != head and mem.valid_ptr(cur):
        sub = mem.read_u32(cur + G.E_SUB)
        homes[cur] = mem.read_floats(sub + G.E_HOME, 3)
        cur = mem.read_u32(cur + G.L_NEXT)

    judge = G.CatcherJudge(posts=load_posts(G), level_radius=load_radii(G))
    ent, fired, blind, why = {}, [], [], []

    def poll(now):
        ks = [dict(ptr=p, pos=homes.get(p), home=homes.get(p), name=e["name"],
                   active=e["active"], anim=e["anim"],
                   defeated=bool(e["hit"]), alive=bool(e["alive"]),
                   addr=0, alive_addr=0)
              for p, e in ent.items() if e["name"].startswith("enemy keeper")]
        fired.extend(judge.poll(5, ks, costume=0x5, taz_state=None,
                                total=None, complete=True, now=now))
        blind.extend(judge.blind)
        why.extend(judge.why)

    import re
    for line in text.split("\n"):
        m = re.match(r"\s*t\+\s*([\d.]+)\s+(.*)", line)
        if not m:
            continue
        t, rest = float(m.group(1)), m.group(2)
        pm = re.search(r"([0-9A-F]{8})", rest)
        if "APPEARS" in rest:
            a = re.search(r"anim=(\d+) alive=(\d+)", rest)
            ent[int(pm.group(1), 16)] = dict(
                name=rest.split("  ")[-1].strip(),
                active="in active" in rest,
                anim=int(a.group(1)), alive=int(a.group(2)), hit=0)
        elif "moved to" in rest:
            ent[int(pm.group(1), 16)]["active"] = "ACTIVE" in rest
        elif "E_ALIVE" in rest:
            a = re.search(r"E_ALIVE (\d+) -> (\d+)", rest)
            ent[int(pm.group(1), 16)]["alive"] = int(a.group(2))
        elif "hit latch" in rest:
            a = re.search(r"hit latch (\d+) -> (\d+)", rest)
            ent[int(pm.group(1), 16)]["hit"] = int(a.group(2))
        elif re.search(r"anim (\d+) -> (\d+)", rest):
            a = re.search(r"anim (\d+) -> (\d+)", rest)
            ent[int(pm.group(1), 16)]["anim"] = int(a.group(2))
        elif "GONE" in rest:
            ent.pop(int(pm.group(1), 16), None)
        else:
            continue
        poll(t)

    check("the judge credits exactly the two catchers he beat",
          sorted(fired) == [1, 5], sorted(fired))
    check("...and each of them once", len(fired) == 2, fired)
    check("nothing is flagged for the player -- no warnings on a clean run",
          blind == [], blind)
    # WHICH path fired matters. The bit clearing is the early one, and until
    # this recording nobody had ever seen E_ALIVE reach 0 on hardware -- it
    # was very nearly deleted as unreachable. Both kills here credit on it,
    # 0.74s before the object is freed. If that stops being true, the bit is
    # not doing what this says and the change should be noticed, not absorbed
    # silently by the departure path.
    bit = [w for w in why if "its defeat bit cleared" in w]
    check("both credits come from the bit clearing, not the departure",
          len(bit) == 2, bit)
    check("...and the departure path books nothing on top of them",
          not any("seen down and then freed" in w for w in why),
          [w for w in why if "seen down and then freed" in w])
    # Identity, from the leash centres rather than from the log's own names.
    check("keeper01's leash centre is catcher 1",
          judge.match_post(5, homes[0x00C86E00]) == 1,
          judge.match_post(5, homes[0x00C86E00]))
    check("keeper05's -- the bee hive one -- is catcher 5",
          judge.match_post(5, homes[0x00C97080]) == 5,
          judge.match_post(5, homes[0x00C97080]))
    return out


# ---------------------------------------------------------------- sim

def cmd_sim(args):
    G = load_game()
    print("    CatcherJudge -- one condition: E_ALIVE (SUB+0x300) observed "
          "going")
    print(f"    from set to clear.  despawn radius {G.DESPAWN_RADIUS:.0f}")
    print()
    bad = 0

    print("    detection")
    for name, why, fn in CASES:
        try:
            got, want = fn(G)
        except Exception as e:
            print(f"      FAIL  {name}\n            {type(e).__name__}: {e}")
            bad += 1
            continue
        ok = got == want
        bad += 0 if ok else 1
        print(f"      {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"            {why}")
            print(f"            wanted {want!r}, got {got!r}")

    print()
    print("    the client's safety net")
    for label, ok, got in net_cases(G):
        bad += 0 if ok else 1
        print(f"      {'ok  ' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"            got {got!r}")

    print()
    print("    the judge against a RECORDED real kill")
    captured = capture_cases(G)
    for label, ok, got in captured:
        bad += 0 if ok else 1
        print(f"      {'ok  ' if ok else 'FAIL'}  {label}")
        if not ok and got is not None:
            print(f"            got {got!r}")

    print()
    print("    catchers() against a real RAM dump")
    dumped = dump_cases(G)
    for label, ok, got in dumped:
        bad += 0 if ok else 1
        print(f"      {'ok  ' if ok else 'FAIL'}  {label}")
        if not ok and got is not None:
            print(f"            got {got!r}")

    print()
    total = len(CASES) + len(net_cases(G)) + len(dumped) \
            + len(captured)
    if bad:
        print(f"    {bad} of {total} FAILED")
    else:
        print(f"    all {total} passed")
    return 1 if bad else 0


# ---------------------------------------------------------------- live

def cmd_live(args):
    """Read-only. Prints the judge's reasoning while you play.

    --banked seeds the judge with catchers you have ALREADY checked, which
    is the state the real client is in and this tool is not. It matters:
    the last-one-standing rescue only fires when exactly one catcher in the
    level is still uncredited, so with a fresh judge -- six uncredited -- it
    correctly refuses to guess and the run looks like a failure that is
    really an untested path.

        taz_catcher_test.py live --level 5 --banked 0,1,2,3,4

    Still read-only: this seeds the judge's own bookkeeping, not the
    client's, and nothing is despawned either way.
    """
    G = load_game()
    game = hooked_game(G)
    if game is None:
        return 1
    n = sum(len(v) for v in game._catcher_posts.values())
    if args.banked:
        lid = args.level
        idx = {int(x) for x in args.banked.replace(",", " ").split()}
        game._catchers.credited.setdefault(lid, set()).update(idx)
        print(f"    Pretending catchers {sorted(idx)} in level {lid} are "
              f"already checked.")
        posts = game._catchers.posts.get(lid) or []
        left = [i for i in range(len(posts)) if i not in idx]
        print(f"    Still uncredited: {left}"
              + ("   <- the rescue can name this one" if len(left) == 1
                 else "   <- the rescue needs exactly one, so it will not fire"))
    print(f"    {n} recorded posts. Read-only: nothing is written, and "
          "nothing is despawned.")
    print("    Fight a keeper. Ctrl-C to stop.")
    print()

    seen_why = None
    t0 = time.time()
    try:
        while True:
            # No `credited`, so _despawn does nothing -- this stays read-only.
            kills = game.catcher_tick()
            why = game.catcher_why
            # Reset on an empty tick as well, so the same line appearing again
            # later still prints rather than being taken for a repeat.
            if why != seen_why:
                for line in why:
                    print(f"      [{time.time() - t0:6.1f}] {line}")
                seen_why = why
            for lid, idx in kills:
                print(f"      [{time.time() - t0:6.1f}] "
                      f"*** CATCHER {idx} in level {lid} CREDITED ***")
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    print("    stopped")
    return 0


def cmd_roster(args):
    """Superseded by taz_enemylist.py.

    This printed the enemy "array" unfiltered, and it was the tool that
    produced the capture the whole bee hive investigation ran on -- three
    enemies counted, two readable. It was right about what it saw and wrong
    about what that meant: 0x0046C680 is a linked list head, so index 0 was
    the first enemy, index 1 the last, and index 2 onwards was permanently
    zero. The missing one was never missing. It was in the middle.

    taz_enemylist.py walks the lists properly, shows both groups, and has a
    `check` verb that asserts the whole structure against a RAM dump with no
    emulator running. There is no version of this command worth keeping
    beside it.
    """
    print("    Superseded by taz_enemylist.py, which walks the enemy lists")
    print("    instead of indexing a list head as though it were an array:")
    print()
    print("        py -3.13 taz_enemylist.py check    offline, vs ee_dump.bin")
    print("        py -3.13 taz_enemylist.py watch    live, both groups")
    print()
    return 2



def cmd_despawn(args):
    """List loaded keepers, and optionally send one away.

    THIS ONE WRITES. It is here so the despawn can be proved on a keeper you
    choose, in a level you do not mind, before the client starts doing it by
    itself to every catcher already checked.
    """
    G = load_game()
    game = hooked_game(G)
    if game is None:
        return 1
    mem = sys.modules["tazworld.game"].mem
    lid = game.level_id()
    keepers = G.TazPS2(mem).catchers()
    posts = game._catcher_posts.get(lid) or []
    print(f"    level {lid}, {len(posts)} recorded post(s), "
          f"{len(keepers)} keeper(s) loaded")
    if not keepers:
        print("    nothing loaded -- walk nearer to one.")
        return 0

    rows = []
    for k in keepers:
        idx = game._catchers.match_post(lid, k.get("pos"))
        d = None
        if idx is not None and idx < len(posts):
            d = G.dist2(k["pos"], posts[idx]) ** 0.5
        rows.append((idx, k, d))
        where = f"{d:8.0f} from its post" if d is not None else "no post match"
        print(f"      catcher {str(idx):>4s}  ptr {k['ptr']:08X}  "
              f"anim {k['anim']:2d}  defeated {int(bool(k['defeated']))}  "
              f"{where}")

    if args.which is None:
        print()
        print("    Nothing written. Add -w N to send catcher N away.")
        return 0

    hit = [(i, k) for i, k, _ in rows if i == args.which]
    if not hit:
        print(f"    catcher {args.which} is not one of the loaded ones.")
        return 1
    _, k = hit[0]
    sub = mem.read_u32(k["ptr"] + G.E_SUB)
    if not mem.valid_ptr(sub):
        print("    its state object did not read back sensibly; not writing.")
        return 1
    before = mem.read_u32(sub + G.E_ANIM)
    total_before = game._read_enemy_total()
    mem.write_u32(sub + G.E_ANIM, G.ANIM_DESPAWN)
    print(f"    wrote anim {G.ANIM_DESPAWN} over {before} at {sub + G.E_ANIM:08X}"
          f" (enemy total {total_before})")

    t0 = time.time()
    while time.time() - t0 < 6.0:
        live = {x["ptr"] for x in G.TazPS2(mem).catchers()}
        if k["ptr"] not in live:
            print(f"    gone after {time.time() - t0:.1f}s. "
                  f"Enemy total now {game._read_enemy_total()} "
                  f"(was {total_before}).")
            return 0
        time.sleep(0.2)
    now = mem.read_u32(sub + G.E_ANIM)
    print(f"    still there after 6s; its anim reads {now}. "
          "The game did not take the write -- say so and this comes back out.")
    return 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("sim").set_defaults(fn=cmd_sim)
    lv = sub.add_parser("live")
    lv.add_argument("--banked", default="",
                    help="catcher indices already checked, e.g. 0,1,2,3,4")
    lv.add_argument("--level", type=int, default=5,
                    help="which level --banked refers to (default 5)")
    lv.set_defaults(fn=cmd_live)
    rr = sub.add_parser("roster")
    rr.add_argument("--out", default="",
                    help="also write to this file, line buffered, so a "
                         "Ctrl-C cannot lose it")
    rr.set_defaults(fn=cmd_roster)
    d = sub.add_parser("despawn")
    d.add_argument("-w", "--which", type=int,
                   help="send this catcher away (THIS WRITES)")
    d.set_defaults(fn=cmd_despawn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
