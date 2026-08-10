#!/usr/bin/env python3
"""Dump PS2 EE RAM out of PCSX2 over PINE, so it can be disassembled offline.

    py -3.13 taz_ramdump.py

Writes ee_dump.bin next to this script: a flat 32MB image where the file
offset IS the EE address, so 0x002E5128 in the game is byte 0x002E5128 in
the file. Nothing is written to the game -- this only reads.

Close the AP client first; only one thing at a time on PINE.

Regions PCSX2 refuses to hand over are zero-filled and reported at the end
rather than aborting the sweep.
"""

import argparse
import os
import socket
import sys
import time

SLOT = 28011
READ32 = 2
MAX_IPC_SIZE = 650000


class Pine:
    def __init__(self, slot=SLOT):
        self.slot = slot
        self.sock = None

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
            raise ConnectionError(
                f"could not reach PCSX2 on {name!r}: {e}\n"
                "Is PCSX2 running with the game booted, and PINE enabled "
                f"(Settings -> Advanced) on slot {self.slot}?") from None
        self.sock = s

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def reconnect(self):
        self.close()
        time.sleep(0.2)
        self.connect()

    def _send(self, request):
        self.sock.sendall(request)
        want, buf = 4, b""
        while len(buf) < want:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("PCSX2 closed the connection.")
            buf += chunk
            if want == 4 and len(buf) >= 4:
                want = int.from_bytes(buf[0:4], "little")
                if want > MAX_IPC_SIZE:
                    raise ConnectionError("oversized PINE reply")
        if buf[4] == 0xFF:
            raise ConnectionError("PCSX2 reported a failure")
        return buf

    def words(self, start, count):
        """count consecutive u32 at start, as raw little-endian bytes."""
        body = b"".join(bytes([READ32]) + a.to_bytes(4, "little")
                        for a in range(start, start + 4 * count, 4))
        reply = self._send((len(body) + 4).to_bytes(4, "little") + body)
        out = reply[5:5 + 4 * count]
        if len(out) != 4 * count:
            raise ConnectionError(
                f"short reply: wanted {4 * count} bytes, got {len(out)}")
        return out


def calibrate(pine, probe_at):
    """Biggest batch PCSX2 will take in one message. Measured, not assumed."""
    for n in (32768, 16384, 8192, 2048, 512, 192, 64):
        try:
            if len(pine.words(probe_at, n)) == 4 * n:
                return n
        except Exception:
            try:
                pine.reconnect()
            except Exception:
                pass
    raise SystemExit("    PINE would not accept even a 64-word batch.")


def bar(done, total, t0):
    pct = 100.0 * done / total if total else 100.0
    rate = done / max(time.time() - t0, 1e-6) / (1 << 20)
    sys.stdout.write(f"\r    {pct:5.1f}%   {done >> 20:3d} / {total >> 20} MB"
                     f"   {rate:5.2f} MB/s   ")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=None, help="output file")
    ap.add_argument("--lo", default="0x00000000", help="first address")
    ap.add_argument("--hi", default="0x02000000", help="one past the last")
    ap.add_argument("--slot", type=int, default=SLOT)
    args = ap.parse_args()

    lo = int(args.lo, 0) & ~3
    hi = (int(args.hi, 0) + 3) & ~3
    if hi <= lo:
        raise SystemExit("    --hi must be above --lo")
    out = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ee_dump.bin")

    pine = Pine(args.slot)
    pine.connect()
    per = calibrate(pine, 0x003F0000)
    step = 4 * per
    total = hi - lo
    print(f"    reading 0x{lo:08X}-0x{hi:08X} ({total >> 20} MB), "
          f"{per} words per request")

    gaps = []
    t0 = time.time()
    with open(out, "wb") as fh:
        # The file is a flat image: offset == address. Anything below --lo is
        # padded so that stays true no matter what range was asked for.
        if lo:
            fh.write(b"\0" * lo)
        at = lo
        while at < hi:
            count = min(per, (hi - at) // 4)
            try:
                fh.write(pine.words(at, count))
            except Exception:
                # One bad word should not cost the whole sweep. Reconnect,
                # then walk the chunk in small pieces so the hole is as small
                # as it really is instead of as big as the batch.
                try:
                    pine.reconnect()
                except Exception as e:
                    raise SystemExit(f"\n    lost PCSX2 at 0x{at:08X}: {e}")
                buf = bytearray()
                for sub in range(at, at + 4 * count, 64 * 4):
                    n = min(64, (at + 4 * count - sub) // 4)
                    try:
                        buf += pine.words(sub, n)
                    except Exception:
                        buf += b"\0" * (4 * n)
                        if gaps and gaps[-1][1] == sub:
                            gaps[-1] = (gaps[-1][0], sub + 4 * n)
                        else:
                            gaps.append((sub, sub + 4 * n))
                        try:
                            pine.reconnect()
                        except Exception as e:
                            raise SystemExit(
                                f"\n    lost PCSX2 at 0x{sub:08X}: {e}")
                fh.write(bytes(buf))
            at += 4 * count
            if (at - lo) % (step * 16) < step:
                bar(at - lo, total, t0)
    bar(total, total, t0)
    pine.close()

    size = os.path.getsize(out)
    print(f"\n    wrote {out}  ({size:,} bytes) in {time.time() - t0:.0f}s")
    if gaps:
        print(f"    {len(gaps)} unreadable region(s), zero-filled:")
        for a, b in gaps[:20]:
            print(f"      0x{a:08X}-0x{b:08X}  ({b - a} bytes)")
        if len(gaps) > 20:
            print(f"      ... and {len(gaps) - 20} more")
    else:
        print("    no unreadable regions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
