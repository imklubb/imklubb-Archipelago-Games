#!/usr/bin/env python3
"""Taz eating dynamite: find the trigger by recording it, then reproduce it.

What the dump says.

  0x0024B8D0 plays "runeat2", rolls a random number, and then branches:

      if (rand > 0.5)  play "tntinside", sound "muffledexplode.wav"
      else             play "badfood"

  which is the two animations. It has no callers anywhere -- it is a handler,
  installed by

      0x002C44D8(obj, 0x4F, 0x0024B8D0)
          state = [obj + 0x1C8]          <- O_STATE_PTR, the one game.py uses
          if (!state) return
          if (!0x002C4110(obj, 0x4F)) return
          [state + 0x108] = handler

  and 0x4F is a Taz STATE id, in the same enum as MOUSE 0x51, BALL 0x52,
  CAUGHT 0x59 and BOSS LOSS 0x5A -- 0x52 and 0x5A both register handlers the
  same way. The client already drives this object: S_REQUEST at +0x10C is
  where it writes SPIN_REQUEST to get Taz spinning again.

  So the handler word sits at +0x108 and the request at +0x10C, four bytes
  apart, and the game's own registration writes both.

The hypothesis is therefore two u32 writes and no injected code. `watch` is
what proves it: eat dynamite for real and see exactly what the game does to
those fields, rather than trusting the reasoning above.

    py -3.13 taz_dynamite.py watch      record a real dynamite eat
    py -3.13 taz_dynamite.py show       one look at the state object
    py -3.13 taz_dynamite.py fire       try it: handler + request
    py -3.13 taz_dynamite.py fire --request-only    request without the handler

Save state first. Be in a level with control of Taz.
"""

import argparse
import os
import socket
import struct
import sys
import time

SLOT = 28011
READ8, READ32, WRITE32 = 0, 2, 6

TAZ_PTR = 0x003FF060
O_STATE_PTR = 0x1C8

# The state the game itself tests. 0x002C4110 reads [state+0xB0] to decide
# whether a transition is legal, and 0x001D19F4 compares that same field to
# 0x59 to ask 'is Taz caught'. game.py's S_STATE at +0x200 is a different
# field -- both are reported below so they can be compared live.
S_ID = 0xB0              # the authoritative state id
S_STATE = 0x200          # what game.py currently calls the state
S_STATE_ECHO = 0x204
S_HANDLER = 0x108        # what 0x002C44D8 writes
S_REQUEST = 0x10C        # the state Taz is ASKING for

# The player actor the squash code drives. Not TAZ_PTR at 0x003FF060 -- the
# net and the squash both read 0x003FF070 instead.
ACTOR_PTR = 0x003FF070
O_FLAGS_1F8 = 0x1F8
SQUASH_BIT = 0x40

# SQUASHTAZ  (0x00275430): state 0x2E, play "recover21a", CLEAR bit 0x40
# UNSQUASHTAZ(0x002754A8): SET bit 0x40, and that is the entire recovery --
# it is what lets him pop back out. Setting the state alone leaves him flat
# forever because nothing ever puts the bit back.
STATE_MOVESQUASHED = 0x2E

EAT_BAD_FOOD = 0x0024B8D0
STATE_EAT_BAD = 0x4F
STATE_CAUGHT = 0x59

# What a REAL capture changes in the state object, apart from the transform
# floats the catcher moves Taz with. Recorded, not guessed. The handler at
# +0x108 and the request at +0x10C do NOT change -- 0x002C4110 writes the
# state id straight into +0xB0, so the request path was never involved.
CAUGHT_SETUP = [
    (0x0B0, 0x00000059, "state id"),
    (0x0B8, 0x00000000, "flag, 1 -> 0"),
    (0x11C, 0x00000000, "1.0832 -> 0"),
    (0x098, 0x00000000, "cleared"),
    (0x09C, 0x00000000, "cleared"),
]

# The game's own state enum, read from the pointer table at 0x00473BB0.
# 96 entries, so every id below is the game's name for it -- no guessing.
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

# Handlers the game installs with 0x002C44D8(taz, id, fn). Writing the
# handler to +0x108 and the id to +0x10C is what the registration does,
# and it is the whole of the dynamite trap.
STATE_HANDLERS = {
    0x10: 0x00214FE0,   # RECOVER
    0x15: 0x00177180,   # PROJECTILE
    0x16: 0x001597E8,   # PROJECTILESLIDE
    0x18: 0x0021FCF0,   # SPRUNG
    0x1D: 0x001DF550,   # ELECTROCUTED
    0x20: 0x00218E88,   # SPINONICE
    0x21: 0x00220480,   # WATERSLIDE
    0x22: 0x002765F8,   # PLAYANIMATION
    0x31: 0x00203108,   # INTRANSPORT
    0x38: 0x00189488,   # EAT
    0x3A: 0x0024B7C0,   # BUBBLEGUM
    0x40: 0x00219090,   # BURNT
    0x42: 0x00220AD8,   # SPLATTED
    0x4F: 0x0024B8D0,   # BADFOOD
    0x52: 0x002268B8,   # BALL
    0x5A: 0x00184A78,   # PLAYANIMANDFREEZE
    0x5D: 0x002269C0,   # ZAPPEDINTOMOUSE
    0x5F: 0x00226AC8,   # ZAPPEDINTOTAZ
}


class Pine:
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
                             "    Is the AP client still open?")
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

    def r8(self, a):
        return self._one(READ8, a)[5]

    def w32(self, a, v):
        self._one(WRITE32, a, (v & 0xFFFFFFFF).to_bytes(4, "little"))

    def many(self, addrs):
        body = b"".join(bytes([READ32]) + a.to_bytes(4, "little") for a in addrs)
        r = self._send((len(body) + 4).to_bytes(4, "little") + body)
        return [int.from_bytes(r[5 + 4 * i:9 + 4 * i], "little")
                for i in range(len(addrs))]


def ee(v):
    return 0x00100000 <= v < 0x02000000


def state_obj(p):
    taz = p.r32(TAZ_PTR)
    if not ee(taz):
        return None, None
    st = p.r32(taz + O_STATE_PTR)
    return (taz, st) if ee(st) else (taz, None)


def name_of(v):
    return STATE_NAMES.get(v, "?")


def snap(p, st):
    a = p.many([st + S_ID, st + S_STATE, st + S_STATE_ECHO,
                st + S_HANDLER, st + S_REQUEST])
    return {"id": a[0] & 0xFF, "state": a[1] & 0xFF, "echo": a[2] & 0xFF,
            "handler": a[3], "request": a[4] & 0xFF}


def cmd_show(p, args):
    taz, st = state_obj(p)
    if not st:
        print("    no Taz state object right now -- be in a level.")
        return 1
    print(f"    taz 0x{taz:08X}   state object 0x{st:08X}")
    s = snap(p, st)
    print(f"      +0x0B0 id       0x{s['id']:02X}  {name_of(s['id'])}"
          "   <- the one the game tests")
    print(f"      +0x200 state    0x{s['state']:02X}  {name_of(s['state'])}")
    print(f"      +0x204 echo     0x{s['echo']:02X}")
    print(f"      +0x108 handler  0x{s['handler']:08X}"
          + ("   <== eat_bad_food" if s["handler"] == EAT_BAD_FOOD else ""))
    print(f"      +0x10C request  0x{s['request']:02X}  {name_of(s['request'])}")
    print()
    print("    words around the handler slot:")
    for o in range(0xF8, 0x120, 4):
        v = p.r32(st + o)
        mark = ""
        if o == S_HANDLER:
            mark = "   <- handler"
        elif o == S_REQUEST:
            mark = "   <- request"
        print(f"      +0x{o:03X}  0x{v:08X}{mark}")
    return 0


def cmd_watch(p, args):
    """Record what a real dynamite eat does to the state object.

    This is the part that matters. Go find dynamite and eat it; every change
    to the four fields is printed with a timestamp, so the actual sequence
    the game performs is on the screen instead of inferred from the code.
    """
    taz, st = state_obj(p)
    if not st:
        print("    no Taz state object -- be in a level.")
        return 1
    print(f"    state object 0x{st:08X}. Go eat dynamite. Ctrl-C to stop.")
    print("    (also worth doing once WITHOUT eating, to see the idle churn)")
    print()
    last, t0, seen = None, time.time(), []
    try:
        while True:
            taz2, st2 = state_obj(p)
            if st2 != st:
                st = st2
                print(f"    [{time.time() - t0:7.2f}] state object moved to "
                      f"0x{(st or 0):08X}")
                last = None
                if not st:
                    time.sleep(0.1)
                    continue
            s = snap(p, st)
            if s != last:
                bits = []
                if last is None or s["id"] != last["id"]:
                    bits.append(f"id 0x{s['id']:02X} {name_of(s['id'])}")
                if last is None or s["state"] != last["state"]:
                    bits.append(f"state 0x{s['state']:02X} {name_of(s['state'])}")
                if last is None or s["request"] != last["request"]:
                    bits.append(f"request 0x{s['request']:02X} "
                                f"{name_of(s['request'])}")
                if last is None or s["handler"] != last["handler"]:
                    tag = " (eat_bad_food)" if s["handler"] == EAT_BAD_FOOD else ""
                    bits.append(f"handler 0x{s['handler']:08X}{tag}")
                if bits:
                    line = f"    [{time.time() - t0:7.2f}]  " + "   ".join(bits)
                    print(line)
                    seen.append((round(time.time() - t0, 2), dict(s)))
                last = s
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    print()
    hits = [x for x in seen if x[1]["handler"] == EAT_BAD_FOOD
            or STATE_EAT_BAD in (x[1]["state"], x[1]["id"])]
    if hits:
        print(f"    Saw state 0x{STATE_EAT_BAD:02X} or the eat_bad_food handler "
              f"{len(hits)} time(s):")
        for t, s in hits[:10]:
            print(f"      t+{t:6.2f}  id 0x{s['id']:02X}  "
                  f"state 0x{s['state']:02X}  request 0x{s['request']:02X}  "
                  f"handler 0x{s['handler']:08X}")
        print()
        print("    That is the sequence to reproduce. If the handler was")
        print("    written before the state changed, `fire` has the order right.")
    else:
        print(f"    Never saw state 0x{STATE_EAT_BAD:02X} or the handler.")
        print("    Either dynamite was not eaten, or 0x4F is reached some")
        print("    other way -- send me the log above either way.")
    return 0


def cmd_fire(p, args):
    """Write what 0x002C44D8 writes, in the same order.

    The registration sets the handler and changes state. We ask through the
    request field instead of writing the state directly, because the game
    drives the state every frame -- the request is the input it reads, which
    is the same reason the spin recovery works that way.
    """
    taz, st = state_obj(p)
    if not st:
        print("    no Taz state object -- be in a level.")
        return 1
    before = snap(p, st)
    print(f"    state object 0x{st:08X}")
    print(f"      before: state 0x{before['state']:02X} "
          f"({name_of(before['state'])})  request 0x{before['request']:02X}  "
          f"handler 0x{before['handler']:08X}")

    if args.direct:
        p.w32(st + S_ID, args.state)
        print(f"      +0x0B0 id <- 0x{args.state:02X}  (the way 0x002C4110 does it)")
        args.request_only = True
    handler = args.handler
    if handler is None:
        handler = STATE_HANDLERS.get(args.state)
    if not args.request_only and handler:
        args.handler = handler
        p.w32(st + S_HANDLER, handler)
        got = p.r32(st + S_HANDLER)
        print(f"      handler <- 0x{handler:08X} "
              f"({STATE_NAMES.get(args.state, chr(63))})  "
              f"{'ok' if got == handler else 'DID NOT STICK (0x%08X)' % got}")
    p.w32(st + S_REQUEST, args.state)
    print(f"      request <- 0x{args.state:02X}")
    print()

    t0 = time.time()
    last = None
    while time.time() - t0 < args.hold:
        s = snap(p, st)
        if s != last:
            print(f"      [{time.time() - t0:5.2f}]  state 0x{s['state']:02X} "
                  f"{name_of(s['state']):14s} request 0x{s['request']:02X}  "
                  f"handler 0x{s['handler']:08X}")
            last = s
        time.sleep(0.02)
    print()
    consumed = last is not None and last["request"] != args.state
    cleared = last is not None and last["handler"] == 0
    if consumed and cleared:
        print("    The game took the request and cleared both fields, which is")
        print("    what a state that ran and finished looks like. The +0x200")
        print("    field may never show it -- these states can come and go")
        print("    inside a frame. Trust the screen over this trace.")
    elif last and last["state"] == args.state:
        print("    Taz is STILL in that state. If nothing happened on screen it")
        print("    wants more than the request -- send me the trace.")
    elif not consumed:
        print("    The request was never picked up. Try again while Taz is idle")
        print("    and on the ground: 0x002C4110 validates the change and can")
        print("    refuse it outright.")
    return 0


def cmd_caught(p, args):
    """Enter the caught state the way the game does: straight into +0xB0.

    A real capture never touches the request field. 0x002C4110 writes the id
    into +0xB0 itself, so asking through +0x10C -- which is what made Taz
    float -- was the wrong lever entirely.

    The extra writes are the other non-transform changes the recording caught.
    Bisect them with --id-only if the full set behaves oddly; the positions
    are the zookeeper carrying him and are his to set, not ours.
    """
    taz, st = state_obj(p)
    if not st:
        print("    no Taz state object -- be in a level.")
        return 1
    writes = CAUGHT_SETUP[:1] if args.id_only else CAUGHT_SETUP
    print(f"    state object 0x{st:08X}")
    for off, val, why in writes:
        was = p.r32(st + off)
        p.w32(st + off, val)
        print(f"      +0x{off:03X}  0x{was:08X} -> 0x{p.r32(st + off):08X}   {why}")
    print()
    t0, last = time.time(), None
    while time.time() - t0 < args.hold:
        cur = p.r32(st + S_ID) & 0xFF
        if args.reassert and cur != STATE_CAUGHT and time.time() - t0 < 0.5:
            p.w32(st + CAUGHT_SETUP[0][0], STATE_CAUGHT)
        s = snap(p, st)
        if s != last:
            print(f"      [{time.time() - t0:5.2f}]  id 0x{s['id']:02X} "
                  f"{name_of(s['id']):10s} state 0x{s['state']:02X}  "
                  f"request 0x{s['request']:02X}")
            last = s
        time.sleep(0.02)
    print()
    if last and last["id"] == STATE_CAUGHT:
        print("    Still caught. If the zookeeper sequence is not playing, the")
        print("    state is Taz's half only and the catcher drives the rest.")
    elif last and last["id"] != STATE_CAUGHT:
        print("    The state moved on by itself, which is what a sequence that")
        print("    ran and finished looks like. Watch the screen, not this.")
    return 0


def cmd_record(p, args):
    """Record what a REAL event does to Taz, field by field.

    Catching is driven by the catcher, not by Taz -- 0x00170AC0 is a virtual
    method on the catcher (vtable 0x00364874) that calls 0x002C4110(taz, 0x59)
    and then does a great deal more. Asking for the state alone is why Taz
    floats: he enters it with nothing driving him.

    So rather than guess at the rest, this watches a window of both objects,
    waits for the state id to change, and prints every word that moved. Go get
    caught by a zookeeper for real.
    """
    taz, st = state_obj(p)
    if not st:
        print("    no Taz state object -- be in a level.")
        return 1
    span = args.span
    print(f"    taz 0x{taz:08X}  state 0x{st:08X}, watching 0x{span:X} bytes "
          f"of each")
    print(f"    waiting for the state id at +0x{S_ID:02X} to leave "
          f"0x{p.r32(st + S_ID) & 0xFF:02X}. Go get caught. Ctrl-C to stop.")
    print()

    def window():
        return (p.many(list(range(taz, taz + span, 4))),
                p.many(list(range(st, st + span, 4))))

    base_id = p.r32(st + S_ID) & 0xFF
    base = window()
    t0 = time.time()
    try:
        while time.time() - t0 < args.wait:
            now = p.r32(st + S_ID) & 0xFF
            if now != base_id:
                after = window()
                print(f"    [{time.time() - t0:6.2f}] state id "
                      f"0x{base_id:02X} -> 0x{now:02X} ({name_of(now)})")
                for label, o, a, b in (("taz  ", taz, base[0], after[0]),
                                       ("state", st, base[1], after[1])):
                    for i, (x, y) in enumerate(zip(a, b)):
                        if x != y:
                            print(f"      {label} +0x{4 * i:03X}  "
                                  f"0x{x:08X} -> 0x{y:08X}")
                print()
                base_id, base = now, after
                if now == 0x59:
                    print("    That is the caught state. Everything above is")
                    print("    what the catcher set up -- send me this.")
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    print("    stopped")
    return 0


def cmd_crush(p, args):
    """Squash Taz, let him waddle, then let him pop back out.

    The game does it in two halves and only the first is a state. SQUASHTAZ
    sets STATE_MOVESQUASHED and clears bit 0x40 of [actor + 0x1F8];
    UNSQUASHTAZ does nothing except put that bit back. Without the second
    half he stays flat, which is what you saw.
    """
    actor = p.r32(ACTOR_PTR)
    if not ee(actor):
        print(f"    [0x{ACTOR_PTR:08X}] is not an object -- be in a level.")
        return 1
    taz, st = state_obj(p)
    if not st:
        print("    no Taz state object.")
        return 1
    fa = actor + O_FLAGS_1F8
    was = p.r32(fa)
    print(f"    actor 0x{actor:08X}   +0x1F8 0x{was:08X} "
          f"(bit 0x40 {'set' if was & SQUASH_BIT else 'clear'})")

    p.w32(fa, was & ~SQUASH_BIT)
    p.w32(st + S_HANDLER, 0)
    p.w32(st + S_REQUEST, args.state)
    print(f"    squashed: bit cleared, state 0x{args.state:02X} "
          f"({name_of(args.state)}) requested")
    print(f"    walking around for {args.seconds}s...")
    t0, last = time.time(), None
    while time.time() - t0 < args.seconds:
        v = p.r32(st + S_ID) & 0xFF
        if v != last:
            print(f"      [{time.time() - t0:5.2f}]  state 0x{v:02X} "
                  f"{name_of(v)}")
            last = v
        time.sleep(0.05)

    now = p.r32(fa)
    p.w32(fa, now | SQUASH_BIT)
    print(f"    unsquashed: +0x1F8 0x{now:08X} -> 0x{p.r32(fa):08X}")
    t0, last = time.time(), None
    while time.time() - t0 < 4.0:
        v = p.r32(st + S_ID) & 0xFF
        if v != last:
            print(f"      [{time.time() - t0:5.2f}]  state 0x{v:02X} "
                  f"{name_of(v)}")
            last = v
        time.sleep(0.05)
    return 0


def cmd_deaths(p, args):
    """Try each death state in turn, straight into +0xB0.

    Unlike the caught state, drowning and the ordinary deaths have no second
    actor -- there is no zookeeper to carry Taz anywhere, so whatever they do
    they should do on their own. That makes them a far better fit for
    DeathLink than a capture that structurally needs a keeper standing there.
    """
    taz, st = state_obj(p)
    if not st:
        print("    no Taz state object -- be in a level.")
        return 1
    for sid in args.states:
        try:
            input(f"    about to write 0x{sid:02X} ({name_of(sid)}). Enter... ")
        except EOFError:
            return 1
        was = p.r32(st + S_ID) & 0xFF
        p.w32(st + S_ID, sid)
        t0, last, seen = time.time(), None, []
        while time.time() - t0 < args.hold:
            v = p.r32(st + S_ID) & 0xFF
            if v != last:
                seen.append((round(time.time() - t0, 2), v))
                print(f"      [{time.time() - t0:5.2f}]  0x{v:02X} "
                      f"{name_of(v)}")
                last = v
            time.sleep(0.02)
        held = sum(1 for _, v in seen if v == sid)
        print(f"      from 0x{was:02X}: {'took' if held else 'refused'}, "
              f"{len(seen)} transition(s)")
        print()
    print("    Whichever of these played a full death and put Taz back is the")
    print("    one DeathLink wants.")
    return 0


def cmd_states(p, args):
    """Every state id worth trying, and what is known about each.

    `fire --state N` works for any of them, so this doubles as a menu. The
    handler at +0x108 only matters for states whose behaviour is installed
    rather than built in, which is what INSTALLED_STATES lists.
    """
    print("    known Taz states:")
    for v, n in sorted(STATE_NAMES.items()):
        mark = ("   handler 0x%08X" % STATE_HANDLERS[v]) if v in STATE_HANDLERS else ""
        print(f"      0x{v:02X}  {n}{mark}")
    print()
    print("    try one with, for example:")
    print("      taz_dynamite.py fire --state 0x59 --request-only")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("show").set_defaults(fn=cmd_show)
    sub.add_parser("watch").set_defaults(fn=cmd_watch)
    sub.add_parser("states").set_defaults(fn=cmd_states)

    cr = sub.add_parser("crush")
    cr.add_argument("--seconds", type=float, default=6.0,
                    help="how long he stays flat before popping back")
    cr.add_argument("--state", type=lambda x: int(x, 0),
                    default=STATE_MOVESQUASHED,
                    help="0x2E MOVESQUASHED, or 0x2B SQUASHED")
    cr.set_defaults(fn=cmd_crush)

    dz = sub.add_parser("deaths")
    dz.add_argument("--states", type=lambda x: int(x, 0), nargs="*",
                    default=[0x2C, 0x2D, 0x3D, 0x3E])
    dz.add_argument("--hold", type=float, default=8.0)
    dz.set_defaults(fn=cmd_deaths)

    c = sub.add_parser("caught")
    c.add_argument("--id-only", action="store_true",
                   help="write only +0xB0, none of the other recorded fields")
    c.add_argument("--reassert", action="store_true",
                   help="keep writing the id for half a second if it reverts")
    c.add_argument("--hold", type=float, default=8.0)
    c.set_defaults(fn=cmd_caught)

    r = sub.add_parser("record")
    r.add_argument("--span", type=lambda x: int(x, 0), default=0x300)
    r.add_argument("--wait", type=float, default=300.0)
    r.set_defaults(fn=cmd_record)

    f = sub.add_parser("fire")
    f.add_argument("--state", type=lambda x: int(x, 0), default=STATE_EAT_BAD)
    f.add_argument("--handler", type=lambda x: int(x, 0), default=None,
                   help="defaults to the handler the game installs for --state")
    f.add_argument("--request-only", action="store_true",
                   help="ask for the state without installing the handler")
    f.add_argument("--direct", action="store_true",
                   help="write the id straight into +0xB0, as 0x002C4110 does")
    f.add_argument("--hold", type=float, default=6.0)
    f.set_defaults(fn=cmd_fire)

    args = ap.parse_args()
    return args.fn(Pine().connect(), args)


if __name__ == "__main__":
    sys.exit(main())
