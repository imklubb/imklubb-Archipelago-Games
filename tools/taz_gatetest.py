#!/usr/bin/env python3
"""Every gate locked, and blocked by electrocution instead of a shove.

The apworld currently snaps Taz to just outside a locked zone's radius. This
does it the way you actually wanted: he walks in, gets shocked, and drifts
back out while he is stunned -- so it reads as the door refusing him rather
than as the game glitching, and he is clear of the trigger by the time he can
move again, instead of standing in it and being shocked over and over.

Locking is total here: every gate in taz_gates.json is treated as closed
whatever the seed says, which is what makes this testable before it goes near
a build.

    py -3.13 taz_gatetest.py run
    py -3.13 taz_gatetest.py run --only "Ice Burg"
    py -3.13 taz_gatetest.py run --no-shock        the drift on its own
    py -3.13 taz_gatetest.py gates                 what is loaded, and where

STATE_ELECTROCUTED is 0x1D and its handler is 0x001DF550, installed the way
0x002C44D8 does it: handler to the state object at +0x108, id to +0x10C.
Same two writes as the dynamite trap.
"""

import argparse
import json
import math
import os
import socket
import struct
import sys
import time

SLOT = 28011
READ32, WRITE32 = 2, 6

TAZ_PTR = 0x003FF060
LEVEL_ID = 0x003FF048
GAME_STATE = 0x003FF040
STATE_ACTIVE = 1

O_POS = 0xC0                 # three floats, the same field the apworld writes
O_STATE_PTR = 0x1C8
S_ID, S_HANDLER, S_REQUEST = 0xB0, 0x108, 0x10C

ELECTROCUTE_FN = 0x001DF550
ELECTROCUTE_STATE = 0x1D

GATES_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "worlds", "tazwanted", "data", "taz_gates.json")


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
                             "    Close the AP client first -- only one thing "
                             "at a time on PINE.")
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
        return [int.from_bytes(r[5 + 4 * i:9 + 4 * i], "little")
                for i in range(len(addrs))]

    def r32(self, a):
        return self._batch([a])[0]

    def w32(self, a, v):
        body = bytes([WRITE32]) + a.to_bytes(4, "little") \
            + (v & 0xFFFFFFFF).to_bytes(4, "little")
        self._send((len(body) + 4).to_bytes(4, "little") + body)

    def pos(self, base):
        w = self._batch([base + 0, base + 4, base + 8])
        return tuple(struct.unpack("<f", x.to_bytes(4, "little"))[0] for x in w)

    def wpos(self, base, p):
        for i, v in enumerate(p):
            self.w32(base + 4 * i,
                     struct.unpack("<I", struct.pack("<f", float(v)))[0])


def ee(v):
    return 0x00100000 <= v < 0x02000000


def dist(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def exit_point(pos, point, want):
    """Where to put Taz so he ends `want` away from the zone centre.

    His height is kept -- shoving him vertically out of a doorway looks
    wrong -- so the horizontal leg has to make up the whole of the distance
    that the vertical one does not. Scaling the 3D offset and then pinning Y
    back is what does NOT work: from close to the centre with any height
    difference at all it lands him back inside the radius.
    """
    dy = pos[1] - point[1]
    leg = math.sqrt(max(want * want - dy * dy, 1.0))
    dh = math.hypot(pos[0] - point[0], pos[2] - point[2])
    if dh < 1.0:
        ux, uz = 1.0, 0.0            # dead centre: any direction will do
    else:
        ux, uz = (pos[0] - point[0]) / dh, (pos[2] - point[2]) / dh
    return (point[0] + ux * leg, pos[1], point[2] + uz * leg)


def load_gates(path=None):
    """Flattened the same way game.py flattens them, so this tests the real
    data rather than a re-typed copy of it."""
    p = path or GATES_JSON
    raw = json.load(open(p))
    out = []
    for key, g in raw.items():
        points = g.get("points")
        if points is None and "trigger" in g:
            points = [g["trigger"]]
        if not points:
            continue
        dest = g.get("gates")
        if dest is None:
            try:
                dest = int(key)
            except (TypeError, ValueError):
                continue
        out.append({"name": g.get("name", key),
                    "where": g.get("hub", g.get("in")),
                    "gates": int(dest),
                    "radius": float(g.get("radius", 800.0)),
                    "points": [tuple(float(v) for v in q) for q in points]})
    return out


def cmd_gates(p, args):
    gates = load_gates(args.gates)
    lid = p.r32(LEVEL_ID)
    print(f"    {len(gates)} gate(s) in taz_gates.json; you are in level {lid}")
    for g in gates:
        here = "  <- this level" if g["where"] in (None, lid) else ""
        print(f"      {g['name']:20s} in {str(g['where']):>4s}  "
              f"-> {g['gates']:2d}  r {g['radius']:7.1f}  "
              f"{len(g['points'])} point(s){here}")
    return 0


def shock(p, taz):
    st = p.r32(taz + O_STATE_PTR)
    if not ee(st):
        return False
    p.w32(st + S_HANDLER, ELECTROCUTE_FN)
    p.w32(st + S_REQUEST, ELECTROCUTE_STATE)
    return True


def cmd_run(p, args):
    gates = load_gates(args.gates)
    if args.only:
        gates = [g for g in gates if args.only.lower() in g["name"].lower()]
        if not gates:
            print(f"    no gate matching {args.only!r}")
            return 1
    print(f"    {len(gates)} gate(s), ALL treated as locked.")
    print(f"    shock {'on' if not args.no_shock else 'OFF'}, "
          f"drift {args.seconds}s to {args.margin:.2f}x radius + {args.extra:.0f}")
    print("    a gate re-arms by Taz leaving it, not by a timer")
    print("    Walk into every door. Ctrl-C to stop.")
    print()

    gliding = None          # {"gate","from","to","t0"}
    # Not a timer. A gate re-arms by Taz LEAVING it, so leaning on a door
    # gets him shocked at the boundary every time instead of buying him a
    # free run at it. A wall-clock cooldown let him cover 760 units back in
    # before the gate was allowed to fire again -- which is how he got through.
    armed = {}
    last_fire = {}
    fired = {}
    t_start = time.time()
    try:
        while True:
            if p.r32(GAME_STATE) != STATE_ACTIVE:
                time.sleep(0.1)
                continue
            taz = p.r32(TAZ_PTR)
            if not ee(taz):
                time.sleep(0.1)
                continue
            base = taz + O_POS
            lid = p.r32(LEVEL_ID)
            now = time.time()

            if gliding:
                f = (now - gliding["t0"]) / args.seconds
                if f >= 1.0:
                    p.wpos(base, gliding["to"])
                    print(f"      [{now - t_start:6.1f}] {gliding['gate']}: "
                          f"set down {gliding['out']:.0f} from the centre")
                    gliding = None
                else:
                    # ease out: quick at first, gentle as he settles
                    e = 1 - (1 - f) ** 2
                    a, b = gliding["from"], gliding["to"]
                    p.wpos(base, tuple(a[i] + (b[i] - a[i]) * e
                                       for i in range(3)))
                time.sleep(args.step)
                continue

            pos = p.pos(base)
            for g in gates:
                if g["where"] not in (None, lid):
                    continue
                name = g["name"]
                inside = any(dist(pos, q) < g["radius"] for q in g["points"])
                if not inside:
                    armed[name] = True          # left the zone: armed again
                    continue
                if not armed.get(name, True):
                    continue
                if now - last_fire.get(name, 0) < args.min_gap:
                    continue
                for point in g["points"]:
                    d = dist(pos, point)
                    if d >= g["radius"]:
                        continue
                    want = g["radius"] * args.margin + args.extra
                    to = exit_point(pos, point, want)
                    ok = shock(p, taz) if not args.no_shock else True
                    fired[name] = fired.get(name, 0) + 1
                    armed[name] = False
                    last_fire[name] = now
                    print(f"      [{now - t_start:6.1f}] {name}: "
                          f"{g['radius'] - d:.0f} deep "
                          f"(d {d:.0f} of {g['radius']:.0f})"
                          f"{'  shocked' if ok and not args.no_shock else ''}"
                          f"  -> moving {dist(pos, to):.0f} to {want:.0f} out")
                    gliding = {"gate": name, "from": pos, "to": to,
                               "t0": now, "out": want}
                    break
                if gliding:
                    break
            time.sleep(args.step)
    except KeyboardInterrupt:
        print()
    print("    stopped")
    if fired:
        print("    gates that fired:")
        for k, v in sorted(fired.items(), key=lambda x: -x[1]):
            print(f"      {k:20s} {v}")
    else:
        print("    nothing fired -- were you in a hub with locked doors?")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gates", help="path to taz_gates.json")
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("gates").set_defaults(fn=cmd_gates)

    r = sub.add_parser("run")
    r.add_argument("--only", help="only gates whose name contains this")
    r.add_argument("--seconds", type=float, default=1.4,
                   help="how long the drift takes")
    r.add_argument("--margin", type=float, default=1.45,
                   help="multiple of the radius he ends up at")
    r.add_argument("--extra", type=float, default=200.0,
                   help="flat distance added on top of the multiple")
    r.add_argument("--min-gap", type=float, default=0.4,
                   help="floor between two shocks from one gate")
    r.add_argument("--step", type=float, default=0.03)
    r.add_argument("--no-shock", action="store_true")
    r.set_defaults(fn=cmd_run)

    args = ap.parse_args()
    return args.fn(Pine().connect(), args)


if __name__ == "__main__":
    sys.exit(main())
