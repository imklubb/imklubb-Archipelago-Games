"""
taz_glad_watch.py -- watch the Gladiatoons timer and both scores.

That fight has no 0x5A state, so it needs its own reading: the timer running
out while Daffy is ahead. The addresses are known; what is not known is what
the timer READS when it reaches zero -- it may be a float counting down, an
integer counting up, or something that stops short of zero entirely.

So this shows all three, live, and flags the moment the timer stops moving.

Run it next to Launcher.py with the AP client CLOSED:

    venv\\Scripts\\python.exe taz_glad_watch.py

    Start the fight and let the clock run all the way out. The last few lines
    before "the timer stopped" are the ones I need.

    --raw   also show the timer's bytes as an integer, in case it is not a
            float at all
"""

import argparse
import struct
import sys
import time

try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  Run:  venv\\Scripts\\python.exe taz_glad_watch.py\n")
    sys.exit(1)

try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import logic as L
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"  Run this from the Archipelago folder, next to Launcher.py.")

TIMER = 0x00380E28
GLADIATOONS = 12

# The old score addresses read 3456251169 -- not a score. They are being
# searched for again, so this scans instead of trusting them.
# Widened: Daffy sits at 0x0088376C, outside every band the first version
# scanned, so the hunt could never have found him. Cheaper to scan too much
# than to search the wrong place twice.
BANDS = [(0x0037D000, 0x00384000),
         (0x0067C000, 0x0068E000),
         (0x00860000, 0x008B0000)]   # Daffy is at 0x0088376C; Taz should be
                                     # in the same neighbourhood


def find_scores():
    """Find the two scores by watching what changes when a point is scored.

    A score is a small integer that only ever goes up, so a snapshot before
    and after a point leaves very few candidates.
    """
    # Exact values, not "went up a bit". The fight moves faster than a
    # snapshot pair can be taken cleanly, so 34 bytes matched "increased by a
    # small amount" and only one of them was a score. Being told the numbers
    # on screen turns that into a handful.
    print("\n  Score hunt.")
    print("  Pause the game if you can -- exact values matter more than speed.")
    b_val = input("\n  What is the score NOW (the side you are hunting)? > ")
    before = snapshot()
    print("  Now let that side score, then pause again.")
    a_val = input("  What does it read now? > ")
    after = snapshot()

    try:
        b_val, a_val = int(b_val.strip()), int(a_val.strip())
    except ValueError:
        print("  Those need to be numbers. Falling back to any increase.")
        b_val = a_val = None

    # Byte-wise, not word-wise. 0x0088376C sits in a run of small bytes
    # (01 03 03 01), so the WORD there is 17040129 -- nowhere near a score,
    # and a word-level comparison hides it completely.
    hits = []
    for a in after:
        if a not in before or before[a] == after[a]:
            continue
        pb = before[a].to_bytes(4, "little")
        pa = after[a].to_bytes(4, "little")
        for i in range(4):
            if b_val is not None:
                # Exactly what the screen said, both times.
                if pb[i] == b_val and pa[i] == a_val:
                    hits.append((a + i, pb[i], pa[i]))
            elif pb[i] < pa[i] <= pb[i] + 5 and pa[i] < 50:
                hits.append((a + i, pb[i], pa[i]))
    what = (f"read {b_val} then {a_val}" if b_val is not None
            else "went up by a small amount")
    print(f"\n    {len(hits)} byte(s) {what}:\n")

    # A raw address is no use here: the scores live in allocated memory and
    # move when the level reloads, exactly as the helmet does. What holds
    # still is the OFFSET from a pointer the game keeps somewhere fixed, so
    # every hit is also expressed that way.
    bases = {}
    for label, addr in (("TAZ_PTR", G.TAZ_PTR),):
        try:
            p = G.mem.read_u32(addr)
            if G.mem.valid_ptr(p):
                bases[label] = p
        except Exception:
            pass
    # Any pointer in the fixed globals region that lands near a hit.
    for a in range(0x003F0000, 0x00412000, 4):
        try:
            p = G.mem.read_u32(a)
        except Exception:
            continue
        if not G.mem.valid_ptr(p):
            continue
        for h, _, _ in hits:
            if 0 <= h - p < 0x2000:
                bases[f"[{a:#08x}]"] = p

    for a, b, c in sorted(hits)[:20]:
        near = ""
        best = None
        for label, base in bases.items():
            d = a - base
            if 0 <= d < 0x2000 and (best is None or d < best[1]):
                best = (label, d)
        if best:
            near = f"   = {best[0]} + {best[1]:#x}"
        print(f"      0x{a:08X}   {b} -> {c}{near}")
    if not hits:
        print("      none -- try again, and make sure only one point is scored")
    print("\n    An offset that is the SAME after a reload is the one to use.")
    print("    Do this once more for the other side, and once after")
    print("    restarting the fight, to confirm the offset holds.\n")


def snapshot():
    out = {}
    for lo, hi in BANDS:
        addr = lo
        while addr < hi:
            n = min(4 * 192, hi - addr)
            try:
                raw = G.mem.read_bytes(addr, n)
            except Exception:
                addr += n
                continue
            for i in range(0, len(raw) - 3, 4):
                out[addr + i] = struct.unpack_from("<I", raw, i)[0]
            addr += n
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true",
                    help="also read the timer as an integer")
    ap.add_argument("--secs", type=float, default=600.0)
    ap.add_argument("--scores", action="store_true",
                    help="hunt for the two score addresses instead")
    a = ap.parse_args()

    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the AP client still open?")
    g = G.Game()
    g.connected = True

    if a.scores:
        find_scores()
        return

    print(f"\n  timer  0x{TIMER:08X}")
    print(f"  Daffy  0x{DAFFY:08X}")
    print(f"  Taz    0x{TAZ:08X}")
    print(f"\n  Start Gladiatoons and let the clock run out.\n")
    print(f"  {'time':>6}  {'timer':>12} {'as int':>11}  "
          f"{'Taz':>4} {'Daffy':>5}  {'state':>5}  note")

    last = None
    still_since = None
    said_stopped = False
    t0 = time.time()
    while time.time() - t0 < a.secs:
        time.sleep(0.05)
        lid = g.level_id()
        if lid != GLADIATOONS:
            continue
        try:
            raw = G.mem.read_bytes(TIMER, 4)
            as_float = struct.unpack("<f", raw)[0]
            as_int = struct.unpack("<I", raw)[0]
            taz = daffy = 0
            st = g._state()
        except Exception:
            continue

        note = ""
        # A timer that stops moving has either finished or been paused, and
        # the value it stopped at is the whole question.
        if last is not None and abs(as_float - last[0]) < 1e-6:
            if still_since is None:
                still_since = time.time()
            elif time.time() - still_since > 1.5 and not said_stopped:
                said_stopped = True
                note = "   <-- the timer STOPPED at this value"
        else:
            still_since = None
            said_stopped = False

        if taz < daffy and note:
            note += ", and Daffy is ahead -- this is a loss"

        row = (as_float, taz, daffy)
        if row != last or note:
            last = row
            intcol = f"{as_int}" if a.raw else ""
            print(f"  {time.time() - t0:>6.1f}  {as_float:>12.3f} "
                  f"{intcol:>11}  {taz:>4} {daffy:>5}  "
                  f"0x{st:02X}  {note}"
                  if st is not None else
                  f"  {time.time() - t0:>6.1f}  {as_float:>12.3f} "
                  f"{intcol:>11}  {taz:>4} {daffy:>5}     ?  {note}")

    print("\n  The value the timer stopped at is what the loss condition")
    print("  should test. If it never reached zero, it is counting UP and")
    print("  the limit is whatever it stopped at.\n")


if __name__ == "__main__":
    main()
