"""
taz_death_finder.py -- find the flag that means Taz actually died.

Inferring death from a position jump was always a compromise, and it has now
been caught out: some levels warp Taz across themselves, which looks identical.
What is needed is whatever the game sets when the death transition plays --
something that moves on a real death and stays put on a warp.

The method is elimination. Take a snapshot, do the thing, snapshot again, and
keep only the addresses that behaved the way a death flag must:

    changed on every death
    unchanged on every warp

Run it next to Launcher.py with the AP client CLOSED:

    venv\\Scripts\\python.exe taz_death_finder.py

    snap              take a baseline
    death             you just died -- keep what changed
    warp              you just warped -- discard what changed
    idle              you did nothing -- discard what changed anyway,
                      which removes timers and other constant churn
    report            the surviving candidates
    watch <addr>      follow one address live to confirm it
    watchlist         follow every candidate at once, which is the quickest
                      way to see which behaves like a death flag
    flag              after a death, list every 0 -> 1 change as an offset
                      from a known pointer. Allocated memory moves between
                      levels, so the offset is the part worth keeping
    reset             start the hunt again
    quit

Do it in this order and it converges fast:

    snap, idle, idle           strips out anything that changes on its own
    snap, die, death           keeps what a death touched
    snap, warp, warp           removes anything a warp also touches
    repeat the last two once or twice, then report

Reading memory over PINE is not fast, so only a region is scanned. The default
covers the game-state area, which is where the other flags of this kind live.
"""

import argparse
import json
import struct
import sys
import time

try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  Run it by path instead:")
    print("      venv\\Scripts\\python.exe taz_death_finder.py\n")
    sys.exit(1)

try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import logic as L
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"  Run this from the Archipelago folder, next to Launcher.py.")

# The bands worth scanning. The save data, the level globals and the object
# tables all live here; a full 32MB sweep over PINE would take minutes per
# snapshot, which makes the whole method impractical.
REGIONS = [
    (0x003F0000, 0x00412000),   # game state, level id, Taz pointer
    (0x0046C000, 0x0046D000),   # enemy tables
    (0x004B0000, 0x004B4000),   # flow table
    (0x00A70000, 0x00A90000),   # where a per-level flag turned up once
]


def read_region(lo, hi):
    """Every u32 in a band, as {address: value}."""
    out = {}
    step = 4 * 192          # what pcsx2_mem batches per request
    addr = lo
    while addr < hi:
        n = min(step, hi - addr)
        try:
            raw = G.mem.read_bytes(addr, n)
        except Exception:
            addr += n
            continue
        for i in range(0, len(raw) - 3, 4):
            out[addr + i] = struct.unpack_from("<I", raw, i)[0]
        addr += n
    return out


def snapshot():
    out = {}
    for lo, hi in REGIONS:
        out.update(read_region(lo, hi))
    return out


def changed(before, after):
    return {a for a, v in after.items() if before.get(a) != v}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wide", action="store_true",
                    help="scan a much larger region (slow)")
    a = ap.parse_args()
    if a.wide:
        REGIONS.append((0x00100000, 0x00800000))

    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the AP client still open?")
    g = G.Game()
    g.connected = True

    base = None
    candidates = None       # None until the first death narrows it
    print(f"\n  Scanning {sum(hi - lo for lo, hi in REGIONS) // 1024} KB "
          f"per snapshot.")
    print(f"  Try: snap, idle, idle, snap, <die>, death, snap, <warp>, warp\n")

    while True:
        try:
            cmd = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd in ("quit", "q"):
            break

        if cmd == "reset":
            base, candidates = None, None
            print("    hunt reset\n")
            continue

        if cmd == "snap":
            t = time.time()
            base = snapshot()
            print(f"    baseline of {len(base)} words "
                  f"({time.time() - t:.1f}s)\n")
            continue

        if cmd in ("death", "warp", "idle"):
            if base is None:
                print("    take a snap first\n")
                continue
            t = time.time()
            now = snapshot()
            diff = changed(base, now)
            if cmd == "death":
                # Keep only what moved this time as well as every time before.
                candidates = diff if candidates is None else (candidates & diff)
                print(f"    {len(diff)} changed; {len(candidates)} still "
                      f"look like a death flag ({time.time() - t:.1f}s)\n")
            else:
                if candidates is None:
                    print(f"    {len(diff)} changed, but nothing to narrow "
                          f"yet -- record a death first\n")
                else:
                    before = len(candidates)
                    candidates -= diff
                    print(f"    {len(diff)} changed; dropped "
                          f"{before - len(candidates)}, "
                          f"{len(candidates)} left ({time.time() - t:.1f}s)\n")
            base = now
            continue

        if cmd == "report":
            if not candidates:
                print("    nothing yet\n")
                continue
            print(f"\n    {len(candidates)} candidate(s):\n")
            for addr in sorted(candidates)[:40]:
                try:
                    v = G.mem.read_u32(addr)
                except Exception:
                    v = None
                near = ""
                for name in ("GAME_STATE", "LEVEL_ID", "TAZ_PTR",
                             "CURRENT_FILE", "DEMO_MODE"):
                    known = getattr(G, name, None)
                    if isinstance(known, int) and abs(addr - known) <= 0x40:
                        near = f"  ({name} {addr - known:+#x})"
                print(f"      0x{addr:08X} = {v}{near}")
            if len(candidates) > 40:
                print(f"      ... and {len(candidates) - 40} more")
            print("\n    Watch one with:  watch 0x3ff040\n")
            continue

        if cmd in ("flag", "flags"):
            # A flag living in allocated memory has no fixed address -- it
            # moves with the level. What DOES hold still is its offset from a
            # pointer the game keeps somewhere fixed, so every 0 -> 1 change is
            # reported that way. An offset that is the same in two different
            # levels is the thing worth using.
            if base is None:
                print("    take a snap first, then die, then run this\n")
                continue
            now = snapshot()
            rose = [a for a in now
                    if base.get(a) == 0 and now[a] == 1]
            print(f"\n    {len(rose)} word(s) went 0 -> 1\n")
            bases = {}
            for label, addr in (("TAZ_PTR", getattr(G, "TAZ_PTR", None)),
                                ("GAME_STATE", getattr(G, "GAME_STATE", None)),
                                ("ENEMY_ARRAY", getattr(G, "ENEMY_ARRAY", None))):
                if addr is None:
                    continue
                try:
                    p = G.mem.read_u32(addr)
                    if G.mem.valid_ptr(p):
                        bases[label] = p
                except Exception:
                    pass
            # Anything Taz points at, one level down, is worth measuring from
            # too: the costume and state objects live there.
            try:
                taz = G.mem.read_u32(G.TAZ_PTR)
                for off in (0x1C0, 0x1C8, 0x1CC, 0x1D0):
                    q = G.mem.read_u32(taz + off)
                    if G.mem.valid_ptr(q):
                        bases[f"[TAZ+{off:#x}]"] = q
            except Exception:
                pass

            for a in sorted(rose)[:30]:
                near = ""
                best = None
                for label, b in bases.items():
                    d = a - b
                    if 0 <= d < 0x4000 and (best is None or d < best[1]):
                        best = (label, d)
                if best:
                    near = f"   = {best[0]} + {best[1]:#x}"
                print(f"      0x{a:08X}{near}")
            if not rose:
                print("      nothing -- was the snapshot taken before the "
                      "death?")
            print("\n    Do this in a SECOND level and compare the offsets.")
            print("    One that matches is the flag, wherever it lands.\n")
            base = now
            continue

        if cmd in ("watchlist", "wl"):
            # Follow every candidate at once. A flag that means "Taz died"
            # should move on a death and sit still through a warp; watching
            # them side by side is the quickest way to see which one does.
            try:
                with open("taz_death_candidates.json", encoding="utf-8") as f:
                    watch = [int(x, 16) for x in json.load(f)]
            except Exception:
                watch = sorted(candidates or [])
            if not watch:
                print("    nothing to watch -- run the hunt or report first\n")
                continue
            vals = {}
            for w in watch:
                try:
                    vals[w] = G.mem.read_u32(w)
                except Exception:
                    vals[w] = None
            print(f"\n    Following {len(watch)} address(es). Die, then warp,")
            print(f"    and note which ones move for one but not the other.")
            print(f"    Ctrl-C to stop.\n")
            try:
                while True:
                    time.sleep(0.05)
                    lid = g.level_id()
                    where = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
                    for w in watch:
                        try:
                            v = G.mem.read_u32(w)
                        except Exception:
                            continue
                        if v != vals[w]:
                            # Floats look like enormous integers, so show both
                            # readings and let the shape speak for itself.
                            try:
                                f32 = struct.unpack("<f",
                                                    struct.pack("<I", v))[0]
                            except Exception:
                                f32 = 0.0
                            extra = (f"  ({f32:.2f} as a float)"
                                     if 1e-6 < abs(f32) < 1e9 else "")
                            print(f"      0x{w:08X}  {vals[w]} -> {v}"
                                  f"{extra}   [{where}]")
                            vals[w] = v
            except KeyboardInterrupt:
                print("\n    ...stopped\n")
            continue

        if cmd.startswith("watch"):
            try:
                addr = int(cmd.split()[1], 0)
            except (IndexError, ValueError):
                print("    usage: watch 0x3ff040\n")
                continue
            print(f"\n    Following 0x{addr:08X}. Die, then warp. "
                  f"Ctrl-C to stop.\n")
            last = None
            try:
                while True:
                    time.sleep(0.05)
                    try:
                        v = G.mem.read_u32(addr)
                    except Exception:
                        continue
                    if v != last:
                        lid = g.level_id()
                        where = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
                        print(f"      {v:>12}   {where}")
                        last = v
            except KeyboardInterrupt:
                print("\n    ...stopped\n")
            continue

        if cmd:
            print("    snap | death | warp | idle | report | watch <addr> | "
                  "watchlist | reset | quit\n")

    if candidates:
        with open("taz_death_candidates.json", "w", encoding="utf-8") as f:
            json.dump(sorted(f"0x{a:08X}" for a in candidates), f, indent=2)
        print(f"\n  Wrote {len(candidates)} candidate(s) to "
              f"taz_death_candidates.json\n")


if __name__ == "__main__":
    main()
