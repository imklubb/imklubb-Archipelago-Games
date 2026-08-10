"""
taz_addr.py -- turn the pointer chains into addresses you can actually type
into a memory viewer.

A chain like [[TAZ_PTR] + 0x1c8] + 0xb0 is not an address. It is a recipe:

    0x3FF060                     a fixed address holding a pointer
      read 4 bytes there    -> 0x0069BB50    Taz's object
    0x0069BB50 + 0x1C8 = 0x0069BD18
      read 4 bytes there    -> 0x006863F0    the state object
    0x006863F0 + 0xB0  = 0x006864A0          the state byte

Only the first address stays put. The rest move whenever the game reallocates,
which is why a chain works across loads and a fixed address does not.

Run it next to Launcher.py with the AP client CLOSED:

    venv\\Scripts\\python.exe taz_addr.py           resolve once
    venv\\Scripts\\python.exe taz_addr.py --follow  keep resolving as you play

Paste the resolved address into PCSX2's memory viewer -- but re-run this after
any load, because it will have moved.
"""

import argparse
import sys
import time

try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  Run:  venv\\Scripts\\python.exe taz_addr.py\n")
    sys.exit(1)

try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import logic as L
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"  Run this from the Archipelago folder, next to Launcher.py.")

# label, the offsets to walk from TAZ_PTR, and what the last one points at
CHAINS = [
    ("Taz object",     [],                       None),
    ("state object",   [G.O_STATE_PTR],          None),
    ("  state byte",   [G.O_STATE_PTR],          G.S_STATE),
    ("  animation",    [G.O_STATE_PTR],          G.S_ANIM),
    ("  request",      [G.O_STATE_PTR],          G.S_REQUEST),
    ("costume object", [G.O_COSTUME_PTR],        None),
    ("  costume byte", [G.O_COSTUME_PTR],        G.C_COSTUME),
    ("helmet object",  [G.O_COSTUME_PTR, 0x1D0], None),
    # The two fields the boss-loss code already zeroes. The object's own base
    # reads 0, which makes it look wrong -- these are where it counts.
    ("  helmet +0x08", [G.O_COSTUME_PTR, 0x1D0], 0x08),
    ("  helmet +0x0C", [G.O_COSTUME_PTR, 0x1D0], 0x0C),
    ("bonus object",   [G.O_BONUS_PTR],          None),
    ("position",       [],                       0xC0),
]


def resolve():
    """Walk each chain, returning label -> (address, value) or a reason."""
    out = []
    try:
        taz = G.mem.read_u32(G.TAZ_PTR)
    except Exception as exc:
        return [("TAZ_PTR", None, f"could not read: {exc}")]
    if not G.mem.valid_ptr(taz):
        return [("TAZ_PTR", None, "Taz does not exist -- load a level")]

    for label, offs, field in CHAINS:
        p = taz
        ok = True
        for off in offs:
            try:
                p = G.mem.read_u32(p + off)
            except Exception:
                ok = False
                break
            if not G.mem.valid_ptr(p):
                ok = False
                break
        if not ok:
            out.append((label, None, "chain broken here"))
            continue
        addr = p + field if field is not None else p
        val = ""
        if field is not None:
            # Both readings: a score is a word, a state is a byte, and showing
            # only one of them hides whichever it turns out to be.
            try:
                b = G.mem.read_u8(addr)
                w = G.mem.read_u32(addr)
                val = f"= 0x{b:02X} as a byte, {w} as a word"
            except Exception:
                val = ""
        out.append((label, addr, val))
    return out


def show(g):
    lid = g.level_id()
    where = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
    print(f"\n  {where}   (TAZ_PTR is 0x{G.TAZ_PTR:06X}, the only fixed one)\n")
    for label, addr, note in resolve():
        if addr is None:
            print(f"    {label:<16} {note}")
        else:
            print(f"    {label:<16} 0x{addr:08X}  {note}")
    print()


def hold(g, off, secs, via=None):
    """Zero three floats at Taz + off, repeatedly, and see if he stops.

    Freezing him has failed twice: pinning his POSITION removed the level's
    kill volumes, because a teleported object never crosses a trigger; and
    writing a state each tick just re-interrupted the animation and shook the
    camera.

    Zeroing velocity is the third idea and the most promising -- he is never
    moved, so collision behaves normally, he simply stops being pushed.

    Position is at 0xC0, so velocity is likely just after it. Try 0xCC, 0xD0,
    0xD8 and see which one holds him.
    """
    import struct
    where = f"[TAZ+{via:#x}] + {off:#x}" if via is not None \
        else f"Taz + {off:#x}"
    print(f"\n  Zeroing three floats at {where} for {secs:.0f}s.")
    print(f"  Try to run around. Ctrl-C to stop early.\n")
    end = time.time() + secs
    seen = None
    try:
        while time.time() < end:
            time.sleep(0.02)
            try:
                taz = G.mem.read_u32(G.TAZ_PTR)
                if not G.mem.valid_ptr(taz):
                    continue
                base = taz
                if via is not None:
                    # The promising fields live in an object Taz points at,
                    # not in Taz himself.
                    base = G.mem.read_u32(taz + via)
                    if not G.mem.valid_ptr(base):
                        continue
                a = base + off
                raw = G.mem.read_bytes(a, 12)
                vals = struct.unpack("<3f", raw)
                if seen is None or max(abs(v - w) for v, w
                                       in zip(vals, seen)) > 0.5:
                    seen = vals
                    print(f"      before zeroing: "
                          f"({vals[0]:>9.2f}, {vals[1]:>9.2f}, "
                          f"{vals[2]:>9.2f})")
                G.mem.write_floats(a, (0.0, 0.0, 0.0))
            except Exception:
                continue
    except KeyboardInterrupt:
        pass
    print("\n  Stopped. If he froze and the level still killed him, that is")
    print("  the field. If he kept moving, try the next offset.\n")


def pad(g, secs, addr=0x00514FC2, buttons=False):
    """Hold the controller neutral, which is the one thing that must stop him.

    Every attempt at freezing Taz from his own data has failed: the transform
    deforms him, states get re-driven every frame, and nothing reads as a
    plain velocity. Input is upstream of all of it -- if the game believes no
    direction is held, he has nothing to move towards.

    An earlier attempt at masking the pad was written at the client's polling
    rate and never landed. The geofences write every four milliseconds and
    work perfectly, so this does the same.

    Directions are ACTIVE LOW, so neutral is 0xFF -- writing zero would read
    as every direction held at once.
    """
    print(f"\n  Holding the pad neutral at 0x{addr:08X} for {secs:.0f}s.")
    print(f"  Mash every direction. Ctrl-C to stop early.\n")
    end = time.time() + secs
    wrote = 0
    try:
        while time.time() < end:
            try:
                G.mem.write_u8(addr, 0xFF)
                if buttons:
                    G.mem.write_u8(addr + 1, 0xFF)
                wrote += 1
            except Exception:
                pass
            time.sleep(0.004)
    except KeyboardInterrupt:
        pass
    print(f"  {wrote} write(s), about "
          f"{wrote / max(0.1, secs):.0f} a second.")
    print("  If he stood still and the level could still kill him, that is")
    print("  the freeze trap -- and it needs no knowledge of his physics.\n")


def offset_of(target):
    """Which fixed pointer reaches this address, and at what offset.

    The Gladiatoons scores move every load, so a raw address is only good for
    the session it was found in. What holds still is a pointer the game keeps
    at a fixed place plus a constant offset -- the same arrangement the state
    object and the helmet use.

    Find the address with a memory search, run this, then do it again next
    load. An offset that comes out the same both times is the answer.
    """
    print(f"\n  Looking for a pointer that reaches 0x{target:08X}")
    print(f"  Scanning 0x00100000-0x01000000; this takes a minute.\n")
    found = []
    # Widened well beyond the fixed globals. Every candidate in
    # 0x300000-0x600000 shifted by exactly the amount the score moved, which
    # means the pointer is not there at all -- Daffy's happened to be, and
    # Taz's evidently is not. A pointer inside the heap is perfectly normal;
    # it just costs more to find.
    for a in range(0x00100000, 0x01000000, 4):
        try:
            p = G.mem.read_u32(a)
        except Exception:
            continue
        if not G.mem.valid_ptr(p):
            continue
        d = target - p
        if -0x8000 < d < 0x8000:
            found.append((a, p, d))
    found.sort(key=lambda f: f[2])

    # Saved rather than printed, because the answer is usually below the
    # cutoff: 166 candidates with 25 shown means the one that matters is
    # very likely among the 141 you never saw. Comparing two saved runs
    # removes the eyeballing entirely.
    import json
    import os
    path = "taz_offsets.json"
    runs = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                runs = json.load(f)
        except Exception:
            runs = []
    runs.append({"target": target,
                 "hits": {f"{a:08X}": d for a, _, d in found}})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(runs[-4:], f)

    print(f"    {len(found)} candidate(s), saved as run #{len(runs)}.")
    for a, p, d in found[:10]:
        print(f"      [0x{a:08X}] = 0x{p:08X}   + {d:#07x}")
    if len(found) > 10:
        print(f"      ... and {len(found) - 10} more, all saved")

    if len(runs) >= 2:
        prev, cur = runs[-2], runs[-1]
        if prev["target"] != cur["target"]:
            same = {a: d for a, d in cur["hits"].items()
                    if prev["hits"].get(a) == d}
            print(f"\n    Compared with the previous run "
                  f"(0x{prev['target']:08X} -> 0x{cur['target']:08X}):\n")
            if same:
                for a, d in sorted(same.items(), key=lambda kv: kv[1])[:10]:
                    print(f"      [0x{a}] + {d:#07x}   <-- the offset HELD")
                print(f"\n      {len(same)} pointer(s) kept their offset.")
                print(f"      If there is exactly one, that is the answer.")
            else:
                print(f"      No pointer kept its offset. The score may sit")
                print(f"      behind a chain of two pointers rather than one.")
        else:
            print(f"\n    Same target as last run -- reload the fight and")
            print(f"    find the new address before comparing.")
    else:
        print(f"\n    Run this again after a reload to compare.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--follow", action="store_true",
                    help="keep resolving, so a load can be watched")
    ap.add_argument("--hold", default=None,
                    help="zero three floats at Taz + this offset, e.g. 0xD0")
    ap.add_argument("--secs", type=float, default=8.0)
    ap.add_argument("--via", default=None,
                    help="follow this offset from Taz first, e.g. 0x1C0")
    ap.add_argument("--offsetof", default=None,
                    help="find a fixed pointer that reaches this address")
    ap.add_argument("--pad", action="store_true",
                    help="hold the controller neutral instead")
    ap.add_argument("--pad-addr", default="0x00514FC2")
    ap.add_argument("--buttons", action="store_true",
                    help="also neutralise the face buttons")
    a = ap.parse_args()

    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the AP client still open?")
    g = G.Game()
    g.connected = True

    if a.offsetof:
        offset_of(int(a.offsetof, 0))
        return

    if a.pad:
        pad(g, a.secs, int(a.pad_addr, 0), a.buttons)
        return

    if a.hold:
        hold(g, int(a.hold, 0), a.secs,
             int(a.via, 0) if a.via else None)
        return

    if not a.follow:
        show(g)
        return

    print("\n  Following. Ctrl-C to stop. Watch these move across a load.\n")
    last = None
    try:
        while True:
            time.sleep(0.3)
            now = tuple(a for _, a, _ in resolve())
            if now != last:
                last = now
                show(g)
    except KeyboardInterrupt:
        print("  ...stopped\n")


if __name__ == "__main__":
    main()
