#!/usr/bin/env python3
"""Rollercoaster: find out how the game says Taz is riding one, and what
dying on one actually does.

READ-ONLY. This tool never writes to memory. It can be run on a save you care
about.

WHY THIS EXISTS
---------------
Two notes need the same fact:

  * modifier items -- filler and traps -- must not fire while Taz is on a
    rollercoaster, because they break the ride. So the client needs a test for
    "he is on one", and it has to be true for the WHOLE ride.

  * dying on a rollercoaster does not send a void-out DeathLink. death_tick()
    reports a death by reading Taz's state and comparing it to the death
    states, so either the coaster death enters a state that is not in that
    list, or it enters no state at all -- the way dying as the ball in
    Taz: Haunted drops to 0x00 and stays.

WHAT THE DUMP ALREADY SAYS, AND WHAT IT DOES NOT
------------------------------------------------
The state name table at 0x00473BB0 has 96 entries. Number 77 is

    0x4D  STATE_ONMINECART

and the minecart is the rollercoaster: minecart.cpp loads rcoasterlp.wav and
rcoasterlp2.wav, which is a rollercoaster loop by any reading. The same file
owns these animations --

    minecartdrive  minecartdrivefast  minecartjumprise  minecartland
    minecarttumble  minecartflamedeath  minecartdrop
    minecartfallleft  minecartfallright        + minecartcrash.wav

-- so there are at least five distinct ways to die on it, and one system
serves both Cartoon Strip-Mine and Taz: Haunted.

That is where the reading stops. Nothing above proves the game writes 0x4D
into the field death_tick() reads, and nothing above says what that field
holds while Taz is tumbling out of a cart. Those are recordings, not
deductions, which is what this tool takes.

HOW TO USE IT
-------------
Close the AP client first -- only one thing at a time on PINE.

    py -3.13 taz_coaster.py live

Then, in Cartoon Strip-Mine:

  1. Walk around normally for a few seconds.       (the baseline)
  2. Get on the rollercoaster and ride it.         (answers note 2)
  3. Ride it again and DIE on it.                  (answers note 3)
  4. Ctrl-C.

Every change to Taz's state is printed as it happens and written to
taz_coaster.json. Send me that file.

Do the same in Taz: Haunted afterwards -- the two are probably one system,
but "probably" is what this project keeps getting burned by.

If `live` shows the state never changing while he rides, that is the useful
answer and not a failure: it means the tell is somewhere else, and

    py -3.13 taz_coaster.py record

is the follow-up. It watches whole windows of the Taz object and the state
object and prints every word that moved, so whatever the coaster actually
touches shows up without having to be guessed at first.
"""

import argparse
import importlib.util
import json
import os
import re
import socket
import struct
import sys
import time
import types

SLOT = 28011
READ8, READ32 = 0, 2

# Globals. Static across runs.
LEVEL_ID = 0x003FF048
GAME_STATE = 0x003FF040          # 1 = Active
TAZ_PTR = 0x003FF060
ACTOR_PTR = 0x003FF070

# Taz object.
O_POS = 0x0C0
O_ANIM_PTR = 0x134
O_STATE_PTR = 0x1C8
O_COSTUME_PTR = 0x1CC
O_ACTOR_FLAGS = 0x1F8

# State object. +0xB0 is the field the game itself tests and the field
# game.py's taz_state() reads, so it is the one that decides both notes.
S_STATE = 0x0B0
S_HANDLER = 0x108
S_REQUEST = 0x10C

STATE_ACTIVE = 1

# The two levels the note names. Others are accepted -- if a third level turns
# out to have a ride in it, that is worth knowing rather than filtering out.
COASTER_LEVELS = {15: "Cartoon Strip-Mine", 14: "Taz: Haunted"}

# States that would be a plausible "riding something" if 0x4D is not it. Only
# used to colour the output; the recording is of whatever actually appears.
RIDE_CANDIDATES = {
    0x4D: "ONMINECART -- the one the strings point at",
    0x19: "VEHICLE",
    0x31: "INTRANSPORT",
    0x15: "PROJECTILE",
    0x16: "PROJECTILESLIDE",
    0x17: "SWINGING",
    0x57: "VEHICLEWATERKOIK",
}

# Offsets already known by name, so a diff reads as fields rather than as a
# wall of numbers. Anything NOT listed here is the interesting part.
OFFSET_NOTES = {
    "taz": {0x0C0: "pos x", 0x0C4: "pos y", 0x0C8: "pos z",
            0x134: "anim ptr", 0x1C8: "state ptr", 0x1CC: "costume ptr",
            0x1F8: "actor flags"},
    "state": {0x0B0: "state id", 0x108: "handler", 0x10C: "request",
              0x200: "state (old offset)", 0x204: "echo"},
}

# What death_tick() currently reports on. A coaster death that never enters
# one of these is exactly the bug in note 3.
DEATH_STATES = {
    0x2C: "drown",
    0x2D: "void_out (fall)",
    0x3D: "void_out (void)",
    0x3E: "void_out (crush)",
    0x59: "captures",
    0x5A: "boss loss",
}

# Read from the pointer table at 0x00473BB0 in ee_dump.bin. All 96, so every
# id printed below is the game's own name for it.
STATE_NAMES = {
    0x00: "MOVE",
    0x01: "SKIDSTOP",
    0x02: "TIPTOE",
    0x03: "SLIDE",
    0x04: "GETUPFROMSLIDE",
    0x05: "GETUPFROMWATER",
    0x06: "BIGFALL",
    0x07: "SPLAT",
    0x08: "JUMP",
    0x09: "FALL",
    0x0A: "IDLE",
    0x0B: "BITE",
    0x0C: "SPINUP",
    0x0D: "SPIN",
    0x0E: "SPINDOWN",
    0x0F: "SPINDOWNONWATER",
    0x10: "RECOVER",
    0x11: "COLLECTPOSTCARD",
    0x12: "HOLDINGPOSTCARD",
    0x13: "DESTROYPOSTCARD",
    0x14: "KOIKFROMWATER",
    0x15: "PROJECTILE",
    0x16: "PROJECTILESLIDE",
    0x17: "SWINGING",
    0x18: "SPRUNG",
    0x19: "VEHICLE",
    0x1A: "TRAPPED",
    0x1B: "DEAD",
    0x1C: "DONOTHING",
    0x1D: "ELECTROCUTED",
    0x1E: "GROUNDELECTROCUTED",
    0x1F: "ONICE",
    0x20: "SPINONICE",
    0x21: "WATERSLIDE",
    0x22: "PLAYANIMATION",
    0x23: "SCARE",
    0x24: "ENTERINGPHONEBOX",
    0x25: "ONFOUNTAIN",
    0x26: "FRONTENDUSE",
    0x27: "GOTOSLEEP",
    0x28: "SLEEP",
    0x29: "DANCE",
    0x2A: "DEBUGMOVE",
    0x2B: "SQUASHED",
    0x2C: "CATATONIC",
    0x2D: "CATATONICPHYS",
    0x2E: "MOVESQUASHED",
    0x2F: "SHOCKED",
    0x30: "CATATONICDELAY",
    0x31: "INTRANSPORT",
    0x32: "ATLASSPINUP",
    0x33: "ATLASSPIN",
    0x34: "ATLASSPINDOWN",
    0x35: "ATLASSPHERES",
    0x36: "LOOKAROUND",
    0x37: "ENTERLOOKAROUND",
    0x38: "EAT",
    0x39: "SPIT",
    0x3A: "BUBBLEGUM",
    0x3B: "CHILLIPEPPER",
    0x3C: "FRONTEND",
    0x3D: "RUNON",
    0x3E: "INIT",
    0x3F: "NINJAKICK",
    0x40: "BURNT",
    0x41: "SKATECHARGE",
    0x42: "SPLATTED",
    0x43: "SPLATRECOVER",
    0x44: "SNOWBOARDATTACK",
    0x45: "SURFBOARDATTACK",
    0x46: "RAPPERATTACK",
    0x47: "WEREWOLFATTACK",
    0x48: "COWBOYSHOOT",
    0x49: "TAZANYELL",
    0x4A: "INDYROLL",
    0x4B: "CHEESYATTACK",
    0x4C: "MESMERISED",
    0x4D: "ONMINECART",
    0x4E: "INPORTAL",
    0x4F: "BADFOOD",
    0x50: "ENTERINGXDOOR",
    0x51: "MOUSE",
    0x52: "BALL",
    0x53: "CRATE",
    0x54: "CAGED",
    0x55: "CAGEDMOVE",
    0x56: "SMASH",
    0x57: "VEHICLEWATERKOIK",
    0x58: "KOIKFROMDEATHPLANE",
    0x59: "NET",
    0x5A: "PLAYANIMANDFREEZE",
    0x5B: "LOSECOSTUME",
    0x5C: "WAITFORTEXT",
    0x5D: "ZAPPEDINTOMOUSE",
    0x5E: "ZAPPEDINTOBALL",
    0x5F: "ZAPPEDINTOTAZ",
}


class Pine:
    """The same minimal PINE client the other taz_* tools use."""

    def __init__(self, slot=SLOT):
        self.slot, self.sock = slot, None

    def connect(self):
        if sys.platform == "win32":
            family, name = socket.AF_INET, ("127.0.0.1", self.slot)
        elif sys.platform == "darwin":
            family = socket.AF_UNIX
            name = os.environ.get("TMPDIR", "/tmp") + "/pcsx2.sock"
        else:
            family = socket.AF_UNIX
            name = os.environ.get("XDG_RUNTIME_DIR", "/tmp") + "/pcsx2.sock"
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(10.0)
        try:
            s.connect(name)
        except socket.error as e:
            s.close()
            raise SystemExit(f"    could not reach PCSX2 on {name!r}: {e}\n"
                             "    Is PCSX2 running with the game booted, and "
                             "is the AP client closed?")
        self.sock = s
        return self

    def _send(self, req):
        self.sock.sendall(req)
        want, buf = 4, b""
        while len(buf) < want:
            c = self.sock.recv(1 << 16)
            if not c:
                raise ConnectionError("PCSX2 closed the connection.")
            buf += c
            if want == 4 and len(buf) >= 4:
                want = int.from_bytes(buf[0:4], "little")
        if buf[4] == 0xFF:
            raise ConnectionError("PCSX2 reported a failure")
        return buf

    def _one(self, cmd, addr, extra=b""):
        body = bytes([cmd]) + addr.to_bytes(4, "little") + extra
        return self._send((len(body) + 4).to_bytes(4, "little") + body)

    def r32(self, a):
        return int.from_bytes(self._one(READ32, a)[5:9], "little")

    def many(self, addrs):
        if not addrs:
            return []
        body = b"".join(bytes([READ32]) + a.to_bytes(4, "little")
                        for a in addrs)
        r = self._send((len(body) + 4).to_bytes(4, "little") + body)
        return [int.from_bytes(r[5 + 4 * i:9 + 4 * i], "little")
                for i in range(len(addrs))]


# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")


def client_tick(default=0.1):
    """The client's poll interval, read out of client.py rather than copied.

    It matters more than it looks: a coaster death's MOVE frame lasts 0.02s,
    so at 0.1s the client misses it four times in five. Judging at this tool's
    own 50Hz would report deaths the real client would never see.
    """
    try:
        src = open(os.path.join(WORLD, "client.py"), encoding="utf-8").read()
        m = re.search(r"^TICK\s*=\s*([0-9.]+)", src, re.M)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return default


class Judge:
    """The world's own death_tick, fed from what this tool is already reading.

    Not a reimplementation of the rule. If game.py and this tool ever disagree
    about whether something was a death, this tool is wrong by definition --
    which is the entire point of loading the shipped file.

    The four memory reads are replaced because only one thing can hold PINE at
    a time and this tool is holding it. force_spin is stubbed to do nothing:
    game.py would write to the game, and this tool promises not to.
    """

    def __init__(self, G, game):
        self.G, self.g = G, game
        self.cur = {}
        game.level_id = lambda: self.cur.get("lid")
        game.game_state = lambda: self.cur.get("gs")
        game._state = lambda: self.cur.get("state")
        game.taz_state = lambda: self.cur.get("state")
        game.force_spin = lambda: False
        self.coaster_state = getattr(G, "COASTER_STATE", None)

    def tick(self, s):
        """One client poll. Returns the death kind, or None."""
        self.cur = s
        return self.g.death_tick()

    def held(self):
        """Would an incoming filler or trap be held right now?"""
        try:
            return bool(self.g.on_coaster())
        except Exception:
            return False


def load_judge():
    """Load the world's game.py, or explain why not.

    Returns (Judge, None) or (None, reason).
    """
    if not os.path.isdir(WORLD):
        return None, (f"no world at {WORLD} -- run this from the "
                      "Archipelago root, beside the other taz_ tools")
    try:
        pkg = types.ModuleType("tazworld")
        pkg.__path__ = [WORLD]
        sys.modules["tazworld"] = pkg
        for name in ("_imports", "logic", "game"):
            path = os.path.join(WORLD, name + ".py")
            spec = importlib.util.spec_from_file_location(
                "tazworld." + name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["tazworld." + name] = mod
            setattr(pkg, name, mod)
            spec.loader.exec_module(mod)
        G = sys.modules["tazworld.game"]
    except Exception as e:
        return None, f"could not load game.py: {e!r}"
    if not hasattr(G, "COASTER_STATE"):
        return None, ("this game.py has no COASTER_STATE -- it predates the "
                      "rollercoaster work")
    return Judge(G, G.Game()), None


def ee(v):
    return v is not None and 0x00100000 <= v < 0x02000000


def name_of(v):
    if v is None:
        return "-"
    return STATE_NAMES.get(v, "?")


def tag_of(v):
    """A short note on why an id is interesting, or "" if it is ordinary."""
    if v in DEATH_STATES:
        return f"   <== a death state: death_tick reports {DEATH_STATES[v]}"
    if v in RIDE_CANDIDATES:
        return f"   <== {RIDE_CANDIDATES[v]}"
    return ""


def f32(word):
    return struct.unpack("<f", word.to_bytes(4, "little"))[0]


def sample(p):
    """One reading of everything worth having, in three round trips.

    The pointers are re-read every time rather than cached. They move, and an
    address captured a moment ago is the oldest mistake in this project.
    """
    g = p.many([LEVEL_ID, GAME_STATE, TAZ_PTR, ACTOR_PTR])
    s = {"lid": g[0], "gs": g[1], "taz": g[2], "actor": g[3],
         "st": None, "state": None, "handler": None, "request": None,
         "pos": None, "anim": None, "flags": None, "costume_obj": None}
    if not ee(s["taz"]):
        return s

    t = p.many([s["taz"] + O_STATE_PTR, s["taz"] + O_COSTUME_PTR,
                s["taz"] + O_ANIM_PTR, s["taz"] + O_ACTOR_FLAGS,
                s["taz"] + O_POS, s["taz"] + O_POS + 4, s["taz"] + O_POS + 8])
    s["st"] = t[0]
    s["costume_obj"] = t[1]
    s["anim"] = t[2]
    s["flags"] = t[3]
    s["pos"] = (round(f32(t[4]), 1), round(f32(t[5]), 1), round(f32(t[6]), 1))
    if not ee(s["st"]):
        s["st"] = None
        return s

    v = p.many([s["st"] + S_STATE, s["st"] + S_HANDLER, s["st"] + S_REQUEST])
    s["state"] = v[0] & 0xFF
    s["handler"] = v[1]
    s["request"] = v[2] & 0xFF
    return s


def key(s):
    """The parts a transition is judged on. Position is deliberately not in
    here -- it changes every frame and would make every tick a transition."""
    return (s["lid"], s["gs"], s["st"], s["state"], s["handler"],
            s["request"], s["anim"], s["flags"])


def cmd_live(p, args):
    """Record Taz's state through a ride and through a death on one.

    Prints every transition as it happens and saves the lot. What matters in
    the output is two things: which id is held for the length of the ride, and
    which ids appear between the crash and the respawn.
    """
    print("    READ-ONLY. Nothing is written to the game.")
    print()
    print("    1. walk around normally for a few seconds  (the baseline)")
    print("    2. ride the rollercoaster                  (note 2)")
    print("    3. ride it again and die on it             (note 3)")
    print("    4. Ctrl-C")
    print()

    judge, why = (None, "turned off") if args.no_judge else load_judge()
    tick = args.client_tick if args.client_tick else client_tick()
    if judge:
        print(f"    judging with the world's own death_tick, polled every "
              f"{tick}s -- the rate client.py actually uses.")
        print("      >> DEATHLINK   a void-out would have been sent")
        print("      >> HELD        filler and traps are being withheld")
        print()
    else:
        print(f"    not judging ({why}). Still recording.")
        print()

    log, last, t0 = [], None, time.time()
    ride_ids = {}
    verdicts, held_now, held_since, held_total = [], False, None, 0.0
    left_cart_at, next_judge, judged_prev = None, 0.0, None
    try:
        while True:
            try:
                s = sample(p)
            except ConnectionError as e:
                print(f"    lost PCSX2: {e}")
                break

            now = time.time()
            if judge and now >= next_judge:
                next_judge = now + tick
                # When the cart ended, so a verdict can be timed against it.
                # Tracked on what the JUDGE saw, not on every 50Hz sample --
                # it has to be the same view the rule is being fed.
                if judged_prev == judge.coaster_state \
                        and s["state"] != judge.coaster_state:
                    left_cart_at = now
                if s["state"] == judge.coaster_state:
                    left_cart_at = None
                judged_prev = s["state"]
                kind = judge.tick(s)
                if kind:
                    since = (f", {now - left_cart_at:.2f}s after the cart"
                             if left_cart_at else "")
                    print(f"    [{now - t0:7.2f}] >> DEATHLINK would send: "
                          f"{kind}{since}")
                    verdicts.append({"t": round(now - t0, 2), "kind": kind})
                    left_cart_at = None
                h = judge.held()
                if h != held_now:
                    held_now = h
                    if h:
                        held_since = now
                        print(f"    [{now - t0:7.2f}] >> HELD -- filler and "
                              "traps are being withheld")
                    else:
                        was = now - (held_since or now)
                        held_total += was
                        print(f"    [{now - t0:7.2f}] >> released after "
                              f"{was:.1f}s")

            k = key(s)
            if k != last:
                dt = time.time() - t0
                bits = []
                if last is None or k[0] != last[0]:
                    lvl = COASTER_LEVELS.get(s["lid"], "")
                    bits.append(f"level {s['lid']}"
                                + (f" ({lvl})" if lvl else ""))
                if last is None or k[1] != last[1]:
                    bits.append("ACTIVE" if s["gs"] == STATE_ACTIVE
                                else f"game_state {s['gs']}")
                if last is None or k[2] != last[2]:
                    bits.append("state obj "
                                + (f"0x{s['st']:08X}" if s["st"] else "gone"))
                if last is None or k[3] != last[3]:
                    bits.append(f"state 0x{(s['state'] or 0):02X} "
                                f"{name_of(s['state'])}")
                if last is None or k[4] != last[4]:
                    bits.append(f"handler 0x{(s['handler'] or 0):08X}")
                if last is None or k[5] != last[5]:
                    bits.append(f"request 0x{(s['request'] or 0):02X} "
                                f"{name_of(s['request'])}")
                if last is None or k[6] != last[6]:
                    bits.append(f"anim 0x{(s['anim'] or 0):08X}")
                if last is None or k[7] != last[7]:
                    bits.append(f"flags 0x{(s['flags'] or 0):08X}")
                print(f"    [{dt:7.2f}] " + ",  ".join(bits)
                      + tag_of(s["state"]))
                rec = dict(s)
                rec["t"] = round(dt, 3)
                rec["state_name"] = name_of(s["state"])
                log.append(rec)
                last = k

            if s["state"] is not None:
                ride_ids[s["state"]] = ride_ids.get(s["state"], 0) + 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()

    if held_now and held_since:
        held_total += time.time() - held_since

    out = {"kind": "live", "seconds": round(time.time() - t0, 2),
           "interval": args.interval, "transitions": log,
           "judged": bool(judge), "client_tick": tick,
           "deathlink_verdicts": verdicts,
           "held_seconds": round(held_total, 1),
           "state_ticks": {f"0x{k:02X} {name_of(k)}": v
                           for k, v in sorted(ride_ids.items())}}
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)

    print(f"    {len(log)} transition(s) over "
          f"{out['seconds']:.1f}s -> {args.out}")
    print()
    print("    how long Taz spent in each state:")
    total = sum(ride_ids.values()) or 1
    for v, n in sorted(ride_ids.items(), key=lambda kv: -kv[1]):
        secs = n * args.interval
        print(f"      0x{v:02X}  {name_of(v):<24} {secs:6.1f}s "
              f"{100.0 * n / total:5.1f}%" + tag_of(v))
    print()
    ridden = [v for v in ride_ids if v in RIDE_CANDIDATES]
    if ridden:
        print("    a ride candidate was held: "
              + ", ".join(f"0x{v:02X} {name_of(v)}" for v in ridden))
        print("    if the long one there covers the whole ride, that is the")
        print("    test note 2 needs.")
    else:
        print("    no ride candidate appeared. Whatever marks the coaster is")
        print("    not Taz's state -- run `record` next; it looks at every")
        print("    word of both objects instead of at one field.")
    died = [v for v in ride_ids if v in DEATH_STATES]
    print("    death states seen: "
          + (", ".join(f"0x{v:02X} {name_of(v)}" for v in died)
             if died else "NONE -- which is note 3, recorded"))

    if judge:
        print()
        if verdicts:
            print(f"    DeathLink: {len(verdicts)} would have sent --")
            for v in verdicts:
                print(f"      [{v['t']:7.2f}]  {v['kind']}")
        else:
            print("    DeathLink: none would have sent.")
        print(f"    filler and traps were held for {held_total:.1f}s in total.")
        print()
        print("    Count those against the rides you actually died on. One")
        print("    missing, or one for a ride you survived, is the thing to")
        print("    tell me -- send the json either way.")
    return 0


def cmd_record(p, args):
    """The deep version: every word of both objects, diffed on each change.

    Run this if `live` says the state field is not the tell. It is the same
    method that found the despawn recipe: watch a real event and print what
    actually moved, rather than deciding in advance where to look.
    """
    s = sample(p)
    taz, st = s["taz"], s["st"]
    if not ee(taz) or not st:
        print("    no Taz object right now -- be in a level with control.")
        return 1
    span = args.span
    print(f"    taz 0x{taz:08X}  state 0x{st:08X}, "
          f"watching 0x{span:X} bytes of each")
    print("    READ-ONLY. Get on the coaster, then die on it. Ctrl-C to stop.")
    print()

    def window():
        return (p.many(list(range(taz, taz + span, 4))),
                p.many(list(range(st, st + span, 4))))

    # Record what moves during ORDINARY play first. Position, velocity and
    # animation timers change every frame no matter what Taz is doing, so
    # without this the diff at the interesting moment is a hundred lines of
    # churn with the one word that matters buried in it.
    churn = {"taz": set(), "state": set()}
    if args.baseline > 0:
        print(f"    baseline: walk around normally for {args.baseline:.0f}s "
              "and do NOT get on the coaster...")
        prev, tb = window(), time.time()
        while time.time() - tb < args.baseline:
            cur = window()
            for label, a, b in (("taz", prev[0], cur[0]),
                                ("state", prev[1], cur[1])):
                for i, (x, y) in enumerate(zip(a, b)):
                    if x != y:
                        churn[label].add(4 * i)
            prev = cur
            time.sleep(args.interval)
        print(f"    {len(churn['taz'])} word(s) of the Taz object and "
              f"{len(churn['state'])} of the state object churn on their own; "
              "those are marked below.")
        print()

    base = window()
    base_state = s["state"]
    t0, log = time.time(), []
    try:
        while True:
            cur = sample(p)
            # A pointer that moved invalidates the comparison entirely, so
            # start over rather than diff two different objects.
            if cur["taz"] != taz or cur["st"] != st:
                if not ee(cur["taz"]) or not cur["st"]:
                    time.sleep(0.1)
                    continue
                taz, st = cur["taz"], cur["st"]
                base, base_state = window(), cur["state"]
                print(f"    [{time.time() - t0:7.2f}] objects moved -- "
                      f"taz 0x{taz:08X} state 0x{st:08X}, rebaselined")
                continue
            if cur["state"] != base_state:
                after = window()
                dt = time.time() - t0
                print(f"    [{dt:7.2f}] state 0x{(base_state or 0):02X} "
                      f"{name_of(base_state)} -> 0x{(cur['state'] or 0):02X} "
                      f"{name_of(cur['state'])}" + tag_of(cur["state"]))
                moved = []
                for label, a, b in (("taz", base[0], after[0]),
                                    ("state", base[1], after[1])):
                    for i, (x, y) in enumerate(zip(a, b)):
                        if x == y:
                            continue
                        off = 4 * i
                        note = OFFSET_NOTES[label].get(off, "")
                        if off in churn[label]:
                            note = (note + ", churns anyway").lstrip(", ")
                        moved.append({"obj": label, "off": off,
                                      "from": x, "to": y, "note": note})
                        print(f"      {label:<5} +0x{off:03X}  "
                              f"0x{x:08X} -> 0x{y:08X}"
                              + (f"   ({note})" if note else "   <--"))
                log.append({"t": round(dt, 3),
                            "from": base_state, "to": cur["state"],
                            "from_name": name_of(base_state),
                            "to_name": name_of(cur["state"]),
                            "moved": moved})
                print()
                base, base_state = after, cur["state"]
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()

    with open(args.out, "w") as fh:
        json.dump({"kind": "record", "span": span,
                   "seconds": round(time.time() - t0, 2),
                   "baseline_churn": {k: sorted(v) for k, v in churn.items()},
                   "transitions": log}, fh, indent=1)
    print(f"    {len(log)} transition(s) -> {args.out}")

    # The short list: words that moved at a transition, are not already known
    # by name, and do not move on their own. If the coaster marks itself
    # anywhere other than the state id, it is one of these.
    cand = {}
    for t in log:
        for m in t["moved"]:
            if m["note"]:
                continue
            cand.setdefault((m["obj"], m["off"]), 0)
            cand[(m["obj"], m["off"])] += 1
    if cand:
        print()
        print("    unexplained words -- neither named nor ordinary churn:")
        for (obj, off), n in sorted(cand.items(), key=lambda kv: -kv[1]):
            print(f"      {obj:<5} +0x{off:03X}   moved at {n} transition(s)")
    return 0


def cmd_show(p, args):
    """One reading, for checking the tool is talking to the game at all."""
    s = sample(p)
    lvl = COASTER_LEVELS.get(s["lid"], "")
    print(f"    level {s['lid']}" + (f"  ({lvl})" if lvl else ""))
    print(f"    game_state {s['gs']}"
          + ("  ACTIVE" if s["gs"] == STATE_ACTIVE else "  not active"))
    print(f"    taz   0x{(s['taz'] or 0):08X}")
    if not s["st"]:
        print("    no state object -- in a menu or loading.")
        return 1
    print(f"    state 0x{s['st']:08X}")
    print(f"      +0x0B0 state    0x{s['state']:02X}  {name_of(s['state'])}"
          + tag_of(s["state"]))
    print(f"      +0x108 handler  0x{s['handler']:08X}")
    print(f"      +0x10C request  0x{s['request']:02X}  "
          f"{name_of(s['request'])}")
    print(f"    pos   {s['pos']}")
    print(f"    anim  0x{(s['anim'] or 0):08X}   "
          f"flags 0x{(s['flags'] or 0):08X}")
    return 0


def cmd_states(p, args):
    """All 96 state names, straight from the table at 0x00473BB0."""
    print("    every Taz state the game has a name for:")
    for v, n in sorted(STATE_NAMES.items()):
        print(f"      0x{v:02X}  {n}{tag_of(v)}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="taz_coaster.json",
                    help="where the recording is written")
    ap.add_argument("--interval", type=float, default=0.02,
                    help="seconds between samples; a coaster death is quick")
    ap.add_argument("--no-judge", action="store_true",
                    help="record only; do not load the world's death_tick")
    ap.add_argument("--client-tick", type=float, default=0.0,
                    help="override the poll rate the judge is driven at; "
                         "0 reads TICK out of client.py")
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("live", help="record a ride and a death").set_defaults(
        fn=cmd_live)
    sub.add_parser("show", help="one reading").set_defaults(fn=cmd_show)
    sub.add_parser("states", help="all 96 state names").set_defaults(
        fn=cmd_states)

    rec = sub.add_parser("record", help="diff whole objects on every change")
    rec.add_argument("--span", type=lambda x: int(x, 0), default=0x280,
                     help="bytes of each object to watch")
    rec.add_argument("--baseline", type=float, default=8.0,
                     help="seconds of ordinary play first, to learn which "
                          "words move on their own; 0 to skip")
    rec.set_defaults(fn=cmd_record)

    args = ap.parse_args()
    if args.verb == "states":
        return args.fn(None, args)
    return args.fn(Pine().connect(), args)


if __name__ == "__main__":
    raise SystemExit(main())
