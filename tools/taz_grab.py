#!/usr/bin/env python3
"""Make a zookeeper catch Taz, without a zookeeper actually catching him.

Two things were wrong in the earlier attempts, and the second was mine.

  * Requesting state 0x59 through +0x10C -- a real capture never touches
    that field, so Taz entered the state through a door the game does not
    use, and floated.
  * Writing 0x59 straight into +0xB0 -- the right field, and he holds it for
    about three and a half seconds, but that is only Taz's half.
  * Calling 0x00170AC0(catcher, some_object) -- 0x00336238 turns out to be
    strcmp, so the second argument is a STRING. My offset analysis counted
    fields read through $s0 after it had been reassigned, and concluded it
    was a big object. It is not.

What 0x00170AC0 actually is: the zookeeper's animation-event callback,
installed per instance by 0x00164438 at [[keeper + 0x1D8] + 0x154]. It is

    on_event(keeper, name)

and it strcmps `name` against "attack1", then "keeperhit". The keeperhit
branch is the capture: it reads Taz from the global at 0x003FF060, puts him
in state 0x59, and goes on to the carry and the cage. The source file is
C:/Taz/Source/zookeeper.cpp.

So there is no context object to find. One scan for zookeepers, one call
through the trampoline with the game's own string address.

    py -3.13 taz_grab.py scan             find the zookeepers
    py -3.13 taz_grab.py grab             fire "keeperhit" at the first one
    py -3.13 taz_grab.py grab --list      the other event names
    py -3.13 taz_grab.py grab --catcher 0x... --event trapped

SAVE STATE FIRST.
"""

import argparse
import json
import os
import socket
import struct
import sys
import time

SLOT = 28011
READ32, WRITE32 = 2, 6
PER_REQ = 8192
EE_LO, EE_HI = 0x00100000, 0x02000000

TAZ_PTR = 0x003FF060
O_STATE_PTR = 0x1C8
S_ID = 0xB0
STATE_CAUGHT = 0x59
O_ANIM_PTR = 0x134
TAZ_XFORM = 0x1C0           # [[taz+0x1C0]+0x10] is the height gate's right side
GATE_MARGIN = 112.5         # 0x42E10000, the slack the keeper is allowed

# 0x00162E98 is a jump table at 0x00494F20 indexed by Taz's state id: these
# are the states a capture is allowed to start from. Read out of the dump.
ALLOWED_STATES = {
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D,
    0x0E, 0x0F, 0x1F, 0x20, 0x22, 0x23, 0x27, 0x28, 0x29, 0x36, 0x37, 0x3B,
    0x3F, 0x40, 0x41, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x4B, 0x4F,
}

GRAB_FN = 0x00170AC0
GRAB_SLOT = 0x154           # inside [catcher + 0x1D8]
SUB_PTR = 0x1D8
# The second argument is a STRING, not an object. 0x00336238 is strcmp, and
# 0x00170AC0 is the zookeeper's animation-event callback -- on_event(self,
# name). The caught state is reached from the branch guarded by "keeperhit",
# and Taz comes from the global at 0x003FF060, so no context object is
# needed at all. These are the game's own string addresses; pass them straight in.
EVENTS = {
    "keeperhit": 0x00496A68,          # -> the caught state
    "attack1": 0x00496840,
    "trapped": 0x00496A88,
    "netidle": 0x00496A60,
    "dragonground2": 0x00496A50,
    "fromdraggedtostanding": 0x00496A38,
    "attack2fail": 0x00496A20,
}

# Must match notify.py exactly -- same trampoline, same control block.
TICK = 0x002C5838
CALL_SITES = (0x002827D8, 0x002BC968)
ORIGINAL_JAL = 0x0C000000 | (TICK >> 2)
CTRL, CODE = 0x01F00900, 0x01F00940
C_CALL_FN, C_CALL_A0, C_CALL_A1, C_CALL_RET, C_CALLS = 0x1C, 0x20, 0x24, 0x28, 0x2C

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "taz_grab.json")


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
        s.settimeout(20.0)
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

    def _batch(self, addrs):
        body = b"".join(bytes([READ32]) + a.to_bytes(4, "little") for a in addrs)
        r = self._send((len(body) + 4).to_bytes(4, "little") + body)
        out = r[5:5 + 4 * len(addrs)]
        if len(out) != 4 * len(addrs):
            raise ConnectionError("short reply")
        return out

    def span(self, start, count):
        return self._batch(range(start, start + 4 * count, 4))

    def at(self, addrs):
        addrs = list(addrs)
        out = {}
        for i in range(0, len(addrs), PER_REQ):
            chunk = addrs[i:i + PER_REQ]
            raw = self._batch(chunk)
            for j, a in enumerate(chunk):
                out[a] = int.from_bytes(raw[4 * j:4 * j + 4], "little")
        return out

    def r32(self, a):
        return int.from_bytes(self.span(a, 1), "little")

    def w32(self, a, v):
        body = bytes([WRITE32]) + a.to_bytes(4, "little") \
            + (v & 0xFFFFFFFF).to_bytes(4, "little")
        self._send((len(body) + 4).to_bytes(4, "little") + body)


def ee(v):
    return EE_LO <= v < EE_HI


def sweep(p, lo=EE_LO, hi=EE_HI, label="reading"):
    out, at, total = bytearray(), lo, hi - lo
    while at < hi:
        n = min(PER_REQ, (hi - at) // 4)
        try:
            out += p.span(at, n)
        except Exception:
            out += b"\0" * (4 * n)
        at += 4 * n
        sys.stdout.write(f"\r      {label} {100.0 * (at - lo) / total:5.1f}%  ")
        sys.stdout.flush()
    sys.stdout.write("\r" + " " * 40 + "\r")
    return bytes(out), lo


def find_word(buf, base, value):
    pat = struct.pack("<I", value)
    out, at = [], buf.find(pat)
    while at != -1:
        if at % 4 == 0:
            out.append(base + at)
        at = buf.find(pat, at + 1)
    return out


def word(buf, base, addr):
    o = addr - base
    if 0 <= o <= len(buf) - 4:
        return int.from_bytes(buf[o:o + 4], "little")
    return None


def cmd_scan(p, args):
    """Find every live zookeeper.

    A word equal to 0x00170AC0 is a sub + 0x154, and the zookeeper is
    whatever holds that sub at +0x1D8. Nothing else has to be found: the
    callback takes a string, and Taz comes from the global.
    """
    taz = p.r32(TAZ_PTR)
    if not ee(taz):
        print("    Taz has no object right now -- be in a level.")
        return 1
    print(f"    taz 0x{taz:08X}. Sweeping EE RAM for zookeepers...")
    buf, base = sweep(p)

    subs = [a - GRAB_SLOT for a in find_word(buf, base, GRAB_FN)]
    catchers = []
    for sub in subs:
        for holder in find_word(buf, base, sub):
            cand = holder - SUB_PTR
            if ee(cand):
                catchers.append({"catcher": cand, "sub": sub})
    print(f"    {len(subs)} object(s) hold the callback at +0x154 -> "
          f"{len(catchers)} zookeeper(s)")
    for c in catchers[:12]:
        print(f"      zookeeper 0x{c['catcher']:08X}   sub 0x{c['sub']:08X}")

    with open(STATE, "w") as fh:
        json.dump({"taz": taz, "catchers": catchers,
                   "when": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=2)
    print()
    print(f"    written to {os.path.basename(STATE)}")
    if catchers:
        print("    Try:  py -3.13 taz_grab.py grab")
    else:
        print("    No zookeeper in this level.")
    return 0


def installed(p):
    want = 0x0C000000 | (CODE >> 2)
    return any(p.r32(a) == want for a in CALL_SITES)


def call(p, fn, a0, a1, wait=2.0):
    if not installed(p):
        print("    the trampoline is not installed -- start the AP client once,")
        print("    or run taz_tramp.py install.")
        return None
    if p.r32(CTRL + C_CALL_FN):
        print("    a call is still pending")
        return None
    before = p.r32(CTRL + C_CALLS)
    p.w32(CTRL + C_CALL_A0, a0)
    p.w32(CTRL + C_CALL_A1, a1)
    p.w32(CTRL + C_CALL_FN, fn)
    end = time.time() + wait
    while time.time() < end:
        if p.r32(CTRL + C_CALLS) != before:
            return p.r32(CTRL + C_CALL_RET)
        time.sleep(0.004)
    p.w32(CTRL + C_CALL_FN, 0)
    print("    the call never ran -- is the trampoline ticking?")
    return None


def f32(p, a):
    return struct.unpack("<f", p.span(a, 1))[0]


def cmd_check(p, args):
    """Both gates, evaluated for every zookeeper we know about.

    Gate 1 is Taz's state against the jump table at 0x00494F20. Gate 2 is a
    height comparison: the keeper's "handle" bone has to sit below Taz's
    height plus 112.5, which is what stops a keeper on a ledge grabbing you
    from above. Neither can be argued with -- they can only be measured.
    """
    taz = p.r32(TAZ_PTR)
    if not ee(taz):
        print("    Taz has no object -- be in a level.")
        return 1
    st = p.r32(taz + O_STATE_PTR)
    sid = p.r32(st + S_ID) & 0xFF
    ok1 = sid in ALLOWED_STATES
    print(f"    taz 0x{taz:08X}   state 0x{sid:02X}   "
          f"gate 1 {'PASS' if ok1 else 'FAIL -- capture not allowed here'}")
    xf = p.r32(taz + TAZ_XFORM)
    if ee(xf):
        h = f32(p, xf + 0x10)
        print(f"    height gate: keeper handle must be below "
              f"{h:.1f} + {GATE_MARGIN} = {h + GATE_MARGIN:.1f}")
    try:
        d = json.load(open(STATE))
    except Exception:
        d = {}
    for c in d.get("catchers", []):
        a = c["catcher"]
        sub = p.r32(a + SUB_PTR)
        live = ee(sub) and p.r32(sub + GRAB_SLOT) == GRAB_FN
        anim = p.r32(a + O_ANIM_PTR)
        print(f"      keeper 0x{a:08X}  {'live' if live else 'STALE'}  "
              f"anim 0x{anim:08X}")
    print()
    print("    The handle bone's height is only knowable by calling the game,")
    print("    so the practical answer is to try each keeper:")
    print("      py -3.13 taz_grab.py grab --all")
    return 0


def cmd_grab(p, args):
    """Fire one of the zookeeper's animation events at it.

    on_event(zookeeper, "keeperhit") is the branch that runs the capture --
    it reads Taz from the global at 0x003FF060 and puts him in state 0x59
    525 instructions in, then carries on with the carry and the cage.
    """
    if args.list:
        print("    event names this callback compares against:")
        for k, v in EVENTS.items():
            print(f"      {k:24s} 0x{v:08X}"
                  + ("   <- the capture" if k == "keeperhit" else ""))
        return 0
    try:
        d = json.load(open(STATE))
    except Exception:
        d = {}
    known = [c.get("catcher") for c in d.get("catchers", []) if c.get("catcher")]
    if args.catcher:
        known = [args.catcher]
    elif not args.all:
        known = known[:1]
    if not known:
        print("    no zookeeper known. Run `scan` in a level with one.")
        return 1
    if args.event not in EVENTS:
        print(f"    unknown event {args.event!r}. --list shows them.")
        return 1
    name = EVENTS[args.event]

    taz = p.r32(TAZ_PTR)
    st = p.r32(taz + O_STATE_PTR)
    sid = p.r32(st + S_ID) & 0xFF
    if sid not in ALLOWED_STATES:
        print(f"    Taz is in state 0x{sid:02X}, which the jump table at "
              f"0x00494F20 refuses. Stand still and try again.")
        return 1

    for catcher in known:
        sub = p.r32(catcher + SUB_PTR)
        if not ee(sub) or p.r32(sub + GRAB_SLOT) != GRAB_FN:
            print(f"    0x{catcher:08X} is stale -- re-run `scan`.")
            continue
        print(f"    keeper 0x{catcher:08X}  event {args.event!r}  "
              f"taz state 0x{sid:02X}")
        ret = call(p, GRAB_FN, catcher, name)
        if ret is None:
            return 1
        t0, last, caught = time.time(), None, False
        while time.time() - t0 < (args.hold if len(known) == 1 else 1.2):
            v = p.r32(st + S_ID) & 0xFF
            if v != last:
                caught |= v == STATE_CAUGHT
                print(f"      [{time.time() - t0:5.2f}]  state 0x{v:02X}"
                      + ("  <== CAUGHT" if v == STATE_CAUGHT else ""))
                last = v
            if caught:
                break
            time.sleep(0.02)
        print(f"      returned 0x{ret:08X}"
              + ("   <- reached the capture" if caught else
                 "   <- bailed at the height gate"))
        if caught:
            print()
            print("    That keeper works. Watch the screen for the rest of it.")
            return 0
    print()
    print("    None of them passed the height gate: the keeper's \"handle\"")
    print("    bone has to sit below Taz's height plus 112.5, so every one of")
    print("    them is on a different level from him right now. Try again")
    print("    standing near one, and tell me whether that changes it.")
    return 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("scan").set_defaults(fn=cmd_scan)
    sub.add_parser("check").set_defaults(fn=cmd_check)

    g = sub.add_parser("grab")
    g.add_argument("--catcher", type=lambda x: int(x, 0))
    g.add_argument("--event", default="keeperhit")
    g.add_argument("--all", action="store_true",
                   help="try every known zookeeper in turn")
    g.add_argument("--list", action="store_true",
                   help="show the event names and stop")
    g.add_argument("--hold", type=float, default=10.0)
    g.set_defaults(fn=cmd_grab)

    args = ap.parse_args()
    return args.fn(Pine().connect(), args)


if __name__ == "__main__":
    sys.exit(main())
