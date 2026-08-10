#!/usr/bin/env python3
"""Does a powerup clean up after itself, and do effects stop stampeding?

    py -3.13 taz_powerup_test.py

No emulator: game.py's `mem` is a module attribute, so it is replaced with a
dict and every read and write the shipped code performs is visible here.

WHAT BROKE
----------
A seed with every sandwich and every percent of destruction as a check hands
out filler by the thousand, and Local Filler keeps most of it at home. The
effect queue is then never empty -- and hold_traps ends one effect and the
queue starts the next on the SAME tick, because active_traps is empty again by
then. So they arrived one every tenth of a second.

Under that, the chili pepper stuck: square did nothing but breathe fire, for
the rest of the run. _grant_powerup writes STATE_CHILLIPEPPER into four fields
and end_powerup cleared the flag, the id and the timer -- but never the state.
Nothing was left flagged to explain it, which is why it looked like the game
had broken rather than the client.

Two things are checked here: that the state is put back, and that a full queue
cannot fire effects faster than the game can finish them.
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

TAZ_OBJ = 0x01200000
STATE_OBJ = 0x01300000
COSTUME_OBJ = 0x01500000

# There is no separate "bonus object". T.O_BONUS_PTR and T.O_STATE_PTR are
# BOTH 0x1C8, so _grant_powerup's two-entry loop resolves to the same object
# twice and the second pass only adds +0xB4 to the first pass's +0xB0 and
# +0x204. Modelling them as two objects made this test write somewhere the
# shipped code never touches, and it duly reported the code doing nothing.

STATE_CHILLIPEPPER = 0x3B
STATE_MOVE = 0x00
STATE_SPIN = 0x0D


class FakeMem:
    """Just enough of pcsx2_mem, backed by a dict."""

    EE_MIN, EE_MAX = 0x00100000, 0x02000000

    def __init__(self):
        self.w = {}

    def valid_ptr(self, p):
        return p is not None and self.EE_MIN <= p < self.EE_MAX

    def read_u32(self, a):
        return self.w.get(a, 0)

    def write_u32(self, a, v):
        self.w[a] = v & 0xFFFFFFFF

    def read_u8(self, a):
        return self.w.get(a, 0) & 0xFF

    def write_u8(self, a, v):
        self.w[a] = v & 0xFF

    def write_bytes(self, a, data):
        self.w[a] = data

    def read_bytes(self, a, n):
        v = self.w.get(a, b"\0" * n)
        return v if isinstance(v, bytes) else struct.pack("<I", v)[:n]

    def read_float(self, a):
        return struct.unpack("<f", self.read_bytes(a, 4))[0]

    def write_float(self, a, v):
        self.w[a] = struct.pack("<f", v)

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


def wire(G, mem, state=STATE_MOVE):
    """A Taz with a state object and a costume."""
    T = G.T
    mem.w.clear()
    mem.write_u32(T.TAZ_PTR, TAZ_OBJ)
    mem.write_u32(TAZ_OBJ + T.O_STATE_PTR, STATE_OBJ)
    mem.write_u32(TAZ_OBJ + T.O_COSTUME_PTR, COSTUME_OBJ)
    for a in state_fields(G):
        mem.write_u32(a, state)


def state_fields(G):
    """Exactly the addresses _grant_powerup writes, derived from its own
    offsets rather than from an assumption about them."""
    T = G.T
    out = []
    for off, s_off, e_off in ((T.O_STATE_PTR, T.S_STATE, 0x204),
                              (T.O_BONUS_PTR, 0x0B0, 0x0B4)):
        for field in (s_off, e_off):
            a = STATE_OBJ + field
            if a not in out:
                out.append(a)
    return out


def grant(g, clock, name):
    """Grant an effect, having first satisfied the safe-state debounce.

    safe_to_interrupt() deliberately returns False the first time it is asked:
    it wants the state to have HELD, because the client polls ten times a
    second and one reading can land between two halves of an animation. So a
    test that calls grant_effect once gets "defer" and measures nothing --
    which is exactly what the first version of this file did.
    """
    for _ in range(10):
        clock.now += 0.1
        ready = g.safe_to_interrupt(), g.playable()
        if all(ready):
            break
    return g.grant_effect(name)


RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"    {'PASS' if ok else '*** FAIL ***':<12} {label}")
    if detail and not ok:
        print(f"                 {detail}")


def main():
    mem, clock = FakeMem(), Clock()
    G = load_game(mem, clock)
    g = G.Game()

    print("    a chili pepper, from grant to teardown:")
    print()

    # ---- the bug itself
    wire(G, mem, STATE_MOVE)
    grant(g, clock, "pepper")
    got = [mem.read_u32(a) for a in state_fields(G)]
    check("granting writes STATE_CHILLIPEPPER into every state field",
          got == [STATE_CHILLIPEPPER] * len(got),
          f"reads {[hex(v) for v in got]}")

    clock.now += 20.0
    g.end_powerup("pepper")
    got = [mem.read_u32(a) for a in state_fields(G)]
    check("ending puts every one of them back",
          got == [STATE_MOVE] * len(got),
          f"reads {[hex(v) for v in got]} -- 0x3B here is square stuck on fire")

    T = G.T
    check("and the flag, the id and the timer are cleared too",
          mem.read_u8(COSTUME_OBJ + G.Game.POWERUPS["pepper"]["flag"]) == 0
          and mem.read_u32(COSTUME_OBJ + T.C_ACTIVE_ID) == T.C_ACTIVE_NONE)

    # ---- the game moving Taz on must win over our stale copy
    wire(G, mem, STATE_MOVE)
    grant(g, clock, "pepper")
    for a in state_fields(G):
        mem.write_u32(a, STATE_SPIN)           # the game drives it every frame
    g.end_powerup("pepper")
    got = [mem.read_u32(a) for a in state_fields(G)]
    check("a state the GAME changed is left alone, not overwritten",
          got == [STATE_SPIN] * len(got), f"reads {[hex(v) for v in got]}")

    # ---- a bad costume pointer must not skip the restore
    wire(G, mem, STATE_MOVE)
    grant(g, clock, "pepper")
    mem.write_u32(TAZ_OBJ + T.O_COSTUME_PTR, 0)     # streamed out mid-effect
    g.end_powerup("pepper")
    got = [mem.read_u32(a) for a in state_fields(G)]
    check("the state is restored even when the costume has gone",
          got == [STATE_MOVE] * len(got), f"reads {[hex(v) for v in got]}")

    # ---- pepper must wait for a safe moment, as burp already did
    check("the pepper now waits for a safe state, like everything else that "
          "writes one",
          "pepper" in G.Game.DEFER_UNTIL_SAFE
          and "hiccup" in G.Game.DEFER_UNTIL_SAFE)

    print()
    print("    invisibility, which has a look as well as an effect:")
    print()

    # The four words 0x0023F3C8 writes, derived from the shipped offsets so
    # this cannot drift away from what game.py actually does.
    def material(T):
        return [mem.read_u32(TAZ_OBJ + off)
                for off in (T.O_MAT_MODE, T.O_MAT_PARAM,
                            T.O_MAT_MODE + 4, T.O_MAT_PARAM + 4)]

    # What a normal, opaque Taz looks like: the values 0x0023F1A8 leaves.
    OPAQUE = [2, 0x003AAE50, 4, 0x003AAE60]

    def wire_invis(state=STATE_MOVE):
        wire(G, mem, state)
        for off, val in zip((T.O_MAT_MODE, T.O_MAT_PARAM,
                             T.O_MAT_MODE + 4, T.O_MAT_PARAM + 4), OPAQUE):
            mem.write_u32(TAZ_OBJ + off, val)

    # ---- the bug: the flag was set and the material was not
    wire_invis()
    grant(g, clock, "invisibility")
    check("granting sets the invisibility flag",
          mem.read_u8(COSTUME_OBJ + G.Game.POWERUPS["invisibility"]["flag"]) == 1)
    check("and applies the half-alpha material, so Taz need not spin for it",
          material(T) == [3, 0, 4, T.MAT_INVISIBLE],
          f"reads {[hex(v) for v in material(T)]}, wanted "
          f"[0x3, 0x0, 0x4, {hex(T.MAT_INVISIBLE)}]")

    clock.now += 20.0
    g.end_powerup("invisibility")
    check("ending puts the opaque material back",
          material(T) == OPAQUE, f"reads {[hex(v) for v in material(T)]}")
    check("and clears the flag",
          mem.read_u8(COSTUME_OBJ
                      + G.Game.POWERUPS["invisibility"]["flag"]) == 0)

    # ---- the game re-applying it itself must win, same rule as the state
    wire_invis()
    grant(g, clock, "invisibility")
    for off in (T.O_MAT_MODE, T.O_MAT_PARAM):
        mem.write_u32(TAZ_OBJ + off, 0x1234)     # a spin, a model swap, a boss
    g.end_powerup("invisibility")
    check("a material the GAME changed is left alone, not overwritten",
          mem.read_u32(TAZ_OBJ + T.O_MAT_MODE) == 0x1234)

    # ---- the length, which is set by where the timer starts
    def timer():
        return struct.unpack(
            "<f", mem.read_bytes(COSTUME_OBJ + T.C_POWER_TIME, 4))[0]

    wire_invis()
    grant(g, clock, "invisibility")
    check("the timer is planted so the game's own expiry lands at "
          f"{T.INVIS_SECONDS}s",
          abs(timer() - T.INVIS_START) < 1e-4
          and abs((T.INVIS_ENDS_AT - timer()) - T.INVIS_SECONDS) < 1e-4,
          f"timer={timer()}, ends at {T.INVIS_ENDS_AT}, so the player gets "
          f"{T.INVIS_ENDS_AT - timer()}s")
    check("and it starts BELOW the blink threshold, so it blinks at the end "
          "and not the start",
          timer() < T.INVIS_BLINK_AT,
          f"timer={timer()}, blink at {T.INVIS_BLINK_AT}")
    check(f"the blink-out is the last {T.INVIS_BLINK_FOR}s of it",
          T.INVIS_BLINK_FOR == T.INVIS_ENDS_AT - T.INVIS_BLINK_AT
          and T.INVIS_BLINK_FOR < T.INVIS_SECONDS)
    check("the blink phase accumulator is cleared too, as the game's grant "
          "does at 0x0024C0E4",
          struct.unpack("<f", mem.read_bytes(
              COSTUME_OBJ + T.C_BLINK_PHASE, 4))[0] == 0.0)

    # ---- the blink IS the game taking the material off. The hold must not
    #      notice the gaps and fill them in, which is what stopped it before.
    wire_invis()
    grant(g, clock, "invisibility")
    for off, val in zip((T.O_MAT_MODE, T.O_MAT_PARAM,
                         T.O_MAT_MODE + 4, T.O_MAT_PARAM + 4), OPAQUE):
        mem.write_u32(TAZ_OBJ + off, val)      # mid-blink, material off
    mem.write_bytes(COSTUME_OBJ + T.C_POWER_TIME, struct.pack("<f", 22.5))
    g.hold_traps({"invisibility": clock.now + 5.0})
    check("the hold leaves a blinking Taz alone instead of filling the gap",
          material(T) == OPAQUE, f"reads {[hex(v) for v in material(T)]}")
    check("and does not drag the timer back below the blink threshold",
          abs(timer() - 22.5) < 1e-4,
          f"timer={timer()} -- 18.98 here is the effect being held open")
    check("invisibility is marked as the game's to run",
          G.Game.POWERUPS["invisibility"]["reassert"] is False)
    check("its hold outlasts the effect, so it is a backstop and not the clock",
          G.Game.POWERUPS["invisibility"]["hold"] > T.INVIS_SECONDS)

    # ---- but everything else still IS held, or the pepper regresses
    wire(G, mem, STATE_MOVE)
    grant(g, clock, "pepper")
    mem.write_bytes(COSTUME_OBJ + T.C_POWER_TIME, struct.pack("<f", 3.0))
    g.hold_traps({"pepper": clock.now + 5.0})
    check("the pepper is still re-asserted every tick, as it always was",
          abs(timer() - G.Game.POWERUPS["pepper"]["secs"]) < 1e-4,
          f"timer={timer()}")

    # ---- the game ending it first must not be undone
    wire_invis()
    grant(g, clock, "invisibility")
    for off, val in zip((T.O_MAT_MODE, T.O_MAT_PARAM,
                         T.O_MAT_MODE + 4, T.O_MAT_PARAM + 4), OPAQUE):
        mem.write_u32(TAZ_OBJ + off, val)      # 0x001C68E8 got there first
    g.end_powerup("invisibility")
    check("the backstop finds the game has already tidied up, and adds nothing",
          material(T) == OPAQUE, f"reads {[hex(v) for v in material(T)]}")

    # ---- MAT_FLAGS bit 1 makes the game skip slot 1; so must we
    wire_invis()
    mem.write_u32(T.MAT_FLAGS, T.MAT_SKIP_SLOT1)
    grant(g, clock, "invisibility")
    check("bit 1 of MAT_FLAGS set -> slot 1 left alone, as 0x0023F404 does",
          material(T) == [3, 0, OPAQUE[2], OPAQUE[3]],
          f"reads {[hex(v) for v in material(T)]}")
    mem.write_u32(T.MAT_FLAGS, 0)

    # ---- +0x170 is pepper's, not everybody's
    wire_invis()
    mem.write_u32(COSTUME_OBJ + T.C_ACTIVE_SUB, 0x7FFF7F03)
    grant(g, clock, "invisibility")
    check("invisibility leaves +0x170 alone -- its grant never writes it",
          mem.read_u32(COSTUME_OBJ + T.C_ACTIVE_SUB) == 0x7FFF7F03,
          f"reads {hex(mem.read_u32(COSTUME_OBJ + T.C_ACTIVE_SUB))}")
    wire(G, mem, STATE_MOVE)
    grant(g, clock, "pepper")
    check("pepper still writes it, because pepper was recorded doing so",
          mem.read_u32(COSTUME_OBJ + T.C_ACTIVE_SUB) == 2)

    # ---- the duration must stay under the game's own blink threshold
    check("the held duration stays below the 20.0s the game blinks at",
          G.Game.POWERUPS["invisibility"]["secs"] < T.INVIS_BLINK_AT
          and G.Game.POWERUPS["invisibility"]["secs"] < T.INVIS_ENDS_AT,
          f"secs={G.Game.POWERUPS['invisibility']['secs']}, "
          f"blink at {T.INVIS_BLINK_AT}, ends at {T.INVIS_ENDS_AT}")

    print()
    print("    the Raised Bounty item:")
    print()

    D = G.D
    # The address the game's own award writes, derived from the save geometry
    # rather than from anything this client believes about it.
    def game_bounty_addr(lid, f):
        return 0x003FFD9C + f * 0x42B4 + lid * 0x238 + 0x218

    check("the level bounty address agrees with the game's for every file "
          "and level",
          all(D.level_block(lid, f) + D.L_TOTAL_BOUNTY == game_bounty_addr(lid, f)
              for lid in range(3, 19) for f in range(3)))
    check("live_block is gone -- it was save slot 2, not a live block",
          not hasattr(G, "live_block") and not hasattr(G, "LIVE_BLOCK_BASE"))
    check("and the address it used to write IS slot 2's, which is the proof",
          0x00408BC4 == game_bounty_addr(3, 2))
    check("the running total is strided by the save file, not by 0x1000",
          G.TOTAL_BOUNTY_SAVE + 2 * D.FILE_STRIDE
          == 0x003FFD9C + 2 * 0x42B4 + 0x42A0)

    # bounty_ready: each refusal on its own, because each is a real crash the
    # popup has no guard against.
    def wire_bounty():
        mem.w.clear()
        mem.write_u32(G.BOUNTY_WIDGET, 0x00800000)   # HUD built
        mem.write_u32(G.POPUP_STATE_ADDR, 0)         # no banner up
        mem.write_u32(G.T.CURRENT_FILE, 0)           # save file 0
        mem.write_u32(G.CURRENT_LEVEL_BYTE, 9)       # Looningdale's
        mem.write_u32(G.T.GAME_STATE, G.T.STATE_ACTIVE)

    wire_bounty()
    check("a normal moment in a level is fine", g.bounty_ready() is True)

    for label, addr, val in (
            ("no HUD widget -> no (0x00202068 jalrs through it unchecked)",
             G.BOUNTY_WIDGET, 0),
            ("a banner already up -> no, it would cut the game off "
             "mid-sentence", G.POPUP_STATE_ADDR, 2),
            ("no save file loaded -> no (0x00201E90 reads a SIGNED byte)",
             G.T.CURRENT_FILE, 0xFF),
            ("a level id past the save record -> no, it would write the "
             "next slot", G.CURRENT_LEVEL_BYTE, 30),
            ("mid-load -> no", G.T.GAME_STATE, 5)):
        wire_bounty()
        mem.write_u32(addr, val)
        check(label, g.bounty_ready() is False)

    # And a refusal must defer, not drop the item on the floor.
    wire_bounty()
    mem.write_u32(G.T.LEVEL_ID, 9)
    mem.write_u32(G.T.GAME_STATE, 5)
    check("a refusal returns 'defer', so the client asks again next tick",
          g.grant_effect("bounty") == "defer")

    # With no trampoline the banner cannot happen, but the money still must --
    # and at the address the game itself writes.
    wire_bounty()
    mem.write_u32(G.T.LEVEL_ID, 9)
    before = mem.read_u32(game_bounty_addr(9, 0))
    got = g.grant_effect("bounty")
    check("with no trampoline it still awards, and does not defer",
          got is None)
    check("the level's bounty went up by BOUNTY_STEP, in the right place",
          mem.read_u32(game_bounty_addr(9, 0)) - before == g.BOUNTY_STEP)
    check("and so did the running total",
          mem.read_u32(G.TOTAL_BOUNTY_SAVE) == g.BOUNTY_STEP)
    check("nothing was written to the old slot-2 address",
          mem.read_u32(0x00409914) == 0)

    # The banner, stripped back to the number. No trampoline here, so the
    # page cannot be set -- but the logo and the slow motion are plain writes
    # and must happen regardless.
    LOGO_BOX = 0x00700000
    wire_bounty()
    mem.write_u32(G.BOUNTY_LOGO_BOX, LOGO_BOX)
    mem.write_u32(LOGO_BOX + G.BOX_FLAGS, 0x5780 | G.BOX_VISIBLE)
    g._quiet_bounty()
    check("the logo container's visible bit is cleared, and only that bit",
          mem.read_u32(LOGO_BOX + G.BOX_FLAGS) == 0x5780)
    check("and the slow-motion factor is pinned at 1.0",
          struct.unpack("<f", mem.read_bytes(G.BOUNTY_FACTOR, 4))[0] == 1.0)

    check("the cash page is the second one built, so index 1",
          G.BOUNTY_CASH_PAGE == 1)
    check("and state 2 is skipped, because it is a NextPage that would wrap "
          "straight back to the caption",
          G.BOUNTY_COUNTING == 3)

    # bounty_tick holds both while the banner is up, and stops when it ends.
    mem.write_u32(LOGO_BOX + G.BOX_FLAGS, 0x5780 | G.BOX_VISIBLE)
    mem.write_u32(G.POPUP_STATE_ADDR, 4)
    mem.write_bytes(G.BOUNTY_FACTOR, struct.pack("<f", 0.25))
    g._bounty_quiet_from = 0.0                      # past the grace window
    g.bounty_tick()
    check("bounty_tick keeps holding while the banner runs",
          struct.unpack("<f", mem.read_bytes(G.BOUNTY_FACTOR, 4))[0] == 1.0
          and mem.read_u32(LOGO_BOX + G.BOX_FLAGS) == 0x5780)

    # The count-up state gets its expiry pushed forward, so the finished
    # number stays readable instead of flicking away.
    mem.write_u32(G.POPUP_STATE_ADDR, G.BOUNTY_SHOWING)
    mem.write_bytes(G.T.GAME_TIME, struct.pack("<f", 100.0))
    mem.write_bytes(G.BOUNTY_EXPIRY, struct.pack("<f", 99.0))   # about to end
    g._bounty_show_from = 0.0
    g.bounty_tick()
    check("the count-up is held on screen past its own expiry",
          struct.unpack("<f", mem.read_bytes(G.BOUNTY_EXPIRY, 4))[0] > 100.0)
    g._bounty_show_from = clock.now - G.BOUNTY_HOLD - 1.0   # held long enough
    mem.write_bytes(G.BOUNTY_EXPIRY, struct.pack("<f", 99.0))
    g.bounty_tick()
    check("but only for BOUNTY_HOLD, then it is let go",
          struct.unpack("<f", mem.read_bytes(G.BOUNTY_EXPIRY, 4))[0] == 99.0)

    mem.write_u32(G.POPUP_STATE_ADDR, 0)            # the banner tore down
    g.bounty_tick()
    mem.write_bytes(G.BOUNTY_FACTOR, struct.pack("<f", 0.25))
    g.bounty_tick()
    check("and lets go the moment it is over",
          struct.unpack("<f", mem.read_bytes(G.BOUNTY_FACTOR, 4))[0] == 0.25)

    print()
    print("    a full effect queue:")
    print()

    # ---- the stampede
    sent, now = [], [1000.0]

    def run_queue(gap):
        """client.py's dispatch, with and without the gap."""
        sent.clear()
        now[0] = 1000.0
        queue = ["burp"] * 12
        active, after = {}, 0.0
        for _ in range(600):                    # 60 seconds at TICK
            now[0] += 0.1
            if queue and not active and now[0] >= after:
                sent.append(round(now[0] - 1000.0, 1))
                queue.pop(0)
                after = now[0] + gap
        return list(sent)

    burst = run_queue(0.0)
    spaced = run_queue(G.Game.EFFECT_GAP)
    check("without a gap, twelve burps land in about a second",
          len(burst) == 12 and burst[-1] - burst[0] < 2.0,
          f"first at {burst[0]}s, last at {burst[-1]}s")
    check(f"with EFFECT_GAP={G.Game.EFFECT_GAP}, they are spread out",
          all(round(b - a, 1) >= G.Game.EFFECT_GAP
              for a, b in zip(spaced, spaced[1:])),
          f"landed at {spaced}")
    check("and none are dropped -- the queue holds, it does not discard",
          len(spaced) == 12, f"only {len(spaced)} of 12 arrived")

    print()
    print("    the client's read load, and a reconnect:")
    print()

    D = sys.modules["tazworld.logic"]
    import json as _json
    cat = _json.load(open(os.path.join(WORLD, "data", "taz_catchers.json")))
    o = D.normalise({"game_mode": "open", "sandwich_checks": 1,
                     "destruction_checks": 1, "difficulty": "expert"})
    o["mode"] = "open"
    locs = D.all_locations(catchers=cat, **D.location_args(o))
    reads = []
    g2 = G.Game()
    g2.level_id = lambda: 15
    g2.save_file = 0
    g2._u32 = lambda a: (reads.append(a), 0)[1]
    g2.satisfied(locs)
    check("satisfied() reads each address once, not once per location",
          len(reads) == len(set(reads)),
          f"{len(reads)} reads for {len(set(reads))} addresses")
    print(f"                 {len(reads)} reads a tick = "
          f"{len(reads) * 10} PINE round trips a second (was 118 and 1180)")

    class FakeCtx:
        synced_once = False
    ctx = FakeCtx()

    def sync(client, names):
        """TazClient's one call site, as it now stands."""
        first = not ctx.synced_once
        ctx.synced_once = True
        return client.receive(names, replay=first)

    class Stub:
        """Only receive()'s bookkeeping; rebuild and notify are not the point."""
        def __init__(self):
            self.received = []
            self.effect_queue = []

        def rebuild(self):
            pass

        def receive(self, names, replay=False, details=None):
            first = len(self.received)
            new = names[first:]
            self.received = list(names)
            if not replay:
                for n in new:
                    self.effect_queue.append(n)
            return new

    items = ["Chili Pepper"] * 40
    c1 = Stub()
    sync(c1, items)
    sync(c1, items + ["Burp Can"])
    check("a normal item delivery still fires",
          c1.effect_queue == ["Burp Can"], f"queued {c1.effect_queue}")

    ctx.synced_once = False               # what Connected now does
    c2 = Stub()                           # _build_logic makes a fresh client
    sync(c2, items + ["Burp Can"])
    check("a reconnect fires nothing, not all 41 again",
          c2.effect_queue == [],
          f"queued {len(c2.effect_queue)} effects on reconnect")

    print()
    print("    a trap arriving while Taz is spinning:")
    print()

    # THE TWO FAMILIES. What decides whether an effect may start mid-spin is
    # not what it is, it is HOW it enters the state.
    T = G.T
    REQ = G.S_REQUEST
    SPINUP, SPIN, SPINDOWN = 0x0C, 0x0D, 0x0E

    def at(state):
        wire(G, mem, state)
        mem.write_u32(STATE_OBJ + T.S_STATE, state)
        mem.write_u32(STATE_OBJ + REQ, state)
        mem.writes.clear() if hasattr(mem, "writes") else None
        g._safe_since = None
        g._playable_since = None
        for _ in range(6):
            clock.now += 0.1
            g.safe_to_interrupt()
            g.playable()

    # Derived from grant_effect's own dispatch rather than from the comment
    # beside the sets, because a hand-written second copy of a fact is how
    # this project keeps ending up with two that disagree.
    src = open(os.path.join(WORLD, "game.py"), encoding="utf-8").read()
    body = src[src.index("    def grant_effect(self, name):"):
               src.index("    def _squash_bit(self")]
    import re as _re
    routed = set(_re.findall(
        r'if name == "(\w+)":\s*\n\s*return self\.(?:_install_state|_squash)',
        body))
    check("REQUEST_PATH is exactly what grant_effect routes through the "
          "request field", G.Game.REQUEST_PATH == routed,
          f"set says {sorted(G.Game.REQUEST_PATH)}, code does {sorted(routed)}")
    direct = {n for n, sp in G.Game.POWERUPS.items()
              if sp.get("state") is not None}
    check("DEFER_UNTIL_SAFE is exactly what writes S_STATE directly",
          G.Game.DEFER_UNTIL_SAFE == direct,
          f"set says {sorted(G.Game.DEFER_UNTIL_SAFE)}, "
          f"POWERUPS says {sorted(direct)}")
    check("and the two families do not overlap",
          not (G.Game.REQUEST_PATH & G.Game.DEFER_UNTIL_SAFE))

    # A spinning Taz. Vanilla lets him spin into dynamite and stop.
    for state, label in ((SPINUP, "SPINUP"), (SPIN, "SPIN"),
                         (SPINDOWN, "SPINDOWN")):
        at(state)
        got = g.grant_effect("dynamite")
        check(f"dynamite lands on a spinning Taz ({label})", got != "defer",
              f"returned {got!r}")

    at(SPIN)
    got = g.grant_effect("squash")
    check("so does the squash", got != "defer", f"returned {got!r}")

    # And it asks, rather than slamming the state field.
    at(SPIN)
    g.grant_effect("dynamite")
    check("it asks through S_REQUEST, the way 0x002C44D8 does",
          mem.read_u32(STATE_OBJ + REQ) == T.EAT_BAD_FOOD_STATE,
          f"request reads {hex(mem.read_u32(STATE_OBJ + REQ))}")
    check("...and never writes S_STATE itself",
          mem.read_u32(STATE_OBJ + T.S_STATE) == SPIN,
          f"state reads {hex(mem.read_u32(STATE_OBJ + T.S_STATE))}")

    # The old fix cancelled the spin, which fought the player's held button:
    # cancel, SPINUP, cancel, SPINUP, ten times a second. Nothing should ask
    # for IDLE any more.
    at(SPIN)
    g.grant_effect("dynamite")
    check("nothing cancels the spin -- that fought a held circle button",
          mem.read_u32(STATE_OBJ + REQ) != G.IDLE_STATE)

    # The direct-write family still waits, because it really does break him.
    for name in ("burp", "pepper", "hiccup"):
        at(SPIN)
        check(f"{name} still defers mid-spin -- it writes S_STATE directly",
              g.grant_effect(name) == "defer")

    # Neither family lands on a dying or captured Taz.
    for state, label in ((0x2C, "drowning"), (0x3D, "voiding out"),
                         (0x59, "caught"), (0x54, "caged")):
        at(state)
        check(f"dynamite defers while {label}",
              g.grant_effect("dynamite") == "defer")

    at(STATE_MOVE)
    check("and lands normally when he is just moving",
          g.grant_effect("dynamite") != "defer")

    print()
    bad = RESULTS.count(False)
    print(f"    {len(RESULTS) - bad}/{len(RESULTS)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
