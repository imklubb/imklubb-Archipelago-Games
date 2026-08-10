#!/usr/bin/env python3
"""Find where the controller's buttons live in EE RAM.

Nothing in the apworld reads the pad, and the address cannot be found by
reading code -- so it gets recorded.

Two full scans establish a candidate set, and every round after that reads
ONLY the survivors, so extra rounds are nearly free. Each round takes two
samples a moment apart and throws away anything that moved between them,
which is what kills the noise: a value that merely happens to differ between
one released and one held snapshot cannot survive being sampled twice per
phase across several alternations.

    py -3.13 taz_pad.py hunt                 default 5 alternations
    py -3.13 taz_pad.py hunt --rounds 8      more, if it is still ambiguous
    py -3.13 taz_pad.py hunt --any-bits      drop the clean-bitfield filter
    py -3.13 taz_pad.py hunt --full          all 32MB rather than the low 7MB
    py -3.13 taz_pad.py watch 0x00123456     live value, for mapping buttons
    py -3.13 taz_pad.py combo 0x00123456     the L1+R1+L2+R2 mask, to json

Be in a level with control of Taz, not paused. Close the AP client first.
"""

import argparse
import json
import os
import socket
import sys
import time

SLOT = 28011
READ32 = 2
MAX_IPC = 650000
PER_REQ = 8192
BAND_LO, BAND_HI = 0x00100000, 0x00800000
FULL_LO, FULL_HI = 0x00000000, 0x02000000


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
            raise SystemExit(f"    could not reach PCSX2 on {name!r}: {e}")
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
                if want > MAX_IPC:
                    raise ConnectionError("oversized reply")
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
        """Scattered reads, batched. This is what makes extra rounds cheap."""
        out = {}
        addrs = list(addrs)
        for i in range(0, len(addrs), PER_REQ):
            chunk = addrs[i:i + PER_REQ]
            raw = self._batch(chunk)
            for j, a in enumerate(chunk):
                out[a] = int.from_bytes(raw[4 * j:4 * j + 4], "little")
        return out

    def r32(self, a):
        return int.from_bytes(self.span(a, 1), "little")


def sweep(p, lo, hi, label=""):
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
    sys.stdout.write("\r" + " " * 44 + "\r")
    return bytes(out)


def ask(prompt):
    try:
        input(f"    {prompt}, then Enter... ")
    except EOFError:
        raise SystemExit("    no input available")


def steady(p, addrs, settle=0.35):
    """Read twice, a moment apart, and keep only what did not move.

    One sample per phase was the flaw in the first version: anything drifting
    had an even chance of looking like it reacted to the buttons.
    """
    a = p.at(addrs)
    time.sleep(settle)
    b = p.at(addrs)
    return {k: v for k, v in a.items() if b.get(k) == v}


def clean_change(released, held):
    """A button field only adds bits or only removes them; it does not swap
    some for others. Anything that does is a counter or a float."""
    return (held & released) == released or (held | released) == released


def cmd_hunt(p, args):
    lo, hi = (FULL_LO, FULL_HI) if args.full else (BAND_LO, BAND_HI)
    if args.lo is not None:
        lo = args.lo
    if args.hi is not None:
        hi = args.hi
    print(f"    searching 0x{lo:08X}-0x{hi:08X} ({(hi - lo) >> 20} MB)")
    print("    Be in a level, in control of Taz, not paused.")
    print()

    ask("1  Let go of every button")
    a = sweep(p, lo, hi, "released")
    ask("2  Hold L1 + R1 + L2 + R2 all together")
    b = sweep(p, lo, hi, "held")

    rel, hel = {}, {}
    for i in range(len(a) // 4):
        o = 4 * i
        va, vb = a[o:o + 4], b[o:o + 4]
        if va != vb:
            addr = lo + o
            rel[addr] = int.from_bytes(va, "little")
            hel[addr] = int.from_bytes(vb, "little")
    print(f"      {len(rel)} address(es) differ between released and held")
    if not rel:
        print("    Nothing changed at all.")
        return 1
    if args.any_bits:
        keep = set(rel)
    else:
        keep = {k for k in rel if clean_change(rel[k], hel[k])}
        print(f"      {len(keep)} of those change bits cleanly "
              f"(only set, or only cleared)")
    if not keep:
        print("    None looked like a bitfield. Re-run with --any-bits.")
        return 1

    # From here on only the survivors are read, so rounds are cheap.
    for r in range(args.rounds):
        ask(f"{r + 3}  Let go of every button")
        vals = steady(p, sorted(keep))
        keep = {k for k in keep if vals.get(k) == rel[k]}
        print(f"      released: {len(keep)} left")
        if not keep:
            break
        ask(f"{r + 3}  Hold all four again")
        vals = steady(p, sorted(keep))
        keep = {k for k in keep if vals.get(k) == hel[k]}
        print(f"      held:     {len(keep)} left")
        if not keep:
            break
        if len(keep) <= args.stop:
            print(f"      down to {len(keep)}, stopping early")
            break

    print()
    if not keep:
        print("    Everything was eliminated -- something drifted, or a")
        print("    button was held during a 'let go' round. Try again.")
        return 1

    hits = sorted(keep, key=lambda k: (
        abs(bin(rel[k] ^ hel[k]).count("1") - 4),
        bin(rel[k] ^ hel[k]).count("1"), k))
    print(f"    {len(hits)} survivor(s), best first:")
    print()
    for addr in hits[:args.top]:
        x = rel[addr] ^ hel[addr]
        n = bin(x).count("1")
        note = ""
        if n == 4:
            note = "   <- four bits, one per button"
        elif n in (1, 2):
            note = "   <- too few bits for four buttons"
        print(f"      0x{addr:08X}  released 0x{rel[addr]:08X}  "
              f"held 0x{hel[addr]:08X}  xor 0x{x:08X}  ({n} bits){note}")
    if len(hits) > args.top:
        print(f"      ... and {len(hits) - args.top} more")

    with open(_beside("taz_pad_candidates.json"), "w") as fh:
        json.dump([{"addr": h, "released": rel[h], "held": hel[h]}
                   for h in hits], fh, indent=2)
    print()
    print(f"    all {len(hits)} written to taz_pad_candidates.json")
    print("    Confirm the top one:")
    print(f"      py -3.13 taz_pad.py combo 0x{hits[0]:08X}")
    return 0


def _beside(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def cmd_watch(p, args):
    print(f"    watching 0x{args.addr:08X}. Press buttons. Ctrl-C to stop.")
    print()
    last = None
    try:
        while True:
            v = p.r32(args.addr)
            if v != last:
                x = "" if last is None else f"  xor 0x{v ^ last:08X}"
                print(f"      0x{v:08X}   {v & 0xFFFF:016b}{x}")
                last = v
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\n    stopped")
    return 0


def cmd_combo(p, args):
    """Record each shoulder button alone, then combine.

    Also a check on the address itself: if the four buttons do not land on
    four different bits, this is not the pad field and the next candidate
    should be tried instead.
    """
    print(f"    reading 0x{args.addr:08X}")
    ask("Let go of everything")
    time.sleep(0.2)
    idle = p.r32(args.addr)
    print(f"      idle 0x{idle:08X}")
    masks = {}
    for name in ("L1", "R1", "L2", "R2"):
        ask(f"Hold {name} on its own")
        time.sleep(0.2)
        seen = [p.r32(args.addr) for _ in range(8)]
        v = max(set(seen), key=seen.count)
        masks[name] = v ^ idle
        bits = bin(masks[name]).count("1")
        print(f"      {name}: 0x{v:08X}   bit(s) 0x{masks[name]:08X} ({bits})")

    combo = 0
    for m in masks.values():
        combo |= m
    distinct = len({m for m in masks.values() if m})
    print()
    print(f"    idle 0x{idle:08X}   all four 0x{combo:08X} (xor from idle)")
    if distinct != 4 or bin(combo).count("1") != 4:
        print()
        print("    These four buttons do not land on four separate bits, so")
        print("    this is not the pad field. Try the next candidate from")
        print("    taz_pad_candidates.json.")
        return 1

    active_low = bin(idle & combo).count("1") > bin((idle ^ combo) & combo).count("1")
    print(f"    buttons read active-{'low' if active_low else 'high'}")
    out = {"pad_state": args.addr, "pad_mask": combo,
           "pad_active_low": active_low, "idle": idle,
           "buttons": dict(masks),
           "measured": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(_beside("taz_pad.json"), "w") as fh:
        json.dump(out, fh, indent=2)
    print()
    print("    written to taz_pad.json -- the client reads it at startup, so")
    print("    the hotkey turns on with no code change.")
    print(f"      PAD_STATE      = 0x{args.addr:08X}")
    print(f"      PAD_MASK       = 0x{combo:08X}")
    print(f"      PAD_ACTIVE_LOW = {active_low}")
    for name, m in masks.items():
        print(f"      # {name} = 0x{m:08X}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)

    h = sub.add_parser("hunt")
    h.add_argument("--rounds", type=int, default=5,
                   help="extra hold/release alternations after the two sweeps")
    h.add_argument("--stop", type=int, default=6,
                   help="stop early once this few candidates remain")
    h.add_argument("--any-bits", action="store_true",
                   help="keep changes that are not clean bit sets/clears")
    h.add_argument("--full", action="store_true")
    h.add_argument("--lo", type=lambda x: int(x, 0))
    h.add_argument("--hi", type=lambda x: int(x, 0))
    h.add_argument("--top", type=int, default=15)
    h.set_defaults(fn=cmd_hunt)

    w = sub.add_parser("watch")
    w.add_argument("addr", type=lambda x: int(x, 0))
    w.set_defaults(fn=cmd_watch)

    c = sub.add_parser("combo")
    c.add_argument("addr", type=lambda x: int(x, 0))
    c.set_defaults(fn=cmd_combo)

    args = ap.parse_args()
    return args.fn(Pine().connect(), args)


if __name__ == "__main__":
    sys.exit(main())
