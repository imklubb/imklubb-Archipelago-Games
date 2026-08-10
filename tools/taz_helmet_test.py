"""
taz_helmet_test.py -- find what the helmet bosses count.

Dodge City and The Hindenbird are fought in a helmet, and unlike the other
three arenas nobody has found what they score. Winning and losing both have to
be readable before a boss loss can send a death link.

The method is the one that worked for the state field: compare known
conditions rather than sample and hope.

Run it next to Launcher.py with the AP client CLOSED:

    venv\\Scripts\\python.exe taz_helmet_test.py

    chain             what the helmet chain currently reads
    snap <name>       remember the arena's memory under a name
    find              words holding a small integer that differ between
                      snapshots -- a score looks exactly like this
    watch <addr>      follow one address as you play
    quit

Take snapshots at points where you know the score:

    snap start        the fight has just begun
    snap hit1         after taking one hit
    snap hit2         after two
    find

A word that reads 0, 1, 2 across those three is the enemy's score, and the
number it reaches when you lose is the losing condition.
"""

import struct
import sys
import time

try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  Run:  venv\\Scripts\\python.exe taz_helmet_test.py\n")
    sys.exit(1)

try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import logic as L
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"  Run this from the Archipelago folder, next to Launcher.py.")

# Where the other three arenas keep their scores. The helmet ones are very
# likely nearby, since the game allocates them the same way.
BANDS = [
    (0x0037D000, 0x00384000),   # Elephant Pong and Disco Volcano live here
    (0x00380000, 0x00381000),   # Gladiatoons' timer
    (0x0067C000, 0x0068E000),   # Gladiatoons' scores
]


def read_band(lo, hi):
    out = {}
    step = 4 * 192
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
    for lo, hi in BANDS:
        out.update(read_band(lo, hi))
    return out


def main():
    print(f"\n  world loaded from: {G.__file__}")
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the AP client still open?")
    g = G.Game()
    g.connected = True

    shots = {}
    print(f"  Scanning {sum(hi - lo for lo, hi in BANDS) // 1024} KB per "
          f"snapshot.\n")
    print("  In the arena: snap start, take a hit, snap hit1, take another,")
    print("  snap hit2, then find.\n")

    while True:
        try:
            cmd = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        low = cmd.lower()
        if low in ("quit", "q"):
            break

        if low == "chain":
            lid = g.level_id()
            print(f"    level {lid} "
                  f"({L.LEVEL_IDS.get(lid, '?')}), "
                  f"boss: {g.is_boss()}")
            try:
                taz = G.mem.read_u32(G.TAZ_PTR)
                cos = G.mem.read_u32(taz + G.O_COSTUME_PTR)
                helm = G.mem.read_u32(cos + 0x1D0)
                print(f"    costume 0x{cos:08X}, helmet 0x{helm:08X} "
                      f"{'valid' if G.mem.valid_ptr(helm) else 'NOT A POINTER'}")
                if G.mem.valid_ptr(helm):
                    raw = G.mem.read_bytes(helm, 0x40)
                    for i in range(0, 0x40, 16):
                        vals = " ".join(f"{raw[i+j]:02X}" for j in range(16))
                        print(f"      +{i:#04x}  {vals}")
            except Exception as exc:
                print(f"    could not read the chain: {exc}")
            print()
            continue

        if low.startswith("snap"):
            parts = cmd.split()
            if len(parts) < 2:
                print("    usage: snap <name>\n")
                continue
            t = time.time()
            shots[parts[1].lower()] = snapshot()
            print(f"    saved '{parts[1].lower()}' ({time.time() - t:.1f}s), "
                  f"have: {', '.join(sorted(shots))}\n")
            continue

        if low == "find":
            if len(shots) < 2:
                print("    take at least two snapshots first\n")
                continue
            names = sorted(shots)
            keys = set(shots[names[0]])
            for n in names[1:]:
                keys &= set(shots[n])
            hits = []
            for a in keys:
                vals = [shots[n][a] for n in names]
                # A score is a small integer that changes. Anything large is a
                # pointer, a float, or a timer.
                if all(v < 100 for v in vals) and len(set(vals)) >= 2:
                    hits.append((a, vals))
            print(f"\n    {'address':<12} " +
                  "  ".join(f"{n[:8]:>8}" for n in names))
            ranked = sorted(hits, key=lambda h: -len(set(h[1])))
            for a, vals in ranked[:30]:
                cells = "  ".join(f"{v:>8}" for v in vals)
                print(f"    0x{a:08X}  {cells}")
            if not hits:
                print("    nothing -- the score may be outside these bands, "
                      "or wider than a small integer")
            print(f"\n    {len(hits)} candidate(s). One that counts up with "
                  f"the hits you took is the enemy score.\n")
            continue

        if low.startswith("watch"):
            try:
                addr = int(cmd.split()[1], 0)
            except (IndexError, ValueError):
                print("    usage: watch 0x37d8fc\n")
                continue
            print(f"\n    Following 0x{addr:08X}. Ctrl-C to stop.\n")
            last = None
            try:
                while True:
                    time.sleep(0.05)
                    try:
                        v = G.mem.read_u32(addr)
                    except Exception:
                        continue
                    if v != last:
                        print(f"      {v}")
                        last = v
            except KeyboardInterrupt:
                print("\n    ...stopped\n")
            continue

        if cmd:
            print("    chain | snap <name> | find | watch <addr> | quit\n")


if __name__ == "__main__":
    main()
